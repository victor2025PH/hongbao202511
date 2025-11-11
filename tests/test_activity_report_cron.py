from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ["DATABASE_URL"] = "sqlite:///./test_activity_report.sqlite"
os.environ["FLAG_ENABLE_PUBLIC_GROUPS"] = "1"

from models.db import get_session, init_db  # noqa: E402
from models.public_group import (  # noqa: E402
    PublicGroup,
    PublicGroupActivity,
    PublicGroupActivityConversionLog,
    PublicGroupActivityLog,
    PublicGroupActivityStatus,
)  # noqa: E402
from scripts import activity_report_cron as cron  # noqa: E402
from scripts.activity_report_cron import (  # noqa: E402
    _determine_range,
    compose_summary_text,
    export_csv,
    generate_activity_report,
)
from services.public_group_activity import create_activity  # noqa: E402


TEST_DB = Path("test_activity_report.sqlite")


def setup_module() -> None:
    TEST_DB.unlink(missing_ok=True)
    init_db()


def teardown_module() -> None:
    try:
        TEST_DB.unlink()
    except PermissionError:
        pass


def _seed_logs() -> None:
    with get_session() as session:
        start, end, _ = cron._determine_range(
            days=1,
            tz_name=cron.settings.TZ,
            anchor=datetime.now(timezone.utc),
        )
        log_time = start + (end - start) / 2
        group = PublicGroup(
            name="Demo Group",
            invite_link="https://t.me/+demo_group",
            creator_tg_id=99999,
        )
        session.add(group)
        session.flush()
        activity = create_activity(
            session,
            name="Test Campaign",
            reward_points=5,
            bonus_points=3,
            start_at=datetime.utcnow() - timedelta(days=2),
            end_at=datetime.utcnow() + timedelta(days=1),
            is_highlight_enabled=True,
        )
        activity.status = PublicGroupActivityStatus.ACTIVE
        session.add(activity)
        session.flush()
        log = PublicGroupActivityLog(
            activity_id=activity.id,
            group_id=group.id,
            user_tg_id=10001,
            event_type="join_bonus",
            points=8,
            date_key=log_time.date().isoformat(),
            created_at=log_time,
        )
        session.add(log)
        conversion = PublicGroupActivityConversionLog(
            activity_id=activity.id,
            group_id=group.id,
            user_tg_id=10001,
            points=8,
            event_type="join_bonus",
            webhook_status="partial",
            webhook_attempts=2,
            webhook_successes=1,
            slack_status="failed",
            created_at=log_time,
        )
        session.add(conversion)
        session.commit()


def test_determine_range_uses_timezone() -> None:
    anchor = datetime(2025, 1, 10, 8, 30, tzinfo=None)
    start, end, label = _determine_range(days=1, tz_name="Asia/Taipei", anchor=anchor)
    assert start < end
    assert label.startswith("2025-01-09")


def test_export_and_summary(tmp_path: Path) -> None:
    summary = {
        "activities": [
            {
                "activity_id": 1,
                "name": "Test",
                "activity_type": "join_bonus",
                "total_grants": 2,
                "total_points": 16,
                "total_conversions": 1,
                "webhook_success_rate": 0.5,
                "webhook_attempts": 2,
                "webhook_failures": 1,
                "slack_failures": 1,
                "daily": [
                    {
                        "date": "2025-01-09",
                        "grants": 2,
                        "points": 16,
                        "conversions": 1,
                        "webhook_success_rate": 0.5,
                        "webhook_attempts": 2,
                        "webhook_failures": 1,
                        "slack_failures": 1,
                    },
                ],
            }
        ]
    }
    csv_path = export_csv(
        summary,
        output_dir=tmp_path,
        label="2025-01-09_2025-01-10",
        include_webhooks=True,
    )
    assert csv_path.exists()
    content = csv_path.read_text(encoding="utf-8")
    assert "activity_id" in content
    assert "2025-01-09" in content
    assert "conversions" in content

    text = compose_summary_text(
        summary,
        label="2025-01-09_2025-01-10",
        overview={
            "total_conversions": 1,
            "total_points": 16,
            "webhook_success_rate": 0.5,
            "webhook_failures": 1,
        },
        include_webhooks=True,
    )
    assert "Test: 2 grants / 16 pts / 1 conversions" in text


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_generate_activity_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_logs()

    sent_messages = {}

    def fake_notify(url: str, text: str) -> bool:
        sent_messages["text"] = text
        return True

    monkeypatch.setenv("REPORT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setitem(os.environ, "REPORT_SLACK_WEBHOOK", "")
    monkeypatch.setattr(cron, "SLACK_WEBHOOK", "")
    monkeypatch.setattr(cron, "notify_slack", fake_notify)
    result = generate_activity_report(days=1, output_dir=tmp_path, slack_summary=True)

    assert Path(result["csv_path"]).exists()
    assert result["notification_sent"] is False
    assert json.loads(json.dumps(result["summary"]))
    assert "conversion_overview" in result
    assert "conversion_totals" in result
    assert result["conversion_overview"]["webhook_attempts"] >= 1
    assert any(alert["slack_failures"] >= 1 for alert in result.get("alerts", []))
    assert "Test Campaign" in sent_messages.get("text", "") or sent_messages == {}


