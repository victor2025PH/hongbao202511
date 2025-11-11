from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from datetime import datetime, timedelta

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_web_admin_public.sqlite"
os.environ["FLAG_ENABLE_PUBLIC_GROUPS"] = "1"
os.environ["ADMIN_WEB_USER"] = "admin"

import sys
for mod in ("models.db", "models.user", "models.public_group"):
    sys.modules.pop(mod, None)

from models.db import get_session, init_db  # noqa: E402
from models.public_group import (  # noqa: E402
    PublicGroup,
    PublicGroupActivity,
    PublicGroupActivityStatus,
    PublicGroupReport,
    PublicGroupReportStatus,
    PublicGroupStatus,
)
from services.public_group_service import create_group, join_group  # noqa: E402
from services.public_group_activity import create_activity, emit_activity_conversion  # noqa: E402
from services.public_group_tracking import record_event  # noqa: E402
from services.public_group_report import create_report_case  # noqa: E402
from sqlalchemy import select  # noqa: E402
from web_admin.constants import SESSION_USER_KEY  # noqa: E402
from web_admin.deps import db_session, db_session_ro, require_admin  # noqa: E402
from web_admin.main import app  # noqa: E402
from core.i18n.i18n import t  # noqa: E402

_TEST_DB = Path("test_web_admin_public.sqlite")
client = TestClient(app)


def override_db():
    with get_session() as session:
        yield session


def override_admin(req: Request):
    return {"username": "admin", "tg_id": 99999}


def setup_module() -> None:
    client.app.dependency_overrides[db_session] = override_db
    client.app.dependency_overrides[db_session_ro] = override_db
    client.app.dependency_overrides[require_admin] = override_admin

    _TEST_DB.unlink(missing_ok=True)
    init_db()

    with get_session() as session:
        group, _ = create_group(
            session,
            creator_tg_id=12345,
            name="Ops Lab",
            invite_link="https://t.me/+ops_lab",
            description="Ops dashboard testing",
            tags=["ops", "lab"],
        )
        record_event(session, group_id=group.id, event_type="view", user_tg_id=50001)
        record_event(session, group_id=group.id, event_type="click", user_tg_id=50001)
        record_event(session, group_id=group.id, event_type="join", user_tg_id=50001)
        activity = create_activity(
            session,
            name="Launch Bonus",
            start_at=datetime.utcnow() - timedelta(hours=1),
            end_at=datetime.utcnow() + timedelta(days=1),
            reward_points=2,
            bonus_points=3,
            highlight_slots=1,
            is_highlight_enabled=True,
            daily_cap=100,
            total_cap=1000,
        )
        session.commit()
        join_group(session, group_id=group.id, user_tg_id=60002)
        emit_activity_conversion(
            session,
            activity_id=activity.id,
            group=group,
            user_tg_id=70001,
            points=7,
            metadata={"source": "test"},
        )
        session.commit()

        create_report_case(
            session,
            group_id=group.id,
            reporter_tg_id=80001,
            report_type="spam",
            description="Mass advertising messages",
            metadata={"source": "seed"},
        )
        session.commit()


def teardown_module() -> None:
    client.app.dependency_overrides.clear()
    client.close()
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass


def test_public_groups_dashboard_page() -> None:
    response = client.get("/admin/public-groups/dashboard")
    assert response.status_code == 200
    assert "Ops Lab" in response.text


def test_public_groups_stats_endpoint() -> None:
    response = client.get("/admin/public-groups/stats?days=7&top=5")
    assert response.status_code == 200
    data = response.json()
    assert data["totals"]["view"] >= 1
    assert data["conversion"]["click_rate"] >= 0


def _extract_csrf(path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert match
    return match.group(1)


def test_public_groups_activity_management() -> None:
    token = _extract_csrf("/admin/public-groups/activities")
    start_at = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
    end_at = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
    create_resp = client.post(
        "/admin/public-groups/activities",
        data={
            "csrf_token": token,
            "name": "Test Bonus",
            "description": "auto bonus",
            "start_at": start_at,
            "end_at": end_at,
            "reward_points": "5",
            "bonus_points": "2",
            "highlight_slots": "1",
            "highlight_enabled": "true",
            "daily_cap": "10",
            "total_cap": "100",
            "front_title": "Launch Highlight",
            "front_subtitle": "Double rewards today",
            "front_cta_label": "Join Now",
            "front_cta_link": "https://t.me/+ops_lab",
            "front_badge": "⚡ Hot",
            "front_priority": "10",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 303

    with get_session() as session:
        activity = (
            session.execute(select(PublicGroupActivity).order_by(PublicGroupActivity.id.desc()))
            .scalars()
            .first()
        )
        assert activity is not None
        card = activity.config.get("front_card") if activity.config else {}
        assert card.get("title") == "Launch Highlight"
        assert card.get("subtitle") == "Double rewards today"
        assert card.get("cta_label") == "Join Now"
        assert card.get("cta_link") == "https://t.me/+ops_lab"
        assert card.get("badge") == "⚡ Hot"
        assert card.get("priority") == 10

    token_toggle = _extract_csrf("/admin/public-groups/activities")
    toggle_resp = client.post(
        f"/admin/public-groups/activities/{activity.id}/toggle",
        data={
            "csrf_token": token_toggle,
            "active": "false",
        },
        follow_redirects=False,
    )
    assert toggle_resp.status_code == 303

    with get_session() as session:
        updated = session.get(PublicGroupActivity, activity.id)
        assert updated is not None
        assert updated.status == PublicGroupActivityStatus.PAUSED


def test_public_groups_bulk_status_endpoint() -> None:
    with get_session() as session:
        group_a, _ = create_group(
            session,
            creator_tg_id=82001,
            name="Bulk Review A",
            invite_link="https://t.me/+bulk_review_a",
            description="Batch approval target A",
        )
        group_b, _ = create_group(
            session,
            creator_tg_id=82002,
            name="Bulk Review B",
            invite_link="https://t.me/+bulk_review_b",
            description="Batch approval target B",
        )
        session.commit()

    payload = {
        "group_ids": [group_a.id, group_b.id, 999999],
        "action": "pause",
        "note": "bulk moderation test",
    }

    resp = client.post("/admin/public-groups/bulk/status", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == PublicGroupStatus.PAUSED.value
    assert body["updated_count"] == 2
    assert sorted(body["updated"]) == sorted([group_a.id, group_b.id])
    assert any(item["group_id"] == 999999 for item in body["errors"])

    with get_session() as session:
        refreshed_a = session.get(PublicGroup, group_a.id)
        refreshed_b = session.get(PublicGroup, group_b.id)
        assert refreshed_a.status == PublicGroupStatus.PAUSED
        assert refreshed_b.status == PublicGroupStatus.PAUSED


def test_public_group_reports_page() -> None:
    response = client.get("/admin/public-groups/reports")
    assert response.status_code == 200
    assert "Ops Lab" in response.text


def test_public_group_report_detail_and_actions() -> None:
    with get_session() as session:
        report = session.execute(
            select(PublicGroupReport).order_by(PublicGroupReport.id.desc())
        ).scalars().first()
        assert report is not None
        report_id = report.id

    token = _extract_csrf(f"/admin/public-groups/reports/{report_id}")
    update_resp = client.post(
        f"/admin/public-groups/reports/{report_id}/status",
        data={
            "csrf_token": token,
            "status": PublicGroupReportStatus.IN_PROGRESS.value,
            "assigned_operator": "99999",
            "priority": "2",
            "resolution_note": "Investigating spam content",
        },
        follow_redirects=False,
    )
    assert update_resp.status_code == 303

    note_token = _extract_csrf(f"/admin/public-groups/reports/{report_id}")
    note_resp = client.post(
        f"/admin/public-groups/reports/{report_id}/notes",
        data={
            "csrf_token": note_token,
            "content": "Contacted group owner for clarification.",
        },
        follow_redirects=False,
    )
    assert note_resp.status_code == 303

    with get_session() as session:
        refreshed = session.get(PublicGroupReport, report_id)
        assert refreshed is not None
        assert refreshed.status == PublicGroupReportStatus.IN_PROGRESS
        assert refreshed.assigned_operator == 99999
        assert refreshed.priority == 2
        notes = refreshed.notes
        assert any("clarification" in note.content for note in notes)


def test_public_group_report_inline_endpoint() -> None:
    with get_session() as session:
        report = session.execute(
            select(PublicGroupReport).order_by(PublicGroupReport.id.desc())
        ).scalars().first()
        assert report is not None
        report_id = report.id

    token = _extract_csrf(f"/admin/public-groups/reports/{report_id}")
    resp = client.post(
        f"/admin/public-groups/reports/{report_id}/inline",
        json={
            "status": PublicGroupReportStatus.RESOLVED.value,
            "resolution_note": "Closed via inline endpoint",
        },
        headers={"x-csrf-token": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == PublicGroupReportStatus.RESOLVED.value

    with get_session() as session:
        refreshed = session.get(PublicGroupReport, report_id)
        assert refreshed is not None
        assert refreshed.status == PublicGroupReportStatus.RESOLVED
        assert refreshed.resolution_note == "Closed via inline endpoint"


def test_public_group_activity_bulk_endpoint() -> None:
    with get_session() as session:
        activity = (
            session.execute(select(PublicGroupActivity).order_by(PublicGroupActivity.id.desc()))
            .scalars()
            .first()
        )
        assert activity is not None
        activity_id = activity.id

    token = _extract_csrf("/admin/public-groups/activities")
    pause_resp = client.post(
        "/admin/public-groups/activities/bulk",
        json={
            "activity_ids": [activity_id],
            "action": "pause",
        },
        headers={"x-csrf-token": token},
    )
    assert pause_resp.status_code == 200
    pause_body = pause_resp.json()
    assert pause_body["updated_count"] == 1

    with get_session() as session:
        paused = session.get(PublicGroupActivity, activity_id)
        assert paused is not None
        assert paused.status == PublicGroupActivityStatus.PAUSED

    update_resp = client.post(
        "/admin/public-groups/activities/bulk",
        json={
            "activity_ids": [activity_id],
            "action": "update",
            "reward_points": 42,
            "highlight_slots": 3,
        },
        headers={"x-csrf-token": token},
    )
    assert update_resp.status_code == 200
    update_body = update_resp.json()
    assert update_body["updated_count"] == 1

    with get_session() as session:
        updated = session.get(PublicGroupActivity, activity_id)
        assert updated is not None
        assert updated.reward_points == 42
        assert updated.highlight_slots == 3

    resume_resp = client.post(
        "/admin/public-groups/activities/bulk",
        json={
            "activity_ids": [activity_id],
            "action": "resume",
        },
        headers={"x-csrf-token": token},
    )
    assert resume_resp.status_code == 200
    resume_body = resume_resp.json()
    assert resume_body["updated_count"] == 1

    with get_session() as session:
        resumed = session.get(PublicGroupActivity, activity_id)
        assert resumed is not None
        assert resumed.status == PublicGroupActivityStatus.ACTIVE
        assert resumed.reward_points == 42

    export_resp = client.get(f"/admin/public-groups/activities/export?activity_ids={activity_id}")
    assert export_resp.status_code == 200
    assert "activity_id" in export_resp.text
    assert str(activity_id) in export_resp.text


def test_public_groups_activity_report_page() -> None:
    response = client.get("/admin/public-groups/activities/report")
    assert response.status_code == 200
    assert "Launch Bonus" in response.text


def test_public_groups_activity_report_csv() -> None:
    response = client.get("/admin/public-groups/activities/report?format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "activity_id" in response.text
    assert "webhook_failures" in response.text


def test_public_groups_activity_report_alerts_only() -> None:
    response = client.get("/admin/public-groups/activities/report?alerts_only=1")
    assert response.status_code == 200
    assert "Launch Bonus" not in response.text


def test_public_groups_activity_insights_page() -> None:
    response = client.get("/admin/public-groups/activities/insights")
    assert response.status_code == 200
    assert "Activity" in response.text or "活动" in response.text


def test_public_groups_activity_insights_data() -> None:
    response = client.get("/admin/public-groups/activities/insights/data?days=7")
    assert response.status_code == 200
    payload = response.json()
    assert "summary" in payload
    assert payload["summary"]["total_conversions"] >= 0
    assert isinstance(payload.get("daily_trend"), list)

