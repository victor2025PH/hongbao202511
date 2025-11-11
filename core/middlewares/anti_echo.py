# core/middlewares/anti_echo.py
# -*- coding: utf-8 -*-
"""
防止按钮回显中间件：
- 作用：避免 CallbackQuery data 在群聊中回显
- 用户点击按钮时，只处理逻辑，不在群里生成多余文本
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery

logger = logging.getLogger("anti_echo")


class AntiEchoMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            try:
                # 仅给个“处理中”反馈，不回显 callback_data
                await event.answer(cache_time=1)
            except Exception as e:
                logger.debug("anti_echo answer failed: %s", e)

        return await handler(event, data)
