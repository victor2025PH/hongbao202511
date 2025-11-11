# feature_flags.py
"""
功能开关入口（Settings 页读取）
优先级与兼容策略：
1) 若存在 config.feature_flags：
   - 优先使用其中的 `flags` 字典；
   - 否则把该模块内的 UPPERCASE 布尔常量收集为开关。
2) 若 1) 不存在，则使用此文件下方的默认开关 DEFAULT_FLAGS。
3) 环境变量覆盖（可选）：FLAG_* 将覆盖当前 flags 同名键。
   - "1"/"true"/"yes" -> True
   - "0"/"false"/"no" -> False
   - 其他字符串按字面量覆盖（谨慎）
注意：Settings 页的“试切开关”只会修改内存对象，不做持久化；真要落盘，自己在 config/ 里写持久化逻辑。
"""

from __future__ import annotations

import os
from typing import Any, Dict

# ------------------------------
# 1) 尝试接管 config.feature_flags
# ------------------------------
_flags: Dict[str, Any] | None = None

try:
    # 你项目里如果已经有 config/feature_flags.py，就优先走它
    import config.feature_flags as cf  # type: ignore

    if hasattr(cf, "flags") and isinstance(cf.flags, dict):
        # 形式一：对方直接提供 flags 字典
        _flags = dict(cf.flags)  # 拷贝一份，避免原地修改外部模块
    else:
        # 形式二：收集模块里的 UPPERCASE 布尔常量
        tmp: Dict[str, Any] = {}
        for k in dir(cf):
            if not k.isupper():
                continue
            v = getattr(cf, k)
            if isinstance(v, (bool, int, str)):
                tmp[k] = v
        if tmp:
            _flags = tmp
except Exception:
    # 没有 config.feature_flags 模块也没关系，走默认
    _flags = None

# ------------------------------
# 2) 默认开关（兜底）
# ------------------------------
if _flags is None:
    # 这里给出一组开箱即用的默认开关；你可以在 config/feature_flags.py 覆盖
    DEFAULT_FLAGS: Dict[str, Any] = {
        # 页面功能
        "ENABLE_RANK_PAGE": True,        # 排行/幸运王页
        "ENABLE_EXPORT_CSV": True,       # 导出 CSV
        "ENABLE_LUCKY_RELAY": False,     # MVP 一键转发
        # 运行时功能
        "ENABLE_GLOBAL_TODAY": True,     # 今日战绩全局开关
        "ENABLE_RECHARGE_TUTORIAL": True,
        "ENABLE_WITHDRAW": False,        # 是否开启提现入口
        # 风控/可观测性
        "ENABLE_AUDIT_LOG": True,
        "ENABLE_RATE_LIMIT": True,
    }
    _flags = dict(DEFAULT_FLAGS)

# ------------------------------
# 3) 环境变量覆盖（可选，前台 Settings 仍可读到覆盖后值）
#     规范：FLAG_<KEY>=1/0/true/false/yes/no 或任意字符串
# ------------------------------
def _coerce_env_value(v: str) -> Any:
    lv = v.strip().lower()
    if lv in {"1", "true", "yes"}:
        return True
    if lv in {"0", "false", "no"}:
        return False
    return v  # 其他字符串原样返回

for k, v in os.environ.items():
    if not k.startswith("FLAG_"):
        continue
    key = k[len("FLAG_") :].strip()
    if not key:
        continue
    _flags[key] = _coerce_env_value(v)

# ------------------------------
# 对外暴露：Settings 页与控制器统一读取这个 `flags`
# ------------------------------
flags: Dict[str, Any] = _flags  # type: ignore


# 可选：提供一个小工具，便于你在 shell 里临时查看
if __name__ == "__main__":
    import json
    print(json.dumps(flags, ensure_ascii=False, indent=2))
