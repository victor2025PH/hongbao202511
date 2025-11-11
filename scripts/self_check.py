# -*- coding: utf-8 -*-
"""
Self-check script that runs a basic health probe and targeted tests.

Usage:
    python scripts/self_check.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_URL = "sqlite:///./runtime_selfcheck.sqlite"
APP_ENTRY = [
    "-m",
    "uvicorn",
    "web_admin.main:app",
    "--host",
    "127.0.0.1",
    "--port",
    "8000",
]
PYTEST_TARGETS: List[str] = [
    "tests/test_regression_features.py",
    "tests/test_public_group_service.py",
    "tests/test_api_public_groups.py",
]


def _env_for_subprocess() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("DATABASE_URL", DEFAULT_DB_URL)
    env.setdefault("FLAG_ENABLE_PUBLIC_GROUPS", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def run_healthz(url: str, timeout: float = 15.0) -> bool:
    """Poll /healthz until success or timeout."""
    started = time.time()
    while time.time() - started <= timeout:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError):
            pass
        time.sleep(0.5)
    return False


def run_subprocess(step: str, args: Iterable[str], env: Optional[Dict[str, str]] = None) -> bool:
    try:
        subprocess.check_call(list(args), cwd=str(ROOT), env=env or _env_for_subprocess())
        return True
    except subprocess.CalledProcessError:
        return False


def run_check_env() -> bool:
    return run_subprocess(
        "check_env",
        [sys.executable, "scripts/check_env.py"],
    )


def run_pytest(target: str) -> bool:
    return run_subprocess(
        f"pytest:{target}",
        [sys.executable, "-m", "pytest", "-q", target],
    )


def run_activity_report() -> bool:
    return run_subprocess(
        "activity_report_cron",
        [
            sys.executable,
            "scripts/activity_report_cron.py",
            "--days",
            "1",
            "--json",
            "--include-webhooks",
        ],
    )


@contextmanager
def launch_app(env: Dict[str, str]):
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, *APP_ENTRY],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        yield proc
    finally:
        if proc and proc.poll() is None:
            if os.name == "nt":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> int:
    steps: List[Dict[str, object]] = []
    env = _env_for_subprocess()

    if os.environ.get("SELF_CHECK_SKIP_SERVER") == "1":
        steps.append({"name": "launch_app", "ok": True, "skipped": True})
        health_ok = run_healthz("http://127.0.0.1:8000/healthz")
    else:
        try:
            with launch_app(env) as proc:
                launch_ok = proc is not None
                steps.append({"name": "launch_app", "ok": launch_ok})
                health_ok = run_healthz("http://127.0.0.1:8000/healthz")
                steps.append({"name": "healthz", "ok": health_ok})
                if not health_ok:
                    output = {"ok": False, "steps": steps}
                    print(json.dumps(output, ensure_ascii=False))
                    return 1

                check_env_ok = run_check_env()
                steps.append({"name": "check_env", "ok": check_env_ok})

                activity_report_ok = run_activity_report()
                steps.append({"name": "activity_report_cron", "ok": activity_report_ok})

                all_pytest_ok = True
                for target in PYTEST_TARGETS:
                    ok = run_pytest(target)
                    steps.append({"name": f"pytest:{target}", "ok": ok})
                    all_pytest_ok = all_pytest_ok and ok

                success = health_ok and check_env_ok and activity_report_ok and all_pytest_ok
                output = {"ok": success, "steps": steps}
                print(json.dumps(output, ensure_ascii=False))
                return 0 if success else 1
        except OSError as exc:
            steps.append({"name": "launch_app", "ok": False, "error": str(exc)})
            output = {"ok": False, "steps": steps}
            print(json.dumps(output, ensure_ascii=False))
            return 1

    # If server skipped we still need to record remaining steps
    steps.append({"name": "healthz", "ok": health_ok})
    check_env_ok = run_check_env()
    steps.append({"name": "check_env", "ok": check_env_ok})
    activity_report_ok = run_activity_report()
    steps.append({"name": "activity_report_cron", "ok": activity_report_ok})
    all_pytest_ok = True
    for target in PYTEST_TARGETS:
        ok = run_pytest(target)
        steps.append({"name": f"pytest:{target}", "ok": ok})
        all_pytest_ok = all_pytest_ok and ok

    success = health_ok and check_env_ok and activity_report_ok and all_pytest_ok
    output = {"ok": success, "steps": steps}
    print(json.dumps(output, ensure_ascii=False))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
