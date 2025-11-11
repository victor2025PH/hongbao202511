# models/recharge.py
# -*- coding: utf-8 -*-
"""
充值订单模型与工具：
- RechargeOrder: 记录用户充值申请
- 状态机：PENDING → SUCCESS / FAILED / EXPIRED
- 工具函数：
    create_order(user_id, amount, token, provider, note)
    write_back_fields(order_id, **kwargs)                # 通用写回（地址、链接、真实应付金额等）
    write_back_np_fields(order_id, np_payload)           # NowPayments 专用写回封装
    mark_success(order_id, tx_hash)
    mark_failed(order_id, note)
    set_expired(order_id)                                # 新增：单笔置为过期
    expire_orders()
    get_order(order_id)
    list_user_orders(user_id, limit)
    order_to_public_dict(order)                          # 给上层 UI/routers 用的统一输出

设计要点：
1) 金额以 "字符串" 持久化，读取时再转 Decimal，杜绝浮点与驱动兼容问题。
2) pay_amount/pay_currency/network/pay_address/payment_url/purchase_id 均为“通道真实值”，用于展示和校验。
3) 幂等安全：mark_success 对已 SUCCESS 的订单直接返回 True。
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Optional, Dict, Any, List, Union

from sqlalchemy import (
    Column, Integer, BigInteger, String, DateTime, Enum, Index, Text, UniqueConstraint
)
from sqlalchemy.orm import Session

# 你项目里的导入路径可能是 app.models.db / core.db / models.db
# 按你的结构保持如下（相对导入）：
from .db import Base, get_session
from .user import update_balance, User
from .ledger import add_ledger_entry, LedgerType
from config.settings import settings


# =========================
# 枚举 & ORM 定义
# =========================

class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class RechargeOrder(Base):
    __tablename__ = "recharge_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)

    user_tg_id = Column(BigInteger, index=True, nullable=False)
    provider = Column(String(32), nullable=False)          # 支付渠道 (mock/nowpayments/...)
    token = Column(String(16), nullable=False, default="USDT")

    # 用 String 保存金额，避免 SQLite/驱动对 Decimal 的告警；业务层再转 Decimal
    amount = Column(String(32), nullable=False)

    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expire_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    tx_hash = Column(String(128), nullable=True)           # 链上交易哈希/第三方订单号
    note = Column(String(255), nullable=True)

    # 通道回填字段（真实应付信息、地址、二维码、链接）
    pay_address = Column(String(128), nullable=True)       # 真实收款地址
    pay_currency = Column(String(32), nullable=True)       # 例如 usdttrc20 / ton
    pay_amount = Column(String(32), nullable=True)         # 真实应付金额（字符串）
    network = Column(String(32), nullable=True)            # 网络简称/名，如 trx/ton/erc20
    invoice_id = Column(String(64), nullable=True)         # NowPayments invoice id
    payment_id = Column(String(64), nullable=True)         # NowPayments payment id
    payment_url = Column(String(255), nullable=True)       # 最终用于展示的支付链接（发票或 payment）
    purchase_id = Column(String(64), nullable=True)        # 可选：第三方 purchase_id
    qr_b64 = Column(Text, nullable=True)                   # 可选：二维码 base64

    __table_args__ = (
        Index("idx_user_status", "user_tg_id", "status"),
        Index("idx_created", "created_at"),
        UniqueConstraint("payment_id", name="uq_payment_id"),
        UniqueConstraint("invoice_id", name="uq_invoice_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<RechargeOrder id={self.id} user={self.user_tg_id} "
            f"token={self.token} amount={self.amount} status={self.status}>"
        )


# =========================
# 内部工具
# =========================

def _canon_token(token: str) -> str:
    return (token or "").upper()


def _q2(x: Union[Decimal, float, int, str]) -> Decimal:
    """量化为 2 位（USDT/TON 用），向下取整避免“超额支付”带来的校验失败。"""
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _q0(x: Union[Decimal, float, int, str]) -> Decimal:
    """量化为整数（POINT 使用）。"""
    return Decimal(int(Decimal(str(x))))


def _fmt_token_amount_for_display(token: str, amount_s: Optional[str]) -> str:
    """
    显示金额优先使用字符串回填（如 pay_amount），否则退回订单下单金额。
    """
    if not amount_s:
        return ""
    try:
        if _canon_token(token) == "POINT":
            return str(_q0(amount_s))
        else:
            return f"{_q2(amount_s):.2f}"
    except InvalidOperation:
        return str(amount_s)


# =========================
# 订单 CRUD/状态流转
# =========================

def create_order(
    user_id: int,
    amount: Union[Decimal, float, int, str],
    token: str = "USDT",
    provider: Optional[str] = None,
    note: Optional[str] = None,
) -> RechargeOrder:
    """
    创建充值订单（状态=PENDING），并显式提交。
    统一口径：
      - USDT/TON：2 位小数（向下取整）
      - POINT   ：整数
    """
    tok = _canon_token(token)
    amt_dec = _q0(amount) if tok == "POINT" else _q2(amount)

    with get_session() as s:
        order = RechargeOrder(
            user_tg_id=int(user_id),
            provider=provider or getattr(settings, "RECHARGE_PROVIDER", "nowpayments"),
            token=tok,
            amount=str(amt_dec),  # 以字符串持久化
            status=OrderStatus.PENDING,
            created_at=datetime.utcnow(),
            expire_at=datetime.utcnow() + timedelta(
                minutes=int(getattr(settings, "RECHARGE_EXPIRE_MINUTES", 60) or 60)
            ),
            note=note or "",
        )
        s.add(order)
        s.flush()
        s.commit()
        return order


def write_back_fields(order_id: int, **kwargs: Any) -> RechargeOrder:
    """
    通用写回：把通道返回的信息写回订单（真实应付金额/币种/网络/地址/链接/发票号等）。
    允许的键：
      - invoice_id, payment_id, payment_url, purchase_id,
        pay_address, pay_currency, pay_amount, network, qr_b64, note
    传入的值均会转成 str（None 则覆盖为 None），确保类型稳定。
    """
    ALLOWED = {
        "invoice_id", "payment_id", "payment_url", "purchase_id",
        "pay_address", "pay_currency", "pay_amount",
        "network", "qr_b64", "note",
    }

    with get_session() as s:
        o: RechargeOrder = s.get(RechargeOrder, order_id)
        if not o:
            raise ValueError(f"Order #{order_id} not found")

        changed = False
        for k, v in kwargs.items():
            if k not in ALLOWED:
                continue
            setattr(o, k, (None if v is None else str(v)))
            changed = True

        if changed:
            s.add(o)
            s.commit()
        return o


def write_back_np_fields(order_id: int, np_payload: Dict[str, Any]) -> RechargeOrder:
    """
    NowPayments 专用：根据通道返回结果回填关键字段。
    兼容两种模式：
      - /invoice: 可得到固定付款链接（invoice_url）
      - /payment: 地址模式，返回地址/网络/真实应付金额与币种
    建议把有值的字段塞进来：
      np_payload = {
        "invoice_id": "...",
        "payment_id": "...",
        "payment_url": "https://nowpayments.io/payment/?iid=...",
        "pay_address": "TFccCARN3B9v...",
        "pay_currency": "usdttrc20",
        "pay_amount": "99.872114",
        "network": "trx",
        "purchase_id": "...",
      }
    """
    keep = {k: np_payload.get(k) for k in (
        "invoice_id", "payment_id", "payment_url", "purchase_id",
        "pay_address", "pay_currency", "pay_amount", "network"
    ) if np_payload.get(k) is not None}
    return write_back_fields(order_id, **keep)


def mark_success(order_id: int, tx_hash: Optional[str] = None) -> bool:
    """
    标记订单成功，并给用户入账（幂等：仅 PENDING→SUCCESS；若已 SUCCESS 也返回 True）。
    入账口径：
      - 优先使用“真实应付金额 pay_amount”；没有则回退到订单下单金额 amount。
      - USDT/TON 两位小数、POINT 整数。
    """
    with get_session() as s:
        o: RechargeOrder = s.get(RechargeOrder, order_id)
        if not o:
            return False
        if o.status == OrderStatus.SUCCESS:
            return True
        if o.status != OrderStatus.PENDING:
            return False

        tok = _canon_token(o.token)

        # 优先真实应付金额，没有则回退下单金额
        raw_amt = o.pay_amount if o.pay_amount else o.amount
        amt_dec = _q0(raw_amt) if tok == "POINT" else _q2(raw_amt)

        # 1) 状态更新
        o.status = OrderStatus.SUCCESS
        o.finished_at = datetime.utcnow()
        if tx_hash:
            o.tx_hash = tx_hash
        s.add(o)

        # 2) 用户余额入账（只增加，不会出现负数）
        u = s.query(User).filter_by(tg_id=o.user_tg_id).first()
        if not u:
            u = User(tg_id=o.user_tg_id)
            s.add(u)
            s.flush()
        update_balance(
            s, u, tok,
            int(amt_dec) if tok == "POINT" else Decimal(amt_dec)
        )

        # 3) 记账（正数收入）
        add_ledger_entry(
            s,
            user_tg_id=o.user_tg_id,
            ltype=LedgerType.RECHARGE,
            token=tok,
            amount=(int(amt_dec) if tok == "POINT" else Decimal(amt_dec)),
            ref_type="ORDER",
            ref_id=str(o.id),
            note="充值入账",
        )

        s.commit()
        return True


def mark_failed(order_id: int, note: Optional[str] = None) -> bool:
    """标记订单失败，仅 PENDING→FAILED。"""
    with get_session() as s:
        o: RechargeOrder = s.get(RechargeOrder, order_id)
        if not o or o.status != OrderStatus.PENDING:
            return False
        o.status = OrderStatus.FAILED
        o.finished_at = datetime.utcnow()
        if note:
            o.note = note
        s.add(o)
        s.commit()
        return True


def set_expired(order_id: int) -> bool:
    """单笔订单置为过期（用于服务层精细化处理）。"""
    with get_session() as s:
        o: RechargeOrder = s.get(RechargeOrder, order_id)
        if not o:
            return False
        if o.status in (OrderStatus.SUCCESS, OrderStatus.FAILED, OrderStatus.EXPIRED):
            return True
        o.status = OrderStatus.EXPIRED
        o.finished_at = datetime.utcnow()
        s.add(o)
        s.commit()
        return True


def expire_orders() -> int:
    """
    将过期未支付订单批量置为 EXPIRED，返回影响行数。
    """
    with get_session() as s:
        now = datetime.utcnow()
        rows: List[RechargeOrder] = (
            s.query(RechargeOrder)
            .filter(RechargeOrder.status == OrderStatus.PENDING)
            .filter(RechargeOrder.expire_at < now)
            .all()
        )
        cnt = 0
        for o in rows:
            o.status = OrderStatus.EXPIRED
            o.finished_at = datetime.utcnow()
            s.add(o)
            cnt += 1
        if cnt:
            s.commit()
        return cnt


def get_order(order_id: int) -> Optional[RechargeOrder]:
    with get_session() as s:
        return s.get(RechargeOrder, order_id)


def list_user_orders(user_id: int, limit: int = 10) -> List[RechargeOrder]:
    with get_session() as s:
        return (
            s.query(RechargeOrder)
            .filter(RechargeOrder.user_tg_id == int(user_id))
            .order_by(RechargeOrder.id.desc())
            .limit(limit)
            .all()
        )


# =========================
# 给上层 UI/routers 使用的统一展示
# =========================

def order_to_public_dict(o: RechargeOrder) -> Dict[str, Any]:
    """
    统一给上层用的展示字典。
    注意这里的 amount / pay_amount：
      - amount       = 下单金额（字符串）
      - pay_amount   = 真实应付金额（字符串），若存在应优先展示它
    上层若要显示“最终请付款金额”，应优先显示 pay_amount + pay_currency；
    若 pay_amount 为空，则回退显示 amount + token。
    """
    data: Dict[str, Any] = {
        "id": o.id,
        "status": o.status.value if isinstance(o.status, OrderStatus) else str(o.status),
        "token": o.token,
        "amount": str(o.amount),                      # 下单金额（原始）
        "created_at": (o.created_at.isoformat() + "Z") if o.created_at else None,
        "expire_at": (o.expire_at.isoformat() + "Z") if o.expire_at else None,
        "payment_url": o.payment_url,
        "pay_address": o.pay_address,
        "pay_amount": str(o.pay_amount) if o.pay_amount else None,  # 真实应付
        "pay_currency": o.pay_currency,
        "network": o.network,
        "invoice_id": o.invoice_id,
        "payment_id": o.payment_id,
        "purchase_id": o.purchase_id,
        "tx_hash": o.tx_hash,
        "note": o.note,
    }

    # 方便客户端直接展示“最终请支付金额（已量化格式化后的字符串）”
    if o.pay_amount:
        data["final_pay_display"] = f"{_fmt_token_amount_for_display(o.pay_currency or o.token, o.pay_amount)} {o.pay_currency or o.token}"
    else:
        data["final_pay_display"] = f"{_fmt_token_amount_for_display(o.token, o.amount)} {o.token}"

    return data
