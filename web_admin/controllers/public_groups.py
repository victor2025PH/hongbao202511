# web_admin/controllers/public_groups.py
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from core.i18n.i18n import t
from services.public_group_service import (
    RISK_SCORE_THRESHOLD_REVIEW,
    PublicGroupError,
    bulk_set_group_status,
    serialize_group,
    set_group_status,
)
from services.public_group_tracking import fetch_dashboard_metrics
from services.public_group_activity import (
    get_active_campaign_summaries,
    summarize_activity_performance,
    summarize_conversions,
    summarize_conversion_overview,
    daily_conversion_trend,
    find_conversion_alerts,
)
from services.public_group_activity import (
    bulk_update_activities,
    create_activity as svc_create_activity,
    list_activities as svc_list_activities,
    toggle_activity as svc_toggle_activity,
)
from models.public_group import PublicGroup, PublicGroupStatus
from web_admin.deps import csrf_protect, db_session, db_session_ro, issue_csrf, require_admin, verify_csrf
from web_admin.services.audit_service import record_audit

router = APIRouter(prefix="/admin/public-groups", tags=["admin-public-groups"])


class BulkStatusRequest(BaseModel):
    group_ids: List[int]
    action: str
    note: Optional[str] = None


class BulkActivityRequest(BaseModel):
    activity_ids: List[int]
    action: str
    reward_points: Optional[int] = None
    bonus_points: Optional[int] = None
    highlight_slots: Optional[int] = None
    daily_cap: Optional[int] = None
    total_cap: Optional[int] = None
    highlight_enabled: Optional[bool] = None


def _fetch_groups_for_review(db: Session) -> List[PublicGroup]:
    return (
        db.query(PublicGroup)
        .filter(PublicGroup.status == PublicGroupStatus.REVIEW)
        .order_by(PublicGroup.created_at.desc())
        .limit(100)
        .all()
    )


def _fetch_high_risk(db: Session) -> List[PublicGroup]:
    return (
        db.query(PublicGroup)
        .filter(PublicGroup.risk_score >= RISK_SCORE_THRESHOLD_REVIEW)
        .order_by(PublicGroup.risk_score.desc(), PublicGroup.created_at.desc())
        .limit(100)
        .all()
    )


def _fetch_by_status(db: Session, status: PublicGroupStatus) -> List[PublicGroup]:
    return (
        db.query(PublicGroup)
        .filter(PublicGroup.status == status)
        .order_by(PublicGroup.updated_at.desc())
        .limit(100)
        .all()
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def public_groups_page(
    req: Request,
    db=Depends(db_session_ro),
    sess=Depends(require_admin),
):
    review_groups = _fetch_groups_for_review(db)
    high_risk_groups = _fetch_high_risk(db)
    paused_groups = _fetch_by_status(db, PublicGroupStatus.PAUSED)
    removed_groups = _fetch_by_status(db, PublicGroupStatus.REMOVED)

    templates = req.app.state.templates
    return templates.TemplateResponse(
        "public_groups.html",
        {
            "request": req,
            "title": t("admin.public_groups.title"),
            "nav_active": "public_groups",
            "review_groups": [serialize_group(g) for g in review_groups],
            "high_risk_groups": [serialize_group(g) for g in high_risk_groups],
            "paused_groups": [serialize_group(g) for g in paused_groups],
            "removed_groups": [serialize_group(g) for g in removed_groups],
            "csrf_token": issue_csrf(req),
            "active_campaigns": get_active_campaign_summaries(db),
        },
    )


@router.get("/dashboard", response_class=HTMLResponse)
def public_groups_dashboard(
    req: Request,
    days: int = Query(14, ge=1, le=90),
    top: int = Query(5, ge=1, le=20),
    db=Depends(db_session_ro),
    sess=Depends(require_admin),
):
    summary = fetch_dashboard_metrics(db, days=days, top_n=top)
    summary_json = json.dumps(summary, ensure_ascii=False)
    templates = req.app.state.templates
    return templates.TemplateResponse(
        "public_groups_dashboard.html",
        {
            "request": req,
            "title": t("admin.public_groups.dashboard_title"),
            "nav_active": "public_groups",
            "summary_json": summary_json,
            "days": days,
            "top": top,
        },
    )


@router.post("/status/{group_id}", response_class=RedirectResponse)
def public_group_set_status(
    req: Request,
    group_id: int,
    action: str = Form(...),
    note: str = Form(""),
    db=Depends(db_session),
    sess=Depends(require_admin),
    _=Depends(csrf_protect),
):
    try:
        if action == "approve":
            target = PublicGroupStatus.ACTIVE
        elif action == "pause":
            target = PublicGroupStatus.PAUSED
        elif action == "remove":
            target = PublicGroupStatus.REMOVED
        elif action == "review":
            target = PublicGroupStatus.REVIEW
        else:
            raise HTTPException(status_code=400, detail="invalid action")

        operator = int(sess.get("tg_id") or 0) if isinstance(sess, dict) else 0
        set_group_status(db, group_id=group_id, target_status=target, operator_tg_id=operator, note=note)
        db.commit()
        message = t("admin.public_groups.status_ok").format(status=target.value)
        return RedirectResponse(f"/admin/public-groups?ok={message}", status_code=303)
    except PublicGroupError as exc:  # type: ignore[name-defined]
        db.rollback()
        message = exc.args[0] if exc.args else "failed"
        return RedirectResponse(f"/admin/public-groups?error={message}", status_code=303)
    except Exception:
        db.rollback()
        raise


@router.post("/bulk/status", response_class=JSONResponse)
def public_group_bulk_status(
    payload: BulkStatusRequest,
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    action_map = {
        "approve": PublicGroupStatus.ACTIVE,
        "pause": PublicGroupStatus.PAUSED,
        "remove": PublicGroupStatus.REMOVED,
        "review": PublicGroupStatus.REVIEW,
    }
    target = action_map.get(payload.action.strip().lower())
    if not target:
        raise HTTPException(status_code=400, detail="invalid_action")

    operator = 0
    if isinstance(sess, dict):
        try:
            operator = int(sess.get("tg_id") or 0)
        except (TypeError, ValueError):
            operator = 0
    if operator <= 0:
        raise HTTPException(status_code=403, detail="operator_required")

    note = (payload.note or "").strip() or None

    try:
        summary = bulk_set_group_status(
            db,
            group_ids=payload.group_ids,
            target_status=target,
            operator_tg_id=operator,
            note=note,
        )
        if summary["updated"]:
            db.commit()
        else:
            db.rollback()
        record_audit(
            action="public_group.bulk_status",
            operator=operator,
            payload={
                "target": summary["target"],
                "updated": summary["updated"],
                "errors": summary["errors"],
                "note": note,
            },
        )
        return JSONResponse(
            {
                "target": summary["target"],
                "updated_count": len(summary["updated"]),
                "updated": summary["updated"],
                "errors": summary["errors"],
            }
        )
    except PublicGroupError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=exc.args[0] if exc.args else "invalid") from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/activities/bulk", response_class=JSONResponse)
def public_group_activities_bulk(
    payload: BulkActivityRequest,
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

    try:
        payload_updates = payload.model_dump(exclude={"activity_ids", "action"}, exclude_unset=True)  # type: ignore[attr-defined]
    except AttributeError:
        payload_updates = payload.dict(exclude={"activity_ids", "action"}, exclude_unset=True)  # type: ignore[call-arg]
    updates: Dict[str, object] = {
        key: value for key, value in payload_updates.items() if value is not None or key == "highlight_enabled"
    }

    try:
        summary = bulk_update_activities(
            db,
            activity_ids=payload.activity_ids,
            action=payload.action,
            updates=updates,
            operator_tg_id=operator,
        )
        if summary["updated"]:
            db.commit()
        else:
            db.rollback()

        record_audit(
            action="public_group.activities.bulk",
            operator=operator,
            payload={
                "action": summary["action"],
                "updated": summary["updated"],
                "errors": summary["errors"],
                "updates": updates,
            },
        )
        return JSONResponse(
            {
                "action": summary["action"],
                "updated": summary["updated"],
                "updated_count": len(summary["updated"]),
                "errors": summary["errors"],
            }
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/stats", response_class=JSONResponse)
def public_group_stats(
    req: Request,
    days: int = Query(14, ge=1, le=90),
    top: int = Query(5, ge=1, le=20),
    db=Depends(db_session_ro),
    sess=Depends(require_admin),
):
    summary = fetch_dashboard_metrics(db, days=days, top_n=top)
    return JSONResponse(summary)


@router.get("/activities/insights", response_class=HTMLResponse)
def public_group_activities_insights_page(
    req: Request,
    days: int = Query(7, ge=1, le=90),
    activity_id: Optional[int] = Query(None),
    db=Depends(db_session_ro),
    sess=Depends(require_admin),
):
    start, end = _default_range(days)
    insights = _build_activity_insights(db, start=start, end=end, activity_id=activity_id)
    templates = req.app.state.templates
    activities = svc_list_activities(db)
    return templates.TemplateResponse(
        "public_groups_activity_insights.html",
        {
            "request": req,
            "title": t("admin.public_groups.insights_title"),
            "nav_active": "public_groups",
            "insights_json": json.dumps(insights, ensure_ascii=False),
            "range_start": insights["range"]["start"][:10],
            "range_end": insights["range"]["end"][:10],
            "days": days,
            "activity_id": activity_id,
            "activities": activities,
        },
    )


@router.get("/activities/insights/data", response_class=JSONResponse)
def public_group_activities_insights_data(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=90),
    activity_id: Optional[int] = Query(None),
    db=Depends(db_session_ro),
    sess=Depends(require_admin),
):
    if start and end:
        start_dt = _parse_datetime(start, "start")
        end_dt = _parse_datetime(end, "end")
    else:
        start_dt, end_dt = _default_range(days)
    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start_after_end")
    data = _build_activity_insights(db, start=start_dt, end=end_dt, activity_id=activity_id)
    return JSONResponse(data)


@router.get("/activities/export", response_class=StreamingResponse)
def public_group_activities_export(
    activity_ids: Optional[str] = Query(None),
    db=Depends(db_session_ro),
    sess=Depends(require_admin),
):
    selected_ids: Optional[set[int]] = None
    if activity_ids:
        selected_ids = set()
        for raw in activity_ids.split(","):
            token = raw.strip()
            if not token:
                continue
            try:
                selected_ids.add(int(token))
            except ValueError:
                continue

    activities = svc_list_activities(db)
    if selected_ids:
        activities = [activity for activity in activities if activity.id in selected_ids]

    output = io.StringIO()
    fieldnames = [
        "activity_id",
        "name",
        "status",
        "reward_points",
        "bonus_points",
        "highlight_slots",
        "highlight_enabled",
        "daily_cap",
        "total_cap",
        "start_at",
        "end_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for activity in activities:
        writer.writerow(
            {
                "activity_id": activity.id,
                "name": activity.name,
                "status": activity.status.value if hasattr(activity.status, "value") else activity.status,
                "reward_points": activity.reward_points,
                "bonus_points": activity.bonus_points,
                "highlight_slots": activity.highlight_slots,
                "highlight_enabled": int(bool(activity.is_highlight_enabled)),
                "daily_cap": activity.daily_cap if activity.daily_cap is not None else "",
                "total_cap": activity.total_cap if activity.total_cap is not None else "",
                "start_at": activity.start_at.isoformat() if activity.start_at else "",
                "end_at": activity.end_at.isoformat() if activity.end_at else "",
            }
        )

    csv_data = output.getvalue()
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="public_group_activities_export.csv"',
        },
    )


def _parse_datetime(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{field}_invalid") from exc


def _default_range(days: int) -> tuple[datetime, datetime]:
    end = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=0)
    start = (end - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def _build_activity_insights(
    db: Session,
    *,
    start: datetime,
    end: datetime,
    activity_id: Optional[int] = None,
) -> Dict[str, object]:
    exclusive_end = end + timedelta(seconds=1)
    activity_filter = [int(activity_id)] if activity_id else None

    overview = summarize_conversion_overview(
        db,
        start_date=start,
        end_date=exclusive_end,
        activity_ids=activity_filter,
    )
    overview["webhook_success_rate"] = round(overview.get("webhook_success_rate", 0.0), 4)

    top = summarize_conversions(
        db,
        start_date=start,
        end_date=exclusive_end,
        activity_ids=activity_filter,
        limit=10,
    )
    for item in top:
        item["webhook_success_rate"] = round(item.get("webhook_success_rate", 0.0), 4)

    trend = daily_conversion_trend(
        db,
        start_date=start,
        end_date=exclusive_end,
        activity_ids=activity_filter,
    )
    for item in trend:
        item["webhook_success_rate"] = round(item.get("webhook_success_rate", 0.0), 4)

    alerts = find_conversion_alerts(
        db,
        start_date=start,
        end_date=exclusive_end,
        activity_ids=activity_filter,
    )
    for item in alerts:
        item["webhook_success_rate"] = round(item.get("webhook_success_rate", 0.0), 4)

    return {
        "range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "summary": overview,
        "daily_trend": trend,
        "top_activities": top,
        "alerts": alerts,
    }


@router.get("/activities", response_class=HTMLResponse)
def public_group_activities_page(
    req: Request,
    db=Depends(db_session_ro),
    sess=Depends(require_admin),
):
    activities = svc_list_activities(db)
    templates = req.app.state.templates
    return templates.TemplateResponse(
        "public_groups_activities.html",
        {
            "request": req,
            "title": t("admin.public_groups.activities_title"),
            "nav_active": "public_groups",
            "activities": activities,
            "csrf_token": issue_csrf(req),
        },
    )


@router.post("/activities", response_class=RedirectResponse)
def public_group_activity_create(
    req: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    start_at: str = Form(""),
    end_at: str = Form(""),
    reward_points: int = Form(...),
    bonus_points: int = Form(0),
    highlight_slots: int = Form(0),
    highlight_enabled: str = Form("false"),
    daily_cap: str = Form(""),
    total_cap: str = Form(""),
    description: str = Form(""),
    front_title: str = Form(""),
    front_subtitle: str = Form(""),
    front_cta_label: str = Form(""),
    front_cta_link: str = Form(""),
    front_badge: str = Form(""),
    front_priority: str = Form(""),
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    ensure = csrf_protect
    ensure(req, csrf_token)

    start_dt = _parse_datetime(start_at, "start_at") if start_at else None
    end_dt = _parse_datetime(end_at, "end_at") if end_at else None

    try:
        daily_cap_val = int(daily_cap) if daily_cap.strip() else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="daily_cap_invalid") from exc
    try:
        total_cap_val = int(total_cap) if total_cap.strip() else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="total_cap_invalid") from exc

    highlight_enabled_bool = highlight_enabled.strip().lower() in {"1", "true", "yes", "on"}

    try:
        front_priority_val = int(front_priority.strip()) if front_priority.strip() else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="front_priority_invalid") from exc

    def _clean(value: str) -> Optional[str]:
        cleaned = value.strip()
        return cleaned or None

    front_card: Dict[str, object] = {}
    title_value = _clean(front_title)
    if title_value:
        front_card["title"] = title_value
    subtitle_value = _clean(front_subtitle)
    if subtitle_value:
        front_card["subtitle"] = subtitle_value
    cta_label_value = _clean(front_cta_label)
    if cta_label_value:
        front_card["cta_label"] = cta_label_value
    cta_link_value = _clean(front_cta_link)
    if cta_link_value:
        front_card["cta_link"] = cta_link_value
    badge_value = _clean(front_badge)
    if badge_value:
        front_card["badge"] = badge_value
    if front_priority_val is not None:
        front_card["priority"] = front_priority_val

    try:
        svc_create_activity(
            db,
            name=name,
            activity_type="join_bonus",
            start_at=start_dt,
            end_at=end_dt,
            reward_points=reward_points,
             bonus_points=bonus_points,
             highlight_slots=highlight_slots,
            daily_cap=daily_cap_val,
            total_cap=total_cap_val,
            is_highlight_enabled=highlight_enabled_bool,
            description=description,
            front_card=front_card or None,
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        log.exception("public_group.activities.create_failed name=%s", name)
        raise HTTPException(status_code=400, detail=str(exc))

    return RedirectResponse("/admin/public-groups/activities", status_code=303)


@router.post("/activities/{activity_id}/toggle", response_class=RedirectResponse)
def public_group_activity_toggle(
    req: Request,
    activity_id: int,
    csrf_token: str = Form(...),
    active: str = Form(...),
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    ensure = csrf_protect
    ensure(req, csrf_token)

    is_active = str(active).strip().lower() in {"true", "1", "yes", "on"}

    try:
        svc_toggle_activity(db, activity_id=activity_id, is_active=is_active)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        log.exception("public_group.activities.toggle_failed id=%s", activity_id)
        raise HTTPException(status_code=400, detail=str(exc))

    return RedirectResponse("/admin/public-groups/activities", status_code=303)


def _parse_report_date(value: Optional[str], default: datetime) -> datetime:
    if not value:
        return default
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_date") from exc


@router.get("/activities/report", response_class=HTMLResponse)
def public_group_activity_report(
    req: Request,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    format: Optional[str] = Query(None),
    alerts_only: bool = Query(False),
    db=Depends(db_session_ro),
    sess=Depends(require_admin),
):
    now = datetime.utcnow()
    default_start = now - timedelta(days=7)
    start_dt = _parse_report_date(start, default_start)
    end_dt = _parse_report_date(end, now)
    if end_dt < start_dt:
        raise HTTPException(status_code=400, detail="invalid_range")

    summary = summarize_activity_performance(db, start_date=start_dt, end_date=end_dt)
    conversions = summarize_conversions(db, start_date=start_dt, end_date=end_dt, limit=None)
    overview = summarize_conversion_overview(db, start_date=start_dt, end_date=end_dt)
    alerts = find_conversion_alerts(db, start_date=start_dt, end_date=end_dt)

    conversion_map = {int(item["activity_id"]): item for item in conversions}
    for activity in summary["activities"]:
        converted = conversion_map.get(int(activity["activity_id"]), {})
        activity.setdefault("total_conversions", 0)
        activity.setdefault("webhook_success_rate", 0.0)
        activity.setdefault("webhook_attempts", 0)
        activity.setdefault("webhook_failures", 0)
        activity.setdefault("slack_failures", 0)
        activity.update(
            {
                "total_conversions": converted.get("conversions", activity.get("total_conversions", 0)),
                "webhook_success_rate": converted.get("webhook_success_rate", activity.get("webhook_success_rate", 0.0)),
                "webhook_attempts": converted.get("webhook_attempts", activity.get("webhook_attempts", 0)),
                "webhook_failures": converted.get("webhook_failures", activity.get("webhook_failures", 0)),
                "slack_failures": converted.get("slack_failures", activity.get("slack_failures", 0)),
            }
        )

    if alerts_only:
        summary["activities"] = [
            activity
            for activity in summary["activities"]
            if activity.get("webhook_failures", 0) > 0 or activity.get("slack_failures", 0) > 0
        ]

    if format == "csv":
        fieldnames = [
            "activity_id",
            "name",
            "activity_type",
            "date",
            "grants",
            "points",
            "conversions",
            "webhook_success_rate",
            "webhook_attempts",
            "webhook_failures",
            "slack_failures",
        ]

        def _flatten() -> List[Dict[str, object]]:
            rows: List[Dict[str, object]] = []
            for activity in summary["activities"]:
                daily_entries = activity.get("daily", [])
                if not daily_entries:
                    rows.append(
                        {
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
                    )
                    continue
                for daily in daily_entries:
                    rows.append(
                        {
                            "activity_id": activity["activity_id"],
                            "name": activity["name"],
                            "activity_type": activity["activity_type"],
                            "date": daily.get("date"),
                            "grants": daily.get("grants", 0),
                            "points": daily.get("points", 0),
                            "conversions": daily.get("conversions", 0),
                            "webhook_success_rate": daily.get("webhook_success_rate", 0.0),
                            "webhook_attempts": daily.get("webhook_attempts", 0),
                            "webhook_failures": daily.get("webhook_failures", 0),
                            "slack_failures": daily.get("slack_failures", 0),
                        }
                    )
            return rows

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        for row in _flatten():
            writer.writerow(row)
        buffer.seek(0)
        csv_data = buffer.getvalue()
        return StreamingResponse(
            iter([csv_data]),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="public_group_activity_report.csv"',
            },
        )

    templates = req.app.state.templates
    return templates.TemplateResponse(
        "public_groups_activity_report.html",
        {
            "request": req,
            "title": t("admin.public_groups.activities_report_title"),
            "nav_active": "public_groups",
            "summary": summary,
            "start": start_dt.strftime("%Y-%m-%d"),
            "end": end_dt.strftime("%Y-%m-%d"),
            "conversion_overview": overview,
            "conversion_alerts": alerts,
            "alerts_only": alerts_only,
        },
    )

