# config/load_env.py
from __future__ import annotations

import os
from pathlib import Path

try:
    # 依赖很轻，找不到也可以退化为纯 os.environ 行为
    from dotenv import load_dotenv, find_dotenv  # type: ignore
except Exception:
    load_dotenv = None
    find_dotenv = None


def load_env(override: bool = False) -> str | None:
    """
    加载 .env 文件到进程环境变量（os.environ）。

    优先级（从高到低）：
      1. 已存在的系统环境变量（override=False 时不会被 .env 覆盖）
      2. 项目根目录下的 .env
      3. 运行目录（cwd）下的 .env

    返回：实际加载到的 .env 文件路径（找不到返回 None）
    """
    # 计算项目根目录：当前文件(config/load_env.py) 的两级父目录
    project_root = Path(__file__).resolve().parents[1]
    candidate_files = [
        project_root / ".env",     # 项目根目录
        Path.cwd() / ".env",       # 当前工作目录（某些任务/脚本会变更 cwd）
    ]

    # 优先使用 python-dotenv 的 find_dotenv 能力
    if find_dotenv is not None:
        found = find_dotenv(".env", usecwd=True)
        if found:
            if load_dotenv is not None:
                load_dotenv(found, override=override)
            return found

    # 退化：手工检查两个候选位置
    for f in candidate_files:
        if f.exists():
            if load_dotenv is not None:
                load_dotenv(f, override=override)
            return str(f)

    return None
