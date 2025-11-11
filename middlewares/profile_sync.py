# middlewares/profile_sync.py
# -*- coding: utf-8 -*-
"""
ProfileSyncMiddleware
---------------------
把“同步用户昵称/用户名/语言等资料”的逻辑放到中间件中：
- 对所有 Update（消息 / 回调）生效
- 静默同步，不发消息、不应答、不阻塞
- 不中断后续路由处理，避免 /start 等指令被“抢走”

如需记录“最近目标群”，可把 enable_last_target=True，并且确保 user.py 里有 set_last_target_chat。
"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable, Dict, Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

log = logging.getLogger("profile_sync_mw")

# 兼容引入 upsert_user_from_tg（你的 user.py 里刚加过）
try:
    from models.user import upsert_user_from_tg  # type: ignore
except Exception:
    try:
        from services.user import upsert_user_from_tg  # type: ignore
    except Exception:
        from user import upsert_user_from_tg  # type: ignore


class ProfileSyncMiddleware(BaseMiddleware):
    def __init__(self, *, enable_last_target: bool = False) -> None:
        super().__init__()
        self.enable_last_target = enable_last_target

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            from_user = getattr(event, "from_user", None)
            if not from_user:
                if isinstance(event, CallbackQuery):
                    from_user = event.from_user
                elif isinstance(event, Message):
                    from_user = event.from_user

            if from_user:
                # 同步昵称 / 用户名 / 语言 / full_name 等到 users 表
                upsert_user_from_tg(from_user)

            # 可选：记录“最近目标群”（仅群/超群），默认关闭
            if self.enable_last_target and from_user:
                chat = None
                if isinstance(event, Message):
                    chat = event.chat
                elif isinstance(event, CallbackQuery) and event.message:
                    chat = event.message.chat
                if chat and getattr(chat, "type", None) in ("group", "supergroup"):
                    try:
                        # 允许两种位置
                        try:
                            from models.user import set_last_target_chat  # type: ignore
                        except Exception:
                            from user import set_last_target_chat  # type: ignore
                        set_last_target_chat(from_user.id, chat.id, getattr(chat, "title", None))
                    except Exception:
                        pass

        except Exception as e:
            log.debug("profile sync skipped: %s", e)

        # 继续交给后续路由/处理器
        return await handler(event, data)
