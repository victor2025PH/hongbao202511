from __future__ import annotations

import json
import logging
import os
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import httpx
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.public_group import (
    PublicGroup,
    PublicGroupActivity,
    PublicGroupActivityLog,
    PublicGroupActivityStatus,
    PublicGroupActivityWebhook,
    PublicGroupActivityConversionLog,
)
from monitoring.metrics import counter as metrics_counter

log = logging.getLogger("public_group.activity")

_WEBHOOK_TIMEOUT = float(os.getenv("ACTIVITY_WEBHOOK_TIMEOUT", "5"))
_SLACK_WEBHOOK_URL = os.getenv("ACTIVITY_SLACK_WEBHOOK") or os.getenv("REPORT_SLACK_WEBHOOK")

_ACTIVITY_CONVERSION_COUNTER = metrics_counter(
    "activity_conversion_total",
    "Count of public group activity conversions.",
    label_names=("activity_id", "status"),
)
_ACTIVITY_CONVERSION_POINTS = metrics_counter(
    "activity_conversion_points",
    "Total points granted via public group activities.",
    label_names=("activity_id",),
)

JOIN_BONUS_TYPE = "join_bonus"
_DEFAULT_FRONT_PRIORITY = 100


def _clean_text(value: Optional[object]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return str(value).strip() or None


def _coerce_priority(value: Optional[object]) -> int:
    try:
        priority = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        priority = _DEFAULT_FRONT_PRIORITY
    return max(priority, 0)


def _build_front_card(
    activity: PublicGroupActivity,
    *,
    time_left_seconds: Optional[int],
    daily_remaining: Optional[int],
    total_remaining: Optional[int],
) -> Dict[str, object]:
    config = activity.config or {}
    raw = config.get("front_card") if isinstance(config, dict) else None
    front_config: Dict[str, object] = raw if isinstance(raw, dict) else {}

    title = _clean_text(front_config.get("title")) or activity.name
    subtitle = _clean_text(front_config.get("subtitle")) or activity.description or None
    cta_label = _clean_text(front_config.get("cta_label"))
    cta_link = _clean_text(front_config.get("cta_link"))
    badge = _clean_text(front_config.get("badge"))
    priority = _coerce_priority(front_config.get("priority"))

    if not badge and activity.is_highlight_enabled:
        badge = f"Highlight ×{activity.highlight_slots or 1}"

    base_points = int(activity.reward_points or 0)
    bonus_points = int(activity.bonus_points or 0)
    total_points = base_points + bonus_points

    metrics_parts: List[str] = []
    if total_points:
        if bonus_points:
            metrics_parts.append(f"+{total_points} pts (base {base_points} + bonus {bonus_points})")
        else:
            metrics_parts.append(f"+{base_points} pts")
    if activity.is_highlight_enabled:
        metrics_parts.append(
            f"highlight ×{activity.highlight_slots}" if activity.highlight_slots else "highlight active"
        )
    if daily_remaining is not None:
        metrics_parts.append(f"daily {daily_remaining} left")
    if total_remaining is not None:
        metrics_parts.append(f"total {total_remaining} left")

    countdown_text: Optional[str] = None
    if time_left_seconds is not None:
        hours_left = max(int(time_left_seconds // 3600), 0)
        if hours_left > 0:
            countdown_text = f"{hours_left}h left"
        else:
            countdown_text = "Ends soon"

    if countdown_text:
        metrics_parts.append(countdown_text)

    metrics_line = " · ".join(metrics_parts) if metrics_parts else None
    subtitle = subtitle or metrics_line

    front_card: Dict[str, object] = {
        "title": title,
        "subtitle": subtitle,
        "cta_label": cta_label,
        "cta_link": cta_link,
        "badge": badge,
        "priority": priority,
        "countdown_seconds": time_left_seconds,
        "countdown_text": countdown_text,
        "metrics": metrics_line,
    }

    # Remove None values except countdown_seconds which may legitimately be None
    for key in ("subtitle", "cta_label", "cta_link", "badge", "countdown_text", "metrics"):
        if front_card.get(key) is None:
            front_card.pop(key, None)

    return front_card


def list_activities(session: Session) -> List[PublicGroupActivity]:
    stmt = select(PublicGroupActivity).order_by(
        PublicGroupActivity.status.desc(),
        PublicGroupActivity.start_at.asc(),
        PublicGroupActivity.id.desc(),
    )
    return list(session.execute(stmt).scalars().all())


def create_activity(
    session: Session,
    *,
    name: str,
    activity_type: str = JOIN_BONUS_TYPE,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    reward_points: int = 0,
    bonus_points: int = 0,
    highlight_slots: int = 0,
    daily_cap: Optional[int] = None,
    total_cap: Optional[int] = None,
    is_highlight_enabled: bool = False,
    description: Optional[str] = None,
    config: Optional[Dict[str, object]] = None,
    front_card: Optional[Dict[str, object]] = None,
) -> PublicGroupActivity:
    if reward_points < 0 or bonus_points < 0:
        raise ValueError("reward_points_invalid")
    if daily_cap is not None and daily_cap < 0:
        raise ValueError("daily_cap_invalid")
    if total_cap is not None and total_cap < 0:
        raise ValueError("total_cap_invalid")
    if start_at and end_at and end_at <= start_at:
        raise ValueError("time_range_invalid")

    activity = PublicGroupActivity(
        name=name.strip(),
        activity_type=activity_type.strip().lower(),
        description=(description or "").strip() or None,
        start_at=start_at,
        end_at=end_at,
        reward_points=max(0, reward_points),
        bonus_points=max(0, bonus_points),
        highlight_slots=max(0, highlight_slots),
        daily_cap=daily_cap,
        total_cap=total_cap,
        is_highlight_enabled=bool(is_highlight_enabled),
        status=PublicGroupActivityStatus.ACTIVE,
    )
    base_config: Dict[str, object] = dict(config) if config else {}
    if front_card:
        base_config["front_card"] = dict(front_card)
    activity.config = base_config
    session.add(activity)
    session.flush()
    log.info("public_group.activity.create id=%s name=%s type=%s", activity.id, name, activity_type)
    return activity


def toggle_activity(
    session: Session,
    *,
    activity_id: int,
    is_active: bool,
) -> PublicGroupActivity:
    activity = session.get(PublicGroupActivity, int(activity_id))
    if not activity:
        raise ValueError("activity_not_found")
    activity.status = PublicGroupActivityStatus.ACTIVE if is_active else PublicGroupActivityStatus.PAUSED
    activity.touch()
    session.add(activity)
    log.info("public_group.activity.toggle id=%s active=%s", activity_id, is_active)
    return activity


def bulk_update_activities(
    session: Session,
    *,
    activity_ids: Sequence[int],
    action: str,
    updates: Optional[Dict[str, object]] = None,
    operator_tg_id: Optional[int] = None,
) -> Dict[str, object]:
    if not activity_ids:
        return {"action": action, "updated": [], "errors": []}

    action_clean = (action or "").strip().lower()
    dedup_ids: List[int] = []
    seen: set[int] = set()
    for raw_id in activity_ids:
        try:
            aid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if aid <= 0 or aid in seen:
            continue
        dedup_ids.append(aid)
        seen.add(aid)

    summary = {"action": action_clean, "updated": [], "errors": []}
    status_map = {
        "pause": PublicGroupActivityStatus.PAUSED,
        "resume": PublicGroupActivityStatus.ACTIVE,
    }

    for activity_id in dedup_ids:
        activity = session.get(PublicGroupActivity, int(activity_id))
        if not activity:
            summary["errors"].append({"activity_id": activity_id, "error": "activity_not_found"})
            continue

        try:
            if action_clean in status_map:
                activity.status = status_map[action_clean]
                activity.touch()
                session.add(activity)
            elif action_clean == "update":
                payload = updates or {}

                def _maybe_int(key: str, allow_none: bool = False) -> Optional[int]:
                    if key not in payload:
                        return None
                    value = payload.get(key)
                    if value is None and allow_none:
                        return None
                    try:
                        return int(value)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{key}_invalid") from exc

                reward_points = _maybe_int("reward_points")
                bonus_points = _maybe_int("bonus_points")
                highlight_slots = _maybe_int("highlight_slots")
                daily_cap = _maybe_int("daily_cap", allow_none=True)
                total_cap = _maybe_int("total_cap", allow_none=True)

                if reward_points is not None:
                    if reward_points < 0:
                        raise ValueError("reward_points_invalid")
                    activity.reward_points = reward_points
                if bonus_points is not None:
                    if bonus_points < 0:
                        raise ValueError("bonus_points_invalid")
                    activity.bonus_points = bonus_points
                if highlight_slots is not None:
                    if highlight_slots < 0:
                        raise ValueError("highlight_slots_invalid")
                    activity.highlight_slots = highlight_slots
                if daily_cap is not None:
                    if daily_cap < 0:
                        raise ValueError("daily_cap_invalid")
                    activity.daily_cap = daily_cap
                if total_cap is not None:
                    if total_cap < 0:
                        raise ValueError("total_cap_invalid")
                    activity.total_cap = total_cap
                if "highlight_enabled" in payload:
                    activity.is_highlight_enabled = bool(payload.get("highlight_enabled"))

                activity.touch()
                session.add(activity)
            else:
                raise ValueError("action_invalid")

            summary["updated"].append(activity_id)
            log.info(
                "public_group.activity.bulk_update id=%s action=%s operator=%s",
                activity_id,
                action_clean,
                operator_tg_id,
            )
        except Exception as exc:
            summary["errors"].append(
                {"activity_id": activity_id, "error": getattr(exc, "args", ["failed"])[0]}
            )
    return summary

def _active_join_activities(session: Session, now: Optional[datetime] = None) -> Sequence[PublicGroupActivity]:
    now = now or datetime.utcnow()
    stmt = select(PublicGroupActivity).where(
        PublicGroupActivity.activity_type == JOIN_BONUS_TYPE,
        PublicGroupActivity.status == PublicGroupActivityStatus.ACTIVE,
        or_(PublicGroupActivity.start_at.is_(None), PublicGroupActivity.start_at <= now),
        or_(PublicGroupActivity.end_at.is_(None), PublicGroupActivity.end_at >= now),
    )
    return session.execute(stmt).scalars().all()


def _today_key(now: Optional[datetime] = None) -> str:
    now = now or datetime.utcnow()
    return now.date().isoformat()


def _count_logs(session: Session, *, activity_id: int, date_key: Optional[str] = None) -> int:
    stmt = select(func.count()).select_from(PublicGroupActivityLog).where(
        PublicGroupActivityLog.activity_id == activity_id
    )
    if date_key:
        stmt = stmt.where(PublicGroupActivityLog.date_key == date_key)
    return int(session.execute(stmt).scalar_one() or 0)


def apply_join_bonus(
    session: Session,
    *,
    group_id: int,
    user_tg_id: int,
    now: Optional[datetime] = None,
) -> Tuple[int, List[Dict[str, object]]]:
    now = now or datetime.utcnow()
    today_key = _today_key(now)
    total_bonus = 0
    bonuses: List[Dict[str, object]] = []

    activities = _active_join_activities(session, now)
    if not activities:
        return total_bonus, bonuses

    for activity in activities:
        try:
            if activity.reward_points <= 0 and activity.bonus_points <= 0:
                continue
            if activity.total_cap is not None:
                used_total = _count_logs(session, activity_id=activity.id)
                if used_total >= activity.total_cap:
                    continue
            if activity.daily_cap is not None:
                used_daily = _count_logs(session, activity_id=activity.id, date_key=today_key)
                if used_daily >= activity.daily_cap:
                    continue

            existing = session.execute(
                select(PublicGroupActivityLog).where(
                    PublicGroupActivityLog.activity_id == activity.id,
                    PublicGroupActivityLog.user_tg_id == int(user_tg_id),
                )
            ).scalar_one_or_none()
            if existing:
                continue

            total_points = activity.reward_points + activity.bonus_points
            log_entry = PublicGroupActivityLog(
                activity_id=activity.id,
                group_id=int(group_id),
                user_tg_id=int(user_tg_id),
                event_type=JOIN_BONUS_TYPE,
                points=total_points,
                date_key=today_key,
            )
            session.add(log_entry)
            session.flush()

            bonuses.append(
                {
                    "activity_id": activity.id,
                    "name": activity.name,
                    "points": total_points,
                }
            )
            total_bonus += total_points
        except IntegrityError:
            session.rollback()
            log.warning("public_group.activity.bonus duplicate activity=%s user=%s", activity.id, user_tg_id)
        except Exception:
            session.rollback()
            log.exception(
                "public_group.activity.bonus unexpected activity=%s user=%s", activity.id, user_tg_id
            )

    return total_bonus, bonuses


def _remaining_cap_for_activity(
    session: Session,
    *,
    activity: PublicGroupActivity,
    user_tg_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Tuple[Optional[int], Optional[int], bool]:
    today_key = _today_key(now)
    daily_remaining: Optional[int] = None
    total_remaining: Optional[int] = None
    participated = False

    if activity.daily_cap is not None:
        used_daily = _count_logs(session, activity_id=activity.id, date_key=today_key)
        daily_remaining = max(activity.daily_cap - used_daily, 0)

    if activity.total_cap is not None:
        used_total = _count_logs(session, activity_id=activity.id)
        total_remaining = max(activity.total_cap - used_total, 0)

    if user_tg_id is not None:
        existing = session.execute(
            select(PublicGroupActivityLog.id).where(
                PublicGroupActivityLog.activity_id == activity.id,
                PublicGroupActivityLog.user_tg_id == int(user_tg_id),
            )
        ).first()
        if existing:
            participated = True
            if daily_remaining is not None:
                daily_remaining = 0
            if total_remaining is not None:
                total_remaining = 0

    return daily_remaining, total_remaining, participated


def get_active_campaign_summaries(
    session: Session,
    *,
    user_tg_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, object]]:
    now = now or datetime.utcnow()
    activities = _active_join_activities(session, now)
    summaries: List[Dict[str, object]] = []
    for activity in activities:
        daily_remaining, total_remaining, participated = _remaining_cap_for_activity(
            session,
            activity=activity,
            user_tg_id=user_tg_id,
            now=now,
        )
        time_left_seconds = None
        if activity.end_at:
            time_left_seconds = int((activity.end_at - now).total_seconds())
            if time_left_seconds < 0:
                time_left_seconds = 0
        front_card = _build_front_card(
            activity,
            time_left_seconds=time_left_seconds,
            daily_remaining=daily_remaining,
            total_remaining=total_remaining,
        )
        headline = front_card.get("metrics")
        countdown_text = front_card.get("countdown_text")
        if countdown_text is None and time_left_seconds is not None:
            hours_left = max(int(time_left_seconds // 3600), 0)
            countdown_text = f"{hours_left} hours remaining" if hours_left else "Ends soon"
        highlight_badge = front_card.get("badge")
        if not highlight_badge and activity.is_highlight_enabled:
            highlight_badge = (
                f"Highlight ×{activity.highlight_slots}"
                if activity.highlight_slots
                else "Highlight active"
            )
        summaries.append(
            {
                "id": activity.id,
                "name": activity.name,
                "description": activity.description,
                "activity_type": activity.activity_type,
                "start_at": activity.start_at.isoformat() if activity.start_at else None,
                "end_at": activity.end_at.isoformat() if activity.end_at else None,
                "reward_points": activity.reward_points,
                "bonus_points": activity.bonus_points,
                "highlight_slots": activity.highlight_slots,
                "highlight_enabled": activity.is_highlight_enabled,
                "daily_cap": activity.daily_cap,
                "total_cap": activity.total_cap,
                "remaining_daily": daily_remaining,
                "remaining_total": total_remaining,
                "status": activity.status.value,
                "time_left_seconds": time_left_seconds,
                "config": activity.config,
                "front_card": front_card,
                "headline": headline,
                "countdown_text": countdown_text,
                "highlight_badge": highlight_badge,
                "front_priority": front_card.get("priority", _DEFAULT_FRONT_PRIORITY),
                "has_participated": participated,
            }
        )
    summaries.sort(
        key=lambda item: (
            int(item.get("front_priority") or _DEFAULT_FRONT_PRIORITY),
            item.get("end_at") or "",
            item["id"],
        )
    )
    return summaries


def get_active_campaign_detail(
    session: Session,
    *,
    activity_id: int,
    user_tg_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, object]]:
    now = now or datetime.utcnow()
    activity = session.get(PublicGroupActivity, int(activity_id))
    if not activity:
        return None
    if activity.activity_type != JOIN_BONUS_TYPE:
        return None

    in_time_window = True
    if activity.start_at and activity.start_at > now:
        in_time_window = False
    if activity.end_at and activity.end_at < now:
        in_time_window = False

    daily_remaining, total_remaining, participated = _remaining_cap_for_activity(
        session,
        activity=activity,
        user_tg_id=user_tg_id,
        now=now,
    )
    time_left_seconds = None
    if activity.end_at:
        time_left_seconds = int((activity.end_at - now).total_seconds())
        if time_left_seconds < 0:
            time_left_seconds = 0
    front_card = _build_front_card(
        activity,
        time_left_seconds=time_left_seconds,
        daily_remaining=daily_remaining,
        total_remaining=total_remaining,
    )
    headline = front_card.get("metrics")
    countdown_text = front_card.get("countdown_text")
    if countdown_text is None and time_left_seconds is not None:
        hours_left = max(int(time_left_seconds // 3600), 0)
        countdown_text = f"{hours_left} hours remaining" if hours_left else "Ends soon"
    highlight_badge = front_card.get("badge")
    if not highlight_badge and activity.is_highlight_enabled:
        highlight_badge = (
            f"Highlight ×{activity.highlight_slots}"
            if activity.highlight_slots
            else "Highlight active"
        )

    base_points = int(activity.reward_points or 0)
    bonus_points = int(activity.bonus_points or 0)
    total_points = base_points + bonus_points

    eligible = (
        activity.status == PublicGroupActivityStatus.ACTIVE
        and in_time_window
        and not participated
        and (daily_remaining is None or daily_remaining > 0)
        and (total_remaining is None or total_remaining > 0)
    )

    rules: List[Dict[str, object]] = [
        {
            "key": "reward_points",
            "label": "reward_points",
            "value": base_points,
        },
    ]
    if bonus_points:
        rules.append(
            {
                "key": "bonus_points",
                "label": "bonus_points",
                "value": bonus_points,
            }
        )
    if activity.daily_cap is not None:
        rules.append(
            {
                "key": "daily_cap",
                "label": "daily_cap",
                "value": activity.daily_cap,
                "remaining": daily_remaining,
            }
        )
    if activity.total_cap is not None:
        rules.append(
            {
                "key": "total_cap",
                "label": "total_cap",
                "value": activity.total_cap,
                "remaining": total_remaining,
            }
        )

    return {
        "id": activity.id,
        "name": activity.name,
        "description": activity.description,
        "activity_type": activity.activity_type,
        "start_at": activity.start_at.isoformat() if activity.start_at else None,
        "end_at": activity.end_at.isoformat() if activity.end_at else None,
        "reward_points": base_points,
        "bonus_points": bonus_points,
        "total_points": total_points,
        "highlight_slots": activity.highlight_slots,
        "highlight_enabled": activity.is_highlight_enabled,
        "daily_cap": activity.daily_cap,
        "total_cap": activity.total_cap,
        "remaining_daily": daily_remaining,
        "remaining_total": total_remaining,
        "status": activity.status.value,
        "time_left_seconds": time_left_seconds,
        "config": activity.config,
        "front_card": front_card,
        "headline": headline,
        "countdown_text": countdown_text,
        "highlight_badge": highlight_badge,
        "front_priority": front_card.get("priority", _DEFAULT_FRONT_PRIORITY),
        "has_participated": participated,
        "eligible": eligible,
        "in_time_window": in_time_window,
        "rules": rules,
    }


def summarize_activity_performance(
    session: Session,
    *,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> Dict[str, object]:
    start = start_date or datetime.utcnow() - timedelta(days=7)
    end = end_date or datetime.utcnow()

    stmt = (
        select(
            PublicGroupActivity.id.label("activity_id"),
            PublicGroupActivity.name,
            PublicGroupActivity.activity_type,
            PublicGroupActivityLog.date_key,
            func.count().label("grants"),
            func.sum(PublicGroupActivityLog.points).label("points"),
        )
        .join(PublicGroupActivity, PublicGroupActivity.id == PublicGroupActivityLog.activity_id)
        .where(
            PublicGroupActivityLog.created_at >= start,
            PublicGroupActivityLog.created_at <= end,
        )
        .group_by(
            PublicGroupActivity.id,
            PublicGroupActivity.name,
            PublicGroupActivity.activity_type,
            PublicGroupActivityLog.date_key,
        )
        .order_by(PublicGroupActivityLog.date_key.asc())
    )

    rows = session.execute(stmt).all()
    activities: Dict[int, Dict[str, object]] = {}
    daily_lookup: Dict[int, Dict[str, Dict[str, object]]] = {}
    for row in rows:
        activity = activities.setdefault(
            row.activity_id,
            {
                "activity_id": row.activity_id,
                "name": row.name,
                "activity_type": row.activity_type,
                "total_grants": 0,
                "total_points": 0,
                "total_conversions": 0,
                "total_conversion_points": 0,
                "daily": [],
            },
        )
        grants = int(row.grants or 0)
        points = int(row.points or 0)
        entry = {
            "date": str(row.date_key),
            "grants": grants,
            "points": points,
            "conversions": 0,
            "conversion_points": 0,
            "webhook_attempts": 0,
            "webhook_successes": 0,
            "webhook_failures": 0,
            "webhook_success_rate": 0.0,
            "slack_failures": 0,
        }
        activity["daily"].append(entry)
        daily_lookup.setdefault(row.activity_id, {})[entry["date"]] = entry
        activity["total_grants"] += grants
        activity["total_points"] += points

    conversion_stmt = (
        select(
            PublicGroupActivityConversionLog.activity_id,
            PublicGroupActivity.name,
            PublicGroupActivity.activity_type,
            func.date(PublicGroupActivityConversionLog.created_at).label("date_key"),
            func.count().label("conversions"),
            func.sum(PublicGroupActivityConversionLog.points).label("conversion_points"),
            func.sum(PublicGroupActivityConversionLog.webhook_attempts).label("webhook_attempts"),
            func.sum(PublicGroupActivityConversionLog.webhook_successes).label("webhook_successes"),
            func.sum(
                case((PublicGroupActivityConversionLog.slack_status == "failed", 1), else_=0)
            ).label("slack_failures"),
        )
        .join(
            PublicGroupActivity,
            PublicGroupActivity.id == PublicGroupActivityConversionLog.activity_id,
        )
        .where(
            PublicGroupActivityConversionLog.created_at >= start,
            PublicGroupActivityConversionLog.created_at <= end,
        )
        .group_by(
            PublicGroupActivityConversionLog.activity_id,
            PublicGroupActivity.name,
            PublicGroupActivity.activity_type,
            func.date(PublicGroupActivityConversionLog.created_at),
        )
    )

    for row in session.execute(conversion_stmt):
        activity = activities.setdefault(
            row.activity_id,
            {
                "activity_id": row.activity_id,
                "name": row.name,
                "activity_type": row.activity_type,
                "total_grants": 0,
                "total_points": 0,
                "total_conversions": 0,
                "total_conversion_points": 0,
                "daily": [],
            },
        )
        date_key = str(row.date_key)
        entry = daily_lookup.setdefault(row.activity_id, {}).get(date_key)
        if entry is None:
            entry = {
                "date": date_key,
                "grants": 0,
                "points": 0,
                "conversions": 0,
                "conversion_points": 0,
                "webhook_attempts": 0,
                "webhook_successes": 0,
                "webhook_failures": 0,
                "webhook_success_rate": 0.0,
                "slack_failures": 0,
            }
            activity["daily"].append(entry)
            daily_lookup.setdefault(row.activity_id, {})[date_key] = entry

        conversions = int(row.conversions or 0)
        conversion_points = int(row.conversion_points or 0)
        webhook_attempts = int(row.webhook_attempts or 0)
        webhook_successes = int(row.webhook_successes or 0)
        slack_failures = int(row.slack_failures or 0)
        webhook_failures = max(webhook_attempts - webhook_successes, 0)

        entry["conversions"] = conversions
        entry["conversion_points"] = conversion_points
        entry["webhook_attempts"] = webhook_attempts
        entry["webhook_successes"] = webhook_successes
        entry["webhook_failures"] = webhook_failures
        entry["webhook_success_rate"] = (webhook_successes / webhook_attempts) if webhook_attempts else 0.0
        entry["slack_failures"] = slack_failures

        activity["total_conversions"] = activity.get("total_conversions", 0) + conversions
        activity["total_conversion_points"] = activity.get("total_conversion_points", 0) + conversion_points

    for activity in activities.values():
        activity["daily"].sort(key=lambda item: item["date"])

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "activities": list(activities.values()),
    }


def list_active_webhooks(session: Session, *, activity_id: Optional[int] = None) -> List[PublicGroupActivityWebhook]:
    stmt = select(PublicGroupActivityWebhook).where(PublicGroupActivityWebhook.is_active.is_(True))
    if activity_id is not None:
        stmt = stmt.where(PublicGroupActivityWebhook.activity_id == int(activity_id))
    return list(session.execute(stmt).scalars().all())


def create_or_update_webhook(
    session: Session,
    *,
    activity_id: int,
    url: str,
    secret: Optional[str] = None,
    is_active: bool = True,
) -> Tuple[PublicGroupActivityWebhook, bool]:
    webhook = session.execute(
        select(PublicGroupActivityWebhook)
        .where(
            PublicGroupActivityWebhook.activity_id == int(activity_id),
            PublicGroupActivityWebhook.url == url.strip(),
        )
    ).scalar_one_or_none()
    if webhook:
        webhook.secret = secret or webhook.secret
        webhook.is_active = is_active
        webhook.touch()
        session.add(webhook)
        session.flush()
        return webhook, False
    webhook = PublicGroupActivityWebhook(
        activity_id=int(activity_id),
        url=url.strip(),
        secret=secret.strip() if secret else None,
        is_active=is_active,
    )
    session.add(webhook)
    session.flush()
    return webhook, True


def deactivate_webhook(
    session: Session,
    *,
    webhook_id: int,
) -> bool:
    stmt = (
        update(PublicGroupActivityWebhook)
        .where(PublicGroupActivityWebhook.id == int(webhook_id))
        .values(is_active=False, updated_at=datetime.utcnow())
    )
    result = session.execute(stmt)
    return result.rowcount > 0


def drop_webhook(
    session: Session,
    *,
    webhook_id: int,
) -> bool:
    webhook = session.get(PublicGroupActivityWebhook, int(webhook_id))
    if not webhook:
        return False
    session.delete(webhook)
    session.flush()
    return True


def emit_activity_conversion(
    session: Session,
    *,
    activity_id: int,
    group: PublicGroup,
    user_tg_id: int,
    points: int,
    event: str = "join_bonus",
    metadata: Optional[Dict[str, object]] = None,
) -> None:
    if points <= 0:
        return
    activity = session.get(PublicGroupActivity, int(activity_id))
    if not activity:
        return

    payload: Dict[str, object] = {
        "event": event,
        "activity": {
            "id": activity.id,
            "name": activity.name,
            "type": activity.activity_type,
        },
        "group": {
            "id": group.id,
            "name": group.name,
            "invite_link": group.invite_link,
        },
        "user": {"tg_id": int(user_tg_id)},
        "points": int(points),
        "triggered_at": datetime.utcnow().isoformat() + "Z",
    }
    if metadata:
        payload["metadata"] = metadata

    webhook_result = _dispatch_activity_webhooks(session, activity, payload)
    slack_status = _notify_slack_conversion(payload)

    webhook_status = webhook_result.get("status", "skipped")
    webhook_attempts = int(webhook_result.get("attempts", 0) or 0)
    webhook_success = int(webhook_result.get("success", 0) or 0)

    # overall status: success if both webhook/slack succeed or skipped; partial for mixed; failed otherwise
    components = []
    if webhook_status not in {"skipped", "success"}:
        components.append(webhook_status)
    if slack_status not in {"skipped", "success"}:
        components.append(slack_status)
    if not components:
        overall_status = "success"
    elif any(status == "failed" for status in components) and webhook_success == 0:
        overall_status = "failed"
    else:
        overall_status = "partial"

    activity_label = str(activity.id)
    _ACTIVITY_CONVERSION_COUNTER.inc(activity_id=activity_label, status=overall_status)
    _ACTIVITY_CONVERSION_POINTS.inc(points, activity_id=activity_label)

    log_entry = PublicGroupActivityConversionLog(
        activity_id=activity.id,
        group_id=group.id,
        user_tg_id=int(user_tg_id),
        points=int(points),
        event_type=event,
        webhook_status=webhook_status,
        webhook_attempts=webhook_attempts,
        webhook_successes=webhook_success,
        slack_status=slack_status,
    )
    log_entry.context = metadata or {}
    session.add(log_entry)


def _dispatch_activity_webhooks(
    session: Session,
    activity: PublicGroupActivity,
    payload: Dict[str, object],
) -> Dict[str, object]:
    webhooks = list_active_webhooks(session, activity_id=activity.id)
    if not webhooks:
        return {"status": "skipped", "attempts": 0, "success": 0}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    attempts = 0
    successes = 0
    for webhook in webhooks:
        attempts += 1
        headers = {"Content-Type": "application/json"}
        if webhook.secret:
            headers["X-Activity-Signature"] = hmac.new(
                webhook.secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
        try:
            resp = httpx.post(
                webhook.url,
                content=body,
                headers=headers,
                timeout=_WEBHOOK_TIMEOUT,
            )
            if 200 <= resp.status_code < 400:
                successes += 1
                continue
            log.warning(
                "public_group.activity.webhook_non_ok webhook_id=%s url=%s status=%s",
                webhook.id,
                webhook.url,
                resp.status_code,
            )
        except Exception:
            log.warning(
                "public_group.activity.webhook_failed webhook_id=%s url=%s",
                webhook.id,
                webhook.url,
                exc_info=True,
            )
    if successes == attempts:
        status = "success"
    elif successes > 0:
        status = "partial"
    else:
        status = "failed"
    return {"status": status, "attempts": attempts, "success": successes}


def _notify_slack_conversion(payload: Dict[str, object]) -> str:
    if not _SLACK_WEBHOOK_URL:
        return "skipped"
    group = payload.get("group", {})
    activity = payload.get("activity", {})
    user = payload.get("user", {})
    text = (
        ":tada: Public Group Activity triggered\n"
        f"- Activity: `{activity.get('name')}` (ID {activity.get('id')})\n"
        f"- Group: `{group.get('name')}` (ID {group.get('id')})\n"
        f"- User: {user.get('tg_id')}\n"
        f"- Points: {payload.get('points')}\n"
        f"- Event: {payload.get('event')}\n"
        f"- Time: {payload.get('triggered_at')}"
    )
    try:
        resp = httpx.post(
            _SLACK_WEBHOOK_URL,
            json={"text": text},
            timeout=_WEBHOOK_TIMEOUT,
        )
        if 200 <= resp.status_code < 400:
            return "success"
        log.warning("public_group.activity.slack_notify_non_ok status=%s", resp.status_code)
        return "failed"
    except Exception:
        log.warning("public_group.activity.slack_notify_failed", exc_info=True)
        return "failed"


def _conversion_filters(
    start_date: datetime,
    end_date: datetime,
    activity_ids: Optional[Sequence[int]] = None,
):
    filters = [
        PublicGroupActivityConversionLog.created_at >= start_date,
        PublicGroupActivityConversionLog.created_at < end_date,
    ]
    if activity_ids:
        filters.append(PublicGroupActivityConversionLog.activity_id.in_([int(a) for a in activity_ids]))
    return filters


def summarize_conversion_overview(
    session: Session,
    *,
    start_date: datetime,
    end_date: datetime,
    activity_ids: Optional[Sequence[int]] = None,
) -> Dict[str, object]:
    attempts_sum = func.sum(PublicGroupActivityConversionLog.webhook_attempts)
    success_sum = func.sum(PublicGroupActivityConversionLog.webhook_successes)
    slack_fail_case = case((PublicGroupActivityConversionLog.slack_status == "failed", 1), else_=0)

    stmt = (
        select(
            func.count().label("conversions"),
            func.sum(PublicGroupActivityConversionLog.points).label("points"),
            attempts_sum.label("webhook_attempts"),
            success_sum.label("webhook_successes"),
            func.sum(slack_fail_case).label("slack_failures"),
        )
        .where(*_conversion_filters(start_date, end_date, activity_ids))
    )
    row = session.execute(stmt).one()
    total = int(row.conversions or 0)
    attempts = int(row.webhook_attempts or 0)
    successes = int(row.webhook_successes or 0)
    slack_failures = int(row.slack_failures or 0)
    webhook_failures = max(attempts - successes, 0)
    return {
        "total_conversions": total,
        "total_points": int(row.points or 0),
        "webhook_attempts": attempts,
        "webhook_successes": successes,
        "webhook_failures": webhook_failures,
        "webhook_success_rate": (successes / attempts) if attempts else 0.0,
        "slack_failures": slack_failures,
    }


def summarize_conversions(
    session: Session,
    *,
    start_date: datetime,
    end_date: datetime,
    activity_ids: Optional[Sequence[int]] = None,
    limit: Optional[int] = 10,
) -> List[Dict[str, object]]:
    attempts_sum = func.sum(PublicGroupActivityConversionLog.webhook_attempts)
    success_sum = func.sum(PublicGroupActivityConversionLog.webhook_successes)
    slack_fail_case = case((PublicGroupActivityConversionLog.slack_status == "failed", 1), else_=0)
    stmt = (
        select(
            PublicGroupActivityConversionLog.activity_id,
            PublicGroupActivity.name,
            func.count().label("conversions"),
            func.sum(PublicGroupActivityConversionLog.points).label("points"),
            attempts_sum.label("webhook_attempts"),
            success_sum.label("webhook_successes"),
            func.sum(slack_fail_case).label("slack_failures"),
        )
        .join(
            PublicGroupActivity,
            PublicGroupActivity.id == PublicGroupActivityConversionLog.activity_id,
            isouter=True,
        )
        .where(*_conversion_filters(start_date, end_date, activity_ids))
        .group_by(
            PublicGroupActivityConversionLog.activity_id,
            PublicGroupActivity.name,
        )
        .order_by(
            func.count().desc(),
            func.sum(PublicGroupActivityConversionLog.points).desc(),
        )
    )
    if limit and limit > 0:
        stmt = stmt.limit(limit)

    result: List[Dict[str, object]] = []
    for row in session.execute(stmt):
        conversions = int(row.conversions or 0)
        webhook_attempts = int(row.webhook_attempts or 0)
        webhook_successes = int(row.webhook_successes or 0)
        slack_failures = int(row.slack_failures or 0)
        webhook_failures = max(webhook_attempts - webhook_successes, 0)
        result.append(
            {
                "activity_id": int(row.activity_id),
                "name": row.name or "",
                "conversions": conversions,
                "points": int(row.points or 0),
                "webhook_attempts": webhook_attempts,
                "webhook_successes": webhook_successes,
                "webhook_failures": webhook_failures,
                "webhook_success_rate": (webhook_successes / webhook_attempts) if webhook_attempts else 0.0,
                "slack_failures": slack_failures,
            }
        )
    return result


def daily_conversion_trend(
    session: Session,
    *,
    start_date: datetime,
    end_date: datetime,
    activity_ids: Optional[Sequence[int]] = None,
) -> List[Dict[str, object]]:
    date_col = func.date(PublicGroupActivityConversionLog.created_at)
    attempts_sum = func.sum(PublicGroupActivityConversionLog.webhook_attempts)
    success_sum = func.sum(PublicGroupActivityConversionLog.webhook_successes)
    stmt = (
        select(
            date_col.label("date"),
            func.count().label("conversions"),
            func.sum(PublicGroupActivityConversionLog.points).label("points"),
            attempts_sum.label("webhook_attempts"),
            success_sum.label("webhook_successes"),
        )
        .where(*_conversion_filters(start_date, end_date, activity_ids))
        .group_by(date_col)
        .order_by(date_col)
    )
    trend: List[Dict[str, object]] = []
    for row in session.execute(stmt):
        conversions = int(row.conversions or 0)
        webhook_attempts = int(row.webhook_attempts or 0)
        webhook_successes = int(row.webhook_successes or 0)
        trend.append(
            {
                "date": row.date.isoformat() if hasattr(row.date, "isoformat") else str(row.date),
                "conversions": conversions,
                "points": int(row.points or 0),
                "webhook_attempts": webhook_attempts,
                "webhook_successes": webhook_successes,
                "webhook_failures": max(webhook_attempts - webhook_successes, 0),
                "webhook_success_rate": (webhook_successes / webhook_attempts) if webhook_attempts else 0.0,
            }
        )
    return trend


def find_conversion_alerts(
    session: Session,
    *,
    start_date: datetime,
    end_date: datetime,
    activity_ids: Optional[Sequence[int]] = None,
    webhook_success_threshold: float = 0.9,
    slack_failure_threshold: int = 1,
) -> List[Dict[str, object]]:
    aggregates = summarize_conversions(
        session,
        start_date=start_date,
        end_date=end_date,
        activity_ids=activity_ids,
        limit=None,
    )
    alerts: List[Dict[str, object]] = []
    for agg in aggregates:
        conversions = agg["conversions"]
        if conversions == 0:
            continue
        success_rate = agg["webhook_success_rate"]
        slack_failures = agg["slack_failures"]
        if success_rate < webhook_success_threshold or slack_failures >= slack_failure_threshold:
            alerts.append(
                {
                    "activity_id": agg["activity_id"],
                    "name": agg["name"],
                    "conversions": conversions,
                    "points": agg["points"],
                    "webhook_attempts": agg.get("webhook_attempts", 0),
                    "webhook_failures": agg.get("webhook_failures", 0),
                    "webhook_success_rate": success_rate,
                    "slack_failures": slack_failures,
                }
            )
    return alerts

