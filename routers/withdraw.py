# routers/withdraw.py
# -*- coding: utf-8 -*-
"""
提现向导（USDT / TON）
流程：
- withdraw:main                → 选择币种
- withdraw:token:{USDT|TON}    → 选择币种后，进入金额输入
- [文本输入金额]               → 校验金额与余额、展示手续费提示，进入地址输入
- [文本输入地址]               → 基础校验，进入确认页
- withdraw:confirm / :cancel   → 确认提交（扣款 + 记账）/ 取消

说明：
- 手续费默认读取 feature_flags，可选项（若没配置则使用本模块默认值）。
- 扣减策略：实际扣减 = 提现金额 + 手续费（用户到手 = 提现金额）。
- 这里只做账务扣减与记录，链上转账请接入你的后端/运营流程。
"""

from __future__ import annotations
import re
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from typing import Optional, Tuple

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from core.i18n.i18n import t
from core.utils.keyboards import back_home_kb
from config.feature_flags import flags
from models.db import get_session
from models.user import User, get_or_create_user, update_balance
from models.ledger import add_ledger_entry, LedgerType

router = Router()

# ====== 可配置的最小额与手续费（可被 feature_flags 覆盖） ======
_MIN = {
    "USDT": Decimal(str(flags.get("WITHDRAW_MIN_USDT", 1.0))),
    "TON":  Decimal(str(flags.get("WITHDRAW_MIN_TON", 1.0))),
}
_FEE = {
    "USDT": Decimal(str(flags.get("WITHDRAW_FEE_USDT", 0.5))),
    "TON":  Decimal(str(flags.get("WITHDRAW_FEE_TON", 0.02))),
}

_DEC6 = Decimal("0.000001")
def _q6(x: Decimal | float | int) -> Decimal:
    return Decimal(str(x)).quantize(_DEC6, rounding=ROUND_DOWN)


# ====== FSM ======
class WDStates(StatesGroup):
    TOKEN = State()
    AMOUNT = State()
    ADDRESS = State()
    CONFIRM = State()


# ====== 语言 ======
def _canon_lang(code: Optional[str]) -> str:
    if not code:
        return "zh"
    c = str(code).strip().lower()
    if c.startswith("zh"): return "zh"
    if c.startswith("en"): return "en"
    return "zh"

def _user_lang(user_id: int, fallback_user) -> str:
    from models.user import User  # 延迟导入避免循环
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u and getattr(u, "language", None):
            return _canon_lang(u.language)
    return _canon_lang(getattr(fallback_user, "language_code", None))


# ====== 本模块内键盘 ======
def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text or "", callback_data=data)

def _token_kb(lang: str) -> InlineKeyboardMarkup:
    return _kb([
        [
            _btn(t("asset.usdt", lang) or "USDT", "withdraw:token:USDT"),
            _btn(t("asset.ton",  lang) or "TON",  "withdraw:token:TON"),
        ],
        [ _btn(t("menu.back", lang) or "⬅️ 返回", "menu:main") ],
    ])

def _back_to_token_kb(lang: str) -> InlineKeyboardMarkup:
    # 修复：这里原来多了一个 ']'，导致语法错误
    return _kb([[ _btn(t("menu.back", lang) or "⬅️ 返回", "withdraw:main") ]])

def _confirm_kb(lang: str) -> InlineKeyboardMarkup:
    return _kb([
        [ _btn(t("withdraw.confirm", lang) or "✅ 确认提现", "withdraw:confirm") ],
        [ _btn(t("withdraw.cancel", lang) or "✖️ 取消", "withdraw:cancel") ],
    ])


# ====== 工具 ======
def _parse_amount(text: str) -> Optional[Decimal]:
    try:
        d = Decimal(str(text).strip())
        if d <= 0:
            return None
        return d
    except Exception:
        return None

def _addr_ok(token: str, address: str) -> bool:
    s = (address or "").strip()
    if len(s) < 10:
        return False
    # 可选的简易规则（避免误判）：USDT/TON 均仅要求非空与长度
    return True

@dataclass
class WDData:
    token: str
    amount: Decimal
    fee: Decimal
    address: str

def _user_balances(user_id: int) -> Tuple[Decimal, Decimal]:
    """返回 (USDT, TON) 余额"""
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first() or get_or_create_user(s, tg_id=user_id)
        usdt = Decimal(str(u.usdt_balance or 0))
        ton  = Decimal(str(u.ton_balance  or 0))
    return usdt, ton


# ====== 入口：/withdraw & withdraw:main ======
@router.message(F.text.regexp(r"^/withdraw$"))
async def cmd_withdraw(msg: Message, state: FSMContext):
    lang = _user_lang(msg.from_user.id, msg.from_user)
    await state.clear()
    await state.set_state(WDStates.TOKEN)
    title = t("withdraw.title", lang) or "🏧 提现中心"
    tip   = t("withdraw.choose_token", lang) or "请选择提现币种"
    await msg.answer(f"{title}\n\n{tip}", parse_mode="HTML", reply_markup=_token_kb(lang))

@router.callback_query(F.data == "withdraw:main")
async def withdraw_main(cb: CallbackQuery, state: FSMContext):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    await state.clear()
    await state.set_state(WDStates.TOKEN)
    title = t("withdraw.title", lang) or "🏧 提现中心"
    tip   = t("withdraw.choose_token", lang) or "请选择提现币种"
    try:
        await cb.message.edit_text(f"{title}\n\n{tip}", parse_mode="HTML", reply_markup=_token_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(f"{title}\n\n{tip}", parse_mode="HTML", reply_markup=_token_kb(lang))
    await cb.answer()


# ====== 选择币种 → 金额 ======
@router.callback_query(F.data.regexp(r"^withdraw:token:(USDT|TON)$"))
async def choose_token(cb: CallbackQuery, state: FSMContext):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    m = re.match(r"^withdraw:token:(USDT|TON)$", cb.data or "")
    token = m.group(1) if m else "USDT"

    await state.update_data(token=token)
    await state.set_state(WDStates.AMOUNT)

    min_amt = _MIN[token]
    fee     = _FEE[token]
    usdt, ton = _user_balances(cb.from_user.id)
    bal = usdt if token == "USDT" else ton

    lines = [
        t("withdraw.amount.ask", lang) or "请输入提现金额：",
        t("withdraw.amount.min", lang, token=token, amount=f"{_q6(min_amt):.6f}") or f"• 最低 {token} 提现额：{_q6(min_amt):.6f}",
        t("withdraw.amount.fee", lang, token=token, fee=f"{_q6(fee):.6f}") or f"• 手续费：{_q6(fee):.6f} {token}（按笔）",
        t("withdraw.balance", lang, token=token, balance=f"{_q6(bal):.6f}") or f"• 当前余额：{_q6(bal):.6f} {token}",
    ]
    text = "\n".join(lines)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_back_to_token_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=_back_to_token_kb(lang))
    await cb.answer()


# ====== 输入金额 → 地址 ======
@router.message(WDStates.AMOUNT)
async def input_amount(msg: Message, state: FSMContext):
    lang = _user_lang(msg.from_user.id, msg.from_user)
    data = await state.get_data()
    token = (data.get("token") or "USDT").upper()

    amt = _parse_amount(msg.text or "")
    if amt is None:
        await msg.answer(t("withdraw.errors.invalid_amount", lang) or "❌ 金额无效，请重新输入", reply_markup=_back_to_token_kb(lang))
        return

    amt = _q6(amt)
    if amt < _MIN[token]:
        await msg.answer(t("withdraw.errors.less_than_min", lang, token=token, amount=f"{_q6(_MIN[token]):.6f}") or "❌ 金额低于最低限额", reply_markup=_back_to_token_kb(lang))
        return

    fee = _FEE[token]
    total_deduct = amt + fee

    usdt, ton = _user_balances(msg.from_user.id)
    bal = usdt if token == "USDT" else ton
    if bal < total_deduct:
        await msg.answer(t("withdraw.errors.insufficient", lang) or "💸 余额不足，请减少金额或先充值", reply_markup=_back_to_token_kb(lang))
        return

    await state.update_data(amount=str(amt), fee=str(fee))
    await state.set_state(WDStates.ADDRESS)

    tip = t("withdraw.address.ask", lang, token=token) or f"请输入 {token} 收款地址："
    await msg.answer(tip, reply_markup=_back_to_token_kb(lang))


# ====== 输入地址 → 确认 ======
@router.message(WDStates.ADDRESS)
async def input_address(msg: Message, state: FSMContext):
    lang = _user_lang(msg.from_user.id, msg.from_user)
    data = await state.get_data()
    token = (data.get("token") or "USDT").upper()
    amt = Decimal(str(data.get("amount") or "0"))
    fee = Decimal(str(data.get("fee") or "0"))

    addr = (msg.text or "").strip()
    if not _addr_ok(token, addr):
        await msg.answer(t("withdraw.errors.bad_address", lang) or "❌ 地址格式不正确，请重新输入", reply_markup=_back_to_token_kb(lang))
        return

    await state.update_data(address=addr)
    await state.set_state(WDStates.CONFIRM)

    lines = [
        t("withdraw.confirm_page.title", lang) or "请确认提现信息",
        "────────────────",
        t("withdraw.confirm_page.token", lang, token=token) or f"• 币种：{token}",
        t("withdraw.confirm_page.amount", lang, amount=f"{amt:.6f}") or f"• 提现金额：{amt:.6f}",
        t("withdraw.confirm_page.fee", lang, fee=f"{fee:.6f}") or f"• 手续费：{fee:.6f}",
        t("withdraw.confirm_page.total", lang, total=f"{(amt+fee):.6f}") or f"• 扣减合计：{(amt+fee):.6f}",
        t("withdraw.confirm_page.address", lang, address=addr) or f"• 地址：{addr}",
    ]
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=_confirm_kb(lang))


# ====== 取消 ======
@router.callback_query(F.data == "withdraw:cancel")
async def wd_cancel(cb: CallbackQuery, state: FSMContext):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    await state.clear()
    tip = t("withdraw.cancelled", lang) or "已取消操作。"
    try:
        await cb.message.edit_text(tip, reply_markup=back_home_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(tip, reply_markup=back_home_kb(lang))
    await cb.answer()


# ====== 确认提交（扣款 + 记账） ======
@router.callback_query(F.data == "withdraw:confirm")
async def wd_confirm(cb: CallbackQuery, state: FSMContext):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    data = await state.get_data()
    token = (data.get("token") or "USDT").upper()
    amt = Decimal(str(data.get("amount") or "0"))
    fee = Decimal(str(data.get("fee") or "0"))
    addr = str(data.get("address") or "")

    total = amt + fee
    # 再次校验余额
    usdt, ton = _user_balances(cb.from_user.id)
    bal = usdt if token == "USDT" else ton
    if bal < total:
        await cb.answer(t("withdraw.errors.insufficient", lang) or "余额不足，请重试", show_alert=True)
        return

    # 扣减余额 + 记账（同一事务）
    try:
        with get_session() as s:
            u = s.query(User).filter_by(tg_id=cb.from_user.id).first() or get_or_create_user(s, tg_id=cb.from_user.id)
            update_balance(s, u, token, -total)
            add_ledger_entry(
                s,
                user_tg_id=cb.from_user.id,
                ltype=LedgerType.WITHDRAW,
                token=token,
                amount=-total,  # 负数 = 支出
                ref_type="WITHDRAW",
                ref_id=None,
                note=f"withdraw {amt} + fee {fee} → {addr}",
            )
            s.commit()
    except Exception:
        await cb.answer(t("withdraw.fail", lang) or "❌ 提现提交失败，请稍后重试", show_alert=True)
        return

    await state.clear()
    lines = [
        t("withdraw.success", lang) or "✅ 提现申请已提交",
        t("withdraw.success_detail", lang, token=token, amount=f"{amt:.6f}", fee=f"{fee:.6f}") or f"• 提现金额：{amt:.6f} {token}（手续费 {fee:.6f}）",
        t("withdraw.success_next", lang) or "我们将尽快处理链上转账，请耐心等待。",
    ]
    tip = "\n".join(lines)
    try:
        await cb.message.edit_text(tip, parse_mode="HTML", reply_markup=back_home_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(tip, parse_mode="HTML", reply_markup=back_home_kb(lang))
    await cb.answer()
