# nowpayments.py
# -*- coding: utf-8 -*-
"""
NOWPayments 对接（同步包装 + 异步客户端，含 IPN 校验）
----------------------------------------------------------------
本文件兼容两种使用方式：
1) 同步包装函数（推荐在你的 service 层内调用）：
   - create_invoice(token: str, amount: Decimal, order_id: int, user_id: int) -> dict
     返回示例:
       {
         "pay_url": "https://nowpayments.io/payment/?iid=INV_...",
         "invoice_id": "INV_....",
         "payment_id": null,
         "pay_address": null,
         "raw": {...原始返回...},
         "extra": {
            "pay_amount": 99.872114,
            "pay_currency": "USDTTRC20",
            "network": "TRON",
            "expires_sec": 870,
            "expires_at": "2025-09-30T03:35:00Z"
         }
       }

2) 轻量异步客户端（在需要时可直接使用）：
   class NowPaymentsClient:
       - min_amount(...)
       - create_payment(...)
       - create_invoice_link(...)
       - get_payment(...)
       - get_invoice(...)

环境变量（.env）：
- NOWPAYMENTS_BASE_URL     默认 https://api.nowpayments.io/v1
- NOWPAYMENTS_API_KEY      必填
- NOWPAYMENTS_IPN_SECRET   用于 IPN 签名校验（x-nowpayments-sig）
- NOWPAYMENTS_IPN_URL      你的回调公网 URL（例如 https://host/nowpayments-ipn ）
- RECHARGE_COIN_USDT       例如 USDTTRC20（可覆盖映射）
- RECHARGE_COIN_TON        例如 TON 或 TONCOIN（可覆盖映射）

币种映射优先顺序：
  环境变量 RECHARGE_COIN_USDT / RECHARGE_COIN_TON > 下面 CURRENCY_MAP_DEFAULT 的默认值
"""

from __future__ import annotations

import os
import hmac
import json
import time
import hashlib
import logging
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple, List

# ====== 日志 ======
logger = logging.getLogger("core.clients.nowpayments")

# ====== 环境配置 ======
NOWP_BASE = os.getenv("NOWPAYMENTS_BASE_URL", "https://api.nowpayments.io/v1").rstrip("/")
NOWP_API_KEY = (os.getenv("NOWPAYMENTS_API_KEY", "") or "").strip()
NOWP_IPN_SECRET = (os.getenv("NOWPAYMENTS_IPN_SECRET", "") or "").strip()
NOWP_IPN_URL = (os.getenv("NOWPAYMENTS_IPN_URL", "") or "").strip()  # 例如: https://your.host/nowpayments-ipn

# 若有自定义映射，以环境变量为准
ENV_COIN_USDT = (os.getenv("RECHARGE_COIN_USDT", "") or "").strip()  # 如 USDTTRC20
ENV_COIN_TON  = (os.getenv("RECHARGE_COIN_TON", "")  or "").strip()  # 如 TON / TONCOIN

# 业务侧代号 -> NOWPayments 货币码（默认）
CURRENCY_MAP_DEFAULT = {
    "USDT": "USDTTRC20",
    "TON":  "TON",     # ✅ 默认 TON，若你设置了 TONCOIN 也会被环境覆盖
    "POINT": "POINT",
}

# 最终使用的映射（环境覆盖默认）
CURRENCY_MAP = {
    "USDT": ENV_COIN_USDT or CURRENCY_MAP_DEFAULT["USDT"],
    "TON":  ENV_COIN_TON  or CURRENCY_MAP_DEFAULT["TON"],
    "POINT": CURRENCY_MAP_DEFAULT["POINT"],
}

# price_currency 映射（基币，不带网络，小写）
PRICE_CURRENCY_MAP = {
    "USDT": "usd",
    "TON": "usd",
    "POINT": "point",
}

# ======================== 工具函数 ========================

def _sorted_json(data: Dict[str, Any]) -> str:
    """NOWPayments IPN 签名算法要求把 JSON 按 key 排序再 stringify。"""
    def _sort(o):
        if isinstance(o, dict):
            return {k: _sort(o[k]) for k in sorted(o.keys())}
        if isinstance(o, list):
            return [_sort(i) for i in o]
        return o
    return json.dumps(_sort(data), separators=(",", ":"), ensure_ascii=False)


def verify_ipn_signature(raw_json: Dict[str, Any], received_sig: str) -> bool:
    """
    校验 x-nowpayments-sig。
    - raw_json: 原始 JSON（字典）
    - received_sig: 请求头 x-nowpayments-sig
    """
    if not NOWP_IPN_SECRET:
        logger.warning("NOWPAYMENTS_IPN_SECRET is empty; cannot verify IPN signature.")
        return False
    message = _sorted_json(raw_json).encode("utf-8")
    digest = hmac.new(NOWP_IPN_SECRET.encode("utf-8"), message, hashlib.sha512).hexdigest()
    ok = hmac.compare_digest(digest, received_sig or "")
    if not ok:
        logger.warning("NOWPayments IPN signature NOT match.")
    return ok


def map_pay_currency(biz_token: str) -> str:
    """
    将业务内的 token（USDT/TON/POINT）映射到 NOWPayments 的 pay_currency。
    """
    key = (biz_token or "").upper()
    pay_cur = CURRENCY_MAP.get(key)
    if not pay_cur:
        raise ValueError(f"Unsupported token for NOWPayments: {biz_token}")
    if pay_cur == "POINT":
        raise ValueError("POINT is not a blockchain currency; cannot create NOWPayments invoice.")
    return pay_cur


def _amount_2f(x: Decimal | float | int) -> float:
    """
    供传参给 NOWPayments 的金额，量化为最多两位小数（浮点格式）。
    """
    return float(Decimal(str(x)).quantize(Decimal("0.01")))


def infer_network(pay_currency: str) -> Optional[str]:
    """
    根据 pay_currency 推断链网络，便于 UI 直观展示。
    例：USDTTRC20 -> TRON, USDTERC20 -> ETH, USDTBSC -> BSC
    """
    if not pay_currency:
        return None
    c = pay_currency.upper()
    # 常见映射
    if "TRC20" in c or c.endswith("TRC20"):
        return "TRON"
    if "ERC20" in c:
        return "ETH"
    if "BSC" in c or "BEP20" in c:
        return "BSC"
    if "POLYGON" in c or "MATIC" in c:
        return "Polygon"
    if c.startswith("TON"):
        return "TON"
    # 兜底用币种大写
    return c


def _extract_expiry(payload_or_resp: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    """
    从返回数据尝试提取过期时间：
    - 部分响应会有 expiration_estimate_date / valid_until / created_at + ttl 等。
    返回 (expires_sec, expires_at_iso)
    """
    # 直接的时间戳/ISO 文本
    for key in ("expiration_estimate_date", "valid_until", "expires_at"):
        v = payload_or_resp.get(key)
        if isinstance(v, str) and v:
            # 仅透传 ISO 文本；秒数尽量估算（如果有 now 字段则计算）
            try:
                # 简单解析（不严格时区处理，交给上层展示）
                expires_at_iso = v
                return None, expires_at_iso
            except Exception:
                pass

    # 如果返回了 seconds/ttl
    for key in ("expires_in", "ttl", "timeout", "valid_for"):
        v = payload_or_resp.get(key)
        try:
            secs = int(v)
            return secs if secs > 0 else None, None
        except Exception:
            pass

    # 部分响应给 created_at + 15min 等信息；这里不强算，交给上层兜底显示
    return None, None

# ======================== 轻量异步客户端 ========================
# 注：项目主体为 aiogram，通常具备 aiohttp 运行环境；保留异步客户端便于定制更复杂交互。

import aiohttp
from aiohttp import ClientTimeout

class NowPaymentsClient:
    """NOWPayments API 轻量客户端（创建支付 / 创建发票 / 查询 / 最小金额）"""

    def __init__(self, api_key: Optional[str] = None, base: Optional[str] = None):
        self.api_key = (api_key or NOWP_API_KEY).strip()
        self.base = (base or NOWP_BASE).rstrip("/")
        if not self.api_key:
            raise RuntimeError("NOWPAYMENTS_API_KEY is empty")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=ClientTimeout(total=30),
                headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def min_amount(self, currency_to: str, currency_from: str = "usd") -> float:
        """获取最小金额。"""
        sess = await self._session_get()
        url = f"{self.base}/min-amount?currency_from={currency_from}&currency_to={currency_to}"
        async with sess.get(url) as resp:
            data = await resp.json()
            # 返回示例: {"currency_from":"usd","currency_to":"USDTTRC20","min_amount":1.45}
            return float(data.get("min_amount", 1.0))

    async def create_payment(
        self,
        *,
        price_amount: float,
        pay_currency: str,
        price_currency: str = "usd",
        order_id: Optional[str] = None,
        ipn_callback_url: Optional[str] = None,
        is_fixed_rate: bool = True,           # ✅ 统一固定汇率
        is_fee_paid_by_user: bool = True,
    ) -> Dict[str, Any]:
        """
        创建支付以获取 pay_address（非托管发票页面，而是“地址+金额”直充）。
        更推荐使用 create_invoice_link 生成官方托管支付页（带 pay_url）。
        """
        sess = await self._session_get()
        payload = {
            "price_amount": float(price_amount),
            "price_currency": price_currency,
            "pay_currency": pay_currency,
            "is_fixed_rate": bool(is_fixed_rate),
            "is_fee_paid_by_user": bool(is_fee_paid_by_user),
        }
        if order_id:
            payload["order_id"] = str(order_id)
        if ipn_callback_url or NOWP_IPN_URL:
            payload["ipn_callback_url"] = ipn_callback_url or NOWP_IPN_URL

        async with sess.post(f"{self.base}/payment", data=json.dumps(payload)) as resp:
            data = await resp.json()
            # 返回包含: id, pay_address, pay_currency, pay_amount, payment_status 等
            return data

    async def create_invoice_link(
        self,
        *,
        price_amount: float,
        pay_currency: str,
        price_currency: str = "usd",
        order_id: Optional[str] = None,
        ipn_callback_url: Optional[str] = None,
        is_fixed_rate: bool = True,           # ✅ 统一固定汇率
        is_fee_paid_by_user: bool = True,
        order_description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        创建托管发票（Hosted Invoice），可返回 invoice_url（即你要展示的支付链接）。
        """
        sess = await self._session_get()
        payload = {
            "price_amount": float(price_amount),
            "price_currency": price_currency,
            "pay_currency": pay_currency,
            "is_fixed_rate": bool(is_fixed_rate),
            "is_fee_paid_by_user": bool(is_fee_paid_by_user),
        }
        if order_id:
            payload["order_id"] = str(order_id)
        if order_description:
            payload["order_description"] = str(order_description)
        if ipn_callback_url or NOWP_IPN_URL:
            payload["ipn_callback_url"] = ipn_callback_url or NOWP_IPN_URL

        async with sess.post(f"{self.base}/invoice", data=json.dumps(payload)) as resp:
            data = await resp.json()
            # 典型返回包含: id / invoice_id / invoice_url / pay_currency / price_amount 等
            return data

    async def get_payment(self, payment_id: int) -> Dict[str, Any]:
        sess = await self._session_get()
        async with sess.get(f"{self.base}/payment/{payment_id}") as resp:
            return await resp.json()

    async def get_invoice(self, invoice_id: str) -> Dict[str, Any]:
        sess = await self._session_get()
        async with sess.get(f"{self.base}/invoice/{invoice_id}") as resp:
            return await resp.json()

    async def list_payments_by_address(
        self, *, pay_address: str, limit: int = 25, page: int = 0
    ) -> Dict[str, Any]:
        """
        官方公开文档未完全标准化此筛选参数；实际项目通常用 /payment?limit=..&page=.. 后端侧做过滤。
        这里保留 pay_address 作为查询参数（若网关忽略则在上层做地址过滤）。
        """
        sess = await self._session_get()
        params = f"limit={limit}&page={page}&orderBy=created_at&sort=desc&pay_address={pay_address}"
        async with sess.get(f"{self.base}/payment?{params}") as resp:
            return await resp.json()
# ======================== 同步包装（推荐在 service 层调用） ========================
# 为了避免在 aiogram 的异步上下文中处理额外 loop，这里使用标准库 urllib 进行同步 HTTP。
# 若你更喜欢 requests/httpx，可在此替换实现。

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

def _http_post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            resp_bytes = resp.read()
            text = resp_bytes.decode("utf-8", "ignore")
            try:
                return json.loads(text)
            except Exception:
                logger.error("NOWPayments JSON decode failed: %s", text[:500])
                raise
    except HTTPError as e:
        err = e.read().decode("utf-8", "ignore")
        logger.error("NOWPayments HTTPError %s: %s", e.code, err[:500])
        raise
    except URLError as e:
        logger.error("NOWPayments URLError: %s", e)
        raise


def create_invoice(*, token: str, amount: Decimal, order_id: int, user_id: int) -> Dict[str, Any]:
    """
    同步创建“托管发票”并返回支付链接（适配 services/recharge_service.new_order 的调用）。
    返回结构：
        {
          "pay_url": "https://nowpayments.io/payment/?iid=INV_...",
          "invoice_id": "INV_....",
          "payment_id": null,
          "pay_address": null,
          "raw": {...原始返回...},
          "extra": { ...参见顶部说明... }
        }
    """
    if not NOWP_API_KEY:
        raise RuntimeError("NOWPAYMENTS_API_KEY is empty")

    if amount <= 0:
        raise ValueError("Amount must be positive")

    pay_currency = map_pay_currency(token)  # 保持原样（例如 USDTTRC20 / TON / TONCOIN）
    price_currency = PRICE_CURRENCY_MAP.get(token.upper(), 'usd')
    price_amount = _amount_2f(amount)

    payload = {
        "price_amount": float(price_amount),
        "price_currency": price_currency,
        "pay_currency": pay_currency,
        "is_fixed_rate": True,                 # ✅ 固定汇率
        "is_fee_paid_by_user": True,
        "order_id": str(order_id),
    }
    # 携带 IPN 回调
    if NOWP_IPN_URL:
        payload["ipn_callback_url"] = NOWP_IPN_URL

    # 可选：带上 order_description，用于运营、对账
    payload["order_description"] = f"recharge u{user_id} oid{order_id} {token}"

    headers = {
        "x-api-key": NOWP_API_KEY,
        "Content-Type": "application/json",
    }

    url = f"{NOWP_BASE}/invoice"
    data: Dict[str, Any]
    try:
        data = _http_post_json(url, payload, headers)
    except HTTPError as e:
        logger.error("Invoice creation failed: %s %s", e.code, e.read().decode("utf-8", "ignore"))
        raise

    # 兼容不同字段名：有的返回 invoice_url，有的返回 payment_url
    pay_url = data.get("invoice_url") or data.get("payment_url") or data.get("pay_url")
    invoice_id = data.get("id") or data.get("invoice_id") or None

    # 先尝试从发票返回里直接拿 pay_amount 等（若网关支持直接返回）
    extra_pay_amount = data.get("pay_amount") or data.get("amount")
    extra_pay_currency = data.get("pay_currency") or pay_currency
    expires_sec, expires_at_iso = _extract_expiry(data)
    network_name = infer_network(str(extra_pay_currency or pay_currency))

    ret: Dict[str, Any] = {
        "pay_url": pay_url,
        "invoice_id": str(invoice_id) if invoice_id is not None else None,
        "payment_id": data.get("payment_id"),
        "pay_address": data.get("pay_address"),
        "raw": data,
        "extra": {
            "pay_amount": float(extra_pay_amount) if extra_pay_amount is not None else None,
            "pay_currency": extra_pay_currency,
            "network": network_name,
            "expires_sec": expires_sec,
            "expires_at": expires_at_iso,
        },
    }

    # 如果托管发票没有提供地址或真实应付金额，则回退调用 /payment 补齐“地址 + 真实金额”
    # 这是你需要优先展示在教程中的那部分数据。
    if not ret["pay_address"] or ret["extra"]["pay_amount"] is None:
        logger.warning("NOWPayments pay_address or pay_amount missing, fallback to /payment (address mode)")
        pay_data = _http_post_json(
            f"{NOWP_BASE}/payment",
            {
                "price_amount": float(price_amount),
                "price_currency": price_currency,
                "pay_currency": pay_currency,
                "is_fixed_rate": True,         # ✅ 固定汇率（与发票一致）
                "is_fee_paid_by_user": True,
                "order_id": str(order_id),
                **({"ipn_callback_url": NOWP_IPN_URL} if NOWP_IPN_URL else {}),
            },
            headers,
        )
        p_amount = pay_data.get("pay_amount")
        p_currency = pay_data.get("pay_currency") or pay_currency
        p_network = infer_network(str(p_currency))
        p_expires_sec, p_expires_at = _extract_expiry(pay_data)

        ret.update(
            {
                "invoice_id": ret["invoice_id"] or None,  # 保留原发票 ID（可能为空）
                "payment_id": pay_data.get("id"),
                "pay_address": pay_data.get("pay_address"),
                "raw_payment": pay_data,
            }
        )
        ret["extra"].update(
            {
                "pay_amount": float(p_amount) if p_amount is not None else ret["extra"]["pay_amount"],
                "pay_currency": p_currency or ret["extra"]["pay_currency"],
                "network": p_network or ret["extra"]["network"],
                "expires_sec": p_expires_sec if p_expires_sec is not None else ret["extra"]["expires_sec"],
                "expires_at": p_expires_at or ret["extra"]["expires_at"],
            }
        )

    return ret


# ======================== IPN/状态辅助 ========================

def parse_ipn_status(ipn_json: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """
    将 NOWPayments 的 payment_status 映射为内部状态：
    返回 (status, tx_hash)
      - status ∈ {"PENDING", "SUCCESS", "FAILED", "EXPIRED"}
      - tx_hash 可能在已确认时给出
    """
    pstatus = (ipn_json.get("payment_status") or "").lower()
    tx_hash = ipn_json.get("payin_hash") or ipn_json.get("purchase_id") or None

    if pstatus in {"finished", "confirmed", "completed"}:
        return "SUCCESS", tx_hash
    if pstatus in {"partially_paid", "waiting", "confirming"}:
        return "PENDING", tx_hash
    if pstatus in {"failed", "refunded", "expired", "chargeback"}:
        if pstatus == "expired":
            return "EXPIRED", tx_hash
        return "FAILED", tx_hash
    # 未知状态，按待处理
    return "PENDING", tx_hash


def example_verify_and_extract(ipn_headers: Dict[str, str], ipn_body: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
    """
    用于路由示例：先验签，再提取状态。
    返回: (ok, status, tx_hash)
    """
    sig = ipn_headers.get("x-nowpayments-sig") or ipn_headers.get("X-Nowpayments-Sig") or ""
    ok = verify_ipn_signature(ipn_body, sig)
    status, txh = parse_ipn_status(ipn_body)
    return ok, status, txh
