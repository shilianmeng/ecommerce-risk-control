"""RAG 审核辅助：知识库检索 + （可选）微调大模型生成。

- 知识库：app/data/knowledge_base.json（政策 / 判例 / FAQ）+ 规则表自动生成的规则说明；
  启动时 upsert 到 knowledge_doc 表。微调项目用的是同一份语料。
- 检索：中文字符 bigram + 词元的 BM25，命中规则编号额外加权。零依赖，几百条文档毫秒级。
- 生成：配置了 RAG_LLM_URL（OpenAI 兼容接口，例如 vLLM 部署的 Qwen2.5 + LoRA）时调用大模型，
  要求只依据给定片段作答、标注引用编号、资料不足时拒答；未配置或调用失败时降级为检索模式，
  用模板把命中规则说明、相似判例、处置规范拼成建议，审核流程不受影响。
"""
from __future__ import annotations

import json
import logging
import math
import re
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.core import config
from app.models import Decision, KnowledgeDoc, RiskCase, Rule

log = logging.getLogger(__name__)
KB_FILE = Path(__file__).resolve().parents[1] / "data" / "knowledge_base.json"

SYSTEM_PROMPT = (
    "你是电商风控审核助手。只能依据【参考资料】回答，每个结论后用 [编号] 标注出处；"
    "资料中没有的信息不得编造；如果资料不足以回答，直接回答：根据现有资料无法确定。"
)


# ------------------------------------------------------------------ 知识库
def rule_doc(r: Rule) -> dict[str, Any]:
    level = "可直接拒绝" if r.score >= 80 else "单独命中即进入人审" if r.score >= 60 else "需与其他规则叠加"
    return {"doc_code": r.code, "category": "rule", "title": f"规则 {r.code} {r.name}",
            "tags": [r.code, r.name] + list(r.event_types),
            "content": f"规则 {r.code}「{r.name}」：{r.description or r.name}。表达式：{r.expression}。"
                       f"命中加 {r.score} 分，{level}。适用事件：{'、'.join(r.event_types) or '全部事件'}。"}


def ensure_knowledge(session: Session) -> int:
    docs = json.loads(KB_FILE.read_text(encoding="utf-8"))
    docs += [rule_doc(r) for r in session.exec(select(Rule).where(Rule.enabled == True)).all()]
    existing = {d.doc_code: d for d in session.exec(select(KnowledgeDoc)).all()}
    active_codes = {d["doc_code"] for d in docs}
    for row in existing.values():
        if row.category == "rule" and row.doc_code not in active_codes:
            session.delete(row)
    for d in docs:
        row = existing.get(d["doc_code"])
        if row is None:
            session.add(KnowledgeDoc(**d))
        elif (row.title, row.content, row.tags) != (d["title"], d["content"], d["tags"]):
            row.title, row.content, row.tags, row.category = d["title"], d["content"], d["tags"], d["category"]
            session.add(row)
    _INDEX.clear()
    return len(docs)


# ------------------------------------------------------------------ 检索
_CODE = re.compile(r"[A-Z]-\d{3}")


def tokenize(text: str) -> list[str]:
    text = text or ""
    toks = [m.lower() for m in re.findall(r"[A-Za-z]+[-_]?\d*|\d+", text)]
    han = re.findall(r"[\u4e00-\u9fff]+", text)
    for seg in han:
        toks += [seg[i:i + 2] for i in range(len(seg) - 1)] or [seg]
    return toks


class _Bm25:
    def __init__(self, docs: list[KnowledgeDoc], k1: float = 1.4, b: float = 0.75):
        self.docs = docs
        self.tf = [Counter(tokenize(f"{d.title} {' '.join(d.tags or [])} {d.content}")) for d in docs]
        self.len = [sum(t.values()) for t in self.tf]
        self.avg = sum(self.len) / max(len(docs), 1)
        df = Counter(w for t in self.tf for w in t)
        n = len(docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.k1, self.b = k1, b

    def search(self, query: str, k: int = 5, boost_codes: set[str] | None = None) -> list[tuple[KnowledgeDoc, float]]:
        q = Counter(tokenize(query))
        out = []
        for i, d in enumerate(self.docs):
            s = 0.0
            for w in q:
                f = self.tf[i].get(w)
                if f:
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            codes = set(_CODE.findall(f"{d.doc_code} {' '.join(d.tags or [])}"))
            if boost_codes and codes & boost_codes:
                s += 8.0 if d.doc_code in boost_codes else 3.0
            if s > 0:
                out.append((d, round(s, 3)))
        return sorted(out, key=lambda x: -x[1])[:k]


_INDEX: dict[str, _Bm25] = {}


def _index(session: Session) -> _Bm25:
    # ponytail: 当前知识库较小，每次读取已提交内容，避免跨事务/进程缓存陈旧。
    return _Bm25(list(session.exec(select(KnowledgeDoc)).all()))


def search(session: Session, query: str, k: int = 5, codes: set[str] | None = None) -> list[dict[str, Any]]:
    return [{"doc_code": d.doc_code, "category": d.category, "title": d.title, "content": d.content, "score": s}
            for d, s in _index(session).search(query, k, codes)]


# ------------------------------------------------------------------ 审核辅助
EVENT_CN = {"login": "登录", "coupon_receive": "领券", "order_create": "下单", "order_pay": "支付",
            "after_sale_apply": "售后申请"}


def case_query(case: RiskCase, d: Decision) -> tuple[str, set[str]]:
    codes = {h["code"] for h in d.hits if _CODE.fullmatch(h["code"])}
    names = " ".join(h["name"] for h in d.hits)
    level = {"REJECT": "拒绝", "REVIEW": "人审"}.get(d.decision, "通过")
    q = f"{EVENT_CN.get(case.event_type, case.event_type)} {names} {' '.join(codes)} {level} 处置 判例"
    return q, codes


def call_llm(question: str, refs: list[dict[str, Any]]) -> str:
    # 与微调训练数据同一格式：[n]《标题》（文档编号）：内容；模型输出用 [n] 引用，再映射回文档编号
    ctx = "\n".join(f"[{i}]《{r['title']}》（{r['doc_code']}）：{r['content']}" for i, r in enumerate(refs, 1))
    body = {"model": config.RAG_LLM_MODEL, "temperature": 0.2, "max_tokens": 600,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": f"【参考资料】\n{ctx}\n\n【问题】\n{question}"}]}
    req = urllib.request.Request(config.RAG_LLM_URL.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=config.RAG_TIMEOUT_SECONDS) as resp:
        text = json.loads(resp.read())["choices"][0]["message"]["content"].strip()
    return re.sub(r"\[(\d+)\]", lambda m: f"[{refs[int(m.group(1)) - 1]['doc_code']}]"
                  if 0 < int(m.group(1)) <= len(refs) else m.group(0), text)


def template_answer(case: RiskCase, d: Decision, refs: list[dict[str, Any]]) -> str:
    lines = [f"【决策】{EVENT_CN.get(case.event_type)}事件，风险分 {d.score}，结论 {d.decision}。"]
    if d.hits:
        lines.append("【命中规则】")
        for h in d.hits:
            unmet = [c for c in h.get("conditions", []) if c.get("matched")]
            facts = "，".join(f"{c['field']}={c['actual']}" for c in unmet[:3])
            lines.append(f"- {h['code']} {h['name']}（+{h['score']}）{('：' + facts) if facts else ''} [{h['code']}]")
    cases = [r for r in refs if r["category"] == "case"]
    if cases:
        lines.append("【相似判例】")
        lines += [f"- {r['title'].replace('判例：', '')}：{_conclusion(r['content'])} [{r['doc_code']}]" for r in cases[:2]]
    lines.append("【建议】")
    if d.decision == "REJECT":
        lines.append("- 证据较强，核对共用设备/收货手机号后，可确认欺诈并拦截订单；团伙特征明显时封禁设备。[P-003]")
    else:
        lines.append("- 证据不足以直接定性，先核查共用设备与收货信息；仅 IP 关联时注意公共网络误判。[P-009]")
    lines.append("- 当前为检索模式（未接入大模型），以上为依据知识库片段的整理，请审核员最终判断。")
    return "\n".join(lines)


def _conclusion(text: str) -> str:
    m = re.search(r"审核结论：([^。]+)。", text)
    return m.group(1) if m else text[:30]


def assist(session: Session, case: RiskCase, question: str | None = None) -> dict[str, Any]:
    d = session.get(Decision, case.decision_id)
    q, codes = case_query(case, d)
    if question:
        q = f"{question} {q}"
    refs = search(session, q, k=6, codes=codes)
    mode, answer = "retrieval", None
    if config.RAG_LLM_URL:
        hits = "；".join(f"{h['code']} {h['name']} +{h['score']}" for h in d.hits) or "无"
        prompt = question or (f"案件 {case.case_no}：{EVENT_CN.get(case.event_type)}事件，风险分 {d.score}，"
                              f"命中规则：{hits}。请解释命中原因、给出相似判例和处置建议。")
        try:
            answer, mode = call_llm(prompt, refs), "llm"
        except Exception as e:     # 大模型旁路：失败不影响审核
            log.warning("RAG 大模型调用失败，降级检索模式：%s", e)
    if answer is None:
        answer = template_answer(case, d, refs)
    return {"mode": mode, "query": q, "answer": answer, "references": refs}
