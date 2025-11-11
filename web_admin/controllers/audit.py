# web_admin/controllers/audit.py
from __future__ import annotations

import csv
import io
import math
import datetime as dt
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from sqlalchemy import desc, or_, func, cast, String

from core.i18n.i18n import t
from web_admin.deps import db_session, require_admin
from models.ledger import Ledger
from models.user import User

router = APIRouter(prefix="/admin/audit", tags=["admin-audit"])

# 与 /admin/ledger 保持一致的分页限制
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# 兼容列名选择器
def _col(model, *names):
    for n in names:
        if hasattr(model, n):
            return getattr(model, n)
    return None

# Ledger 列
L_ID      = _col(Ledger, "id")
L_USER    = _col(Ledger, "user_id", "uid", "tg_id")
L_TOKEN   = _col(Ledger, "token", "asset", "currency")
L_AMOUNT  = _col(Ledger, "amount", "delta", "value")
L_TYPE    = _col(Ledger, "type", "ltype")
L_NOTE    = _col(Ledger, "note", "memo", "remark")
L_CREATED = _col(Ledger, "created_at", "created", "ts", "timestamp")
L_ENV     = _col(Ledger, "envelope_id", "env_id")
L_ORDER   = _col(Ledger, "order_id")
L_OPERATOR= _col(Ledger, "operator_id", "op_id")

# User 列
U_ID      = _col(User, "tg_id", "id", "user_id")
U_NAME    = _col(User, "username", "name")

if not all([L_USER, L_AMOUNT, L_CREATED]):
    raise RuntimeError("Ledger schema missing key columns (user/amount/created_at)")

# 智能匹配 Ledger↔User 的关联列，避免错位 JOIN
def _resolve_user_join_cols():
    def _has(model, name): return hasattr(model, name)
    def _get(model, name): return getattr(model, name, None)

    if _has(Ledger, "tg_id"):
        left = _get(Ledger, "tg_id")
        right = _get(User, "tg_id") or _get(User, "id")
        if left is not None and right is not None:
            return left, right

    for cand in ("user_id", "uid"):
        if _has(Ledger, cand):
            left = _get(Ledger, cand)
            right = _get(User, "id") or _get(User, "tg_id")
            if left is not None and right is not None:
                return left, right

    return L_USER, U_ID

JOIN_L_USER, JOIN_U_ID = _resolve_user_join_cols()

# 审计关注的类型（默认）
FOCUS_TYPES = ("RESET", "ADJUST", "RECHARGE", "CLAIM", "ENVELOPE_CLAIM")

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
    ltypes: Optional[List[str]],
    operator: Optional[int],
    min_amount: Optional[float],
    max_amount: Optional[float],
    start: Optional[str],
    end: Optional[str],
    q: Optional[str],
):
    # 用户：@username 或数值 ID
    if user:
        u = user.strip()
        if u.startswith("@") and U_NAME is not None:
            uname = u.lstrip("@")
            # 审计页更偏“等值命中”，如需模糊可改 .ilike(f"%{uname}%")
            qset = qset.filter(U_NAME == uname)
        else:
            try:
                uid = int(u)
                qset = qset.filter(L_USER == uid)
            except ValueError:
                pass

    # 币种
    if token and L_TOKEN is not None:
        qset = qset.filter(L_TOKEN == token.upper())

    # 类型：支持多选；如未指定，用默认关注集合
    if ltypes and L_TYPE is not None:
        types_norm = [s.upper() for s in ltypes if s]
        if len(types_norm) == 1:
            qset = qset.filter(L_TYPE == types_norm[0])
        elif len(types_norm) > 1:
            qset = qset.filter(L_TYPE.in_(types_norm))
    elif L_TYPE is not None:
        qset = qset.filter(L_TYPE.in_(FOCUS_TYPES))

    # 操作人（如果有该列）
    if operator and L_OPERATOR is not None:
        qset = qset.filter(L_OPERATOR == int(operator))

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
        qset = qset.filter(L_CREATED < (edt + dt.timedelta(days=1) if len(end or "") == 10 else edt))

    # 关键词搜：note/order/env（order/env 可能为整型 → cast 为 String 再 ilike）
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
        "tg_id": _get(u, _col(User, "tg_id")),
        "username": _get(u, U_NAME),
        "user_id": _get(lg, L_USER),
        "envelope_id": _get(lg, L_ENV),
        "order_id": _get(lg, L_ORDER),
        "operator_id": _get(lg, L_OPERATOR),
    }

def _qset_base(db):
    return db.query(Ledger, User).join(User, JOIN_U_ID == JOIN_L_USER).order_by(desc(L_CREATED))

# -----------------------
# 页面：审计列表（与账本对齐）
# -----------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def audit_list(
    req: Request,
    db=Depends(db_session),
    sess=Depends(require_admin),
    page: int = 1,
    per_page: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    # 与旧版兼容：ltype 单值；也支持 types 多值（?types=RESET&types=ADJUST）
    ltype: Optional[str] = Query(None, description="Ledger type"),
    types: Optional[List[str]] = Query(None, description="Multiple ledger types"),
    token: Optional[str] = None,
    user: Optional[str] = None,          # tg_id 或 @username
    operator: Optional[int] = None,      # 操作人 tg_id
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    start: Optional[str] = None,         # ISO 日期 2025-01-01 / 完整时间
    end: Optional[str] = None,           # ISO 日期 / 完整时间
    q: Optional[str] = None,             # 备注/订单/红包关键词
):
    # 组合类型过滤集合
    ltypes = types[:] if types else []
    if ltype:
        ltypes.append(ltype)

    qset = _apply_filters(
        _qset_base(db), user, token, ltypes, operator, min_amount, max_amount, start, end, q
    )

    total = qset.count()
    rows = qset.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = max(1, math.ceil(total / per_page))

    view_rows: List[Dict[str, Any]] = []
    for lg, u in rows:
        view_rows.append(_row_to_view(lg, u))

    # 总金额（受相同筛选）
    subq = qset.with_entities(L_AMOUNT.label("amt")).subquery()
    sum_amount = db.query(func.coalesce(func.sum(subq.c.amt), 0)).scalar()

    return req.app.state.templates.TemplateResponse(
        "audit.html",
        {
            "request": req,
            "title": t("admin.audit.title"),
            "nav_active": "audit",
            "rows": view_rows,                 # 统一为 dict
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "filters": {
                "types": ltypes or [],
                "token": token or "",
                "user": user or "",
                "operator": operator or "",
                "min_amount": min_amount if min_amount is not None else "",
                "max_amount": max_amount if max_amount is not None else "",
                "start": start or "",
                "end": end or "",
                "q": q or "",
            },
            "sum_amount": sum_amount,
        },
    )

# -----------------------
# 导出：CSV
# -----------------------
@router.get("/export.csv")
def audit_export_csv(
    db=Depends(db_session),
    sess=Depends(require_admin),
    ltype: Optional[str] = Query(None),
    types: Optional[List[str]] = Query(None),
    token: Optional[str] = None,
    user: Optional[str] = None,
    operator: Optional[int] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(20000, ge=1, le=200000),
):
    ltypes = list(types) if types else []
    if ltype:
        ltypes.append(ltype)

    qset = _apply_filters(
        _qset_base(db), user, token, ltypes, operator, min_amount, max_amount, start, end, q
    )
    rows = qset.limit(limit).all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "user_id", "username", "type", "token", "amount", "note", "envelope_id", "order_id", "operator_id"])
    for lg, u in rows:
        view = _row_to_view(lg, u)
        w.writerow([
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
            view.get("operator_id", ""),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit.csv"},
    )

# -----------------------
# 导出：JSON
# -----------------------
@router.get("/export.json")
def audit_export_json(
    db=Depends(db_session),
    sess=Depends(require_admin),
    ltype: Optional[str] = Query(None),
    types: Optional[List[str]] = Query(None),
    token: Optional[str] = None,
    user: Optional[str] = None,
    operator: Optional[int] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(20000, ge=1, le=200000),
):
    ltypes = list(types) if types else []
    if ltype:
        ltypes.append(ltype)

    qset = _apply_filters(
        _qset_base(db), user, token, ltypes, operator, min_amount, max_amount, start, end, q
    )
    rows = qset.limit(limit).all()
    data = [_row_to_view(lg, u) for lg, u in rows]
    return JSONResponse({"total": len(data), "items": data})
