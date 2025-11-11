from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.i18n.i18n import t
from models.public_group import PublicGroupReportStatus
from services.public_group_report import (
    PublicGroupReportError,
    add_report_note,
    get_report_detail,
    list_reports,
    update_report_case,
)
from web_admin.deps import csrf_protect, db_session, db_session_ro, issue_csrf, require_admin, verify_csrf
from web_admin.services.audit_service import record_audit

router = APIRouter(prefix="/admin/public-groups/reports", tags=["admin-public-groups-reports"])


class InlineReportUpdate(BaseModel):
    status: Optional[str] = None
    assigned_operator: Optional[int] = None
    priority: Optional[int] = None
    resolution_note: Optional[str] = None


@router.get("", response_class=HTMLResponse)
def public_group_reports_page(
    req: Request,
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    db=Depends(db_session_ro),
    sess=Depends(require_admin),
):
    result = list_reports(db, status=status, search=search, page=page, page_size=20)
    templates = req.app.state.templates
    csrf_token = issue_csrf(req)
    current_operator = 0
    if isinstance(sess, dict):
        try:
            current_operator = int(sess.get("tg_id") or 0)
        except (TypeError, ValueError):
            current_operator = 0
    return templates.TemplateResponse(
        "public_groups_reports.html",
        {
            "request": req,
            "title": t("admin.public_groups_reports.title"),
            "nav_active": "public_group_reports",
            "reports": result["items"],
            "page": result["page"],
            "page_size": result["page_size"],
            "total": result["total"],
            "total_pages": result["total_pages"],
            "status_filter": status or "",
            "search": search or "",
            "statuses": [status_item.value for status_item in PublicGroupReportStatus],
            "csrf_token": csrf_token,
            "status_totals": result.get("status_totals", {}),
            "current_operator": current_operator,
        },
    )


@router.get("/{report_id}", response_class=HTMLResponse)
def public_group_report_detail_page(
    req: Request,
    report_id: int,
    db=Depends(db_session_ro),
    sess=Depends(require_admin),
):
    detail = get_report_detail(db, report_id=report_id)
    if not detail:
        raise HTTPException(status_code=404, detail="report_not_found")

    templates = req.app.state.templates
    csrf_token = issue_csrf(req)
    return templates.TemplateResponse(
        "public_groups_report_detail.html",
        {
            "request": req,
            "title": t("admin.public_groups_reports.detail_title"),
            "nav_active": "public_group_reports",
            "report": detail["report"],
            "group": detail["group"],
            "notes": detail["notes"],
            "statuses": [status_item for status_item in PublicGroupReportStatus],
            "csrf_token": csrf_token,
        },
    )


@router.post(
    "/{report_id}/status",
    response_class=RedirectResponse,
    dependencies=[Depends(csrf_protect)],
)
def public_group_report_update(
    report_id: int,
    status: Optional[str] = Form(None),
    assigned_operator: Optional[str] = Form(None),
    priority: Optional[str] = Form(None),
    resolution_note: Optional[str] = Form(None),
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    operator = 0
    if isinstance(sess, dict):
        try:
            operator = int(sess.get("tg_id") or 0)
        except (TypeError, ValueError):
            operator = 0
    if operator <= 0:
        raise HTTPException(status_code=403, detail="operator_required")

    status_value = (status or "").strip().lower() or None
    assigned_value = (assigned_operator or "").strip()
    priority_value = (priority or "").strip()
    try:
        update_report_case(
            db,
            report_id=report_id,
            operator_tg_id=operator,
            status=status_value,
            assigned_operator=int(assigned_value) if assigned_value else None,
            priority=int(priority_value) if priority_value else None,
            resolution_note=resolution_note,
        )
        db.commit()
        record_audit(
            action="public_group.report.update",
            operator=operator,
            payload={
                "report_id": report_id,
                "status": status_value,
                "assigned_operator": assigned_value or None,
                "priority": priority_value or None,
            },
        )
    except PublicGroupReportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=exc.args[0] if exc.args else "invalid") from exc
    except Exception:
        db.rollback()
        raise

    return RedirectResponse(
        f"/admin/public-groups/reports/{report_id}?ok=1",
        status_code=303,
    )


@router.post(
    "/{report_id}/notes",
    response_class=RedirectResponse,
    dependencies=[Depends(csrf_protect)],
)
def public_group_report_add_note(
    report_id: int,
    content: str = Form(...),
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    operator = 0
    if isinstance(sess, dict):
        try:
            operator = int(sess.get("tg_id") or 0)
        except (TypeError, ValueError):
            operator = 0
    if operator <= 0:
        raise HTTPException(status_code=403, detail="operator_required")

    try:
        add_report_note(
            db,
            report_id=report_id,
            operator_tg_id=operator,
            content=content,
        )
        db.commit()
        record_audit(
            action="public_group.report.note",
            operator=operator,
            payload={
                "report_id": report_id,
                "content": content.strip()[:120],
            },
        )
    except PublicGroupReportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=exc.args[0] if exc.args else "invalid") from exc
    except Exception:
        db.rollback()
        raise

    return RedirectResponse(
        f"/admin/public-groups/reports/{report_id}#notes",
        status_code=303,
    )


@router.post(
    "/{report_id}/inline",
    response_class=JSONResponse,
)
def public_group_report_update_inline(
    report_id: int,
    payload: InlineReportUpdate,
    req: Request,
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    header_token = req.headers.get("x-csrf-token") or req.headers.get("X-CSRF-Token") or ""
    if not verify_csrf(req, header_token, one_time=False):
        raise HTTPException(status_code=403, detail="csrf failed")

    operator = 0
    if isinstance(sess, dict):
        try:
            operator = int(sess.get("tg_id") or 0)
        except (TypeError, ValueError):
            operator = 0
    if operator <= 0:
        raise HTTPException(status_code=403, detail="operator_required")

    status_value = (payload.status or "").strip().lower() or None

    try:
        report = update_report_case(
            db,
            report_id=report_id,
            operator_tg_id=operator,
            status=status_value,
            assigned_operator=payload.assigned_operator,
            priority=payload.priority,
            resolution_note=payload.resolution_note,
        )
        db.commit()
        record_audit(
            action="public_group.report.inline_update",
            operator=operator,
            payload={
                "report_id": report_id,
                "status": status_value,
                "assigned_operator": payload.assigned_operator,
                "priority": payload.priority,
            },
        )
        return JSONResponse(
            {
                "ok": True,
                "status": report.status.value,
                "assigned_operator": report.assigned_operator,
                "priority": report.priority,
                "resolved_at": report.resolved_at.isoformat() if report.resolved_at else None,
            }
        )
    except PublicGroupReportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=exc.args[0] if exc.args else "invalid") from exc
    except Exception:
        db.rollback()
        raise


