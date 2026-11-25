"""全局配置。

按盟哥要求：本地连接参数直接写在这里，不需要配置环境变量。
环境变量（RISK_ 前缀）仍可覆盖，用于部署；本地开发不用管。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]          # backend/
LOG_DIR = BASE_DIR / "logs"


def _env(name: str, default: str) -> str:
    return os.environ.get(f"RISK_{name}", default)


# ---- 服务 ----
APP_NAME = "电商风控系统"
API_PREFIX = "/api/v1"
HOST = _env("HOST", "127.0.0.1")
PORT = int(_env("PORT", "8092"))                       # 8000/8091 已被占用，不要改成它们
CORS_ORIGINS = _env("CORS_ORIGINS", "http://127.0.0.1:5181,http://localhost:5181").split(",")

# ---- MySQL（业务库）----
MYSQL_HOST = _env("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(_env("MYSQL_PORT", "3306"))
MYSQL_USER = _env("MYSQL_USER", "root")
MYSQL_PASSWORD = _env("MYSQL_PASSWORD", "root")
MYSQL_DATABASE = _env("MYSQL_DATABASE", "RiskControl")
# 测试会把它替换成临时库 URL；正式运行不要手动设置
DATABASE_URL = _env(
    "DATABASE_URL",
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4",
)

# ---- Redis（滑动窗口 / 幂等）----
REDIS_URL = _env("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_PREFIX = _env("REDIS_PREFIX", "risk")
IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 3600

# ---- Neo4j（关联图谱）----
NEO4J_URI = _env("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = _env("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = _env("NEO4J_PASSWORD", "RiskNeo4j2026")
GRAPH_BACKEND = _env("GRAPH_BACKEND", "neo4j")          # neo4j | memory（测试用）

# ---- 决策仲裁阈值 ----
REVIEW_THRESHOLD = 60      # 0–59 Pass
REJECT_THRESHOLD = 80      # 60–79 Review，80–100 Reject
GREYLIST_SCORE = 20        # 命中灰名单额外加分（重点观察）

# ---- 鉴权 ----
SECRET_KEY = _env("SECRET_KEY", "risk-control-local-dev-secret-change-in-prod")
TOKEN_TTL_SECONDS = 12 * 3600
DEMO_PASSWORD = "123456"   # 演示账号统一密码，见 README

# ---- 业务系统接入（POST /events）----
# 业务系统（如电商小二 ai-service）调用时带请求头 X-Risk-Api-Key: <下面的值>。
# 前端「事件接入」页用登录 Token 调用，不需要这个密钥。上线前务必换成随机长串。
EVENT_API_HEADER = "X-Risk-Api-Key"
EVENT_API_KEYS = {
    "ecom-xiaoer": _env("EVENT_API_KEY_XIAOER", "xiaoer-local-dev-key-change-in-prod"),
}

# ---- RAG（接入微调模型，旁路可降级）----
# 云端 vLLM 监听 8001，经 SSH 隧道映射到本机 8093（本机 8000 被占用，不要用）。
# 隧道没开时连接会立刻失败，RAG 自动降级为只返回检索结果；设为空串则完全不调大模型。
RAG_LLM_URL = _env("RAG_LLM_URL", "http://127.0.0.1:8093/v1")
RAG_LLM_MODEL = _env("RAG_LLM_MODEL", "qwen-risk-lora")   # vLLM --served-model-name
RAG_TIMEOUT_SECONDS = 90   # 本机 transformers 在 1650 Ti 上生成 600 tokens 要几十秒；云端 vLLM 可调回 15
