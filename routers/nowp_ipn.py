# routers/nowp_ipn.py
import json
from aiogram import Router
from aiohttp import web

from core.clients.nowpayments import verify_ipn_signature
from core.db import SessionLocal
from core.models import User
from routers.recharge import ensure_schema, credit_user  # 复用

router = Router(name="nowpayments_ipn")

async def nowp_ipn_handler(request: web.Request):
    ensure_schema()
    raw = await request.text()
    try:
        data = json.loads(raw)
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    sig = request.headers.get("x-nowpayments-sig", "")
    if not verify_ipn_signature(data, sig):
        return web.json_response({"ok": False, "error": "bad signature"}, status=400)

    pay_address = data.get("pay_address")
    status = (data.get("payment_status") or "").lower()
    actually_paid = float(data.get("actually_paid") or 0)
    payment_id = int(data.get("payment_id") or data.get("id") or 0)
    pay_currency = (data.get("pay_currency") or "").upper()

    # 支持的两种：USDTTRC20 / TON
    token = "USDT" if "USDT" in pay_currency else ("TON" if pay_currency == "TON" else None)
    if not token or not pay_address or payment_id <= 0:
        return web.json_response({"ok": True})  # 忽略其它币种/坏消息

    from sqlalchemy import text as _sql
    with SessionLocal() as s:
        # 反查 address -> user
        field_addr = "usdt_pay_address" if token == "USDT" else "ton_pay_address"
        user = s.query(User).filter(getattr(User, field_addr) == pay_address).first()
        if not user:
            return web.json_response({"ok": True})  # 未找到用户，忽略

        # 去重
        exists = s.execute(_sql("SELECT 1 FROM nowp_seen WHERE payment_id=:pid"), {"pid": payment_id}).fetchone()
        if exists:
            return web.json_response({"ok": True})

        # 状态过滤：确认/完成/部分支付
        if status in {"confirming", "confirmed", "finished", "partially_paid"} and actually_paid > 0:
            credit_user(s, user, token, actually_paid, note=f"NOWPayments #{payment_id}")
            s.execute(
                _sql(
                    "INSERT INTO nowp_seen (payment_id, address, user_id, token, amount) "
                    "VALUES (:pid, :addr, :uid, :token, :amount)"
                ),
                {"pid": payment_id, "addr": pay_address, "uid": user.id, "token": token, "amount": actually_paid},
            )
            s.commit()

    return web.json_response({"ok": True})


def setup_app(app):  # 在你的 app.py 里引入并注册到 aiohttp / FastAPI
    app.router.add_post("/nowp/ipn", nowp_ipn_handler)
