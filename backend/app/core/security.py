"""鉴权：bcrypt 存密码，HMAC-SHA256 签名的 Bearer Token（结构同 JWT HS256，不依赖额外包）。

角色：
    admin       管理员：全部权限
    strategist  策略师：规则、名单、仿真
    auditor     审核员：案件审核与处置
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core import config

ROLES = {"admin": "管理员", "strategist": "策略师", "auditor": "审核员"}
_bearer = HTTPBearer(auto_error=False)


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=10)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_token(username: str, role: str, display_name: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"sub": username, "role": role, "name": display_name,
                            "exp": int(time.time()) + config.TOKEN_TTL_SECONDS},
                           ensure_ascii=False).encode())
    sig = hmac.new(config.SECRET_KEY.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


@dataclass
class CurrentUser:
    username: str
    role: str
    name: str


def decode_token(token: str) -> CurrentUser:
    try:
        header, body, sig = token.split(".")
        expect = hmac.new(config.SECRET_KEY.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, _unb64(sig)):
            raise ValueError("签名不符")
        data = json.loads(_unb64(body))
        if data["exp"] < time.time():
            raise ValueError("已过期")
        return CurrentUser(data["sub"], data["role"], data.get("name", data["sub"]))
    except Exception as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"登录无效：{e}") from None


def current_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> CurrentUser:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    return decode_token(cred.credentials)


def event_caller(request: Request,
                 cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    """POST /events 的调用方鉴权：业务系统带共享密钥请求头，或者控制台用户带登录 Token。
    返回调用方标识（写日志用）：业务系统名，或 user:<用户名>。"""
    key = request.headers.get(config.EVENT_API_HEADER)
    if key:
        for caller, expect in config.EVENT_API_KEYS.items():
            if expect and hmac.compare_digest(key.encode(), expect.encode()):
                return caller
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "接入密钥无效")
    if cred is not None:
        return f"user:{decode_token(cred.credentials).username}"
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"缺少接入密钥（请求头 {config.EVENT_API_HEADER}）")


def require_roles(*roles: str):
    def dep(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if user.role != "admin" and user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"需要角色：{'/'.join(ROLES[r] for r in roles)}")
        return user
    return dep
