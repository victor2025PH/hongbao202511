# core/middlewares/errors.py
# -*- coding: utf-8 -*-
"""
全局错误处理中间件：
- 捕获所有 Handler 异常，统一日志记录
- DEBUG 模式下回显错误信息；非 DEBUG 模式仅提示“内部错误”
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, Message, CallbackQuery

from config.settings import settings

logger = logging.getLogger("errors")


class ErrorsMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as e:
            # 记录 traceback
            tb_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            logger.error("❌ Unhandled exception: %s\n%s", e, tb_str)

            # 提示用户
            try:
                if isinstance(event, CallbackQuery):
                    if settings.DEBUG:
                        await event.message.answer(f"⚠️ <b>Error:</b>\n<pre>{tb_str}</pre>", parse_mode="HTML")
                    else:
                        await event.message.answer("⚠️ Internal error, please try again later.")
                    await event.answer()
                elif isinstance(event, Message):
                    if settings.DEBUG:
                        await event.answer(f"⚠️ <b>Error:</b>\n<pre>{tb_str}</pre>", parse_mode="HTML")
                    else:
                        await event.answer("⚠️ Internal error, please try again later.")
            except Exception as ee:
                logger.warning("Failed to notify user about error: %s", ee)

            # 不中断后续中间件链路
            return None
