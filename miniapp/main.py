# -*- coding: utf-8 -*-
"""
MiniApp REST API for public dating groups.

This FastAPI application is designed for Telegram MiniApps / web frontends that
need to browse, create, join and manage public groups. Authentication is kept
lightweight on purpose: callers must provide ``X-TG-USER-ID`` header which is
the Telegram user id extracted from `initData`. Production deployment应在
反向代理层做合法性校验（例如 verify initData 或加签）。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from config.load_env import load_env

load_env()

from config.feature_flags import flags_obj  # noqa: E402
from config.settings import is_admin  # noqa: E402
from models.db import get_db, init_db  # noqa: E402
from models.public_group import PublicGroup, PublicGroupStatus  # noqa: E402
from services.public_group_service import (  # noqa: E402
    PublicGroupError,
    RiskResult,
    ENTRY_REWARD_MAX_POINTS,
    MAX_DESC_LENGTH,
    MAX_NAME_LENGTH,
    add_bookmark,
    get_user_bookmark_ids,
    create_group,
    remove_bookmark,
    list_bookmarked_groups,
    join_group,
    list_groups,
    pin_group,
    serialize_group,
    unpin_group,
    update_group,
)
from services.public_group_activity import (  # noqa: E402
    get_active_campaign_summaries,
    get_active_campaign_detail,
    create_or_update_webhook,
    deactivate_webhook,
    drop_webhook,
    list_active_webhooks,
)
from services.public_group_report import create_report_case  # noqa: E402
from services.public_group_tracking import (  # noqa: E402
    ALLOWED_EVENT_TYPES,
    fetch_conversion_summary,
    record_event,
)

log = logging.getLogger("miniapp.api")

app = FastAPI(
    title="MiniApp Public Group API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------
class PublicGroupSummary(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    language: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    invite_link: str
    cover_template: Optional[str] = None
    entry_reward_enabled: bool
    entry_reward_points: int
    entry_reward_pool: int
    entry_reward_pool_max: int
    is_pinned: bool
    pinned_until: Optional[str] = None
    status: str
    risk_score: int
    risk_flags: List[str] = Field(default_factory=list)
    members_count: int
    created_at: Optional[str] = None
    is_bookmarked: bool = False


class RiskPayload(BaseModel):
    score: int
    flags: List[str] = Field(default_factory=list)
    requires_review: bool

    @classmethod
    def from_result(cls, risk: RiskResult) -> "RiskPayload":
        return cls(score=risk.score, flags=list(risk.flags), requires_review=risk.requires_review)


class PublicGroupCreateBody(BaseModel):
    name: str = Field(..., min_length=3, max_length=MAX_NAME_LENGTH)
    invite_link: str = Field(..., min_length=10)
    description: Optional[str] = Field(default=None, max_length=MAX_DESC_LENGTH)
    tags: Optional[List[str]] = None
    language: Optional[str] = None
    cover_template: Optional[str] = None
    entry_reward_enabled: bool = True
    entry_reward_points: Optional[int] = Field(default=None, ge=0, le=ENTRY_REWARD_MAX_POINTS)
    entry_reward_pool_max: Optional[int] = Field(default=None, ge=0)


class PublicGroupCreateResponse(BaseModel):
    group: PublicGroupSummary
    risk: RiskPayload


class PublicGroupJoinResponse(BaseModel):
    membership_created: bool
    reward_claimed: bool
    reward_points: int
    reward_status: str
    entry_reward_pool: int
    bonus_points: int = 0
    bonus_details: List[Dict[str, object]] = Field(default_factory=list)


class PublicGroupUpdateBody(BaseModel):
    description: Optional[str] = Field(default=None, max_length=MAX_DESC_LENGTH)
    tags: Optional[List[str]] = None
    language: Optional[str] = None
    entry_reward_enabled: Optional[bool] = None
    entry_reward_points: Optional[int] = Field(default=None, ge=0, le=ENTRY_REWARD_MAX_POINTS)
    entry_reward_pool: Optional[int] = Field(default=None, ge=0)
    entry_reward_pool_max: Optional[int] = Field(default=None, ge=0)
    cover_template: Optional[str] = None


class ReportBody(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)
    details: Optional[str] = Field(default=None, max_length=2000)


class UserContext(BaseModel):
    tg_id: int
    is_admin: bool = False


class PublicGroupActivitySummary(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    activity_type: str
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    reward_points: int
    bonus_points: int
    highlight_slots: int
    highlight_enabled: bool
    daily_cap: Optional[int] = None
    total_cap: Optional[int] = None
    remaining_daily: Optional[int] = None
    remaining_total: Optional[int] = None
    status: str
    time_left_seconds: Optional[int] = None
    config: Dict[str, object] = Field(default_factory=dict)
    headline: Optional[str] = None
    countdown_text: Optional[str] = None
    highlight_badge: Optional[str] = None
    front_card: Dict[str, object] = Field(default_factory=dict)
    front_priority: Optional[int] = None
    has_participated: bool = False


class ActivityRuleItem(BaseModel):
    key: str
    label: str
    value: Optional[int | str] = None
    remaining: Optional[int] = None


class PublicGroupActivityDetail(PublicGroupActivitySummary):
    total_points: int
    eligible: bool
    in_time_window: bool
    rules: List[ActivityRuleItem] = Field(default_factory=list)


class GroupEventBody(BaseModel):
    event_type: str = Field(..., max_length=16)
    context: Optional[Dict[str, object]] = None

    @field_validator("event_type")
    def _normalize_event_type(cls, value: str) -> str:
        if value is None:
            raise ValueError("event_type_required")
        cleaned = value.strip().lower()
        if not cleaned:
            raise ValueError("event_type_required")
        return cleaned


class ConversionStats(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    period: str
    from_: str = Field(..., alias="from")
    to: str
    totals: Dict[str, int]
    conversion: Dict[str, float]
    top_groups: List[Dict[str, object]]


class BookmarkStatusResponse(BaseModel):
    bookmarked: bool


class WebhookPayload(BaseModel):
    url: str = Field(..., max_length=500)
    secret: Optional[str] = Field(default=None, max_length=128)
    is_active: bool = True


class WebhookResponse(BaseModel):
    id: int
    activity_id: int
    url: str
    is_active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Dependencies & helpers
# ---------------------------------------------------------------------------
def ensure_public_groups_enabled() -> None:
    if not flags_obj.ENABLE_PUBLIC_GROUPS:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="public_groups_disabled")


def get_current_user(request: Request) -> UserContext:
    raw_id = request.headers.get("x-tg-user-id") or request.headers.get("x-telegram-user-id")
    if not raw_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_user_header")
    try:
        tg_id = int(raw_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_user_id")
    return UserContext(tg_id=tg_id, is_admin=is_admin(tg_id))


def get_optional_user(request: Request) -> Optional[UserContext]:
    raw_id = request.headers.get("x-tg-user-id") or request.headers.get("x-telegram-user-id")
    if not raw_id:
        return None
    try:
        tg_id = int(raw_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_user_id")
    return UserContext(tg_id=tg_id, is_admin=is_admin(tg_id))


def _to_summary(group: PublicGroup, *, is_bookmarked: bool = False) -> PublicGroupSummary:
    data = serialize_group(group, is_bookmarked=is_bookmarked)
    return PublicGroupSummary(
        id=data["id"],
        name=data["name"],
        description=data.get("description"),
        language=data.get("language"),
        tags=list(data.get("tags") or []),
        invite_link=data["invite_link"],
        cover_template=data.get("cover_template"),
        entry_reward_enabled=bool(data.get("entry_reward_enabled")),
        entry_reward_points=int(data.get("entry_reward_points") or 0),
        entry_reward_pool=int(data.get("entry_reward_pool") or 0),
        entry_reward_pool_max=int(data.get("entry_reward_pool_max") or 0),
        is_pinned=bool(data.get("is_pinned")),
        pinned_until=data.get("pinned_until"),
        status=data.get("status") or PublicGroupStatus.ACTIVE.value,
        risk_score=int(data.get("risk_score") or 0),
        risk_flags=list(data.get("risk_flags") or []),
        members_count=int(data.get("members_count") or 0),
        created_at=data.get("created_at"),
        is_bookmarked=bool(data.get("is_bookmarked")),
    )


def _require_admin(user: UserContext) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")


def _to_webhook_response(webhook) -> WebhookResponse:
    return WebhookResponse(
        id=webhook.id,
        activity_id=webhook.activity_id,
        url=webhook.url,
        is_active=bool(webhook.is_active),
        created_at=webhook.created_at.isoformat() if getattr(webhook, "created_at", None) else None,
        updated_at=webhook.updated_at.isoformat() if getattr(webhook, "updated_at", None) else None,
    )


# ---------------------------------------------------------------------------
# Startup hooks
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _startup() -> None:
    try:
        init_db()
    except Exception as exc:  # pragma: no cover - best effort logging
        log.exception("init_db failed during startup: %s", exc)


# ---------------------------------------------------------------------------
# Basic endpoints
# ---------------------------------------------------------------------------
@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/v1/groups/public", response_model=List[PublicGroupSummary])
def api_list_public_groups(
    limit: int = Query(10, ge=1, le=50),
    q: Optional[str] = Query(None, max_length=64),
    tags: Optional[List[str]] = Query(None),
    sort: str = Query("default"),
    include_review: bool = Query(False),
    language: Optional[str] = None,
    user: Optional[UserContext] = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> List[PublicGroupSummary]:
    ensure_public_groups_enabled()
    normalized_tags = None
    if tags:
        normalized_tags = [t.strip().lower() for t in tags if t and t.strip()]
    allow_review = include_review and user is not None and user.is_admin
    bookmark_ids: set[int] = set()
    if user:
        bookmark_ids = set(get_user_bookmark_ids(db, user_tg_id=user.tg_id))
    groups = list_groups(
        db,
        limit=limit,
        language=language,
        include_review=allow_review,
        search=q,
        tags=normalized_tags,
        sort_by=sort,
    )
    return [_to_summary(g, is_bookmarked=(g.id in bookmark_ids)) for g in groups]


@app.get("/v1/groups/public/bookmarks", response_model=List[PublicGroupSummary])
def api_list_public_group_bookmarks(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[PublicGroupSummary]:
    ensure_public_groups_enabled()
    groups = list_bookmarked_groups(db, user_tg_id=user.tg_id)
    return [_to_summary(group, is_bookmarked=True) for group in groups]


@app.get(
    "/v1/groups/public/activities/{activity_id}/webhooks",
    response_model=List[WebhookResponse],
)
def api_list_activity_webhooks(
    activity_id: int,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[WebhookResponse]:
    ensure_public_groups_enabled()
    _require_admin(user)
    webhooks = list_active_webhooks(db, activity_id=activity_id)
    return [_to_webhook_response(wh) for wh in webhooks]


@app.post(
    "/v1/groups/public/activities/{activity_id}/webhooks",
    response_model=WebhookResponse,
)
def api_create_activity_webhook(
    activity_id: int,
    payload: WebhookPayload,
    response: Response,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WebhookResponse:
    ensure_public_groups_enabled()
    _require_admin(user)
    try:
        webhook, created = create_or_update_webhook(
            db,
            activity_id=activity_id,
            url=payload.url,
            secret=payload.secret,
            is_active=payload.is_active,
        )
        db.commit()
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return _to_webhook_response(webhook)
    except Exception:
        db.rollback()
        log.exception("activity_webhook.upsert_failed activity_id=%s", activity_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="webhook_upsert_failed")


@app.delete(
    "/v1/groups/public/activities/webhooks/{webhook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def api_delete_activity_webhook(
    webhook_id: int,
    hard: bool = Query(False),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    ensure_public_groups_enabled()
    _require_admin(user)
    try:
        soft_removed = deactivate_webhook(db, webhook_id=webhook_id)
        hard_removed = drop_webhook(db, webhook_id=webhook_id) if hard else False
        removed = soft_removed or hard_removed
        db.commit()
    except Exception:
        db.rollback()
        log.exception("activity_webhook.delete_failed webhook_id=%s", webhook_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="webhook_delete_failed")
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="webhook_not_found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/v1/groups/public/activities", response_model=List[PublicGroupActivitySummary])
def api_list_public_group_activities(
    user: Optional[UserContext] = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> List[PublicGroupActivitySummary]:
    ensure_public_groups_enabled()
    summaries = get_active_campaign_summaries(db, user_tg_id=user.tg_id if user else None)
    enriched: List[PublicGroupActivitySummary] = []
    for item in summaries:
        payload = dict(item)
        front_card = dict(payload.get("front_card") or {})
        front_card.setdefault("title", payload.get("name"))
        if not front_card.get("subtitle"):
            front_card["subtitle"] = payload.get("description") or payload.get("headline")
        front_card.setdefault("countdown_seconds", payload.get("time_left_seconds"))
        if "countdown_text" not in front_card and payload.get("countdown_text"):
            front_card["countdown_text"] = payload["countdown_text"]
        payload["front_card"] = front_card
        payload["headline"] = payload.get("headline")
        payload["countdown_text"] = payload.get("countdown_text")
        payload["highlight_badge"] = payload.get("highlight_badge")
        enriched.append(PublicGroupActivitySummary(**payload))
    return enriched


@app.get(
    "/v1/groups/public/activities/{activity_id}",
    response_model=PublicGroupActivityDetail,
)
def api_get_public_group_activity_detail(
    activity_id: int,
    user: Optional[UserContext] = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> PublicGroupActivityDetail:
    ensure_public_groups_enabled()
    detail = get_active_campaign_detail(
        db,
        activity_id=activity_id,
        user_tg_id=user.tg_id if user else None,
    )
    if not detail:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="activity_not_found")

    front_card = dict(detail.get("front_card") or {})
    front_card.setdefault("title", detail.get("name"))
    if not front_card.get("subtitle"):
        front_card["subtitle"] = detail.get("description") or detail.get("headline")
    front_card.setdefault("countdown_seconds", detail.get("time_left_seconds"))
    if "countdown_text" not in front_card and detail.get("countdown_text"):
        front_card["countdown_text"] = detail["countdown_text"]
    detail["front_card"] = front_card

    return PublicGroupActivityDetail(**detail)


@app.get("/v1/groups/public/{group_id}", response_model=PublicGroupSummary)
def api_get_public_group(
    group_id: int,
    user: Optional[UserContext] = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> PublicGroupSummary:
    ensure_public_groups_enabled()
    group = db.get(PublicGroup, int(group_id))
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group_not_found")
    is_bookmarked = False
    if user:
        is_bookmarked = group.id in set(get_user_bookmark_ids(db, user_tg_id=user.tg_id))
    return _to_summary(group, is_bookmarked=is_bookmarked)


@app.post("/v1/groups/public", response_model=PublicGroupCreateResponse, status_code=status.HTTP_201_CREATED)
def api_create_public_group(
    payload: PublicGroupCreateBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PublicGroupCreateResponse:
    ensure_public_groups_enabled()
    try:
        group, risk = create_group(
            db,
            creator_tg_id=user.tg_id,
            name=payload.name,
            invite_link=payload.invite_link,
            description=payload.description,
            tags=payload.tags or [],
            language=payload.language,
            cover_template=payload.cover_template,
            entry_reward_enabled=payload.entry_reward_enabled,
            entry_reward_points=payload.entry_reward_points,
            entry_reward_pool_max=payload.entry_reward_pool_max,
        )
        db.commit()
        db.refresh(group)
    except PublicGroupError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.args[0] if exc.args else "invalid")
    except Exception:
        db.rollback()
        log.exception("create_group unexpected user=%s", user.tg_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal_error")

    return PublicGroupCreateResponse(group=_to_summary(group), risk=RiskPayload.from_result(risk))


@app.post("/v1/groups/public/{group_id}/join", response_model=PublicGroupJoinResponse)
def api_join_public_group(
    group_id: int,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PublicGroupJoinResponse:
    ensure_public_groups_enabled()
    try:
        result = join_group(db, group_id=group_id, user_tg_id=user.tg_id)
        record_event(
            db,
            group_id=group_id,
            event_type="join",
            user_tg_id=user.tg_id,
            context={"source": "miniapp_api"},
        )
        db.commit()
    except PublicGroupError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.args[0] if exc.args else "invalid")
    except Exception:
        db.rollback()
        log.exception("join_group unexpected group=%s user=%s", group_id, user.tg_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal_error")

    return PublicGroupJoinResponse(**result)


@app.post(
    "/v1/groups/public/{group_id}/bookmark",
    response_model=BookmarkStatusResponse,
)
def api_bookmark_public_group(
    group_id: int,
    response: Response,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BookmarkStatusResponse:
    ensure_public_groups_enabled()
    try:
        _, created = add_bookmark(db, group_id=group_id, user_tg_id=user.tg_id)
        db.commit()
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    except PublicGroupError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.args[0] if exc.args else "invalid")
    except Exception:
        db.rollback()
        log.exception("bookmark_group unexpected group=%s user=%s", group_id, user.tg_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal_error")
    return BookmarkStatusResponse(bookmarked=True)


@app.delete(
    "/v1/groups/public/{group_id}/bookmark",
    response_model=BookmarkStatusResponse,
)
def api_unbookmark_public_group(
    group_id: int,
    response: Response,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BookmarkStatusResponse:
    ensure_public_groups_enabled()
    try:
        removed = remove_bookmark(db, group_id=group_id, user_tg_id=user.tg_id)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("unbookmark_group unexpected group=%s user=%s", group_id, user.tg_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal_error")
    response.status_code = status.HTTP_200_OK if removed else status.HTTP_200_OK
    return BookmarkStatusResponse(bookmarked=False)


@app.post("/v1/groups/public/{group_id}/pin", response_model=PublicGroupSummary)
def api_pin_public_group(
    group_id: int,
    duration_hours: Optional[int] = Query(None, ge=1, le=72),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PublicGroupSummary:
    ensure_public_groups_enabled()
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")
    try:
        group = pin_group(db, group_id=group_id, operator_tg_id=user.tg_id, duration_hours=duration_hours)
        db.commit()
        db.refresh(group)
    except PublicGroupError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.args[0] if exc.args else "invalid")
    except Exception:
        db.rollback()
        log.exception("pin_group unexpected group=%s operator=%s", group_id, user.tg_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal_error")
    return _to_summary(group)


@app.post("/v1/groups/public/{group_id}/unpin", response_model=PublicGroupSummary)
def api_unpin_public_group(
    group_id: int,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PublicGroupSummary:
    ensure_public_groups_enabled()
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")
    try:
        group = unpin_group(db, group_id=group_id)
        db.commit()
        db.refresh(group)
    except PublicGroupError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.args[0] if exc.args else "invalid")
    except Exception:
        db.rollback()
        log.exception("unpin_group unexpected group=%s operator=%s", group_id, user.tg_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal_error")
    return _to_summary(group)


@app.get("/v1/groups/public/{group_id}/invite_link")
def api_get_invite_link(group_id: int, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)):
    ensure_public_groups_enabled()
    group = db.get(PublicGroup, int(group_id))
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group_not_found")
    return {"group_id": group.id, "invite_link": group.invite_link}


@app.patch("/v1/groups/public/{group_id}", response_model=PublicGroupSummary)
def api_update_public_group(
    group_id: int,
    payload: PublicGroupUpdateBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PublicGroupSummary:
    ensure_public_groups_enabled()
    try:
        group = update_group(
            db,
            group_id=group_id,
            updater_tg_id=user.tg_id,
            description=payload.description,
            tags=payload.tags,
            language=payload.language,
            entry_reward_enabled=payload.entry_reward_enabled,
            entry_reward_points=payload.entry_reward_points,
            entry_reward_pool=payload.entry_reward_pool,
            entry_reward_pool_max=payload.entry_reward_pool_max,
            cover_template=payload.cover_template,
            is_admin=user.is_admin,
        )
        db.commit()
        db.refresh(group)
    except PublicGroupError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.args[0] if exc.args else "invalid")
    except Exception:
        db.rollback()
        log.exception("update_group unexpected group=%s user=%s", group_id, user.tg_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal_error")
    return _to_summary(group)


@app.post("/v1/groups/public/{group_id}/report", status_code=status.HTTP_202_ACCEPTED)
def api_report_public_group(
    group_id: int,
    payload: ReportBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ensure_public_groups_enabled()
    group = db.get(PublicGroup, int(group_id))
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group_not_found")
    reason = (payload.reason or "").strip() or "unspecified"
    details = (payload.details or "").strip()
    try:
        create_report_case(
            db,
            group_id=group.id,
            reporter_tg_id=user.tg_id,
            report_type=reason.lower()[:32],
            description=details[:2000],
            reporter_username=None,
            metadata={"source": "miniapp"},
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("public_group.report.create_failed group=%s reporter=%s", group_id, user.tg_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal_error")
    return {"ok": True}


@app.post("/v1/groups/public/{group_id}/events", status_code=status.HTTP_201_CREATED)
def api_track_public_group_event(
    group_id: int,
    payload: GroupEventBody,
    user: Optional[UserContext] = Depends(get_optional_user),
    db: Session = Depends(get_db),
):
    ensure_public_groups_enabled()
    group = db.get(PublicGroup, int(group_id))
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group_not_found")

    event_type = payload.event_type.lower()
    if event_type not in ALLOWED_EVENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_event_type")

    try:
        event = record_event(
            db,
            group_id=group_id,
            event_type=event_type,
            user_tg_id=user.tg_id if user else None,
            context=payload.context,
        )
        db.flush()
        created_at = event.created_at.isoformat() + "Z" if event.created_at else None
        db.commit()
    except ValueError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_event_type")
    except Exception:
        db.rollback()
        log.exception("record_event unexpected group=%s type=%s", group_id, event_type)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal_error")

    return {
        "event_id": event.id,
        "group_id": group_id,
        "event_type": event_type,
        "created_at": created_at,
    }


@app.get("/v1/groups/public/stats/summary", response_model=ConversionStats)
def api_public_group_stats_summary(
    period: str = Query("7d"),
    limit: int = Query(10, ge=1, le=50),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConversionStats:
    ensure_public_groups_enabled()
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")
    try:
        summary = fetch_conversion_summary(db, period=period, limit=limit)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_period")
    return ConversionStats.model_validate(summary)

