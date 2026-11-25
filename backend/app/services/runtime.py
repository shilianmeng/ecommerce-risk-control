"""运行时组件容器：Redis、特征引擎、图谱、规则缓存、名单缓存。

规则和名单在内存里缓存（决策热路径不查库），增删改后调用 reload_* 刷新。
多实例部署时可通过 Redis 发布订阅广播刷新，这里单实例直接刷新。
"""
from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app.core import config
from app.engine.dsl import CompiledRule
from app.engine.features import FeatureEngine
from app.engine.graph import GraphStore, build_graph_store
from app.models import NameList, Rule

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, redis_client, graph: GraphStore):
        self.redis = redis_client
        self.features = FeatureEngine(redis_client, config.REDIS_PREFIX)
        self.graph = graph
        self._lock = threading.RLock()
        self.rules: list[CompiledRule] = []
        self.rule_version = ""
        self.lists: dict[tuple[str, str], list[NameList]] = {}

    # ---------------- 规则
    def reload_rules(self, session: Session) -> None:
        rows = session.exec(select(Rule).where(Rule.enabled == True).order_by(Rule.priority, Rule.code)).all()  # noqa: E712
        compiled = []
        for r in rows:
            try:
                compiled.append(CompiledRule(r.code, r.name, r.expression, r.score, r.event_types, r.priority))
            except Exception as e:      # 库里有坏规则也不能拖垮决策，跳过并告警
                log.error("规则 %s 解析失败，已跳过：%s", r.code, e)
        sig = ",".join(f"{r.code}@{r.version}" for r in rows)
        with self._lock:
            self.rules = compiled
            self.rule_version = hashlib.sha1(sig.encode()).hexdigest()[:12] if sig else "empty"

    # ---------------- 名单
    def reload_lists(self, session: Session) -> None:
        index: dict[tuple[str, str], list[NameList]] = {}
        for row in session.exec(select(NameList)).all():
            index.setdefault((row.entity_type, row.value), []).append(row)
        with self._lock:
            self.lists = index

    def match_lists(self, entities: dict[str, Any]) -> list[dict[str, Any]]:
        now = datetime.now()
        hits = []
        for etype, value in entities.items():
            if not value:
                continue
            for row in self.lists.get((etype, str(value)), []):
                if row.expire_at and row.expire_at < now:
                    continue
                hits.append({"list_type": row.list_type, "entity_type": etype,
                             "value": str(value), "reason": row.reason})
        return hits


_runtime: Runtime | None = None


def init_runtime(redis_client=None, graph: GraphStore | None = None) -> Runtime:
    global _runtime
    if redis_client is None:
        import redis
        redis_client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True,
                                            socket_connect_timeout=3, socket_timeout=3)
        redis_client.ping()       # Redis 是实时特征的必需组件，连不上直接启动失败
    if graph is None:
        graph = build_graph_store(config.GRAPH_BACKEND, config.NEO4J_URI,
                                  config.NEO4J_USER, config.NEO4J_PASSWORD)
    _runtime = Runtime(redis_client, graph)
    return _runtime


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("运行时尚未初始化")
    return _runtime
