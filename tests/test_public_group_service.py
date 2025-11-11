from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from models.user import User  # noqa: E402

# 运行测试前强制使用独立的 SQLite 数据库，避免污染生产库
os.environ["DATABASE_URL"] = "sqlite:///./test_public_group.sqlite"
_prev_public_group_flag = os.environ.get("FLAG_ENABLE_PUBLIC_GROUPS")
os.environ["FLAG_ENABLE_PUBLIC_GROUPS"] = "1"

from models.db import engine, get_session, init_db  # noqa: E402
from models.public_group import PublicGroup, PublicGroupStatus  # noqa: E402
from services.public_group_service import (  # noqa: E402
    PublicGroupError,
    bulk_set_group_status,
    create_group,
    evaluate_group_risk,
    join_group,
    list_groups,
    pin_group,
    set_group_status,
)
from services.public_group_activity import create_activity  # noqa: E402


def setup_module() -> None:
    Path("test_public_group.sqlite").unlink(missing_ok=True)
    init_db()


def teardown_module() -> None:
    if _prev_public_group_flag is None:
        os.environ.pop("FLAG_ENABLE_PUBLIC_GROUPS", None)
    else:
        os.environ["FLAG_ENABLE_PUBLIC_GROUPS"] = _prev_public_group_flag


@pytest.fixture(autouse=True)
def _clean_group_tables():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM public_group_activity_logs"))
        conn.execute(text("DELETE FROM public_group_activities"))
        conn.execute(text("DELETE FROM public_group_reward_claims"))
        conn.execute(text("DELETE FROM public_group_members"))
        conn.execute(text("DELETE FROM public_groups"))


def test_create_group_and_risk_flags() -> None:
    with get_session() as session:
        group, risk = create_group(
            session,
            creator_tg_id=1001,
            name="Study & Chill",
            invite_link="https://t.me/+study_group",
            description="Focused coworking and games",
            tags=["study", "games"],
        )
        assert group.id is not None
        assert group.status == PublicGroupStatus.ACTIVE
        assert risk.score == 0
        assert risk.flags == []

        risk2 = evaluate_group_risk(
            session,
            creator_tg_id=1002,
            name="Study Duplicate",
            description=None,
            tags=[],
            invite_link="https://t.me/+study_group",
        )
        assert "duplicate_invite_link" in risk2.flags
        assert risk2.score >= 5

        with pytest.raises(PublicGroupError):
            create_group(
                session,
                creator_tg_id=1002,
                name="Study Duplicate",
                invite_link="https://t.me/+study_group",
            )


def test_join_group_reward_flow() -> None:
    with get_session() as session:
        group, _ = create_group(
            session,
            creator_tg_id=2001,
            name="Music Lovers",
            invite_link="https://t.me/+music_group",
            entry_reward_points=10,
            entry_reward_pool_max=20,
        )

        result = join_group(session, group_id=group.id, user_tg_id=3001)
        assert result["membership_created"] is True
        assert result["reward_claimed"] is True
        assert result["reward_points"] == 10

        # Second join should not duplicate reward
        result2 = join_group(session, group_id=group.id, user_tg_id=3001)
        assert result2["membership_created"] is False
        assert result2["reward_claimed"] is False


def test_pin_group_limit() -> None:
    with get_session() as session:
        created_ids = []
        for idx in range(3):
            group, _ = create_group(
                session,
                creator_tg_id=4000 + idx,
                name=f"Group {idx}",
                invite_link=f"https://t.me/+group_{idx}",
            )
            created_ids.append(group.id)
            pin_group(session, group_id=group.id, operator_tg_id=9999)

        # Exceeding limit should raise
        group4, _ = create_group(
            session,
            creator_tg_id=5000,
            name="Group overflow",
            invite_link="https://t.me/+group_overflow",
        )
        with pytest.raises(PublicGroupError):
            pin_group(session, group_id=group4.id, operator_tg_id=9999)


def test_list_groups_ordering() -> None:
    with get_session() as session:
        group, _ = create_group(
            session,
            creator_tg_id=6001,
            name="Pinned First",
            invite_link="https://t.me/+pinned_first",
        )
        pin_group(session, group_id=group.id, operator_tg_id=6001)

        group2, _ = create_group(
            session,
            creator_tg_id=6002,
            name="Regular Second",
            invite_link="https://t.me/+regular_second",
        )

        groups = list_groups(session, limit=5)
        assert groups[0].id == group.id
        assert group2.id in [g.id for g in groups]


def test_bulk_set_group_status() -> None:
    with get_session() as session:
        groups = []
        for idx in range(3):
            group, _ = create_group(
                session,
                creator_tg_id=7000 + idx,
                name=f"Bulk Target {idx}",
                invite_link=f"https://t.me/+bulk_target_{idx}",
            )
            groups.append(group)
        session.commit()

    group_ids = [g.id for g in groups]

    with get_session() as session:
        summary = bulk_set_group_status(
            session,
            group_ids=group_ids + [999999],
            target_status=PublicGroupStatus.PAUSED,
            operator_tg_id=4242,
            note="bulk moderation",
        )
        assert set(summary["updated"]) == set(group_ids)
        assert any(err["group_id"] == 999999 for err in summary["errors"])
        session.commit()

    with get_session() as session:
        refreshed = [session.get(PublicGroup, gid) for gid in group_ids]
        assert all(item.status == PublicGroupStatus.PAUSED for item in refreshed)


def test_search_tags_and_status_update() -> None:
    with get_session() as session:
        group, _ = create_group(
            session,
            creator_tg_id=7001,
            name="Focus Factory",
            invite_link="https://t.me/+focus_factory",
            tags=["focus", "work"],
            description="Deep work sessions",
        )
        group2, _ = create_group(
            session,
            creator_tg_id=7002,
            name="Casual Corner",
            invite_link="https://t.me/+casual_corner",
            tags=["chill"],
            description="Relax and chat",
        )

        results = list_groups(session, limit=10, search="focus")
        assert results and results[0].id == group.id

        tag_filtered = list_groups(session, limit=10, tags=["focus"])
        ids = [g.id for g in tag_filtered]
        assert group.id in ids and group2.id not in ids

        set_group_status(session, group_id=group2.id, target_status=PublicGroupStatus.PAUSED, operator_tg_id=999)
        session.commit()
        refreshed = session.get(PublicGroup, group2.id)
        assert refreshed.status == PublicGroupStatus.PAUSED


def test_join_group_with_activity_bonus() -> None:
    with get_session() as session:
        group, _ = create_group(
            session,
            creator_tg_id=8001,
            name="Bonus Test",
            invite_link="https://t.me/+bonus_group",
            entry_reward_enabled=False,
        )
        create_activity(
            session,
            name="Daily Bonus",
            start_at=datetime.utcnow() - timedelta(days=1),
            end_at=None,
            reward_points=7,
            daily_cap=10,
            total_cap=100,
        )
        session.commit()

        payload = join_group(session, group_id=group.id, user_tg_id=91001)
        session.commit()

        assert payload["bonus_points"] == 7
        assert payload["bonus_details"]

        payload_second = join_group(session, group_id=group.id, user_tg_id=91001)
        session.commit()
        assert payload_second["bonus_points"] == 0

