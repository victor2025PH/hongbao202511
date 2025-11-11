# core/middlewares/throttling.py
# -*- coding: utf-8 -*-
"""
节流中间件：
- 限制用户点击按钮/发消息过于频繁
- 默认阈值 1 秒；可在 settings 中调整
"""

from __future__ import annotations

import time
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from config.settings import settings

logger = logging.getLogger("throttling")


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 1.0):
        """
        :param rate_limit: 节流阈值（秒）
        """
        self.rate_limit = rate_limit
        self._last_time: Dict[int, float] = {}  # user_id -> timestamp

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user_id = None
        if isinstance(event, CallbackQuery):
            user_id = event.from_user.id
        elif isinstance(event, Message):
            user_id = event.from_user.id

        if user_id:
            now = time.time()
            last = self._last_time.get(user_id, 0)
            if now - last < self.rate_limit:
                # 频繁操作
                try:
                    if isinstance(event, CallbackQuery):
                        await event.answer("⏳ 操作过于频繁，请稍候…", show_alert=False)
                    elif isinstance(event, Message):
                        await event.answer("⏳ 操作过于频繁，请稍候…")
                except Exception as e:
                    logger.debug("throttling notify failed: %s", e)
                return None
            self._last_time[user_id] = now

        return await handler(event, data)
