# routers/__init__.py
# -*- coding: utf-8 -*-
"""
统一注册所有路由模块

特性：
- 全量列出项目内可用路由（admin / admin_adjust / menu / envelope / hongbao / welfare /
  recharge / withdraw / balance / today / rank / invite）
- 兼容 feature flags：若 config.feature_flags 中显式设置了关闭开关，则跳过对应路由；
  未设置时默认开启。
- 每个模块需在其文件内暴露 `router = Router()`
- 导入失败时安全降级并打印日志，不影响其他路由加载。
"""

from __future__ import annotations
import importlib
import logging
from typing import List, Optional

from aiogram import Dispatcher, Router

log = logging.getLogger("routers")

# 可加载路由清单（按推荐顺序）
_MODULES: list[str] = [
    "routers.admin_adjust",
    "routers.admin",
    "routers.menu",
    "routers.envelope",
    "routers.hongbao",
    "routers.welfare",
    "routers.recharge",
    "routers.withdraw",
    "routers.balance",
    "routers.today",
    "routers.rank",
    "routers.invite",
    "routers.member",

]

# feature flags（未配置则默认 True）
try:
    from config import feature_flags as _ff  # type: ignore
except Exception:
    _ff = None  # type: ignore

_FLAG_MAP = {
    "routers.admin_adjust": "ENABLE_ADMIN_ADJUST",
    "routers.admin": "ENABLE_ADMIN",
    "routers.menu": "ENABLE_MENU",
    "routers.envelope": "ENABLE_ENVELOPE",
    "routers.hongbao": "ENABLE_HONGBAO",
    "routers.welfare": "ENABLE_WELFARE",
    "routers.recharge": "ENABLE_RECHARGE",
    "routers.withdraw": "ENABLE_WITHDRAW",
    "routers.balance": "ENABLE_BALANCE",
    "routers.today": "ENABLE_TODAY",
    "routers.rank": "ENABLE_RANK",
    "routers.invite": "ENABLE_INVITE",
    "routers.member": "ENABLE_MEMBER",

}


def _flag_on(modname: str, default: bool = True) -> bool:
    """读取 feature flag；未配置则返回 default（默认 True）。"""
    try:
        if not _ff or not hasattr(_ff, "flags"):
            return default
        flag_name = _FLAG_MAP.get(modname)
        if not flag_name:
            return default
        return bool(getattr(_ff.flags, flag_name, default))
    except Exception:
        return default


def _try_get_router(modname: str) -> Optional[Router]:
    """安全导入模块并取出其中的 `router`。"""
    try:
        m = importlib.import_module(modname)
    except ModuleNotFoundError as e:
        log.warning("⏭️ 跳过：模块不存在 %s (%s)", modname, e)
        return None
    except Exception as e:
        log.exception("❌ 导入模块失败：%s (%s)", modname, e)
        return None

    r = getattr(m, "router", None)
    if not isinstance(r, Router):
        log.warning("⚠️ 模块未导出 `router`: %s", modname)
        return None
    return r


def setup_routers(dp: Dispatcher) -> List[Router]:
    """逐个加载并注册路由，返回成功注册的 Router 列表。"""
    registered: List[Router] = []
    for name in _MODULES:
        if not _flag_on(name, True):
            log.info("⏭️ 跳过（被开关禁用）：%s", name)
            continue
        r = _try_get_router(name)
        if r:
            dp.include_router(r)
            registered.append(r)
            log.info("✅ Registered router: %s", name)

    if not registered:
        log.warning("⚠️ 未注册任何路由，请检查模块与开关配置。")

    return registered


__all__ = ["setup_routers"]
