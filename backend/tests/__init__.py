"""测试隔离：每次测试运行都用临时 SQLite 库 + fakeredis + 内存图谱，绝不碰正式库 RiskControl、
真实 Redis 和 Neo4j。各测试模块第一行 import tests，保证这里先于 app.core.config 执行。

运行：在 backend 目录下  python -m unittest discover -s tests -t .
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

if "app.core.config" in sys.modules:
    raise RuntimeError("app.core.config 已先于 tests 导入，测试可能连到正式库，已中止")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="risk_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["RISK_DATABASE_URL"] = f"sqlite:///{Path(_TMP) / 'test.db'}"
os.environ["RISK_GRAPH_BACKEND"] = "memory"
os.environ["RISK_REDIS_URL"] = "redis://invalid-host-for-tests:1/0"
os.environ["RISK_RAG_LLM_URL"] = ""                      # 测试不调大模型
os.environ["RISK_EVENT_API_KEY_XIAOER"] = "test-event-key"
EVENT_HEADERS = {"X-Risk-Api-Key": "test-event-key"}


def fresh_db(name: str) -> None:
    """切换到一个全新的临时库。"""
    from app.core import config
    from app.db import session
    config.DATABASE_URL = f"sqlite:///{Path(_TMP) / (name + '.db')}"
    session.reset_engine()