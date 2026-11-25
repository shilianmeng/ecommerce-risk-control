"""关联图谱（团伙识别）。

实体：Account（用户）、Device、IP、Phone、Address。
关系：账号 —USES_DEVICE/USES_IP/USES_PHONE/SHIPS_TO→ 实体（同设备/同IP/同手机/同地址由共用实体体现）。

两套实现，接口一致：
    Neo4jGraphStore  —— 正式运行，N 跳查询走 Cypher 变长路径。
    MemoryGraphStore —— NetworkX 内存图，测试和 Neo4j 不可用时的降级。
团伙 = 账号-实体二部图的连通分量里账号数 ≥ min_size 的那些。
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

import networkx as nx

log = logging.getLogger(__name__)

ENTITY_LABELS = {          # 事件字段 → (节点标签, 关系类型)
    "device_id": ("Device", "USES_DEVICE"),
    "ip": ("IP", "USES_IP"),
    "phone": ("Phone", "USES_PHONE"),
    "address": ("Address", "SHIPS_TO"),
}
LABEL_CN = {"Account": "账号", "Device": "设备", "IP": "IP", "Phone": "手机号", "Address": "收货地址"}


class GraphStore(Protocol):
    backend: str
    def add_event(self, ev: dict[str, Any]) -> None: ...
    def related_accounts(self, user_id: str, hops: int = 2) -> list[str]: ...
    def neighborhood(self, user_id: str, hops: int = 2) -> dict[str, Any]: ...
    def gangs(self, min_size: int = 3) -> list[dict[str, Any]]: ...
    def clear(self) -> None: ...


def _node(label: str, value: str) -> str:
    return f"{label}:{value}"


def _split(node: str) -> tuple[str, str]:
    label, _, value = node.partition(":")
    return label, value


def _components(g: nx.Graph, min_size: int) -> list[dict[str, Any]]:
    out = []
    for comp in nx.connected_components(g):
        accounts = sorted(_split(n)[1] for n in comp if n.startswith("Account:"))
        if len(accounts) < min_size:
            continue
        shared = [{"type": _split(n)[0], "value": _split(n)[1],
                   "degree": g.degree(n)} for n in comp if not n.startswith("Account:")]
        shared = [s for s in shared if s["degree"] >= 2]          # 只列被多个账号共用的实体
        shared.sort(key=lambda s: -s["degree"])
        out.append({"size": len(accounts), "accounts": accounts, "shared_entities": shared})
    out.sort(key=lambda c: -c["size"])
    for i, c in enumerate(out, 1):
        c["gang_id"] = f"G{i:03d}"
    return out


def _subgraph_json(g: nx.Graph, center: str) -> dict[str, Any]:
    nodes = [{"id": n, "label": _split(n)[0], "label_cn": LABEL_CN.get(_split(n)[0], ""),
              "value": _split(n)[1], "center": n == center} for n in g.nodes]
    edges = [{"source": a, "target": b, "type": d.get("type", "")} for a, b, d in g.edges(data=True)]
    return {"nodes": nodes, "edges": edges}


class MemoryGraphStore:
    backend = "memory"

    def __init__(self):
        self.g = nx.Graph()

    def add_event(self, ev: dict[str, Any]) -> None:
        acc = _node("Account", ev["user_id"])
        self.g.add_node(acc)
        for field, (label, rel) in ENTITY_LABELS.items():
            if ev.get(field):
                self.g.add_edge(acc, _node(label, str(ev[field])), type=rel)

    def _ego(self, user_id: str, hops: int) -> nx.Graph:
        acc = _node("Account", user_id)
        if acc not in self.g:
            return nx.Graph()
        return nx.ego_graph(self.g, acc, radius=hops * 2)

    def related_accounts(self, user_id: str, hops: int = 2) -> list[str]:
        me = _node("Account", user_id)
        return sorted(_split(n)[1] for n in self._ego(user_id, hops).nodes
                      if n.startswith("Account:") and n != me)

    def neighborhood(self, user_id: str, hops: int = 2) -> dict[str, Any]:
        return _subgraph_json(self._ego(user_id, hops), _node("Account", user_id))

    def gangs(self, min_size: int = 3) -> list[dict[str, Any]]:
        return _components(self.g, min_size)

    def clear(self) -> None:
        self.g.clear()


class Neo4jGraphStore:
    backend = "neo4j"

    def __init__(self, uri: str, user: str, password: str):
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=5)
        self.driver.verify_connectivity()
        for label in ("Account", "Device", "IP", "Phone", "Address"):
            self.driver.execute_query(
                f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE")

    def close(self) -> None:
        self.driver.close()

    def add_event(self, ev: dict[str, Any]) -> None:
        parts = ["MERGE (a:Account {id: $uid})"]
        params: dict[str, Any] = {"uid": ev["user_id"], "ts": float(ev.get("ts") or 0)}
        for i, (field, (label, rel)) in enumerate(ENTITY_LABELS.items()):
            if ev.get(field):
                params[f"v{i}"] = str(ev[field])
                parts.append(
                    f"MERGE (e{i}:{label} {{id: $v{i}}}) "
                    f"MERGE (a)-[r{i}:{rel}]->(e{i}) "
                    f"ON CREATE SET r{i}.first_seen = $ts, r{i}.last_seen = $ts "
                    f"ON MATCH SET r{i}.first_seen = CASE WHEN r{i}.first_seen > $ts THEN $ts ELSE r{i}.first_seen END, "
                    f"r{i}.last_seen = CASE WHEN r{i}.last_seen < $ts THEN $ts ELSE r{i}.last_seen END")
        self.driver.execute_query("\n".join(parts), params)

    def related_accounts(self, user_id: str, hops: int = 2) -> list[str]:
        depth = max(1, min(int(hops), 3)) * 2
        recs, _, _ = self.driver.execute_query(
            f"MATCH (a:Account {{id: $uid}})-[*2..{depth}]-(o:Account) WHERE o <> a "
            "RETURN DISTINCT o.id AS id LIMIT 500", {"uid": user_id})
        return sorted(r["id"] for r in recs)

    def neighborhood(self, user_id: str, hops: int = 2) -> dict[str, Any]:
        depth = max(1, min(int(hops), 3)) * 2
        recs, _, _ = self.driver.execute_query(
            f"MATCH p = (a:Account {{id: $uid}})-[*1..{depth}]-() "
            "WITH p LIMIT 2000 UNWIND relationships(p) AS r "
            "RETURN DISTINCT labels(startNode(r))[0] AS sl, startNode(r).id AS sv, "
            "labels(endNode(r))[0] AS tl, endNode(r).id AS tv, type(r) AS t", {"uid": user_id})
        g = nx.Graph()
        g.add_node(_node("Account", user_id))
        for r in recs:
            g.add_edge(_node(r["sl"], r["sv"]), _node(r["tl"], r["tv"]), type=r["t"])
        return _subgraph_json(g, _node("Account", user_id))

    def gangs(self, min_size: int = 3) -> list[dict[str, Any]]:
        recs, _, _ = self.driver.execute_query(
            "MATCH (a:Account)-[r]->(e) RETURN a.id AS a, labels(e)[0] AS l, e.id AS v, type(r) AS t")
        g = nx.Graph()
        for r in recs:
            g.add_edge(_node("Account", r["a"]), _node(r["l"], r["v"]), type=r["t"])
        return _components(g, min_size)

    def clear(self) -> None:
        self.driver.execute_query("MATCH (n) WHERE n:Account OR n:Device OR n:IP OR n:Phone OR n:Address "
                                  "DETACH DELETE n")


def build_graph_store(backend: str, uri: str, user: str, password: str) -> GraphStore:
    if backend == "neo4j":
        try:
            return Neo4jGraphStore(uri, user, password)
        except Exception as e:  # Neo4j 没起来：降级到内存图，系统仍可用
            log.error("Neo4j 连接失败，降级为内存图谱（重启后图谱为空）：%s", e)
    return MemoryGraphStore()
