"""
Public group models:
- PublicGroup: 基础公开群信息，统一本地元数据与风控字段
- PublicGroupMember: 记录加入群组的用户（Telegram tg_id 维度）
- PublicGroupRewardClaim: 入群奖励领取记录，确保一人一群仅一次
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence
import json
import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    BigInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base


class PublicGroupStatus(str, enum.Enum):
    ACTIVE = "active"
    REVIEW = "review"
    PAUSED = "paused"
    REMOVED = "removed"


def _dump_json(value: Optional[Sequence[str]]) -> str:
    if not value:
        return "[]"
    return json.dumps([str(v) for v in value], ensure_ascii=False)


def _load_json(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


class PublicGroup(Base):
    __tablename__ = "public_groups"
    __table_args__ = (
        UniqueConstraint("invite_link", name="uq_public_groups_invite_link"),
        Index("ix_public_groups_status_created", "status", "created_at"),
        Index("ix_public_groups_is_pinned", "is_pinned", "pinned_until"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    creator_tg_id = Column(BigInteger, nullable=False, index=True)
    creator_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    name = Column(String(80), nullable=False)
    description = Column(Text, nullable=True)
    language = Column(String(8), nullable=True)
    tags_raw = Column("tags", Text, nullable=False, default="[]")

    invite_link = Column(String(255), nullable=False)
    cover_template = Column(String(64), nullable=True)

    entry_reward_enabled = Column(Boolean, nullable=False, default=True)
    entry_reward_points = Column(Integer, nullable=False, default=5)
    entry_reward_pool = Column(Integer, nullable=False, default=0)
    entry_reward_pool_max = Column(Integer, nullable=False, default=0)

    members_count = Column(Integer, nullable=False, default=0)
    joins_today = Column(Integer, nullable=False, default=0)

    is_pinned = Column(Boolean, nullable=False, default=False)
    pinned_at = Column(DateTime, nullable=True)
    pinned_until = Column(DateTime, nullable=True)

    status = Column(Enum(PublicGroupStatus), nullable=False, default=PublicGroupStatus.ACTIVE)
    risk_score = Column(Integer, nullable=False, default=0)
    risk_flags_raw = Column("risk_flags", Text, nullable=False, default="[]")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    creator = relationship("User", backref="public_groups", lazy="joined", uselist=False)

    __mapper_args__ = {
        "eager_defaults": True,
    }

    __table_args__ = __table_args__ + (
        CheckConstraint("entry_reward_points >= 0", name="ck_public_groups_entry_reward_points"),
        CheckConstraint("entry_reward_pool >= 0", name="ck_public_groups_entry_reward_pool"),
        CheckConstraint("entry_reward_pool_max >= 0", name="ck_public_groups_entry_reward_pool_max"),
    )

    # --------- helpers ---------
    @property
    def tags(self) -> List[str]:
        return _load_json(self.tags_raw)

    @tags.setter
    def tags(self, value: Sequence[str]) -> None:
        self.tags_raw = _dump_json(value)

    @property
    def risk_flags(self) -> List[str]:
        return _load_json(self.risk_flags_raw)

    @risk_flags.setter
    def risk_flags(self, value: Sequence[str]) -> None:
        self.risk_flags_raw = _dump_json(value)

    def touch(self) -> None:
        self.updated_at = datetime.utcnow()


class PublicGroupMember(Base):
    __tablename__ = "public_group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "user_tg_id", name="uq_public_group_member_unique"),
        Index("ix_public_group_members_group_id", "group_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("public_groups.id", ondelete="CASCADE"), nullable=False)
    user_tg_id = Column(BigInteger, nullable=False)
    joined_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_active_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_banned = Column(Boolean, nullable=False, default=False)


class PublicGroupRewardClaim(Base):
    __tablename__ = "public_group_reward_claims"
    __table_args__ = (
        UniqueConstraint("group_id", "user_tg_id", name="uq_public_group_reward_once"),
        Index("ix_public_group_reward_claims_group_id", "group_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("public_groups.id", ondelete="CASCADE"), nullable=False)
    user_tg_id = Column(BigInteger, nullable=False)
    points = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="ok")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PublicGroupEvent(Base):
    __tablename__ = "public_group_events"
    __table_args__ = (
        Index("ix_public_group_events_group_type", "group_id", "event_type"),
        Index("ix_public_group_events_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("public_groups.id", ondelete="CASCADE"), nullable=False)
    user_tg_id = Column(BigInteger, nullable=True)
    event_type = Column(String(16), nullable=False)
    context_raw = Column("context", Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def context(self) -> Dict[str, object]:
        if not self.context_raw:
            return {}
        try:
            data = json.loads(self.context_raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    @context.setter
    def context(self, value: Optional[Dict[str, object]]) -> None:
        if not value:
            self.context_raw = None
        else:
            self.context_raw = json.dumps(value, ensure_ascii=False)


class PublicGroupActivityStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"


class PublicGroupActivity(Base):
    __tablename__ = "public_group_activities"
    __table_args__ = (
        Index("ix_public_group_activities_status", "status"),
        Index("ix_public_group_activities_start", "start_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), nullable=False)
    activity_type = Column(String(32), nullable=False)
    description = Column(Text, nullable=True)
    start_at = Column(DateTime, nullable=True)
    end_at = Column(DateTime, nullable=True)
    reward_points = Column(Integer, nullable=False, default=0)
    bonus_points = Column(Integer, nullable=False, default=0)
    highlight_slots = Column(Integer, nullable=False, default=0)
    daily_cap = Column(Integer, nullable=True)
    total_cap = Column(Integer, nullable=True)
    status = Column(Enum(PublicGroupActivityStatus), nullable=False, default=PublicGroupActivityStatus.DRAFT)
    is_highlight_enabled = Column(Boolean, nullable=False, default=False)
    config_raw = Column("config", Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def config(self) -> Dict[str, object]:
        if not self.config_raw:
            return {}
        try:
            data = json.loads(self.config_raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    @config.setter
    def config(self, value: Optional[Dict[str, object]]) -> None:
        if not value:
            self.config_raw = None
        else:
            self.config_raw = json.dumps(value, ensure_ascii=False)

    def touch(self) -> None:
        self.updated_at = datetime.utcnow()


class PublicGroupActivityLog(Base):
    __tablename__ = "public_group_activity_logs"
    __table_args__ = (
        UniqueConstraint("activity_id", "user_tg_id", name="uq_activity_user_unique"),
        Index("ix_public_group_activity_log_activity_date", "activity_id", "date_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(Integer, ForeignKey("public_group_activities.id", ondelete="CASCADE"), nullable=False)
    group_id = Column(Integer, ForeignKey("public_groups.id", ondelete="CASCADE"), nullable=True)
    user_tg_id = Column(BigInteger, nullable=True)
    event_type = Column(String(32), nullable=False)
    points = Column(Integer, nullable=False, default=0)
    date_key = Column(String(10), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PublicGroupBookmark(Base):
    __tablename__ = "public_group_bookmarks"
    __table_args__ = (
        UniqueConstraint("user_tg_id", "group_id", name="uq_public_group_bookmark"),
        Index("ix_public_group_bookmarks_user", "user_tg_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_tg_id = Column(BigInteger, nullable=False)
    group_id = Column(Integer, ForeignKey("public_groups.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PublicGroupActivityWebhook(Base):
    __tablename__ = "public_group_activity_webhooks"
    __table_args__ = (
        UniqueConstraint("activity_id", "url", name="uq_activity_webhook"),
        Index("ix_public_group_activity_webhooks_activity", "activity_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(Integer, ForeignKey("public_group_activities.id", ondelete="CASCADE"), nullable=False)
    url = Column(String(500), nullable=False)
    secret = Column(String(128), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def touch(self) -> None:
        self.updated_at = datetime.utcnow()


class PublicGroupActivityConversionLog(Base):
    __tablename__ = "public_group_activity_conversion_logs"
    __table_args__ = (
        Index("ix_public_group_activity_conv_activity_created", "activity_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(Integer, ForeignKey("public_group_activities.id", ondelete="CASCADE"), nullable=False)
    group_id = Column(Integer, ForeignKey("public_groups.id", ondelete="CASCADE"), nullable=False)
    user_tg_id = Column(BigInteger, nullable=False)
    points = Column(Integer, nullable=False, default=0)
    event_type = Column(String(32), nullable=False)
    webhook_status = Column(String(16), nullable=False, default="skipped")
    webhook_attempts = Column(Integer, nullable=False, default=0)
    webhook_successes = Column(Integer, nullable=False, default=0)
    slack_status = Column(String(16), nullable=False, default="skipped")
    context_raw = Column("context", Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def context(self) -> Dict[str, object]:
        if not self.context_raw:
            return {}
        try:
            data = json.loads(self.context_raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    @context.setter
    def context(self, value: Optional[Dict[str, object]]) -> None:
        if not value:
            self.context_raw = None
        else:
            self.context_raw = json.dumps(value, ensure_ascii=False)


class PublicGroupReportStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class PublicGroupReport(Base):
    __tablename__ = "public_group_reports"
    __table_args__ = (
        Index("ix_public_group_reports_status_created", "status", "created_at"),
        Index("ix_public_group_reports_group", "group_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("public_groups.id", ondelete="CASCADE"), nullable=False)
    reporter_tg_id = Column(BigInteger, nullable=True)
    reporter_username = Column(String(64), nullable=True)
    contact = Column(String(128), nullable=True)
    report_type = Column(String(32), nullable=False, default="general")
    description = Column(Text, nullable=True)
    status = Column(Enum(PublicGroupReportStatus), nullable=False, default=PublicGroupReportStatus.PENDING)
    assigned_operator = Column(BigInteger, nullable=True)
    priority = Column(Integer, nullable=False, default=0)
    context_raw = Column("context", Text, nullable=True)
    meta_raw = Column("metadata", Text, nullable=True)
    resolution_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)

    notes = relationship(
        "PublicGroupReportNote",
        cascade="all, delete-orphan",
        backref="report",
        lazy="selectin",
    )

    @property
    def context(self) -> Dict[str, object]:
        if not self.context_raw:
            return {}
        try:
            data = json.loads(self.context_raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    @context.setter
    def context(self, value: Optional[Dict[str, object]]) -> None:
        if not value:
            self.context_raw = None
        else:
            self.context_raw = json.dumps(value, ensure_ascii=False)

    @property
    def meta(self) -> Dict[str, object]:
        if not self.meta_raw:
            return {}
        try:
            data = json.loads(self.meta_raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    @meta.setter
    def meta(self, value: Optional[Dict[str, object]]) -> None:
        if not value:
            self.meta_raw = None
        else:
            self.meta_raw = json.dumps(value, ensure_ascii=False)


class PublicGroupReportNote(Base):
    __tablename__ = "public_group_report_notes"
    __table_args__ = (
        Index("ix_public_group_report_notes_report_created", "report_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_id = Column(Integer, ForeignKey("public_group_reports.id", ondelete="CASCADE"), nullable=False)
    operator_tg_id = Column(BigInteger, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


__all__ = [
    "PublicGroup",
    "PublicGroupMember",
    "PublicGroupRewardClaim",
    "PublicGroupEvent",
    "PublicGroupActivity",
    "PublicGroupActivityLog",
    "PublicGroupBookmark",
    "PublicGroupActivityWebhook",
    "PublicGroupActivityConversionLog",
    "PublicGroupReport",
    "PublicGroupReportNote",
    "PublicGroupReportStatus",
    "PublicGroupActivityStatus",
    "PublicGroupStatus",
]

