print("[deps] loaded from:", __file__)

import os
import hmac
import secrets
from contextlib import contextmanager
from typing import Iterator, Optional

from fastapi import Depends, HTTPException, status, Request, Form

# 你的 ORM 会话工厂
# 必须存在 models/db.py 中的 get_session()
# 如存在 get_session_ro() 则用作只读；没有就回退到 get_session()
try:
    from models.db import get_session as _get_session
except Exception as e:
    raise RuntimeError("models.db.get_session 未找到，请检查 models/db.py") from e

try:
    from models.db import get_session_ro as _get_session_ro  # 可选
except Exception:
    _get_session_ro = None

# 会话键，和 auth.py 对齐
from web_admin.constants import SESSION_USER_KEY, TWOFA_PASSED_KEY


# -------------------------
# DB 依赖
# -------------------------

def db_session() -> Iterator:
    """
    读写会话。FastAPI 依赖里用：
      db=Depends(db_session)
    """
    with _get_session() as db:
        yield db


def db_session_ro() -> Iterator:
    """
    只读会话。若未实现只读工厂，则回退到读写。
    Dashboard 等查询可用：
      db=Depends(db_session_ro)
    """
    if _get_session_ro:
        with _get_session_ro() as db:
            yield db
    else:
        with _get_session() as db:
            yield db


# -------------------------
# 管理员校验（不再依赖 models.user.is_admin）
# -------------------------

def _env_admin_user() -> str:
    return os.getenv("ADMIN_WEB_USER", "admin")


def _env_super_admins() -> set[int]:
    """
    可选：SUPER_ADMINS=123,456
    用于允许多个 Telegram ID 作为管理员。
    """
    raw = os.getenv("SUPER_ADMINS", "")
    out: set[int] = set()
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except Exception:
            pass
    return out


def require_admin(req: Request):
    """
    简单粗暴的“已登录管理员”判断：
      - session 里存在 admin_user（auth.py 写入）
      - 用户名匹配 ADMIN_WEB_USER
    你要更复杂的多租户/RBAC，这里再扩展。
    """
    u = req.session.get(SESSION_USER_KEY)
    if not u:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    expected = _env_admin_user()
    if not isinstance(u, dict) or u.get("username") != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    # 可选：如配置 SUPER_ADMINS，则若提供 tg_id 也做白名单限制
    sa = _env_super_admins()
    if sa:
        try:
            tg_id = int(u.get("tg_id") or 0)
        except Exception:
            tg_id = 0
        if tg_id not in sa:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not in super admins")
    return u  # 让后续处理能拿到 {username, tg_id}


# -------------------------
# 高危操作二次校验守卫
# -------------------------

class GuardDangerOp:
    """
    用法：
        @router.post("/danger")
        def do_something(req: Request, sess=Depends(GuardDangerOp())): ...
    规则：
      - 需要 require_admin 先通过
      - session[TWOFA_PASSED_KEY] 为 True 才放行
      - 可选：minutes > 0 时，允许自行扩展时效控制（当前仅布尔）
    """
    def __init__(self, minutes: int = 10):
        self.minutes = minutes

    def __call__(self, req: Request, u=Depends(require_admin)):
        ok = bool(req.session.get(TWOFA_PASSED_KEY))
        if not ok:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="2FA required")
        return u  # 返回管理员会话信息


# -------------------------
# 超危险操作守卫（reset 专用）
# -------------------------

class GuardDangerOpWithReset(GuardDangerOp):
    """
    超危险操作守卫（用于 reset）：
      - 先走 GuardDangerOp（要求已登录 + 2FA 通过）
      - 再检查环境开关，默认“关闭”，只有显式打开才放行：
            ADMIN_ALLOW_RESET=1   或   ALLOW_RESET=1
    """
    def __call__(self, req: Request, u=Depends(require_admin)):
        # 先执行父类校验（管理员 + 2FA）
        u = super().__call__(req, u)

        allow_env = os.getenv("ADMIN_ALLOW_RESET") == "1" or os.getenv("ALLOW_RESET") == "1"
        if not allow_env:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="reset disabled by config (set ADMIN_ALLOW_RESET=1 to enable)",
            )
        return u


# -------------------------
# CSRF 工具（签发与校验）
# -------------------------

_CSRF_SESSION_KEY = "_csrf_token"


def issue_csrf(req: Request) -> str:
    """
    生成一个一次性的 CSRF token 并写入会话。
    - 在渲染表单页面时调用，把返回值作为隐藏字段：<input name="csrf_token" ...>
    - 成功校验后默认清除（一次性使用）
    """
    token = secrets.token_urlsafe(32)
    req.session[_CSRF_SESSION_KEY] = token
    return token


def verify_csrf(req: Request, token_from_form: str, *, one_time: bool = True) -> bool:
    """
    校验 CSRF：从 session 取真实 token 与表单上的对比。
    - one_time=True：校验通过后立刻删除，防重放
    """
    real = str(req.session.get(_CSRF_SESSION_KEY) or "")
    ok = bool(real) and hmac.compare_digest(real, str(token_from_form or ""))
    if ok and one_time:
        req.session.pop(_CSRF_SESSION_KEY, None)
    return ok


def ensure_csrf_or_403(req: Request, token_from_form: str):
    """
    校验失败即抛 403。可在路由里直接调用：
        ensure_csrf_or_403(req, form['csrf_token'])
    """
    if not verify_csrf(req, token_from_form):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf failed")


def csrf_protect(req: Request, csrf_token: str = Form(default="")):
    """
    作为 FastAPI 依赖使用的 CSRF 守卫（推荐写在 POST 路由签名里）：
        @router.post("/xxx")
        def save_xxx(req: Request, _=Depends(csrf_protect), ...):
            ...
    """
    if not verify_csrf(req, csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf failed")
    # 返回 True 仅占位，调用方无需接收
    return True
