"""规则 DSL：词法 → 递归下降语法分析 → AST → 静态类型检查 → 求值（带命中追踪 + 反事实分析）。

不使用 eval，表达式只能访问特征字典里的字段和白名单函数，语法之外的任何东西都会被拒绝。

语法（优先级从低到高）：
    expr     := or_expr
    or_expr  := and_expr ( OR and_expr )*
    and_expr := not_expr ( AND not_expr )*
    not_expr := NOT not_expr | cmp
    cmp      := arith ( (== | != | > | >= | < | <=) arith
                      | [NOT] IN '[' literal (',' literal)* ']' )?
    arith    := term ( (+ | -) term )*
    term     := unary ( (* | /) unary )*
    unary    := '-' unary | atom
    atom     := NUMBER | STRING | true | false | null
              | IDENT [ '(' expr (',' expr)* ')' ]     -- 函数：abs / min / max / coalesce
              | '(' expr ')'

示例：
    device_order_count_1h > 5 AND is_new_account == true
    event_type IN ['order_create', 'order_pay'] AND NOT (ip_account_count_24h <= 2)
    refund_amount > amount * 0.8 AND coalesce(account_age_days, 0) < 30

三件事让规则「可回溯」而不只是「可执行」：
    1. 静态类型检查：保存前就拦下 refund_reason > 3、amount AND true 这类写错的规则；
    2. 条件级追踪：每个原子条件记录实际值、阈值、是否成立（求值不短路，回溯拿到完整条件）；
    3. 反事实：每个条件「差多少会翻转」，以及哪些条件是关键条件（单独翻转就改变规则结果）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Union


class DSLError(ValueError):
    """表达式语法或语义错误，pos 为出错字符位置。"""

    def __init__(self, message: str, pos: int = -1):
        super().__init__(message if pos < 0 else f"{message}（位置 {pos}）")
        self.pos = pos


# ---------------------------------------------------------------- 词法
_TOKEN_RE = re.compile(r"""
    (?P<WS>\s+)
  | (?P<NUMBER>\d+(?:\.\d+)?)
  | (?P<STRING>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<OP>==|!=|>=|<=|>|<)
  | (?P<ARITH>[-+*/])
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<LBRACK>\[)
  | (?P<RBRACK>\])
  | (?P<COMMA>,)
  | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
""", re.VERBOSE)

_KEYWORDS = {"AND", "OR", "NOT", "IN", "TRUE", "FALSE", "NULL"}
FUNCTIONS = {"abs": (1, 1), "min": (2, 8), "max": (2, 8), "coalesce": (2, 8)}


@dataclass
class Token:
    kind: str
    value: Any
    pos: int


def tokenize(src: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i < len(src):
        m = _TOKEN_RE.match(src, i)
        if not m:
            raise DSLError(f"无法识别的字符 {src[i]!r}", i)
        kind = m.lastgroup
        text = m.group()
        if kind == "WS":
            pass
        elif kind == "NUMBER":
            tokens.append(Token("NUMBER", float(text) if "." in text else int(text), i))
        elif kind == "STRING":
            body = text[1:-1]
            tokens.append(Token("STRING", re.sub(r"\\(.)", r"\1", body), i))
        elif kind == "IDENT":
            up = text.upper()
            if up in _KEYWORDS:
                if up in ("TRUE", "FALSE"):
                    tokens.append(Token("BOOL", up == "TRUE", i))
                elif up == "NULL":
                    tokens.append(Token("NULL", None, i))
                else:
                    tokens.append(Token(up, up, i))
            else:
                tokens.append(Token("IDENT", text, i))
        else:
            tokens.append(Token(kind, text, i))
        i = m.end()
    tokens.append(Token("EOF", None, len(src)))
    return tokens


# ---------------------------------------------------------------- AST
@dataclass
class Literal:
    value: Any


@dataclass
class Var:
    name: str
    cid: int = -1                 # 作为布尔条件单独出现时的条件编号


@dataclass
class Arith:
    op: str                       # + - * / neg
    left: "Node"
    right: "Node | None" = None


@dataclass
class Call:
    func: str
    args: list["Node"] = field(default_factory=list)


@dataclass
class Compare:
    op: str                       # == != > >= < <= in not_in
    left: "Node"
    right: "Node"
    text: str = ""                # 原始片段，用于回溯展示
    cid: int = -1                 # 条件编号（反事实分析时按编号强制翻转）


@dataclass
class Not:
    operand: "Node"


@dataclass
class BoolOp:
    op: str                       # AND / OR
    operands: list["Node"] = field(default_factory=list)


Node = Union[Literal, Var, Arith, Call, Compare, Not, BoolOp]


# ---------------------------------------------------------------- 语法分析
class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = tokenize(src)
        self.i = 0
        self.cid = 0

    def _next_cid(self) -> int:
        self.cid += 1
        return self.cid - 1

    def peek(self, k: int = 0) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def take(self, kind: str | None = None) -> Token:
        t = self.toks[self.i]
        if kind and t.kind != kind:
            want = {"RPAREN": "')'", "RBRACK": "']'", "LBRACK": "'['", "EOF": "表达式结束"}.get(kind, kind)
            got = "表达式结束" if t.kind == "EOF" else repr(t.value)
            raise DSLError(f"这里应为 {want}，实际是 {got}", t.pos)
        self.i += 1
        return t

    def parse(self) -> Node:
        if self.peek().kind == "EOF":
            raise DSLError("表达式为空", 0)
        node = self.or_expr()
        self.take("EOF")
        return node

    def or_expr(self) -> Node:
        items = [self.and_expr()]
        while self.peek().kind == "OR":
            self.take()
            items.append(self.and_expr())
        return items[0] if len(items) == 1 else BoolOp("OR", items)

    def and_expr(self) -> Node:
        items = [self.not_expr()]
        while self.peek().kind == "AND":
            self.take()
            items.append(self.not_expr())
        return items[0] if len(items) == 1 else BoolOp("AND", items)

    def not_expr(self) -> Node:
        if self.peek().kind == "NOT" and self.peek(1).kind != "IN":
            self.take()
            return Not(self.not_expr())
        return self.cmp()

    def cmp(self) -> Node:
        start = self.peek().pos
        left = self.arith()
        t = self.peek()
        if t.kind == "OP":
            self.take()
            right = self.arith()
            return Compare(t.value, left, right, self._slice(start), self._next_cid())
        negate = False
        if t.kind == "NOT" and self.peek(1).kind == "IN":
            self.take()
            negate = True
            t = self.peek()
        if t.kind == "IN":
            self.take()
            self.take("LBRACK")
            values = [self.literal()]
            while self.peek().kind == "COMMA":
                self.take()
                values.append(self.literal())
            self.take("RBRACK")
            return Compare("not_in" if negate else "in", left,
                           Literal([v.value for v in values]), self._slice(start), self._next_cid())
        if isinstance(left, Var):
            left.cid = self._next_cid()
        return left

    def arith(self) -> Node:
        node = self.term()
        while self.peek().kind == "ARITH" and self.peek().value in "+-":
            op = self.take().value
            node = Arith(op, node, self.term())
        return node

    def term(self) -> Node:
        node = self.unary()
        while self.peek().kind == "ARITH" and self.peek().value in "*/":
            op = self.take().value
            node = Arith(op, node, self.unary())
        return node

    def unary(self) -> Node:
        if self.peek().kind == "ARITH" and self.peek().value == "-":
            self.take()
            inner = self.unary()
            if isinstance(inner, Literal) and _is_num(inner.value):
                return Literal(-inner.value)
            return Arith("neg", inner)
        return self.atom()

    def _slice(self, start: int) -> str:
        end = self.toks[self.i].pos if self.i < len(self.toks) else len(self.src)
        return self.src[start:end].strip()

    def literal(self) -> Literal:
        t = self.peek()
        if t.kind == "ARITH" and t.value == "-" and self.peek(1).kind == "NUMBER":
            self.take()
            return Literal(-self.take().value)
        if t.kind in ("NUMBER", "STRING", "BOOL", "NULL"):
            self.take()
            return Literal(t.value)
        raise DSLError("列表里只能放常量", t.pos)

    def atom(self) -> Node:
        t = self.peek()
        if t.kind in ("NUMBER", "STRING", "BOOL", "NULL"):
            return self.literal()
        if t.kind == "IDENT":
            self.take()
            if self.peek().kind != "LPAREN":
                return Var(t.value)
            if t.value not in FUNCTIONS:
                raise DSLError(f"不支持的函数 {t.value}（可用：{', '.join(FUNCTIONS)}）", t.pos)
            self.take("LPAREN")
            args = [self.or_expr()]
            while self.peek().kind == "COMMA":
                self.take()
                args.append(self.or_expr())
            self.take("RPAREN")
            lo, hi = FUNCTIONS[t.value]
            if not lo <= len(args) <= hi:
                raise DSLError(f"函数 {t.value} 需要 {lo}{'' if lo == hi else f'~{hi}'} 个参数，实际 {len(args)} 个", t.pos)
            return Call(t.value, args)
        if t.kind == "LPAREN":
            self.take()
            node = self.or_expr()
            self.take("RPAREN")
            return node
        got = "表达式结束" if t.kind == "EOF" else repr(t.value)
        raise DSLError(f"这里应为字段、常量或 '('，实际是 {got}", t.pos)


def parse(src: str) -> Node:
    return _Parser(src).parse()


def _children(node: Node) -> list[Node]:
    if isinstance(node, (Compare,)):
        return [node.left, node.right]
    if isinstance(node, Arith):
        return [node.left] + ([node.right] if node.right is not None else [])
    if isinstance(node, Call):
        return list(node.args)
    if isinstance(node, Not):
        return [node.operand]
    if isinstance(node, BoolOp):
        return list(node.operands)
    return []


def variables(node: Node) -> set[str]:
    if isinstance(node, Var):
        return {node.name}
    out: set[str] = set()
    for c in _children(node):
        out |= variables(c)
    return out


def source(node: Node) -> str:
    """AST → 规范化文本（展示用）。"""
    if isinstance(node, Literal):
        v = node.value
        return "null" if v is None else ("true" if v is True else "false" if v is False else
                                         repr(v) if isinstance(v, (str, list)) else str(v))
    if isinstance(node, Var):
        return node.name
    if isinstance(node, Arith):
        if node.op == "neg":
            return f"-{source(node.left)}"
        return f"({source(node.left)} {node.op} {source(node.right)})"
    if isinstance(node, Call):
        return f"{node.func}({', '.join(source(a) for a in node.args)})"
    if isinstance(node, Compare):
        return node.text or f"{source(node.left)} {node.op} {source(node.right)}"
    if isinstance(node, Not):
        return f"NOT ({source(node.operand)})"
    return f" {node.op} ".join(f"({source(n)})" if isinstance(n, BoolOp) else source(n) for n in node.operands)


# ---------------------------------------------------------------- 静态类型检查
NUMBER, BOOL, STRING, NULL, ANY = "number", "bool", "string", "null", "any"
_CN = {NUMBER: "数值", BOOL: "布尔", STRING: "字符串", NULL: "null", ANY: "任意"}


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _lit_type(v: Any) -> str:
    if v is None:
        return NULL
    if isinstance(v, bool):
        return BOOL
    if _is_num(v):
        return NUMBER
    if isinstance(v, str):
        return STRING
    return ANY


def _compatible(a: str, b: str) -> bool:
    return a == b or ANY in (a, b) or NULL in (a, b)


def typecheck(node: Node, types: dict[str, str], *, want_bool: bool = True) -> str:
    """推导表达式类型；类型不对直接抛 DSLError。types：字段 → number/bool/string。"""
    t = _infer(node, types)
    if want_bool and t not in (BOOL, ANY):
        raise DSLError(f"整条规则的结果必须是布尔条件，'{source(node)}' 是{_CN[t]}")
    return t


def _need_bool(node: Node, types: dict[str, str], where: str) -> None:
    t = _infer(node, types)
    if t not in (BOOL, ANY):
        hint = f"，请写成 {source(node)} > 0 之类的比较" if t == NUMBER else ""
        raise DSLError(f"{where}的操作数必须是布尔条件，'{source(node)}' 是{_CN[t]}{hint}")


def _infer(node: Node, types: dict[str, str]) -> str:
    if isinstance(node, Literal):
        return _lit_type(node.value)
    if isinstance(node, Var):
        return types.get(node.name, ANY)
    if isinstance(node, Arith):
        for side in _children(node):
            st = _infer(side, types)
            if st not in (NUMBER, ANY):
                raise DSLError(f"算术运算 {'取负' if node.op == 'neg' else node.op} 只能用于数值，"
                               f"'{source(side)}' 是{_CN[st]}")
        return NUMBER
    if isinstance(node, Call):
        ts = [_infer(a, types) for a in node.args]
        if node.func in ("abs", "min", "max"):
            for a, at in zip(node.args, ts):
                if at not in (NUMBER, ANY):
                    raise DSLError(f"函数 {node.func} 只接受数值，'{source(a)}' 是{_CN[at]}")
            return NUMBER
        concrete = {x for x in ts if x not in (NULL, ANY)}
        if len(concrete) > 1:
            raise DSLError(f"coalesce 的参数类型必须一致，实际有 {'、'.join(_CN[x] for x in sorted(concrete))}")
        return concrete.pop() if concrete else ANY
    if isinstance(node, Compare):
        lt = _infer(node.left, types)
        if node.op in ("in", "not_in"):
            for v in node.right.value:
                if not _compatible(lt, _lit_type(v)):
                    raise DSLError(f"'{source(node.left)}' 是{_CN[lt]}，IN 列表里的 {v!r} 是{_CN[_lit_type(v)]}")
            return BOOL
        rt = _infer(node.right, types)
        if node.op in (">", ">=", "<", "<="):
            for side, st in ((node.left, lt), (node.right, rt)):
                if st not in (NUMBER, ANY):
                    raise DSLError(f"只有数值能比较大小：'{node.text}' 里的 '{source(side)}' 是{_CN[st]}")
        elif not _compatible(lt, rt):
            raise DSLError(f"类型不匹配：'{source(node.left)}' 是{_CN[lt]}，'{source(node.right)}' 是{_CN[rt]}")
        return BOOL
    if isinstance(node, Not):
        _need_bool(node.operand, types, "NOT ")
        return BOOL
    for n in node.operands:
        _need_bool(n, types, f"{node.op} ")
    return BOOL


def validate(src: str, known_fields: set[str] | None = None, types: dict[str, str] | None = None) -> Node:
    """解析 + 字段名校验 + 类型检查；返回 AST。保存规则前调用。"""
    node = parse(src)
    if known_fields is not None:
        unknown = sorted(variables(node) - known_fields)
        if unknown:
            raise DSLError(f"未知字段：{', '.join(unknown)}")
    if types is not None:
        typecheck(node, types)
    return node


# ---------------------------------------------------------------- 求值
_CMP = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


def _value(node: Node, ctx: dict[str, Any]) -> Any:
    """取值。缺失特征（None）参与算术时结果为 None，比较时条件不成立。"""
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Var):
        return ctx.get(node.name)
    if isinstance(node, Arith):
        a = _value(node.left, ctx)
        if node.op == "neg":
            return -a if _is_num(a) else None
        b = _value(node.right, ctx)
        if not (_is_num(a) and _is_num(b)):
            return None
        if node.op == "/":
            return None if b == 0 else a / b
        return a + b if node.op == "+" else a - b if node.op == "-" else a * b
    if isinstance(node, Call):
        vals = [_value(a, ctx) for a in node.args]
        if node.func == "coalesce":
            return next((v for v in vals if v is not None), None)
        if any(not _is_num(v) for v in vals):
            return None
        return abs(vals[0]) if node.func == "abs" else min(vals) if node.func == "min" else max(vals)
    return evaluate(node, ctx)


def evaluate(node: Node, ctx: dict[str, Any], trace: list[dict] | None = None,
             overrides: dict[int, bool] | None = None) -> bool:
    """求值。trace 不为 None 时记录每个原子条件；overrides 按条件编号强制结果（反事实分析用）。"""
    if isinstance(node, BoolOp):
        if node.op == "AND":
            result = True
            for n in node.operands:            # 回溯需要完整条件，所以不短路
                result = evaluate(n, ctx, trace, overrides) and result
            return result
        result = False
        for n in node.operands:
            result = evaluate(n, ctx, trace, overrides) or result
        return result
    if isinstance(node, Not):
        return not evaluate(node.operand, ctx, trace, overrides)
    if isinstance(node, Compare):
        left = _value(node.left, ctx)
        right = _value(node.right, ctx)
        if node.op in ("in", "not_in"):
            ok = left in right
            ok = ok if node.op == "in" else not ok
        else:
            try:
                ok = bool(_CMP[node.op](left, right))
            except TypeError:               # None > 5 之类：缺特征时条件不成立
                ok = False
        actual = ok
        if overrides and node.cid in overrides:
            ok = overrides[node.cid]
        if trace is not None:
            trace.append({
                "cid": node.cid,
                "condition": node.text,
                "field": node.left.name if isinstance(node.left, Var) else None,
                "expr": source(node.left),
                "op": node.op,
                "actual": left,
                "threshold": right,
                "matched": actual,
                "flip": flip_point(node.op, left, right, actual),
            })
        return ok
    if isinstance(node, Var):
        v = ctx.get(node.name)
        ok = bool(v)
        actual = ok
        if overrides and node.cid in overrides:
            ok = overrides[node.cid]
        if trace is not None and node.cid >= 0:
            trace.append({"cid": node.cid, "condition": node.name, "field": node.name, "expr": node.name,
                          "op": "truthy", "actual": v, "threshold": True, "matched": actual,
                          "flip": {"to": f"{node.name} == {'false' if actual else 'true'}", "distance": None,
                                   "target": not actual}})
        return ok
    return bool(_value(node, ctx))


# ---------------------------------------------------------------- 反事实
_NEG = {">": "<=", ">=": "<", "<": ">=", "<=": ">", "==": "!=", "!=": "==", "in": "not_in", "not_in": "in"}


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return repr(v) if isinstance(v, str) else str(v)


def flip_point(op: str, actual: Any, threshold: Any, matched: bool) -> dict[str, Any] | None:
    """这个条件要翻转，左侧的值需要变成多少。

    返回 {to: 翻转后的条件, target: 最近的翻转值, distance: 需要改变的量, direction: up/down}；
    缺特征时返回 None。整数特征给出整数目标（计数类特征最常见）。
    """
    if actual is None:
        return None
    want = _NEG[op] if matched else op
    if not (_is_num(actual) and _is_num(threshold)) or want in ("in", "not_in", "==", "!="):
        return {"to": f"{want} {_fmt(threshold)}", "target": None, "distance": None, "direction": None}
    integer = isinstance(actual, int) and float(threshold) == int(threshold) or (
        isinstance(actual, int) and want in (">", ">=", "<", "<="))
    if want == "<":
        target = math.ceil(threshold) - 1 if integer else threshold
    elif want == "<=":
        target = math.floor(threshold) if integer else threshold
    elif want == ">":
        target = math.floor(threshold) + 1 if integer else threshold
    else:
        target = math.ceil(threshold) if integer else threshold
    strict = not integer and want in ("<", ">")
    distance = abs(target - actual)
    direction = "down" if target < actual or (target == actual and want == "<") else "up"
    return {"to": f"{want} {_fmt(threshold)}", "target": target, "distance": round(distance, 4),
            "direction": direction, "strict": strict}


def _conditions(node: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node, bool_ctx: bool):
        if isinstance(n, Compare):
            out.append(n)
        elif isinstance(n, Var):
            if bool_ctx and n.cid >= 0:
                out.append(n)
        elif isinstance(n, (BoolOp, Not)):
            for c in _children(n):
                walk(c, True)
    walk(node, True)
    return out


def counterfactual(node: Node, ctx: dict[str, Any]) -> dict[str, Any]:
    """反事实分析：哪些条件是关键条件（单独翻转即改变规则结果），以及最小改动。

    思路：先正常求值得到每个条件的真值，再对每个条件强制取反重新求值；
    结果变了就是关键条件。对命中的 AND 规则，所有条件都是关键条件；
    对 OR 规则，两个分支同时成立时任何一个都不关键——这正是"只看阈值"看不出来的。
    """
    trace: list[dict] = []
    result = evaluate(node, ctx, trace)
    by_cid = {t["cid"]: t for t in trace}
    items = []
    for c in _conditions(node):
        t = by_cid.get(c.cid)
        if t is None:
            continue
        flipped = evaluate(node, ctx, None, {c.cid: not t["matched"]})
        item = dict(t, critical=flipped != result)
        f = t["flip"]
        if f and f.get("distance") is not None and _is_num(t["threshold"]):
            item["relative"] = round(f["distance"] / max(abs(float(t["threshold"])), 1.0), 4)
        items.append(item)
    crit = [i for i in items if i["critical"]]
    best = min((i for i in crit if i.get("relative") is not None), key=lambda i: i["relative"], default=None)
    if best is None and crit:
        best = crit[0]
    return {"matched": result, "conditions": items, "critical": [i["cid"] for i in crit],
            "minimal_change": _describe(best, result) if best else None}


def _describe(item: dict[str, Any], matched: bool) -> dict[str, Any]:
    f = item["flip"] or {}
    who = item["field"] or item["expr"]
    verb = "不再命中" if matched else "就会命中"
    if f.get("target") is not None:
        move = "降到" if f["direction"] == "down" else "升到"
        tgt = ("低于 " if f["direction"] == "down" else "高于 ") + _fmt(f["target"]) if f.get("strict") \
            else _fmt(f["target"])
        text = f"{who} 当前 {_fmt(item['actual'])}，{move} {tgt}，规则{verb}"
    elif item["actual"] is None:
        text = f"{who} 缺失，补齐后才可能改变结果"
    else:
        text = f"{who} 当前 {_fmt(item['actual'])}，满足 {who} {f.get('to', '')} 时规则{verb}"
    return {"cid": item["cid"], "field": who, "text": text, "flip": f}


# ---------------------------------------------------------------- 条件树（前端可视化）
def explain_tree(node: Node, ctx: dict[str, Any]) -> dict[str, Any]:
    """带求值结果的条件树：每个节点的真值，叶子带实际值、阈值、翻转点。"""
    if isinstance(node, BoolOp):
        kids = [explain_tree(n, ctx) for n in node.operands]
        ok = all(k["matched"] for k in kids) if node.op == "AND" else any(k["matched"] for k in kids)
        return {"type": node.op.lower(), "matched": ok, "children": kids}
    if isinstance(node, Not):
        kid = explain_tree(node.operand, ctx)
        return {"type": "not", "matched": not kid["matched"], "children": [kid]}
    trace: list[dict] = []
    ok = evaluate(node, ctx, trace)
    leaf = trace[0] if trace else {"condition": source(node), "actual": _value(node, ctx)}
    return {"type": "cond", "matched": ok, **leaf}


def to_json(node: Node) -> dict[str, Any]:
    """AST 序列化（前端画语法树 / 规则对比用）。"""
    if isinstance(node, Literal):
        return {"type": "literal", "value": node.value}
    if isinstance(node, Var):
        return {"type": "var", "name": node.name}
    if isinstance(node, Arith):
        return {"type": "arith", "op": node.op, "args": [to_json(c) for c in _children(node)]}
    if isinstance(node, Call):
        return {"type": "call", "func": node.func, "args": [to_json(a) for a in node.args]}
    if isinstance(node, Compare):
        return {"type": "compare", "op": node.op, "text": node.text, "cid": node.cid,
                "left": to_json(node.left), "right": to_json(node.right)}
    if isinstance(node, Not):
        return {"type": "not", "operand": to_json(node.operand)}
    return {"type": node.op.lower(), "operands": [to_json(n) for n in node.operands]}


class CompiledRule:
    """缓存好的规则（避免每次决策重复解析）。"""

    __slots__ = ("code", "name", "score", "event_types", "expression", "ast", "priority")

    def __init__(self, code: str, name: str, expression: str, score: int,
                 event_types: list[str] | None = None, priority: int = 100):
        self.code = code
        self.name = name
        self.expression = expression
        self.score = score
        self.event_types = set(event_types or [])
        self.priority = priority
        self.ast = parse(expression)

    def applies_to(self, event_type: str) -> bool:
        return not self.event_types or event_type in self.event_types

    def run(self, ctx: dict[str, Any]) -> tuple[bool, list[dict]]:
        trace: list[dict] = []
        return evaluate(self.ast, ctx, trace), trace

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "expression": self.expression, "score": self.score,
                "event_types": sorted(self.event_types), "priority": self.priority}
