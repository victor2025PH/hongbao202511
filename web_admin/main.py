from __future__ import annotations

# 调试：确认载入的是这份文件（可保留）
print("[boot] web_admin.main loaded from:", __file__)

import os
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from config.load_env import load_env
load_env()


# i18n
from core.i18n.i18n import t

# Auth 与模板注入
from web_admin.auth import router as auth_router, inject_templates

# 业务控制器（缺谁谁自己补）
from web_admin.controllers.dashboard import router as dashboard_router
from web_admin.controllers.covers import router as covers_router
from web_admin.controllers.export import router as export_router
from web_admin.controllers.adjust import router as adjust_router
from web_admin.controllers.reset import router as reset_router
from web_admin.controllers.envelopes import router as envelopes_router
from web_admin.controllers.recharge import router as recharge_router
from web_admin.controllers.settings import router as settings_router
from web_admin.controllers.audit import router as audit_router
from web_admin.controllers.approvals import router as approvals_router
from web_admin.controllers.queue import router as queue_router
from web_admin.controllers.public_groups import router as public_groups_router
from web_admin.controllers.public_group_reports import router as public_group_reports_router
from web_admin.controllers.invites import router as invites_router
from web_admin.controllers.users import router as users_router
from web_admin.controllers.a11y import router as a11y_router
from web_admin.controllers.ledger import router as ledger_router
from web_admin.controllers.ipn import router as ipn_router
from web_admin.controllers.sheet_users import router as sheet_users_router

# 仅保留 FastAPI 版本的 StaticFiles
from fastapi.staticfiles import StaticFiles
from monitoring.metrics import render_prometheus


def create_app() -> FastAPI:
    app = FastAPI(title="Admin Console", docs_url=None, redoc_url=None)

    # -----------------------------
    # 静态目录
    # -----------------------------
    static_dir = os.getenv("STATIC_DIR", "static")
    os.makedirs(static_dir, exist_ok=True)
    os.makedirs(os.path.join(static_dir, "uploads"), exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # 文件下载/导出目录（若 .env 未给 FILES_DIR，就回退到 static/uploads）
    FILES_DIR = os.getenv("FILES_DIR")
    if not FILES_DIR:
        FILES_DIR = os.path.join(static_dir, "uploads")
    os.makedirs(FILES_DIR, exist_ok=True)
    # 同时把它挂到 app.state 上，后续其他模块要用可从这里取
    app.state.FILES_DIR = FILES_DIR

    # -----------------------------
    # 模板
    # -----------------------------
    templates = Jinja2Templates(directory=os.getenv("TEMPLATE_DIR", "templates"))
    # 注入 i18n 与一些全局方法
    templates.env.globals["t"] = t
    templates.env.globals["now"] = lambda: datetime.now().strftime("%Y")

    # === 这里新增：Jinja 通用属性/键过滤器，修复 |attribute 报错 ===
    def _jinja_attribute(obj, name, default=None):
        """
        统一支持两种取值方式：
        - 字典：obj[name]
        - 对象：getattr(obj, name)
        任意失败返回 default（默认 None），避免模板渲染直接崩溃。
        """
        try:
            if isinstance(obj, dict):
                return obj.get(name, default)
            return getattr(obj, name, default)
        except Exception:
            return default

    templates.env.filters["attribute"] = _jinja_attribute
    templates.env.filters["attr"] = _jinja_attribute  # 兼容别名
    # === /新增结束 ===

    app.state.templates = templates
    inject_templates(templates)  # 给 auth 模块

    # -----------------------------
    # 会话中间件
    # -----------------------------
    session_secret = os.getenv("ADMIN_SESSION_SECRET", "dev_secret")
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site="lax",
        https_only=False,   # 如有 HTTPS 反代可改 True
        max_age=60 * 60 * 8,
    )

    # -----------------------------
    # 压缩中间件（节省带宽）
    # -----------------------------
    gzip_min_size = int(os.getenv("GZIP_MIN_SIZE", "1024") or "1024")
    app.add_middleware(GZipMiddleware, minimum_size=gzip_min_size)

    # -----------------------------
    # 安全响应头中间件（CSP、XFO、XCTO、RP 等）
    # 可通过环境变量覆写 CSP，默认兼容现有页面（允许内联脚本/样式）
    # -----------------------------
    class SecurityHeadersMiddleware:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app
            # 默认较宽松，便于逐步收紧
            default_csp = (
                "default-src 'self'; "
                "img-src 'self' data: https:; "
                "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
                "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
                "font-src 'self' data:; "
                "connect-src 'self' https:; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )
            self.csp = os.getenv("ADMIN_CSP", default_csp)
            self.referrer = os.getenv("REFERRER_POLICY", "no-referrer")
            self.xfo = os.getenv("X_FRAME_OPTIONS", "DENY")
            self.xcto = os.getenv("X_CONTENT_TYPE_OPTIONS", "nosniff")
            self.xxss = os.getenv("X_XSS_PROTECTION", "0")  # 现代浏览器已废弃
            self.perms = os.getenv("PERMISSIONS_POLICY", "geolocation=(), microphone=(), camera=()")

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = message.setdefault("headers", [])
                    def _set_header(k: str, v: str):
                        headers.append((k.encode("latin-1"), v.encode("latin-1")))
                    _set_header("content-security-policy", self.csp)
                    _set_header("referrer-policy", self.referrer)
                    _set_header("x-frame-options", self.xfo)
                    _set_header("x-content-type-options", self.xcto)
                    _set_header("permissions-policy", self.perms)
                    # HSTS 仅在 HTTPS 环境启用（避免本地开发被缓存）
                    if os.getenv("ENABLE_HSTS", "0") == "1":
                        _set_header("strict-transport-security", "max-age=63072000; includeSubDomains; preload")
                await send(message)

            await self.app(scope, receive, send_wrapper)

    app.add_middleware(SecurityHeadersMiddleware)

    # -----------------------------
    # 首页跳转：把 / 落到 Dashboard
    # -----------------------------
    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/admin")

    # -----------------------------
    # 健康/就绪检查
    # -----------------------------
    app.state.start_time = datetime.utcnow()

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        # 简单返回；可扩展：DB 连接、外部服务连通性等
        return JSONResponse({"ok": True, "ts": datetime.utcnow().isoformat()})

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        checks = {}

        checks["static_dir"] = os.path.isdir(static_dir)
        template_dir = os.getenv("TEMPLATE_DIR", "templates")
        checks["templates"] = os.path.isdir(template_dir)

        try:
            from models.db import engine  # 延迟导入，避免应用初始化时循环依赖

            with engine.connect() as conn:
                conn.execute("SELECT 1")
            checks["database"] = True
        except Exception:
            checks["database"] = False

        ready = all(checks.values())
        return JSONResponse({"ready": ready, "checks": checks})

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        start_time = getattr(app.state, "start_time", datetime.utcnow())
        uptime_seconds = (datetime.utcnow() - start_time).total_seconds()
        lines = [
            "# HELP app_uptime_seconds Application uptime in seconds.",
            "# TYPE app_uptime_seconds counter",
            f"app_uptime_seconds {uptime_seconds:.0f}",
            "# HELP app_info Application info.",
            "# TYPE app_info gauge",
            'app_info{app="telegram-hongbao-web-admin"} 1',
        ]
        extra = render_prometheus()
        if extra:
            lines.extend(extra)
        body = "\n".join(lines) + "\n"
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    # -----------------------------
    # 路由注册（顺序基本不敏感）
    # -----------------------------
    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(covers_router)
    app.include_router(export_router)
    app.include_router(adjust_router)
    app.include_router(reset_router)
    app.include_router(envelopes_router)
    app.include_router(recharge_router)
    app.include_router(settings_router)
    app.include_router(audit_router)
    app.include_router(approvals_router)
    app.include_router(queue_router)
    app.include_router(invites_router)
    app.include_router(users_router)
    app.include_router(public_groups_router)
    app.include_router(public_group_reports_router)
    app.include_router(a11y_router)
    app.include_router(ledger_router)
    app.include_router(ipn_router)
    app.include_router(sheet_users_router)  # /admin/sheet-users

    # /files 静态挂载（用于下载导出文件、临时文件等）
    app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

    # -----------------------------
    # 统一异常处理
    # -----------------------------
    @app.exception_handler(StarletteHTTPException)
    async def http_exc_handler(request: Request, exc: StarletteHTTPException):
        # 401 未登录：发回登录页
        if exc.status_code == 401:
            return RedirectResponse("/admin/login?error=login+required", status_code=303)
        # 403：只有明确 detail 指 2FA 才跳二次校验页
        if exc.status_code == 403:
            detail = (exc.detail or "") if isinstance(exc.detail, str) else ""
            if "2FA required" in detail:
                return RedirectResponse("/admin/twofactor?error=2FA+required", status_code=303)
            return RedirectResponse("/admin/login?error=forbidden", status_code=303)
        # 其他
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    return app


# ---- 关键导出：给 uvicorn 找到 ASGI 实例 ----
app = create_app()
__all__ = ["app"]

# 如需直接用 python 运行（可选）
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_admin.main:app", reload=True, port=int(os.getenv("PORT", "8080")))
