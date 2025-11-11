# routers/menu.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import logging
import asyncio
import re

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command

from core.i18n.i18n import t
from core.utils.keyboards import (
    main_menu, back_home_kb, admin_menu, language_kb, welfare_menu, asset_menu
)
from models.user import User, get_or_create_user
from models.db import get_session
from config.settings import is_admin as _settings_is_admin

# === 新增：为“选择封面（最近红包）”做桥接 ===
from models.envelope import Envelope  # 查找最近红包
from routers.hongbao import show_cover_picker  # 直接拉起封面选择器
from services.google_logger import log_user_to_sheet

router = Router()
log = logging.getLogger("menu")

# ================== 基础工具 ==================
# 支持 8 种语言 + 地区码主码回退
_SUPPORTED_LANGS = {"zh", "en", "fr", "de", "es", "hi", "vi", "th"}

def _canon_lang(code: str | None) -> str:
    default = "zh"
    if not code:
        return default
    c = str(code).strip().lower().replace("_", "-")
    if not c:
        return default
    # 完整命中（将来你想支持 pt-br 也能直接加进 _SUPPORTED_LANGS）
    if c in _SUPPORTED_LANGS:
        return c
    # 主码回退：fr-ca -> fr
    primary = c.split("-", 1)[0]
    if primary in _SUPPORTED_LANGS:
        return primary
    # 历史兼容（避免旧数据只存 zh/en）
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"
    return default


def _tg_lang(u) -> str:
    return _canon_lang(getattr(u, "language_code", None))

def _db_lang_or_fallback(user_id: int, fallback_user) -> str:
    try:
        with get_session() as s:
            u = s.query(User).filter_by(tg_id=user_id).first()
            if u and getattr(u, "language", None):
                return _canon_lang(u.language)
    except Exception as e:
        log.exception("menu._db_lang_or_fallback: read db lang failed: %s", e)
    return _tg_lang(fallback_user)

def _is_admin(user_id: int) -> bool:
    try:
        return _settings_is_admin(user_id)
    except Exception:
        return False

async def _auto_delete(bot, chat_id: int, message_id: int, delay: int = 60):
    """延迟删除一条消息（无权限或失败忽略）。"""
    await asyncio.sleep(max(1, delay))
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass

# ---------- 新增：安全答复与安全文本 ----------
async def _safe_cb_answer(cb: CallbackQuery, text: str | None = None, show_alert: bool = False, cache_time: int | None = None):
    """对 CallbackQuery 的安全答复：query 过期或无效时忽略异常。"""
    try:
        if cache_time is None:
            await cb.answer(text=text, show_alert=show_alert)
        else:
            await cb.answer(text=text, show_alert=show_alert, cache_time=cache_time)
    except TelegramBadRequest:
        # query 已过期/无效时不抛错，避免打断业务流程
        pass
    except Exception:
        # 其他异常也忽略（不影响后续逻辑）
        pass

def _non_empty(text: str | None, fallback: str) -> str:
    """保证正文类文案非空（i18n 缺键时返回空串的兜底）。"""
    if text is None:
        return fallback
    t_ = str(text).strip()
    return t_ if t_ else fallback

async def _safe_edit_or_answer_text(cb_or_msg, text: str, kb=None):
    """
    统一安全输出文本：优先 edit_text，失败降级 answer。
    无论 i18n 是否缺键，都确保不会传空文本给 Telegram。
    """
    safe_text = _non_empty(text, "✅ OK")
    # 兼容 CallbackQuery / Message
    if isinstance(cb_or_msg, CallbackQuery):
        msg = cb_or_msg.message
    else:
        msg = cb_or_msg
    try:
        await msg.edit_text(safe_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest:
        # 可能是“不可编辑/类型不匹配”等，降级新发一条
        await msg.answer(safe_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

# ================== 内部键盘（全部走 i18n） ==================
def _group_bind_kb(chat_id: int, lang: str) -> InlineKeyboardMarkup:
    """群里提示用：只给“绑定本群并在私聊继续”按钮"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=t("menu.bind_this_group", lang),
            callback_data=f"menu:bind_group:{int(chat_id)}"
        )]
    ])

# 预检：机器人是否能在目标群发言（用于“绑定本群并在私聊继续”）
async def _preflight_can_post(bot, target_chat_id: int) -> tuple[bool, str]:
    """
    返回 (ok, reason)
    reason: "" | "not_in_chat" | "no_rights" | "not_found" | "unknown"
    """
    try:
        me = await bot.get_me()
        # 检查群是否存在
        try:
            await bot.get_chat(target_chat_id)
        except TelegramBadRequest as e:
            err = str(e).lower()
            if "chat not found" in err or "chat is deactivated" in err:
                return False, "not_found"
        except Exception:
            pass

        # 检查机器人是否在群内
        try:
            m = await bot.get_chat_member(target_chat_id, me.id)
            status = str(getattr(m, "status", "")).lower()
            if status in ("left", "kicked"):
                return False, "not_in_chat"
        except TelegramBadRequest:
            return False, "not_in_chat"
        except Exception:
            pass

        # 检查是否可发言（触发 typing 动作）
        try:
            await bot.send_chat_action(target_chat_id, "typing")
        except TelegramBadRequest as e:
            l = str(e).lower()
            if "not enough rights" in l or "have no rights" in l:
                return False, "no_rights"
            if "bot was blocked" in l or "bot was kicked" in l:
                return False, "not_in_chat"
            if "chat not found" in l:
                return False, "not_found"
            return False, "unknown"

        return True, ""
    except Exception as e:
        log.exception("menu._preflight_can_post failed: %s", e)
        return False, "unknown"

# ================== /start /menu 入口 ==================
@router.message(CommandStart())
@router.message(Command("menu"))
@router.message(F.text.startswith("/start"))
@router.message(F.text.startswith("/menu"))
async def cmd_start_or_menu(msg: Message):

    # --- 新增：群里先删触发命令的那条消息 ---
    try:
        if getattr(msg.chat, "type", "") in {"group", "supergroup"} and (msg.text or "").startswith("/start"):
            await msg.delete()  # 需要机器人拥有“删除消息”权限；失败忽略
    except TelegramBadRequest:
        pass
    except Exception:
        pass
    """
    私聊：显示主菜单
    群聊：只显示“绑定本群并在私聊继续”的唯一按钮；并在短时间后删除这条消息（减少驻留）
    """
    init_lang = _tg_lang(msg.from_user)
    with get_session() as s:
        user = s.query(User).filter_by(tg_id=msg.from_user.id).first()
        if user is None:
            get_or_create_user(s, tg_id=msg.from_user.id, username=msg.from_user.username or None, lang=init_lang)
            s.commit()
            lang = init_lang
        else:
            lang = _canon_lang(user.language or init_lang)
        s.expunge_all()

    chat_type = getattr(msg.chat, "type", "")
    if chat_type in {"group", "supergroup"}:
        # ← 新增：群内首次交互，补记到在线文档（幂等：google_logger 已覆盖 first_seen_in_group）
        try:
            log_user_to_sheet(
                msg.from_user,
                source="first_seen_in_group",
                chat=msg.chat,
                inviter_user_id=None,
                joined_via_invite_link=False,
                note="first interaction in group (menu entry)"
            )
        except Exception as e:
            log.warning("menu.first_seen log failed (cmd_start_or_menu in group): %s", e)

        # 群中：只给“绑定本群并在私聊继续”的按钮
        text = _non_empty(t("menu.bind_hint_in_group", lang), "🔧 请点下方按钮绑定本群并在私聊继续。")
        kb = _group_bind_kb(msg.chat.id, lang)
        try:
            m = await msg.answer(text, parse_mode="HTML", reply_markup=kb)
            # 若希望消息短暂存在，可定时删除
            asyncio.create_task(_auto_delete(msg.bot, m.chat.id, m.message_id, delay=60))
        except TelegramBadRequest:
            pass
        return


    # 私聊：主菜单
    kb = main_menu(lang, _is_admin(msg.from_user.id))
    text = _non_empty(t("welcome", lang, username=msg.from_user.full_name), "🎉 请选择功能 👇")
    try:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)

# 单独的 /start（带或不带 @bot），先删除命令消息，再按上面的入口逻辑分流
@router.message(F.text.regexp(r"^/start(?:@\w+)?(?:\s+.*)?$"))
async def deep_start(msg: Message, state):
    # === 在群里隐藏用户输入的 /start 或 /start@bot 命令 ===
    try:
        if getattr(msg.chat, "type", "") in {"group", "supergroup"}:
            await msg.delete()  # 需要机器人在群里有“删除消息”权限；失败忽略
    except TelegramBadRequest:
        pass
    except Exception:
        pass

    """
    私聊：显示主菜单
    群聊：只显示“绑定本群并在私聊继续”的唯一按钮；并在短时间后删除这条消息（减少驻留）
    """
    init_lang = _tg_lang(msg.from_user)
    with get_session() as s:
        user = s.query(User).filter_by(tg_id=msg.from_user.id).first()
        if user is None:
            get_or_create_user(s, tg_id=msg.from_user.id, username=msg.from_user.username or None, lang=init_lang)
            s.commit()
            lang = init_lang
        else:
            lang = _canon_lang(user.language or init_lang)
        s.expunge_all()

    # ← 新增：群里触发 /start（含 @bot），视为首次交互进行补记
    if getattr(msg.chat, "type", "") in {"group", "supergroup"}:
        try:
            log_user_to_sheet(
                msg.from_user,
                source="first_seen_in_group",
                chat=msg.chat,
                inviter_user_id=None,
                joined_via_invite_link=False,
                note="first interaction in group (deep /start)"
            )
        except Exception as e:
            log.warning("menu.first_seen log failed (deep_start in group): %s", e)

    chat_type = getattr(msg.chat, "type", "")
    if chat_type in {"group", "supergroup"}:
        text = _non_empty(t("menu.bind_hint_in_group", lang), "🔧 请点下方按钮绑定本群并在私聊继续。")

        kb = _group_bind_kb(msg.chat.id, lang)
        try:
            m = await msg.answer(text, parse_mode="HTML", reply_markup=kb)
            asyncio.create_task(_auto_delete(msg.bot, m.chat.id, m.message_id, delay=60))
        except TelegramBadRequest:
            pass
        return

    kb = main_menu(lang, _is_admin(msg.from_user.id))
    text = _non_empty(t("welcome", lang, username=msg.from_user.full_name), "🎉 请选择功能 👇")
    try:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)

# ================== 主菜单回到首页 ==================
@router.callback_query(F.data.in_({"menu:home", "menu:main"}))
async def menu_home(cb: CallbackQuery):
    await _safe_cb_answer(cb)  # ✅ 先 ACK
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    kb = main_menu(lang, _is_admin(cb.from_user.id))
    text = _non_empty(t("welcome", lang, username=cb.from_user.full_name), "🎉 请选择功能 👇")
    await _safe_edit_or_answer_text(cb, text, kb)

# ================== 福利中心 ==================
@router.callback_query(F.data.in_({"menu:welfare", "wf:main"}))
async def menu_welfare(cb: CallbackQuery):
    await _safe_cb_answer(cb)  # ✅ 先 ACK
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    text = _non_empty(t("welfare.title", lang), "🎁 福利中心")
    await _safe_edit_or_answer_text(cb, text, welfare_menu(lang))

# ================== 管理面板 ==================
@router.callback_query(F.data == "menu:admin")
async def menu_admin(cb: CallbackQuery):
    await _safe_cb_answer(cb)  # ✅ 先 ACK
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    if not _is_admin(cb.from_user.id):
        tip = _non_empty(t("admin.no_permission", lang), "⛔ 你没有权限。")
        try:
            await cb.message.answer(tip, parse_mode="HTML", reply_markup=back_home_kb(lang))
        except TelegramBadRequest:
            pass
        return
    text = _non_empty(t("menu.admin", lang), "🛠 管理面板")
    kb = admin_menu(lang)
    await _safe_edit_or_answer_text(cb, text, kb)

# ================== 资产面板 ==================
@router.callback_query(F.data.in_({"balance:main", "asset:main", "menu:asset", "menu:assets"}))
async def menu_assets(cb: CallbackQuery):
    await _safe_cb_answer(cb)  # ✅ 先 ACK
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    # 多键回退：balance.title -> asset.title -> balance_page.title -> 硬兜底
    title = (t("balance.title", lang) or
             t("asset.title", lang) or
             t("balance_page.title", lang))
    title = _non_empty(title, "💼 我的资产")
    kb = asset_menu(lang)
    await _safe_edit_or_answer_text(cb, title, kb)

# ================== 发红包入口（保持原有逻辑） ==================
@router.callback_query(F.data.in_({"menu:send", "hb:menu"}))
async def menu_send_hb(cb: CallbackQuery):
    await _safe_cb_answer(cb)  # ✅ 先 ACK
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    if getattr(cb.message.chat, "type", "") in {"group", "supergroup"}:
        # ← 新增：群内点击“发红包”菜单，补记首次交互
        try:
            log_user_to_sheet(
                cb.from_user,
                source="first_seen_in_group",
                chat=cb.message.chat,
                inviter_user_id=None,
                joined_via_invite_link=False,
                note="first interaction in group (menu:send/hb:menu)"
            )
        except Exception as e:
            log.warning("menu.first_seen log failed (menu_send_hb in group): %s", e)

        tip = _non_empty(t("env.dm_hint", lang), "🔒 为保护隐私，已在私聊继续发红包。")
        kb = await _dm_continue_kb(cb, lang)
        try:
            await cb.message.answer(tip, parse_mode="HTML", reply_markup=kb)

        except TelegramBadRequest:
            await cb.message.answer(tip, parse_mode="HTML")
    else:
        text = _non_empty(t("env.title", lang), "🧧 发红包向导")
        kb = _hb_start_kb(lang)
        await _safe_edit_or_answer_text(cb, text, kb)

# ================== 语言设置 ==================
@router.callback_query(F.data.in_({"admin:lang", "lang:menu"}))
async def language_menu(cb: CallbackQuery):
    await _safe_cb_answer(cb)  # ✅ 先 ACK
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    text = _non_empty(t("lang.title", lang), "🌐 请选择语言")
    kb = language_kb(lang)
    await _safe_edit_or_answer_text(cb, text, kb)

@router.callback_query(F.data.startswith("lang:set:"))
async def set_language(cb: CallbackQuery):
    await _safe_cb_answer(cb)  # ✅ 先 ACK
    _, _, code = cb.data.split(":", 2)
    await _persist_lang_and_back_to_menu(cb, code)

@router.callback_query(F.data.regexp(r"^lang:(zh|en|fr|de|es|hi|vi|th)$"))
async def set_language_short(cb: CallbackQuery):
    await _safe_cb_answer(cb)  # ✅ 先 ACK
    _, code = cb.data.split(":", 1)
    await _persist_lang_and_back_to_menu(cb, code)


async def _persist_lang_and_back_to_menu(cb: CallbackQuery, code: str):
    new_lang = _canon_lang(code)
    with get_session() as s:
        user = get_or_create_user(
            s, tg_id=cb.from_user.id, username=cb.from_user.username or None, lang=new_lang
        )
        user.language = new_lang
        s.add(user)
        s.commit()
        s.expunge_all()
    text = _non_empty(t("welcome", new_lang, username=cb.from_user.full_name), "🎉 请选择功能 👇")
    kb = main_menu(new_lang, _is_admin(cb.from_user.id))
    await _safe_edit_or_answer_text(cb, text, kb)
    # 这里不再强制再次 answer；若需要可调用 _safe_cb_answer(cb)

# ================== 新增：绑定本群并在私聊继续 ==================
@router.callback_query(F.data.regexp(r"^menu:bind_group:(-?\d+)$"))
async def bind_this_group(cb: CallbackQuery):
    """
    在群里点“绑定本群并在私聊继续”：
    - 预检机器人是否能在该群发言
    - 记录到 User.last_target_chat_id / last_target_chat_title（若字段存在）
    - 群里编辑消息为“已绑定，去私聊继续” + 按钮，并定时删除
    - 私聊里发送“开始发红包 / 打开主菜单”的按钮（不再发纯文本链接）
    """
    await _safe_cb_answer(cb)  # ✅ 先 ACK
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    m = re.match(r"^menu:bind_group:(-?\d+)$", cb.data or "")
    if not m:
        await _safe_cb_answer(cb, _non_empty(t("errors.bad_request", lang), "⚠️ 请求有误，请重试"), show_alert=True)
        return
    target_chat_id = int(m.group(1))

    ok, reason = await _preflight_can_post(cb.message.bot, target_chat_id)
    if not ok:
        reason_map = {
            "not_in_chat": _non_empty(t("env.errors.not_in_chat", lang), "🚫 机器人不在该群"),
            "no_rights":   _non_empty(t("env.errors.no_rights", lang), "🚫 机器人无发言权限"),
            "not_found":   _non_empty(t("env.errors.not_found", lang), "❌ 未找到该群"),
            "unknown":     _non_empty(t("env.errors.unknown", lang), "⚠️ 无法验证目标群"),
        }
        await _safe_cb_answer(cb, reason_map.get(reason, reason_map["unknown"]), show_alert=True)
        return

    # ← 新增：预检通过，确认是有效群，补记首次交互
    try:
        if getattr(cb.message.chat, "type", "") in {"group", "supergroup"}:
            log_user_to_sheet(
                cb.from_user,
                source="first_seen_in_group",
                chat=cb.message.chat,
                inviter_user_id=None,
                joined_via_invite_link=False,
                note="first interaction in group (bind_this_group)"
            )
    except Exception as e:
        log.warning("menu.first_seen log failed (bind_this_group): %s", e)

    # 记录“最近目标群”（如果你的 User 表有这两个字段）
    try:
        with get_session() as s:
            u = get_or_create_user(s, tg_id=cb.from_user.id, username=cb.from_user.username or None, lang=lang)

            u.last_target_chat_id = target_chat_id
            try:
                ch = await cb.message.bot.get_chat(target_chat_id)
                u.last_target_chat_title = getattr(ch, "title", None) or (f"@{ch.username}" if getattr(ch, "username", None) else None)
            except Exception:
                pass
            s.add(u)
            s.commit()
    except Exception:
        pass

    # 群里编辑 + 定时删除
    tip = _non_empty(t("menu.bound_go_dm", lang), "✅ 已绑定本群为目标群，请到私聊继续。")
    try:
        me = await cb.message.bot.get_me()
        deep = f"https://t.me/{me.username}?start=hb" if getattr(me, "username", None) else "https://t.me/"
    except Exception:
        deep = "https://t.me/"
    kb_group = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_non_empty(t("env.continue_in_dm", lang), "在私聊继续 ➡️"), url=deep)]
    ])
    try:
        m2 = await cb.message.edit_text(tip, reply_markup=kb_group, parse_mode="HTML")
        asyncio.create_task(_auto_delete(cb.message.bot, m2.chat.id, m2.message_id, delay=60))
    except TelegramBadRequest:
        pass

    # 私聊推送入口
    try:
        text_dm = _non_empty(t("env.title", lang), "🧧 发红包向导")
        await cb.message.bot.send_message(cb.from_user.id, text_dm, reply_markup=_hb_start_kb(lang), parse_mode="HTML")
    except Exception:
        pass

# ======== 发红包向导的入口键盘（与 envelope/hongbao 配合使用） ========
def _hb_start_kb(lang: str) -> InlineKeyboardMarkup:
    # === 新增第三个按钮：🖼 选择封面（最近红包） ===
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_non_empty(t("menu.send", lang), "🧧 发红包"), callback_data="hb:start")],
        [InlineKeyboardButton(text=_non_empty(t("hongbao.cover.pick_btn", lang), "🖼 选择封面（最近红包）"), callback_data="hb:pick_cover")],
        [InlineKeyboardButton(text=_non_empty(t("menu.back", lang), "⬅️ 返回"), callback_data="menu:main")],
    ])

async def _dm_continue_kb(cb: CallbackQuery, lang: str) -> InlineKeyboardMarkup:
    try:
        me = await cb.message.bot.get_me()
        if getattr(me, "username", None):
            url = f"https://t.me/{me.username}?start=hb"
        else:
            url = "https://t.me/"
    except Exception:
        url = "https://t.me/"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_non_empty(t("env.continue_in_dm", lang), "在私聊继续 ➡️"), url=url)]
    ])

# ================== 新增：快捷入口 —— 选择封面（最近红包） ==================
def _resolve_target_chat_id_for_user(user_id: int, current_chat_id: int | None = None) -> int | None:
    """
    优先使用用户在 bind_this_group 时记录的 last_target_chat_id；
    否则在群里触发时使用当前群；
    私聊里且没有记录则返回 None（引导去群里点击“绑定本群并在私聊继续”）。
    """
    try:
        with get_session() as s:
            u = s.query(User).filter_by(tg_id=user_id).first()
            if u and getattr(u, "last_target_chat_id", None):
                return int(u.last_target_chat_id)
    except Exception:
        pass
    if current_chat_id is not None:
        return int(current_chat_id)
    return None

def _find_latest_envelope_for_user(user_tg_id: int, target_chat_id: int | None = None) -> tuple[int | None, int | None]:
    """
    返回 (envelope_id, chat_id)
    - 若提供 target_chat_id，则优先限定该群；
    - 否则在该用户创建的所有红包里取最近一条。
    """
    try:
        with get_session() as s:
            q = s.query(Envelope).filter(Envelope.sender_tg_id == int(user_tg_id))
            if target_chat_id is not None:
                q = q.filter(Envelope.chat_id == int(target_chat_id))
            # 优先按 created_at DESC；无则按 id DESC
            try:
                q = q.order_by(Envelope.created_at.desc())
            except Exception:
                q = q.order_by(Envelope.id.desc())
            row = q.first()
            if row:
                return int(row.id), int(row.chat_id)
    except Exception as e:
        log.debug("menu._find_latest_envelope_for_user failed: %s", e)
    return None, None

@router.callback_query(F.data == "hb:pick_cover")
async def quick_pick_cover_for_latest(cb: CallbackQuery):
    """
    从发红包向导的私聊页，快速打开“封面选择器”，针对“最近红包”。
    仅管理员可用；没找到最近红包时会提示。
    """
    await _safe_cb_answer(cb)  # ✅ 先 ACK
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    if not _is_admin(cb.from_user.id):
        await _safe_cb_answer(cb, _non_empty(t("admin.no_permission", lang), "⛔ 你没有权限。"), show_alert=True)
        return

    chat_type = getattr(cb.message.chat, "type", "")
    current_chat_id = cb.message.chat.id if chat_type in {"group", "supergroup"} else None
    target_chat_id = _resolve_target_chat_id_for_user(cb.from_user.id, current_chat_id)

    if target_chat_id is None:
        # 引导去群里先绑定
        tip = _non_empty(t("env.bind_first", lang), "请先到目标群点击“绑定本群并在私聊继续”，再回来选择封面。")
        try:
            await cb.message.answer(tip, reply_markup=back_home_kb(lang), parse_mode="HTML")
        except TelegramBadRequest:
            pass
        return

    eid, chat_id = _find_latest_envelope_for_user(cb.from_user.id, target_chat_id)
    if not eid or not chat_id:
        tip = _non_empty(t("hongbao.cover.no_recent_env", lang), "未找到你最近创建的红包，请先创建红包再试。")
        try:
            await cb.message.answer(tip, reply_markup=back_home_kb(lang), parse_mode="HTML")
        except TelegramBadRequest:
            pass
        return

    # 直接拉起封面选择器（分页从第 1 页开始）
    try:
        await show_cover_picker(cb, envelope_id=eid, chat_id=chat_id, lang=lang)
    except Exception as e:
        log.exception("menu.quick_pick_cover_for_latest: %s", e)
        tip = _non_empty(t("common.not_available", lang), "暂不可用")
        try:
            await cb.message.answer(tip, reply_markup=back_home_kb(lang), parse_mode="HTML")
        except TelegramBadRequest:
            pass
