# web_admin/controllers/ledger.py
from __future__ import annotations

import csv
import io
import math
import datetime as dt
import logging
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from sqlalchemy import desc, or_, func, cast, String

from core.i18n.i18n import t
from web_admin.deps import db_session, require_admin
from models.ledger import Ledger
from models.user import User

router = APIRouter(prefix="/admin/ledger", tags=["admin-ledger"])

# 默认分页
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


# 兼容列名选择器
def _col(model, *names):
    for n in names:
        if hasattr(model, n):
            return getattr(model, n)
    return None


L_ID = _col(Ledger, "id")
L_USER = _col(Ledger, "user_id", "uid", "tg_id")
L_TOKEN = _col(Ledger, "token", "asset", "currency")
L_AMOUNT = _col(Ledger, "amount", "delta", "value")
L_TYPE = _col(Ledger, "type", "ltype")
L_NOTE = _col(Ledger, "note", "memo", "remark")
L_CREATED = _col(Ledger, "created_at", "created", "ts", "timestamp")
L_ENV = _col(Ledger, "envelope_id", "env_id")
L_ORDER = _col(Ledger, "order_id")

U_ID = _col(User, "tg_id", "id", "user_id")
U_NAME = _col(User, "username", "name")

if not all([L_USER, L_AMOUNT, L_CREATED]):
    logging.warning(
        "Ledger schema is missing key columns; continuing with degraded view."
    )


# === 智能匹配 Ledger↔User 的外键关系，避免错位 JOIN ===
def _resolve_user_join_cols():
    """
    - 若 Ledger 有 tg_id，则优先与 User.tg_id（无则退 User.id）
    - 若 Ledger 有 user_id/uid，则优先与 User.id（无则退 User.tg_id）
    - 上述都没有就回落到 (L_USER, U_ID)
    """

    def _has(model, name):
        return hasattr(model, name)

    def _get(model, name):
        return getattr(model, name, None)

    # Ledger.tg_id → User.tg_id / User.id
    if _has(Ledger, "tg_id"):
        left = _get(Ledger, "tg_id")
        right = _get(User, "tg_id") or _get(User, "id")
        if left is not None and right is not None:
            return left, right

    # Ledger.user_id / Ledger.uid → User.id / User.tg_id
    for cand in ("user_id", "uid"):
        if _has(Ledger, cand):
            left = _get(Ledger, cand)
            right = _get(User, "id") or _get(User, "tg_id")
            if left is not None and right is not None:
                return left, right

    # 兜底
    return L_USER, U_ID


JOIN_L_USER, JOIN_U_ID = _resolve_user_join_cols()


def _dt(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _apply_filters(
    qset,
    user: Optional[str],
    token: Optional[str],
    ltype: Optional[str],
    min_amount: Optional[float],
    max_amount: Optional[float],
    start: Optional[str],
    end: Optional[str],
    q: Optional[str],
):
    # 用户过滤：@username 或整型 ID（与 Ledger 上的用户列匹配）
    if user:
        u = user.strip()
        if u.startswith("@") and U_NAME is not None:
            # 关键修正：加通配符，否则 ilike 不会“包含匹配”
            uname = u.lstrip("@")
            qset = qset.filter(U_NAME.ilike(f"%{uname}%"))
        else:
            try:
                uid = int(u)
                qset = qset.filter(L_USER == uid)
            except ValueError:
                pass

    # 币种、类型
    if token and L_TOKEN is not None:
        qset = qset.filter(L_TOKEN == token.upper())
    if ltype and L_TYPE is not None:
        qset = qset.filter(L_TYPE == ltype.upper())

    # 金额区间
    if min_amount is not None:
        qset = qset.filter(L_AMOUNT >= min_amount)
    if max_amount is not None:
        qset = qset.filter(L_AMOUNT <= max_amount)

    # 时间区间
    sdt = _dt(start)
    edt = _dt(end)
    if sdt:
        qset = qset.filter(L_CREATED >= sdt)
    if edt:
        # 纯日期尾部补一天
        qset = qset.filter(
            L_CREATED < (edt + dt.timedelta(days=1) if len(end or "") == 10 else edt)
        )

    # 关键词搜 note / order / env（整型列先 cast 为 String 再 ilike）
    if q:
        like = f"%{q.strip()}%"
        conds = []
        if L_NOTE is not None:
            conds.append(L_NOTE.ilike(like))
        if L_ORDER is not None:
            conds.append(cast(L_ORDER, String).ilike(like))
        if L_ENV is not None:
            conds.append(cast(L_ENV, String).ilike(like))
        if conds:
            qset = qset.filter(or_(*conds))

    return qset


def _row_to_view(lg: Ledger, u: User) -> Dict[str, Any]:
    """将 (Ledger, User) 转为干净的 dict，避免模板取 ORM 属性差异。"""

    def _get(obj, col, default=None):
        if not col:
            return default
        key = getattr(col, "key", None) or getattr(col, "name", None)
        return getattr(obj, key, default)

    return {
        "id": _get(lg, L_ID),
        "created_at": _get(lg, L_CREATED),
        "type": _get(lg, L_TYPE),
        "token": _get(lg, L_TOKEN),
        "amount": _get(lg, L_AMOUNT),
        "note": _get(lg, L_NOTE),
        # 用户侧字段：模板优先展示 tg_id 与 username
        "tg_id": _get(u, _col(User, "tg_id")),
        "username": _get(u, U_NAME),
        "user_id": _get(lg, L_USER),
        "envelope_id": _get(lg, L_ENV),
        "order_id": _get(lg, L_ORDER),
    }


def _qset_base(db):
    return (
        db.query(Ledger, User)
        .join(User, JOIN_U_ID == JOIN_L_USER)
        .order_by(desc(L_CREATED))
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def ledger_list(
    req: Request,
    db=Depends(db_session),
    sess=Depends(require_admin),
    page: int = 1,
    per_page: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    user: Optional[str] = None,  # tg_id 或 @username
    token: Optional[str] = None,
    # 统一: 前端传 ?type=，后端变量仍叫 ltype
    ltype: Optional[str] = Query(None, alias="type"),
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    start: Optional[str] = None,  # ISO 2025-01-01 或完整时间
    end: Optional[str] = None,
    q: Optional[str] = None,  # note/order/env 模糊匹配
):
    qset = _apply_filters(
        _qset_base(db), user, token, ltype, min_amount, max_amount, start, end, q
    )

    total = qset.count()
    rows = qset.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = max(1, math.ceil(total / per_page))

    view_rows: List[Dict[str, Any]] = []
    for lg, u in rows:
        view_rows.append(_row_to_view(lg, u))

    # 总和（受同样筛选）——通过子查询列求和，避免笛卡尔积
    subq = qset.with_entities(L_AMOUNT.label("amt")).subquery()
    total_amount = db.query(func.coalesce(func.sum(subq.c.amt), 0)).scalar()

    return req.app.state.templates.TemplateResponse(
        "ledger.html",
        {
            "request": req,
            "title": "Ledger",
            "nav_active": "audit",
            "rows": view_rows,
            "page": page,
            "total_pages": total_pages,
            "total_amount": total_amount,
            "filters": {
                "user": user or "",
                "token": token or "",
                "type": ltype or "",
                "min_amount": min_amount if min_amount is not None else "",
                "max_amount": max_amount if max_amount is not None else "",
                "start": start or "",
                "end": end or "",
                "q": q or "",
            },
        },
    )


@router.get("/export.csv")
def ledger_export_csv(
    db=Depends(db_session),
    sess=Depends(require_admin),
    user: Optional[str] = None,
    token: Optional[str] = None,
    ltype: Optional[str] = Query(None, alias="type"),
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(20000, ge=1, le=200000),  # 保护一下
):
    qset = _apply_filters(
        _qset_base(db), user, token, ltype, min_amount, max_amount, start, end, q
    )

    rows = qset.limit(limit).all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "id",
            "created_at",
            "user_id",
            "username",
            "type",
            "token",
            "amount",
            "note",
            "envelope_id",
            "order_id",
        ]
    )
    for lg, u in rows:
        view = _row_to_view(lg, u)
        w.writerow(
            [
                view.get("id", ""),
                view.get("created_at", ""),
                view.get("user_id", ""),
                view.get("username", ""),
                view.get("type", ""),
                view.get("token", ""),
                view.get("amount", ""),
                view.get("note", ""),
                view.get("envelope_id", ""),
                view.get("order_id", ""),
            ]
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=ledger.csv"},
    )


@router.get("/export.json")
def ledger_export_json(
    db=Depends(db_session),
    sess=Depends(require_admin),
    user: Optional[str] = None,
    token: Optional[str] = None,
    ltype: Optional[str] = Query(None, alias="type"),
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(20000, ge=1, le=200000),
):
    qset = _apply_filters(
        _qset_base(db), user, token, ltype, min_amount, max_amount, start, end, q
    )
    rows = qset.limit(limit).all()
    data = [_row_to_view(lg, u) for lg, u in rows]
    return JSONResponse({"total": len(data), "items": data})
