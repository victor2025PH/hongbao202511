# -*- coding: utf-8 -*-
"""
Check .env.example against config/settings.py requirements.

Usage:
    python scripts/check_env.py

Exit codes:
    0 - all required keys are present
    1 - missing keys or duplicated keys detected
    2 - failed to parse configuration files
"""
from __future__ import annotations

import os
import re
import sys
import importlib.util
from pathlib import Path
from typing import Dict, Set, Iterable

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
SETTINGS_FILE = ROOT / "config" / "settings.py"
FIELD_OVERRIDES = {
    "COVER_CHANNEL_ID": "HB_COVER_CHANNEL_ID",
}


def load_env_example(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f".env example not found: {path}")

    values: Dict[str, str] = {}
    duplicate_keys: Set[str] = set()

    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            if key in values:
                duplicate_keys.add(key)
            values[key] = value.strip()

    if duplicate_keys:
        raise ValueError(f"Duplicate keys found in {path.name}: {', '.join(sorted(duplicate_keys))}")

    return values


def extract_settings_keys(path: Path) -> Set[str]:
    """
    Load config/settings.py dynamically, read Settings dataclass to discover env keys.
    """
    spec = importlib.util.spec_from_file_location("project_config_settings", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # type: ignore[index]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]

    if not hasattr(module, "Settings"):
        raise AttributeError("Settings dataclass not found in config/settings.py")

    settings_cls = module.Settings
    attr_names = {a for a in dir(settings_cls) if not a.startswith("_")}

    # env field names correspond to __init__ parameters
    init_params: Iterable[str]
    if hasattr(settings_cls, "__dataclass_fields__"):
        init_params = settings_cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
    else:
        init_params = attr_names

    # map to potential env keys
    env_keys: Set[str] = set()
    for name in init_params:
        if not any(ch.isupper() for ch in name):
            continue
        key = FIELD_OVERRIDES.get(name.upper(), name.upper())
        env_keys.add(key)

    # Additional names referenced by Settings factory
    extra_candidates = [
        "BOT_TOKEN",
        "DATABASE_URL",
        "ADMIN_IDS",
        "SUPER_ADMINS",
        "NOWPAYMENTS_API_KEY",
        "NOWPAYMENTS_IPN_SECRET",
        "NOWPAYMENTS_IPN_URL",
        "NP_PAY_COIN_USDT",
        "NP_PAY_COIN_TON",
        "HB_COVER_CHANNEL_ID",
        "AI_PROVIDER",
        "AI_TIMEOUT",
        "AI_MAX_TOKENS",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "ALLOW_RESET",
        "DEBUG",
    ]
    env_keys.update(extra_candidates)

    return env_keys


def main() -> int:
    try:
        env_values = load_env_example(ENV_EXAMPLE)
    except Exception as exc:
        print(f"[ERROR] failed to load {ENV_EXAMPLE.name}: {exc}", file=sys.stderr)
        return 2

    try:
        settings_keys = extract_settings_keys(SETTINGS_FILE)
    except Exception as exc:
        print(f"[ERROR] failed to parse {SETTINGS_FILE}: {exc}", file=sys.stderr)
        return 2

    missing_keys = sorted(k for k in settings_keys if k and k not in env_values)
    if missing_keys:
        print("[ERROR] missing keys in .env.example:")
        for key in missing_keys:
            print(f"  - {key}")
        return 1

    print("[OK] .env.example covers required keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

