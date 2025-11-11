# routers/welcome.py
# -*- coding: utf-8 -*-
"""
首次使用欢迎流程（先欢迎→再菜单 版）：
 - 监听 /start（仅私聊；无 payload）
 - **先发欢迎封面（或欢迎文字）**，再发送主菜单
 - 欢迎封面图：file_id 缓存 + 超时与重试（既尽量快，又保证可靠）
 - 找不到封面图片时，自动发送纯文字版本
 - 其它逻辑/注释/行为尽量与原有工程保持一致
"""

from __future__ import annotations
import logging
import os
import json
import asyncio
from typing import Optional, Dict

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile
from aiogram.exceptions import TelegramBadRequest

from core.i18n.i18n import t
from core.utils.keyboards import main_menu  # ✅ 主菜单键盘
from config.settings import is_admin as _is_admin  # ✅ 判断是否管理员，决定是否显示“管理面板”
from models.db import get_session
from models.user import User, get_or_create_user

router = Router()
log = logging.getLogger("welcome_router")
PRIORITY_FIRST = True  # 仅作标记；实际优先级以 app 的 include 顺序为准

# ======== 媒体 file_id 缓存（持久化到 assets/.media_cache.json） ========

_MEDIA_CACHE_PATH = os.path.join(os.getcwd(), "assets", ".media_cache.json")
_media_cache: Dict[str, str] = {}  # 内存缓存（进程内）


def _media_cache_load() -> None:
    """从磁盘加载 file_id 缓存（若不存在则忽略）。"""
    global _media_cache
    try:
        if os.path.isfile(_MEDIA_CACHE_PATH):
            with open(_MEDIA_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    _media_cache = {str(k): str(v) for k, v in data.items()}
                    log.info("welcome: media cache loaded %d items", len(_media_cache))
    except Exception as e:
        log.warning("welcome: load media cache failed: %s", e)


def _media_cache_save() -> None:
    """把内存中的 file_id 缓存落盘。"""
    try:
        os.makedirs(os.path.dirname(_MEDIA_CACHE_PATH), exist_ok=True)
        with open(_MEDIA_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_media_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("welcome: save media cache failed: %s", e)


# 模块导入时尝试加载一次缓存
_media_cache_load()


async def _send_photo_safe(
    msg: Message,
    path: str,
    caption: Optional[str],
    cache_key: Optional[str],
    request_timeout: float = 12.0,
    max_retries: int = 2,
    initial_delay: float = 1.0,
):
    """
    安全发送图片：
      - 优先使用 file_id（极快）
      - 首次没有 file_id 则上传并缓存
      - 发送失败进行指数退避重试
      - 捕获异常，避免打断后续流程
    """
    key = cache_key or path
    file_id = _media_cache.get(key)

    delay = initial_delay
    for attempt in range(1, max_retries + 1):
        try:
            if file_id:
                # 直接使用 file_id 发送
                await msg.answer_photo(
                    photo=file_id,
                    caption=caption,
                    parse_mode="HTML",
                    request_timeout=request_timeout,
                )
                return

            # 没有缓存，则走首次上传（相对较慢）
            message = await msg.answer_photo(
                photo=FSInputFile(path),
                caption=caption,
                parse_mode="HTML",
                request_timeout=request_timeout,
            )
            try:
                if message and message.photo:
                    fid = message.photo[-1].file_id
                    _media_cache[key] = fid
                    _media_cache_save()
                    log.info("welcome: cached file_id for %s -> %s", key, fid)
            except Exception:
                log.exception("welcome: cache file_id failed")
            return

        except Exception as e:
            if attempt >= max_retries:
                log.error("welcome: send_photo failed after %s tries: %s", attempt, e)
                return
            await asyncio.sleep(delay)
            delay *= 2  # 指数退避


# ======== 原有工具函数（保留） ========

def _canon_lang(code: Optional[str]) -> str:
    if not code:
        return "zh"
    c = str(code).lower()
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"
    return "zh"


def _ensure_user_and_check_new(tg_id: int, username: Optional[str], language_code: Optional[str]) -> bool:
    """
    返回是否新用户（首次创建），同时尽量更新 username / language 字段。
    """
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=tg_id).first()
        is_new = False
        if not u:
            u = get_or_create_user(s, tg_id=tg_id, username=username, lang=language_code)
            is_new = True
        else:
            try:
                if hasattr(u, "username") and username and u.username != username:
                    u.username = username
                if hasattr(u, "language") and language_code:
                    canon = _canon_lang(language_code)
                    if u.language != canon:
                        u.language = canon
            except Exception:
                pass
        s.add(u)
        s.commit()
        return is_new


def _find_cover_image(lang: str) -> Optional[str]:
    base = os.path.join(os.getcwd(), "assets")
    candidates = [
        os.path.join(base, f"cover_{lang}.jpg"),
        os.path.join(base, f"cover_{lang}.png"),
        os.path.join(base, f"cover_{lang}.webp"),
        os.path.join(base, "cover.jpg"),
        os.path.join(base, "cover.png"),
        os.path.join(base, "cover.webp"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _build_welcome_text(lang: str, username: str) -> str:
    title = t("welcome_full.title", lang, username=username) or f"🧧 欢迎，{username}！"
    subtitle = t("welcome_full.subtitle", lang) or ""
    howto_title = t("welcome_full.howto.title", lang) or "🎮 玩法"
    howto_steps = t("welcome_full.howto.steps", lang) or ""
    rules_title = t("welcome_full.rules.title", lang) or "📜 基本规则"
    rules_list = t("welcome_full.rules.points", lang) or ""
    fair_title = t("welcome_full.fair.title", lang) or "⚖️ 公平公正声明"
    fair_points = t("welcome_full.fair.points", lang) or ""
    cta = t("welcome_full.cta", lang) or ""

    parts = [
        title,
        "────────────────",
        subtitle,
        "",
        howto_title,
        howto_steps,
        "",
        rules_title,
        rules_list,
        "",
        fair_title,
        fair_points,
    ]
    if cta:
        parts += ["", cta]
    return "\n".join([p for p in parts if p is not None])


# ======== Handler（核心改动：先欢迎→再菜单） ========

@router.message(CommandStart(deep_link=False), F.chat.type == "private")
async def first_time_welcome(msg: Message):
    """
    私聊 /start（无 payload）：
      1) 先发送欢迎封面（或纯文字欢迎）
      2) 再发送主菜单（main_menu）
    """
    user = msg.from_user
    lang = _canon_lang(getattr(user, "language_code", None))
    username = getattr(user, "first_name", "") or (getattr(user, "username", "") or "User")

    is_new = _ensure_user_and_check_new(
        tg_id=int(user.id),
        username=getattr(user, "username", None),
        language_code=lang,
    )
    log.info("welcome: ensured user %s (new=%s)", user.id, is_new)

    # 预先准备欢迎文本（供图文 caption 或纯文字兜底）
    text = _build_welcome_text(lang, username)
    cover = _find_cover_image(lang)

    # 1) 先把欢迎内容发出去（有封面则发图，无封面发文字）
    try:
        if cover:
            # 为保证“先欢迎后菜单”的顺序，这里 **await** 发送（不再用 create_task）
            cache_key = f"welcome_cover_{lang}_v1"
            await _send_photo_safe(
                msg=msg,
                path=cover,
                caption=text,
                cache_key=cache_key,
                request_timeout=12.0,
                max_retries=2,
                initial_delay=1.0,
            )
        else:
            # 没有封面图则发送纯文字欢迎
            try:
                await msg.answer(text, parse_mode="HTML")
            except TelegramBadRequest:
                await msg.answer(text)
    except Exception as e:
        log.exception("welcome: send welcome content failed: %s", e)

    # 2) 再发送主菜单
    try:
        title_for_menu = t("welcome", lang, username=username) or t("menu.back", lang) or "Menu"
        await msg.answer(
            title_for_menu,
            reply_markup=main_menu(lang=lang, is_admin=_is_admin(int(user.id))),
        )
    except Exception as e:
        log.exception("welcome: send main menu failed: %s", e)
