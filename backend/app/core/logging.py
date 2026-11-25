"""日志：控制台 + 按天滚动文件 backend/logs/app.log。"""
from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler

from app.core import config

_done = False


def setup_logging() -> None:
    global _done
    if _done:
        return
    _done = True
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    # Neo4j 每次启动都会把「约束已存在」作为 INFO 通知刷屏，只保留警告以上
    logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(config.LOG_DIR / "app.log", when="midnight", backupCount=14, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        root.warning("日志目录不可写，只输出到控制台")