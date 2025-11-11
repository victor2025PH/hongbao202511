# app.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
应用入口（仅调整路由注册顺序：让 admin 提前；其余逻辑保持不变）
"""

import asyncio
import logging
import sys
from config.load_env import load_env
load_env()  # 必须在导入 models/db 之前调用

import aiohttp
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramNetworkError
from aiogram.client.session.aiohttp import AiohttpSession  # 使用 aiogram 自带会话

from config.settings import settings, is_admin as _settings_is_admin
from models.db import init_db
from core.i18n import i18n as _i18n

# 可选：读取功能开关（不存在也不会影响启动）
try:
    from config.feature_flags import flags as _ff  # 标准位置
except Exception:
    try:
        from feature_flags import flags as _ff     # 兼容老位置
    except Exception:
        _ff = None  # 没有 feature_flags 也不影响启动


def _bootstrap_compat_aliases() -> None:
    """针对老代码里的 import 差异做一次性兼容。"""
    try:
        import core.utils.keyboards as kb  # type: ignore
        if not hasattr(kb, "main_menu_kb") and hasattr(kb, "main_menu"):
            kb.main_menu_kb = kb.main_menu  # alias
    except Exception as e:
        logging.getLogger("bootstrap").warning("keyboards alias failed: %s", e)

    try:
        import models.user as user_mod  # type: ignore
        if not hasattr(user_mod, "is_admin"):
            def _is_admin(user_id: int) -> bool:
                return _settings_is_admin(user_id)
            user_mod.is_admin = _is_admin  # type: ignore
    except Exception as e:
        logging.getLogger("bootstrap").warning("user.is_admin alias failed: %s", e)


def _flag_on(name: str, default: bool = True) -> bool:
    """读取 feature flag；flags 缺失时按 default 返回（保持向后兼容）。"""
    try:
        return bool(getattr(_ff, name)) if _ff is not None else bool(default)
    except Exception:
        return bool(default)


# ========================= 稳定的 aiogram 会话（AiohttpSession） =========================
def build_bot_session() -> AiohttpSession:
    # 注意：这里的 timeout 必须是 int（秒）
    return AiohttpSession(timeout=40)


# ========================= 启动预热（get_me 重试） ======================
async def preheat_get_me(bot: Bot, max_retries: int = 3) -> None:
    delay = 1.5
    for i in range(1, max_retries + 1):
        try:
            me = await bot.get_me()
            logging.getLogger("app").info(
                "preheat ok: @%s (%s)", getattr(me, "username", "?"), getattr(me, "id", "?")
            )
            return
        except (TelegramNetworkError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.getLogger("app").warning("get_me timeout (%s/%s): %s", i, max_retries, e)
            if i == max_retries:
                return
            await asyncio.sleep(delay)
            delay *= 2


# ========================= 内联 ProfileSyncMiddleware（非阻塞） =========================
_profile_log = logging.getLogger("profile_sync_mw")

# 兼容三种位置引入 upsert_user_from_tg
try:
    from models.user import upsert_user_from_tg  # type: ignore
except Exception:
    try:
        from services.user import upsert_user_from_tg  # type: ignore
    except Exception:
        from user import upsert_user_from_tg  # type: ignore

class ProfileSyncMiddleware(BaseMiddleware):
    """
    在事件进入路由前静默刷新用户资料（昵称/用户名/语言等），不发消息、不阻塞。
    如需记录“最近目标群”，把 enable_last_target=True（并确保 user.py 里有 set_last_target_chat）。
    """
    def __init__(self, *, enable_last_target: bool = False) -> None:
        super().__init__()
        self.enable_last_target = enable_last_target

    async def __call__(self, handler, event: TelegramObject, data):
        try:
            fu = getattr(event, "from_user", None)
            if fu is None:
                if isinstance(event, CallbackQuery):
                    fu = event.from_user
                elif isinstance(event, Message):
                    fu = event.from_user
            if fu:
                upsert_user_from_tg(fu)

            # （可选）记录最近目标群：仅群/超群；默认关闭
            if self.enable_last_target and fu:
                chat = None
                if isinstance(event, Message):
                    chat = event.chat
                elif isinstance(event, CallbackQuery) and event.message:
                    chat = event.message.chat
                if chat and getattr(chat, "type", None) in ("group", "supergroup"):
                    try:
                        try:
                            from models.user import set_last_target_chat  # type: ignore
                        except Exception:
                            from user import set_last_target_chat  # type: ignore
                        set_last_target_chat(fu.id, chat.id, getattr(chat, "title", None))
                    except Exception:
                        pass
        except Exception as e:
            _profile_log.debug("profile sync skipped: %s", e)

        # 继续交给后续路由
        return await handler(event, data)


async def _register_routers(dp: Dispatcher) -> None:
    """
    统一注册所有路由。

    ✅ 仅修改点：把 admin 系列（能找到哪个就注册哪个）放到最前，保证“按用户导出”的文本不会被其它路由抢走。
    其余路由顺序保持不变。
    """
    log = logging.getLogger("app.routers")

    # ---------- 1) 管理类路由 —— 放到最前（高优先级） ----------
    # admin_adjust（如果你的工程里没有这个模块，会告警并跳过）
    try:
        from routers import admin_adjust as r_admin_adjust
        dp.include_router(r_admin_adjust.router)
        log.info("router loaded: admin_adjust (priority first)")
    except Exception as e:
        log.warning("router load failed: admin_adjust -> %s", e)

    # admin（必须有；核心：把它放到最前）
    try:
        from routers import admin as r_admin
        dp.include_router(r_admin.router)
        log.info("router loaded: admin (priority high)")
    except Exception as e:
        log.warning("router load failed: admin -> %s", e)

    # admin_covers（如果存在就加载；没有则跳过）
    try:
        from routers.admin_covers import router as admin_covers_router
        dp.include_router(admin_covers_router)
        log.info("router loaded: admin_covers")
    except Exception as e:
        log.warning("router load failed: admin_covers -> %s", e)

    # ---------- 2) 其它业务路由（顺序保持与你原来一致） ----------
    route_plan = [
        ("welcome",     "router", False, None),
        ("menu",        "router", False, None),
        ("help",        "router", True,  "ENABLE_HELP"),
        ("envelope",    "router", False, None),
        ("hongbao",     "router", False, None),
        ("member",      "router", False, None),
        ("welfare",     "router", True,  "ENABLE_WELFARE"),
        ("balance",     "router", False, None),
        ("recharge",    "router", False, None),
        ("withdraw",    "router", False, None),
        ("today",       "router", False, None),
        ("rank",        "router", True,  "ENABLE_RANK_GLOBAL"),
        ("invite",      "router", True,  "ENABLE_INVITE"),
        ("public_group","router", True,  "ENABLE_PUBLIC_GROUPS"),
        # 注意：这里不再 include admin（已在前面高优先级加载）
    ]

    def _try_include(modname: str, attr: str = "router") -> None:
        try:
            module = __import__(f"routers.{modname}", fromlist=[attr])
            dp.include_router(getattr(module, attr))
            log.info("router loaded: %s", modname)
        except Exception as ex:
            log.warning("router load failed: %s -> %s", modname, ex)

    for mod, attr, gated, flagname in route_plan:
        if gated and flagname and not _flag_on(flagname, True):
            log.info("router skipped (flag off): %s [%s]", mod, flagname)
            continue
        _try_include(mod, attr)


async def main() -> None:
    # Windows 兼容
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    log = logging.getLogger("app")

    if settings.BOT_TOKEN == "PLEASE_SET_BOT_TOKEN" or not settings.BOT_TOKEN:
        log.error("BOT_TOKEN 未设置。请在环境变量或 .env 中配置 BOT_TOKEN")
        sys.exit(1)

    _bootstrap_compat_aliases()

    # i18n 自检
    try:
        getattr(_i18n, "self_check", lambda: None)()
        log.info("i18n ready.")
    except Exception as e:
        log.warning("i18n 初始化提示：%s", e)

    # 初始化数据库
    init_db()
    log.info("Database initialized.")

    # 封面表 schema 自检（若有）
    try:
        from models.cover import ensure_cover_schema
        ensure_cover_schema()
        log.info("Cover schema ensured.")
    except Exception as e:
        log.warning("ensure_cover_schema failed: %s", e)

    # Bot & Dispatcher（使用 AiohttpSession）
    session = build_bot_session()
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    dp = Dispatcher(storage=MemoryStorage())

    # ★ 注册“资料同步”中间件（非阻塞）—— 必须在路由前后均可，这里放在前面
    dp.update.outer_middleware(ProfileSyncMiddleware(enable_last_target=False))

    # 注册路由
    await _register_routers(dp)

    log.info("Bot is starting polling...")
    try:
        await preheat_get_me(bot, max_retries=3)
        await dp.start_polling(
            bot,
            polling_timeout=30,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        # 优雅关闭
        try:
            await bot.session.close()
        except Exception:
            pass
        try:
            await dp.storage.close()
            await dp.storage.wait_closed()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")
