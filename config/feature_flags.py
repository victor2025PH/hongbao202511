# feature_flags.py
# -*- coding: utf-8 -*-
"""
功能开关与数值参数（运营可调）
- 统一在此配置，避免散落在代码里
- 大部分开关支持**运行时切换**（管理面板可 toggle）
- 与 Settings 页面契约：导出一个“可变的 dict-like 对象”命名为 `flags`，
  并且任何对 `flags[key]` 的赋值会同步回写到内部 dataclass 实例。

字段说明（节选）：
- ENABLE_RANK_GLOBAL：全局排行榜开关（默认 True）
- HB_ALLOW_FIXED: 是否允许“等额分配”生效（默认 False，当前仅 UI 占位）
- HB_POINT_MIN_AMOUNT: 发积分红包时的最小金额（默认 1）
- RECHARGE_QUICK_AMOUNTS: 充值快捷金额（逗号分隔），默认 10,50,100,200
- HONGBAO_RELAY_ONLY_LUCKY: 接力是否仅限“运气王”（默认 True）
- WITHDRAW_MIN_USDT / WITHDRAW_MIN_TON：提现最低限额（默认 1.0）
- WITHDRAW_FEE_USDT / WITHDRAW_FEE_TON：提现按笔固定手续费（默认 0.5 USDT / 0.02 TON）
- AUTO_MODE：演示用可切换开关，便于在管理面板中演示 toggle（默认 False）

新增：
- ENABLE_ADMIN_ADJUST：是否启用“管理员余额调整”功能（默认 True）
- INVITE_LINK_PREFIX：邀请链接前缀（默认 https://t.me/your_bot?start=invite_ ）
- ENV_QUICK_AMOUNTS / ENV_QUICK_SHARES：红包金额/份数的快捷按钮选项（默认 1,5,10,20,50,100）
- RATE_USDT_PER_TON / RATE_USDT_PER_STAR：汇率（1 TON≈? USDT、1 星≈? USDT），用于展示换算提示
"""

from __future__ import annotations
import os
from dataclasses import dataclass, fields, is_dataclass
from typing import Tuple, Any, Dict, Iterable, Iterator

# ---------- 环境变量读取工具 ----------

def _getenv_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)).strip())
    except Exception:
        return default

def _getenv_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)).strip())
    except Exception:
        return default

def _getenv_bool(key: str, default: bool = False) -> bool:
    v = (os.getenv(key, str(default)) or "").strip().lower()
    return v in ("1", "true", "yes", "on")

def _getenv_csv_ints(key: str, default: Tuple[int, ...]) -> Tuple[int, ...]:
    raw = os.getenv(key, "")
    if not raw:
        return default
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except Exception:
            # 忽略无法解析的项
            continue
    return tuple(out) if out else default

def _getenv_str(key: str, default: str) -> str:
    v = os.getenv(key, None)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


# ---------- 功能开关 ----------
@dataclass  # ← 可变，以便管理面板可 runtime toggle
class FeatureFlags:
    # ===== 通用 / 演示 =====
    AUTO_MODE: bool = False               # 演示/预留开关，管理面板可 toggle

    # ===== 管理功能 =====
    ENABLE_ADMIN_ADJUST: bool = True      # 管理员余额调整功能开关
    ENABLE_PUBLIC_GROUPS: bool = False    # 公开交友群功能开关（默认关闭）

    # ===== 福利中心 =====
    ENABLE_WELFARE: bool = True           # 是否启用福利中心
    ENABLE_SIGNIN: bool = True            # 是否启用每日签到
    ENABLE_INVITE: bool = True            # 是否启用邀请有奖
    ENABLE_EXCHANGE: bool = True          # 是否启用积分/能量兑换
    ENABLE_PROMO: bool = True             # 是否启用活动公告模块

    # ===== 邀请有奖逻辑 =====
    INVITE_PROGRESS_PER_PERSON: float = 1.0   # 每邀请 1 人 → 进度条增加 %
    INVITE_HINT_THRESHOLD: int = 95           # 达到多少% 给出“再邀请 5 人”的提示
    INVITE_HINT_STEP: int = 5                 # “再邀请多少人”固定值
    INVITE_LINK_PREFIX: str = "https://t.me/your_bot?start=invite_"  # 邀请链接前缀

    # ===== 积分 ↔ 进度 ↔ 能量规则 =====
    POINTS_PER_PROGRESS: int = 1000       # 1000 积分 → +1% 进度
    ENERGY_REWARD_AT_PROGRESS: int = 2    # 进度跨过多少% 时送能量
    ENERGY_REWARD_AMOUNT: int = 100       # 奖励能量数量
    ENERGY_TO_POINTS_RATIO: int = 1000    # 每 1000 能量 → 100 积分
    ENERGY_TO_POINTS_VALUE: int = 100     # 兑换的积分数

    # ===== 积分、签到、奖励 =====
    SIGNIN_REWARD_POINTS: int = 10        # 每日签到奖励积分
    INVITE_EXTRA_POINTS: int = 50         # 每多拉新 1 人额外积分（运营活动）
    RANK_TOPN: int = 10                   # 排行榜显示前 N

    # ===== 排行（新增全局开关，不影响现有逻辑）=====
    ENABLE_RANK_GLOBAL: bool = True       # 全局排行榜是否启用（与 routers/rank.py 配合使用）

    # ===== 红包逻辑 =====
    HB_MIN_SHARES: int = 1
    HB_MAX_SHARES: int = 100
    HB_MIN_AMOUNT: float = 0.01
    HB_ALLOW_FIXED: bool = False          # 是否允许“等额分配”真正生效（目前 UI 占位）
    HB_POINT_MIN_AMOUNT: int = 1          # 发积分红包的最小金额（份额合规仍以 HB_MIN_SHARES/Max 为准）
    HONGBAO_RELAY_ONLY_LUCKY: bool = True # 接力是否仅限“运气王”

    # ===== 发红包快捷选项（新增）=====
    ENV_QUICK_AMOUNTS: Tuple[int, ...] = (1, 5, 10, 20, 50, 100)  # 金额快捷选项
    ENV_QUICK_SHARES:  Tuple[int, ...] = (1, 5, 10, 20, 50, 100)  # 份数快捷选项

    # ===== 充值快捷金额 =====
    RECHARGE_QUICK_AMOUNTS: Tuple[int, ...] = (10, 50, 100, 200)

    # ===== 汇率（新增，用于展示换算/提示，不做精确结算）=====
    RATE_USDT_PER_TON: float = 6.0        # 1 TON ≈ 6.0 USDT（示例默认）
    RATE_USDT_PER_STAR: float = 0.015     # 1 星 ≈ 0.015 USDT（示例默认）

    # ===== 提现参数 =====
    WITHDRAW_MIN_USDT: float = 1.0        # USDT 提现最小额
    WITHDRAW_MIN_TON: float = 1.0         # TON  提现最小额
    WITHDRAW_FEE_USDT: float = 0.5        # USDT 按笔固定手续费
    WITHDRAW_FEE_TON: float = 0.02        # TON  按笔固定手续费


def _from_env() -> FeatureFlags:
    return FeatureFlags(
        # 通用
        AUTO_MODE=_getenv_bool("AUTO_MODE", False),

        # 管理功能
        ENABLE_ADMIN_ADJUST=_getenv_bool("ENABLE_ADMIN_ADJUST", True),
        ENABLE_PUBLIC_GROUPS=_getenv_bool("ENABLE_PUBLIC_GROUPS", False),

        # 福利中心
        ENABLE_WELFARE=_getenv_bool("ENABLE_WELFARE", True),
        ENABLE_SIGNIN=_getenv_bool("ENABLE_SIGNIN", True),
        ENABLE_INVITE=_getenv_bool("ENABLE_INVITE", True),
        ENABLE_EXCHANGE=_getenv_bool("ENABLE_EXCHANGE", True),
        ENABLE_PROMO=_getenv_bool("ENABLE_PROMO", True),

        # 邀请
        INVITE_PROGRESS_PER_PERSON=_getenv_float("INVITE_PROGRESS_PER_PERSON", 1.0),
        INVITE_HINT_THRESHOLD=_getenv_int("INVITE_HINT_THRESHOLD", 95),
        INVITE_HINT_STEP=_getenv_int("INVITE_HINT_STEP", 5),
        INVITE_LINK_PREFIX=_getenv_str("INVITE_LINK_PREFIX", "https://t.me/your_bot?start=invite_"),

        # 兑换规则
        POINTS_PER_PROGRESS=_getenv_int("POINTS_PER_PROGRESS", 1000),
        ENERGY_REWARD_AT_PROGRESS=_getenv_int("ENERGY_REWARD_AT_PROGRESS", 2),
        ENERGY_REWARD_AMOUNT=_getenv_int("ENERGY_REWARD_AMOUNT", 100),
        ENERGY_TO_POINTS_RATIO=_getenv_int("ENERGY_TO_POINTS_RATIO", 1000),
        ENERGY_TO_POINTS_VALUE=_getenv_int("ENERGY_TO_POINTS_VALUE", 100),

        # 积分/签到/排行
        SIGNIN_REWARD_POINTS=_getenv_int("SIGNIN_REWARD_POINTS", 10),
        INVITE_EXTRA_POINTS=_getenv_int("INVITE_EXTRA_POINTS", 50),
        RANK_TOPN=_getenv_int("RANK_TOPN", 10),

        # 排行开关
        ENABLE_RANK_GLOBAL=_getenv_bool("ENABLE_RANK_GLOBAL", True),

        # 红包
        HB_MIN_SHARES=_getenv_int("HB_MIN_SHARES", 1),
        HB_MAX_SHARES=_getenv_int("HB_MAX_SHARES", 100),
        HB_MIN_AMOUNT=_getenv_float("HB_MIN_AMOUNT", 0.01),
        HB_ALLOW_FIXED=_getenv_bool("HB_ALLOW_FIXED", False),
        HB_POINT_MIN_AMOUNT=_getenv_int("HB_POINT_MIN_AMOUNT", 1),
        HONGBAO_RELAY_ONLY_LUCKY=_getenv_bool("HONGBAO_RELAY_ONLY_LUCKY", True),

        # 发红包快捷选项（新增）
        ENV_QUICK_AMOUNTS=_getenv_csv_ints("ENV_QUICK_AMOUNTS", (1, 5, 10, 20, 50, 100)),
        ENV_QUICK_SHARES=_getenv_csv_ints("ENV_QUICK_SHARES", (1, 5, 10, 20, 50, 100)),

        # 充值快捷金额
        RECHARGE_QUICK_AMOUNTS=_getenv_csv_ints("RECHARGE_QUICK_AMOUNTS", (10, 50, 100, 200)),

        # 汇率（新增）
        RATE_USDT_PER_TON=_getenv_float("RATE_USDT_PER_TON", 6.0),
        RATE_USDT_PER_STAR=_getenv_float("RATE_USDT_PER_STAR", 0.015),

        # 提现参数
        WITHDRAW_MIN_USDT=_getenv_float("WITHDRAW_MIN_USDT", 1.0),
        WITHDRAW_MIN_TON=_getenv_float("WITHDRAW_MIN_TON", 1.0),
        WITHDRAW_FEE_USDT=_getenv_float("WITHDRAW_FEE_USDT", 0.5),
        WITHDRAW_FEE_TON=_getenv_float("WITHDRAW_FEE_TON", 0.02),
    )

# ---------- dict 代理，供 Settings 页面安全 toggle ----------
class _FlagsDict(dict):
    """
    一个真正继承自 dict 的代理：
    - isinstance(proxy, dict) == True 以适配 settings.py 现有逻辑
    - 读：优先从 dataclass 实例取值，保证实时
    - 写：同时写回 dict 自身与 dataclass 实例属性
    """
    def __init__(self, obj: Any):
        if not is_dataclass(obj):
            raise TypeError("flags proxy requires a dataclass instance")
        self._obj = obj
        # 初始填充
        super().__init__({f.name: getattr(obj, f.name) for f in fields(obj)})

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        if hasattr(self._obj, key):
            setattr(self._obj, key, value)

    # 同步 get 保持和 dataclass 一致
    def __getitem__(self, key: str) -> Any:
        if hasattr(self._obj, key):
            return getattr(self._obj, key)
        return super().__getitem__(key)

    # 批量更新
    def update(self, *args, **kwargs) -> None:
        it: Iterable[tuple[str, Any]] = {}
        if args:
            if len(args) > 1:
                raise TypeError("update expected at most 1 arguments")
            other = args[0]
            if isinstance(other, dict):
                it = other.items()
            else:
                it = other
        for k, v in dict(it, **kwargs).items():
            self[k] = v  # 走 __setitem__，自动回写 dataclass

# 单例（可变实例）
_flags_obj = _from_env()

# 环境变量 FLAG_* 覆盖：FLAG_ENABLE_WITHDRAW=1 等
for k, v in os.environ.items():
    if not k.startswith("FLAG_"):
        continue
    name = k[len("FLAG_"):].strip()
    if not name:
        continue
    lv = v.strip().lower()
    if lv in {"1", "true", "yes", "on"}:
        val: Any = True
    elif lv in {"0", "false", "no", "off"}:
        val = False
    else:
        try:
            # 尝试数字
            if "." in lv:
                val = float(lv)
            else:
                val = int(lv)
        except Exception:
            val = v  # 其它按字符串覆盖
    if hasattr(_flags_obj, name):
        setattr(_flags_obj, name, val)

# 对外暴露的 dict 代理（Settings 页请使用这个）
flags: Dict[str, Any] = _FlagsDict(_flags_obj)

# 可选：同时暴露原始对象，供需要强类型访问的代码使用
flags_obj: FeatureFlags = _flags_obj  # noqa

__all__ = ["FeatureFlags", "flags", "flags_obj"]
