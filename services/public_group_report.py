from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models.public_group import (
    PublicGroup,
    PublicGroupReport,
    PublicGroupReportNote,
    PublicGroupReportStatus,
)

log = logging.getLogger("public_group.report")


class PublicGroupReportError(RuntimeError):
    """Raised when report operations fail."""


def create_report_case(
    session: Session,
    *,
    group_id: int,
    reporter_tg_id: Optional[int] = None,
    report_type: str = "general",
    description: Optional[str] = None,
    reporter_username: Optional[str] = None,
    contact: Optional[str] = None,
    metadata: Optional[Dict[str, object]] = None,
) -> PublicGroupReport:
    report = PublicGroupReport(
        group_id=int(group_id),
        reporter_tg_id=int(reporter_tg_id) if reporter_tg_id else None,
        reporter_username=(reporter_username or "").strip() or None,
        contact=(contact or "").strip() or None,
        report_type=(report_type or "general").strip().lower()[:32] or "general",
        description=(description or "").strip() or None,
    )
    if metadata:
        report.meta = metadata
    session.add(report)
    session.flush()
    log.warning(
        "public_group.report.created id=%s group=%s reporter=%s type=%s",
        report.id,
        report.group_id,
        report.reporter_tg_id,
        report.report_type,
    )
    return report


def _apply_report_filters(stmt, status: Optional[str], search: Optional[str]):
    if status:
        allowed = {item.value for item in PublicGroupReportStatus}
        statuses = [s.strip().lower() for s in status.split(",")]
        filtered = [s for s in statuses if s in allowed]
        if filtered:
            stmt = stmt.where(PublicGroupReport.status.in_(filtered))
    if search:
        keyword = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(PublicGroupReport.description).like(keyword),
                func.lower(PublicGroupReport.report_type).like(keyword),
                func.lower(PublicGroupReport.resolution_note).like(keyword),
                func.lower(PublicGroupReport.contact).like(keyword),
                func.lower(PublicGroupReport.reporter_username).like(keyword),
                func.lower(PublicGroup.name).like(keyword),
            )
        )
    return stmt


def list_reports(
    session: Session,
    *,
    status: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, object]:
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))
    base_stmt = select(PublicGroupReport.id).join(PublicGroup, PublicGroup.id == PublicGroupReport.group_id)
    base_stmt = _apply_report_filters(base_stmt, status, search)
    total = session.execute(select(func.count()).select_from(base_stmt.subquery())).scalar_one()

    data_stmt = (
        select(PublicGroupReport, PublicGroup)
        .join(PublicGroup, PublicGroup.id == PublicGroupReport.group_id)
        .order_by(PublicGroupReport.priority.desc(), PublicGroupReport.created_at.desc())
    )
    data_stmt = _apply_report_filters(data_stmt, status, search)
    rows = (
        session.execute(
            data_stmt.offset((page - 1) * page_size).limit(page_size)
        ).all()
    )

    items: List[Dict[str, object]] = []
    for report, group in rows:
        items.append(
            {
                "id": report.id,
                "group": {
                    "id": group.id,
                    "name": group.name,
                    "invite_link": group.invite_link,
                },
                "report_type": report.report_type,
                "status": report.status.value,
                "priority": report.priority,
                "assigned_operator": report.assigned_operator,
                "created_at": report.created_at.isoformat() if report.created_at else None,
                "updated_at": report.updated_at.isoformat() if report.updated_at else None,
                "reporter_tg_id": report.reporter_tg_id,
                "reporter_username": report.reporter_username,
                "description": report.description,
            }
        )

    totals_rows = session.execute(
        select(PublicGroupReport.status, func.count())
        .select_from(PublicGroupReport)
        .group_by(PublicGroupReport.status)
    ).all()
    status_totals = {
        status.value if isinstance(status, PublicGroupReportStatus) else str(status): int(count)
        for status, count in totals_rows
    }

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
        "status_totals": status_totals,
    }


def get_report_detail(session: Session, *, report_id: int) -> Optional[Dict[str, object]]:
    report = session.get(PublicGroupReport, int(report_id))
    if not report:
        return None
    group = session.get(PublicGroup, report.group_id)
    notes = (
        session.execute(
            select(PublicGroupReportNote).where(PublicGroupReportNote.report_id == report.id).order_by(PublicGroupReportNote.created_at.asc())
        )
        .scalars()
        .all()
    )
    return {
        "report": report,
        "group": group,
        "notes": notes,
    }


def update_report_case(
    session: Session,
    *,
    report_id: int,
    operator_tg_id: int,
    status: Optional[str] = None,
    assigned_operator: Optional[int] = None,
    priority: Optional[int] = None,
    resolution_note: Optional[str] = None,
) -> PublicGroupReport:
    report = session.get(PublicGroupReport, int(report_id))
    if not report:
        raise PublicGroupReportError("report_not_found")

    if status:
        try:
            status_enum = PublicGroupReportStatus(status)
        except Exception as exc:
            raise PublicGroupReportError("status_invalid") from exc
        report.status = status_enum
        if status_enum in {PublicGroupReportStatus.RESOLVED, PublicGroupReportStatus.DISMISSED}:
            report.resolved_at = datetime.utcnow()
        else:
            report.resolved_at = None

    if assigned_operator is not None:
        value = int(assigned_operator)
        report.assigned_operator = value if value > 0 else None

    if priority is not None:
        report.priority = max(0, int(priority))

    if resolution_note is not None:
        note = resolution_note.strip()
        report.resolution_note = note or None

    report.updated_at = datetime.utcnow()
    session.add(report)
    log.info(
        "public_group.report.updated id=%s operator=%s status=%s assign=%s",
        report.id,
        operator_tg_id,
        report.status.value,
        report.assigned_operator,
    )
    return report


def add_report_note(
    session: Session,
    *,
    report_id: int,
    operator_tg_id: int,
    content: str,
) -> PublicGroupReportNote:
    report = session.get(PublicGroupReport, int(report_id))
    if not report:
        raise PublicGroupReportError("report_not_found")
    note = content.strip()
    if not note:
        raise PublicGroupReportError("note_empty")
    entry = PublicGroupReportNote(
        report_id=report.id,
        operator_tg_id=int(operator_tg_id),
        content=note,
    )
    report.updated_at = datetime.utcnow()
    session.add(entry)
    session.add(report)
    session.flush()
    log.info(
        "public_group.report.note_added report=%s operator=%s",
        report.id,
        operator_tg_id,
    )
    return entry


