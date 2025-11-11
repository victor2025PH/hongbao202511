# routers/recharge.py
# -*- coding: utf-8 -*-
"""
充值中心（USDT / TON / POINT）：
- /recharge / recharge:main
- recharge:new:{TOKEN}
- recharge:amt:{N}
- recharge:amt:custom + 文本输入
- recharge:refresh:{order_id}
- recharge:copy_addr:{order_id}
- recharge:copy_amt:{order_id}

说明：
1) 路由不再自行“创建第二次通道订单”，而是调用服务层 ensure_payment()，保证只有一个 payment。
2) 展示、复制、二维码与支付链接全部来自 order 中写回的 payment 字段（单一事实来源）。
3) 新增：下单阶段的“假进度条加载画面”，避免用户误以为卡死反复点。
4) 新增：金额选择键盘展示为美元文案（$10/$50/$100/$200），callback 保持原样。
"""

from __future__ import annotations

import io
import re
import importlib
import logging
import asyncio
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Dict, Sequence, Optional, Callable, Any, List

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.exceptions import TelegramBadRequest

# 生成二维码依赖
try:
    import qrcode
    from PIL import Image  # noqa: F401
except Exception:  # noqa: BLE001
    qrcode = None
    Image = None

# i18n
try:
    from core.i18n.i18n import t
except Exception:
    # 极简兜底
    def t(key: str, lang: str, **kwargs) -> str:
        return ""

# 键盘多路径兜底导入（不同项目结构下的兼容）
recharge_main_kb = None
recharge_amount_kb = None
recharge_order_kb = None
back_home_kb = None
recharge_invoice_kb = None
_kb_import_ok = False
for _modpath in (
    "core.utils.keyboards",
    "routers.keyboards",
    "keyboards",
    "app.keyboards",
):
    try:
        _kb_mod = importlib.import_module(_modpath)
        recharge_main_kb = getattr(_kb_mod, "recharge_main_kb")
        recharge_amount_kb = getattr(_kb_mod, "recharge_amount_kb", None)
        recharge_order_kb = getattr(_kb_mod, "recharge_order_kb")
        back_home_kb = getattr(_kb_mod, "back_home_kb")
        recharge_invoice_kb = getattr(_kb_mod, "recharge_invoice_kb", None)
        _kb_import_ok = True
        break
    except Exception:
        continue

if not _kb_import_ok:
    # 万一完全没有，就给个非常简陋的兜底，避免崩溃（建议尽快用你项目里的 keyboards.py 覆盖）
    def _simple_kb(rows: List[List[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def recharge_main_kb(lang: str) -> InlineKeyboardMarkup:  # type: ignore
        rows = [
            [InlineKeyboardButton(text="USDT", callback_data="recharge:new:USDT")],
            [InlineKeyboardButton(text="TON", callback_data="recharge:new:TON")],
            [InlineKeyboardButton(text="POINT", callback_data="recharge:new:POINT")],
        ]
        return _simple_kb(rows)

    def recharge_order_kb(order_id: int, lang: str) -> InlineKeyboardMarkup:  # type: ignore
        rows = [
            [InlineKeyboardButton(text="🔁 刷新状态", callback_data=f"recharge:refresh:{order_id}")],
            [InlineKeyboardButton(text="⬅️ 返回", callback_data="recharge:main")],
        ]
        return _simple_kb(rows)

    def back_home_kb(lang: str) -> InlineKeyboardMarkup:  # type: ignore
        rows = [[InlineKeyboardButton(text="⬅️ 返回", callback_data="recharge:main")]]
        return _simple_kb(rows)

    def recharge_invoice_kb(order_id: int, lang: str) -> InlineKeyboardMarkup:  # type: ignore
        rows = [
            [InlineKeyboardButton(text="🔁 刷新状态", callback_data=f"recharge:refresh:{order_id}")],
            [InlineKeyboardButton(text="📋 复制地址", callback_data=f"recharge:copy_addr:{order_id}")],
            [InlineKeyboardButton(text="📋 复制金额", callback_data=f"recharge:copy_amt:{order_id}")],
            [InlineKeyboardButton(text="⬅️ 返回", callback_data="recharge:main")],
        ]
        return _simple_kb(rows)

# DB
try:
    from models.db import get_session
    from models.user import User
except Exception:
    # 兜底：允许模块路径差异
    from ..models.db import get_session  # type: ignore
    from ..models.user import User  # type: ignore

_logger = logging.getLogger("recharge_router")
router = Router()

# =============== i18n helpers ===============
def _tt(key: str, lang: str, **kwargs) -> str:
    try:
        return t(key, lang, **kwargs) or ""
    except Exception:
        return ""

def _tt_first(keys: Sequence[str], lang: str, **kwargs) -> str:
    for k in keys:
        val = _tt(k, lang, **kwargs)
        if val:
            return val
    return ""

# =============== language helpers ===============
def _canon_lang(code: Optional[str]) -> str:
    if not code:
        return "zh"
    c = str(code).strip().lower()
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"
    return "zh"

def _db_lang_or_fallback(user_id: int, fallback_user) -> str:
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u and getattr(u, "language", None):
            return _canon_lang(u.language)
    return _canon_lang(getattr(fallback_user, "language_code", None))

# =============== 服务函数解析 ===============
def _resolve_service() -> Any:
    try:
        return importlib.import_module("services.recharge_service")
    except Exception as e1:
        _logger.warning("services.recharge_service not found, fallback top-level. err=%s", e1)
        return importlib.import_module("recharge_service")

def _pick_callable(mod: Any, *names: str) -> Callable:
    for nm in names:
        fn = getattr(mod, nm, None)
        if callable(fn):
            return fn
    raise ImportError(f"recharge_service 缺少函数：{', '.join(names)}")

def _svc_new_order() -> Callable:
    m = _resolve_service()
    return _pick_callable(m, "new_order", "create_order", "make_order", "create_recharge_order")

def _svc_get_order() -> Callable:
    m = _resolve_service()
    return _pick_callable(m, "get_order", "fetch_order", "read_order", "get_recharge_order", "find_order")

def _svc_refresh() -> Callable:
    m = _resolve_service()
    return _pick_callable(m, "refresh_status_if_needed", "refresh_status", "refresh_order_status", "update_status")

def _svc_ensure_payment() -> Callable:
    m = _resolve_service()
    return _pick_callable(m, "ensure_payment")

# =============== session state ===============
_PENDING_TOKEN: Dict[int, str] = {}       # user_id -> "USDT"/"TON"/"POINT"
_AWAITING_AMOUNT: Dict[int, bool] = {}    # user_id -> True/False

def _clear_pending(user_id: int):
    _PENDING_TOKEN.pop(user_id, None)
    _AWAITING_AMOUNT.pop(user_id, None)

# ====== 文本工具 ======
def _fmt_amt(val) -> str:
    try:
        q = Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        return f"{q:.2f}"
    except Exception:
        return str(val)

def _order_pay_url_from_obj(order) -> Optional[str]:
    for key in ("payment_url", "invoice_url", "pay_url", "url", "pay_link"):
        val = getattr(order, key, None)
        if val:
            return str(val)
    return None

# ====== 二维码 ======
def _build_qr_bytes(addr: str) -> Optional[io.BytesIO]:
    if not addr or qrcode is None:
        return None
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(addr)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# ====== 假进度条（加载动画） ======
def _progress_bar(pct: int) -> str:
    """简易文本进度条（只显示百分比，避免终端字符宽度问题）。"""
    pct = max(0, min(100, int(pct)))
    return f"{pct}%"

async def _start_fake_loader(message: Message, lang: str = "zh"):
    """
    发送“正在生成订单”的消息，并周期性更新进度条。
    返回 stop() 协程函数，订单就绪后调用以结束并删除消息。
    """
    title = "⏳ 正在生成订单，请稍候…" if lang.startswith("zh") else "⏳ Generating your invoice, please wait…"
    wait_msg = await message.answer(f"{title}\n{_progress_bar(6)}")

    stop_event = asyncio.Event()

    async def _runner():
        steps = [10, 18, 26, 34, 42, 50, 58, 66, 74, 82, 89, 93, 96, 98]
        i = 0
        while not stop_event.is_set():
            try:
                pct = steps[i % len(steps)]
                await wait_msg.edit_text(f"{title}\n{_progress_bar(pct)}")
            except Exception:
                pass
            await asyncio.sleep(0.8)
            i += 1

    task = asyncio.create_task(_runner())

    async def _stop(delete: bool = True):
        stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except Exception:
            try:
                task.cancel()
            except Exception:
                pass
        if delete:
            try:
                await wait_msg.delete()
            except Exception:
                pass

    return _stop

# ====== 展示卡片文本 ======
def _order_card_text(order, lang: str, pay_link: Optional[str]) -> str:
    lines: List[str] = []
    title = _tt("recharge.title", lang) or "💰 充值中心"
    lines.append(title)
    lines.append("──────────────────────")

    oid = getattr(order, "id", None)
    if oid:
        lines.append(f"{_tt('recharge.fields.id', lang) or 'ID'} #{oid}")

    token = getattr(order, "token", None) or "USDT"
    lines.append(f"{_tt('recharge.fields.token', lang) or 'Token'} {token}")

    pay_amount = getattr(order, "pay_amount", None)
    pay_currency = getattr(order, "pay_currency", None)
    if pay_amount:
        lines.append(f"{_tt('recharge.invoice.amount', lang) or 'Amount'} {pay_amount} {pay_currency or token}")
    else:
        lines.append(f"{_tt('recharge.fields.amount', lang) or 'Amount'} {_fmt_amt(getattr(order, 'amount', 0))} {token}")

    addr = getattr(order, "pay_address", None)
    if addr:
        addr_html = f'<a href="{pay_link}">{addr}</a>' if pay_link else addr
        lines.append(f"{_tt('recharge.fields.address', lang) or 'Address'} {addr_html}")

    st = str(getattr(order, "status", "PENDING")).lower()
    st_text = _tt(f"recharge.fields.status.{st}", lang) or st.upper()
    lines.append(f"{_tt('recharge.fields.status.label', lang) or 'Status'} {st_text}")

    created = getattr(order, "created_at", None)
    expires = getattr(order, "expire_at", None)
    if created:
        lines.append(f"{_tt('recharge.fields.created', lang) or 'Created'} {created}")
    if expires:
        lines.append(f"{_tt('recharge.fields.expires', lang) or 'Expires'} {expires}")

    final_link = pay_link or _order_pay_url_from_obj(order)
    if final_link:
        link_title = _tt("recharge.link", lang) or "🔗 支付链接"
        lines.append(f'👉 <a href="{final_link}">{link_title}</a>')
    return "\n".join(lines)

def _build_tutorial_caption(order, lang: str, pay_link: Optional[str]) -> str:
    title = _tt("recharge.tutorial.title", lang) or (_tt("recharge.title", lang) or "💰 充值中心")
    step1 = _tt("recharge.tutorial.step1", lang) or "1️⃣ 选择币种"
    step2 = _tt("recharge.tutorial.step2", lang) or "2️⃣ 转账金额"
    step3 = _tt("recharge.tutorial.step3", lang) or "3️⃣ 转账地址"
    step4 = _tt("recharge.tutorial.step4", lang) or "4️⃣ 二维码支付（可选）"
    step5 = _tt("recharge.tutorial.step5", lang) or "5️⃣ 确认与到账"
    warn  = _tt("recharge.tutorial.warn_amount", lang) or "⚠️ 金额必须完全一致，否则不到账！"
    link_text = _tt("recharge.link", lang) or "🔗 支付链接"

    f_token  = _tt("recharge.fields.token", lang) or "🪙 Token"
    f_amount = _tt("recharge.tutorial.amount", lang) or (_tt("recharge.invoice.amount", lang) or "金额")
    f_net    = _tt("recharge.tutorial.network", lang) or (_tt("recharge.invoice.network", lang) or "网络")
    f_exp    = _tt("recharge.tutorial.expires", lang) or (_tt("recharge.invoice.expires", lang) or "过期")

    token        = getattr(order, "token", "USDT")
    pay_amount   = getattr(order, "pay_amount", None)
    pay_currency = getattr(order, "pay_currency", None)
    addr         = getattr(order, "pay_address", None)
    net          = getattr(order, "network", None)
    exp          = getattr(order, "expire_at", None)

    amount_line  = f"{pay_amount} {pay_currency or token}" if pay_amount else f"{_fmt_amt(getattr(order, 'amount', 0))} {token}"
    expires_line = f"{exp} UTC" if exp else "-"

    st = str(getattr(order, "status", "PENDING")).lower()
    status_line = _tt(f"recharge.fields.status.{st}", lang) or st.upper()

    if addr and pay_link:
        addr_line = f'<a href="{pay_link}">{addr}</a>'
    else:
        addr_line = addr or "-"

    lines = [
        title,
        "──────────────────────",
        f"{step1}\n{f_token}：{token}",
        f"{step2}\n{f_amount}：{amount_line}\n{warn}",
        f"{step3}\n{addr_line}",
        f"{step4}\n{_tt('recharge.tutorial.step4_tip', lang) or '使用钱包扫码本消息图片完成支付'}",
        f"{step5}\n{_tt('recharge.tutorial.step5_tip', lang) or '支付成功后点击下方【刷新状态】查看是否到账'}",
        f"{f_net}：{net or '-'}",
        f"{f_exp}：{expires_line}",
        f"{_tt('recharge.fields.status.label', lang) or '状态'}：{status_line}",
    ]
    if pay_link:
        lines.append(f"👉 <a href=\"{pay_link}\">{link_text}</a>")
    return "\n".join(lines)

def _invoice_kb_with_url(order_id: int, lang: str, pay_link: Optional[str]) -> InlineKeyboardMarkup:
    kb = recharge_invoice_kb(order_id, lang) if callable(recharge_invoice_kb) else None
    if not isinstance(kb, InlineKeyboardMarkup):
        kb = InlineKeyboardMarkup(inline_keyboard=getattr(kb, "inline_keyboard", []) or [])
    if pay_link:
        text = _tt("recharge.link", lang) or "🔗 支付链接"
        kb.inline_keyboard.append([InlineKeyboardButton(text=text, url=pay_link)])
    return kb

# ====== 自定义“美元文案”的金额键盘（callback 与原逻辑兼容） ======
def _amount_kb_usd(lang: str) -> InlineKeyboardMarkup:
    """
    aiogram v3 需要显式提供 inline_keyboard（二维数组）。
    这里按 3 个按钮一行排布，最后两行是【自定义金额】和【返回】。
    """
    amounts = [10, 50, 100, 200]

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for i, a in enumerate(amounts, start=1):
        row.append(InlineKeyboardButton(text=f"${a}", callback_data=f"recharge:amt:{a}"))
        if i % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    txt_custom = _tt_first(["recharge.amount.custom", "recharge.custom_amount", "common.custom_amount"], lang) or "🖊 自定义金额"
    txt_back   = _tt_first(["common.back", "recharge.back"], lang) or "⬅️ 返回"

    rows.append([InlineKeyboardButton(text=txt_custom, callback_data="recharge:amt:custom")])
    rows.append([InlineKeyboardButton(text=txt_back, callback_data="recharge:main")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

# =============== /recharge 入口 ===============
@router.message(F.text.regexp(r"^/recharge$"))
async def cmd_recharge(msg: Message):
    user_id = msg.from_user.id
    _clear_pending(user_id)
    lang = _db_lang_or_fallback(user_id, msg.from_user)

    title = _tt("recharge.title", lang)
    choose = _tt_first(["recharge.choose_method", "recharge.choose_token"], lang)
    text = "\n\n".join([x for x in (title, choose) if x])

    await msg.answer(text, parse_mode="HTML", reply_markup=recharge_main_kb(lang))

@router.callback_query(F.data == "recharge:main")
async def recharge_main(cb: CallbackQuery):
    user_id = cb.from_user.id
    _clear_pending(user_id)
    lang = _db_lang_or_fallback(user_id, cb.from_user)

    title = _tt("recharge.title", lang)
    choose = _tt_first(["recharge.choose_method", "recharge.choose_token"], lang)
    text = "\n\n".join([x for x in (title, choose) if x])

    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=recharge_main_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=recharge_main_kb(lang))
    await cb.answer()

# =============== 选择币种 ===============
@router.callback_query(F.data.regexp(r"^recharge:new:(USDT|TON|POINT)$"))
async def recharge_choose_token(cb: CallbackQuery):
    user_id = cb.from_user.id
    lang = _db_lang_or_fallback(user_id, cb.from_user)

    m = re.match(r"^recharge:new:(USDT|TON|POINT)$", cb.data or "")
    token = m.group(1) if m else "USDT"

    _clear_pending(user_id)
    _PENDING_TOKEN[user_id] = token

    title = _tt("recharge.title", lang)
    choose_amount = _tt("recharge.choose_amount", lang)
    text = "\n\n".join([x for x in (title, choose_amount) if x])

    # 使用本文件的“美元文案金额键盘”
    kb = _amount_kb_usd(lang)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

# =============== 快捷金额 ===============
@router.callback_query(F.data.regexp(r"^recharge:amt:(\d+)$"))
async def recharge_amount_quick(cb: CallbackQuery):
    user_id = cb.from_user.id
    lang = _db_lang_or_fallback(user_id, cb.from_user)

    m = re.match(r"^recharge:amt:(\d+)$", cb.data or "")
    amt = Decimal(m.group(1)) if m else Decimal("0")

    token = _PENDING_TOKEN.get(user_id) or "USDT"
    _AWAITING_AMOUNT.pop(user_id, None)

    # ===== 启动假进度条 =====
    stop_loader = await _start_fake_loader(cb.message, lang)

    try:
        new_order_fn = _svc_new_order()
        order = new_order_fn(user_id=user_id, token=token, amount=amt)

        # 确保 payment（只创建一次，或读取已有）
        ensure_fn = _svc_ensure_payment()
        order = ensure_fn(order)

        pay_link = _order_pay_url_from_obj(order)
        addr = getattr(order, "pay_address", None)

        if addr and qrcode is not None:
            qr = _build_qr_bytes(addr)
            caption = _build_tutorial_caption(order, lang, pay_link)
            if qr:
                try:
                    await cb.message.answer_photo(
                        photo=BufferedInputFile(qr.getvalue(), filename=f"qr_{getattr(order, 'id', 0)}.png"),
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=_invoice_kb_with_url(getattr(order, "id", 0), lang, pay_link),
                    )
                    await cb.answer()
                    return
                except TelegramBadRequest:
                    pass

        text = _order_card_text(order, lang, pay_link=pay_link)
        try:
            await cb.message.edit_text(
                text, parse_mode="HTML",
                reply_markup=recharge_order_kb(getattr(order, "id", 0), lang)
            )
        except TelegramBadRequest:
            await cb.message.answer(
                text, parse_mode="HTML",
                reply_markup=recharge_order_kb(getattr(order, "id", 0), lang)
            )
        await cb.answer()
    finally:
        # ===== 停止并删除“加载中” =====
        try:
            await stop_loader()
        except Exception:
            pass

# =============== 自定义金额入口 ===============
@router.callback_query(F.data == "recharge:amt:custom")
async def recharge_amount_custom(cb: CallbackQuery):
    user_id = cb.from_user.id
    lang = _db_lang_or_fallback(user_id, cb.from_user)
    _AWAITING_AMOUNT[user_id] = True
    await cb.message.answer(_tt("recharge.input_custom", lang) or "请输入金额（最多两位小数）：")
    await cb.answer()

# =============== 自定义金额文本 ===============
@router.message(F.text.regexp(r"^\d+(\.\d{1,2})?$"))
async def on_custom_amount_text(msg: Message):
    user_id = msg.from_user.id
    if not _AWAITING_AMOUNT.get(user_id):
        return

    lang = _db_lang_or_fallback(user_id, msg.from_user)
    raw = (msg.text or "").strip()
    try:
        q = Decimal(raw).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if q <= 0:
            raise InvalidOperation()
    except InvalidOperation:
        await msg.answer(_tt("recharge.invalid_amount", lang) or "金额格式不正确，请重新输入。")
        return

    token = _PENDING_TOKEN.get(user_id) or "USDT"
    _AWAITING_AMOUNT.pop(user_id, None)

    # ===== 启动假进度条 =====
    stop_loader = await _start_fake_loader(msg, lang)

    try:
        new_order_fn = _svc_new_order()
        order = new_order_fn(user_id=user_id, token=token, amount=q)

        ensure_fn = _svc_ensure_payment()
        order = ensure_fn(order)

        pay_link = _order_pay_url_from_obj(order)
        addr = getattr(order, "pay_address", None)

        if addr and qrcode is not None:
            qr = _build_qr_bytes(addr)
            caption = _build_tutorial_caption(order, lang, pay_link)
            if qr:
                try:
                    # 1) on_custom_amount_text 内：
                    await msg.answer_photo(
                        photo=BufferedInputFile(qr.getvalue(), filename=f"qr_{getattr(order, 'id', 0)}.png"),
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=_invoice_kb_with_url(getattr(order, 'id', 0), lang, pay_link),
                    )

                    return
                except TelegramBadRequest:
                    pass

        text = _order_card_text(order, lang, pay_link=pay_link)
        await msg.answer(
            text, parse_mode="HTML",
            reply_markup=recharge_order_kb(getattr(order, "id", 0), lang)
        )
    finally:
        # ===== 停止并删除“加载中” =====
        try:
            await stop_loader()
        except Exception:
            pass


# =============== 刷新状态 ===============
@router.callback_query(F.data.regexp(r"^recharge:refresh:(\d+)$"))
async def recharge_refresh(cb: CallbackQuery):
    user_id = cb.from_user.id
    lang = _db_lang_or_fallback(user_id, cb.from_user)

    m = re.match(r"^recharge:refresh:(\d+)$", cb.data or "")
    oid = int(m.group(1)) if m else 0

    get_order_fn = _svc_get_order()
    order = get_order_fn(oid)
    if not order:
        await cb.answer(_tt_first(["errors.not_found", "common.not_found"], lang) or "Not found")
        return

    refresh_fn = _svc_refresh()
    order = refresh_fn(order)

    # 若仍 PENDING 但还没写入 payment 关键字段（历史订单、边界情况），补一次 ensure_payment（不重复创建）
    if str(getattr(order, "status", "")).upper() == "PENDING" and not getattr(order, "payment_url", None):
        ensure_fn = _svc_ensure_payment()
        order = ensure_fn(order)

    pay_link = _order_pay_url_from_obj(order)
    st = str(getattr(order, "status", "PENDING")).lower()

    if st in ("success",) or str(getattr(order, "status", "")).upper() == "SUCCESS":
        ok_text = _tt_first(["recharge.success_text", "recharge.status.success"], lang) or "✅ 充值到账成功！"
        detail = []
        pam = getattr(order, "pay_amount", None)
        pccy = getattr(order, "pay_currency", None)
        if pam:
            detail.append(f"{_tt('recharge.invoice.amount', lang) or '金额'}：{pam} {pccy or ''}".strip())
        if getattr(order, "amount", None) and not pam:
            detail.append(f"{_tt('recharge.fields.amount', lang) or '金额'}：{_fmt_amt(order.amount)}")
        if detail:
            ok_text = ok_text + "\n" + "\n".join(detail)
        try:
            await cb.message.edit_text(ok_text, parse_mode="HTML", reply_markup=back_home_kb(lang))
        except TelegramBadRequest:
            await cb.message.answer(ok_text, parse_mode="HTML", reply_markup=back_home_kb(lang))
        await cb.answer()
        return

    addr = getattr(order, "pay_address", None)
    if addr and qrcode is not None:
        qr = _build_qr_bytes(addr)
        caption = _build_tutorial_caption(order, lang, pay_link)
        if qr:
            try:
                await cb.message.answer_photo(
                    photo=BufferedInputFile(qr.getvalue(), filename=f"qr_{getattr(order, 'id', 0)}.png"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=_invoice_kb_with_url(getattr(order, 'id', 0), lang, pay_link),
                )
                await cb.answer()
                return
            except TelegramBadRequest:
                pass

    text = _order_card_text(order, lang, pay_link=pay_link)
    try:
        await cb.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=recharge_order_kb(getattr(order, "id", 0), lang)
        )
    except TelegramBadRequest:
        await cb.message.answer(
            text, parse_mode="HTML",
            reply_markup=recharge_order_kb(getattr(order, "id", 0), lang)
        )
    await cb.answer()

# =============== 复制地址 ===============
@router.callback_query(F.data.regexp(r"^recharge:copy_addr:(\d+)$"))
async def recharge_copy_address(cb: CallbackQuery):
    user_id = cb.from_user.id
    lang = _db_lang_or_fallback(user_id, cb.from_user)
    m = re.match(r"^recharge:copy_addr:(\d+)$", cb.data or "")
    oid = int(m.group(1)) if m else 0

    get_order_fn = _svc_get_order()
    order = get_order_fn(oid)
    if not order:
        await cb.answer(_tt_first(["errors.not_found", "common.not_found"], lang) or "Not found", show_alert=True)
        return

    addr = (getattr(order, "pay_address", None) or "").strip()
    if not addr:
        await cb.answer(_tt_first(["recharge.addr_missing", "common.not_found"], lang) or "No address", show_alert=True)
        return

    await cb.message.answer(addr)
    await cb.answer(_tt("common.copied_tip", lang) or "已发送可复制内容")

# =============== 复制金额 ===============
@router.callback_query(F.data.regexp(r'^recharge:copy_amt:(\d+)$'))
async def recharge_copy_amount(cb: CallbackQuery):
    user_id = cb.from_user.id
    lang = _db_lang_or_fallback(user_id, cb.from_user)

    m = re.match(r'^recharge:copy_amt:(\d+)$', cb.data or '')
    oid = int(m.group(1)) if m else 0

    get_order_fn = _svc_get_order()
    order = get_order_fn(oid)
    if not order:
        await cb.answer(_tt_first(['errors.not_found', 'common.not_found'], lang) or 'Not found', show_alert=True)
        return

    # 优先取 pay_amount（即使为 0 也视为有效）
    pam = getattr(order, 'pay_amount', None)
    pccy = getattr(order, 'pay_currency', None)

    if pam is not None:
        txt = f"{_fmt_amt(pam)} {pccy or ''}".strip()
    else:
        token = getattr(order, 'token', 'USDT') or 'USDT'
        base_amount = getattr(order, 'amount', 0)
        txt = f"{_fmt_amt(base_amount)} {token}".strip()

    await cb.message.answer(txt)
    await cb.answer(_tt('common.copied_tip', lang) or '已发送可复制内容')

