# routers/member.py
# -*- coding: utf-8 -*-
"""
群成员事件监听（含兜底）：
- ChatMemberUpdated：当成员加入 / 被邀请 / 审批通过时触发
- Message.new_chat_members：有些群把“入群提示”作为普通消息发送，需要兜底监听
- 均写入 Google Sheet（调用 services.google_logger.log_user_to_sheet）
"""

from __future__ import annotations
import time
from typing import Set, Tuple, Optional

from aiogram import Router, F
from aiogram.types import (
    ChatMemberUpdated, ChatMember, Message, User, Chat,
)

# 这里依赖全表头版本的 logger（支持 chat / inviter_user_id / joined_via_invite_link / note 参数）
from services.google_logger import log_user_to_sheet

router = Router()
__all__ = ["router"]

# ========= 秒级防抖去重：避免重复写表 =========
_RECENT_LOG_CACHE: Set[Tuple[int, int]] = set()   # (chat_id, user_id)
_RECENT_TTL_SEC = 10.0
_LAST_SWEEP_TS = 0.0


def _now() -> float:
    return time.time()


def _sweep_cache():
    """
    简单清理：每隔几分钟整体清空一次，防止集合无限增长。
    """
    global _LAST_SWEEP_TS, _RECENT_LOG_CACHE
    t = _now()
    if t - _LAST_SWEEP_TS > 300.0:  # 5 分钟清一次
        _RECENT_LOG_CACHE.clear()
        _LAST_SWEEP_TS = t


def _should_log_once(chat_id: int, user_id: int) -> bool:
    """
    秒级去重，防止短时间内多次写入（Telegram 可能重放或同一事件触发多次）
    """
    _sweep_cache()
    key = (int(chat_id), int(user_id))
    if key in _RECENT_LOG_CACHE:
        return False
    _RECENT_LOG_CACHE.add(key)
    return True


def _is_join_status(status: str) -> bool:
    """
    新状态是否可视为“加入成功”
    """
    # 'member'：普通成员；'administrator'/'creator'：被直接设为管理/群主的场景（可视为加入）
    return status in {"member", "administrator", "creator"}


# ========= 监听 1：ChatMemberUpdated =========
@router.chat_member()
async def on_chat_member_updated(event: ChatMemberUpdated):
    """
    监听成员状态变化，只在“加入群”时记录用户信息。
    """
    # guard
    if not event.chat or not event.new_chat_member:
        return

    new: ChatMember = event.new_chat_member
    old: ChatMember = event.old_chat_member

    new_status = str(getattr(new, "status", "") or "")
    old_status = str(getattr(old, "status", "") or "")

    # 只处理“从非成员/受限 -> 成员/管理员/群主”的场景
    if not _is_join_status(new_status):
        return
    if _is_join_status(old_status):
        # 之前已经是成员/管理员/群主，不算“加入”
        return

    user: User = new.user
    if not user or getattr(user, "is_bot", False):
        # 过滤机器人
        return

    # 秒级去重
    try:
        chat_id_int = int(event.chat.id)
    except Exception:
        return
    if not _should_log_once(chat_id_int, int(user.id)):
        return

    # 识别来源/邀请信息
    source = "member_join"
    joined_via_invite_link = False
    inviter_user_id: Optional[int] = None

    try:
        # 通过邀请链接加入（不同 api 版本字段不同，这里多做兼容）
        if getattr(event, "via_chat_folder_invite_link", False) or getattr(event, "via_invite_link", False):
            joined_via_invite_link = True
            source = "member_join_via_invite"

        # 审批通过：受限 -> 成员
        if old_status == "restricted" and new_status == "member":
            source = "member_join_approved"

        # 邀请人（若有发起者）
        if getattr(event, "from_user", None):
            inviter_user_id = getattr(event.from_user, "id", None)
    except Exception:
        pass

    # 落表（全表头版本）
    try:
        log_user_to_sheet(
            user,
            source=source,
            chat=event.chat,  # Chat
            inviter_user_id=inviter_user_id,
            joined_via_invite_link=joined_via_invite_link,
            note="member joined",
        )
    except Exception as e:
        # 不抛异常，避免影响群内流程；如需可改用 logging
        print(f"[member] log_user_to_sheet(chat_member) failed: {e}")


# ========= 监听 2（兜底）：新成员作为普通消息出现 =========
@router.message(F.new_chat_members)
async def on_new_chat_members(msg: Message):
    """
    有些群把“入群提示”作为普通消息下发（message.new_chat_members），此处兜底记录。
    """
    chat: Chat = msg.chat
    inviter_user_id: Optional[int] = getattr(msg.from_user, "id", None) if getattr(msg, "from_user", None) else None

    if not msg.new_chat_members:
        return

    for u in msg.new_chat_members:
        if not u or getattr(u, "is_bot", False):
            continue
        try:
            chat_id_int = int(chat.id)
        except Exception:
            continue
        if not _should_log_once(chat_id_int, int(u.id)):
            continue

        # 兜底事件没有明确的“邀请链接标记”，这里按 false 落表；来源标注为 message
        try:
            log_user_to_sheet(
                u,
                source="member_join_message",
                chat=chat,
                inviter_user_id=inviter_user_id,
                joined_via_invite_link=False,
                note="member joined via message",
            )
        except Exception as e:
            print(f"[member] log_user_to_sheet(message) failed: {e}")
