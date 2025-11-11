# routers/admin.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import logging
import re
from typing import Optional, List, Tuple, Dict, Any, Sequence, Union
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)
    # ↑ aiogram 可能不稳定，反正这货见风使舵
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from sqlalchemy import func, and_, or_
from datetime import datetime, timedelta

from models.cover import list_covers, add_cover, delete_cover, get_cover_by_id
from config.settings import settings
# 变更点：引入 i18n 实例以获取“可用语言列表”
from core.i18n.i18n import t, i18n

log = logging.getLogger("admin")
router = Router(name="admin")

# =================== 键盘依赖 ===================
try:
    from core.utils.keyboards import admin_menu as _admin_menu_fn, back_home_kb as _back_home_kb_fn  # type: ignore
except Exception as e1:
    try:
        from keyboards import admin_menu as _admin_menu_fn, back_home_kb as _back_home_kb_fn  # type: ignore
    except Exception as e2:
        raise ImportError(
            f"Cannot import admin keyboards. Tried core.utils.keyboards and keyboards.py. "
            f"errors: {e1!r} ; {e2!r}"
        )

# is_admin 兼容两处
try:
    from settings import is_admin as _is_admin  # type: ignore
except Exception:
    from config.settings import is_admin as _is_admin  # type: ignore

# feature flags（可无）
try:
    from config import feature_flags as _ff  # type: ignore
except Exception:
    _ff = None  # type: ignore

from models.db import get_session
from models.user import User
from models.ledger import Ledger, LedgerType
from web_admin.services.audit_service import record_audit

# =================== 导出服务（向下兼容地引入） ===================
export_one_user_full = None
export_all_users_detail = None
export_all_users_and_ledger = None
export_user_records = None
# 新增：多用户导出（若你的 export_service 已实现）
export_users_full = None
export_some_users_and_ledger = None
try:
    from services.export_service import export_one_user_full as _new_one  # type: ignore
    from services.export_service import export_all_users_detail as _new_all_users  # type: ignore
    from services.export_service import export_all_users_and_ledger as _new_all_both  # type: ignore
    export_one_user_full = _new_one
    export_all_users_detail = _new_all_users
    export_all_users_and_ledger = _new_all_both
except Exception as e:
    log.info("new export (single/all) not found or failed to import: %s", e)

try:
    # 可能存在的“多用户导出”实现（新版）
    from services.export_service import export_users_full as _new_multi_full  # type: ignore
    export_users_full = _new_multi_full
except Exception as e:
    log.info("multi export export_users_full not found: %s", e)

try:
    # 另一种命名（有的项目叫 some_users_and_ledger）
    from services.export_service import export_some_users_and_ledger as _new_some  # type: ignore
    export_some_users_and_ledger = _new_some
except Exception as e:
    log.info("multi export export_some_users_and_ledger not found: %s", e)

try:
    from services.export_service import export_user_records as _old_user  # type: ignore
    from services.export_service import export_all_records as _old_all  # type: ignore
    export_user_records = _old_user
    export_all_records = _old_all
except Exception as e:
    log.exception("import legacy export functions failed: %s", e)

# =================== FSM 状态 ===================
class AdminExportStates(StatesGroup):
    waiting_user = State()  # 等待输入“用户ID或@用户名”（支持多个，以逗号/空格/换行分隔）

# ===== 新增：清零流程状态机（全体 / 指定） =====
class ResetStates(StatesGroup):
    confirm_all_1 = State()      # 全体清零：一级确认（按钮）
    confirm_all_2 = State()      # 全体清零：二级口令
    input_targets = State()      # 指定清零：输入目标
    preview_select = State()     # 指定清零：预览与确认


class ConfirmStates(StatesGroup):
    export_all = State()

# =================== 本地状态（封面上传仍用标记） ===================
_PENDING_UPLOAD: Dict[int, bool] = {}

# =================== 工具函数 ===================
def _canon_lang(code: Optional[str]) -> str:
    """
    动态规范化语言码（和 routers/menu.py 保持一致的脾气）：
      1) 读取 messages/*.yml 里的可用语言
      2) 完整命中直接用；否则取主标签（hi-IN -> hi）
      3) 历史兼容：zh*/en* 前缀照旧回退 zh/en
      4) 其他都回到 zh（毕竟默认就爱用中文）
    """
    if not code:
        return "zh"
    c = str(code).strip().lower().replace("_", "-")
    if not c:
        return "zh"

    try:
        available = set(i18n.available_languages() or [])
    except Exception:
        available = set()

    # 提前把常见目标语言加入，以防 messages 目录还没就绪时“自作聪明”退回中文
    available |= {"zh", "en", "fr", "de", "es", "hi", "vi", "th"}

    if c in available:
        return c
    primary = c.split("-", 1)[0]
    if primary in available:
        return primary
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"
    return "zh"

def _db_lang(user_id: int, fallback_user) -> str:
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u and getattr(u, "language", None):
            return _canon_lang(u.language)
    return _canon_lang(getattr(fallback_user, "language_code", None))

def _fmt6(v) -> str:
    try:
        return f"{float(v):.6f}"
    except Exception:
        return str(v)

def _admin_menu(lang: str) -> InlineKeyboardMarkup:
    if not _admin_menu_fn:
        raise RuntimeError("admin_menu keyboard function is not loaded.")
    return _admin_menu_fn(lang)  # type: ignore

def _back_home_kb(lang: str) -> InlineKeyboardMarkup:
    if not _back_home_kb_fn:
        raise RuntimeError("back_home_kb keyboard function is not loaded.")
    return _back_home_kb_fn(lang)  # type: ignore

def _t_first(keys: List[str], lang: str, fallback: str = "") -> str:
    for k in keys:
        try:
            v = t(k, lang)
            if v:
                return v
        except Exception:
            pass
    return fallback

async def _must_admin(cb_or_msg) -> bool:
    uid = cb_or_msg.from_user.id
    if _is_admin(uid):
        return True
    lang = _db_lang(uid, cb_or_msg.from_user)
    tip = _t_first(["admin.no_permission"], lang, "⛔ 你没有权限。")
    try:
        if hasattr(cb_or_msg, "message"):  # CallbackQuery
            await cb_or_msg.message.answer(tip, reply_markup=_back_home_kb(lang))
        else:  # Message
            await cb_or_msg.answer(tip, reply_markup=_back_home_kb(lang))
    except TelegramBadRequest:
        pass
    try:
        if hasattr(cb_or_msg, "answer"):
            await cb_or_msg.answer()
    except Exception:
        pass
    return False

# 安全地应答 callback，避免“query is too old”异常
async def _cb_safe_answer(cb: CallbackQuery, text: str | None = None, show_alert: bool = False):
    try:
        await cb.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest:
        pass
    except Exception:
        pass

# 解析 caption 的 hashtag：首个 #xxx 作为 slug，所有 #xxx 汇总为 tags（以逗号分隔）
_HASHTAG_RE = re.compile(r"#([\w\-@]+)", re.U)

def _extract_slug_tags(caption: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not caption:
        return None, None
    tags = _HASHTAG_RE.findall(caption)
    if not tags:
        return None, None
    slug = tags[0] if tags else None
    tags_str = ",".join(dict.fromkeys(tags))  # 去重保序
    return slug, tags_str

# ======================
#   多用户输入解析
# ======================
_SPLIT_RE = re.compile(r"[,\s；；，]+" )  # 逗号/空格/换行/中英文逗号分隔

def _split_targets(text: str) -> List[str]:
    parts = [p.strip() for p in _SPLIT_RE.split(text or "") if p.strip()]
    return list(dict.fromkeys(parts))  # 去重保序

def _is_int(s: str) -> bool:
    try:
        int(s)
        return True
    except Exception:
        return False

def _strip_at(s: str) -> str:
    return s[1:] if s and s.startswith("@") else s

def _resolve_targets_to_tg_ids(parts: Sequence[str]) -> Tuple[List[int], List[str]]:
    """
    把若干 <tg_id 或 @username> 解析成 tg_id 列表。
    返回: (tg_ids, unresolved_tokens)
    """
    tg_ids: List[int] = []
    usernames: List[str] = []
    unresolved: List[str] = []

    for p in parts:
        p0 = _strip_at(p)
        if _is_int(p0):
            try:
                tg_ids.append(int(p0))
            except Exception:
                unresolved.append(p)
        else:
            usernames.append(p0)

    # 去重
    tg_ids = list(dict.fromkeys(tg_ids))
    usernames = list(dict.fromkeys([u.lower() for u in usernames]))

    if usernames:
        with get_session() as s:
            rows = (
                s.query(User.username, User.tg_id)
                .filter(User.username.isnot(None))
                .filter(func.lower(User.username).in_(usernames))
                .all()
            )
        found_map = { (uname or "").lower(): int(tgid) for uname, tgid in rows }
        for u in usernames:
            if u in found_map:
                tg_ids.append(found_map[u])
            else:
                unresolved.append("@"+u)

    tg_ids = list(dict.fromkeys(tg_ids))
    return tg_ids, unresolved

# ======================
#   管理入口
# ======================
@router.message(F.text.regexp(r"^/admin$"))
async def cmd_admin(msg: Message, state: FSMContext):
    if not await _must_admin(msg):
        return
    await state.clear()
    lang = _db_lang(msg.from_user.id, msg.from_user)
    title = _t_first(["menu.admin"], lang, "🛠 管理面板")
    try:
        await msg.answer(title, parse_mode="HTML", reply_markup=_admin_menu(lang))
    except TelegramBadRequest:
        await msg.answer(title, reply_markup=_admin_menu(lang))

@router.callback_query(F.data == "admin:main")
async def admin_main(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()  # 返回主面板时清理所有状态
    lang = _db_lang(cb.from_user.id, cb.from_user)
    title = _t_first(["menu.admin"], lang, "🛠 管理面板")
    try:
        await cb.message.edit_text(title, parse_mode="HTML", reply_markup=_admin_menu(lang))
    except TelegramBadRequest:
        await cb.message.answer(title, parse_mode="HTML", reply_markup=_admin_menu(lang))
    await cb.answer()

# ======================
#   封面管理主入口
# ======================
@router.callback_query(F.data == "admin:covers")
async def admin_covers_entry(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    lang = _db_lang(cb.from_user.id, cb.from_user)
    text = _t_first(["admin.covers.menu_desc"], lang, "🎨 管理红包封面\n请选择操作：")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_t_first(["admin.covers.upload_btn"], lang, "➕ 上传封面"), callback_data="admin:covers:add")],
        [InlineKeyboardButton(text=_t_first(["admin.covers.list_btn"], lang, "📚 查看封面列表"), callback_data="admin:covers:list")],
        [InlineKeyboardButton(text=_t_first(["admin.covers.delete_btn"], lang, "🗑 删除封面"), callback_data="admin:covers:del")],
        [InlineKeyboardButton(text=_t_first(["menu.back"], lang, "⬅️ 返回"), callback_data="admin:main")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()

# ======================
#   上传封面流程
# ======================
@router.callback_query(F.data == "admin:covers:add")
async def admin_cover_add(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    lang = _db_lang(cb.from_user.id, cb.from_user)
    if not getattr(settings, "COVER_CHANNEL_ID", None):
        tip = _t_first(["admin.covers.no_channel"], lang, "❌ 尚未配置素材频道 ID（settings.COVER_CHANNEL_ID）。请先在配置中设置。")
        try:
            await cb.message.answer(tip)
        except TelegramBadRequest:
            pass
        await cb.answer()
        return
    _PENDING_UPLOAD[cb.from_user.id] = True
    ask = _t_first(["admin.covers.upload_ask"], lang, "请发送一张图片作为红包封面，可附加标题说明（支持 #标签 自动归档）。")
    try:
        await cb.message.answer(ask)
    except TelegramBadRequest:
        pass
    await cb.answer()

async def _handle_cover_upload(msg: Message, kind: str = "photo"):
    if not _PENDING_UPLOAD.get(msg.from_user.id):
        return
    if not await _must_admin(msg):
        return
    lang = _db_lang(msg.from_user.id, msg.from_user)

    if not getattr(settings, "COVER_CHANNEL_ID", None):
        tip = _t_first(["admin.covers.no_channel"], lang, "❌ 尚未配置素材频道 ID（settings.COVER_CHANNEL_ID）。请先在配置中设置。")
        try:
            await msg.answer(tip)
        except TelegramBadRequest:
            pass
        _PENDING_UPLOAD.pop(msg.from_user.id, None)
        return

    # 媒体 file_id
    file_id: Optional[str] = None
    try:
        if kind == "photo" and msg.photo:
            file_id = msg.photo[-1].file_id
        elif kind == "animation" and msg.animation:
            file_id = msg.animation.file_id
        elif kind == "document" and msg.document:
            file_id = msg.document.file_id
    except Exception:
        file_id = None

    caption = (msg.caption or "").strip() or None
    slug, tags = _extract_slug_tags(caption)

    # 推断媒体类型（便于以后筛选）
    media_type = "photo"
    if kind == "animation":
        media_type = "animation"
    elif kind == "document":
        # 尝试用 mime 判断是否 gif
        mime = (getattr(msg.document, "mime_type", "") or "").lower()
        name = (getattr(msg.document, "file_name", "") or "").lower()
        if name.endswith(".gif") or mime in ("image/gif", "video/mp4"):  # Telegram 的 GIF 有时走 mp4
            media_type = "animation"
        else:
            media_type = "document"

    # 复制到素材频道
    try:
        m = await msg.bot.copy_message(
            chat_id=settings.COVER_CHANNEL_ID,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id
        )
    except Exception as e:
        # 给出可能的原因，便于排障
        hint = _t_first(["admin.covers.copy_fail_hint"], lang,
                        "可能原因：\n• 机器人未加入该频道或没有“发布消息”权限；\n• 频道ID填写错误（应为以 -100 开头的数值ID）。")
        fail_txt = _t_first(["admin.covers.add_fail"], lang, "❌ 上传失败：{reason}").format(reason=str(e))
        try:
            await msg.answer(f"{fail_txt}\n\n{hint}")
        except TelegramBadRequest:
            pass
        _PENDING_UPLOAD.pop(msg.from_user.id, None)
        return

    # 入库：新签名优先，旧签名回退
    payload = dict(
        channel_id=settings.COVER_CHANNEL_ID,
        message_id=m.message_id,
        file_id=file_id or "",
        slug=slug,
        title=caption,
        creator_tg_id=msg.from_user.id,
        media_type=media_type,
        tags=tags
    )
    try:
        new = add_cover(**payload)  # 假如 cover.add_cover 支持这些字段
    except TypeError:
        # 旧签名回退（只传已有参数）
        new = add_cover(
            channel_id=settings.COVER_CHANNEL_ID,
            message_id=m.message_id,
            file_id=file_id or "",
            slug=slug,
            title=caption
        )

    ok_txt = _t_first(["admin.covers.add_ok"], lang, "✅ 已添加红包封面 ID={id}").format(id=getattr(new, "id", "?"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_t_first(["admin.covers.back_to_mgr"], lang, "⬅️ 返回封面管理"), callback_data="admin:covers")]
    ])
    try:
        await msg.answer(ok_txt, reply_markup=kb)
    except TelegramBadRequest:
        await msg.answer(ok_txt)
    finally:
        _PENDING_UPLOAD.pop(msg.from_user.id, None)

@router.message(F.photo)
async def admin_cover_upload_photo(msg: Message):
    await _handle_cover_upload(msg, kind="photo")

@router.message(F.animation)
async def admin_cover_upload_animation(msg: Message):
    await _handle_cover_upload(msg, kind="animation")

@router.message(F.document)
async def admin_cover_upload_document(msg: Message):
    mime = (getattr(msg.document, "mime_type", "") or "").lower()
    name = (getattr(msg.document, "file_name", "") or "").lower()
    ok = (
        mime.startswith("image/") or
        name.endswith(".gif") or
        (mime in ("application/octet-stream", "video/mp4") and name.endswith(".gif"))
    )
    if not ok:
        return
    await _handle_cover_upload(msg, kind="document")

# ======================
#   查看封面列表（分页） + 🔍预览
# ======================

def _covers_page(page: int, page_size: int) -> Tuple[List, int]:
    try:
        res = list_covers(page=page, page_size=page_size, only_enabled=True)  # type: ignore[arg-type]
    except TypeError:
        res = list_covers(page=page, page_size=page_size)
    total = None
    if isinstance(res, tuple) and len(res) == 2:
        items, total = res
    else:
        items = list(res)
    if total is None:
        total = page * page_size + (1 if len(items) == page_size else 0)
    return list(items), int(total)

@router.callback_query(F.data == "admin:covers:list")
async def admin_cover_list(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    await _show_covers_page(cb, page=1)

@router.callback_query(F.data.regexp(r"^admin:covers:list:(\d+)$"))
async def admin_cover_list_paged(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    m = re.match(r"^admin:covers:list:(\d+)$", cb.data or "")
    page = int(m.group(1)) if m else 1
    await _show_covers_page(cb, page=page)

async def _show_covers_page(cb: CallbackQuery, page: int, page_size: int = 10):
    lang = _db_lang(cb.from_user.id, cb.from_user)
    items, total = _covers_page(page, page_size)
    if not items:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=_t_first(["admin.covers.back_to_mgr"], lang, "⬅️ 返回封面管理"), callback_data="admin:covers")]
        ])
        txt = _t_first(["admin.covers.empty"], lang, "📭 暂无封面。")
        try:
            await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest:
            await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
        await cb.answer()
        return

    # 文本列表（保留原样）
    lines = [_t_first(["admin.covers.list_title"], lang, "📚 封面列表"), "────────────────"]
    item_buttons: List[List[InlineKeyboardButton]] = []
    for c in items:
        cid = getattr(c, "id", None)
        slug = getattr(c, "slug", None)
        title = getattr(c, "title", None)
        msg_id = getattr(c, "message_id", None)
        ch_id = getattr(c, "channel_id", None)
        media_type = getattr(c, "media_type", None)
        created = getattr(c, "created_at", None) or getattr(c, "created", None)
        if isinstance(created, datetime):
            dt_str = created.strftime("%Y-%m-%d %H:%M")
        else:
            dt_str = str(created) if created else "-"
        name = slug or title or f"#{msg_id}"
        src = f"{ch_id}/{msg_id}" if (ch_id and msg_id) else f"{msg_id or '-'}"
        type_badge = f" [{media_type}]" if media_type else ""
        lines.append(f"• #{cid} {name}{type_badge}  ——  {dt_str}  ({src})")

        # 每条记录对应一个“🔍 预览”按钮
        btn_text = f"🔍 预览  #{cid} {name[:28]}" if name else f"🔍 预览  #{cid}"
        item_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"admin:covers:preview:{cid}")])

    text = "\n".join(lines)

    # 分页/返回按钮
    rows: List[List[InlineKeyboardButton]] = []
    rows.extend(item_buttons)
    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton(text=_t_first(["common.prev"], lang, "« 上一页"), callback_data=f"admin:covers:list:{page-1}"))
    has_more = page * page_size < total
    if has_more:
        nav.append(InlineKeyboardButton(text=_t_first(["common.next"], lang, "下一页 »"), callback_data=f"admin:covers:list:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=_t_first(["admin.covers.back_to_mgr"], lang, "⬅️ 返回封面管理"), callback_data="admin:covers")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()

# 🔍 预览：复制频道消息到当前聊天；失败时使用 file_id 兜底
@router.callback_query(F.data.regexp(r"^admin:covers:preview:(\d+)$"))
async def admin_cover_preview(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    # 不强制清 FSM 状态，允许管理者预览后继续分页等操作
    lang = _db_lang(cb.from_user.id, cb.from_user)
    # 修复：回调命名空间应为冒号，不是点号
    m = re.match(r"^admin:covers:preview:(\d+)$", cb.data or "")
    if not m:
        await cb.answer("参数错误", show_alert=True)
        return
    cover_id = int(m.group(1))
    c = get_cover_by_id(cover_id)
    if not c:
        await cb.answer(_t_first(["admin.covers.not_found"], lang, "未找到该封面"), show_alert=True)
        return

    src_chat_id = getattr(c, "channel_id", None) or getattr(settings, "COVER_CHANNEL_ID", None)
    src_msg_id = getattr(c, "message_id", None)
    file_id = getattr(c, "file_id", None)
    media_type = (getattr(c, "media_type", None) or "").lower()

    # 优先 copyMessage
    try:
        if src_chat_id and src_msg_id:
            await cb.message.bot.copy_message(
                chat_id=cb.message.chat.id,
                from_chat_id=src_chat_id,
                message_id=src_msg_id,
            )
            await cb.answer(_t_first(["admin.covers.preview_ok"], lang, "已发送预览"))
            return
    except Exception as e:
        log.warning("copy_message failed for cover #%s: %s", cover_id, e)

    # 兜底：根据 file_id 直接发送
    try:
        if file_id:
            caption = getattr(c, "title", None) or f"封面 #{cover_id}"
            if media_type == "animation":
                await cb.message.answer_animation(file_id, caption=caption)
            else:
                # 默认按图片发送
                await cb.message.answer_photo(file_id, caption=caption)
            await cb.answer(_t_first(["admin.covers.preview_ok"], lang, "已发送预览"))
            return
    except Exception as e:
        log.warning("fallback send failed for cover #%s: %s", cover_id, e)

    # 仍失败：给出清晰提示
    hint = _t_first(["admin.covers.preview_fail_hint"], lang,
                    "可能原因：\n• 机器人未加入该频道或没有“发布消息”权限；\n• 频道ID填写错误；\n• 记录缺少有效的 file_id。")
    try:
        await cb.message.answer(_t_first(["admin.covers.preview_fail"], lang, "❌ 预览失败。") + "\n\n" + hint)
    except TelegramBadRequest:
        pass
    await cb.answer()

# ======================
#   删除封面（选择 + 执行）
# ======================
@router.callback_query(F.data == "admin:covers:del")
async def admin_cover_delete_entry(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    lang = _db_lang(cb.from_user.id, cb.from_user)
    try:
        res = list_covers(page=1, page_size=20, only_enabled=True)  # type: ignore[arg-type]
    except TypeError:
        res = list_covers(page=1, page_size=20)
    covers = res[0] if isinstance(res, tuple) else res
    covers = list(covers) if covers else []
    if not covers:
        txt = _t_first(["admin.covers.empty"], lang, "📭 暂无封面。")
        try:
            await cb.message.edit_text(txt)
        except TelegramBadRequest:
            await cb.message.answer(txt)
        await cb.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"#{getattr(c,'id',None)} {getattr(c,'slug',None) or getattr(c,'title',None) or getattr(c,'message_id',None)}",
            callback_data=f"admin:covers:del:{getattr(c,'id',None)}"
        )]
        for c in covers
    ] + [[InlineKeyboardButton(text=_t_first(["admin.covers.back_to_mgr"], lang, "⬅️ 返回封面管理"), callback_data="admin:covers")]])
    ask = _t_first(["admin.covers.delete_pick"], lang, "请选择要删除的封面：")
    try:
        await cb.message.edit_text(ask, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(ask, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.regexp(r"^admin:covers:del:(\d+)$"))
async def admin_cover_delete_do(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    lang = _db_lang(cb.from_user.id, cb.from_user)
    # 修复：命名空间应为冒号
    m = re.match(r"^admin:covers:del:(\d+)$", cb.data or "")
    cover_id = int(m.group(1))
    ok = delete_cover(cover_id)
    msg = _t_first(["admin.covers.delete_ok"], lang, "✅ 已删除红包封面 #{id}").format(id=cover_id) if ok else \
          _t_first(["admin.covers.delete_fail"], lang, "❌ 未找到红包封面 #{id}").format(id=cover_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_t_first(["admin.covers.back_to_mgr"], lang, "⬅️ 返回封面管理"), callback_data="admin:covers")]
    ])
    try:
        await cb.message.edit_text(msg, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(msg, reply_markup=kb)
    await cb.answer()

# ======================
#   统计 / 设置 / 开关
# ======================
@router.callback_query(F.data == "admin:stats")
async def admin_stats(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    lang = _db_lang(cb.from_user.id, cb.from_user)
    with get_session() as s:
        total_users = s.query(func.count(User.id)).scalar() or 0

    env_summary_lines: List[str] = []
    try:
        from models.envelope import Envelope  # type: ignore
        with get_session() as s:
            total_env = s.query(func.count(Envelope.id)).scalar() or 0
            ongoing = 0
            try:
                ongoing = (
                    s.query(func.count(Envelope.id))
                    .filter((Envelope.left_shares > 0))  # type: ignore[attr-defined]
                    .scalar() or 0
                )
            except Exception:
                pass
            env_summary_lines.append(_t_first(["admin.stats_total_env"], lang, "🧧 总红包：{n}").format(n=total_env))
            if ongoing:
                env_summary_lines.append(_t_first(["admin.stats_ongoing"], lang, "⏳ 进行中：{n}").format(n=ongoing))
    except Exception as e:
        log.debug("envelope stats skipped: %s", e)

    recharge_lines: List[str] = []
    with get_session() as s:
        rows = (
            s.query(Ledger.token, func.sum(Ledger.amount).label("sum_amt"))
            .filter(Ledger.type == LedgerType.RECHARGE)
            .group_by(Ledger.token)
            .all()
        )
        for token, sum_amt in rows:
            if (token or "").upper() == "POINT":
                try:
                    recharge_lines.append(f"➕ {token}: {int(sum_amt or 0)}")
                except Exception:
                    recharge_lines.append(f"➕ {token}: {sum_amt}")
            else:
                recharge_lines.append(f"➕ {token}: {_fmt6(sum_amt or 0)}")

    active_today = 0
    try:
        start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        with get_session() as s:
            active_today = (
                s.query(Ledger.user_tg_id)
                .filter(and_(Ledger.created_at >= start, Ledger.created_at < end))
                .distinct().count()
            )
    except Exception:
        pass

    title = _t_first(["admin.stats"], lang, "📈 统计概览")
    lines = [title, "────────────────"]
    lines.append(_t_first(["admin.stats_users"], lang, "👥 用户数：{n}").format(n=total_users))
    if env_summary_lines:
        lines.extend(env_summary_lines)
    if recharge_lines:
        lines.append(_t_first(["admin.stats_recharge"], lang, "💰 充值汇总："))
        lines.extend([f"• {x}" for x in recharge_lines])
    lines.append(_t_first(["admin.stats_active_today"], lang, "🔥 今日活跃用户：{n}").format(n=active_today))
    text = "\n".join(lines)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_admin_menu(lang))
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=_admin_menu(lang))
    await cb.answer()

@router.callback_query(F.data == "admin:settings")
async def admin_settings(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    lang = _db_lang(cb.from_user.id, cb.from_user)
    show_keys = [
        "ENABLE_SIGNIN","SIGNIN_REWARD_POINTS","ENABLE_INVITE","ENABLE_EXCHANGE",
        "POINTS_PER_PROGRESS","ENERGY_REWARD_AT_PROGRESS","ENERGY_REWARD_AMOUNT",
        "ENERGY_TO_POINTS_RATIO","ENERGY_TO_POINTS_VALUE",
        "RECHARGE_EXPIRE_MINUTES",
        "WITHDRAW_MIN_USDT","WITHDRAW_MIN_TON","WITHDRAW_FEE_USDT","WITHDRAW_FEE_TON",
        "RECHARGE_QUICK_AMOUNTS",
    ]
    kv_lines: List[str] = []
    try:
        flags = getattr(_ff, "flags", None) if _ff else None
        for k in show_keys:
            if flags is not None and k in flags:
                kv_lines.append(f"• {k} = {flags.get(k)}")
    except Exception:
        pass
    title = _t_first(["admin.settings"], lang, "⚙️ 系统设置")
    text = f"{title}\n────────────────\n" + ("\n".join(kv_lines) if kv_lines else "(no flags)")
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_admin_menu(lang))
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=_admin_menu(lang))
    await cb.answer()

def _toggle_first_available_flag() -> Tuple[str, Optional[bool], Optional[str]]:
    """
    在 flags 中按顺序找到第一个布尔开关并取反。
    返回: (flag_name, new_value, error_message|None)
    """
    flags = getattr(_ff, "flags", None) if _ff else None
    if flags is None:
        return "N/A", None, "flags_not_available"
    for name in ["AUTO_MODE", "ENABLE_SIGNIN", "ENABLE_INVITE", "ENABLE_EXCHANGE"]:
        if name in flags:
            v = flags.get(name)
            if isinstance(v, bool):
                try:
                    flags[name] = (not v)
                    return name, (not v), None
                except Exception as e:
                    return name, None, f"frozen ({e.__class__.__name__})"
    return "N/A", None, "no_boolean_flag"

@router.callback_query(F.data == "admin:toggle")
async def admin_toggle(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    lang = _db_lang(cb.from_user.id, cb.from_user)
    name, val, err = _toggle_first_available_flag()
    if val is None:
        label = _t_first(["admin.toggle_auto"], lang, "🔁 开关自动模式")
        if err == "no_boolean_flag":
            msg = f"{label}\n────────────────\n(no boolean flag available)"
        elif err == "flags_not_available":
            msg = f"{label}\n────────────────\n(flags object not available)"
        else:
            msg = f"{label}\n────────────────\n{name}: cannot toggle ({err})"
    else:
        state_txt = "ON ✅" if val else "OFF ⛔"
        label = _t_first(["admin.toggle_auto"], lang, "🔁 开关自动模式")
        msg = f"{label}\n────────────────\n{name} → {state_txt}"
    try:
        await cb.message.edit_text(msg, parse_mode="HTML", reply_markup=_admin_menu(lang))
    except TelegramBadRequest:
        await cb.message.answer(msg, parse_mode="HTML", reply_markup=_admin_menu(lang))
    await cb.answer()

# ===========================
#         导出交互（FSM）
# ===========================
@router.callback_query(F.data == "admin:export")
async def admin_export_entry(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    lang = _db_lang(cb.from_user.id, cb.from_user)
    text = _t_first(["admin.export_intro"], lang, "请选择导出范围")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_t_first(["admin.export_user"], lang, "👤 按用户导出"), callback_data="admin:export:user")],
        [InlineKeyboardButton(text=_t_first(["admin.export_all"], lang, "📊 全量导出"), callback_data="admin:export:all")],
        [InlineKeyboardButton(text=_t_first(["menu.back"], lang, "⬅️ 返回"), callback_data="admin:main")],
    ])
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "admin:export:user")
async def admin_export_user_ask(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.set_state(AdminExportStates.waiting_user)
    lang = _db_lang(cb.from_user.id, cb.from_user)
    text = _t_first(["admin.ask_user", "admin.adjust.ask_user"], lang,
                    "👤 请输入目标用户（支持多个）：\n• 可输入 用户ID 或 @用户名\n• 以 逗号/空格/换行 分隔")
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_back_home_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=_back_home_kb(lang))
    await cb.answer()

# 便捷命令：/u 也支持多个
@router.message(F.text.regexp(r"^/u(?:\s+.+)?$"))
async def admin_export_user_cmd(msg: Message, state: FSMContext):
    """快捷命令：/u <id|@用户名|多项分隔> —— 与 FSM 无关，直接导出"""
    if not await _must_admin(msg):
        return
    await state.clear()
    lang = _db_lang(msg.from_user.id, msg.from_user)
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await msg.answer(_t_first(["admin.ask_user"], lang, "👤 用法：/u <用户ID或@用户名，支持多个>"))
        return
    query = parts[1].strip()

    segs = _split_targets(query)
    tg_ids, unresolved = _resolve_targets_to_tg_ids(segs)
    if not tg_ids:
        tip = _t_first(["admin.export_empty"], lang, "📭 未解析到有效用户。")
        if unresolved:
            tip += "\n" + _t_first(["admin.unresolved"], lang, "未识别：") + " " + ", ".join(unresolved)
        await msg.answer(tip, reply_markup=_back_home_kb(lang))
        return

    tip = _t_first(["admin.generating", "help.thinking"], lang, "⏳ 正在生成，请稍候…")
    await msg.answer(tip)

    # 调用导出服务（优先多用户 → 单用户循环兜底）
    paths: Union[str, List[str], None] = None
    try:
        if export_users_full is not None:
            # 新版：一个 Excel 内含 Users + Ledger（只含所选用户）
            paths = export_users_full(tg_ids=tg_ids, fmt="xlsx")  # type: ignore[operator]
        elif export_some_users_and_ledger is not None:
            paths = export_some_users_and_ledger(tg_ids=tg_ids, fmt="xlsx")  # type: ignore[operator]
        elif export_one_user_full is not None:
            # 兜底：逐个导出单人版（每人 1 个文件）
            many: List[str] = []
            for uid in tg_ids:
                p = export_one_user_full(str(uid), fmt="xlsx")  # type: ignore[operator]
                if p:
                    many.append(p)
            paths = many
        elif export_user_records is not None:
            # 更旧的兜底：只导流水
            many: List[str] = []
            for uid in tg_ids:
                p = export_user_records(user_id_or_username=str(uid), start=None, end=None, tokens=None, types=None, fmt="xlsx")  # type: ignore[operator]
                if p:
                    many.append(p)
            paths = many
        else:
            paths = None
    except Exception as e:
        record_audit("export_user", msg.from_user.id, {"status": "error", "error": str(e), "targets": tg_ids})
        await msg.answer(_t_first(["admin.export_failed"], lang, "❌ 生成失败：{reason}").format(reason=str(e)), reply_markup=_back_home_kb(lang))
        return

    # 发送文件
    if not paths or (isinstance(paths, list) and not paths):
        tip = _t_first(["admin.export_empty"], lang, "📭 查无数据")
        if unresolved:
            tip += "\n" + _t_first(["admin.unresolved"], lang, "未识别：") + " " + ", ".join(unresolved)
        await msg.answer(tip, reply_markup=_back_home_kb(lang))
        record_audit("export_user", msg.from_user.id, {"status": "empty", "targets": tg_ids})
        return

    file_count = 1 if isinstance(paths, str) else len(paths)
    record_audit("export_user", msg.from_user.id, {"status": "success", "targets": tg_ids, "files": file_count})

    if isinstance(paths, str):
        try:
            await msg.answer_document(FSInputFile(paths), caption=_t_first(["admin.export_done"], lang, "✅ 导出完成，文件如下："))
        except Exception as e:
            await msg.answer(_t_first(["admin.send_file_failed"], lang, "❌ 发送文件失败：{reason}").format(reason=str(e)), reply_markup=_back_home_kb(lang))
    else:
        # 多个文件（单人兜底模式）
        for idx, p in enumerate(paths, 1):
            cap = _t_first(["admin.export_done"], lang, "✅ 导出完成，文件如下：") + f"  ({idx}/{len(paths)})"
            try:
                await msg.answer_document(FSInputFile(p), caption=cap)
            except Exception as e:
                await msg.answer(_t_first(["admin.send_file_failed"], lang, "❌ 发送文件失败：{reason}").format(reason=str(e)), reply_markup=_back_home_kb(lang))

# 仅当处于 FSM 等待态时，才接收文本；不会再抢占 /start 等任意文本
@router.message(AdminExportStates.waiting_user, F.text)
async def admin_export_user_capture(msg: Message, state: FSMContext):
    lang = _db_lang(msg.from_user.id, msg.from_user)
    if not await _must_admin(msg):
        await state.clear()
        return

    query = (msg.text or "").strip()
    if not query:
        # 留在等待态，允许继续输入
        await msg.answer(_t_first(["admin.ask_user", "admin.adjust.ask_user"], lang,
                                  "👤 请输入目标用户（支持多个）：\n• 可输入 用户ID 或 @用户名\n• 以 逗号/空格/换行 分隔"),
                         reply_markup=_back_home_kb(lang))
        return

    # 服务可用性判断（新增优先，旧版回退）
    if (export_users_full is None and export_some_users_and_ledger is None and
        export_one_user_full is None and export_user_records is None):
        await state.clear()
        await msg.answer(_t_first(["admin.export_service_missing"], lang, "❌ 导出服务不可用"), reply_markup=_back_home_kb(lang))
        return

    segs = _split_targets(query)
    tg_ids, unresolved = _resolve_targets_to_tg_ids(segs)
    if not tg_ids:
        tip = _t_first(["admin.export_empty"], lang, "📭 未解析到有效用户。")
        if unresolved:
            tip += "\n" + _t_first(["admin.unresolved"], lang, "未识别：") + " " + ", ".join(unresolved)
        await msg.answer(tip, reply_markup=_back_home_kb(lang))
        return

    tip = _t_first(["admin.generating", "help.thinking"], lang, "⏳ 正在生成，请稍候…")
    await msg.answer(tip)

    # 调用导出服务（优先多用户 → 单用户循环兜底）
    try:
        if export_users_full is not None:
            path_or_paths = export_users_full(tg_ids=tg_ids, fmt="xlsx")  # type: ignore[operator]
        elif export_some_users_and_ledger is not None:
            path_or_paths = export_some_users_and_ledger(tg_ids=tg_ids, fmt="xlsx")  # type: ignore[operator]
        elif export_one_user_full is not None:
            many: List[str] = []
            for uid in tg_ids:
                p = export_one_user_full(str(uid), fmt="xlsx")  # type: ignore[operator]
                if p:
                    many.append(p)
            path_or_paths = many
        else:
            many: List[str] = []
            for uid in tg_ids:
                p = export_user_records(user_id_or_username=str(uid), start=None, end=None, tokens=None, types=None, fmt="xlsx")  # type: ignore[operator]
                if p:
                    many.append(p)
            path_or_paths = many
    except Exception as e:
        record_audit("export_user", msg.from_user.id, {"status": "error", "error": str(e), "targets": tg_ids})
        await state.clear()
        await msg.answer(_t_first(["admin.export_failed"], lang, "❌ 生成失败：{reason}").format(reason=str(e)), reply_markup=_back_home_kb(lang))
        return

    if not path_or_paths or (isinstance(path_or_paths, list) and not path_or_paths):
        # 没数据也不清状态，允许继续重试
        tip2 = _t_first(["admin.export_empty"], lang, "📭 查无数据")
        if unresolved:
            tip2 += "\n" + _t_first(["admin.unresolved"], lang, "未识别：") + " " + ", ".join(unresolved)
        await msg.answer(tip2, reply_markup=_back_home_kb(lang))
        record_audit("export_user", msg.from_user.id, {"status": "empty", "targets": tg_ids})
        return

    # 成功：清状态并发送
    await state.clear()
    file_count = 1 if isinstance(path_or_paths, str) else len(path_or_paths)
    record_audit("export_user", msg.from_user.id, {"status": "success", "targets": tg_ids, "files": file_count})
    if isinstance(path_or_paths, str):
        try:
            await msg.answer_document(FSInputFile(path_or_paths), caption=_t_first(["admin.export_done"], lang, "✅ 导出完成，文件如下："))
        except Exception as e:
            await msg.answer(_t_first(["admin.send_file_failed"], lang, "❌ 发送文件失败：{reason}").format(reason=str(e)), reply_markup=_back_home_kb(lang))
    else:
        for idx, p in enumerate(path_or_paths, 1):
            cap = _t_first(["admin.export_done"], lang, "✅ 导出完成，文件如下：") + f"  ({idx}/{len(path_or_paths)})"
            try:
                await msg.answer_document(FSInputFile(p), caption=cap)
            except Exception as e:
                await msg.answer(_t_first(["admin.send_file_failed"], lang, "❌ 发送文件失败：{reason}").format(reason=str(e)), reply_markup=_back_home_kb(lang))

@router.callback_query(F.data == "admin:export:all")
async def admin_export_all(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.clear()
    lang = _db_lang(cb.from_user.id, cb.from_user)

    # 关键：先安全应答，避免长时间生成导致 “query is too old”
    await _cb_safe_answer(cb)

    if export_all_users_and_ledger is None and export_all_users_detail is None and export_all_records is None:
        try:
            await cb.message.answer(_t_first(["admin.export_service_missing"], lang, "❌ 导出服务不可用"), reply_markup=_back_home_kb(lang))
        except TelegramBadRequest:
            pass
        return

    tip = _t_first(["admin.generating", "help.thinking"], lang, "⏳ 正在生成，请稍候…")
    try:
        await cb.message.answer(tip)
    except TelegramBadRequest:
        pass

    await state.set_state(ConfirmStates.export_all)
    await cb.message.answer(
        _t_first(["admin.export_all.confirm"], lang, "⚠️ 导出全量数据，确认继续？"),
        reply_markup=_confirm_action_kb(lang, "export_all"),
    )


@router.callback_query(ConfirmStates.export_all, F.data == "admin:confirm:export_all")
async def admin_export_all_confirm(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    lang = _db_lang(cb.from_user.id, cb.from_user)
    await _cb_safe_answer(cb)
    await state.clear()

    sent_any = False
    files_sent = 0
    try:
        if export_all_users_and_ledger is not None:
            path = export_all_users_and_ledger(fmt="xlsx")  # type: ignore[operator]
            if path:
                try:
                    await cb.message.answer_document(FSInputFile(path), caption=_t_first(["admin.export_done"], lang, "✅ 导出完成，文件如下："))
                    sent_any = True
                    files_sent += 1
                except Exception:
                    pass
        else:
            users_path = export_all_users_detail(fmt="xlsx") if export_all_users_detail else None  # type: ignore[operator]
            if users_path:
                try:
                    await cb.message.answer_document(FSInputFile(users_path), caption=_t_first(["admin.export_done"], lang, "✅ 用户明细已导出："))
                    sent_any = True
                    files_sent += 1
                except Exception:
                    pass
            ledgers_path = export_all_records(start=None, end=None, tokens=None, types=None, fmt="xlsx") if export_all_records else None  # type: ignore[operator]
            if ledgers_path:
                try:
                    await cb.message.answer_document(FSInputFile(ledgers_path), caption=_t_first(["admin.export_done"], lang, "✅ 全部流水已导出："))
                    sent_any = True
                    files_sent += 1
                except Exception:
                    pass
    except Exception as e:
        record_audit("export_all", cb.from_user.id, {"status": "error", "error": str(e)})
        try:
            await cb.message.answer(_t_first(["admin.export_failed"], lang, "❌ 生成失败：{reason}").format(reason=str(e)), reply_markup=_back_home_kb(lang))
        except TelegramBadRequest:
            pass
        return

    if not sent_any:
        record_audit("export_all", cb.from_user.id, {"status": "empty"})
        try:
            await cb.message.answer(_t_first(["admin.export_empty"], lang, "📭 查无数据"), reply_markup=_back_home_kb(lang))
        except TelegramBadRequest:
            pass
        return

    record_audit("export_all", cb.from_user.id, {"status": "success", "files": files_sent})

# ===================================================================
#                    新增：余额“清零”系列（两个按钮）
# ===================================================================

# 约定：在 balance.py 实现以下服务；当前路由仅做交互与权限控制
reset_all_balances = None
reset_selected_balances = None
# 修复：优先尝试根级 balance.py，失败再回退 routers.balance
try:
    from balance import reset_all_balances as _reset_all_balances  # type: ignore
    from balance import reset_selected_balances as _reset_selected_balances  # type: ignore
    reset_all_balances = _reset_all_balances
    reset_selected_balances = _reset_selected_balances
except Exception as e1:
    try:
        from routers.balance import reset_all_balances as _reset_all_balances  # type: ignore
        from routers.balance import reset_selected_balances as _reset_selected_balances  # type: ignore
        reset_all_balances = _reset_all_balances
        reset_selected_balances = _reset_selected_balances
    except Exception as e2:
        log.info("reset services not available. errors: %r ; %r", e1, e2)

# 安全短语
_CONFIRM_PHRASE_ALL = "RESET ALL"
_CONFIRM_PHRASE_ALL_ZH = "我确认清零"
_CONFIRM_PHRASE_SELECT = "RESET SELECT"

def _danger_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ 我知道后果", callback_data="admin:reset_all:ack")],
        [InlineKeyboardButton(text=_t_first(["menu.back"], lang, "⬅️ 返回"), callback_data="admin:main")],
    ])

def _select_confirm_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ 确认清零", callback_data="admin:reset_select:confirm")],
        [InlineKeyboardButton(text="↩️ 重新输入", callback_data="admin:reset_select:retry")],
        [InlineKeyboardButton(text=_t_first(["menu.back"], lang, "⬅️ 返回"), callback_data="admin:main")],
    ])


def _confirm_action_kb(lang: str, action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t_first(["common.confirm"], lang, "✅ 确认"),
                    callback_data=f"admin:confirm:{action}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t_first(["common.cancel"], lang, "取消"),
                    callback_data="admin:main",
                )
            ],
        ]
    )

# 入口按钮：全体清零
@router.callback_query(F.data == "admin:reset_all")
async def admin_reset_all_entry(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.set_state(ResetStates.confirm_all_1)
    lang = _db_lang(cb.from_user.id, cb.from_user)

    if getattr(settings, "ALLOW_RESET", False) is not True:
        try:
            await cb.message.edit_text(_t_first(["admin.reset.disabled"], lang, "❌ 当前环境未开启清零功能（ALLOW_RESET=false）"),
                                       reply_markup=_back_home_kb(lang))
        except TelegramBadRequest:
            await cb.message.answer(_t_first(["admin.reset.disabled"], lang, "❌ 当前环境未开启清零功能（ALLOW_RESET=false）"),
                                    reply_markup=_back_home_kb(lang))
        await cb.answer()
        return

    text = (
        "⚠️ 批量清零（全体）\n"
        "此操作将把所有用户的 USDT/TON/积分余额归零，不可撤销。\n"
        "继续前请确认你真的不是在做梦。"
    )
    try:
        await cb.message.edit_text(text, reply_markup=_danger_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=_danger_kb(lang))
    await cb.answer()

@router.callback_query(ResetStates.confirm_all_1, F.data == "admin:reset_all:ack")
async def admin_reset_all_ask_phrase(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.set_state(ResetStates.confirm_all_2)
    lang = _db_lang(cb.from_user.id, cb.from_user)
    text = (
        "请在一条消息中输入确认短语继续：\n"
        f"• `{_CONFIRM_PHRASE_ALL}`  或  `{_CONFIRM_PHRASE_ALL_ZH}`\n"
        "可在同一条消息中附带备注（自由文本）。"
    )
    try:
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=_back_home_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="Markdown", reply_markup=_back_home_kb(lang))
    await cb.answer()

@router.message(ResetStates.confirm_all_2, F.text)
async def admin_reset_all_do(msg: Message, state: FSMContext):
    if not await _must_admin(msg):
        await state.clear()
        return
    lang = _db_lang(msg.from_user.id, msg.from_user)
    raw = (msg.text or "").strip()
    if not (raw.startswith(_CONFIRM_PHRASE_ALL) or raw.startswith(_CONFIRM_PHRASE_ALL_ZH)):
        await state.clear()
        await msg.answer(_t_first(["admin.reset.all.confirm_phrase.invalid"], lang, "❌ 口令不正确，已取消。"),
                         reply_markup=_back_home_kb(lang))
        return

    if reset_all_balances is None:
        await state.clear()
        await msg.answer("⚠️ 功能未启用：balance.reset_all_balances 未实现。",
                         reply_markup=_back_home_kb(lang))
        return

    # 备注在确认短语后面
    note = raw[len(_CONFIRM_PHRASE_ALL):].strip() if raw.startswith(_CONFIRM_PHRASE_ALL) else raw[len(_CONFIRM_PHRASE_ALL_ZH):].strip()
    try:
        result = reset_all_balances(note=note, operator_id=msg.from_user.id)  # type: ignore[misc]
    except Exception as e:
        record_audit("reset_all_balances", msg.from_user.id, {"status": "error", "error": str(e)})
        await state.clear()
        await msg.answer(f"❌ 执行失败：{e}", reply_markup=_back_home_kb(lang))
        return

    await state.clear()
    # 期望返回：affected_users, usdt_total, ton_total, point_total, batch_id, elapsed(optional)
    lines = [
        "✅ 批量清零完成",
        f"批次：{result.get('batch_id','-')}",
        f"受影响用户：{result.get('affected_users',0)}",
        "总扣减：",
        f" • USDT: {result.get('usdt_total','0')}",
        f" • TON:  {result.get('ton_total','0')}",
        f" • 积分: {result.get('point_total','0')}",
    ]
    if result.get("elapsed"):
        lines.append(f"耗时：{result['elapsed']}")
    await msg.answer("\n".join(lines), reply_markup=_back_home_kb(lang))
    record_audit("reset_all_balances", msg.from_user.id, {"status": "success", **result})

# 入口按钮：指定清零
@router.callback_query(F.data == "admin:reset_select")
async def admin_reset_select_entry(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.set_state(ResetStates.input_targets)
    lang = _db_lang(cb.from_user.id, cb.from_user)

    if getattr(settings, "ALLOW_RESET", False) is not True:
        try:
            await cb.message.edit_text(_t_first(["admin.reset.disabled"], lang, "❌ 当前环境未开启清零功能（ALLOW_RESET=false）"),
                                       reply_markup=_back_home_kb(lang))
        except TelegramBadRequest:
            await cb.message.answer(_t_first(["admin.reset.disabled"], lang, "❌ 当前环境未开启清零功能（ALLOW_RESET=false）"),
                                    reply_markup=_back_home_kb(lang))
        await cb.answer()
        return

    ask = _t_first(["admin.reset.select.input"], lang,
                   "请输入需要清零的目标（用户ID或@用户名），支持多个，逗号/空格/分号/换行分隔：")
    try:
        await cb.message.edit_text(ask, parse_mode="HTML", reply_markup=_back_home_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(ask, parse_mode="HTML", reply_markup=_back_home_kb(lang))
    await cb.answer()

@router.message(ResetStates.input_targets, F.text)
async def admin_reset_select_parse(msg: Message, state: FSMContext):
    if not await _must_admin(msg):
        await state.clear()
        return
    lang = _db_lang(msg.from_user.id, msg.from_user)

    segs = _split_targets(msg.text or "")
    if not segs:
        await msg.answer(_t_first(["admin.reset.select.input"], lang,
                                  "请输入需要清零的目标（用户ID或@用户名），支持多个，逗号/空格/分号/换行分隔："),
                         reply_markup=_back_home_kb(lang))
        return

    # 限流：最多 200 人一批
    if len(segs) > 200:
        segs = segs[:200]

    tg_ids, unresolved = _resolve_targets_to_tg_ids(segs)
    if not tg_ids:
        tip = "📭 未解析到有效用户。"
        if unresolved:
            tip += "\n未识别：" + ", ".join(unresolved[:10]) + ("…" if len(unresolved) > 10 else "")
        await msg.answer(tip, reply_markup=_back_home_kb(lang))
        return

    sample = ", ".join(map(str, tg_ids[:5])) + ("…" if len(tg_ids) > 5 else "")
    lines = [
        f"🧹 将清零 {len(tg_ids)} 人",
        f"样例：{sample}",
    ]
    if unresolved:
        lines.append("未解析：")
        lines.append(", ".join(unresolved[:6]) + ("…" if len(unresolved) > 6 else ""))
    lines.append("—— 请确认。")

    await state.update_data(target_ids=tg_ids)
    await state.set_state(ResetStates.preview_select)
    await msg.answer("\n".join(lines), reply_markup=_select_confirm_kb(lang))

@router.callback_query(ResetStates.preview_select, F.data == "admin:reset_select:retry")
async def admin_reset_select_retry(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    await state.set_state(ResetStates.input_targets)
    lang = _db_lang(cb.from_user.id, cb.from_user)
    ask = _t_first(["admin.reset.select.input"], lang,
                   "请输入需要清零的目标（用户ID或@用户名），支持多个，逗号/空格/分号/换行分隔：")
    try:
        await cb.message.edit_text(ask, parse_mode="HTML", reply_markup=_back_home_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(ask, parse_mode="HTML", reply_markup=_back_home_kb(lang))
    await cb.answer()

@router.callback_query(ResetStates.preview_select, F.data == "admin:reset_select:confirm")
async def admin_reset_select_do(cb: CallbackQuery, state: FSMContext):
    if not await _must_admin(cb):
        return
    lang = _db_lang(cb.from_user.id, cb.from_user)
    data = await state.get_data()
    tg_ids: List[int] = list(map(int, data.get("target_ids") or []))

    if not tg_ids:
        await state.clear()
        await cb.message.edit_text("📭 没有可操作的用户。", reply_markup=_back_home_kb(lang))
        await cb.answer()
        return

    if reset_selected_balances is None:
        await state.clear()
        try:
            await cb.message.edit_text("⚠️ 功能未启用：balance.reset_selected_balances 未实现。", reply_markup=_back_home_kb(lang))
        except TelegramBadRequest:
            await cb.message.answer("⚠️ 功能未启用：balance.reset_selected_balances 未实现。", reply_markup=_back_home_kb(lang))
        await cb.answer()
        return

    await _cb_safe_answer(cb)
    try:
        result = reset_selected_balances(user_ids=tg_ids, note="", operator_id=cb.from_user.id)  # type: ignore[misc]
    except Exception as e:
        record_audit("reset_selected_balances", cb.from_user.id, {"status": "error", "error": str(e), "targets": tg_ids})
        await state.clear()
        try:
            await cb.message.edit_text(f"❌ 执行失败：{e}", reply_markup=_back_home_kb(lang))
        except TelegramBadRequest:
            await cb.message.answer(f"❌ 执行失败：{e}", reply_markup=_back_home_kb(lang))
        await cb.answer()
        return

    await state.clear()
    ok = result.get("success_count", 0)
    fail = result.get("fail_count", 0)
    batch = result.get("batch_id", "-")
    lines = [
        "✅ 指定清零完成",
        f"批次：{batch}",
        f"成功：{ok}",
        f"失败：{fail}",
    ]
    if result.get("totals"):
        totals = result["totals"]
        lines.append("总扣减：")
        lines.append(f" • USDT: {totals.get('USDT','0')}")
        lines.append(f" • TON:  {totals.get('TON','0')}")
        lines.append(f" • 积分: {totals.get('POINT','0')}")
    if result.get("errors_by_user"):
        errs = result["errors_by_user"]
        keys = list(errs.keys())[:10]
        if keys:
            lines.append("失败样例：")
            for uid in keys:
                lines.append(f" • {uid}: {errs[uid]}")
            if len(errs) > 10:
                lines.append("…")
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=_back_home_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer("\n".join(lines), reply_markup=_back_home_kb(lang))
    await cb.answer()
    record_audit("reset_selected_balances", cb.from_user.id, {"status": "success", **result})

# 便捷命令：/reset_select <ids or @>
@router.message(F.text.regexp(r"^/reset_select(?:\s+.+)?$"))
async def reset_select_cmd(msg: Message, state: FSMContext):
    if not await _must_admin(msg):
        return
    await state.clear()
    lang = _db_lang(msg.from_user.id, msg.from_user)
    if getattr(settings, "ALLOW_RESET", False) is not True:
        await msg.answer(_t_first(["admin.reset.disabled"], lang, "❌ 当前环境未开启清零功能（ALLOW_RESET=false）"),
                         reply_markup=_back_home_kb(lang))
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("用法：/reset_select <用户ID或@用户名，支持多个>")
        return
    segs = _split_targets(parts[1])
    segs = segs[:200]
    tg_ids, unresolved = _resolve_targets_to_tg_ids(segs)
    if not tg_ids:
        tip = "📭 未解析到有效用户。"
        if unresolved:
            tip += "\n未识别：" + ", ".join(unresolved[:10]) + ("…" if len(unresolved) > 10 else "")
        await msg.answer(tip)
        return
    if reset_selected_balances is None:
        await msg.answer("⚠️ 功能未启用：balance.reset_selected_balances 未实现。")
        return
    try:
        result = reset_selected_balances(user_ids=tg_ids, note="", operator_id=msg.from_user.id)  # type: ignore[misc]
    except Exception as e:
        record_audit("reset_selected_balances", msg.from_user.id, {"status": "error", "error": str(e), "targets": tg_ids})
        await msg.answer(f"❌ 执行失败：{e}")
        return
    ok = result.get("success_count", 0)
    fail = result.get("fail_count", 0)
    batch = result.get("batch_id", "-")
    await msg.answer(f"✅ 指定清零完成\n批次：{batch}\n成功：{ok}\n失败：{fail}")
    record_audit("reset_selected_balances", msg.from_user.id, {"status": "success", **result})
