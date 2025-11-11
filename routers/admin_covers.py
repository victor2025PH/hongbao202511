# routers/admin_covers.py
# -*- coding: utf-8 -*-
"""
管理员：红包封面管理
- 入口：callback -> "admin:covers"
- 功能：新增（转发频道消息登记为封面）/ 列表与删除 / 预览 / 启用开关
- i18n：使用 core.i18n.i18n.t 读取文案；若缺失则英文兜底
- 权限：仅 config.settings.is_admin(user_id) 为 True 的账号可用
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Optional, Tuple, List

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import StateFilter
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from core.i18n.i18n import t
from config.settings import is_admin as _is_admin
from models.db import get_session
from models.user import User
from models.cover import (
    Cover,
    list_covers,
    get_cover_by_id,
    add_cover,
    upsert_from_channel_post,
    delete_cover,
    set_cover_enabled,
    ensure_cover_schema,
)

from core.utils.keyboards import admin_covers_kb

log = logging.getLogger("admin_covers")
router = Router()

# ---------- 工具 ----------

def _canon_lang(code: Optional[str]) -> str:
    if not code:
        return "zh"
    c = str(code).strip().lower()
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"
    return "zh"

def _db_lang_or_fallback(uid: int, fallback_user) -> str:
    """优先读取数据库中的用户语言；不存在则回退 Telegram profile 的 language_code。"""
    try:
        with get_session() as s:
            u = s.query(User).filter_by(tg_id=uid).first()
            if u and getattr(u, "lang", None):
                return _canon_lang(u.lang)
    except Exception:
        pass
    return _canon_lang(getattr(fallback_user, "language_code", "zh"))

def _tt(key: str, lang: str, zh_fallback: str = "", en_fallback: str = "") -> str:
    try:
        txt = t(key, lang)
        if txt and str(txt).strip():
            return txt
    except Exception:
        pass
    return zh_fallback if lang == "zh" else en_fallback

def _is_admin_uid(uid: int) -> bool:
    try:
        return bool(_is_admin(uid))
    except Exception:
        return False

def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text or " ", callback_data=data)

# ---------- FSM ----------

class CoverStates(StatesGroup):
    WAIT_FORWARD = State()   # 等待管理员转发频道消息
    CONFIRM_DEL = State()    # 选择删除

@dataclass
class Ctx:
    page: int = 1

# ---------- 入口 ----------

@router.callback_query(F.data == "admin:covers")
async def admin_covers_entry(cb: CallbackQuery, state: FSMContext):
    if not _is_admin_uid(cb.from_user.id):
        await cb.answer("⛔ You have no permission.", show_alert=True)
        return
    await state.clear()
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    await cb.message.edit_text(
        _tt("admin.covers.menu_desc", lang, "🎨 管理红包封面\n请选择操作：", "🎨 Manage covers\nChoose an action:"),
        reply_markup=admin_covers_kb(lang),
        disable_web_page_preview=True,
    )
    await cb.answer()

# ---------- 新增（转发频道消息） ----------

@router.callback_query(F.data == "admin:covers:add")
async def covers_add_ask(cb: CallbackQuery, state: FSMContext):
    if not _is_admin_uid(cb.from_user.id):
        await cb.answer("⛔ You have no permission.", show_alert=True)
        return
    await state.set_state(CoverStates.WAIT_FORWARD)
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    text = _tt(
        "admin.covers.upload_ask",
        lang,
        "请发送一条来自素材频道的消息（建议直接在频道里“转发”过来）。我会自动登记为封面。",
        "Please forward a message from the materials channel. I will register it as a cover.",
    )
    await cb.message.edit_text(text, reply_markup=_kb([[ _btn(_tt("menu.back", lang, "⬅️ 返回", "⬅️ Back"), "admin:covers") ]]))

def _extract_media_from_message(msg: Message) -> tuple[Optional[str], Optional[str]]:
    """
    从消息中提取 (file_id, media_type)
    - 优先照片：取最大分辨率的 photo.file_id
    - 其次动画/视频
    """
    if getattr(msg, "photo", None):
        # 最大尺寸
        try:
            best = max(msg.photo, key=lambda p: (p.width or 0, p.height or 0))
            return best.file_id, "photo"
        except Exception:
            pass
    if getattr(msg, "animation", None):
        return msg.animation.file_id, "animation"
    if getattr(msg, "video", None):
        return msg.video.file_id, "video"
    return None, None

@router.message(StateFilter(CoverStates.WAIT_FORWARD))
async def covers_add_on_message(msg: Message, state: FSMContext):
    if not _is_admin_uid(msg.from_user.id):
        return
    lang = _db_lang_or_fallback(msg.from_user.id, msg.from_user)

    # 仅接受“从频道转发”的消息
    fwd = getattr(msg, "forward_origin", None) or getattr(msg, "forward_from_chat", None)
    channel_id = None
    message_id = None

    # aiogram v3 的 forward 信息统一封装在 forward_origin
    try:
        # v3: forward_origin.chat.id / forward_origin.message_id
        if getattr(msg, "forward_origin", None) and getattr(msg.forward_origin, "chat", None):
            ch = msg.forward_origin.chat
            if getattr(ch, "type", None) == "channel":
                channel_id = int(ch.id)
                message_id = int(getattr(msg.forward_origin, "message_id", 0) or msg.message_id)
    except Exception:
        pass

    # 兼容 v2/v3：forward_from_chat
    if channel_id is None and getattr(msg, "forward_from_chat", None):
        ch = msg.forward_from_chat
        if getattr(ch, "type", None) == "channel":
            channel_id = int(ch.id)
            message_id = int(getattr(msg, "forward_from_message_id", 0) or msg.message_id)

    if channel_id is None or message_id is None:
        await msg.reply(
            _tt("admin.covers.add_fail", lang, "❌ 上传失败：不是来自频道的转发消息。", "❌ Upload failed: not a forwarded channel message."),
            reply_markup=_kb([[ _btn(_tt("menu.back", lang, "⬅️ 返回", "⬅️ Back"), "admin:covers") ]]),
        )
        await state.clear()
        return

    file_id, media_type = _extract_media_from_message(msg)

    try:
        # 热迁移一次，避免缺列
        ensure_cover_schema()
        row = upsert_from_channel_post(
            channel_id=channel_id,
            message_id=message_id,
            file_id=file_id,
            media_type=media_type,
            enabled=True,
            creator_tg_id=msg.from_user.id,
        )
        await msg.reply(
            _tt("admin.covers.add_ok", lang, f"✅ 已添加红包封面 ID={row.id}", f"✅ Cover added ID={row.id}"),
            reply_markup=_kb([[ _btn(_tt("menu.back", lang, "⬅️ 返回", "⬅️ Back"), "admin:covers") ]]),
        )
    except Exception as e:
        log.exception("add cover failed")
        await msg.reply(
            _tt("admin.covers.add_fail", lang, f"❌ 上传失败：{e}", f"❌ Upload failed: {e}"),
            reply_markup=_kb([[ _btn(_tt("menu.back", lang, "⬅️ 返回", "⬅️ Back"), "admin:covers") ]]),
        )
    finally:
        await state.clear()

# ---------- 删除 / 列表 ----------

def _list_kb(rows: List[Cover], page: int, total: int, lang: str) -> InlineKeyboardMarkup:
    btn_rows: List[List[InlineKeyboardButton]] = []
    if not rows:
        btn_rows.append([_btn(_tt("admin.covers.empty", lang, "📭 暂无封面。", "📭 No covers."), "noop")])
    else:
        for r in rows:
            label = f"#{r.id} {(r.slug or '')}".strip()
            btn_rows.append([_btn(label, f"admin:covers:view:{r.id}"),
                             _btn("🗑", f"admin:covers:del:{r.id}"),
                             _btn("✅" if r.enabled else "🚫", f"admin:covers:toggle:{r.id}")])
    # 分页
    pages = max(1, (total + 9) // 10)
    nav = []
    if page > 1:
        nav.append(_btn(_tt("common.prev", lang, "« 上一页", "« Prev"), f"admin:covers:list:{page-1}"))
    if page < pages:
        nav.append(_btn(_tt("common.next", lang, "下一页 »", "Next »"), f"admin:covers:list:{page+1}"))
    if nav:
        btn_rows.append(nav)
    btn_rows.append([_btn(_tt("menu.back", lang, "⬅️ 返回", "⬅️ Back"), "admin:covers")])
    return _kb(btn_rows)

@router.callback_query(F.data == "admin:covers:del")
async def covers_list_for_delete(cb: CallbackQuery, state: FSMContext):
    if not _is_admin_uid(cb.from_user.id):
        await cb.answer("⛔ You have no permission.", show_alert=True)
        return
    await state.clear()
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    rows, total = list_covers(page=1, page_size=10, only_enabled=False)
    await cb.message.edit_text(
        _tt("admin.covers.list_title", lang, "📚 封面列表", "📚 Cover List"),
        reply_markup=_list_kb(rows, page=1, total=total, lang=lang),
        disable_web_page_preview=True,
    )
    await cb.answer()

@router.callback_query(F.data.startswith("admin:covers:list:"))
async def covers_list_paged(cb: CallbackQuery):
    if not _is_admin_uid(cb.from_user.id):
        await cb.answer("⛔ You have no permission.", show_alert=True)
        return
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    try:
        page = int(cb.data.split(":")[-1])
    except Exception:
        page = 1
    rows, total = list_covers(page=page, page_size=10, only_enabled=False)
    try:
        await cb.message.edit_reply_markup(reply_markup=_list_kb(rows, page=page, total=total, lang=lang))
    except TelegramBadRequest:
        # 旧消息无法仅改键盘，则整条重发
        await cb.message.edit_text(
            _tt("admin.covers.list_title", lang, "📚 封面列表", "📚 Cover List"),
            reply_markup=_list_kb(rows, page=page, total=total, lang=lang),
        )
    await cb.answer()

@router.callback_query(F.data.startswith("admin:covers:del:"))
async def covers_delete_one(cb: CallbackQuery):
    if not _is_admin_uid(cb.from_user.id):
        await cb.answer("⛔ You have no permission.", show_alert=True)
        return
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    try:
        cid = int(cb.data.split(":")[-1])
    except Exception:
        await cb.answer(_tt("common.bad_request", lang, "⚠️ 请求有误", "⚠️ Bad request"))
        return
    ok = delete_cover(cid)
    await cb.answer(_tt("admin.covers.delete_ok", lang, "✅ 已删除", "✅ Deleted") if ok else _tt("admin.covers.delete_fail", lang, "❌ 未找到", "❌ Not found"), show_alert=not ok)
    # 刷新列表（留在原页）
    rows, total = list_covers(page=1, page_size=10, only_enabled=False)
    await cb.message.edit_reply_markup(reply_markup=_list_kb(rows, page=1, total=total, lang=lang))

@router.callback_query(F.data.startswith("admin:covers:toggle:"))
async def covers_toggle_one(cb: CallbackQuery):
    if not _is_admin_uid(cb.from_user.id):
        await cb.answer("⛔ You have no permission.", show_alert=True)
        return
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    try:
        cid = int(cb.data.split(":")[-1])
    except Exception:
        await cb.answer(_tt("common.bad_request", lang, "⚠️ 请求有误", "⚠️ Bad request"))
        return
    row = get_cover_by_id(cid)
    if not row:
        await cb.answer(_tt("errors.not_found", lang, "🔍 未找到", "🔍 Not found"), show_alert=True)
        return
    set_cover_enabled(cid, not row.enabled)
    # 更新按钮
    rows, total = list_covers(page=1, page_size=10, only_enabled=False)
    await cb.message.edit_reply_markup(reply_markup=_list_kb(rows, page=1, total=total, lang=lang))
    await cb.answer(_tt("common.ok_emoji", lang, "👌", "👌"))

@router.callback_query(F.data.startswith("admin:covers:view:"))
async def covers_view_one(cb: CallbackQuery):
    if not _is_admin_uid(cb.from_user.id):
        await cb.answer("⛔ You have no permission.", show_alert=True)
        return
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    try:
        cid = int(cb.data.split(":")[-1])
    except Exception:
        await cb.answer(_tt("common.bad_request", lang, "⚠️ 请求有误", "⚠️ Bad request"))
        return
    row = get_cover_by_id(cid)
    if not row:
        await cb.answer(_tt("errors.not_found", lang, "🔍 未找到", "🔍 Not found"), show_alert=True)
        return

    # 预览：优先 copyMessage from channel
    try:
        await cb.message.bot.copy_message(
            chat_id=cb.message.chat.id,
            from_chat_id=row.channel_id,
            message_id=row.message_id,
        )
    except Exception:
        # 降级为发送 file_id
        try:
            if row.media_type == "photo" and row.file_id:
                await cb.message.bot.send_photo(cb.message.chat.id, row.file_id)
            elif row.media_type == "animation" and row.file_id:
                await cb.message.bot.send_animation(cb.message.chat.id, row.file_id)
            elif row.media_type == "video" and row.file_id:
                await cb.message.bot.send_video(cb.message.chat.id, row.file_id)
        except Exception:
            pass

    # 回到列表（保持当前页为 1 简化）
    rows, total = list_covers(page=1, page_size=10, only_enabled=False)
    try:
        await cb.message.edit_reply_markup(reply_markup=_list_kb(rows, page=1, total=total, lang=lang))
    except TelegramBadRequest:
        await cb.answer()
