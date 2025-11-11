# web_admin/controllers/reset.py
from __future__ import annotations

import re
from decimal import Decimal
from typing import List, Dict, Tuple

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import asc, func

# i18n: 优先根级 i18n.py，失败再走 core.i18n.i18n
try:
    from i18n import t
except Exception:
    from core.i18n.i18n import t

from web_admin.deps import (
    db_session,
    GuardDangerOpWithReset,
    require_admin,
    csrf_protect,     # ✅ 新增：POST 路由 CSRF 守卫
    issue_csrf,       # ✅ 新增：GET/预览/结果页签发 CSRF
)
from models.user import User, get_balance, update_balance
from models.ledger import LedgerType

router = APIRouter(prefix="/admin/reset", tags=["admin-reset"])

ASSETS = ("USDT", "TON", "POINT", "ENERGY")
BATCH_SIZE = 200  # 批量执行限流，别把数据库打爆


# ------------ helpers ------------
def _split_users(raw: str) -> List[str]:
    """支持 ID / @username，逗号/空格/换行/分号/中文逗号 等分隔"""
    if not raw:
        return []
    raw = (
        raw.replace("，", ",")
           .replace("\r", ",")
           .replace("\n", ",")
           .replace("\t", ",")
           .replace(";", ",")
           .replace(" ", ",")
    )
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok:
            out.append(tok)
    return out


def _resolve_single(db, token: str) -> User | None:
    """数字当 tg_id，其他按 @username 小写匹配"""
    if re.fullmatch(r"\d{4,}", token):
        return db.query(User).filter(User.tg_id == int(token)).first()
    uname = token.lstrip("@").lower()
    return db.query(User).filter(func.lower(User.username) == uname).first()


def _resolve_users(db, tokens: List[str]) -> Tuple[List[User], List[str]]:
    found, missing = [], []
    for tok in tokens:
        u = _resolve_single(db, tok)
        if u:
            found.append(u)
        else:
            missing.append(tok)
    return found, missing


# ------------ pages ------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def reset_page(req: Request, sess=Depends(require_admin)):
    # ✅ 为选择页签发 CSRF
    csrf_token = issue_csrf(req)
    return req.app.state.templates.TemplateResponse(
        "reset_select.html",
        {
            "request": req,
            "title": t("admin.reset.title"),
            "nav_active": "reset",
            "csrf_token": csrf_token,  # 表单隐藏字段使用
        },
    )


# 预览（选中用户）
@router.post("/preview", response_class=HTMLResponse)
def reset_preview_selected(
    req: Request,
    users: str = Form(""),
    asset: str = Form(...),
    passphrase: str = Form(""),
    _=Depends(csrf_protect),          # ✅ 预览也需要 CSRF
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    asset = asset.upper().strip()
    if asset not in ASSETS:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))
    if not passphrase:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))

    tokens = _split_users(users)
    resolved, missing = _resolve_users(db, tokens)
    rows: List[Dict] = []
    total_will_deduct = Decimal("0")

    for u in resolved:
        bal = get_balance(db, u.tg_id, asset)
        if bal and bal != 0:
            rows.append({"user": u, "asset": asset, "balance": bal, "delta": -bal})
            total_will_deduct += abs(bal)

    # ✅ 预览页继续签发新的 CSRF，供确认执行使用
    csrf_token = issue_csrf(req)
    return req.app.state.templates.TemplateResponse(
        "reset_confirm.html",
        {
            "request": req,
            "title": t("admin.reset.confirm_title"),
            "nav_active": "reset",
            "mode": "selected",
            "asset": asset,
            "passphrase": passphrase,
            "rows": rows,
            "missing": missing,
            "total": str(total_will_deduct),
            # 模板如需回显原始 users
            "users_raw": users,
            "csrf_token": csrf_token,  # 确认执行表单使用
        },
    )


# 预览（全体）
@router.post("/preview_all", response_class=HTMLResponse)
def reset_preview_all(
    req: Request,
    asset: str = Form(...),
    passphrase: str = Form(""),
    _=Depends(csrf_protect),          # ✅ 预览也需要 CSRF
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    asset = asset.upper().strip()
    if asset not in ASSETS:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))
    if not passphrase:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))

    # 只做统计，别把全表搬到内存
    total_users = 0
    total_will_deduct = Decimal("0")

    q = db.query(User.tg_id).order_by(asc(User.tg_id))
    offset = 0
    while True:
        chunk = q.offset(offset).limit(BATCH_SIZE).all()
        if not chunk:
            break
        for (tg_id,) in chunk:
            bal = get_balance(db, tg_id, asset)
            if bal and bal != 0:
                total_users += 1
                total_will_deduct += abs(bal)

        offset += BATCH_SIZE

    # ✅ 预览页继续签发新的 CSRF，供确认执行使用
    csrf_token = issue_csrf(req)
    return req.app.state.templates.TemplateResponse(
        "reset_confirm.html",
        {
            "request": req,
            "title": t("admin.reset.confirm_title"),
            "nav_active": "reset",
            "mode": "everyone",
            "asset": asset,
            "passphrase": passphrase,
            "total_users": total_users,
            "total": str(total_will_deduct),
            "csrf_token": csrf_token,  # 确认执行表单使用
        },
    )


# 执行（选中用户）
@router.post("/do_selected", response_class=HTMLResponse)
def reset_do_selected(
    req: Request,
    users: str = Form(...),
    asset: str = Form(...),
    passphrase: str = Form(...),
    dryrun: int = Form(0),
    note: str = Form("RESET"),
    _=Depends(csrf_protect),          # ✅ 执行提交必须通过 CSRF
    db=Depends(db_session),
    sess=Depends(GuardDangerOpWithReset(5)),  # 超管 + 2FA + 环境允许
):
    asset = asset.upper().strip()
    tokens = _split_users(users)
    resolved, missing = _resolve_users(db, tokens)

    ok = 0
    fail = 0
    results: List[Dict] = []

    if dryrun:
        # 只统计
        total = Decimal("0")
        for u in resolved:
            bal = get_balance(db, u.tg_id, asset)
            if bal and bal != 0:
                total += (bal if bal > 0 else -bal)
        csrf_token = issue_csrf(req)  # ✅ 结果页也发一个新的 Token（便于继续操作）
        return req.app.state.templates.TemplateResponse(
            "reset_result.html",
            {
                "request": req,
                "title": t("admin.reset.title"),
                "nav_active": "reset",
                "mode": "selected",
                "asset": asset,
                "dryrun": True,
                "total": str(total),
                "missing": missing,
                "csrf_token": csrf_token,
            },
        )

    # 真执行（分批提交足够快，这里按用户循环后一次 commit）
    for u in resolved:
        try:
            bal = get_balance(db, u.tg_id, asset)
            if not bal or bal == 0:
                results.append({"u": u, "ok": True, "msg": t("common.skip") or "SKIP"})
                continue
            delta = -bal
            update_balance(
                db,
                u.tg_id,
                asset,
                delta,
                write_ledger=True,
                ltype=LedgerType.RESET,
                note=note[:120],
                operator_id=sess.get("tg_id"),
            )
            ok += 1
            results.append({"u": u, "ok": True, "msg": t("admin.toast.done") or "OK"})
        except Exception as e:
            fail += 1
            results.append({"u": u, "ok": False, "msg": str(e)})
    db.commit()

    csrf_token = issue_csrf(req)  # ✅ 结果页也发一个新的 Token
    return req.app.state.templates.TemplateResponse(
        "reset_result.html",
        {
            "request": req,
            "title": t("admin.reset.title"),
            "nav_active": "reset",
            "mode": "selected",
            "asset": asset,
            "dryrun": False,
            "ok": ok,
            "fail": fail,
            "missing": missing,
            "results": results,
            "csrf_token": csrf_token,
        },
    )


# 执行（全体）
@router.post("/do_all", response_class=HTMLResponse)
def reset_do_all(
    req: Request,
    asset: str = Form(...),
    passphrase: str = Form(...),
    dryrun: int = Form(0),
    note: str = Form("RESET"),
    _=Depends(csrf_protect),          # ✅ 执行提交必须通过 CSRF
    db=Depends(db_session),
    sess=Depends(GuardDangerOpWithReset(5)),  # 超管 + 2FA + 环境允许
):
    asset = asset.upper().strip()

    total_users = 0
    ok = 0
    fail = 0
    total_deduct = Decimal("0")
    results_sample: List[Dict] = []  # 只取前 20 条示例，避免页面爆炸

    if dryrun:
        q = db.query(User.tg_id).order_by(asc(User.tg_id))
        offset = 0
        while True:
            chunk = q.offset(offset).limit(BATCH_SIZE).all()
            if not chunk:
                break
            for (tg_id,) in chunk:
                bal = get_balance(db, tg_id, asset)
                if bal and bal != 0:
                    total_users += 1
                    total_deduct += (bal if bal > 0 else -bal)
            offset += BATCH_SIZE

        csrf_token = issue_csrf(req)  # ✅ 结果页也发一个新的 Token
        return req.app.state.templates.TemplateResponse(
            "reset_result.html",
            {
                "request": req,
                "title": t("admin.reset.title"),
                "nav_active": "reset",
                "mode": "everyone",
                "asset": asset,
                "dryrun": True,
                "total_users": total_users,
                "total": str(total_deduct),
                "csrf_token": csrf_token,
            },
        )

    # 真执行（分页 + 每页 commit，避免长事务）
    q = db.query(User.tg_id).order_by(asc(User.tg_id))
    offset = 0
    while True:
        chunk = q.offset(offset).limit(BATCH_SIZE).all()
        if not chunk:
            break
        for (tg_id,) in chunk:
            try:
                bal = get_balance(db, tg_id, asset)
                if not bal or bal == 0:
                    continue
                delta = -bal
                update_balance(
                    db,
                    tg_id,
                    asset,
                    delta,
                    write_ledger=True,
                    ltype=LedgerType.RESET,
                    note=note[:120],
                    operator_id=sess.get("tg_id"),
                )
                ok += 1
                total_deduct += (bal if bal > 0 else -bal)
                # 只收集少量示例
                if len(results_sample) < 20:
                    results_sample.append({"u": {"tg_id": tg_id}, "ok": True, "msg": t("admin.toast.done") or "OK"})
            except Exception as e:
                fail += 1
                if len(results_sample) < 20:
                    results_sample.append({"u": {"tg_id": tg_id}, "ok": False, "msg": str(e)})
        db.commit()
        offset += BATCH_SIZE

    csrf_token = issue_csrf(req)  # ✅ 结果页也发一个新的 Token
    return req.app.state.templates.TemplateResponse(
        "reset_result.html",
        {
            "request": req,
            "title": t("admin.reset.title"),
            "nav_active": "reset",
            "mode": "everyone",
            "asset": asset,
            "dryrun": False,
            "ok": ok,
            "fail": fail,
            "total": str(total_deduct),
            "sample": results_sample,
            "csrf_token": csrf_token,
        },
    )
