# web_admin/controllers/recharge.py
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, or_, cast, String

from core.i18n.i18n import t
from web_admin.deps import (
    db_session,
    require_admin,
    GuardDangerOp,
    csrf_protect,   # ✅ 新增：POST 路由 CSRF 校验
    issue_csrf,     # ✅ 新增：GET 列表页签发 CSRF
)
from models.recharge import RechargeOrder, OrderStatus  # ← 模型与枚举

# 业务动作：刷新状态 / 置为过期（按主键 id）
# 修正导入路径：现在服务在 services 包里
from services.recharge_service import (
    refresh_status_if_needed,   # (db, order_id:int) -> updated RechargeOrder
    mark_expired,               # (db, order_id:int) -> None
)

router = APIRouter(prefix="/admin/recharge", tags=["admin-recharge"])

PAGE_SIZE = 20


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def list_recharges(
    req: Request,
    db=Depends(db_session),
    sess=Depends(require_admin),
    page: int = 1,
    status: Optional[str] = None,
    q: Optional[str] = None,
):
    # 基础查询：按 id 倒序
    query = db.query(RechargeOrder).order_by(desc(RechargeOrder.id))

    # 状态过滤：字符串 -> 枚举
    if status:
        try:
            st = OrderStatus(status.upper())
            query = query.filter(RechargeOrder.status == st)
        except Exception:
            # 非法状态值就忽略
            pass

    # 关键字搜索：支持 id / user_tg_id / address / network / payment_id / invoice_id / tx_hash
    if q:
        q = q.strip()
        like = f"%{q}%"
        # cast 到字符串以兼容不同方言（SQLite/MySQL/Postgres）
        query = query.filter(
            or_(
                cast(RechargeOrder.id, String).ilike(like),
                cast(RechargeOrder.user_tg_id, String).ilike(like),
                RechargeOrder.address.ilike(like) if hasattr(RechargeOrder, "address") else False,
                RechargeOrder.pay_address.ilike(like),
                RechargeOrder.network.ilike(like),
                RechargeOrder.payment_id.ilike(like),
                RechargeOrder.invoice_id.ilike(like),
                RechargeOrder.tx_hash.ilike(like),
            )
        )

    total = query.count()
    rows = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    # ✅ 给页面表单签发 CSRF（刷新、过期按钮都会用到）
    csrf_token = issue_csrf(req)

    return req.app.state.templates.TemplateResponse(
        "recharge_orders.html",
        {
            "request": req,
            "title": t("admin.recharge.title"),
            "nav_active": "recharge",
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "status": status or "",
            "q": q or "",
            "csrf_token": csrf_token,  # 模板里的操作表单隐藏字段使用
        },
    )


@router.post("/refresh", response_class=RedirectResponse)
def refresh_order(
    req: Request,
    id: int = Form(...),             # ← 用主键 id
    _=Depends(csrf_protect),         # ✅ CSRF 守卫
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    if not id:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))
    try:
        # services.recharge_service 兼容签名：refresh_status_if_needed(db, order_id=...)
        refresh_status_if_needed(db, order_id=id)  # ← 传主键
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(f"/admin/recharge?refreshed={id}", status_code=303)


@router.post("/expire", response_class=RedirectResponse)
def expire_order(
    req: Request,
    id: int = Form(...),              # ← 用主键 id
    _=Depends(csrf_protect),          # ✅ CSRF 守卫
    db=Depends(db_session),
    sess=Depends(GuardDangerOp(10)),  # 敏感操作：要求最近 2FA
):
    if not id:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))
    try:
        # services.recharge_service 兼容签名：mark_expired(db, order_id=...)
        mark_expired(db, order_id=id)  # ← 传主键
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(f"/admin/recharge?expired={id}", status_code=303)
