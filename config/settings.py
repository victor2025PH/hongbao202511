# config/settings.py
# -*- coding: utf-8 -*-
"""
全局配置中心：
- 从环境变量与 .env 载入配置（自动向上递归查找；兼容 BOM；无依赖兜底）
- 暴露 settings 单例（只读属性）
- 提供 is_admin(user_id) 判断
- 同时提供命名空间：settings.recharge / settings.nowpayments / settings.AI
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import List, Optional


# ========== .env 加载（稳健版）==========
def _find_env_path() -> Optional[str]:
    """从当前工作目录开始，向上递归查找最近的 .env，直到根目录。"""
    cur = pathlib.Path.cwd()
    for p in [cur, *cur.parents]:
        cand = p / ".env"
        if cand.exists():
            return str(cand)
    return None


def _load_dotenv_if_exists() -> None:
    """优先使用 python-dotenv；若未安装或失败，则用内置解析器兜底。"""
    env_path = _find_env_path()
    if not env_path:
        return

    try:
        from dotenv import load_dotenv, find_dotenv  # type: ignore
        _ = find_dotenv()
        load_dotenv(dotenv_path=env_path, override=False)
        return
    except Exception:
        pass

    try:
        with open(env_path, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                key = k.strip()
                val = v.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


_load_dotenv_if_exists()


# ========== 工具函数 ==========
def _get_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _split_ids(csv: str) -> List[int]:
    res: List[int] = []
    for part in (csv or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            res.append(int(part))
        except Exception:
            pass
    return res


# ========== 命名空间数据类 ==========
@dataclass(frozen=True)
class RechargeNamespace:
    provider: str
    expire_minutes: int
    coin_usdt: str
    coin_ton: str


@dataclass(frozen=True)
class NowpaymentsNamespace:
    base_url: str
    api_key: str
    ipn_secret: str
    ipn_url: str


# ========== 主配置 ==========
@dataclass
class Settings:
    # Telegram
    BOT_TOKEN: str
    # Database
    DATABASE_URL: str
    # Admin
    ADMIN_IDS: List[int]
    # Locale
    DEFAULT_LANG: str
    FALLBACK_LANG: str
    TZ: str
    DEBUG: bool

    # Recharge
    RECHARGE_PROVIDER: str
    RECHARGE_EXPIRE_MINUTES: int
    RECHARGE_COIN_USDT: str
    RECHARGE_COIN_TON: str

    # NOWPayments
    NOWPAYMENTS_BASE_URL: str
    NOWPAYMENTS_API_KEY: str
    NOWPAYMENTS_IPN_SECRET: str
    NOWPAYMENTS_IPN_URL: str
    NP_PAY_COIN_USDT: str
    NP_PAY_COIN_TON: str
    RECHARGE_ENABLE_TON: bool

    # 命名空间
    recharge: RechargeNamespace
    nowpayments: NowpaymentsNamespace

    # Envelope Cover
    COVER_CHANNEL_ID: Optional[int]

    # AI
    AI_PROVIDER: str
    AI_TIMEOUT: int
    AI_MAX_TOKENS: int
    OPENAI_API_KEY: str
    OPENAI_MODEL: str
    OPENROUTER_API_KEY: str
    OPENROUTER_MODEL: str

    # === 新增：危险操作与超管白名单 ===
    ALLOW_RESET: bool              # 是否允许“清零”相关操作
    SUPER_ADMINS: List[int]        # 超管白名单（并入 is_admin 判定）

    # --------- 工厂：从 env 读取 ----------
    @classmethod
    def from_env(cls) -> "Settings":
        BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("\ufeffBOT_TOKEN") or ""
        BOT_TOKEN = BOT_TOKEN.strip()

        DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data.sqlite").strip()
        ADMIN_IDS = _split_ids(os.getenv("ADMIN_IDS", ""))

        DEFAULT_LANG = (os.getenv("DEFAULT_LANG", "zh") or "zh").strip()
        FALLBACK_LANG = (os.getenv("FALLBACK_LANG", "en") or "en").strip()
        TZ = (os.getenv("TZ", "Asia/Manila") or "Asia/Manila").strip()

        DEBUG = _get_bool("DEBUG", True)

        RECHARGE_PROVIDER = (os.getenv("RECHARGE_PROVIDER", "mock") or "mock").strip()
        RECHARGE_EXPIRE_MINUTES = _get_int("RECHARGE_EXPIRE_MINUTES", 60)
        RECHARGE_COIN_USDT = (os.getenv("RECHARGE_COIN_USDT", "USDTTRC20") or "USDTTRC20").strip()
        RECHARGE_COIN_TON = (os.getenv("RECHARGE_COIN_TON", "TON") or "TON").strip()

        # NP 实际支付币种映射（小写是 NP 的规范）
        NP_PAY_COIN_USDT = (os.getenv("NP_PAY_COIN_USDT", "usdttrc20") or "usdttrc20").strip().lower()
        NP_PAY_COIN_TON  = (os.getenv("NP_PAY_COIN_TON",  "ton") or "ton").strip().lower()

        # 是否在前端展示 TON 入口（账号没开通 TON 时可临时关闭）
        RECHARGE_ENABLE_TON = _get_bool("RECHARGE_ENABLE_TON", True)

        NOWPAYMENTS_BASE_URL = (os.getenv("NOWPAYMENTS_BASE_URL", "https://api.nowpayments.io/v1") or "").strip()
        NOWPAYMENTS_API_KEY = (os.getenv("NOWPAYMENTS_API_KEY", "") or "").strip()
        NOWPAYMENTS_IPN_SECRET = (os.getenv("NOWPAYMENTS_IPN_SECRET", "") or "").strip()
        NOWPAYMENTS_IPN_URL = (os.getenv("NOWPAYMENTS_IPN_URL", "") or "").strip()

        COVER_CHANNEL_ID: Optional[int] = None
        _cov = (os.getenv("HB_COVER_CHANNEL_ID", "") or "").strip()
        if _cov:
            try:
                COVER_CHANNEL_ID = int(_cov)
            except Exception:
                COVER_CHANNEL_ID = None

        recharge_ns = RechargeNamespace(
            provider=RECHARGE_PROVIDER,
            expire_minutes=RECHARGE_EXPIRE_MINUTES,
            coin_usdt=RECHARGE_COIN_USDT,
            coin_ton=RECHARGE_COIN_TON,
        )
        nowp_ns = NowpaymentsNamespace(
            base_url=NOWPAYMENTS_BASE_URL,
            api_key=NOWPAYMENTS_API_KEY,
            ipn_secret=NOWPAYMENTS_IPN_SECRET,
            ipn_url=NOWPAYMENTS_IPN_URL,
        )

        # AI 配置
        AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").strip().lower()
        AI_TIMEOUT = _get_int("AI_TIMEOUT", 20)
        AI_MAX_TOKENS = _get_int("AI_MAX_TOKENS", 500)
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
        OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
        OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

        # === 新增：危险操作与超管白名单 ===
        ALLOW_RESET = _get_bool("ALLOW_RESET", False)
        SUPER_ADMINS = _split_ids(os.getenv("SUPER_ADMINS", ""))

        return cls(
            BOT_TOKEN=BOT_TOKEN,
            DATABASE_URL=DATABASE_URL,
            ADMIN_IDS=ADMIN_IDS,
            DEFAULT_LANG=DEFAULT_LANG,
            FALLBACK_LANG=FALLBACK_LANG,
            TZ=TZ,
            DEBUG=DEBUG,
            RECHARGE_PROVIDER=RECHARGE_PROVIDER,
            RECHARGE_EXPIRE_MINUTES=RECHARGE_EXPIRE_MINUTES,
            RECHARGE_COIN_USDT=RECHARGE_COIN_USDT,
            RECHARGE_COIN_TON=RECHARGE_COIN_TON,
            NOWPAYMENTS_BASE_URL=NOWPAYMENTS_BASE_URL,
            NOWPAYMENTS_API_KEY=NOWPAYMENTS_API_KEY,
            NOWPAYMENTS_IPN_SECRET=NOWPAYMENTS_IPN_SECRET,
            NOWPAYMENTS_IPN_URL=NOWPAYMENTS_IPN_URL,
            NP_PAY_COIN_USDT=NP_PAY_COIN_USDT,
            NP_PAY_COIN_TON=NP_PAY_COIN_TON,
            RECHARGE_ENABLE_TON=RECHARGE_ENABLE_TON,
            recharge=recharge_ns,
            nowpayments=nowp_ns,
            COVER_CHANNEL_ID=COVER_CHANNEL_ID,
            AI_PROVIDER=AI_PROVIDER,
            AI_TIMEOUT=AI_TIMEOUT,
            AI_MAX_TOKENS=AI_MAX_TOKENS,
            OPENAI_API_KEY=OPENAI_API_KEY,
            OPENAI_MODEL=OPENAI_MODEL,
            OPENROUTER_API_KEY=OPENROUTER_API_KEY,
            OPENROUTER_MODEL=OPENROUTER_MODEL,
            ALLOW_RESET=ALLOW_RESET,
            SUPER_ADMINS=SUPER_ADMINS,
        )


# —— 单例暴露 ——
settings = Settings.from_env()


def is_admin(user_id: int) -> bool:
    """判断是否管理员（管理员列表 ∪ 超管白名单）"""
    try:
        uid = int(user_id)
    except Exception:
        return False
    admin_set = set(settings.ADMIN_IDS or [])
    super_set = set(settings.SUPER_ADMINS or [])
    return uid in (admin_set | super_set)
