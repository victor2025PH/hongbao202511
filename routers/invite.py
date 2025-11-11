# routers/invite.py
# -*- coding: utf-8 -*-
"""
Invite Center（纯 i18n 版）

改动要点：
- 所有对用户可见的文字一律通过 t() 获取，不做任何硬编码 fallback。
- 按钮文字优先复用已存在的 welfare.* / menu.* / labels.* 等键；
  需要新文案的，先占位为 t("...")，待你稍后提供 zh.yml 我再统一补全。
- 继续优先使用 core.utils.keyboards.invite_main_kb（若存在），以保持全局键盘风格一致。
"""

from __future__ import annotations
import logging
from typing import Optional

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from core.i18n.i18n import t
from models.db import get_session
from models.user import User

# 优先从项目根或 services 目录导入业务函数（保持与你现有项目兼容）
try:
    from invite_service import (
        add_invite_and_rewards,
        get_invite_progress_text,
        build_invite_share_link,
        redeem_points_to_progress,
        redeem_energy_to_points,
    )
except Exception:
    from services.invite_service import (  # type: ignore
        add_invite_and_rewards,
        get_invite_progress_text,
        build_invite_share_link,
        redeem_points_to_progress,
        redeem_energy_to_points,
    )

logger = logging.getLogger("invite")
router = Router(name="invite")

# ---- 键盘：优先使用项目统一键盘（若不存在则用本文件的 i18n 版兜底） ----
try:
    from core.utils.keyboards import invite_main_kb as _kb_invite_main  # 统一风格
except Exception:
    _kb_invite_main = None  # 运行时再使用本地兜底键盘


# =========================
# 语言获取
# =========================
def _canon_lang(x: Optional[str]) -> str:
    if not x:
        return "zh"
    x = (x or "").lower()
    if x.startswith("zh"):
        return "zh"
    if x.startswith("en"):
        return "en"
    return "zh"


def _tg_lang(user) -> str:
    code = getattr(user, "language_code", None) or ""
    return _canon_lang(code)


def _user_lang(user_id: int, fallback_user=None) -> str:
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u and getattr(u, "language", None):
            return _canon_lang(u.language)
        return _tg_lang(fallback_user)


# =========================
# 本地兜底键盘（纯 i18n，无任何硬编码文案）
# =========================
def _invite_menu_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    """
    邀请中心主操作：
    - 分享邀请链接 welfare.invite_share_btn
    - 兑换 welfare.invite_redeem_btn
    - 返回 menu.back
    """
    if _kb_invite_main:
        try:
            return _kb_invite_main(lang=lang)  # type: ignore[misc]
        except Exception:
            pass

    rows = [
        [InlineKeyboardButton(text=(t("welfare.invite_share_btn", lang) or ""), callback_data="invite:share")],
        [InlineKeyboardButton(text=(t("welfare.invite_redeem_btn", lang) or ""), callback_data="invite:redeem")],
        [InlineKeyboardButton(text=(t("menu.back", lang) or ""), callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _invite_redeem_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    """
    兑换页操作：
    - “消耗积分→进度” welfare.exchange.to_progress（整句由 i18n 给出）
    - “能量换积分按钮文案” welfare.exchange.energy_to_points_btn（整句由 i18n 给出）
    - 返回 menu.back
    """
    rows = [
        [InlineKeyboardButton(text=(t("welfare.exchange.to_progress", lang) or ""), callback_data="invite:redeem:progress")],
        [InlineKeyboardButton(text=(t("welfare.exchange.energy_to_points_btn", lang) or ""), callback_data="invite:redeem:energy")],
        [InlineKeyboardButton(text=(t("menu.back", lang) or ""), callback_data="invite:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =========================
# /start 深链解析
# =========================
def _parse_invite_payload(text: Optional[str]) -> Optional[int]:
    """
    从 /start payload 中解析 inviter_id，兼容多种前缀：
    - /start invite_123
    - /start ref_123
    - /start r_123
    """
    if not text or not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    payload = (parts[1] or "").strip()
    for prefix in ("invite_", "ref_", "r_"):
        if payload.startswith(prefix):
            tail = payload[len(prefix):].split()[0]
            try:
                return int(tail)
            except Exception:
                return None
    return None


# =========================
# 渲染：邀请进度主面板
# =========================
async def _show_invite_panel(msg_or_cb, *, lang: str, as_edit: bool = True):
    """
    展示“邀请进度 + 操作按钮”
    - 标题 welfare.invite_title
    - 进度正文由 service 内部按 i18n 组织
    """
    uid = msg_or_cb.from_user.id
    text_progress, _percent = get_invite_progress_text(uid, lang=lang)
    title = t("welfare.invite_title", lang) or ""
    body = f"{title}\n\n{text_progress}" if title else text_progress

    kb = _invite_menu_kb(lang)

    if isinstance(msg_or_cb, Message):
        await msg_or_cb.answer(body, parse_mode="HTML", reply_markup=kb)
    else:
        try:
            if as_edit:
                await msg_or_cb.message.edit_text(body, parse_mode="HTML", reply_markup=kb)
            else:
                await msg_or_cb.message.answer(body, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest:
            await msg_or_cb.message.answer(body, parse_mode="HTML", reply_markup=kb)

    if not isinstance(msg_or_cb, Message):
        await msg_or_cb.answer()


# =========================
# /start 深链：记录邀请关系
# =========================
@router.message(CommandStart(deep_link=True))
async def handle_invite_deeplink(msg: Message, command: CommandStart):
    lang = _user_lang(msg.from_user.id, msg.from_user)

    inviter_id = _parse_invite_payload(msg.text)
    if not inviter_id:
        await msg.answer(t("errors.bad_request", lang) or "")
        return

    # 自己邀请自己：忽略
    if inviter_id == msg.from_user.id:
        await msg.answer(t("errors.bad_request", lang) or "")
        return

    ok = False
    try:
        ok = add_invite_and_rewards(inviter_id, msg.from_user.id, give_extra_points=True)
    except Exception as e:
        logger.exception("add_invite_and_rewards failed: %s", e)

    # 通知邀请人其最新进度（若对方未启用 bot，忽略异常）
    if ok:
        try:
            inviter_lang = _user_lang(inviter_id, None)
            text_progress, _ = get_invite_progress_text(inviter_id, lang=inviter_lang)
            title = t("welfare.invite_title", inviter_lang) or ""
            notify_text = f"{title}\n\n{text_progress}" if title else text_progress
            await msg.bot.send_message(inviter_id, notify_text, parse_mode="HTML")
        except Exception as e:
            logger.warning("notify inviter failed inviter=%s: %s", inviter_id, e)

    # 给新用户一个友好的落地：直接打开邀请中心
    await _show_invite_panel(msg, lang=lang)


# =========================
# /invite 命令：打开邀请中心
# =========================
@router.message(Command("invite"))
async def cmd_invite(msg: Message):
    lang = _user_lang(msg.from_user.id, msg.from_user)
    await _show_invite_panel(msg, lang=lang)


# =========================
# 回调：邀请中心 / 分享 / 兑换
# =========================
@router.callback_query(F.data == "invite:main")
async def invite_main(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    await _show_invite_panel(cb, lang=lang)


@router.callback_query(F.data == "invite:share")
async def invite_share(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    link = build_invite_share_link(cb.from_user.id)

    # 标题 + 链接说明均由 i18n 提供；如缺失，只显示链接本身
    ready = t("welfare.invite_share_ready", lang) or ""
    link_label = t("welfare.invite_share_link_label", lang) or ""
    text_lines = [x for x in (ready, "", f"{link_label} {link}".strip()) if x is not None]
    text = "\n".join(text_lines) if text_lines else link

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(t("welfare.invite_title", lang) or ""), callback_data="invite:main")],
        [InlineKeyboardButton(text=(t("menu.back", lang) or ""), callback_data="menu:main")],
    ])

    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "invite:redeem")
async def invite_redeem(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    # 头部：福利中心标题 + 邀请页副标题（均从 i18n 获取）
    title = t("welfare.title", lang) or ""
    sub = t("welfare.invite_title", lang) or ""
    head = f"{title} · {sub}".strip(" ·")
    desc = t("welfare.exchange.desc", lang) or t("welfare.exchange.to_progress", lang) or ""
    text = f"{head}\n\n{desc}" if head else desc

    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_invite_redeem_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=_invite_redeem_kb(lang))
    await cb.answer()


@router.callback_query(F.data == "invite:redeem:progress")
async def invite_redeem_progress(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    ok, msg_text, _percent = redeem_points_to_progress(cb.from_user.id, lang=lang)
    tip = msg_text or (t("common.bad_request", lang) or "")
    try:
        await cb.message.edit_text(tip, parse_mode="HTML", reply_markup=_invite_menu_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(tip, parse_mode="HTML", reply_markup=_invite_menu_kb(lang))
    await cb.answer()


@router.callback_query(F.data == "invite:redeem:energy")
async def invite_redeem_energy(cb: CallbackQuery):
    lang = _user_lang(cb.from_user.id, cb.from_user)
    ok, msg_text = redeem_energy_to_points(cb.from_user.id, lang=lang)
    tip = msg_text or (t("common.bad_request", lang) or "")
    try:
        await cb.message.edit_text(tip, parse_mode="HTML", reply_markup=_invite_menu_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(tip, parse_mode="HTML", reply_markup=_invite_menu_kb(lang))
    await cb.answer()
