# routers/help.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from typing import Optional, List, Tuple

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter  # ✅ 新增
from core.i18n.i18n import t
from services.ai_helper import ai_answer

router = Router()
__all__ = ["router"]
log = logging.getLogger("help_router")

# 仅当用户明确进入“AI提问模式”且在私聊中时才允许 AI 回复
_AI_ACTIVE_USERS: set[int] = set()

# ---- 小工具 ----
def _kb(rows): 
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _btn(text, data): 
    return InlineKeyboardButton(text=(text or ""), callback_data=data)

def _canon_lang(code: Optional[str]) -> str:
    if not code:
        return "zh"
    c = str(code).lower()
    if c.startswith("en"): return "en"
    if c.startswith("zh"): return "zh"
    return "zh"

def _get_user_lang(user_id: int, fallback_code: Optional[str] = None) -> str:
    try:
        from models.db import get_session
        from models.user import User
        with get_session() as s:
            u = s.query(User).filter_by(tg_id=int(user_id)).first()
            if u and getattr(u, "language", None):
                return _canon_lang(getattr(u, "language"))
    except Exception:
        pass
    return _canon_lang(fallback_code)

# ---- 主页 & FAQ ----
@router.callback_query(F.data == "help:main")
async def help_main(cb: CallbackQuery):
    lang = _get_user_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None))
    title = t("help.title", lang)
    sub = t("help.subtitle", lang)
    ask = t("help.ask_me", lang)
    txt = f"<b>{title}</b>\n{sub}\n\n{ask}"
    kb = _kb([
        [_btn(t("help.faq.send", lang), "help:faq:send"),
         _btn(t("help.faq.grab", lang), "help:faq:grab")],
        [_btn(t("help.faq.recharge", lang), "help:faq:recharge"),
         _btn(t("help.faq.withdraw", lang), "help:faq:withdraw")],
        [_btn(t("help.faq.rules", lang), "help:faq:rules")],
        [_btn(t("help.ask_ai_btn", lang), "help:ask_ai")],
        [_btn(t("menu.back", lang), "menu:main")],
    ])
    try:
        await cb.message.answer(txt, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    finally:
        await cb.answer()

def _faq_text(key: str, lang: str) -> str:
    return t(key, lang)

@router.callback_query(F.data.regexp(r"^help:faq:(send|grab|recharge|withdraw|rules)$"))
async def help_faq(cb: CallbackQuery):
    lang = _get_user_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None))
    topic = (cb.data or "").split(":")[-1]
    txt = _faq_text(f"help.faq.{topic}", lang)
    kb = _kb([[ _btn(t("menu.back", lang), "help:main") ]])
    try:
        await cb.message.answer(txt, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    finally:
        await cb.answer()

# ---- 进入 / 退出 AI 模式 ----
@router.callback_query(F.data == "help:ask_ai")
async def help_ask_ai(cb: CallbackQuery):
    lang = _get_user_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None))
    _AI_ACTIVE_USERS.add(cb.from_user.id)
    tip = t("help.ask_me", lang)
    place = t("help.ask_placeholder", lang)
    extra = t("help.exit_ai_tip", lang)
    txt = f"{tip}\n\n<code>{place}</code>\n\n{extra}"
    kb = _kb([
        [_btn(t("help.exit_ai", lang), "help:exit_ai")],
        [_btn(t("menu.back", lang), "help:main")],
    ])
    try:
        await cb.message.answer(txt, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    finally:
        await cb.answer()

@router.callback_query(F.data == "help:exit_ai")
async def help_exit_ai(cb: CallbackQuery):
    lang = _get_user_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None))
    _AI_ACTIVE_USERS.discard(cb.from_user.id)
    try:
        await cb.message.answer(t("help.exit_ok", lang), reply_markup=_kb([[ _btn(t("menu.back", lang), "help:main") ]])) 
    finally:
        await cb.answer()

# ---- AI 文本消息（仅私聊 & AI 模式） ----
@router.message(F.text, StateFilter(None))  # ✅ 限制仅在没有 FSM 状态时生效
async def help_ai_message(msg: Message):
    # 非私聊直接忽略
    if str(getattr(msg.chat, "type", "")) in {"group", "supergroup"}:
        return
    # 仅当用户开启了 AI 模式
    uid = int(getattr(msg.from_user, "id", 0) or 0)
    if uid not in _AI_ACTIVE_USERS:
        return

    lang = _get_user_lang(uid, getattr(msg.from_user, "language_code", None))
    q = (msg.text or "").strip()
    if not q:
        await msg.answer(t("help.ai_empty", lang))
        return

    thinking_msg = await msg.answer(t("help.thinking", lang))
    context: List[Tuple[str, str]] = [("user", q)]

    ans: Optional[str] = None
    try:
        ans = await asyncio.wait_for(ai_answer(q, lang=lang, user_id=uid, context=context), timeout=25)
    except asyncio.TimeoutError:
        ans = None
    except Exception as e:
        log.exception("ai_answer error: %s", e)
        ans = None

    kb = _kb([[ _btn(t("help.exit_ai", lang), "help:exit_ai") ]])
    if ans:
        try:
            await thinking_msg.edit_text(ans, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        except TelegramBadRequest:
            await msg.answer(ans, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
    else:
        fallback = t("help.ai_fallback", lang)
        try:
            await thinking_msg.edit_text(fallback, reply_markup=_kb([
                [_btn(t("help.faq.send", lang), "help:faq:send"),
                 _btn(t("help.faq.grab", lang), "help:faq:grab")],
                [_btn(t("help.faq.recharge", lang), "help:faq:recharge"),
                 _btn(t("help.faq.rules", lang), "help:faq:rules")],
                [_btn(t("help.exit_ai", lang), "help:exit_ai")],
                [_btn(t("menu.back", lang), "help:main")],
            ]), parse_mode="HTML", disable_web_page_preview=True)
        except TelegramBadRequest:
            await msg.answer(fallback, reply_markup=_kb([
                [_btn(t("help.faq.send", lang), "help:faq:send"),
                 _btn(t("help.faq.grab", lang), "help:faq:grab")],
                [_btn(t("help.faq.recharge", lang), "help:faq:recharge"),
                 _btn(t("help.faq.rules", lang), "help:faq:rules")],
                [_btn(t("help.exit_ai", lang), "help:exit_ai")],
                [_btn(t("menu.back", lang), "help:main")],
            ]), parse_mode="HTML", disable_web_page_preview=True)
