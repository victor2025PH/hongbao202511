# web_admin/controllers/approvals.py
from __future__ import annotations

import json
from typing import Optional, List, Dict, Any, Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Column, Integer, BigInteger, String, Text, DateTime, func, desc
from sqlalchemy.orm import declarative_base

from core.i18n.i18n import t
from web_admin.deps import db_session, require_admin, GuardDangerOp
from models.user import User, get_balance, update_balance
from models.ledger import LedgerType

# --- 兼容导入：优先 services.recharge_service，其次 services.recharge，再次根级 recharge_service，最后 models.recharge.set_expired；都没有时提供安全兜底 ---
recharge_mark_expired: Optional[Callable[..., Any]] = None
try:
    # 优先：你的项目结构里实际文件为 services/recharge_service.py
    from services.recharge_service import mark_expired as recharge_mark_expired  # type: ignore
except Exception:
    try:
        # 次选：如果你后续把文件命名为了 services/recharge.py
        from services.recharge import mark_expired as recharge_mark_expired  # type: ignore
    except Exception:
        try:
            # 再次选：历史部署把 recharge_service.py 放在项目根
            from recharge_service import mark_expired as recharge_mark_expired  # type: ignore
        except Exception:
            try:
                # 模型层仅提供 set_expired：兼容成 mark_expired
                from models.recharge import set_expired as recharge_mark_expired  # type: ignore
            except Exception:
                recharge_mark_expired = None  # 多路径都不在时，后续用兜底逻辑
# ------------------------------------------------------------------------------------------------

Base = declarative_base()
router = APIRouter(prefix="/admin/approvals", tags=["admin-approvals"])

# --------- ORM: approvals ---------
class Approval(Base):
    __tablename__ = "admin_approvals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    status = Column(String(20), nullable=False, default="PENDING")  # PENDING/APPROVED/REJECTED/FAILED
    op_type = Column(String(40), nullable=False)                    # RESET_SELECTED/RESET_ALL/ADJUST_BATCH/RECHARGE_EXPIRE
    payload = Column(Text, nullable=False)                          # JSON
    submitter_id = Column(BigInteger, nullable=False)               # tg_id
    approver_id = Column(BigInteger, nullable=True)
    result = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


def _ensure_table(db):
    # 用当前会话绑定的引擎建表，避免你去找别的元数据
    engine = db.get_bind()
    Base.metadata.create_all(engine)


# --------- helpers ---------
BATCH = 200

def _json_payload(raw: str | Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}

def _mark_expired(db, order_id: str):
    """
    统一的“订单过期”适配器：
      - 若存在服务层 mark_expired，实现可能是 mark_expired(db, order_id=...) 或 mark_expired(order_id)
      - 若只存在模型层 set_expired，则同名适配为 mark_expired(order_id)
      - 两者都不存在时，这里抛出 500，以便你后续感知并实现
    """
    if callable(recharge_mark_expired):
        oid = int(order_id) if str(order_id).isdigit() else order_id
        # 适配两种签名： (db, order_id=...) / (order_id)
        try:
            return recharge_mark_expired(db, order_id=oid)  # type: ignore
        except TypeError:
            return recharge_mark_expired(oid)  # type: ignore
    # 兜底：尝试直接用模型层 set_expired（若可导入）
    try:
        from models.recharge import set_expired as _set_expired  # noqa: F401
        oid = int(order_id) if str(order_id).isdigit() else order_id
        return _set_expired(oid)
    except Exception:
        # 明确报错，提示需要提供实现
        raise HTTPException(status_code=500, detail="mark_expired/set_expired implementation not found")

# === 执行器：根据 op_type 执行 ===
def _exec_adjust_batch(db, payload: Dict[str, Any], operator_id: int) -> Dict[str, Any]:
    asset = str(payload.get("asset", "")).upper()
    amount = payload.get("amount")
    note = str(payload.get("note") or "")[:120]
    users: List[int] = list(payload.get("users") or [])
    if not asset or amount is None or not users:
        raise ValueError("bad payload")

    ok = 0
    fail = 0
    for uid in users:
        try:
            update_balance(
                db,
                int(uid),
                asset,
                amount,
                write_ledger=True,
                ltype=LedgerType.ADJUSTMENT,
                note=note,
                operator_id=operator_id,
            )
            ok += 1
        except Exception:
            fail += 1
    db.commit()
    return {"ok": ok, "fail": fail, "count": len(users)}

def _exec_reset_selected(db, payload: Dict[str, Any], operator_id: int) -> Dict[str, Any]:
    asset = str(payload.get("asset", "")).upper()
    note = str(payload.get("note") or "RESET")[:120]
    users: List[int] = list(payload.get("users") or [])
    if not asset or not users:
        raise ValueError("bad payload")

    ok = 0
    fail = 0
    total = 0
    for uid in users:
        try:
            bal = get_balance(db, int(uid), asset)
            if not bal or bal == 0:
                continue
            delta = -bal
            update_balance(
                db,
                int(uid),
                asset,
                delta,
                write_ledger=True,
                ltype=LedgerType.RESET,
                note=note,
                operator_id=operator_id,
            )
            ok += 1
            total += abs(bal)
        except Exception:
            fail += 1
    db.commit()
    return {"ok": ok, "fail": fail, "total_deduct": str(total), "count": len(users)}

def _exec_reset_all(db, payload: Dict[str, Any], operator_id: int) -> Dict[str, Any]:
    from sqlalchemy import asc  # 局部导入，避免污染
    asset = str(payload.get("asset", "")).upper()
    note = str(payload.get("note") or "RESET")[:120]
    if not asset:
        raise ValueError("bad payload")

    q = db.query(User.tg_id).order_by(asc(User.tg_id))
    offset = 0
    ok = 0
    fail = 0
    total = 0
    while True:
        chunk = q.offset(offset).limit(BATCH).all()
        if not chunk:
            break
        for (uid,) in chunk:
            try:
                bal = get_balance(db, int(uid), asset)
                if not bal or bal == 0:
                    continue
                delta = -bal
                update_balance(
                    db,
                    int(uid),
                    asset,
                    delta,
                    write_ledger=True,
                    ltype=LedgerType.RESET,
                    note=note,
                    operator_id=operator_id,
                )
                ok += 1
                total += abs(bal)
            except Exception:
                fail += 1
        db.commit()
        offset += BATCH
    return {"ok": ok, "fail": fail, "total_deduct": str(total)}

def _exec_recharge_expire(db, payload: Dict[str, Any], operator_id: int) -> Dict[str, Any]:
    order_id = str(payload.get("order_id") or "")
    if not order_id:
        raise ValueError("bad payload")
    _mark_expired(db, order_id)
    db.commit()
    return {"order_id": order_id, "status": "EXPIRED"}

def _dispatch(db, item: Approval, operator_id: int) -> Dict[str, Any]:
    payload = _json_payload(item.payload)
    if item.op_type == "ADJUST_BATCH":
        return _exec_adjust_batch(db, payload, operator_id)
    if item.op_type == "RESET_SELECTED":
        return _exec_reset_selected(db, payload, operator_id)
    if item.op_type == "RESET_ALL":
        return _exec_reset_all(db, payload, operator_id)
    if item.op_type == "RECHARGE_EXPIRE":
        return _exec_recharge_expire(db, payload, operator_id)
    raise ValueError(f"unsupported op_type: {item.op_type}")


# --------- 路由 ---------

@router.on_event("startup")
def _startup_init():
    # 无需会话，run-time 再确保；这里留空
    pass

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def approvals_list(
    req: Request,
    db=Depends(db_session),
    sess=Depends(require_admin),
    status: Optional[str] = None,  # PENDING 默认
    page: int = 1,
    per_page: int = 20,
):
    _ensure_table(db)
    q = db.query(Approval).order_by(desc(Approval.id))
    if status:
        q = q.filter(Approval.status == status.upper())
    else:
        q = q.filter(Approval.status == "PENDING")

    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = max(1, (total + per_page - 1) // per_page)

    return req.app.state.templates.TemplateResponse(
        "approvals.html",
        {
            "request": req,
            "title": t("admin.approvals.title"),
            "nav_active": "approvals",
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "status": status or "PENDING",
        },
    )

# 供其它控制器调用：入队一个审批请求
@router.post("/enqueue", response_class=RedirectResponse)
def approvals_enqueue(
    req: Request,
    op_type: str = Form(...),
    payload: str = Form(...),           # JSON 字符串
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    _ensure_table(db)
    # 基本校验
    try:
        data = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))

    item = Approval(
        status="PENDING",
        op_type=op_type.strip().upper(),
        payload=json.dumps(data, ensure_ascii=False),
        submitter_id=int(sess.get("tg_id") or 0),
    )
    db.add(item)
    db.commit()
    return RedirectResponse("/admin/approvals", status_code=303)

# 审批通过并执行
@router.post("/{aid}/approve", response_class=RedirectResponse)
def approvals_approve(
    req: Request,
    aid: int,
    db=Depends(db_session),
    sess=Depends(GuardDangerOp(10)),   # 二次校验
):
    _ensure_table(db)
    item = db.query(Approval).filter(Approval.id == aid).first()
    if not item:
        raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))
    if item.status != "PENDING":
        raise HTTPException(status_code=400, detail="not pending")

    approver_id = int(sess.get("tg_id") or 0)
    if approver_id == int(item.submitter_id or 0):
        raise HTTPException(status_code=400, detail=t("admin.approvals.need_two_admins"))

    # 执行
    try:
        result = _dispatch(db, item, approver_id)
        item.status = "APPROVED"
        item.approver_id = approver_id
        item.result = json.dumps(result, ensure_ascii=False)
        db.commit()
    except Exception as e:
        db.rollback()
        item.status = "FAILED"
        item.approver_id = approver_id
        item.result = str(e)
        db.add(item)
        db.commit()
        raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse("/admin/approvals?done=1", status_code=303)

# 拒绝
@router.post("/{aid}/reject", response_class=RedirectResponse)
def approvals_reject(
    req: Request,
    aid: int,
    reason: str = Form(""),
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    _ensure_table(db)
    item = db.query(Approval).filter(Approval.id == aid).first()
    if not item:
        raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))
    if item.status != "PENDING":
        raise HTTPException(status_code=400, detail="not pending")

    approver_id = int(sess.get("tg_id") or 0)
    if approver_id == int(item.submitter_id or 0):
        # 允许提交人撤回，标 REJECTED
        pass
    item.status = "REJECTED"
    item.approver_id = approver_id
    item.result = reason[:200] if reason else "rejected"
    db.commit()
    return RedirectResponse("/admin/approvals?rejected=1", status_code=303)
