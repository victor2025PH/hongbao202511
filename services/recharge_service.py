# services/recharge_service.py
# -*- coding: utf-8 -*-
"""
充值业务服务层（NOWPayments） + 后台兼容包装
--------------------------------
目标：二维码 / 地址 / 支付链接 三者严格一致，且链接可公开打开。
同时对齐后台控制器的接口期望：
  - refresh_status_if_needed(db, order_id:int)  ← 兼容
  - mark_expired(db, order_id:int)              ← 新增包装（内部走 mark_order_expired）

原有能力全部保留：创建发票/付款、状态轮询、写回字段、IPN 校验、UI 文案等。
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import requests
try:
    # 仅用于类型判断，不强依赖
    from sqlalchemy.orm import Session as _SA_Session  # type: ignore
except Exception:
    _SA_Session = object  # 占位，避免无 SQLAlchemy 环境时报错

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ========= 项目内模块（兼容不同目录结构） =========
try:
    from config.settings import settings
except Exception:
    from settings import settings  # type: ignore

try:
    from core.i18n.i18n import t  # type: ignore
except Exception:
    def t(key: str, lang: str, **kwargs) -> str:  # 极简兜底
        return ""

from models.db import get_session
from models.recharge import (  # type: ignore
    RechargeOrder,
    OrderStatus,
    create_order as _create_order,
    get_order as _get_order,
    mark_success as _mark_success,
)

# 可选函数（可能不存在）
try:
    from models.recharge import mark_failed as _mark_failed  # type: ignore
except Exception:
    _mark_failed = None  # type: ignore

try:
    from models.recharge import set_expired as _set_expired  # type: ignore
except Exception:
    _set_expired = None  # type: ignore

logger = logging.getLogger(__name__)

# =============================================================================
# 配置
# =============================================================================
def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}

_PROVIDER = (
    getattr(settings, "RECHARGE_PROVIDER", None)
    or getattr(settings, "recharge_provider", None)
    or "nowpayments"
).lower()

_EXPIRE_MIN = int(getattr(settings, "RECHARGE_EXPIRE_MINUTES", 60) or 60)
_POLL_MIN_INTERVAL = 15  # 轮询节流（秒）

# NOWPayments
_NP_API_KEY = getattr(settings, "NOWPAYMENTS_API_KEY", None)
_NP_IPN_SECRET = getattr(settings, "NOWPAYMENTS_IPN_SECRET", None)
_NP_BASE_URL = getattr(settings, "NOWPAYMENTS_BASE_URL", None) or "https://api.nowpayments.io/v1"
_NP_IPN_URL = getattr(settings, "NOWPAYMENTS_IPN_URL", None)

# 若 .env 未提供真实地址，用占位以避免 /invoice 400；配置后会自动覆盖
_NP_SUCCESS_URL = getattr(settings, "NOWPAYMENTS_SUCCESS_URL", None) or "https://example.com/np-success"
_NP_CANCEL_URL = getattr(settings, "NOWPAYMENTS_CANCEL_URL", None) or "https://example.com/np-cancel"

# 强制旧流程（直连 /payment）；默认 False
_NP_FORCE_LEGACY = _as_bool(getattr(settings, "NOWPAYMENTS_FORCE_LEGACY", None), default=False)

# 价格计价法币（由 NP 换算为目标加密币种数量）
_NP_PRICE_CCY = (getattr(settings, "NOWPAYMENTS_PRICE_CCY", None) or "usd").lower()

# 币种映射（业务 Token -> 统一 Token）
_TOKEN_ALIASES: Dict[str, str] = {
    "USDTTRC20": "USDT",
    "USDT-TRC20": "USDT",
    "USDT_TRC20": "USDT",
    "USDTTRON": "USDT",
    "TRC20USDT": "USDT",
    "TONCOIN": "TON",
    "TON-COIN": "TON",
    "TON_COIN": "TON",
    "POINTS": "POINT",
    "STAR": "POINT",
}
# 展示精度
_TOKEN_META: Dict[str, Dict[str, Any]] = {
    "USDT": {"decimals": 2},
    "TON": {"decimals": 2},
    "POINT": {"decimals": 0},
}

# 状态缓存与防重
_STATUS_CACHE: Dict[int, Tuple[float, str, OrderStatus]] = {}
_CREATING: Dict[int, float] = {}

# =============================================================================
# 工具
# =============================================================================
def map_user_token(token: str) -> str:
    if not token:
        return ""
    up = token.upper().strip()
    return _TOKEN_ALIASES.get(up, up)

def _assert_supported_token(token: str) -> None:
    if token not in _TOKEN_META:
        raise ValueError(f"Unsupported token: {token}")

def _quantize_by_token(token: str, amt: Union[Decimal, float, int]) -> Decimal:
    _assert_supported_token(token)
    decimals = _TOKEN_META[token]["decimals"]
    if decimals <= 0:
        return Decimal(int(Decimal(str(amt))))
    q = Decimal("0." + "0" * (decimals - 1) + "1")
    return Decimal(str(amt)).quantize(q, rounding=ROUND_DOWN)

def _canonical_pay_currency(s: Optional[str]) -> str:
    """
    统一 pay_currency 字符串：
      - USDT TRC20 -> 'usdttrc20'
      - TON 有两种写法：'ton' 或 'toncoin'，原样尊重传入（并规范大小写）
      - 其它常见别名兜底为小写
    """
    if not s:
        return ""
    up = s.upper().replace("_", "").replace("-", "")
    # USDT-TRC20
    if up in {"USDTTRC20", "TRC20USDT"}:
        return "usdttrc20"
    # TON 两种
    if up == "TON":
        return "ton"
    if up == "TONCOIN":
        return "toncoin"
    # 其它常见
    if up in {"TRX", "TRON"}:
        return "trx"
    return s.lower()

def resolve_pay_currency(token: str) -> str:
    """
    把业务 Token 映射到 NOWPayments 的 pay_currency。
    优先使用 .env 中的 NP_PAY_COIN_*（例如：NP_PAY_COIN_TON=ton）。
    其次回退到 RECHARGE_COIN_*。
    最后给默认值。
    """
    token = token.upper()
    override = None
    try:
        if token == "USDT":
            override = (
                getattr(settings, "NP_PAY_COIN_USDT", None)
                or getattr(settings, "RECHARGE_COIN_USDT", None)
            )
        elif token == "TON":
            override = (
                getattr(settings, "NP_PAY_COIN_TON", None)
                or getattr(settings, "RECHARGE_COIN_TON", None)
            )
    except Exception:
        override = None

    if override:
        return _canonical_pay_currency(override)

    # 默认值（避免空）
    if token == "USDT":
        return "usdttrc20"
    if token == "TON":
        # 有些账号接受 'toncoin'，你的环境已配置 'ton'，如需切换改 .env 即可
        return "ton"
    return token.lower()

def resolve_price_currency(token: str) -> str:
    return _NP_PRICE_CCY

def _fmt_amount_for_display(token: str, amount: Union[str, Decimal]) -> str:
    _assert_supported_token(token)
    amt_dec = Decimal(amount) if isinstance(amount, str) else amount
    dec = _TOKEN_META[token]["decimals"]
    if dec <= 0:
        return str(int(amt_dec))
    return f"{amt_dec.quantize(Decimal('0.01'), rounding=ROUND_DOWN):.2f}"

def _np_headers() -> Dict[str, str]:
    return {"x-api-key": _NP_API_KEY or "", "Content-Type": "application/json"}

def _http_get_payment(payment_id: Union[str, int]) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(f"{_NP_BASE_URL}/payment/{payment_id}", headers=_np_headers(), timeout=30)
        if r.status_code == 200:
            return r.json()
        logger.error("GET /payment/%s failed: %s %s", payment_id, r.status_code, r.text)
    except Exception as e:
        logger.exception("http get payment error: %s", e)
    return None

def _map_np_status(s: str) -> Tuple[OrderStatus, str]:
    raw = (s or "").strip().lower()
    if raw in {"finished", "confirmed", "paid"}:
        return (OrderStatus.SUCCESS, raw)
    if raw in {"failed", "refunded"}:
        return (OrderStatus.FAILED, raw)
    if raw in {"expired"}:
        return (OrderStatus.EXPIRED, raw)
    return (OrderStatus.PENDING, raw)

def _parse_iso8601(s: str) -> Optional[datetime]:
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return None

# =============================================================================
# Provider 抽象
# =============================================================================
class _ProviderBase:
    name = "base"

    def ensure_payment(
        self, order: RechargeOrder
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Dict[str, Any]]:
        """
        返回：
          (invoice_id, payment_id, payment_url, pay_address, extra)
        extra 至少包含：
          - pay_amount, pay_currency, network, expires_sec, purchase_id(可选)
        """
        raise NotImplementedError

    def poll_status(self, order: RechargeOrder) -> Tuple[Optional[OrderStatus], Optional[str]]:
        raise NotImplementedError

class _MockProvider(_ProviderBase):
    name = "mock"

    def ensure_payment(self, order: RechargeOrder):
        base = "https://example.com/pay"
        amt = _fmt_amount_for_display(order.token, order.amount)
        url = f"{base}?oid={order.id}&amt={amt}&token={order.token}&p={self.name}"
        addr = f"mock_addr_{order.id}"
        extra = {
            "pay_amount": amt,
            "pay_currency": order.token,
            "network": "MOCK",
            "expires_sec": _EXPIRE_MIN * 60,
            "purchase_id": f"MOCK-{order.id}",
        }
        return (f"INV-{order.id}", f"PAY-{order.id}", url, addr, extra)

    def poll_status(self, order: RechargeOrder):
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        if order.expire_at and now >= order.expire_at.replace(tzinfo=timezone.utc):
            return (OrderStatus.EXPIRED, "expired")
        return (OrderStatus.PENDING, "pending")

def _get_provider(name: str) -> _ProviderBase:
    if name.lower() == "nowpayments" and _NP_API_KEY:
        return _NowPaymentsProvider()
    if name.lower() == "nowpayments" and not _NP_API_KEY:
        logger.warning("NOWPayments selected but API key missing, fallback to mock.")
    return _MockProvider()

# =============================================================================
# NOWPayments Provider
# =============================================================================
class _NowPaymentsProvider(_ProviderBase):
    name = "nowpayments"

    # ---------- A. 创建发票 ----------
    def _http_create_invoice(self, order: RechargeOrder) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        try:
            body = {
                "price_amount": float(Decimal(order.amount)),
                "price_currency": resolve_price_currency(order.token),  # usd
                "order_id": str(order.id),
                "order_description": f"Recharge for user {order.user_tg_id}",
                "ipn_callback_url": _NP_IPN_URL or "",
                "is_fixed_rate": True,
                "pay_currency": resolve_pay_currency(order.token),  # usdttrc20 / ton / toncoin
                "success_url": (
                    f"{_NP_SUCCESS_URL}&oid={order.id}" if "?" in _NP_SUCCESS_URL else f"{_NP_SUCCESS_URL}?oid={order.id}"
                ),
                "cancel_url": (
                    f"{_NP_CANCEL_URL}&oid={order.id}" if "?" in _NP_CANCEL_URL else f"{_NP_CANCEL_URL}?oid={order.id}"
                ),
            }
            r = requests.post(f"{_NP_BASE_URL}/invoice", headers=_np_headers(), data=json.dumps(body), timeout=30)
            if r.status_code not in (200, 201):
                logger.error("HTTP /invoice failed: %s %s", r.status_code, r.text)
                try:
                    err = r.json()
                except Exception:
                    err = {"text": r.text}
                return (None, None, json.dumps(err, ensure_ascii=False))

            data = r.json()
            iid = data.get("id") or data.get("iid")
            invoice_url = data.get("invoice_url") or data.get("url")
            logger.info("NOWPayments invoice created: %s", r.text)
            return (str(iid) if iid is not None else None, invoice_url, None)
        except Exception as e:
            logger.exception("http create invoice failed: %s", e)
            return (None, None, str(e))

    # ---------- B. 用 iid 创建付款 ----------
    def _http_create_payment_by_invoice(
    self, order: RechargeOrder, iid: str
    ) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
        try:
            body = {
                "iid": iid,
                "pay_currency": resolve_pay_currency(order.token),
                "order_id": str(order.id),
                "order_description": f"Recharge for user {order.user_tg_id}",
                "is_fixed_rate": True,
                # 某些账号需要即便 by-invoice 也带 price 字段，冗余兼容
                "price_amount": float(Decimal(order.amount)),
                "price_currency": resolve_price_currency(order.token),
            }
            r = requests.post(f"{_NP_BASE_URL}/payment", headers=_np_headers(), data=json.dumps(body), timeout=30)
            if r.status_code not in (200, 201):
                logger.error("HTTP /payment(by invoice) failed: %s %s", r.status_code, r.text)
                return (None, None, {})
            data = r.json()
            logger.info("NOWPayments payment(by invoice) created: %s", r.text)
            return self._extract_payment_common(order, data)
        except Exception as e:
            logger.exception("http create payment by invoice failed: %s", e)
            return (None, None, {})

    # ---------- C. 回退：直接 /payment ----------
    def _http_create_payment_direct(
        self, order: RechargeOrder
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Dict[str, Any]]:
        try:
            body = {
                "price_amount": float(Decimal(order.amount)),
                "price_currency": resolve_price_currency(order.token),  # usd
                "pay_currency": resolve_pay_currency(order.token),
                "order_id": str(order.id),
                "order_description": f"Recharge for user {order.user_tg_id}",
                "is_fixed_rate": True,
                "ipn_callback_url": _NP_IPN_URL or "",
            }
            r = requests.post(f"{_NP_BASE_URL}/payment", headers=_np_headers(), data=json.dumps(body), timeout=30)
            if r.status_code not in (200, 201):
                logger.error("HTTP /payment(direct) failed: %s %s", r.status_code, r.text)
                return (None, None, None, {})
            data = r.json()
            logger.info("NOWPayments payment(direct) created: %s", r.text)

            pay_id, addr, extra = self._extract_payment_common(order, data)
            purl = f"https://nowpayments.io/payment/?paymentId={pay_id}" if pay_id else None
            return (pay_id, purl, addr, extra)
        except Exception as e:
            logger.exception("http create payment direct failed: %s", e)
            return (None, None, None, {})

    # ---------- 通用提取 ----------
    def _extract_payment_common(
        self, order: RechargeOrder, data: Dict[str, Any]
    ) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
        pay_id = data.get("payment_id")
        addr = data.get("pay_address")
        extra: Dict[str, Any] = {
            "pay_amount": data.get("pay_amount"),
            "pay_currency": data.get("pay_currency") or resolve_pay_currency(order.token),
            "network": data.get("network"),
            "expires_sec": 1800,
            "purchase_id": data.get("purchase_id"),
        }
        iso_exp = data.get("valid_until") or data.get("expiration_estimate_date")
        if iso_exp and getattr(order, "created_at", None):
            dt_exp = _parse_iso8601(iso_exp)
            if dt_exp:
                try:
                    sec = int((dt_exp - order.created_at.replace(tzinfo=timezone.utc)).total_seconds())
                    if sec > 60:
                        extra["expires_sec"] = sec
                except Exception:
                    pass
        return (pay_id, addr, extra)

    # ---------- 统一入口 ----------
    def ensure_payment(
        self, order: RechargeOrder
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Dict[str, Any]]:
        # 已有 payment：只查询并回填，链接优先发票
        if getattr(order, "payment_id", None):
            js = _http_get_payment(order.payment_id)
            if js:
                pay_id = js.get("payment_id") or order.payment_id
                addr = js.get("pay_address") or getattr(order, "pay_address", None)

                pay_url = None
                inv_id = getattr(order, "invoice_id", None)
                if inv_id:
                    pay_url = f"https://nowpayments.io/payment/?iid={inv_id}"
                if not pay_url and pay_id:
                    pay_url = f"https://nowpayments.io/payment/?paymentId={pay_id}"
                if not pay_url:
                    pay_url = (
                        getattr(order, "payment_url", None)
                        or js.get("invoice_url")
                        or js.get("payment_url")
                        or js.get("pay_url")
                        or js.get("checkout_url")
                        or js.get("invoice_link")
                    )

                expires_sec = 1800
                iso_exp = js.get("valid_until") or js.get("expiration_estimate_date")
                if iso_exp and getattr(order, "created_at", None):
                    dt_exp = _parse_iso8601(iso_exp)
                    if dt_exp:
                        try:
                            sec = int((dt_exp - order.created_at.replace(tzinfo=timezone.utc)).total_seconds())
                            if sec > 60:
                                expires_sec = sec
                        except Exception:
                            pass

                extra = {
                    "pay_amount": js.get("pay_amount") or getattr(order, "pay_amount", None),
                    "pay_currency": js.get("pay_currency") or getattr(order, "pay_currency", resolve_pay_currency(order.token)),
                    "network": js.get("network") or getattr(order, "network", None),
                    "expires_sec": expires_sec,
                    "purchase_id": js.get("purchase_id") or getattr(order, "purchase_id", None),
                }
                return (getattr(order, "invoice_id", None), pay_id, pay_url, addr, extra)
            # 回退：查询失败时直接复用现有字段，避免再次创建支付
            return (
                getattr(order, "invoice_id", None),
                getattr(order, "payment_id", None),
                getattr(order, "payment_url", None),
                getattr(order, "pay_address", None),
                {
                    "pay_amount": getattr(order, "pay_amount", None),
                    "pay_currency": getattr(order, "pay_currency", resolve_pay_currency(order.token)),
                    "network": getattr(order, "network", None),
                    "expires_sec": _EXPIRE_MIN * 60,
                    "purchase_id": getattr(order, "purchase_id", None),
                },
            )

        # 未有 payment：创建
        if not _NP_FORCE_LEGACY:
            iid, invoice_url, err = self._http_create_invoice(order)
            if iid and invoice_url:
                pay_id, addr, extra = self._http_create_payment_by_invoice(order, iid)
                if not pay_id:
                    logger.warning("Payment-by-invoice failed (%s), fallback to direct payment.", err or "no detail")
                    pay_id2, pay_url2, addr2, extra2 = self._http_create_payment_direct(order)
                    if pay_id2:
                        return (iid, pay_id2, pay_url2, addr2, extra2 or {})
                    # 极端兜底：返回发票链接
                    return (iid, None, invoice_url, None, extra or {})
                # 成功：统一返回发票链接
                pay_url = invoice_url
                return (iid, pay_id, pay_url, addr, extra)
            else:
                logger.warning("Invoice create failed, will fallback to direct payment. err=%s", err)

        # 回退直连
        pay_id, pay_url, addr, extra = self._http_create_payment_direct(order)
        return (None, pay_id, pay_url, addr, extra)

    def poll_status(self, order: RechargeOrder) -> Tuple[Optional[OrderStatus], Optional[str]]:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        if order.expire_at and now >= order.expire_at.replace(tzinfo=timezone.utc):
            return (OrderStatus.EXPIRED, "expired")
        try:
            if getattr(order, "payment_id", None):
                js = _http_get_payment(order.payment_id)
                if js and js.get("payment_status"):
                    mapped, raw = _map_np_status(js["payment_status"])
                    return (mapped, raw)
        except Exception as e:
            logger.exception("http poll_status error: %s", e)
        return (OrderStatus.PENDING, "pending")

# =============================================================================
# 订单创建与写回
# =============================================================================
def _write_back_fields(order_id: int, **kwargs: Any) -> RechargeOrder:
    with get_session() as s:
        o: RechargeOrder = s.get(RechargeOrder, order_id)  # type: ignore
        if not o:
            raise ValueError(f"Order #{order_id} not found for write-back")
        for k in ("invoice_id", "payment_id", "payment_url", "pay_address", "pay_currency", "pay_amount", "network"):
            if k in kwargs and kwargs.get(k) is not None:
                setattr(o, k, kwargs[k])
        if "purchase_id" in kwargs and kwargs["purchase_id"]:
            try:
                setattr(o, "purchase_id", kwargs["purchase_id"])
            except Exception:
                pass
        if "expires_sec" in kwargs and kwargs["expires_sec"]:
            try:
                sec = int(kwargs["expires_sec"])
                if o.created_at:
                    o.expire_at = o.created_at + timedelta(seconds=sec)
            except Exception:
                pass
        s.add(o)
        s.commit()
        return o

def new_order(user_id: int, token: str, amount: Union[Decimal, float, int], provider: Optional[str] = None) -> RechargeOrder:
    tok = map_user_token(token)
    _assert_supported_token(tok)
    amt = _quantize_by_token(tok, amount)
    if amt <= 0:
        raise ValueError("Recharge amount must be positive")

    order = _create_order(user_id=user_id, amount=amt, token=tok, provider=provider or _PROVIDER)
    if tok != "POINT":
        order = ensure_payment(order)  # 赋回
    return order

def ensure_payment(order_or_id: Union[int, RechargeOrder]) -> RechargeOrder:
    order = order_or_id if isinstance(order_or_id, RechargeOrder) else get_order_or_404(order_or_id)
    if order.token == "POINT":
        return order

    if getattr(order, "payment_id", None):
        provider = _get_provider(order.provider)
        inv_id, pay_id, url, addr, extra = provider.ensure_payment(order)
        return _write_back_fields(
            order.id,
            invoice_id=inv_id,
            payment_id=pay_id,
            payment_url=url,
            pay_address=addr,
            pay_currency=extra.get("pay_currency"),
            pay_amount=extra.get("pay_amount"),
            network=extra.get("network"),
            expires_sec=extra.get("expires_sec"),
            purchase_id=extra.get("purchase_id"),
        )

    now = time.time()
    if _CREATING.get(order.id, 0) and now - _CREATING[order.id] < 15:
        return get_order_or_404(order.id)
    _CREATING[order.id] = now
    try:
        provider = _get_provider(order.provider)
        inv_id, pay_id, url, addr, extra = provider.ensure_payment(order)
        return _write_back_fields(
            order.id,
            invoice_id=inv_id,
            payment_id=pay_id,
            payment_url=url,
            pay_address=addr,
            pay_currency=extra.get("pay_currency"),
            pay_amount=extra.get("pay_amount"),
            network=extra.get("network"),
            expires_sec=extra.get("expires_sec"),
            purchase_id=extra.get("purchase_id"),
        )
    finally:
        _CREATING.pop(order.id, None)

# =============================================================================
# 查询 / 状态变更
# =============================================================================
def get_order(order_id: int) -> Optional[RechargeOrder]:
    return _get_order(order_id)

def get_order_or_404(order_id: int) -> RechargeOrder:
    o = get_order(order_id)
    if not o:
        raise ValueError(f"Order #{order_id} not found")
    return o

def list_user_orders(user_id: int, limit: int = 10) -> List[RechargeOrder]:
    with get_session() as s:
        return (
            s.query(RechargeOrder)  # type: ignore
            .filter(RechargeOrder.user_tg_id == user_id)  # type: ignore
            .order_by(RechargeOrder.id.desc())  # type: ignore
            .limit(limit)
            .all()
        )

def mark_order_success(order_id: int, tx_hash: Optional[str] = None) -> bool:
    try:
        return _mark_success(order_id, tx_hash=tx_hash)
    except Exception as e:
        logger.exception("mark_order_success failed: %s", e)
        return False

def mark_order_failed(order_id: int, reason: Optional[str] = None) -> bool:
    try:
        if callable(_mark_failed):  # type: ignore
            return _mark_failed(order_id, reason=reason)  # type: ignore[misc]
    except Exception as e:
        logger.warning("mark_failed not implemented or failed, fallback set_expired. err=%s", e)
    return mark_order_expired(order_id)

def mark_order_expired(order_id: int) -> bool:
    try:
        if callable(_set_expired):  # type: ignore
            return _set_expired(order_id)  # type: ignore[misc]
    except Exception as e:
        logger.info("set_expired not implemented in models.recharge, will fallback. err=%s", e)
    with get_session() as s:
        o: RechargeOrder = s.get(RechargeOrder, order_id)  # type: ignore
        if not o:
            return False
        if o.status in (OrderStatus.SUCCESS, OrderStatus.FAILED, OrderStatus.EXPIRED):
            return True
        o.status = OrderStatus.EXPIRED
        o.finished_at = datetime.utcnow()
        s.add(o)
        s.commit()
    return True

# =============================================================================
# 刷新 / 轮询（兼容后台签名）
# =============================================================================
def _refresh_status_if_needed_core(order_or_id: Union[int, RechargeOrder]) -> Optional[RechargeOrder]:
    """
    原始核心实现：入参为订单或订单ID。
    """
    order = order_or_id if isinstance(order_or_id, RechargeOrder) else get_order(order_or_id)
    if not order:
        return None
    if order.status in (OrderStatus.SUCCESS, OrderStatus.FAILED, OrderStatus.EXPIRED):
        return order

    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    if order.expire_at:
        exp = order.expire_at.replace(tzinfo=timezone.utc)
        if now_utc >= exp and order.status == OrderStatus.PENDING:
            mark_order_expired(order.id)
            return get_order(order.id)

    cached = _STATUS_CACHE.get(order.id)
    if cached and time.time() - cached[0] < _POLL_MIN_INTERVAL:
        if cached[2] in (OrderStatus.SUCCESS, OrderStatus.FAILED, OrderStatus.EXPIRED):
            _write_status_if_changed(order.id, cached[2])
            return get_order(order.id)
        return order

    return refresh_from_provider(order)

def refresh_status_if_needed(*args, **kwargs) -> Optional[RechargeOrder]:
    """
    兼容包装：
      - refresh_status_if_needed(order_id | order)
      - refresh_status_if_needed(db, order_id)   ← 后台控制器旧签名
    """
    # 形如 refresh_status_if_needed(db, order_id)
    if args and isinstance(args[0], _SA_Session):
        if len(args) >= 2:
            return _refresh_status_if_needed_core(args[1])
        if "order_id" in kwargs:
            return _refresh_status_if_needed_core(kwargs["order_id"])
        raise ValueError("refresh_status_if_needed(db, order_id) missing order_id")

    # 形如 refresh_status_if_needed(order_id|order)
    if args:
        return _refresh_status_if_needed_core(args[0])
    if "order_id" in kwargs:
        return _refresh_status_if_needed_core(kwargs["order_id"])
    if "order_or_id" in kwargs:
        return _refresh_status_if_needed_core(kwargs["order_or_id"])
    raise ValueError("refresh_status_if_needed requires order or order_id")

def refresh_from_provider(order_or_id: Union[int, RechargeOrder]) -> Optional[RechargeOrder]:
    order = order_or_id if isinstance(order_or_id, RechargeOrder) else get_order(order_or_id)
    if not order:
        return None
    if order.status in (OrderStatus.SUCCESS, OrderStatus.FAILED, OrderStatus.EXPIRED):
        return order

    provider = _get_provider(order.provider)
    mapped, raw = provider.poll_status(order)
    _STATUS_CACHE[order.id] = (time.time(), raw or "unknown", mapped or OrderStatus.PENDING)

    if not mapped:
        return order
    _write_status_if_changed(order.id, mapped)
    return get_order(order.id)

def _write_status_if_changed(order_id: int, mapped: OrderStatus) -> None:
    with get_session() as s:
        o: RechargeOrder = s.get(RechargeOrder, order_id)  # type: ignore
        if not o or o.status == mapped:
            return
        if mapped == OrderStatus.SUCCESS:
            try:
                _mark_success(o.id, tx_hash=None)
            except Exception as e:
                logger.exception("mark_success failed in _write_status_if_changed: %s", e)
        elif mapped == OrderStatus.EXPIRED:
            o.status = OrderStatus.EXPIRED
            o.finished_at = datetime.utcnow()
            s.add(o)
            s.commit()
        elif mapped == OrderStatus.FAILED:
            try:
                if callable(_mark_failed):  # type: ignore
                    _mark_failed(o.id, reason="provider_failed")  # type: ignore[misc]
                else:
                    o.status = OrderStatus.EXPIRED
                    o.finished_at = datetime.utcnow()
                    s.add(o)
                    s.commit()
            except Exception as e:
                logger.exception("mark_failed fallback failed: %s", e)

# =============================================================================
# IPN 签名校验
# =============================================================================
def verify_ipn_signature(raw_body_bytes: bytes, x_nowpayments_sig: str) -> bool:
    try:
        secret = (getattr(settings, "NOWPAYMENTS_IPN_SECRET", None) or _NP_IPN_SECRET or "").encode("utf-8")
        mac = hmac.new(secret, msg=raw_body_bytes, digestmod="sha512").hexdigest()
        return mac.lower() == (x_nowpayments_sig or "").lower()
    except Exception as e:
        logger.exception("verify_ipn_signature error: %s", e)
        return False

# =============================================================================
# UI 渲染（保持向后兼容；此处也改成 v3 友好的写法）
# =============================================================================
def _status_text(status: OrderStatus, lang: str) -> str:
    def _tf(keys: Sequence[str], fallback: str) -> str:
        for k in keys:
            try:
                v = t(k, lang)
            except Exception:
                v = ""
            if v:
                return v
        return fallback

    if status == OrderStatus.SUCCESS:
        return _tf(["recharge.fields.status.success", "recharge.status.success"], "✅ SUCCESS")
    if status == OrderStatus.FAILED:
        return _tf(["recharge.fields.status.failed", "recharge.status.failed"], "❌ FAILED")
    if status == OrderStatus.EXPIRED:
        return _tf(["recharge.fields.status.expired", "recharge.status.expired"], "🕒 EXPIRED")
    return _tf(["recharge.fields.status.pending", "recharge.status.pending"], "⏳ PENDING")

def build_order_text(order: RechargeOrder, lang: str, pay_link: Optional[str] = None) -> str:
    title = t("recharge.title", lang) or "💰 充值中心"
    f_id = t("recharge.fields.id", lang) or "🧾 订单编号"
    f_token = t("recharge.fields.token", lang) or "🪙 币种"
    f_amount = t("recharge.fields.amount", lang) or "💵 金额"
    f_created = t("recharge.fields.created", lang) or "🗓️ 创建时间"
    f_expires = t("recharge.fields.expires", lang) or "⏰ 过期时间"
    link_text = t("recharge.link", lang) or "🔗 支付链接"
    status_label = (t("recharge.fields.status.label", lang) or "").strip()

    status_text = _status_text(order.status, lang)
    if getattr(order, "pay_amount", None):
        _ccy = getattr(order, "pay_currency", None) or order.token
        amount_txt = f"{order.pay_amount} {_ccy}"
    else:
        amount_txt = _fmt_amount_for_display(order.token, order.amount)

    created_txt = order.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if order.created_at else "-"
    expires_txt = order.expire_at.strftime("%Y-%m-%d %H:%M:%S UTC") if order.expire_at else "-"

    lines = [
        f"{title}",
        "—" * 20,
        f"{f_id}: #{order.id}",
        f"{f_token}: {order.token}",
        f"{f_amount}: {amount_txt}",
    ]
    if status_label:
        lines.append(f"{status_label}: {status_text}")
    else:
        lines.append(f"{status_text}")
    lines.append(f"{f_created}: {created_txt}")
    lines.append(f"{f_expires}: {expires_txt}")

    addr_label = t("recharge.invoice.address", lang) or t("recharge.fields.address", lang) or "🏦 收款地址"
    amt_label = t("recharge.invoice.amount", lang) or (t("recharge.fields.amount", lang) or "💵 金额")
    net_label = t("recharge.invoice.network", lang) or t("recharge.tutorial.network", lang) or "🌐 网络"

    if getattr(order, "pay_address", None):
        lines.append(f"{addr_label}: {order.pay_address}")
    if getattr(order, "pay_amount", None):
        ccy = getattr(order, "pay_currency", None) or order.token
        lines.append(f"{amt_label}: {order.pay_amount} {ccy}")
    if getattr(order, "network", None):
        lines.append(f"{net_label}: {order.network}")

    link = pay_link or getattr(order, "payment_url", None)
    if link:
        lines.append(f"{link_text}: {link}")
    return "\n".join(lines)

def build_recharge_keyboard(
    order: RechargeOrder,
    lang: str,
    show_link: bool = True,
    show_refresh: bool = True,
    show_back: bool = True,
) -> InlineKeyboardMarkup:
    # aiogram v3: 需要传入 inline_keyboard（二维数组），不用 .add() / row_width
    rows: List[List[InlineKeyboardButton]] = []

    if show_link:
        url = getattr(order, "payment_url", None)
        if url:
            txt = t("recharge.link", lang) or "🔗 支付链接"
            rows.append([InlineKeyboardButton(text=txt, url=url)])

    if getattr(order, "pay_address", None):
        rows.append([
            InlineKeyboardButton(
                text=t("recharge.copy_addr", lang) or "📋 复制地址",
                callback_data=f"recharge:copy_addr:{order.id}",
            )
        ])

    _amt_to_copy = getattr(order, "pay_amount", None) or _fmt_amount_for_display(order.token, order.amount)
    if _amt_to_copy:
        rows.append([
            InlineKeyboardButton(
                text=t("recharge.copy_amount", lang) or "📋 复制金额",
                callback_data=f"recharge:copy_amt:{order.id}",
            )
        ])

    if show_refresh:
        txt = t("recharge.refresh", lang) or "🔁 刷新状态"
        rows.append([InlineKeyboardButton(text=txt, callback_data=f"recharge:refresh:{order.id}")])

    if show_back:
        txt = t("menu.back", lang) or "⬅️ 返回"
        rows.append([InlineKeyboardButton(text=txt, callback_data="recharge:back")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def render_order_card(order_id: int, lang: str) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    order = get_order(order_id)
    if not order:
        return ("未找到订单", None)
    pay_link = getattr(order, "payment_url", None)
    text = build_order_text(order, lang, pay_link=pay_link)
    kb = build_recharge_keyboard(order, lang, show_link=bool(pay_link and isinstance(pay_link, str)))
    return (text, kb)

# =============================================================================
# 后台兼容包装（新增）
# =============================================================================
def mark_expired(db, order_id: int) -> None:
    """
    兼容后台控制器：mark_expired(db, order_id)
    内部调用现有 mark_order_expired，无需使用 db。
    """
    mark_order_expired(order_id)
