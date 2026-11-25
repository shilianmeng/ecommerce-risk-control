"""滑动窗口特征引擎（Redis ZSET，时间戳做 score）。

为什么用 ZSET 而不是固定窗口计数器：
    固定窗口（INCR + EXPIRE）在窗口边界会清零，59:59 和 00:01 的两次下单落在两个窗口里，
    特征突变；ZSET 以事件时间为 score，任意时刻都用 ZCOUNT [t-窗口, t] 精确统计，窗口随事件滑动。

两类窗口：
    计数窗口：member = 事件键（天然去重），统计窗口内事件数。
    去重窗口：member = [账号 ID, 事件键]，查询时间范围后按账号去重，保留乱序事件历史。

窗口以「事件时间」为基准；保留窗口之外额外一小时的数据，不支持任意久远的迟到事件。
namespace 用于仿真隔离：仿真在独立前缀下计算，结束后整体删除，不污染线上窗口。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from redis.exceptions import WatchError

HOUR = 3600
DAY = 24 * HOUR


class LateEventError(ValueError):
    """超过保留历史所支持的乱序范围，不能给出可靠特征。"""

# 特征注册表：名字 → 说明。DSL 校验字段名、前端展示都用它。
FEATURES: dict[str, str] = {
    # 事件自身
    "event_type": "事件类型",
    "amount": "订单/支付金额（元）",
    "coupon_amount": "优惠券面额（元）",
    "refund_amount": "退款金额（元）",
    "refund_reason": "退款原因（not_received / quality / other …）",
    "account_age_days": "账号注册天数",
    "is_new_account": "是否新账号（注册不满 7 天）",
    # 滑动窗口
    "device_order_count_1h": "近 1 小时同设备下单次数",
    "user_order_count_24h": "近 24 小时同账号下单次数",
    "user_coupon_count_1h": "近 1 小时同账号领券次数",
    "user_coupon_count_24h": "近 24 小时同账号领券次数",
    "device_coupon_count_24h": "近 24 小时同设备领券次数（跨账号）",
    "phone_order_count_24h": "近 24 小时同收货手机号下单次数",
    "phone_refund_count_24h": "近 24 小时同收货手机号退款次数",
    "phone_refund_rate_24h": "近 24 小时同收货手机号退款率（退款数/下单数）",
    "user_refund_count_30d": "近 30 天同账号退款申请次数",
    "ip_account_count_24h": "近 24 小时同 IP 出现的账号数",
    "device_account_count_24h": "近 24 小时同设备登录/操作的账号数",
    "address_account_count_7d": "近 7 天同收货地址的账号数（地址聚集度）",
    "device_fingerprint": "设备指纹（device_id）",
    # 图谱 & 名单（由决策服务补充）
    "related_account_count": "图谱 2 跳内（共用设备/IP/手机/地址）关联的其他账号数",
    "in_greylist": "是否命中灰名单",
}


@dataclass(frozen=True)
class _Window:
    feature: str
    kind: str            # count | distinct
    entity: str          # 事件里取哪个字段做 key
    events: frozenset    # 哪些事件类型会写入这个窗口
    seconds: int


_ALL = frozenset({"login", "coupon_receive", "order_create", "order_pay", "after_sale_apply"})

WINDOWS: tuple[_Window, ...] = (
    _Window("device_order_count_1h", "count", "device_id", frozenset({"order_create"}), HOUR),
    _Window("user_order_count_24h", "count", "user_id", frozenset({"order_create"}), DAY),
    _Window("user_coupon_count_1h", "count", "user_id", frozenset({"coupon_receive"}), HOUR),
    _Window("user_coupon_count_24h", "count", "user_id", frozenset({"coupon_receive"}), DAY),
    _Window("device_coupon_count_24h", "count", "device_id", frozenset({"coupon_receive"}), DAY),
    _Window("phone_order_count_24h", "count", "phone", frozenset({"order_create"}), DAY),
    _Window("phone_refund_count_24h", "count", "phone", frozenset({"after_sale_apply"}), DAY),
    _Window("user_refund_count_30d", "count", "user_id", frozenset({"after_sale_apply"}), 30 * DAY),
    _Window("ip_account_count_24h", "distinct", "ip", _ALL, DAY),
    _Window("device_account_count_24h", "distinct", "device_id", _ALL, DAY),
    _Window("address_account_count_7d", "distinct", "address", _ALL, 7 * DAY),
)

# 同一个 Redis key 可能被多个特征共用（如 user_coupon 1h/24h），按 (kind, entity, events) 归并
def _bucket(w: _Window) -> str:
    kind = "distinct_v2" if w.kind == "distinct" else w.kind
    return f"{kind}:{w.entity}:{'+'.join(sorted(w.events))}"


_RETENTION: dict[str, int] = {}
for _w in WINDOWS:
    _RETENTION[_bucket(_w)] = max(_RETENTION.get(_bucket(_w), 0), _w.seconds)


class FeatureEngine:
    def __init__(self, redis_client, prefix: str = "risk"):
        self.r = redis_client
        self.prefix = prefix

    def _key(self, namespace: str, bucket: str, value: str) -> str:
        ns = f"{namespace}:" if namespace else ""
        return f"{self.prefix}:{ns}win:{bucket}:{value}"

    def _accept_timestamp(self, ts: float, namespace: str) -> None:
        key = self._key(namespace, "watermark", "event_time")
        while True:
            with self.r.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    latest = float(pipe.get(key) or ts)
                    if ts < latest - HOUR:
                        raise LateEventError("事件比已接收的最新事件早超过一小时，请走离线补录")
                    pipe.multi()
                    pipe.set(key, max(latest, ts), ex=31 * DAY)
                    pipe.execute()
                    return
                except WatchError:
                    continue

    def record_and_compute(self, ev: dict[str, Any], namespace: str = "", *, accept_late: bool = False) -> dict[str, Any]:
        """写入本事件并计算全部特征（本事件计入窗口）。

        ev 需含：event_type, user_id, ts(秒), event_key；可选 device_id/ip/phone/address/payload。
        """
        ts = float(ev["ts"])
        if not accept_late:
            self._accept_timestamp(ts, namespace)
        etype = ev["event_type"]
        pipe = self.r.pipeline(transaction=False)

        # 1) 写入：每个 bucket 只写一次
        written: set[str] = set()
        for w in WINDOWS:
            b = _bucket(w)
            val = ev.get(w.entity)
            if b in written or not val or etype not in w.events:
                continue
            written.add(b)
            key = self._key(namespace, b, str(val))
            if w.kind == "count":
                pipe.zadd(key, {ev["event_key"]: ts})
            else:
                member = json.dumps([ev["user_id"], ev["event_key"]], ensure_ascii=False)
                pipe.zadd(key, {member: ts})
            retention = _RETENTION[b]
            pipe.zremrangebyscore(key, "-inf", ts - retention - HOUR)
            pipe.expire(key, retention + DAY)

        # 2) 读取
        names: list[str] = []
        for w in WINDOWS:
            val = ev.get(w.entity)
            if not val:
                continue
            names.append(w.feature)
            key = self._key(namespace, _bucket(w), str(val))
            if w.kind == "distinct":
                # ponytail: O(窗口事件数)，大规模时改用服务端聚合；保留事件才能处理乱序。
                pipe.zrangebyscore(key, ts - w.seconds, ts)
            else:
                pipe.zcount(key, ts - w.seconds, ts)
        results = pipe.execute()
        counts = dict(zip(names, results[-len(names):] if names else []))
        for w in WINDOWS:
            if w.kind == "distinct" and w.feature in counts:
                counts[w.feature] = len({json.loads(m)[0] for m in counts[w.feature]})

        return self.assemble(ev, counts)

    @staticmethod
    def from_events(ev: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        """SQL 事务内的权威事件集合；纯计算，不向外部存储写入。"""
        ts = float(ev["ts"])
        counts = {}
        for w in WINDOWS:
            value = ev.get(w.entity)
            rows = [e for e in events if value and e.get(w.entity) == value
                    and e["event_type"] in w.events and ts - w.seconds <= float(e["ts"]) <= ts]
            counts[w.feature] = len({e["user_id"] if w.kind == "distinct" else e["event_key"] for e in rows})
        return FeatureEngine.assemble(ev, counts)

    @staticmethod
    def assemble(ev: dict[str, Any], counts: dict) -> dict[str, Any]:
        ts, etype = float(ev["ts"]), ev["event_type"]
        feats: dict[str, Any] = {w.feature: int(counts.get(w.feature, 0)) for w in WINDOWS}
        orders = feats["phone_order_count_24h"]
        refunds = feats["phone_refund_count_24h"]
        feats["phone_refund_rate_24h"] = round(refunds / orders, 4) if orders else (1.0 if refunds else 0.0)

        payload = ev.get("payload") or {}
        age = payload.get("account_age_days")
        if age is None and payload.get("register_ts"):
            age = max(0.0, (ts - float(payload["register_ts"])) / DAY)
        feats["account_age_days"] = round(float(age), 2) if age is not None else None
        feats["is_new_account"] = age is not None and float(age) < 7
        feats["event_type"] = etype
        feats["amount"] = float(ev.get("amount") or 0)
        feats["coupon_amount"] = float(payload.get("coupon_amount") or 0)
        feats["refund_amount"] = float(payload.get("refund_amount") or 0)
        feats["refund_reason"] = payload.get("refund_reason")
        feats["device_fingerprint"] = ev.get("device_id")
        return feats

    def drop_namespace(self, namespace: str) -> int:
        """删除仿真命名空间下的全部窗口。"""
        n = 0
        for key in self.r.scan_iter(match=f"{self.prefix}:{namespace}:win:*", count=500):
            self.r.delete(key)
            n += 1
        return n

    def flush_all(self) -> int:
        """清空线上窗口（重灌演示数据时用）。"""
        n = 0
        for key in self.r.scan_iter(match=f"{self.prefix}:*", count=500):
            self.r.delete(key)
            n += 1
        return n
