#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
定期彙整公開群活動績效：
- 預設產出「昨日」的活動獎勵績效 CSV。
- 可透過參數調整統計區間與輸出目錄。
- 若設置 Slack Webhook，會自動推送摘要通知。

使用方式（範例）：

    $env:FLAG_ENABLE_PUBLIC_GROUPS="1"
    $env:DATABASE_URL="sqlite:///./data.sqlite"
    py -3 scripts/activity_report_cron.py --days 1 --output-dir reports

可搭配排程（cron / Windows Task Scheduler）定時執行。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

from config.load_env import load_env

load_env()

from config.feature_flags import flags_obj  # noqa: E402
from config.settings import settings  # noqa: E402
from models.db import get_session, init_db  # noqa: E402
from services.public_group_activity import (  # noqa: E402
    summarize_activity_performance,
    summarize_conversion_overview,
    summarize_conversions,
    find_conversion_alerts,
)  # noqa: E402

log = logging.getLogger("scripts.activity_report")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

DEFAULT_OUTPUT_DIR = os.getenv("REPORT_OUTPUT_DIR", "reports")
SLACK_WEBHOOK = os.getenv("REPORT_SLACK_WEBHOOK")


def _determine_range(
    *,
    days: int,
    tz_name: str,
    anchor: Optional[datetime] = None,
) -> Tuple[datetime, datetime, str]:
    """
    根據當地時區計算統計區間：
    - anchor：基準時間（預設為現在）
    - days：往前統計天數，例如 1 表示昨天整日

    回傳 UTC（naive）的 start/end，以及用於檔名的區段標籤。
    """
    tz = ZoneInfo(tz_name)
    anchor_local = anchor.astimezone(tz) if anchor and anchor.tzinfo else (anchor or datetime.now(tz).replace())
    if anchor_local.tzinfo is None:
        anchor_local = anchor_local.replace(tzinfo=tz)

    end_local = anchor_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(days=days)

    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)

    label = f"{start_local.date().isoformat()}_{(end_local - timedelta(seconds=1)).date().isoformat()}"
    return start_utc, end_utc, label


def _flatten_summary(summary: Dict[str, object]) -> Iterable[Dict[str, object]]:
    activities: List[Dict[str, object]] = summary.get("activities", [])  # type: ignore[assignment]
    for activity in activities:
        daily_rows: List[Dict[str, object]] = activity.get("daily", [])  # type: ignore[assignment]
        if not daily_rows:
            yield {
                "activity_id": activity["activity_id"],
                "name": activity["name"],
                "activity_type": activity["activity_type"],
                "date": "",
                "grants": activity.get("total_grants", 0),
                "points": activity.get("total_points", 0),
                "conversions": activity.get("total_conversions", 0),
                "webhook_success_rate": activity.get("webhook_success_rate", 0.0),
                "webhook_attempts": activity.get("webhook_attempts", 0),
                "webhook_failures": activity.get("webhook_failures", 0),
                "slack_failures": activity.get("slack_failures", 0),
            }
            continue
        for item in daily_rows:
            yield {
                "activity_id": activity["activity_id"],
                "name": activity["name"],
                "activity_type": activity["activity_type"],
                "date": item.get("date"),
                "grants": item.get("grants", 0),
                "points": item.get("points", 0),
                "conversions": item.get("conversions", 0),
                "webhook_success_rate": item.get("webhook_success_rate", 0.0),
                "webhook_attempts": item.get("webhook_attempts", 0),
                "webhook_failures": item.get("webhook_failures", 0),
                "slack_failures": item.get("slack_failures", 0),
            }


def export_csv(
    summary: Dict[str, object],
    *,
    output_dir: Path,
    label: str,
    include_webhooks: bool,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"activity_report_{label}.csv"
    fieldnames = ["activity_id", "name", "activity_type", "date", "grants", "points"]
    if include_webhooks:
        fieldnames.extend(
            ["conversions", "webhook_success_rate", "webhook_attempts", "webhook_failures", "slack_failures"]
        )
    with csv_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for row in _flatten_summary(summary):
            activity_id = int(row["activity_id"])
            if include_webhooks and not row.get("date"):
                # 補齊總覽欄位（每日資料已包含在 row 中）
                row.setdefault("webhook_success_rate", 0.0)
                row.setdefault("webhook_attempts", 0)
                row.setdefault("webhook_failures", 0)
                row.setdefault("slack_failures", 0)
            if not include_webhooks:
                row = {key: row.get(key) for key in fieldnames}
            writer.writerow(row)
    return csv_path


def compose_summary_text(
    summary: Dict[str, object],
    *,
    label: str,
    overview: Optional[Dict[str, object]] = None,
    top_activities: Optional[List[Dict[str, object]]] = None,
    include_webhooks: bool = True,
) -> str:
    activities: List[Dict[str, object]] = summary.get("activities", [])  # type: ignore[assignment]
    lines = [f"Public Group Activity Report ({label})"]
    if overview:
        lines.append(
            "- Totals: "
            f"{overview.get('total_conversions', 0)} conversions / "
            f"{overview.get('total_points', 0)} pts / "
            f"Webhook success {(overview.get('webhook_success_rate', 0.0) * 100):.2f}% "
            f"(fails {overview.get('webhook_failures', 0)})"
        )
    if not activities:
        lines.append("- No activity logs recorded.")
        return "\n".join(lines)

    for item in activities:
        name = item.get("name", "Unnamed")
        grants = item.get("total_grants", 0)
        points = item.get("total_points", 0)
        if include_webhooks:
            conversions = item.get("total_conversions", 0)
            success_rate = item.get("webhook_success_rate", 0.0)
            lines.append(
                f"- {name}: {grants} grants / {points} pts / "
                f"{conversions} conversions (Webhook {(success_rate * 100):.2f}%)"
            )
        else:
            lines.append(f"- {name}: {grants} grants / {points} pts")

    if include_webhooks and top_activities:
        lines.append("")
        lines.append("Top activities:")
        for item in top_activities[:3]:
            lines.append(
                f"  • {item.get('name', 'Unnamed')}: {item.get('conversions', 0)} conv / "
                f"{item.get('points', 0)} pts / "
                f"Webhook {(item.get('webhook_success_rate', 0.0) * 100):.2f}% "
                f"(Slack fails {item.get('slack_failures', 0)})"
            )
    return "\n".join(lines)


def notify_slack(webhook: str, message: str) -> bool:
    try:
        resp = requests.post(webhook, json={"text": message}, timeout=5)
        if resp.status_code // 100 != 2:
            log.warning("slack notification failed status=%s body=%s", resp.status_code, resp.text)
            return False
        return True
    except Exception as exc:
        log.warning("slack notification error: %s", exc)
        return False


def generate_activity_report(
    *,
    days: int,
    output_dir: Path,
    include_webhooks: bool = True,
    slack_summary: bool = False,
) -> Dict[str, object]:
    if not flags_obj.ENABLE_PUBLIC_GROUPS:
        raise RuntimeError("public groups feature flag is disabled")

    start, end, label = _determine_range(days=days, tz_name=settings.TZ)
    init_db()
    with get_session() as session:
        summary = summarize_activity_performance(session, start_date=start, end_date=end)
        overview = summarize_conversion_overview(session, start_date=start, end_date=end) if include_webhooks else {}
        conversions = (
            summarize_conversions(session, start_date=start, end_date=end, limit=None) if include_webhooks else []
        )
        alerts = find_conversion_alerts(session, start_date=start, end_date=end) if include_webhooks else []

    conversion_map = {int(item["activity_id"]): item for item in conversions} if include_webhooks else {}

    if include_webhooks:
        for activity in summary.get("activities", []):
            converted = conversion_map.get(int(activity["activity_id"]), {})
            activity.update(
                {
                    "total_conversions": converted.get("conversions", 0),
                    "webhook_success_rate": converted.get("webhook_success_rate", 0.0),
                    "webhook_attempts": converted.get("webhook_attempts", 0),
                    "webhook_failures": converted.get("webhook_failures", 0),
                    "slack_failures": converted.get("slack_failures", 0),
                }
            )

    csv_path = export_csv(
        summary,
        output_dir=output_dir,
        label=label,
        include_webhooks=include_webhooks,
    )
    text = compose_summary_text(
        summary,
        label=label,
        overview=overview if include_webhooks else None,
        top_activities=conversions if (include_webhooks and slack_summary) else None,
        include_webhooks=include_webhooks,
    )

    payload = {
        "label": label,
        "start": summary.get("from"),
        "end": summary.get("to"),
        "csv_path": str(csv_path),
        "summary": summary,
        "notification_sent": False,
    }
    if include_webhooks:
        payload["conversion_overview"] = overview
        payload["conversion_totals"] = conversions
        payload["alerts"] = alerts

    if SLACK_WEBHOOK:
        sent = notify_slack(SLACK_WEBHOOK, text)
        payload["notification_sent"] = sent

    log.info("activity report generated label=%s csv=%s", label, csv_path)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate public group activity performance report.")
    parser.add_argument("--days", type=int, default=1, help="統計的天數（預設 1，代表昨天整日）")
    parser.add_argument(
        "--output-dir",
        type=str,
        default=DEFAULT_OUTPUT_DIR,
        help=f"輸出目錄（預設：{DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="將結果以 JSON 格式輸出到 stdout",
    )
    parser.add_argument(
        "--include-webhooks",
        dest="include_webhooks",
        action="store_true",
        default=True,
        help="在報表與輸出中包含 Webhook / Slack 指標（預設開啟）。",
    )
    parser.add_argument(
        "--no-include-webhooks",
        dest="include_webhooks",
        action="store_false",
        help="停用 Webhook / Slack 指標欄位與統計。",
    )
    parser.add_argument(
        "--slack-summary",
        action="store_true",
        help="於 Slack 通知中顯示轉化摘要與熱門活動。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = generate_activity_report(
        days=args.days,
        output_dir=Path(args.output_dir),
        include_webhooks=args.include_webhooks,
        slack_summary=args.slack_summary,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

