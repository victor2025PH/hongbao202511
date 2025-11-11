# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ["DATABASE_URL"] = "sqlite:///./test_api_public.sqlite"
os.environ["FLAG_ENABLE_PUBLIC_GROUPS"] = "1"
os.environ["ACTIVITY_SLACK_WEBHOOK"] = ""
os.environ["REPORT_SLACK_WEBHOOK"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from config.feature_flags import flags  # noqa: E402
from config.settings import settings  # noqa: E402
from miniapp.main import app  # noqa: E402
from models.db import get_session, init_db  # noqa: E402
from sqlalchemy import select  # noqa: E402

from sqlalchemy import func  # noqa: E402

from services.public_group_activity import create_activity  # noqa: E402
from services.public_group_service import create_group  # noqa: E402
from models.public_group import (  # noqa: E402
    PublicGroupActivityWebhook,
    PublicGroupReport,
)


client = TestClient(app)
_TEST_DB = Path("test_api_public.sqlite")


def setup_module() -> None:
    flags.ENABLE_PUBLIC_GROUPS = True
    settings.ADMIN_IDS = [99999]
    settings.SUPER_ADMINS = [99999]
    _TEST_DB.unlink(missing_ok=True)
    init_db()
    with get_session() as session:
        create_activity(
            session,
            name="Launch Bonus",
            start_at=datetime.utcnow() - timedelta(hours=1),
            end_at=datetime.utcnow() + timedelta(days=1),
            reward_points=2,
            bonus_points=3,
            highlight_slots=1,
            is_highlight_enabled=True,
        )
        session.commit()


def teardown_module() -> None:
    client.close()
    try:
        _TEST_DB.unlink()
    except (PermissionError, FileNotFoundError):
        pass


def _admin_headers() -> dict[str, str]:
    return {"X-TG-USER-ID": "99999"}


def _user_headers(tg_id: int) -> dict[str, str]:
    return {"X-TG-USER-ID": str(tg_id)}


def test_public_group_api_flow() -> None:
    payload = {
        "name": "Study Buddies",
        "invite_link": "https://t.me/+study_buddies",
        "description": "Focus & games",
        "tags": ["study", "games"],
        "language": "en",
        "entry_reward_enabled": True,
        "entry_reward_points": 10,
        "entry_reward_pool_max": 100,
    }
    create_resp = client.post("/v1/groups/public", json=payload, headers=_admin_headers())
    assert create_resp.status_code == 201, create_resp.text
    body = create_resp.json()
    group_id = body["group"]["id"]
    assert body["risk"]["score"] == 0

    list_resp = client.get("/v1/groups/public")
    assert list_resp.status_code == 200
    groups = list_resp.json()
    assert any(g["id"] == group_id for g in groups)
    assert all("is_bookmarked" in g for g in groups)

    detail_resp = client.get(f"/v1/groups/public/{group_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["name"] == payload["name"]

    join_resp = client.post(f"/v1/groups/public/{group_id}/join", headers=_user_headers(20001))
    assert join_resp.status_code == 200
    join_data = join_resp.json()
    assert join_data["membership_created"] is True
    assert join_data["reward_claimed"] is True

    # second join should be idempotent
    join_again = client.post(f"/v1/groups/public/{group_id}/join", headers=_user_headers(20001))
    assert join_again.status_code == 200
    assert join_again.json()["membership_created"] is False

    # second group for sorting/filter tests
    payload2 = {
        "name": "Focus Group",
        "invite_link": "https://t.me/+focus_group",
        "description": "Deep focus sessions",
        "tags": ["Focus", "Work"],
        "language": "en",
        "entry_reward_enabled": True,
        "entry_reward_points": 5,
        "entry_reward_pool_max": 50,
    }
    create_resp2 = client.post("/v1/groups/public", json=payload2, headers=_admin_headers())
    assert create_resp2.status_code == 201, create_resp2.text
    group_id2 = create_resp2.json()["group"]["id"]

    client.post(f"/v1/groups/public/{group_id2}/join", headers=_user_headers(20002))
    client.post(f"/v1/groups/public/{group_id2}/join", headers=_user_headers(20003))

    list_sorted = client.get("/v1/groups/public", params={"sort": "members"})
    assert list_sorted.status_code == 200
    sorted_ids = [g["id"] for g in list_sorted.json()]
    assert sorted_ids[0] == group_id2

    search_resp = client.get("/v1/groups/public", params={"q": "focus"})
    assert search_resp.status_code == 200
    search_ids = [g["id"] for g in search_resp.json()]
    assert search_ids[0] == group_id2
    assert group_id2 in search_ids

    tag_resp = client.get("/v1/groups/public", params=[("tags", "focus")])
    assert tag_resp.status_code == 200
    assert [g["id"] for g in tag_resp.json()] == [group_id2]

    # non-admin pin forbidden
    pin_forbidden = client.post(f"/v1/groups/public/{group_id}/pin", headers=_user_headers(30001))
    assert pin_forbidden.status_code == 403

    pin_resp = client.post(f"/v1/groups/public/{group_id}/pin", headers=_admin_headers())
    assert pin_resp.status_code == 200
    assert pin_resp.json()["is_pinned"] is True

    unpin_resp = client.post(f"/v1/groups/public/{group_id}/unpin", headers=_admin_headers())
    assert unpin_resp.status_code == 200
    assert unpin_resp.json()["is_pinned"] is False

    # update description & tags
    patch_resp = client.patch(
        f"/v1/groups/public/{group_id}",
        json={"description": "Updated desc", "tags": ["focus", "fun"]},
        headers=_admin_headers(),
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["description"] == "Updated desc"
    assert patch_resp.json()["tags"] == ["focus", "fun"]

    # third group to increase creation frequency
    payload3 = {
        "name": "Chill Corner",
        "invite_link": "https://t.me/+chill_corner",
        "description": "Relax together",
    }
    create_resp3 = client.post("/v1/groups/public", json=payload3, headers=_admin_headers())
    assert create_resp3.status_code == 201

    # group requiring review
    review_payload = {
        "name": "Casino Lovers",
        "invite_link": "https://t.me/+casino_lovers",
        "description": "casino time",
    }
    review_resp = client.post("/v1/groups/public", json=review_payload, headers=_admin_headers())
    assert review_resp.status_code == 201
    review_id = review_resp.json()["group"]["id"]
    assert review_resp.json()["risk"]["requires_review"] is True

    link_resp = client.get(f"/v1/groups/public/{group_id}/invite_link", headers=_user_headers(20001))
    assert link_resp.status_code == 200
    assert link_resp.json()["invite_link"] == payload["invite_link"]

    report_resp = client.post(
        f"/v1/groups/public/{group_id}/report",
        json={"reason": "spam", "details": "looks like spam"},
        headers=_user_headers(20002),
    )
    assert report_resp.status_code == 202

    # include_review flag only for admin
    default_list = client.get("/v1/groups/public")
    assert all(g["id"] != review_id for g in default_list.json())

    review_list = client.get(
        "/v1/groups/public",
        params={"include_review": "true"},
        headers=_admin_headers(),
    )
    assert review_list.status_code == 200
    assert any(g["id"] == review_id for g in review_list.json())


def test_public_group_event_tracking() -> None:
    payload = {
        "name": "Analytics Lab",
        "invite_link": "https://t.me/+analytics_lab",
        "description": "Discuss data insights",
        "tags": ["data", "analysis"],
    }
    create_resp = client.post("/v1/groups/public", json=payload, headers=_admin_headers())
    assert create_resp.status_code == 201, create_resp.text
    group_id = create_resp.json()["group"]["id"]

    view_resp = client.post(
        f"/v1/groups/public/{group_id}/events",
        json={"event_type": "view", "context": {"slot": "hero"}},
        headers=_user_headers(50001),
    )
    assert view_resp.status_code == 201, view_resp.text
    body = view_resp.json()
    assert body["event_type"] == "view"
    assert body["group_id"] == group_id
    assert body["event_id"] > 0

    click_resp = client.post(
        f"/v1/groups/public/{group_id}/events",
        json={"event_type": "click"},
        headers=_user_headers(50001),
    )
    assert click_resp.status_code == 201

    invalid_resp = client.post(
        f"/v1/groups/public/{group_id}/events",
        json={"event_type": "invalid"},
        headers=_user_headers(50001),
    )
    assert invalid_resp.status_code == 400

    join_resp = client.post(f"/v1/groups/public/{group_id}/join", headers=_user_headers(50001))
    assert join_resp.status_code == 200

    stats_forbidden = client.get("/v1/groups/public/stats/summary", headers=_user_headers(12345))
    assert stats_forbidden.status_code == 403

    stats_resp = client.get(
        "/v1/groups/public/stats/summary",
        params={"period": "7d", "limit": 5},
        headers=_admin_headers(),
    )
    assert stats_resp.status_code == 200, stats_resp.text
    stats = stats_resp.json()
    assert stats["totals"]["view"] >= 1
    assert stats["totals"]["click"] >= 1
    assert stats["totals"]["join"] >= 1
    assert stats["conversion"]["join_rate"] >= 0
    assert stats["top_groups"]

    invalid_period = client.get(
        "/v1/groups/public/stats/summary",
        params={"period": "foo"},
        headers=_admin_headers(),
    )
    assert invalid_period.status_code == 400


def test_public_group_active_activities_endpoint() -> None:
    resp = client.get("/v1/groups/public/activities", headers=_user_headers(60001))
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data, "expected at least one active campaign"
    first = data[0]
    assert "reward_points" in first
    assert "bonus_points" in first
    assert "highlight_enabled" in first
    assert "headline" in first
    assert first["headline"] is None or "pts" in first["headline"] or "Highlight" in first["headline"]
    if first["highlight_enabled"]:
        assert first["highlight_badge"] in ("Highlight ×1", "Highlight active", "Highlight ×0", "highlight ×1")
    assert "countdown_text" in first
    assert "front_card" in first
    assert isinstance(first["front_card"], dict)
    assert first["front_card"].get("title")
    assert "countdown_seconds" in first["front_card"]
    assert "front_priority" in first
    assert "has_participated" in first


def test_public_group_activity_detail_endpoint() -> None:
    resp = client.get("/v1/groups/public/activities", headers=_user_headers(60001))
    assert resp.status_code == 200
    activities = resp.json()
    activity_id = activities[0]["id"]

    detail_resp = client.get(
        f"/v1/groups/public/activities/{activity_id}",
        headers=_user_headers(60001),
    )
    assert detail_resp.status_code == 200
    body = detail_resp.json()
    assert body["id"] == activity_id
    assert body["front_card"]["title"]
    assert "eligible" in body
    assert "rules" in body
    assert isinstance(body["rules"], list)
    assert any(rule["key"] == "reward_points" for rule in body["rules"])
    assert body["total_points"] >= body["reward_points"]


def test_public_group_bookmarks_flow() -> None:
    payload = {
        "name": "Bookmark Demo",
        "invite_link": "https://t.me/+bookmark_demo",
        "description": "Bookmark testing group",
        "tags": ["demo"],
        "language": "en",
        "entry_reward_enabled": True,
        "entry_reward_points": 3,
        "entry_reward_pool_max": 30,
    }
    create_resp = client.post("/v1/groups/public", json=payload, headers=_admin_headers())
    assert create_resp.status_code == 201, create_resp.text
    group_id = create_resp.json()["group"]["id"]

    user_headers = _user_headers(61001)

    bookmark_resp = client.post(f"/v1/groups/public/{group_id}/bookmark", headers=user_headers)
    assert bookmark_resp.status_code == 201
    assert bookmark_resp.json()["bookmarked"] is True

    bookmark_again = client.post(f"/v1/groups/public/{group_id}/bookmark", headers=user_headers)
    assert bookmark_again.status_code == 200
    assert bookmark_again.json()["bookmarked"] is True

    bookmark_list = client.get("/v1/groups/public/bookmarks", headers=user_headers)
    assert bookmark_list.status_code == 200
    data = bookmark_list.json()
    assert data and data[0]["id"] == group_id
    assert data[0]["is_bookmarked"] is True

    list_resp = client.get("/v1/groups/public", headers=user_headers)
    assert list_resp.status_code == 200
    assert any(item["id"] == group_id and item["is_bookmarked"] is True for item in list_resp.json())

    detail_resp = client.get(f"/v1/groups/public/{group_id}", headers=user_headers)
    assert detail_resp.status_code == 200
    assert detail_resp.json()["is_bookmarked"] is True

    remove_resp = client.delete(f"/v1/groups/public/{group_id}/bookmark", headers=user_headers)
    assert remove_resp.status_code == 200
    assert remove_resp.json()["bookmarked"] is False

    remove_again = client.delete(f"/v1/groups/public/{group_id}/bookmark", headers=user_headers)
    assert remove_again.status_code == 200
    assert remove_again.json()["bookmarked"] is False

    detail_after = client.get(f"/v1/groups/public/{group_id}", headers=user_headers)
    assert detail_after.status_code == 200
    assert detail_after.json()["is_bookmarked"] is False

    bookmark_list_after = client.get("/v1/groups/public/bookmarks", headers=user_headers)
    assert bookmark_list_after.status_code == 200
    assert bookmark_list_after.json() == []


def test_public_group_report_endpoint_creates_case() -> None:
    with get_session() as session:
        group, _ = create_group(
            session,
            creator_tg_id=72001,
            name="Report Sandbox",
            invite_link="https://t.me/+report_sandbox",
        )
        session.commit()
        group_id = group.id

    payload = {"reason": "spam", "details": "Advertising links all day"}
    resp = client.post(
        f"/v1/groups/public/{group_id}/report",
        json=payload,
        headers=_user_headers(72002),
    )
    assert resp.status_code == 202

    with get_session() as session:
        total = session.execute(
            select(func.count()).select_from(PublicGroupReport).where(PublicGroupReport.group_id == group_id)
        ).scalar_one()
        assert total == 1
        case = session.execute(
            select(PublicGroupReport).where(PublicGroupReport.group_id == group_id)
        ).scalar_one()
        assert case.report_type == "spam"
        assert (case.description or "").startswith("Advertising")


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_public_group_activity_webhook_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.public_group_activity.httpx.post",
        lambda *args, **kwargs: type("Resp", (), {"status_code": 200})(),
    )
    activities_resp = client.get("/v1/groups/public/activities", headers=_user_headers(99999))
    assert activities_resp.status_code == 200
    activity_id = activities_resp.json()[0]["id"]

    create_resp = client.post(
        f"/v1/groups/public/activities/{activity_id}/webhooks",
        json={"url": "https://example.com/notify", "secret": "demo", "is_active": True},
        headers=_admin_headers(),
    )
    assert create_resp.status_code == 201
    webhook_id = create_resp.json()["id"]

    list_resp = client.get(
        f"/v1/groups/public/activities/{activity_id}/webhooks",
        headers=_admin_headers(),
    )
    assert list_resp.status_code == 200
    assert any(item["id"] == webhook_id for item in list_resp.json())

    forbidden = client.post(
        f"/v1/groups/public/activities/{activity_id}/webhooks",
        json={"url": "https://example.com/forbidden"},
        headers=_user_headers(20003),
    )
    assert forbidden.status_code == 403

    delete_resp = client.delete(
        f"/v1/groups/public/activities/webhooks/{webhook_id}",
        params={"hard": True},
        headers=_admin_headers(),
    )
    assert delete_resp.status_code == 204

    list_after = client.get(
        f"/v1/groups/public/activities/{activity_id}/webhooks",
        headers=_admin_headers(),
    )
    assert list_after.status_code == 200
    assert all(item["id"] != webhook_id for item in list_after.json())

    with get_session() as session:
        record = session.execute(
            select(PublicGroupActivityWebhook).where(PublicGroupActivityWebhook.id == webhook_id)
        ).scalar_one_or_none()
        assert record is None or record.is_active is False