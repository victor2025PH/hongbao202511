# web_admin/controllers/dashboard.py
from __future__ import annotations

import time
import datetime as dt
from typing import Dict, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, case, and_

from core.i18n.i18n import t
# 读写分离：如未配置只读连接，deps 内部会回落到读写连接
from web_admin.deps import db_session_ro as db_session, require_admin

# —— 模型统一从 models 包导入（注意：你的充值模型名是 RechargeOrder，不是 Recharge）
from models.user import User
from models.envelope import Envelope
from models.ledger import Ledger
from models.recharge import RechargeOrder, OrderStatus  # ← 修正类名

router = APIRouter(prefix="/admin", tags=["admin-dashboard"])

# ---- 进程内软缓存（30 秒） ----
_CACHE: Dict[str, Any] = {"ts": 0, "data": None}
_CACHE_TTL = 30  # 秒


def _col(model, *names):
    """容错字段选择：按给定字段名依次找第一个存在的列属性。"""
    for n in names:
        if hasattr(model, n):
            return getattr(model, n)
    return None


def _stats_query(db) -> Dict[str, Any]:
    """
    用 3~4 个聚合查询拿到所有卡片数据。字段名不一致也能兜住。
    依赖索引建议：
      - users(created_at) [可选]
      - envelopes(status) 或 (remain_count, closed_at)
      - ledger(created_at), ledger(type, created_at)
      - recharge_orders(status), recharge_orders(status, created_at)
    """
    seven_days_ago = dt.datetime.utcnow().replace(tzinfo=None) - dt.timedelta(days=7)

    # ---- Users ----
    users_total = 0
    U_ID = _col(User, "tg_id", "id")
    if U_ID is not None:
        users_total = db.query(func.count(U_ID)).scalar() or 0

    # ---- Envelopes（活跃中）----
    # 有 status 就按 OPEN 认定“活跃”；否则按 remain_count>0 且未关闭
    E_STATUS = _col(Envelope, "status")
    E_CLOSED = _col(Envelope, "closed_at", "finished_at", "ended_at", "is_finished")
    E_REMAIN = _col(Envelope, "remain_count", "left_count", "rest_count", "left", "left_shares")

    q_env = db.query(func.count(1)).select_from(Envelope)
    if E_STATUS is not None:
        q_env = q_env.filter(E_STATUS.in_(["OPEN", "ACTIVE", "ONGOING"]))
    else:
        conds = []
        if E_REMAIN is not None:
            conds.append(E_REMAIN > 0)
        if E_CLOSED is not None:
            # 支持字段为布尔/时间的情况
            try:
                conds.append(and_(E_CLOSED.is_(None)))
            except Exception:
                pass
        if conds:
            q_env = q_env.filter(and_(*conds))
    envelopes_active = q_env.scalar() or 0

    # ---- Ledger（近 7 天 Σ 金额与条数，只算核心类型）----
    L_AMOUNT = _col(Ledger, "amount", "delta", "value")
    L_CREATED = _col(Ledger, "created_at", "created", "ts")
    L_TYPE = _col(Ledger, "type", "ltype")
    # 用你模型里的规范类型名
    FOCUS_TYPES = ("RECHARGE", "HONGBAO_SEND", "HONGBAO_GRAB", "ADJUSTMENT")

    if L_AMOUNT is not None:
        q_ledger = db.query(
            func.coalesce(func.sum(L_AMOUNT), 0).label("sum_amt"),
            func.count(1).label("cnt"),
        ).select_from(Ledger)
    else:
        q_ledger = db.query(func.count(1).label("cnt")).select_from(Ledger)

    if L_CREATED is not None:
        q_ledger = q_ledger.filter(L_CREATED >= seven_days_ago)
    if L_TYPE is not None:
        q_ledger = q_ledger.filter(L_TYPE.in_(FOCUS_TYPES))

    rec = q_ledger.first()
    ledger_7d_amount = ((rec.sum_amt if rec else 0) or 0) if L_AMOUNT is not None else 0
    ledger_7d_count = (rec.cnt if rec else 0) or 0

    # ---- Recharge（PENDING / SUCCESS 数量）----
    # 你的模型字段：RechargeOrder.status 是 Enum(OrderStatus)
    R_STATUS = _col(RechargeOrder, "status")
    recharge_pending = recharge_success = 0
    if R_STATUS is not None:
        # SQLAlchemy Enum 列直接与枚举值比较最稳妥
        q_re = db.query(
            func.sum(case((R_STATUS == OrderStatus.PENDING, 1), else_=0)).label("p"),
            func.sum(case((R_STATUS == OrderStatus.SUCCESS, 1), else_=0)).label("s"),
        ).select_from(RechargeOrder)
        row = q_re.first()
        recharge_pending = int((row.p if row else 0) or 0)
        recharge_success = int((row.s if row else 0) or 0)

    return {
        "users_total": int(users_total or 0),
        "envelopes_active": int(envelopes_active or 0),
        "ledger_7d_amount": f"{(ledger_7d_amount or 0):.2f}",
        "ledger_7d_count": int(ledger_7d_count or 0),
        "recharge_pending": int(recharge_pending or 0),
        "recharge_success": int(recharge_success or 0),
        # 修改点 1：返回 datetime，供模板 .strftime 使用
        "since": seven_days_ago,
        # 修改点 2：新增 until，模板若用 .strftime 也安全
        "until": dt.datetime.utcnow().replace(tzinfo=None),
    }


# -------- 首页跳转到仪表盘 --------
@router.get("", include_in_schema=False)
def _root():
    return RedirectResponse(url="/admin/dashboard")


# -------- 仪表盘页面 --------
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(req: Request, db=Depends(db_session), sess=Depends(require_admin)):
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] <= _CACHE_TTL:
        data = _CACHE["data"]
    else:
        data = _stats_query(db)
        _CACHE["ts"] = now
        _CACHE["data"] = data

    return req.app.state.templates.TemplateResponse(
        "dashboard.html",
        {
            "request": req,
            "title": t("admin.nav.dashboard"),
            "nav_active": "dashboard",
            "s": data,
        },
    )
