# web_admin/controllers/envelopes.py
from __future__ import annotations

import math
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, desc

from core.i18n.i18n import t
from web_admin.deps import db_session, require_admin

# 你的模型
from models.envelope import Envelope
from models.user import User
from models.ledger import Ledger

router = APIRouter(prefix="/admin/envelopes", tags=["admin-envelopes"])

# ---------- 兼容型取列 ----------
def _col(model, *names):
    for n in names:
        if hasattr(model, n):
            return getattr(model, n)
    return None

# ---------- 降级实现：summary ----------
def _summary(db, eid: int) -> Dict[str, Any]:
    # 先拿到红包
    env: Envelope | None = db.query(Envelope).filter(Envelope.id == eid).first()
    if not env:
        raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))

    # 字段猜测与容错
    token_col = _col(Envelope, "token", "asset", "currency")
    total_amount_col = _col(Envelope, "total_amount", "amount", "sum_amount")
    total_count_col = _col(Envelope, "total_count", "count", "pieces")
    remain_amount_col = _col(Envelope, "remain_amount", "left_amount", "rest_amount")
    remain_count_col = _col(Envelope, "remain_count", "left_count", "rest_count")
    created_at_col = _col(Envelope, "created_at", "created", "ts")
    closed_at_col = _col(Envelope, "closed_at", "finished_at", "ended_at")
    creator_id_col = _col(Envelope, "creator_id", "owner_id", "tg_id")

    token = getattr(env, token_col.key) if token_col else "POINT"
    total_amount = getattr(env, total_amount_col.key) if total_amount_col else 0
    total_count = getattr(env, total_count_col.key) if total_count_col else 0
    remain_amount = getattr(env, remain_amount_col.key) if remain_amount_col else None
    remain_count = getattr(env, remain_count_col.key) if remain_count_col else None
    created_at = getattr(env, created_at_col.key) if created_at_col else None
    closed_at = getattr(env, closed_at_col.key) if closed_at_col else None
    creator_id = getattr(env, creator_id_col.key) if creator_id_col else None

    # 领取统计来自 Ledger：类型一般是 CLAIM/ENVELOPE_CLAIM
    ltype_col = _col(Ledger, "type", "ltype")
    amount_col = _col(Ledger, "amount", "delta", "value")
    note_col = _col(Ledger, "note", "memo")
    created_col = _col(Ledger, "created_at", "created", "ts")
    token_l_col = _col(Ledger, "token", "asset", "currency")
    env_id_col = _col(Ledger, "envelope_id", "env_id")

    claimed_amount = 0
    claimed_count = 0
    if all([ltype_col, amount_col, token_l_col, env_id_col]):
        try:
            q = (
                db.query(func.coalesce(func.sum(amount_col), 0), func.count(1))
                .filter(env_id_col == eid)
                .filter(token_l_col == token)
                .filter(ltype_col.in_(["CLAIM", "ENVELOPE_CLAIM"]))
            )
            claimed_amount, claimed_count = q.first()
        except Exception:
            pass

    # 幸运王：按同一 envelope_id 的单笔最大领取额找用户
    lucky: Optional[Dict[str, Any]] = None
    if all([ltype_col, amount_col, env_id_col]):
        try:
            sub = (
                db.query(
                    Ledger.user_id.label("uid"),
                    amount_col.label("amt"),
                )
                .filter(env_id_col == eid)
                .order_by(desc(amount_col))
                .limit(1)
                .subquery()
            )
            row = db.query(sub.c.uid, sub.c.amt).first()
            if row:
                u = db.query(User).filter(User.tg_id == row.uid).first()
                lucky = {
                    "tg_id": row.uid,
                    "username": getattr(u, "username", None) if u else None,
                    "amount": row.amt,
                }
        except Exception:
            pass

    # 创建者
    creator = None
    if creator_id:
        creator = db.query(User).filter(User.tg_id == creator_id).first()

    # 剩余推断
    if remain_amount is None and total_amount is not None:
        remain_amount = total_amount - (claimed_amount or 0)
    if remain_count is None and total_count is not None:
        remain_count = total_count - (claimed_count or 0)

    return {
        "envelope": env,
        "token": token,
        "total_amount": total_amount,
        "total_count": total_count,
        "claimed_amount": claimed_amount,
        "claimed_count": claimed_count,
        "remain_amount": remain_amount,
        "remain_count": remain_count,
        "created_at": created_at,
        "closed_at": closed_at,
        "creator": creator,
        "lucky": lucky,
    }

# ---------- 明细分页 ----------
def _claims_page(db, eid: int, page: int, per_page: int = 20):
    ltype_col = _col(Ledger, "type", "ltype")
    amount_col = _col(Ledger, "amount", "delta", "value")
    created_col = _col(Ledger, "created_at", "created", "ts")
    user_id_col = _col(Ledger, "user_id", "uid", "tg_id")
    env_id_col = _col(Ledger, "envelope_id", "env_id")

    if not all([ltype_col, amount_col, created_col, user_id_col, env_id_col]):
        return [], 0

    base = (
        db.query(Ledger, User)
        .join(User, User.tg_id == user_id_col)
        .filter(env_id_col == eid)
        .filter(ltype_col.in_(["CLAIM", "ENVELOPE_CLAIM"]))
        .order_by(desc(created_col))
    )
    total = base.count()
    rows = base.offset((page - 1) * per_page).limit(per_page).all()
    return rows, total

# ---------- 路由 ----------
@router.get("/{eid}", response_class=HTMLResponse)
def envelope_detail(
    req: Request,
    eid: int,
    page: int = 1,
    per_page: int = 20,
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    info = _summary(db, eid)
    rows, total = _claims_page(db, eid, page, per_page)
    total_pages = max(1, math.ceil(total / per_page)) if per_page else 1

    return req.app.state.templates.TemplateResponse(
        "envelopes_view.html",
        {
            "request": req,
            "title": t("admin.envelopes.title"),
            "nav_active": "envelopes",
            "eid": eid,
            "info": info,
            "rows": rows,        # 每条是 (Ledger, User)
            "page": page,
            "total_pages": total_pages,
        },
    )
