from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from models.public_group import PublicGroup, PublicGroupEvent, PublicGroupStatus

ALLOWED_EVENT_TYPES = {"view", "click", "join"}


def _normalize_context(context: Optional[Dict[str, object]]) -> Optional[str]:
    if not context:
        return None
    try:
        return json.dumps(context, ensure_ascii=False)
    except Exception:
        return None


def record_event(
    session: Session,
    *,
    group_id: int,
    event_type: str,
    user_tg_id: Optional[int] = None,
    context: Optional[Dict[str, object]] = None,
) -> PublicGroupEvent:
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"invalid_event_type:{event_type}")
    event = PublicGroupEvent(
        group_id=int(group_id),
        user_tg_id=int(user_tg_id) if user_tg_id is not None else None,
        event_type=event_type,
        context_raw=_normalize_context(context),
    )
    session.add(event)
    return event


def _period_to_timedelta(period: str) -> timedelta:
    bucket = period.strip().lower()
    if bucket in {"1d", "24h"}:
        return timedelta(days=1)
    if bucket in {"7d", "week"}:
        return timedelta(days=7)
    if bucket in {"30d", "month"}:
        return timedelta(days=30)
    if bucket in {"90d", "quarter"}:
        return timedelta(days=90)
    if bucket.endswith("d") and bucket[:-1].isdigit():
        value = int(bucket[:-1])
        if value > 0:
            return timedelta(days=value)
    raise ValueError("invalid_period")


def fetch_conversion_summary(
    session: Session,
    *,
    period: str = "7d",
    limit: int = 10,
) -> Dict[str, object]:
    window = _period_to_timedelta(period)
    utc_now = datetime.utcnow()
    since = utc_now - window

    totals_stmt = (
        select(PublicGroupEvent.event_type, func.count().label("count"))
        .where(PublicGroupEvent.created_at >= since)
        .group_by(PublicGroupEvent.event_type)
    )
    totals_result = session.execute(totals_stmt).all()
    totals = {row.event_type: int(row.count) for row in totals_result}

    event_case = {
        "view": func.sum(case((PublicGroupEvent.event_type == "view", 1), else_=0)),
        "click": func.sum(case((PublicGroupEvent.event_type == "click", 1), else_=0)),
        "join": func.sum(case((PublicGroupEvent.event_type == "join", 1), else_=0)),
    }

    group_stmt = (
        select(
            PublicGroupEvent.group_id,
            func.max(PublicGroup.name).label("name"),
            event_case["view"].label("views"),
            event_case["click"].label("clicks"),
            event_case["join"].label("joins"),
        )
        .join(PublicGroup, PublicGroup.id == PublicGroupEvent.group_id)
        .where(PublicGroupEvent.created_at >= since)
        .group_by(PublicGroupEvent.group_id)
        .order_by(event_case["join"].desc(), event_case["click"].desc(), event_case["view"].desc())
        .limit(max(1, limit))
    )

    group_rows = session.execute(group_stmt).all()
    top_groups: List[Dict[str, object]] = []
    for row in group_rows:
        top_groups.append(
            {
                "group_id": row.group_id,
                "name": row.name,
                "views": int(row.views or 0),
                "clicks": int(row.clicks or 0),
                "joins": int(row.joins or 0),
            }
        )

    views = totals.get("view", 0)
    clicks = totals.get("click", 0)
    joins = totals.get("join", 0)
    click_rate = (clicks / views) if views else 0.0
    join_rate = (joins / views) if views else 0.0
    join_per_click = (joins / clicks) if clicks else 0.0

    return {
        "period": period,
        "from": since.isoformat() + "Z",
        "to": utc_now.isoformat() + "Z",
        "totals": {"view": views, "click": clicks, "join": joins},
        "conversion": {
            "click_rate": round(click_rate, 4),
            "join_rate": round(join_rate, 4),
            "join_per_click": round(join_per_click, 4),
        },
        "top_groups": top_groups,
    }


def fetch_dashboard_metrics(
    session: Session,
    *,
    days: int = 14,
    top_n: int = 5,
) -> Dict[str, object]:
    if days <= 0:
        raise ValueError("invalid_days")
    utc_now = datetime.utcnow()
    since = utc_now - timedelta(days=days)

    event_rows = session.execute(
        select(PublicGroupEvent.event_type, PublicGroupEvent.created_at, PublicGroupEvent.group_id).where(
            PublicGroupEvent.created_at >= since
        )
    ).all()
    timeline: Dict[str, Dict[str, int]] = {}
    for row in event_rows:
        created_at: datetime = row.created_at
        if not created_at:
            continue
        day_key = created_at.date().isoformat()
        per_day = timeline.setdefault(day_key, {"view": 0, "click": 0, "join": 0})
        if row.event_type in ALLOWED_EVENT_TYPES:
            per_day[row.event_type] += 1

    creation_rows = session.execute(
        select(PublicGroup.created_at).where(PublicGroup.created_at >= since)
    ).scalars().all()
    creation_timeline: Dict[str, int] = {}
    for created_at in creation_rows:
        if not created_at:
            continue
        day_key = created_at.date().isoformat()
        creation_timeline[day_key] = creation_timeline.get(day_key, 0) + 1

    status_stmt = (
        select(
            PublicGroup.status,
            func.count().label("count"),
        )
        .group_by(PublicGroup.status)
    )
    status_rows = session.execute(status_stmt).all()
    status_distribution = {row.status.value if isinstance(row.status, PublicGroupStatus) else row.status: int(row.count or 0) for row in status_rows}

    tag_counts: Dict[str, int] = {}
    tag_rows = session.execute(select(PublicGroup.tags_raw)).all()
    for (raw,) in tag_rows:
        if not raw:
            continue
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for tag in data:
                    norm = str(tag).strip().lower()
                    if norm:
                        tag_counts[norm] = tag_counts.get(norm, 0) + 1
        except Exception:
            continue
    top_tags = [
        {"tag": tag, "count": count}
        for tag, count in sorted(tag_counts.items(), key=lambda item: item[1], reverse=True)[: max(1, top_n)]
    ]

    group_stmt = (
        select(
            PublicGroup.id.label("group_id"),
            PublicGroup.name,
            func.sum(case((PublicGroupEvent.event_type == "view", 1), else_=0)).label("views"),
            func.sum(case((PublicGroupEvent.event_type == "click", 1), else_=0)).label("clicks"),
            func.sum(case((PublicGroupEvent.event_type == "join", 1), else_=0)).label("joins"),
        )
        .join(PublicGroup, PublicGroup.id == PublicGroupEvent.group_id)
        .where(PublicGroupEvent.created_at >= since)
        .group_by(PublicGroup.id, PublicGroup.name)
        .order_by(func.sum(case((PublicGroupEvent.event_type == "join", 1), else_=0)).desc())
        .limit(max(1, top_n))
    )
    group_rows = session.execute(group_stmt).all()
    top_groups = [
        {
            "group_id": row.group_id,
            "name": row.name,
            "views": int(row.views or 0),
            "clicks": int(row.clicks or 0),
            "joins": int(row.joins or 0),
        }
        for row in group_rows
    ]

    view_totals = sum(day.get("view", 0) for day in timeline.values())
    click_totals = sum(day.get("click", 0) for day in timeline.values())
    join_totals = sum(day.get("join", 0) for day in timeline.values())
    click_rate = (click_totals / view_totals) if view_totals else 0.0
    join_rate = (join_totals / view_totals) if view_totals else 0.0
    join_per_click = (join_totals / click_totals) if click_totals else 0.0

    return {
        "range": days,
        "from": since.isoformat() + "Z",
        "to": utc_now.isoformat() + "Z",
        "totals": {
            "view": view_totals,
            "click": click_totals,
            "join": join_totals,
            "created": sum(creation_timeline.values()),
        },
        "conversion": {
            "click_rate": round(click_rate, 4),
            "join_rate": round(join_rate, 4),
            "join_per_click": round(join_per_click, 4),
        },
        "timeline": timeline,
        "creation_timeline": creation_timeline,
        "status_breakdown": status_distribution,
        "top_tags": top_tags,
        "top_groups": top_groups,
    }


