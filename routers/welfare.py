# routers/welfare.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.exceptions import TelegramBadRequest

from core.i18n.i18n import t
from core.utils.keyboards import welfare_menu, back_home_kb
from config.feature_flags import flags

from models.db import get_session
from models.user import User, update_balance, get_or_create_user
from models.ledger import Ledger, LedgerType, add_ledger_entry

logger = logging.getLogger("welfare")
router = Router(name="welfare")

def _get_invite_handlers():
    try:
        from routers.invite import invite_main, invite_share, invite_redeem  # type: ignore
        return invite_main, invite_share, invite_redeem
    except Exception as e:
        logger.warning("Invite router not available: %s", e)
        return None, None, None

def _canon_lang(code: str | None) -> str:
    if not code: return "zh"
    c = str(code).strip().lower()
    if c.startswith("zh"): return "zh"
    if c.startswith("en"): return "en"
    return "zh"

def _user_lang(user_id: int, fallback_user) -> str:
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u and getattr(u, "language", None):
            return _canon_lang(u.language)
    return _canon_lang(getattr(fallback_user, "language_code", None))

def _has_signed_today(user_id: int) -> bool:
    today_utc = datetime.utcnow().date()
    with get_session() as s:
        row = (
            s.query(Ledger)
            .filter(Ledger.user_tg_id == user_id)
            .filter(Ledger.type == LedgerType.SIGNIN)
            .order_by(Ledger.created_at.desc())
            .first()
        )
        if not row:
            return False
        return (row.created_at.date() == today_utc)

@router.callback_query(F.data.in_({"wf:main", "wf:home"}))
async def wf_main(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    text = t("welfare.title", lang) or "🎁 Welfare Center"
    kb = welfare_menu(lang)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "wf:signin")
async def wf_signin(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    if not flags.get("ENABLE_SIGNIN", True):
        await cb.answer(t("common.not_available", lang) or "⛔ This feature is temporarily unavailable.", show_alert=True)
        return
    if _has_signed_today(cb.from_user.id):
        text = t("welfare.signin_already", lang) or "ℹ️ You have already checked in today."
        await cb.answer(text, show_alert=True)
        return
    reward_points = int(flags.get("SIGNIN_REWARD_POINTS", 0) or 0)
    with get_session() as s:
        last = (
            s.query(Ledger)
            .filter(Ledger.user_tg_id == cb.from_user.id, Ledger.type == LedgerType.SIGNIN)
            .order_by(Ledger.created_at.desc())
            .first()
        )
        if last and last.created_at.date() == datetime.utcnow().date():
            text = t("welfare.signin_already", lang) or "ℹ️ You have already checked in today."
            try:
                await cb.message.edit_text(text, parse_mode="HTML", reply_markup=welfare_menu(lang))
            except TelegramBadRequest:
                await cb.message.answer(text, parse_mode="HTML", reply_markup=welfare_menu(lang))
            await cb.answer(); return

        u = s.query(User).filter_by(tg_id=cb.from_user.id).first()
        if not u:
            u = get_or_create_user(s, tg_id=cb.from_user.id)

        if reward_points > 0:
            update_balance(s, u, "POINT", reward_points)
            add_ledger_entry(
                s,
                user_tg_id=cb.from_user.id,
                ltype=LedgerType.SIGNIN,
                token="POINT",
                amount=reward_points,
                ref_type="SIGNIN",
                ref_id="DAILY",
                note=t("welfare.signin_ok", lang, points=reward_points) or f"+{reward_points} points",
            )
        s.commit()

    text = t("welfare.signin_ok", lang, points=reward_points) or f"✅ +{reward_points} points"
    kb = welfare_menu(lang)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "wf:promo")
async def wf_promo(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    title = t("welfare.promo", lang) or "📣 Announcements"
    text = f"{title}\n\n🎉 Coming soon..."
    kb = back_home_kb(lang)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "wf:rules")
async def wf_rules(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    text = t("welfare.invite_rules", lang) or "📜 Invite & Redeem Rules"
    kb = back_home_kb(lang)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.in_({"wf:invite", "wf:invite:main"}))
async def wf_invite_forward(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    invite_main, _, _ = _get_invite_handlers()
    if not invite_main:
        await cb.answer(t("common.not_available", lang) or "⛔ Not available", show_alert=True); return
    return await invite_main(cb)

@router.callback_query(F.data == "wf:invite:share")
async def wf_invite_share_forward(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    _, invite_share, _ = _get_invite_handlers()
    if not invite_share:
        await cb.answer(t("common.not_available", lang) or "⛔ Not available", show_alert=True); return
    return await invite_share(cb)

@router.callback_query(F.data.in_({"wf:invite:redeem", "wf:redeem"}))
async def wf_invite_redeem_forward(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    _, _, invite_redeem = _get_invite_handlers()
    if not invite_redeem:
        await cb.answer(t("common.not_available", lang) or "⛔ Not available", show_alert=True); return
    return await invite_redeem(cb)
