# routers/admin_adjust.py
# -*- coding: utf-8 -*-
"""
管理员余额调整面板（USDT / TON / POINT）
入口：
  • 管理面板按钮：callback -> "admin:adjust"
  • 文本命令：/adjust  [可选：直接跟一个用户ID或@username，或多个：123,@alice 456]

新增能力（在你上传版本基础上扩展）：
  1) 批量目标：一次输入多个 user_id 与 @username，混合也行，分隔符支持空格/逗号/分号/换行
  2) 批量解析预览：展示成功解析的数量与无法解析的目标，确认后逐条执行
  3) 单个 @username 直接支持
  4) 扣减前余额预校验；对 INSUFFICIENT_BALANCE 给出明确提示
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Tuple, Union, List, Iterable

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ForceReply
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from core.i18n.i18n import t, i18n
from config.settings import is_admin as _is_admin
from models.db import get_session
from models.user import User, get_or_create_user, update_balance, get_balance
from models.ledger import add_ledger_entry, LedgerType, Ledger
from sqlalchemy import func

router = Router()
log = logging.getLogger("admin_adjust")

# ---------- 工具 ----------
def _canon_lang(code: Optional[str]) -> str:
    if not code:
        return "zh"
    c = str(code).strip().lower().replace("_", "-")
    if not c:
        return "zh"
    try:
        available = set(i18n.available_languages() or [])
    except Exception:
        available = set()
    available |= {"zh", "en", "fr", "de", "es", "hi", "vi", "th"}
    if c in available:
        return c
    p = c.split("-", 1)[0]
    if p in available:
        return p
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"
    return "zh"

def _db_lang_or_fallback(uid: int, fallback_user) -> str:
    try:
        with get_session() as s:
            u = s.query(User).filter_by(tg_id=uid).first()
            if u and getattr(u, "language", None):
                return _canon_lang(u.language)
    except Exception as e:
        log.exception("read user language failed(uid=%s): %s", uid, e)
    return _canon_lang(getattr(fallback_user, "language_code", None))

def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _btn(text: str | None, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=(text or ""), callback_data=data)

def _is_admin_uid(uid: int) -> bool:
    try:
        return bool(_is_admin(uid))
    except Exception:
        return False

def _normalize_amount_for_token(token: str, s: str) -> Optional[Decimal]:
    token = token.upper()
    s = (s or "").strip()
    try:
        if token in ("USDT", "TON"):
            if not re.fullmatch(r"-?\d+(\.\d{1,6})?", s):
                return None
            return Decimal(s)
        elif token == "POINT":
            if not re.fullmatch(r"-?\d+", s):
                return None
            return Decimal(int(s))
    except (InvalidOperation, ValueError):
        return None
    return None

def _get_wallets_for_user_id(user_tg_id: int) -> Dict[str, Decimal]:
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_tg_id).first()
        usdt = Decimal(getattr(u, "usdt_balance", 0) or 0)
        ton  = Decimal(getattr(u, "ton_balance", 0) or 0)
        pt   = Decimal(getattr(u, "point_balance", 0) or 0)
        if usdt or ton or pt:
            return {"USDT": usdt, "TON": ton, "POINT": pt}
        totals = dict(
            s.query(Ledger.token, func.sum(Ledger.amount))
             .filter(Ledger.user_tg_id == int(user_tg_id))
             .group_by(Ledger.token)
             .all()
        )
        return {
            "USDT": Decimal(totals.get("USDT") or 0),
            "TON":  Decimal(totals.get("TON")  or 0),
            "POINT":Decimal(totals.get("POINT")or 0),
        }

def _format_wallets_for_text(w: Dict[str, Decimal], lang: str) -> str:
    def f2(x: Decimal) -> str:
        try:
            return f"{float(x):.2f}"
        except Exception:
            return str(x)
    usdt_label = t("labels.usdt", lang) or "USDT"
    ton_label  = t("labels.ton", lang) or "TON"
    pt_label   = t("labels.point", lang) or "POINT"
    return f"{usdt_label}: {f2(w.get('USDT', Decimal(0)))}\n{ton_label}: {f2(w.get('TON', Decimal(0)))}\n{pt_label}: {int(w.get('POINT', Decimal(0)))}"

def _split_targets(text: str) -> List[str]:
    # 用空格/逗号/分号/换行拆分
    items = re.split(r"[\s,;]+", (text or "").strip())
    return [x for x in items if x]

def _is_username(x: str) -> bool:
    return x.startswith("@") and len(x) > 1

def _is_id(x: str) -> bool:
    return bool(re.fullmatch(r"\d{5,20}", x))

def _limit_check(items: Iterable[str], limit: int = 200) -> bool:
    return len(list(items)) <= limit

# ---------- FSM ----------
class AdjStates(StatesGroup):
    USER = State()      # 目标输入（支持批量）
    TOKEN = State()
    AMOUNT = State()
    MEMO = State()
    CONFIRM = State()

@dataclass
class AdjustCtx:
    # 批量：解析成功的用户 ID 列表
    target_ids: List[int] = field(default_factory=list)
    # 解析失败原始目标
    unresolved: List[str] = field(default_factory=list)
    token: str = "USDT"
    amount: Decimal = Decimal(0)
    memo: str = ""

# ---------- 入口 ----------
@router.callback_query(F.data == "admin:adjust")
async def admin_adjust_entry(cb: CallbackQuery, state: FSMContext):
    if not _is_admin_uid(cb.from_user.id):
        lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
        await cb.answer(t("admin.no_permission", lang), show_alert=False)
        return

    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    await state.clear()
    await state.set_state(AdjStates.USER)

    tip = t("admin.adjust.ask_user", lang) or "请输入目标用户：发送 用户ID 或 @用户名；支持多个，空格/逗号/分号/换行分隔。"
    try:
        await cb.message.edit_text(
            tip, parse_mode="HTML",
            reply_markup=_kb([[ _btn(t("menu.back", lang), "menu:admin") ]])
        )
    except TelegramBadRequest:
        await cb.message.answer(
            tip, parse_mode="HTML",
            reply_markup=_kb([[ _btn(t("menu.back", lang), "menu:admin") ]])
        )

@router.message(Command("adjust"))
async def admin_adjust_cmd(msg: Message, state: FSMContext):
    if not _is_admin_uid(msg.from_user.id):
        lang = _db_lang_or_fallback(msg.from_user.id, msg.from_user)
        await msg.answer(t("admin.no_permission", lang))
        return

    lang = _db_lang_or_fallback(msg.from_user.id, msg.from_user)
    await state.clear()
    await state.set_state(AdjStates.USER)

    tip = (t("admin.adjust.ask_user", lang)
           or "请输入目标用户：发送 用户ID 或 @用户名；支持多个，空格/逗号/分号/换行分隔。")
    await msg.answer(
        tip,
        parse_mode="HTML",
        reply_markup=ForceReply(selective=True, input_field_placeholder="@alice 12345 @bob")
    )

# ---------- 解析目标 ----------
async def _resolve_targets(raw: str, lang: str, bot) -> Tuple[List[int], List[str]]:
    """
    返回：成功解析的 tg_id 列表，未解析的原始目标列表
    策略：
      1) 数字直接当 tg_id
      2) @username：
         - 先查本地 User.username（大小写不敏感）
         - 找不到再尝试 bot.get_chat(@username) 获取 id
    """
    pieces = _split_targets(raw)
    if not pieces:
        return [], []
    # 去重保持顺序
    seen = set()
    unique = []
    for p in pieces:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)

    if not _limit_check(unique, 200):
        # 你非要一次塞一车，我也只能给你拦住
        unique = unique[:200]

    ok: List[int] = []
    bad: List[str] = []

    with get_session() as s:
        for item in unique:
            if _is_id(item):
                ok.append(int(item))
                continue
            if _is_username(item):
                uname = item[1:].strip().lower()
                # 先查 DB
                u = s.query(User).filter(func.lower(User.username) == uname).first()
                if u and u.tg_id:
                    ok.append(int(u.tg_id))
                    continue
                # 再查 Telegram
                try:
                    chat = await bot.get_chat(item)
                    if chat and getattr(chat, "id", None):
                        ok.append(int(chat.id))
                        continue
                except Exception as e:
                    log.info("resolve username via bot failed: %s -> %s", item, e)
                bad.append(item)
                continue
            # 既不是 id 也不是 @username
            bad.append(item)

    return ok, bad

async def _go_token_step(msg_or_cb, state: FSMContext, lang: str):
    await state.set_state(AdjStates.TOKEN)
    kb = _kb([
        [ _btn(t("env.mode.usdt", lang), "adj:token:USDT"),
          _btn(t("env.mode.ton", lang), "adj:token:TON"),
          _btn(t("env.mode.point", lang), "adj:token:POINT") ],
        [ _btn(t("menu.back", lang), "menu:admin") ]
    ])
    tip = t("admin.adjust.choose_asset", lang) or "选择需要调整的资产："
    if isinstance(msg_or_cb, Message):
        await msg_or_cb.answer(tip, reply_markup=kb)
    else:
        try:
            await msg_or_cb.message.edit_text(tip, reply_markup=kb)
        except TelegramBadRequest:
            await msg_or_cb.message.answer(tip, reply_markup=kb)

# ---------- 选择用户（普通消息/回复） ----------
@router.message(StateFilter(AdjStates.USER), F.text)
async def adj_pick_user(msg: Message, state: FSMContext):
    if not _is_admin_uid(msg.from_user.id):
        lang = _db_lang_or_fallback(msg.from_user.id, msg.from_user)
        await msg.answer(t("admin.no_permission", lang))
        return

    lang = _db_lang_or_fallback(msg.from_user.id, msg.from_user)
    raw_text = (msg.text or "")
    ok_ids, bad_items = await _resolve_targets(raw_text, lang, msg.bot)

    if not ok_ids:
        # 全挂
        tip = t("admin.adjust.errors.bad_user", lang) or "目标无效，请发送用户ID或@用户名。"
        if bad_items:
            tip += "\n\n未识别：\n" + "\n".join(bad_items[:10])
        await msg.answer(tip)
        return

    # 预览
    await state.update_data(target_ids=ok_ids, unresolved=bad_items)
    await _go_token_step(msg, state, lang)

@router.message(StateFilter(AdjStates.USER), F.text, F.reply_to_message, F.reply_to_message.from_user.is_bot)
async def adj_pick_user_reply(msg: Message, state: FSMContext):
    return await adj_pick_user(msg, state)

# ---------- 选择资产 ----------
@router.callback_query(StateFilter(AdjStates.TOKEN), F.data.startswith("adj:token:"))
async def adj_choose_token(cb: CallbackQuery, state: FSMContext):
    if not _is_admin_uid(cb.from_user.id):
        lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
        await cb.answer(t("admin.no_permission", lang), show_alert=False)
        return

    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    _, _, token = cb.data.partition("adj:token:")
    token = (token or "USDT").upper()
    await state.update_data(token=token)
    await state.set_state(AdjStates.AMOUNT)

    # 金额输入提示
    if token == "POINT":
        tip = t("admin.adjust.amount.ask_point", lang) or "请输入积分调整数量（整数，可为负表示扣减）："
        await cb.message.edit_text(
            tip,
            reply_markup=_kb([[ _btn(t("menu.back", lang), "admin:adjust") ]]),
            parse_mode="HTML"
        )
    else:
        hint = t("admin.adjust.amount.hint_fx", lang) or "示例：10.5 或 -2.3（最多 6 位小数）"
        ask  = t("admin.adjust.amount.ask_fx", lang) or "请输入金额（正数加钱，负数扣减）："
        tip = ask + f"\n<code>{hint}</code>"
        await cb.message.edit_text(
            tip,
            reply_markup=_kb([[ _btn(t("menu.back", lang), "admin:adjust") ]]),
            parse_mode="HTML"
        )

# ---------- 输入金额 ----------
@router.message(StateFilter(AdjStates.AMOUNT), F.text)
async def adj_amount_input(msg: Message, state: FSMContext):
    if not _is_admin_uid(msg.from_user.id):
        lang = _db_lang_or_fallback(msg.from_user.id, msg.from_user)
        await msg.answer(t("admin.no_permission", lang))
        return

    lang = _db_lang_or_fallback(msg.from_user.id, msg.from_user)
    data = await state.get_data()
    token = (data.get("token") or "USDT").upper()
    amt = _normalize_amount_for_token(token, msg.text or "")

    if amt is None:
        await msg.answer(t("admin.adjust.errors.bad_amount", lang) or "金额格式不合法。")
        return
    if amt == 0:
        await msg.answer(t("admin.adjust.errors.zero", lang) or "金额不能为 0。")
        return

    await state.update_data(amount=str(amt))
    await state.set_state(AdjStates.MEMO)

    tip = t("admin.adjust.memo.ask", lang) or "请输入备注（可输入“跳过/skip”跳过）："
    await msg.answer(tip, parse_mode="HTML")

# ---------- 备注 & 确认 ----------
@router.message(StateFilter(AdjStates.MEMO), F.text)
async def adj_memo_and_confirm(msg: Message, state: FSMContext):
    if not _is_admin_uid(msg.from_user.id):
        lang = _db_lang_or_fallback(msg.from_user.id, msg.from_user)
        await msg.answer(t("admin.no_permission", lang))
        return

    lang = _db_lang_or_fallback(msg.from_user.id, msg.from_user)
    data = await state.get_data()
    token = (data.get("token") or "USDT").upper()
    amount = _normalize_amount_for_token(token, str(data.get("amount")))
    memo_raw = msg.text or ""
    memo = "" if memo_raw.lower() in ("跳过", "skip") else memo_raw

    await state.update_data(memo=memo)
    await state.set_state(AdjStates.CONFIRM)

    target_ids: List[int] = list(map(int, data.get("target_ids") or []))
    unresolved: List[str] = list(data.get("unresolved") or [])

    # 构造确认页
    head = t("admin.adjust.confirm.title", lang) or "请确认以下调整："
    asset_line = t("admin.adjust.confirm.asset", lang, token=(t(f"labels.{token.lower()}", lang) or token)) \
                 or f"资产：{token}"
    amount_line = t("admin.adjust.confirm.amount", lang, amount=str(amount)) \
                  or f"金额：{amount}"

    lines = [head, asset_line, amount_line, "────────────────"]

    if len(target_ids) == 1:
        # 单人展示用户名
        try:
            with get_session() as s:
                u = s.query(User).filter_by(tg_id=target_ids[0]).first()
                uname = f"@{u.username}" if (u and u.username) else str(target_ids[0])
        except Exception:
            uname = str(target_ids[0])
        lines.insert(1, t("admin.adjust.confirm.user", lang, user=uname) or f"用户：{uname}")
    else:
        lines.append(f"目标用户：{len(target_ids)} 人")
        sample = ", ".join(map(str, target_ids[:5]))
        lines.append(f"样例：{sample}{' 等' if len(target_ids) > 5 else ''}")

    if unresolved:
        lines.append("未解析：")
        lines.append("\n".join(unresolved[:6]) + ("…" if len(unresolved) > 6 else ""))

    if memo:
        lines.append("────────────────")
        lines.append(t("admin.adjust.confirm.memo", lang, memo=memo) or f"备注：{memo}")

    kb = _kb([
        [ _btn(t("admin.adjust.confirm.ok", lang) or "确认执行", "adj:confirm"),
          _btn(t("admin.adjust.confirm.cancel", lang) or "取消", "adj:cancel") ]
    ])
    await msg.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")

# ---------- 取消 ----------
@router.callback_query(StateFilter(AdjStates.CONFIRM), F.data == "adj:cancel")
async def adj_cancel(cb: CallbackQuery, state: FSMContext):
    if not _is_admin_uid(cb.from_user.id):
        lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
        await cb.answer(t("admin.no_permission", lang), show_alert=False)
        return
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    tip = t("admin.adjust.cancelled", lang) or "已取消。"
    try:
        await cb.message.edit_text(tip, reply_markup=_kb([[ _btn(t("menu.back", lang) or "返回", "menu:admin") ]]))
    except TelegramBadRequest:
        await cb.message.answer(tip, reply_markup=_kb([[ _btn(t("menu.back", lang) or "返回", "menu:admin") ]]))

# ---------- 确认执行 ----------
@router.callback_query(StateFilter(AdjStates.CONFIRM), F.data == "adj:confirm")
async def adj_do_confirm(cb: CallbackQuery, state: FSMContext):
    if not _is_admin_uid(cb.from_user.id):
        lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
        await cb.answer(t("admin.no_permission", lang), show_alert=False)
        return

    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    data = await state.get_data()
    try:
        target_ids = list(map(int, data.get("target_ids") or []))
        token  = (data.get("token") or "USDT").upper()
        amount = Decimal(str(data.get("amount") or "0"))
        memo   = data.get("memo") or ""
    except Exception as e:
        log.exception("adj_do_confirm parse ctx failed: %s", e)
        await cb.answer(t("admin.adjust.fail", lang) or "操作失败。", show_alert=True)
        return

    if not target_ids:
        await cb.answer(t("admin.adjust.fail", lang) or "操作失败。", show_alert=True)
        return

    successes: List[str] = []
    failures: List[str] = []

    for uid in target_ids:
        # 扣减预校验
        try:
            if amount < 0:
                with get_session() as s:
                    bal = get_balance(s, uid, token)
                bal_dec = Decimal(str(bal))
                if bal_dec + amount < 0:
                    failures.append(f"{uid} | 余额不足")
                    continue
        except Exception as e:
            log.warning("precheck balance failed(uid=%s, token=%s): %s", uid, token, e)

        try:
            with get_session() as s:
                u = s.query(User).filter_by(tg_id=uid).first()
                if not u:
                    u = get_or_create_user(s, tg_id=uid)

                try:
                    update_balance(s, u, token=token, delta=amount)
                except ValueError as ve:
                    if str(ve) == "INSUFFICIENT_BALANCE":
                        failures.append(f"{uid} | 余额不足")
                        continue
                    raise

                add_ledger_entry(
                    s,
                    user_tg_id=uid,
                    token=token,
                    amount=amount,
                    ltype=LedgerType.ADJUSTMENT,
                    note=memo or ""
                )
                s.commit()
                successes.append(str(uid))
        except Exception as e:
            log.exception("adjust failed uid=%s: %s", uid, e)
            failures.append(f"{uid} | {type(e).__name__}")

    await state.clear()

    # 构造汇总文本
    ok_head = t("admin.adjust.success", lang) or "操作完成。"
    summary = [
        ok_head,
        f"成功：{len(successes)}",
        f"失败：{len(failures)}",
    ]
    if successes:
        sample = ", ".join(successes[:10])
        summary.append(f"成功样例：{sample}{'…' if len(successes) > 10 else ''}")
    if failures:
        summary.append("失败列表（前 10 项）：")
        summary.append("\n".join(failures[:10]) + ("…" if len(failures) > 10 else ""))

    # 如果只有 1 人，回显余额
    tail = ""
    if len(target_ids) == 1 and len(successes) == 1:
        try:
            wallets = _get_wallets_for_user_id(int(successes[0]))
            balance_text = _format_wallets_for_text(wallets, lang)
            tail = f"\n\n{t('admin.adjust.balance_after', lang) or '调整后余额：'}\n{balance_text}"
        except Exception as e:
            log.exception("read wallets after adjust failed: %s", e)

    text = "\n".join(summary) + tail

    try:
        await cb.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_kb([[ _btn(t("menu.back", lang) or "返回", "menu:admin") ]])
        )
    except TelegramBadRequest:
        await cb.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=_kb([[ _btn(t("menu.back", lang) or "返回", "menu:admin") ]])
        )
