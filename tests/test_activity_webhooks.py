from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from uuid import uuid4

import pytest
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///./test_activity_webhook.sqlite"
os.environ["FLAG_ENABLE_PUBLIC_GROUPS"] = "1"
os.environ.pop("ACTIVITY_SLACK_WEBHOOK", None)
os.environ.pop("REPORT_SLACK_WEBHOOK", None)

from models.db import get_session, init_db  # noqa: E402
from models.public_group import (  # noqa: E402
    PublicGroup,
    PublicGroupStatus,
    PublicGroupActivityConversionLog,
)  # noqa: E402
from services.public_group_activity import (  # noqa: E402
    create_activity,
    create_or_update_webhook,
    emit_activity_conversion,
    list_active_webhooks,
)


TEST_DB = Path("test_activity_webhook.sqlite")


def setup_module() -> None:
    TEST_DB.unlink(missing_ok=True)
    init_db()


def teardown_module() -> None:
    try:
        TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_emit_activity_conversion_dispatches_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_requests: List[dict] = []

    def fake_post(url: str, **kwargs):
        sent_requests.append({"url": url, **kwargs})

        class _Resp:
            status_code = 200

        return _Resp()

    monkeypatch.setattr("services.public_group_activity.httpx.post", fake_post)
    # ensure slack notifications disabled
    monkeypatch.setenv("ACTIVITY_SLACK_WEBHOOK", "")
    monkeypatch.setenv("REPORT_SLACK_WEBHOOK", "")

    with get_session() as session:
        activity = create_activity(
            session,
            name="Webhook Bonus",
            reward_points=5,
            bonus_points=5,
            start_at=datetime.utcnow() - timedelta(hours=1),
            end_at=datetime.utcnow() + timedelta(hours=1),
            is_highlight_enabled=True,
        )
        session.flush()
        create_or_update_webhook(
            session,
            activity_id=activity.id,
            url="https://example.com/hooks",
            secret="shhh",
        )
        group = PublicGroup(
            creator_tg_id=90001,
            name="Notify Group",
            invite_link=f"https://t.me/+notify_group_{uuid4().hex}",
        )
        group.tags = []
        group.status = PublicGroupStatus.ACTIVE
        session.add(group)
        session.flush()

        emit_activity_conversion(
            session,
            activity_id=activity.id,
            group=group,
            user_tg_id=777001,
            points=10,
            metadata={"sample": True},
        )

    assert sent_requests, "expected webhook call dispatched"
    assert sent_requests[0]["url"] == "https://example.com/hooks"
    assert sent_requests[0]["headers"]["Content-Type"] == "application/json"
    assert "X-Activity-Signature" in sent_requests[0]["headers"]

    with get_session() as session:
        logs = session.execute(select(PublicGroupActivityConversionLog).order_by(PublicGroupActivityConversionLog.id)).scalars().all()
        assert logs, "expected conversion log entry"
        log_entry = logs[-1]
        assert log_entry.points == 10
        assert log_entry.webhook_status == "success"
        assert log_entry.webhook_attempts == 1
        assert log_entry.webhook_successes == 1
        assert log_entry.slack_status == "skipped"
        assert log_entry.context.get("sample") is True


def test_create_or_update_webhook_returns_latest() -> None:
    with get_session() as session:
        activity = create_activity(
            session,
            name="Track",
            reward_points=1,
            bonus_points=1,
        )
        session.flush()
        webhook, created = create_or_update_webhook(
            session,
            activity_id=activity.id,
            url="https://example.com/a",
            secret=None,
        )
        assert created is True
        assert webhook.is_active is True

        webhook2, created2 = create_or_update_webhook(
            session,
            activity_id=activity.id,
            url="https://example.com/a",
            secret="new",
            is_active=False,
        )
        assert created2 is False
        assert webhook2.secret == "new"
        assert webhook2.is_active is False

        hooks = list_active_webhooks(session, activity_id=activity.id)
        assert hooks == []

