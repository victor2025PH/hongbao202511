# web_admin/controllers/a11y.py
from __future__ import annotations

import os
import sys
import json
import time
import socket
from pathlib import Path
from typing import Dict, Any, List, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from core.i18n.i18n import t
from web_admin.deps import db_session, require_admin

router = APIRouter(prefix="/admin/a11y", tags=["admin-a11y"])

# 你后台常用到的一些关键 i18n 键；缺了就会在页面上红出来
REQUIRED_I18N_KEYS = [
    # 登录/2FA
    "admin.login.title", "admin.login.username", "admin.login.password",
    "admin.login.submit", "admin.login.invalid", "admin.login.otp_input",
    "admin.login.otp_send", "admin.login.otp_hint", "admin.login.blocked",
    "admin.login.otp_required", "admin.login.otp_invalid",
    "admin.twofactor.need_otp", "admin.twofactor.ok",
    # 导航/通用
    "admin.nav.dashboard", "admin.common.search", "admin.common.actions",
    "admin.common.status", "admin.common.created_at", "admin.common.download",
    "admin.common.back", "admin.common.enable", "admin.common.disable",
    "admin.pagination.prev", "admin.pagination.next",
    # Dashboard
    "admin.dashboard.users_total", "admin.dashboard.envelopes_active",
    "admin.dashboard.ledger_7d_amount", "admin.dashboard.ledger_7d_count",
    "admin.dashboard.recharge_pending", "admin.dashboard.recharge_success",
    # Covers
    "admin.covers.title", "admin.covers.file_label", "admin.covers.title_label",
    "admin.covers.upload", "admin.covers.empty",
    # Export
    "admin.export.title", "admin.export.all_users_ledger", "admin.export.users_only",
    "admin.export.ledger_only", "admin.export.selected_users", "admin.export.users_label",
    "admin.export.done",
    # Adjust
    "admin.adjust.title", "admin.adjust.users", "admin.adjust.asset",
    "admin.adjust.amount", "admin.adjust.note", "admin.adjust.preview",
    "admin.adjust.execute", "admin.adjust.success_count", "admin.adjust.fail_count",
    "admin.adjust.insufficient",
    # Reset
    "admin.reset.title", "admin.reset.selected", "admin.reset.everyone",
    "admin.reset.passphrase", "admin.reset.confirm_title",
    "admin.reset.execute", "admin.reset.dryrun", "admin.reset.disallowed",
    # Recharge
    "admin.recharge.title", "admin.recharge.order_id", "admin.recharge.token",
    "admin.recharge.amount", "admin.recharge.network", "admin.recharge.address",
    "admin.recharge.final_pay", "admin.recharge.refresh", "admin.recharge.expire",
    "admin.recharge.payment_url",
    "admin.recharge.status.PENDING", "admin.recharge.status.SUCCESS",
    "admin.recharge.status.FAILED", "admin.recharge.status.EXPIRED",
    # 审计/审批/队列/标签/邀请/用户
    "admin.audit.title", "admin.audit.operator", "admin.audit.detail",
    "admin.approvals.title", "admin.approvals.need_two_admins",
    "admin.queue.title", "admin.tags.title", "admin.nav.invites",
    "admin.table.user", "admin.table.username", "admin.table.token",
    "admin.table.amount", "admin.table.result", "admin.table.note",
    # 设置
    "admin.settings.title", "admin.settings.feature_flags", "admin.settings.toggle_first",
    "admin.common.on", "admin.common.off", "admin.toast.done",
    # envelopes
    "admin.envelopes.title", "admin.envelopes.summary", "admin.envelopes.lucky",
    "admin.envelopes.claims",
]

def _i18n_probe() -> Dict[str, Any]:
    missing: List[str] = []
    # 简单策略：如果 t(key) 返回还带点 key 的原文，视为缺失
    for k in REQUIRED_I18N_KEYS:
        try:
            val = t(k)
        except Exception:
            val = k
        if not val or val == k or val.lower() == "missing":
            missing.append(k)
    return {
        "required": len(REQUIRED_I18N_KEYS),
        "missing": missing,
        "missing_count": len(missing),
    }

def _env_probe() -> Dict[str, Any]:
    def on(k: str) -> bool:
        v = os.getenv(k, "")
        return v not in ("", "0", "false", "False", "no", "NO")
    return {
        "ADMIN_WEB_USER": bool(os.getenv("ADMIN_WEB_USER")),
        "ADMIN_WEB_PASSWORD": bool(os.getenv("ADMIN_WEB_PASSWORD")),
        "ADMIN_WEB_PASSWORD_HASH": bool(os.getenv("ADMIN_WEB_PASSWORD_HASH")),
        "ADMIN_TG_ID": bool(os.getenv("ADMIN_TG_ID")),
        "ADMIN_SESSION_SECRET": bool(os.getenv("ADMIN_SESSION_SECRET")),
        "ALLOW_RESET": on("ALLOW_RESET"),
        "SUPER_ADMINS": bool(os.getenv("SUPER_ADMINS")),
        "REDIS_URL": bool(os.getenv("REDIS_URL")),
        "EXPORT_DIR": os.getenv("EXPORT_DIR") or "exports",
    }

def _fs_probe() -> Dict[str, Any]:
    checks: List[Tuple[str, bool, str]] = []
    def check(path: Path, must_write: bool) -> Tuple[str, bool, str]:
        ok = path.exists()
        msg = "exists" if ok else "missing"
        if ok and must_write:
            try:
                path.mkdir(parents=True, exist_ok=True)
                test = path / ".touch_test"
                test.write_text(str(time.time()))
                test.unlink(missing_ok=True)  # type: ignore
                msg = "writable"
            except Exception as e:
                ok = False
                msg = f"not writable: {e}"
        return (str(path), ok, msg)
    checks.append(check(Path("static/uploads"), True))
    checks.append(check(Path(os.getenv("EXPORT_DIR", "exports")), True))
    checks.append(check(Path("templates"), False))
    checks.append(check(Path("static"), False))
    return {
        "paths": [{"path": p, "ok": ok, "msg": msg} for p, ok, msg in checks],
    }

def _db_probe(db) -> Dict[str, Any]:
    try:
        db.execute(text("SELECT 1"))
        ok = True
        msg = "ok"
    except Exception as e:
        ok = False
        msg = str(e)
    return {"ok": ok, "msg": msg}

def _redis_probe() -> Dict[str, Any]:
    url = os.getenv("REDIS_URL") or ""
    if not url:
        return {"enabled": False, "ok": None, "msg": "REDIS_URL not set"}
    try:
        import redis  # type: ignore
        r = redis.from_url(url, socket_timeout=1.5)
        r.ping()
        return {"enabled": True, "ok": True, "msg": "pong"}
    except Exception as e:
        return {"enabled": True, "ok": False, "msg": str(e)}

def _net_probe() -> Dict[str, Any]:
    # 轻探外网 DNS 解析，别瞎搞 HTTP
    targets = ["api.telegram.org", "cdn.jsdelivr.net"]
    results = []
    for host in targets:
        try:
            socket.gethostbyname(host)
            results.append({"host": host, "ok": True})
        except Exception as e:
            results.append({"host": host, "ok": False, "msg": str(e)})
    return {"targets": results}

def _versions() -> Dict[str, Any]:
    try:
        import fastapi  # type: ignore
        import sqlalchemy  # type: ignore
        fv = fastapi.__version__
        sv = sqlalchemy.__version__
    except Exception:
        fv = sv = "unknown"
    return {
        "python": sys.version.split()[0],
        "fastapi": fv,
        "sqlalchemy": sv,
    }

def _summary(db) -> Dict[str, Any]:
    return {
        "versions": _versions(),
        "env": _env_probe(),
        "fs": _fs_probe(),
        "db": _db_probe(db),
        "redis": _redis_probe(),
        "net": _net_probe(),
        "i18n": _i18n_probe(),
    }

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def a11y_page(req: Request, db=Depends(db_session), sess=Depends(require_admin)):
    data = _summary(db)
    return req.app.state.templates.TemplateResponse(
        "a11y.html",
        {
            "request": req,
            "title": "Health & A11y",
            "nav_active": "a11y",
            "data": data,
        },
    )

@router.get("/json")
def a11y_json(db=Depends(db_session), sess=Depends(require_admin)):
    return JSONResponse(_summary(db))
