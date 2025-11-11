# core/middlewares/__init__.py
# -*- coding: utf-8 -*-
"""
中间件集中注册：
- setup_middlewares(dp) 统一把中间件挂到 Dispatcher
"""

from __future__ import annotations
from aiogram import Dispatcher

from .errors import ErrorsMiddleware
from .user_bootstrap import UserBootstrapMiddleware
from .throttling import ThrottlingMiddleware
from .anti_echo import AntiEchoMiddleware


def setup_middlewares(dp: Dispatcher) -> None:
    """
    建议注册顺序：
    1) 错误捕获
    2) 用户建档
    3) 频率限制
    4) 防回显
    """
    dp.message.middleware(ErrorsMiddleware())
    dp.callback_query.middleware(ErrorsMiddleware())

    dp.message.middleware(UserBootstrapMiddleware())
    dp.callback_query.middleware(UserBootstrapMiddleware())

    dp.message.middleware(ThrottlingMiddleware(rate_limit=1.0))
    dp.callback_query.middleware(ThrottlingMiddleware(rate_limit=0.8))

    dp.callback_query.middleware(AntiEchoMiddleware())
