from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ["DATABASE_URL"] = "sqlite:///./test_public_group_reports.sqlite"

from models.db import get_session, init_db  # noqa: E402
from models.public_group import PublicGroupReport, PublicGroupReportStatus  # noqa: E402
from services.public_group_service import create_group  # noqa: E402
from services.public_group_report import (  # noqa: E402
    add_report_note,
    create_report_case,
    get_report_detail,
    list_reports,
    update_report_case,
)

_TEST_DB = Path("test_public_group_reports.sqlite")
GROUP_ID: int = 0


def setup_module() -> None:
    global GROUP_ID
    _TEST_DB.unlink(missing_ok=True)
    init_db()
    with get_session() as session:
        group, _ = create_group(
            session,
            creator_tg_id=50001,
            name="Report Demo",
            invite_link="https://t.me/+report_demo",
        )
        session.commit()
        GROUP_ID = group.id


def teardown_module() -> None:
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass


def test_create_and_list_reports() -> None:
    with get_session() as session:
        report = create_report_case(
            session,
            group_id=GROUP_ID,
            reporter_tg_id=70001,
            report_type="spam",
            description="Suspicious advertisements",
            metadata={"source": "test"},
        )
        session.commit()

    with get_session() as session:
        result = list_reports(session, status=None, search="spam", page=1, page_size=10)
        assert result["total"] >= 1
        assert any(item["id"] == report.id for item in result["items"])


def test_update_report_case_and_notes() -> None:
    with get_session() as session:
        report = create_report_case(
            session,
            group_id=GROUP_ID,
            reporter_tg_id=70002,
            report_type="abuse",
            description="Harassment",
        )
        session.commit()
        report_id = report.id

    with get_session() as session:
        update_report_case(
            session,
            report_id=report_id,
            operator_tg_id=99999,
            status=PublicGroupReportStatus.IN_PROGRESS.value,
            assigned_operator=88888,
            priority=3,
            resolution_note=None,
        )
        add_report_note(
            session,
            report_id=report_id,
            operator_tg_id=99999,
            content="Reviewed evidence, contacting group owner.",
        )
        session.commit()

    with get_session() as session:
        detail = get_report_detail(session, report_id=report_id)
        assert detail is not None
        report = detail["report"]
        assert report.status == PublicGroupReportStatus.IN_PROGRESS
        assert report.assigned_operator == 88888
        assert report.priority == 3
        notes = detail["notes"]
        assert any("contacting group owner" in note.content for note in notes)


