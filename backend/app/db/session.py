"""数据库引擎与会话（SQLModel / SQLAlchemy 2.0）。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url
from sqlmodel import Session, SQLModel, create_engine

from app.core import config

_engine: Engine | None = None


def _ensure_mysql_database(url: str) -> None:
    """MySQL 库不存在时自动创建（utf8mb4）。"""
    u = make_url(url)
    if not u.drivername.startswith("mysql"):
        return
    # SQLAlchemy 2.1 的 URL.set(database=None) 不会清掉库名，改连系统库 information_schema 再建库
    server = create_engine(u.set(database="information_schema"), pool_pre_ping=True)
    with server.connect() as conn:
        conn.execute(text(
            f"CREATE DATABASE IF NOT EXISTS `{u.database}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    server.dispose()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = config.DATABASE_URL
        if url.startswith("sqlite"):
            _engine = create_engine(url, connect_args={"check_same_thread": False})
        else:
            _ensure_mysql_database(url)
            _engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20,
                                    pool_recycle=3600)
    return _engine


def reset_engine() -> None:
    """测试用：切换 DATABASE_URL 后重建引擎。"""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def init_db() -> None:
    from app import models  # noqa: F401  注册所有表
    SQLModel.metadata.create_all(get_engine())
    from sqlalchemy.exc import IntegrityError
    with Session(get_engine()) as s:
        if s.get(models.DecisionGuard, 1) is None:
            s.add(models.DecisionGuard())
            try:
                s.commit()
            except IntegrityError:  # 另一启动进程已经初始化
                s.rollback()


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


def get_session() -> Iterator[Session]:
    """FastAPI 依赖。"""
    with session_scope() as s:
        yield s


# 接口统一用这个依赖。scope="function"：接口函数一结束就提交，然后才发响应。
# FastAPI 默认 scope="request" 会在响应发出后才执行 yield 之后的提交，客户端拿到 200 时数据可能还没落库
# （认领后立刻处置会读到旧状态报 409），提交失败也只能落进日志，客户端已经收到了成功。
DbSession = Depends(get_session, scope="function")
