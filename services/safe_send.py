# services/safe_send.py
import asyncio
import logging
from typing import Optional, Union

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramNetworkError,
)

logger = logging.getLogger(__name__)

class MediaCache:
    """简单的内存缓存；你也可以换成 DB/JSON 配置持久化"""
    file_ids = {}

async def send_photo_safe(
    bot: Bot,
    chat_id: int,
    path: str,
    caption: Optional[str] = None,
    cache_key: Optional[str] = None,
    request_timeout: float = 15.0,
    max_retries: int = 3,
    delay: float = 1.5,
):
    """
    优先用 file_id 发送；若没有则首次上传并缓存。
    超时/网络错误时进行指数退避重试。
    """
    file_id = MediaCache.file_ids.get(cache_key or path)

    for attempt in range(1, max_retries + 1):
        try:
            if file_id:
                msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=file_id,
                    caption=caption,
                    request_timeout=request_timeout,
                )
                return msg

            # 首次上传（可能慢）——建议放到 create_task 里，避免阻塞主流程
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(path),
                caption=caption,
                request_timeout=request_timeout,
            )
            try:
                fid = msg.photo[-1].file_id
                MediaCache.file_ids[cache_key or path] = fid
                logger.info("cached file_id for %s -> %s", cache_key or path, fid)
            except Exception:
                logger.exception("cache file_id failed")
            return msg

        except TelegramRetryAfter as e:
            # 命中限流，等待官方给定秒数后重试
            await asyncio.sleep(getattr(e, "retry_after", delay))
        except TelegramNetworkError:
            # 网络问题：指数退避
            if attempt >= max_retries:
                logger.error("send_photo failed after %s tries (network)", attempt)
                return None
            await asyncio.sleep(delay)
            delay *= 2
        except Exception as e:
            if attempt >= max_retries:
                logger.error("send_photo failed after %s tries: %s", attempt, e)
                return None
            await asyncio.sleep(delay)
            delay *= 2


# ======================================================================
# 新增：统一安全编辑助手（文本优先，失败自动回退到编辑媒体 caption）
# ======================================================================

async def edit_text_or_caption(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "HTML",
    request_timeout: float = 15.0,
    max_retries: int = 3,
    delay: float = 1.0,
) -> bool:
    """
    功能：
      - 先尝试 editMessageText（适用于纯文本消息）；
      - 若报错（例如该消息其实是 photo/animation），自动回退到 editMessageCaption；
      - 内置对 RetryAfter、网络异常的退避重试；
      - “message is not modified” 直接视作成功返回 True。

    返回：
      True = 成功（或内容相同无需更新）；
      False = 最终失败。
    """
    # 尝试编辑文本
    cur_delay = delay
    for attempt in range(1, max_retries + 1):
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                request_timeout=request_timeout,
            )
            return True
        except TelegramBadRequest as e:
            msg = str(e)
            if "message is not modified" in msg:
                return True  # 不需要更新也算成功
            # 这些报错通常意味着该消息不是纯文本，需要回退到 caption
            if any(key in msg.lower() for key in [
                "message to edit not found",
                "message can't be edited",
                "message is not modified: specified new message content and reply markup are exactly the same",  # 冗余兜底
                "not modified",
                "photo caption",
                "can't edit message of this type",
            ]):
                break  # 跳出文本编辑重试，进入 caption 分支
            # 其他 BadRequest 直接失败
            logger.debug("edit_message_text bad request: %s", msg)
            break
        except TelegramRetryAfter as e:
            await asyncio.sleep(getattr(e, "retry_after", cur_delay))
        except TelegramNetworkError:
            if attempt >= max_retries:
                logger.error("edit_message_text failed after %s tries (network)", attempt)
                break
            await asyncio.sleep(cur_delay)
            cur_delay *= 2
        except Exception as e:
            logger.debug("edit_message_text unexpected: %s", e)
            break

    # 回退到编辑 caption（适用于带封面图片/动图的消息）
    cur_delay = delay
    for attempt in range(1, max_retries + 1):
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                request_timeout=request_timeout,
            )
            return True
        except TelegramBadRequest as e:
            msg = str(e)
            if "message is not modified" in msg or "not modified" in msg.lower():
                return True
            logger.debug("edit_message_caption bad request: %s", msg)
            return False
        except TelegramRetryAfter as e:
            await asyncio.sleep(getattr(e, "retry_after", cur_delay))
        except TelegramNetworkError:
            if attempt >= max_retries:
                logger.error("edit_message_caption failed after %s tries (network)", attempt)
                return False
            await asyncio.sleep(cur_delay)
            cur_delay *= 2
        except Exception as e:
            logger.debug("edit_message_caption unexpected: %s", e)
            return False
    return False


async def edit_text_or_caption_by_message(
    message: Message,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "HTML",
    request_timeout: float = 15.0,
    max_retries: int = 3,
    delay: float = 1.0,
) -> bool:
    """
    与 edit_text_or_caption 等价，但接收 aiogram 的 Message 对象，便于在回调里直接使用。
    """
    bot = message.bot
    chat_id = int(message.chat.id)
    message_id = int(message.message_id)
    return await edit_text_or_caption(
        bot=bot,
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        request_timeout=request_timeout,
        max_retries=max_retries,
        delay=delay,
    )
