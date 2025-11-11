# core/middlewares/user_bootstrap.py
# -*- coding: utf-8 -*-
"""
用户初始化中间件：
- 确保所有消息/回调的用户在数据库中有记录
- 自动更新 username / language_code
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from models.db import get_session
from models.user import get_or_create_user

logger = logging.getLogger("user_bootstrap")


class UserBootstrapMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            if isinstance(event, (Message, CallbackQuery)) and event.from_user:
                uid = event.from_user.id
                uname = event.from_user.username
                lang = getattr(event.from_user, "language_code", None)

                with get_session() as s:
                    get_or_create_user(s, tg_id=uid, username=uname, lang=lang)
                    s.commit()
        except Exception as e:
            logger.warning("UserBootstrap failed: %s", e)

        return await handler(event, data)
