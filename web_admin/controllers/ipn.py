# web_admin/controllers/ipn.py
# -*- coding: utf-8 -*-
"""
NOWPayments IPN 回调入口
- 路径：POST /api/np/ipn
- 验签头：X-Nowpayments-Sig
- 体裁：application/json
- 关键字段：order_id, payment_status(字符串), payment_id, pay_address, pay_amount, pay_currency, network, purchase_id
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from services.recharge_service import (  # 业务层：我们只做分发，不在这里写业务
    verify_ipn_signature,
    refresh_status_if_needed,
    mark_order_success,
    mark_order_failed,
    mark_order_expired,
)
from models.recharge import OrderStatus  # 映射用

router = APIRouter(prefix="/api/np", tags=["ipn"])
log = logging.getLogger(__name__)


def _map_status(raw: Optional[str]) -> OrderStatus:
    s = (raw or "").strip().lower()
    if s in {"finished", "confirmed", "paid"}:
        return OrderStatus.SUCCESS
    if s in {"failed", "refunded"}:
        return OrderStatus.FAILED
    if s in {"expired"}:
        return OrderStatus.EXPIRED
    return OrderStatus.PENDING


@router.post("/ipn")
async def ipn_nowpayments(
    request: Request,
    x_nowpayments_sig: Optional[str] = Header(None, convert_underscores=False),
):
    """
    NOWPayments IPN 回调。返回 {"ok": true} 即视为处理成功。
    失败一律抛 4xx/5xx，NOWPayments 会重试。
    """
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="empty body")

    # 严格验签：如果配置了 IPN 密钥但验签失败，直接 403
    if not x_nowpayments_sig:
        raise HTTPException(status_code=400, detail="missing X-Nowpayments-Sig header")
    try:
        ok = verify_ipn_signature(raw, x_nowpayments_sig)
    except Exception as e:
        log.exception("verify_ipn_signature error: %s", e)
        ok = False
    if not ok:
        raise HTTPException(status_code=403, detail="bad signature")

    # 解析 JSON
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    order_id = data.get("order_id")
    payment_status = data.get("payment_status")
    payment_id = data.get("payment_id")
    pay_address = data.get("pay_address")
    log.info("NP IPN: oid=%s status=%s pay_id=%s addr=%s", order_id, payment_status, payment_id, pay_address)

    # 校验 order_id
    try:
        oid = int(str(order_id))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid order_id")

    mapped = _map_status(payment_status)

    # 状态处理策略：
    # - SUCCESS：直接落成功（内部会写账、结束订单）
    # - FAILED：落失败（没有失败实现就过期）
    # - EXPIRED：落过期
    # - PENDING/其它：做一次主动刷新（由服务层去拉取权威状态）
    if mapped == OrderStatus.SUCCESS:
        mark_order_success(oid, tx_hash=data.get("payment_tx_hash") or None)
    elif mapped == OrderStatus.FAILED:
        mark_order_failed(oid, reason="ipn_failed")
    elif mapped == OrderStatus.EXPIRED:
        mark_order_expired(oid)
    else:
        # 给条命令让它自己查一次，防止供应商侧状态边缘态
        refresh_status_if_needed(order_id=oid)

    return JSONResponse(
        {"ok": True, "order_id": oid, "status": mapped.name, "raw_status": payment_status or "unknown"}
    )


# 可选：健康检查（NOWPayments 不会用到，但你调试方便）
@router.get("/ipn/health")
def ipn_health():
    return {"ok": True}
