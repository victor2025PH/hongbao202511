"""
Public group service layer:
- 提供创建、加入、置顶、列表与风控评估
- 所有函数接收 Session（同步调用），未自动提交，由调用方掌控事务
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import perf_counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from monitoring.metrics import counter as metrics_counter, histogram as metrics_histogram
from models.public_group import (
    PublicGroup,
    PublicGroupMember,
    PublicGroupRewardClaim,
    PublicGroupStatus,
    PublicGroupBookmark,
)
from models.user import User, get_or_create_user
from services.public_group_activity import apply_join_bonus, emit_activity_conversion


# --------- 常量配置（后续可提取至配置中心） ---------
MAX_TAGS = 5
MAX_NAME_LENGTH = 80
MAX_DESC_LENGTH = 400
PIN_LIMIT = 3
DEFAULT_PIN_DURATION_HOURS = 24
ENTRY_REWARD_DEFAULT_POINTS = 5
ENTRY_REWARD_DEFAULT_POOL = 1000
ENTRY_REWARD_MAX_POINTS = 50

BANNED_KEYWORDS = {
    "casino",
    "赌",
    "賭",
    "博彩",
    "airdrop",
    "profit",
    "roi",
    "cashback",
}

RISK_SCORE_THRESHOLD_REVIEW = 5
RECENT_CREATE_MINUTES = 10
RECENT_CREATE_LIMIT = 3


class PublicGroupError(RuntimeError):
    """业务校验失败时抛出。"""


log = logging.getLogger("public_group.service")

_PG_OPERATION_COUNTER = metrics_counter(
    "public_group_operation_total",
    "Count of public group service operations.",
    label_names=("operation", "status"),
)
_PG_OPERATION_LATENCY = metrics_histogram(
    "public_group_operation_seconds",
    "Duration of public group service operations (seconds).",
    label_names=("operation", "status"),
)


@dataclass
class RiskResult:
    score: int
    flags: List[str]

    @property
    def requires_review(self) -> bool:
        return self.score >= RISK_SCORE_THRESHOLD_REVIEW


def _normalize_tags(tags: Optional[Iterable[str]]) -> List[str]:
    if not tags:
        return []
    cleaned: List[str] = []
    for raw in tags:
        if not raw:
            continue
        tag = str(raw).strip().lower()
        if not tag:
            continue
        if len(tag) > 24:
            tag = tag[:24]
        if tag not in cleaned:
            cleaned.append(tag)
        if len(cleaned) >= MAX_TAGS:
            break
    return cleaned


def _normalize_language(lang: Optional[str]) -> Optional[str]:
    if not lang:
        return None
    val = str(lang).strip().lower()
    if len(val) > 8:
        val = val[:8]
    return val or None


def evaluate_group_risk(
    session: Session,
    *,
    creator_tg_id: int,
    name: str,
    description: Optional[str],
    tags: Sequence[str],
    invite_link: str,
) -> RiskResult:
    score = 0
    flags: List[str] = []

    tokens = (name or "").lower()
    desc_lower = (description or "").lower()

    for word in BANNED_KEYWORDS:
        if word in tokens or word in desc_lower:
            flags.append(f"keyword:{word}")
            score += 3

    # 重复邀请链接
    duplicated = session.execute(
        select(func.count())
        .select_from(PublicGroup)
        .where(PublicGroup.invite_link == invite_link)
    ).scalar_one()
    if duplicated:
        flags.append("duplicate_invite_link")
        score += 5

    # 创建频率
    cutoff = datetime.utcnow() - timedelta(minutes=RECENT_CREATE_MINUTES)
    recent_count = session.execute(
        select(func.count())
        .select_from(PublicGroup)
        .where(
            and_(
                PublicGroup.creator_tg_id == int(creator_tg_id),
                PublicGroup.created_at >= cutoff,
            )
        )
    ).scalar_one()
    if recent_count >= RECENT_CREATE_LIMIT:
        flags.append("frequency_high")
        score += 4

    # 标签风险（重复或过多）
    if len(tags) > MAX_TAGS:
        flags.append("too_many_tags")
        score += 1
    if len(set(tags)) < len(tags):
        flags.append("duplicate_tags")
        score += 1

    return RiskResult(score=score, flags=flags)


def create_group(
    session: Session,
    *,
    creator_tg_id: int,
    name: str,
    invite_link: str,
    description: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    language: Optional[str] = None,
    cover_template: Optional[str] = None,
    entry_reward_enabled: bool = True,
    entry_reward_points: Optional[int] = None,
    entry_reward_pool_max: Optional[int] = None,
) -> Tuple[PublicGroup, RiskResult]:
    op = "create"
    start = perf_counter()
    try:
        if not name or len(name.strip()) < 3:
            raise PublicGroupError("group_name_invalid")
        name = name.strip()
        if len(name) > MAX_NAME_LENGTH:
            raise PublicGroupError("group_name_too_long")

        if description and len(description) > MAX_DESC_LENGTH:
            raise PublicGroupError("group_description_too_long")

        if not invite_link or not invite_link.startswith(("https://t.me/", "http://t.me/")):
            raise PublicGroupError("invite_link_invalid")

        norm_tags = _normalize_tags(tags)
        lang = _normalize_language(language)

        reward_points = ENTRY_REWARD_DEFAULT_POINTS if entry_reward_points is None else int(entry_reward_points)
        reward_pool_max = ENTRY_REWARD_DEFAULT_POOL if entry_reward_pool_max is None else int(entry_reward_pool_max)

        if reward_points < 0 or reward_points > ENTRY_REWARD_MAX_POINTS:
            raise PublicGroupError("entry_reward_points_invalid")
        if reward_pool_max < 0:
            raise PublicGroupError("entry_reward_pool_invalid")

        risk = evaluate_group_risk(
            session,
            creator_tg_id=creator_tg_id,
            name=name,
            description=description,
            tags=norm_tags,
            invite_link=invite_link,
        )

        group = PublicGroup(
            creator_tg_id=int(creator_tg_id),
            name=name,
            description=description.strip() if description else None,
            language=lang,
            invite_link=invite_link.strip(),
            cover_template=(cover_template or "").strip() or None,
        )
        group.tags = norm_tags
        group.status = PublicGroupStatus.REVIEW if risk.requires_review else PublicGroupStatus.ACTIVE
        group.risk_score = risk.score
        group.risk_flags = risk.flags

        group.entry_reward_enabled = bool(entry_reward_enabled)
        group.entry_reward_points = reward_points
        group.entry_reward_pool_max = reward_pool_max
        group.entry_reward_pool = reward_pool_max if entry_reward_enabled else 0

        session.add(group)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise PublicGroupError("invite_link_conflict") from exc

        creator = session.execute(
            select(User).where(User.tg_id == int(creator_tg_id))
        ).scalar_one_or_none()
        if creator:
            group.creator_user_id = creator.id

        group.touch()
        session.add(group)

        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="success")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="success")
        if risk.requires_review:
            _PG_OPERATION_COUNTER.inc(operation=op, status="review")
            _PG_OPERATION_LATENCY.observe(duration, operation=op, status="review")
            log.warning(
                "public_group.create flagged_for_review group_id=%s creator=%s score=%s flags=%s",
                group.id,
                creator_tg_id,
                risk.score,
                ",".join(risk.flags),
            )
        else:
            log.info(
                "public_group.create success group_id=%s creator=%s score=%s flags=%s",
                group.id,
                creator_tg_id,
                risk.score,
                ",".join(risk.flags),
            )
        return group, risk
    except PublicGroupError as exc:
        duration = perf_counter() - start
        status = exc.args[0] if exc.args else "public_group_error"
        _PG_OPERATION_COUNTER.inc(operation=op, status=status)
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status=status)
        log.warning(
            "public_group.create failed creator=%s status=%s",
            creator_tg_id,
            status,
        )
        raise
    except Exception:
        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="unexpected")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="unexpected")
        log.exception("public_group.create unexpected_error creator=%s", creator_tg_id)
        raise


def join_group(
    session: Session,
    *,
    group_id: int,
    user_tg_id: int,
    grant_reward: bool = True,
) -> Dict[str, object]:
    op = "join"
    start = perf_counter()
    try:
        group = session.get(PublicGroup, int(group_id))
        if not group:
            raise PublicGroupError("group_not_found")
        if group.status != PublicGroupStatus.ACTIVE:
            raise PublicGroupError("group_not_active")

        membership = session.execute(
            select(PublicGroupMember)
            .where(
                PublicGroupMember.group_id == group.id,
                PublicGroupMember.user_tg_id == int(user_tg_id),
            )
        ).scalar_one_or_none()

        created = False
        if not membership:
            membership = PublicGroupMember(
                group_id=group.id,
                user_tg_id=int(user_tg_id),
            )
            session.add(membership)
            created = True
            group.members_count += 1
            group.joins_today += 1
            session.flush()

        membership.last_active_at = datetime.utcnow()

        reward_claimed = False
        reward_points = 0
        reward_status = "skipped"

        if (
            grant_reward
            and group.entry_reward_enabled
            and group.entry_reward_points > 0
            and group.entry_reward_pool >= group.entry_reward_points
        ):
            existing_claim = session.execute(
                select(PublicGroupRewardClaim).where(
                    PublicGroupRewardClaim.group_id == group.id,
                    PublicGroupRewardClaim.user_tg_id == int(user_tg_id),
                )
            ).scalar_one_or_none()
            if not existing_claim:
                claim = PublicGroupRewardClaim(
                    group_id=group.id,
                    user_tg_id=int(user_tg_id),
                    points=group.entry_reward_points,
                    status="ok",
                )
                session.add(claim)
                session.flush()
                reward_claimed = True
                reward_points = group.entry_reward_points
                reward_status = "ok"
                group.entry_reward_pool -= group.entry_reward_points
            else:
                reward_status = existing_claim.status

        group.touch()
        session.add(group)

        bonus_points = 0
        bonus_details: List[Dict[str, object]] = []

        try:
            bonus_points, bonus_details = apply_join_bonus(
                session,
                group_id=group.id,
                user_tg_id=user_tg_id,
            )
        except Exception:
            log.exception("public_group.activity.bonus_failed group_id=%s user=%s", group_id, user_tg_id)
            bonus_points = 0
            bonus_details = []

        if bonus_points:
            user_obj = get_or_create_user(session, tg_id=int(user_tg_id))
            current = getattr(user_obj, "point_balance", 0) or 0
            user_obj.point_balance = current + bonus_points
            session.add(user_obj)

        for detail in bonus_details:
            activity_id = detail.get("activity_id")
            if not activity_id:
                continue
            try:
                emit_activity_conversion(
                    session,
                    activity_id=int(activity_id),
                    group=group,
                    user_tg_id=user_tg_id,
                    points=int(detail.get("points") or 0),
                    metadata={"activity_name": detail.get("name")},
                )
            except Exception:
                log.exception(
                    "public_group.activity.webhook_emit_failed activity_id=%s group_id=%s user=%s",
                    activity_id,
                    group_id,
                    user_tg_id,
                )

        payload = {
            "membership_created": created,
            "reward_claimed": reward_claimed,
            "reward_points": reward_points,
            "reward_status": reward_status,
            "entry_reward_pool": group.entry_reward_pool,
            "bonus_points": bonus_points,
            "bonus_details": bonus_details,
        }

        duration = perf_counter() - start
        if created:
            status = "joined_rewarded" if reward_claimed else "joined"
        else:
            status = "existing"
        _PG_OPERATION_COUNTER.inc(operation=op, status=status)
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status=status)
        log.info(
            "public_group.join status=%s group_id=%s user=%s reward_status=%s created=%s pool=%s",
            status,
            group_id,
            user_tg_id,
            reward_status,
            created,
            group.entry_reward_pool,
        )
        return payload
    except PublicGroupError as exc:
        duration = perf_counter() - start
        status = exc.args[0] if exc.args else "public_group_error"
        _PG_OPERATION_COUNTER.inc(operation=op, status=status)
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status=status)
        log.warning(
            "public_group.join failed group_id=%s user=%s status=%s",
            group_id,
            user_tg_id,
            status,
        )
        raise
    except Exception:
        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="unexpected")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="unexpected")
        log.exception("public_group.join unexpected_error group_id=%s user=%s", group_id, user_tg_id)
        raise


def pin_group(
    session: Session,
    *,
    group_id: int,
    operator_tg_id: int,
    duration_hours: Optional[int] = None,
    pin_limit: int = PIN_LIMIT,
) -> PublicGroup:
    op = "pin"
    start = perf_counter()
    try:
        group = session.get(PublicGroup, int(group_id))
        if not group:
            raise PublicGroupError("group_not_found")

        now = datetime.utcnow()
        duration = duration_hours or DEFAULT_PIN_DURATION_HOURS
        if duration <= 0:
            raise PublicGroupError("pin_duration_invalid")

        active_pins = session.execute(
            select(func.count())
            .select_from(PublicGroup)
            .where(
                and_(
                    PublicGroup.is_pinned.is_(True),
                    PublicGroup.pinned_until.isnot(None),
                    PublicGroup.pinned_until > now,
                )
            )
        ).scalar_one()

        if not group.is_pinned and active_pins >= pin_limit:
            raise PublicGroupError("pin_limit_reached")

        group.is_pinned = True
        group.pinned_at = now
        group.pinned_until = now + timedelta(hours=duration)
        group.touch()
        session.add(group)

        duration_sec = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="success")
        _PG_OPERATION_LATENCY.observe(duration_sec, operation=op, status="success")
        log.info(
            "public_group.pin success group_id=%s operator=%s duration=%s",
            group_id,
            operator_tg_id,
            duration,
        )
        return group
    except PublicGroupError as exc:
        duration = perf_counter() - start
        status = exc.args[0] if exc.args else "public_group_error"
        _PG_OPERATION_COUNTER.inc(operation=op, status=status)
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status=status)
        log.warning(
            "public_group.pin failed group_id=%s operator=%s status=%s",
            group_id,
            operator_tg_id,
            status,
        )
        raise
    except Exception:
        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="unexpected")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="unexpected")
        log.exception("public_group.pin unexpected_error group_id=%s operator=%s", group_id, operator_tg_id)
        raise


def unpin_group(session: Session, *, group_id: int) -> PublicGroup:
    op = "unpin"
    start = perf_counter()
    try:
        group = session.get(PublicGroup, int(group_id))
        if not group:
            raise PublicGroupError("group_not_found")
        group.is_pinned = False
        group.pinned_until = None
        group.touch()
        session.add(group)

        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="success")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="success")
        log.info("public_group.unpin success group_id=%s", group_id)
        return group
    except PublicGroupError as exc:
        duration = perf_counter() - start
        status = exc.args[0] if exc.args else "public_group_error"
        _PG_OPERATION_COUNTER.inc(operation=op, status=status)
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status=status)
        log.warning("public_group.unpin failed group_id=%s status=%s", group_id, status)
        raise
    except Exception:
        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="unexpected")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="unexpected")
        log.exception("public_group.unpin unexpected_error group_id=%s", group_id)
        raise


def update_group(
    session: Session,
    *,
    group_id: int,
    updater_tg_id: int,
    description: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    language: Optional[str] = None,
    entry_reward_enabled: Optional[bool] = None,
    entry_reward_points: Optional[int] = None,
    entry_reward_pool: Optional[int] = None,
    entry_reward_pool_max: Optional[int] = None,
    cover_template: Optional[str] = None,
    is_admin: bool = False,
) -> PublicGroup:
    op = "update"
    start = perf_counter()
    try:
        group = session.get(PublicGroup, int(group_id))
        if not group:
            raise PublicGroupError("group_not_found")

        owner_id = int(getattr(group, "creator_tg_id", 0) or 0)
        if not is_admin and owner_id not in {int(updater_tg_id), 0}:
            raise PublicGroupError("forbidden")

        if description is not None:
            description = description.strip()
            if description and len(description) > MAX_DESC_LENGTH:
                raise PublicGroupError("group_description_too_long")
            group.description = description or None

        if tags is not None:
            group.tags = _normalize_tags(tags)

        if language is not None:
            group.language = _normalize_language(language)

        if cover_template is not None:
            cover_template = cover_template.strip()
            group.cover_template = cover_template or None

        if entry_reward_points is not None:
            value = int(entry_reward_points)
            if value < 0 or value > ENTRY_REWARD_MAX_POINTS:
                raise PublicGroupError("entry_reward_points_invalid")
            group.entry_reward_points = value

        if entry_reward_pool_max is not None:
            value = int(entry_reward_pool_max)
            if value < 0:
                raise PublicGroupError("entry_reward_pool_invalid")
            group.entry_reward_pool_max = value
            if group.entry_reward_pool > value:
                group.entry_reward_pool = value

        if entry_reward_enabled is not None:
            group.entry_reward_enabled = bool(entry_reward_enabled)
            if not entry_reward_enabled:
                group.entry_reward_pool = 0

        if entry_reward_pool is not None:
            value = int(entry_reward_pool)
            if value < 0:
                raise PublicGroupError("entry_reward_pool_invalid")
            if value > group.entry_reward_pool_max:
                value = group.entry_reward_pool_max
            group.entry_reward_pool = value

        group.touch()
        session.add(group)

        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="success")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="success")
        log.info(
            "public_group.update success group_id=%s updater=%s admin=%s",
            group_id,
            updater_tg_id,
            is_admin,
        )
        return group
    except PublicGroupError as exc:
        duration = perf_counter() - start
        status = exc.args[0] if exc.args else "public_group_error"
        _PG_OPERATION_COUNTER.inc(operation=op, status=status)
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status=status)
        log.warning(
            "public_group.update failed group_id=%s updater=%s status=%s",
            group_id,
            updater_tg_id,
            status,
        )
        raise
    except Exception:
        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="unexpected")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="unexpected")
        log.exception("public_group.update unexpected_error group_id=%s updater=%s", group_id, updater_tg_id)
        raise


def add_bookmark(
    session: Session,
    *,
    group_id: int,
    user_tg_id: int,
) -> Tuple[PublicGroupBookmark, bool]:
    op = "bookmark_add"
    start = perf_counter()
    try:
        group = session.get(PublicGroup, int(group_id))
        if not group:
            raise PublicGroupError("group_not_found")
        if group.status != PublicGroupStatus.ACTIVE:
            raise PublicGroupError("group_not_active")

        existing = session.execute(
            select(PublicGroupBookmark)
            .where(
                PublicGroupBookmark.group_id == group.id,
                PublicGroupBookmark.user_tg_id == int(user_tg_id),
            )
        ).scalar_one_or_none()
        if existing:
            return existing, False

        bookmark = PublicGroupBookmark(
            group_id=group.id,
            user_tg_id=int(user_tg_id),
        )
        session.add(bookmark)
        session.flush()

        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="created")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="created")
        log.info("public_group.bookmark_add group_id=%s user=%s", group_id, user_tg_id)
        return bookmark, True
    except PublicGroupError as exc:
        duration = perf_counter() - start
        status = exc.args[0] if exc.args else "public_group_error"
        _PG_OPERATION_COUNTER.inc(operation=op, status=status)
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status=status)
        log.warning(
            "public_group.bookmark_add_failed group_id=%s user=%s status=%s",
            group_id,
            user_tg_id,
            status,
        )
        raise
    except Exception:
        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="unexpected")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="unexpected")
        log.exception("public_group.bookmark_add_unexpected group_id=%s user=%s", group_id, user_tg_id)
        raise


def remove_bookmark(
    session: Session,
    *,
    group_id: int,
    user_tg_id: int,
) -> bool:
    op = "bookmark_remove"
    start = perf_counter()
    try:
        bookmark = session.execute(
            select(PublicGroupBookmark)
            .where(
                PublicGroupBookmark.group_id == int(group_id),
                PublicGroupBookmark.user_tg_id == int(user_tg_id),
            )
        ).scalar_one_or_none()
        if not bookmark:
            return False
        session.delete(bookmark)
        session.flush()

        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="deleted")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="deleted")
        log.info("public_group.bookmark_remove group_id=%s user=%s", group_id, user_tg_id)
        return True
    except Exception:
        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="unexpected")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="unexpected")
        log.exception("public_group.bookmark_remove_unexpected group_id=%s user=%s", group_id, user_tg_id)
        raise


def list_bookmarked_groups(
    session: Session,
    *,
    user_tg_id: int,
    limit: Optional[int] = None,
) -> List[PublicGroup]:
    stmt = (
        select(PublicGroup)
        .join(
            PublicGroupBookmark,
            PublicGroupBookmark.group_id == PublicGroup.id,
        )
        .where(PublicGroupBookmark.user_tg_id == int(user_tg_id))
        .order_by(PublicGroupBookmark.created_at.desc())
    )
    if limit:
        stmt = stmt.limit(max(1, limit))
    return list(session.execute(stmt).scalars().all())


def get_user_bookmark_ids(session: Session, *, user_tg_id: int) -> List[int]:
    stmt = select(PublicGroupBookmark.group_id).where(PublicGroupBookmark.user_tg_id == int(user_tg_id))
    return [int(row[0]) for row in session.execute(stmt).all()]


def list_groups(
    session: Session,
    *,
    limit: int = 10,
    language: Optional[str] = None,
    include_review: bool = False,
    search: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    sort_by: str = "default",
) -> List[PublicGroup]:
    stmt = select(PublicGroup)

    if not include_review:
        stmt = stmt.where(PublicGroup.status == PublicGroupStatus.ACTIVE)

    if language:
        stmt = stmt.where(PublicGroup.language == _normalize_language(language))

    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(PublicGroup.name).like(pattern),
                func.lower(func.coalesce(PublicGroup.description, "")).like(pattern),
                func.lower(PublicGroup.tags_raw).like(pattern),
            )
        )

    if tags:
        for tag in tags:
            normalized = str(tag).strip().lower()
            if not normalized:
                continue
            stmt = stmt.where(PublicGroup.tags_raw.ilike(f'%"{normalized}"%'))

    base_order = [
        PublicGroup.is_pinned.desc(),
        PublicGroup.pinned_until.desc().nullslast(),
    ]

    sort_key = (sort_by or "default").lower()
    if sort_key == "members":
        order_clause = base_order + [PublicGroup.members_count.desc(), PublicGroup.created_at.desc()]
    elif sort_key == "reward":
        order_clause = base_order + [
            PublicGroup.entry_reward_pool.desc(),
            PublicGroup.entry_reward_points.desc(),
            PublicGroup.created_at.desc(),
        ]
    elif sort_key == "new":
        order_clause = base_order + [PublicGroup.created_at.desc()]
    else:
        order_clause = base_order + [PublicGroup.created_at.desc()]

    stmt = stmt.order_by(*order_clause)

    if limit:
        stmt = stmt.limit(max(1, limit))

    return list(session.execute(stmt).scalars().all())


def serialize_group(group: PublicGroup, *, is_bookmarked: bool = False) -> Dict[str, object]:
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "language": group.language,
        "tags": group.tags,
        "invite_link": group.invite_link,
        "cover_template": group.cover_template,
        "entry_reward_enabled": group.entry_reward_enabled,
        "entry_reward_points": group.entry_reward_points,
        "entry_reward_pool": group.entry_reward_pool,
        "entry_reward_pool_max": getattr(group, "entry_reward_pool_max", 0),
        "is_pinned": group.is_pinned,
        "pinned_until": group.pinned_until.isoformat() if group.pinned_until else None,
        "status": group.status.value if isinstance(group.status, PublicGroupStatus) else group.status,
        "risk_score": group.risk_score,
        "risk_flags": group.risk_flags,
        "members_count": group.members_count,
        "joins_today": getattr(group, "joins_today", 0),
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "is_bookmarked": bool(is_bookmarked),
    }


def set_group_status(
    session: Session,
    *,
    group_id: int,
    target_status: PublicGroupStatus | str,
    operator_tg_id: int,
    note: Optional[str] = None,
) -> PublicGroup:
    op = "status"
    start = perf_counter()
    try:
        group = session.get(PublicGroup, int(group_id))
        if not group:
            raise PublicGroupError("group_not_found")

        if isinstance(target_status, str):
            try:
                status_enum = PublicGroupStatus(target_status)
            except Exception:
                raise PublicGroupError("status_invalid")
        else:
            status_enum = target_status

        group.status = status_enum
        group.touch()
        session.add(group)

        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status=status_enum.value)
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status=status_enum.value)
        log.info(
            "public_group.status_change group_id=%s operator=%s status=%s note=%s",
            group_id,
            operator_tg_id,
            status_enum.value,
            (note or "").strip(),
        )
        return group
    except PublicGroupError as exc:
        duration = perf_counter() - start
        status = exc.args[0] if exc.args else "public_group_error"
        _PG_OPERATION_COUNTER.inc(operation=op, status=status)
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status=status)
        log.warning(
            "public_group.status_change_failed group_id=%s operator=%s status=%s",
            group_id,
            operator_tg_id,
            status,
        )
        raise
    except Exception:
        duration = perf_counter() - start
        _PG_OPERATION_COUNTER.inc(operation=op, status="unexpected")
        _PG_OPERATION_LATENCY.observe(duration, operation=op, status="unexpected")
        log.exception("public_group.status_change_unexpected group_id=%s operator=%s", group_id, operator_tg_id)
        raise


def bulk_set_group_status(
    session: Session,
    *,
    group_ids: Sequence[int],
    target_status: PublicGroupStatus | str,
    operator_tg_id: int,
    note: Optional[str] = None,
) -> Dict[str, object]:
    if not group_ids:
        return {"target": None, "updated": [], "errors": []}

    if isinstance(target_status, str):
        try:
            status_enum = PublicGroupStatus(target_status)
        except Exception as exc:
            raise PublicGroupError("status_invalid") from exc
    else:
        status_enum = target_status

    dedup_ids: List[int] = []
    seen: set[int] = set()
    for raw_id in group_ids:
        try:
            gid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if gid <= 0 or gid in seen:
            continue
        dedup_ids.append(gid)
        seen.add(gid)

    summary = {"target": status_enum.value, "updated": [], "errors": []}

    for gid in dedup_ids:
        try:
            group = set_group_status(
                session,
                group_id=gid,
                target_status=status_enum,
                operator_tg_id=operator_tg_id,
                note=note,
            )
            summary["updated"].append(group.id)
        except PublicGroupError as exc:
            summary["errors"].append(
                {
                    "group_id": gid,
                    "error": exc.args[0] if exc.args else "public_group_error",
                }
            )
        except Exception:
            summary["errors"].append(
                {
                    "group_id": gid,
                    "error": "unexpected",
                }
            )
    return summary


__all__ = [
    "create_group",
    "join_group",
    "pin_group",
    "unpin_group",
    "update_group",
    "list_groups",
    "add_bookmark",
    "remove_bookmark",
    "list_bookmarked_groups",
    "get_user_bookmark_ids",
    "serialize_group",
    "set_group_status",
    "bulk_set_group_status",
    "PublicGroupError",
    "RiskResult",
    "evaluate_group_risk",
]

