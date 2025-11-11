# web_admin/auth.py
from __future__ import annotations

import os
import hmac
import time
import hashlib
import secrets
import base64
import struct
from typing import Optional, Tuple, Dict

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates  # 用于模板

# i18n：根据你工程的习惯，先尝试根级，再兜底 core.i18n
try:
    from i18n import t  # type: ignore
except Exception:  # pragma: no cover
    try:
        from core.i18n.i18n import t  # type: ignore
    except Exception:
        def t(x: str) -> str:
            return x

# .env 优先：若没装 python-dotenv，也能优雅退化
try:
    from dotenv import dotenv_values  # type: ignore
    _ENV_FROM_DOTENV = dotenv_values(".env") or {}
except Exception:  # pragma: no cover
    _ENV_FROM_DOTENV = {}

# 与 deps.py 对齐的会话键（工程已有）
from web_admin.constants import SESSION_USER_KEY, TWOFA_PASSED_KEY

router = APIRouter(prefix="/admin", tags=["admin-auth"])

# 模块级模板缓存：既支持从 app 注入，也支持从外部直接传实例
_TPL: Jinja2Templates | None = None

# ========== 模板注入：供 main.py 调用 ==========
def inject_templates(app_or_templates):
    """
    兼容两种调用：
      1) inject_templates(app) -> 使用 app.state.templates，没有就创建并缓存到 _TPL
      2) inject_templates(Jinja2Templates(...)) -> 直接缓存传入实例到 _TPL
    返回一个 Jinja2Templates 实例。
    """
    global _TPL
    if isinstance(app_or_templates, Jinja2Templates):
        _TPL = app_or_templates
        return _TPL

    app = app_or_templates
    cur = getattr(app.state, "templates", None)
    if cur is None:
        cur = Jinja2Templates(directory="templates")
        app.state.templates = cur
    _TPL = cur
    return _TPL

def _ensure_templates(req: Request) -> Jinja2Templates:
    """优先 req.app.state.templates，其次模块缓存 _TPL，最后兜底创建。"""
    global _TPL
    cur = getattr(req.app.state, "templates", None)
    if isinstance(cur, Jinja2Templates):
        _TPL = cur
        return cur
    if isinstance(_TPL, Jinja2Templates):
        return _TPL
    _TPL = Jinja2Templates(directory="templates")
    req.app.state.templates = _TPL
    return _TPL

# ========== 配置读取：优先 .env，然后回退系统环境 ==========
def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    v = _ENV_FROM_DOTENV.get(key)
    if v not in (None, ""):
        return str(v)
    return os.getenv(key, default)

def _env_user() -> str:
    return get_env("ADMIN_WEB_USER", "admin")  # 默认 admin（仅开发期）

def _env_pass_plain() -> Optional[str]:
    return get_env("ADMIN_WEB_PASSWORD")

def _env_pass_hash() -> Optional[str]:
    return get_env("ADMIN_WEB_PASSWORD_HASH")

def _env_admin_tg_id() -> Optional[int]:
    v = get_env("ADMIN_TG_ID")
    return int(v) if v and v.isdigit() else None

def _env_bot_token() -> Optional[str]:
    return get_env("TELEGRAM_BOT_TOKEN")

def _env_session_secret() -> str:
    return get_env("ADMIN_SESSION_SECRET", "CHANGE_ME_PLEASE_32CHARS_MIN") or "CHANGE_ME_PLEASE_32CHARS_MIN"

def _env_totp_secret() -> Optional[str]:
    """
    如果配置了 ADMIN_TOTP_SECRET（Base32），则启用 TOTP 两步验证。
    与 Telegram OTP 并行：有 TOTP 优先校验 TOTP；否则走 Telegram OTP 流程（可选）。
    """
    return get_env("ADMIN_TOTP_SECRET")

# 登录节流参数（可调）
MAX_FAILED_PER_WINDOW = int(get_env("ADMIN_LOGIN_MAX_FAILED", "5") or "5")
WINDOW_SECONDS = int(get_env("ADMIN_LOGIN_WINDOW_SEC", "900") or "900")  # 15 分钟
LOCK_MINUTES = int(get_env("ADMIN_LOGIN_LOCK_MIN", "10") or "10")  # 锁定 10 分钟

# OTP 过期时间（秒）
OTP_TTL_SECONDS = int(get_env("ADMIN_OTP_TTL_SEC", "600") or "600")

# ========== 密码校验 ==========
_BCRYPT_AVAILABLE = False
try:
    import bcrypt  # type: ignore
    _BCRYPT_AVAILABLE = True
except Exception:  # pragma: no cover
    _BCRYPT_AVAILABLE = False

def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _verify_password(input_password: str) -> bool:
    """
    校验顺序（更安全的优先）：
      1. ADMIN_WEB_PASSWORD_HASH（sha256:<hex> 或 $2b$... 的 bcrypt 字符串）
      2. ADMIN_WEB_PASSWORD（明文）
      3. 都没设：默认 'admin'（仅开发期）
    """
    h = (_env_pass_hash() or "").strip()
    if h:
        # sha256:<hex>
        if h.lower().startswith("sha256:"):
            expected = h.split(":", 1)[1].strip().lower()
            calc = _sha256_hex(input_password).lower()
            return hmac.compare_digest(calc, expected)
        # bcrypt:$2b$... 或 bcrypt: 前缀
        if h.startswith("$2a$") or h.startswith("$2b$") or h.startswith("$2y$") or h.lower().startswith("bcrypt:"):
            hp = h.split(":", 1)[1].strip() if h.lower().startswith("bcrypt:") else h
            if not _BCRYPT_AVAILABLE:
                return False
            try:
                return bcrypt.checkpw(input_password.encode("utf-8"), hp.encode("utf-8"))  # type: ignore
            except Exception:
                return False

    p = _env_pass_plain()
    if p is not None and p != "":
        return hmac.compare_digest(p, input_password)

    # 开发兜底：没有任何配置时，密码默认为 "admin"
    return hmac.compare_digest("admin", input_password)

# ========== CSRF ==========
def _issue_csrf(req: Request) -> str:
    token = secrets.token_urlsafe(32)
    req.session["_csrf_token"] = token
    return token

def _check_csrf(req: Request, token_from_form: str) -> bool:
    real = str(req.session.get("_csrf_token") or "")
    ok = bool(real) and hmac.compare_digest(real, str(token_from_form or ""))
    # 单次性使用：通过后即作废，防重复提交
    if ok:
        req.session.pop("_csrf_token", None)
    return ok

# ========== 登录节流（内存）==========
# 结构：{ key: {"fails": int, "first": ts, "locked_until": ts or 0} }
_RATE: Dict[str, Dict[str, float | int]] = {}

def _client_ip(req: Request) -> str:
    h = req.headers
    xff = (h.get("x-forwarded-for") or h.get("X-Forwarded-For") or "").split(",")[0].strip()
    if xff:
        return xff
    return req.client.host if req.client else "0.0.0.0"

def _rate_key(req: Request, username: str) -> str:
    return f"{_client_ip(req)}|{username.strip().lower()}"

def _rate_check_and_bump(req: Request, username: str) -> Tuple[bool, str]:
    """
    返回 (allowed, reason)
    - 失败时：给出 locked / too_many / reset_in 等理由
    """
    now = time.time()
    key = _rate_key(req, username)
    rec = _RATE.get(key, {"fails": 0, "first": now, "locked_until": 0})
    locked_until = float(rec.get("locked_until") or 0)
    if locked_until and now < locked_until:
        left = int(locked_until - now)
        return False, f"locked:{left}s"

    first = float(rec.get("first") or now)
    fails = int(rec.get("fails") or 0)

    # 窗口过期重置
    if now - first > WINDOW_SECONDS:
        rec = {"fails": 0, "first": now, "locked_until": 0}
        _RATE[key] = rec
        return True, "ok"

    # 没有超过限制
    if fails < MAX_FAILED_PER_WINDOW:
        return True, "ok"

    # 超过限制，锁定
    rec["locked_until"] = now + LOCK_MINUTES * 60
    _RATE[key] = rec
    return False, f"locked:{LOCK_MINUTES*60}s"

def _rate_fail(req: Request, username: str):
    now = time.time()
    key = _rate_key(req, username)
    rec = _RATE.get(key, {"fails": 0, "first": now, "locked_until": 0})
    first = float(rec.get("first") or now)
    if now - first > WINDOW_SECONDS:
        # 新窗口
        rec = {"fails": 1, "first": now, "locked_until": 0}
    else:
        rec["fails"] = int(rec.get("fails") or 0) + 1
    _RATE[key] = rec

def _rate_reset(req: Request, username: str):
    key = _rate_key(req, username)
    if key in _RATE:
        del _RATE[key]

# ========== TOTP ==========
def _b32_decode(secret_b32: str) -> bytes:
    # 允许无填充的 Base32
    s = secret_b32.strip().replace(" ", "").upper()
    pad = "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s + pad)

def _hotp(secret: bytes, counter: int, digits: int = 6) -> int:
    msg = struct.pack(">Q", counter)
    h = hmac.new(secret, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o+4])[0] & 0x7fffffff) % (10 ** digits)
    return code

def _totp_verify(secret_b32: str, code: str, skew: int = 1, interval: int = 30, digits: int = 6) -> bool:
    if not code or not code.isdigit():
        return False
    try:
        secret = _b32_decode(secret_b32)
    except Exception:
        return False
    try:
        c = int(time.time()) // interval
        target = int(code)
        # 允许 ±1 个时间步的漂移
        for delta in range(-skew, skew + 1):
            if _hotp(secret, c + delta, digits=digits) == target:
                return True
        return False
    except Exception:
        return False

# ========== OTP（Telegram）==========
def _gen_otp() -> str:
    # 6 位数字，不以 0 起头
    n = secrets.randbelow(900000) + 100000
    return str(n)

def _otp_store(req: Request, code: str, ttl: int = OTP_TTL_SECONDS) -> None:
    session = req.session
    session["otp_code"] = code
    session["otp_exp"] = int(time.time()) + int(ttl)

def _otp_check(req: Request, code: str) -> bool:
    session = req.session
    exp = int(session.get("otp_exp") or 0)
    real = str(session.get("otp_code") or "")
    now = int(time.time())
    if now > exp:
        return False
    return bool(real) and hmac.compare_digest(real, str(code or ""))

def _send_telegram_text(bot_token: str, chat_id: int, text: str) -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        import httpx  # type: ignore
        with httpx.Client(timeout=8.0) as cli:
            r = cli.post(url, json=payload)
            if r.status_code == 200 and (r.json().get("ok") is True):
                return True, "ok"
            return False, f"HTTP {r.status_code}: {r.text}"
    except Exception as e_httpx:  # pragma: no cover
        try:
            import requests  # type: ignore
            r = requests.post(url, json=payload, timeout=8)
            if r.status_code == 200 and (r.json().get("ok") is True):
                return True, "ok"
            return False, f"HTTP {r.status_code}: {r.text}"
        except Exception as e_requests:
            return False, f"httpx/requests not available: {e_httpx} | {e_requests}"

# ========== 审计（可选：若无 ledger 模块则静默）==========
def _audit(action: str, ok: bool, req: Request, note: str = ""):
    try:
        import ledger  # type: ignore
        # 兼容两种常见接口名
        if hasattr(ledger, "append"):
            ledger.append(action=action, ok=ok, ip=_client_ip(req), note=note)
        elif hasattr(ledger, "log"):
            ledger.log(action=action, ok=ok, ip=_client_ip(req), note=note)
    except Exception:
        # 不阻断主流程
        pass

# ========== 视图 ==========
@router.get("/login", response_class=HTMLResponse)
def login_form(req: Request):
    tpl = _ensure_templates(req)
    # 发一个 CSRF
    csrf_token = _issue_csrf(req)
    # 若启用 TOTP，前端可以提示“支持 TOTP”
    totp_enabled = bool(_env_totp_secret())
    return tpl.TemplateResponse(
        "login.html",
        {
            "request": req,
            "title": t("admin.login.title") if t else "Admin Login",
            "message": req.query_params.get("error") or "",
            "username": req.session.get("last_username") or "",
            "csrf_token": csrf_token,
            "totp_enabled": totp_enabled,
            # 如果你需要在模板里显示“已发送 OTP”状态，也可以带上：
            "otp_pending": bool(req.session.get("otp_code")),
            "otp_ttl": OTP_TTL_SECONDS,
        },
    )

@router.post("/send_otp")
def send_otp(req: Request, csrf_token: str = Form(default="")):
    # CSRF
    if not _check_csrf(req, csrf_token):
        return JSONResponse({"ok": False, "error": "csrf_failed"})
    # 环境
    bot = _env_bot_token()
    uid = _env_admin_tg_id()
    if not bot or not uid:
        return JSONResponse({"ok": False, "error": "bot_token_or_admin_tg_id_missing"})
    code = _gen_otp()
    _otp_store(req, code, OTP_TTL_SECONDS)
    ok, msg = _send_telegram_text(bot, uid, f"🔐 Admin OTP: <b>{code}</b>\n⏱ {OTP_TTL_SECONDS//60} min valid.")
    _audit("auth.send_otp", ok, req, note=("telegram" if ok else msg))
    # 发送成功后，前端如需继续 POST 操作，最好再发一个新的 CSRF
    new_csrf = _issue_csrf(req)
    return JSONResponse({"ok": ok, "message": msg, "csrf_token": new_csrf})

@router.post("/login", response_class=HTMLResponse)
def do_login(
    req: Request,
    username: str = Form(""),
    password: str = Form(""),
    otp: str = Form(""),
    csrf_token: str = Form(""),
):
    tpl = _ensure_templates(req)

    # 登录节流（检查是否锁定）
    allowed, reason = _rate_check_and_bump(req, username)
    if not allowed:
        _audit("auth.login_locked", False, req, note=reason)
        return tpl.TemplateResponse(
            "login.html",
            {
                "request": req,
                "title": t("admin.login.title") if t else "Admin Login",
                "message": f"Too many attempts. {reason}",
                "username": username,
                "csrf_token": _issue_csrf(req),
            },
            status_code=429,
        )

    # 记录最后一次用户名，便于回填
    req.session["last_username"] = username.strip()

    # CSRF 检查
    if not _check_csrf(req, csrf_token):
        _rate_fail(req, username)
        _audit("auth.csrf_failed", False, req)
        return tpl.TemplateResponse(
            "login.html",
            {
                "request": req,
                "title": t("admin.login.title") if t else "Admin Login",
                "message": "CSRF check failed",
                "username": username,
                "csrf_token": _issue_csrf(req),
            },
            status_code=400,
        )

    # 用户名校验
    if username.strip() != _env_user():
        _rate_fail(req, username)
        _audit("auth.username_invalid", False, req)
        return tpl.TemplateResponse(
            "login.html",
            {
                "request": req,
                "title": t("admin.login.title") if t else "Admin Login",
                "message": "invalid username",
                "username": username,
                "csrf_token": _issue_csrf(req),
            },
            status_code=401,
        )

    # 密码校验
    if not _verify_password(password or ""):
        _rate_fail(req, username)
        msg = "invalid password"
        if _env_pass_hash() and not _BCRYPT_AVAILABLE and (_env_pass_hash() or "").lower().startswith("bcrypt:"):
            msg = "bcrypt not installed on server"
        _audit("auth.password_invalid", False, req, note=msg)
        return tpl.TemplateResponse(
            "login.html",
            {
                "request": req,
                "title": t("admin.login.title") if t else "Admin Login",
                "message": msg,
                "username": username,
                "csrf_token": _issue_csrf(req),
            },
            status_code=401,
        )

    # 二次验证（优先 TOTP；否则 Telegram OTP 若已触发）
    totp_secret = _env_totp_secret()
    if totp_secret:
        if not (otp and _totp_verify(totp_secret, otp)):
            _rate_fail(req, username)
            _audit("auth.totp_invalid", False, req)
            return tpl.TemplateResponse(
                "login.html",
                {
                    "request": req,
                    "title": t("admin.login.title") if t else "Admin Login",
                    "message": "TOTP invalid or expired",
                    "username": username,
                    "csrf_token": _issue_csrf(req),
                    "totp_enabled": True,
                },
                status_code=401,
            )
    else:
        # 若已发送 OTP：必须校验
        if req.session.get("otp_code"):
            if not (otp and _otp_check(req, otp)):
                _rate_fail(req, username)
                _audit("auth.otp_invalid", False, req)
                return tpl.TemplateResponse(
                    "login.html",
                    {
                        "request": req,
                        "title": t("admin.login.title") if t else "Admin Login",
                        "message": "otp invalid or expired",
                        "username": username,
                        "csrf_token": _issue_csrf(req),
                    },
                    status_code=401,
                )

    # >>> 登录成功：写会话（与 deps.py 一致）
    req.session[SESSION_USER_KEY] = {
        "username": username.strip(),
        "tg_id": int(get_env("ADMIN_TG_ID") or 0),
    }
    req.session[TWOFA_PASSED_KEY] = True  # 高危操作会检查这个

    # 清理 OTP
    for k in ("otp_code", "otp_exp"):
        req.session.pop(k, None)

    # 节流记录重置
    _rate_reset(req, username)

    _audit("auth.login_ok", True, req)

    # 跳转后台首页
    return RedirectResponse(url="/admin", status_code=302)

@router.post("/logout")
def logout(req: Request, csrf_token: str = Form(default="")):
    # CSRF：若你在模板里为登出按钮也加上隐藏 CSRF 字段，这里会验证；否则可放宽。
    if csrf_token and not _check_csrf(req, csrf_token):
        return RedirectResponse("/admin/login?error=csrf+failed", status_code=303)
    _audit("auth.logout", True, req)
    req.session.clear()
    return RedirectResponse(url="/admin/login?error=logged+out", status_code=302)
