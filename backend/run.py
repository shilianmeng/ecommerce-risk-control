"""启动后端：python run.py （VS Code F5 也是跑这个文件）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from app.core import config  # noqa: E402

if __name__ == "__main__":
    print(f"后端接口：http://{config.HOST}:{config.PORT}{config.API_PREFIX}")
    print(f"接口文档：http://{config.HOST}:{config.PORT}/docs")
    print("演示账号：admin / strategist / auditor，密码均为 123456")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)