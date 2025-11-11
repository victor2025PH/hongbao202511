# models/invite.py
# -*- coding: utf-8 -*-
"""
邀请关系与进度：
- InviteRelation: 记录邀请者与被邀请者关系
- InviteProgress: 记录邀请者当前进度（百分比/人数/奖励）

接口：
  - add_invite(inviter_id, invitee_id) -> bool
  - get_progress(inviter_id) -> dict
  - update_progress(inviter_id, delta_points=0, delta_energy=0) -> None
  - list_invitees(inviter_id) -> List[int]
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Dict

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    DateTime,
    UniqueConstraint,
)
from sqlalchemy.exc import IntegrityError
# 兼容层需要：把常量/字面量映射成 ORM 属性
from sqlalchemy.orm import column_property
from sqlalchemy import literal

# 与全工程对齐：统一走 models.db
from models.db import Base, get_session

# feature_flags 路径兼容（优先根目录 feature_flags.py）
try:
    from feature_flags import flags  # 项目主路径
except Exception:  # pragma: no cover
    from config.feature_flags import flags  # 兼容旧路径/备用


class InviteRelation(Base):
    __tablename__ = "invite_relations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    inviter_tg_id = Column(BigInteger, nullable=False, index=True)
    invitee_tg_id = Column(BigInteger, nullable=False, index=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        # 防止同一个受邀者被多名邀请者重复记录（与 invitee_tg_id 唯一约束互为补充）
        UniqueConstraint("inviter_tg_id", "invitee_tg_id", name="uq_inviter_invitee"),
    )

    def __repr__(self) -> str:
        return f"<InviteRelation inviter={self.inviter_tg_id} invitee={self.invitee_tg_id}>"


class InviteProgress(Base):
    __tablename__ = "invite_progress"

    id = Column(Integer, primary_key=True, autoincrement=True)
    inviter_tg_id = Column(BigInteger, nullable=False, unique=True, index=True)

    invited_count = Column(Integer, default=0)        # 累计邀请人数
    progress_percent = Column(Integer, default=0)     # 当前进度（0-100）
    points_earned = Column(Integer, default=0)        # 通过活动累计获得的积分
    energy_earned = Column(Integer, default=0)        # 通过活动累计获得的能量

    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<InviteProgress inviter={self.inviter_tg_id} progress={self.progress_percent}%>"


# ========== 工具函数 ==========

def _clamp_percent(v: int) -> int:
    try:
        iv = int(v)
    except Exception:
        iv = 0
    if iv < 0:
        return 0
    if iv > 100:
        return 100
    return iv


def add_invite(inviter_id: int, invitee_id: int) -> bool:
    """
    新增一条邀请关系，并推进邀请者进度。
    - 若 invitee 已被邀请过（无论由谁），返回 False
    - 成功返回 True
    - 内部自动提交（commit）
    """
    inviter_id = int(inviter_id)
    invitee_id = int(invitee_id)

    with get_session() as s:
        # 受邀者是否已被任何人邀请过（全局唯一）
        exists = s.query(InviteRelation).filter_by(invitee_tg_id=invitee_id).first()
        if exists:
            return False

        try:
            rel = InviteRelation(
                inviter_tg_id=inviter_id,
                invitee_tg_id=invitee_id,
            )
            s.add(rel)

            # 取/建进度对象
            prog = s.query(InviteProgress).filter_by(inviter_tg_id=inviter_id).first()
            if not prog:
                prog = InviteProgress(inviter_tg_id=inviter_id)
                s.add(prog)

            # 更新统计：人数 +1
            prog.invited_count = int(prog.invited_count or 0) + 1

            # 每邀请 1 人，进度 +X（默认 1%；若配置 <1，则至少 +1，若 <=0 则 +0）
            step_cfg = float(getattr(flags, "INVITE_PROGRESS_PER_PERSON", 1.0) or 1.0)
            step = int(step_cfg) if step_cfg >= 1.0 else (1 if step_cfg > 0 else 0)
            prog.progress_percent = _clamp_percent(int(prog.progress_percent or 0) + step)
            prog.updated_at = datetime.utcnow()

            # 显式提交
            s.commit()
            return True

        except IntegrityError:
            # 并发下可能命中唯一约束（invitee_tg_id 唯一），按“已存在”处理
            s.rollback()
            return False


def get_progress(inviter_id: int) -> Dict:
    """
    获取邀请进度数据（若无记录则返回默认 0）
    """
    inviter_id = int(inviter_id)
    with get_session() as s:
        prog = s.query(InviteProgress).filter_by(inviter_tg_id=inviter_id).first()
        if not prog:
            return {
                "invited_count": 0,
                "progress_percent": 0,
                "points_earned": 0,
                "energy_earned": 0,
            }
        return {
                "invited_count": int(prog.invited_count or 0),
                "progress_percent": _clamp_percent(int(prog.progress_percent or 0)),
                "points_earned": int(prog.points_earned or 0),
                "energy_earned": int(prog.energy_earned or 0),
            }


def update_progress(inviter_id: int, delta_points: int = 0, delta_energy: int = 0) -> None:
    """
    手动累加积分或能量统计（常用于“跨阈值奖励能量”等场景）
    - 内部自动 upsert 并提交
    """
    inviter_id = int(inviter_id)
    delta_points = int(delta_points or 0)
    delta_energy = int(delta_energy or 0)

    with get_session() as s:
        prog = s.query(InviteProgress).filter_by(inviter_tg_id=inviter_id).first()
        if not prog:
            prog = InviteProgress(inviter_tg_id=inviter_id)
            s.add(prog)

        prog.points_earned = int(prog.points_earned or 0) + delta_points
        prog.energy_earned = int(prog.energy_earned or 0) + delta_energy
        prog.updated_at = datetime.utcnow()

        s.add(prog)
        s.commit()


def list_invitees(inviter_id: int) -> List[int]:
    """
    返回该 inviter 的所有被邀请者 tg_id 列表
    """
    inviter_id = int(inviter_id)
    with get_session() as s:
        rows = s.query(InviteRelation).filter_by(inviter_tg_id=inviter_id).all()
        return [int(r.invitee_tg_id) for r in rows]


# ========== 兼容层（供 web_admin.controllers.invites 静态导入） ==========
# 目的：提供一个名为 Invite 的 ORM 类，字段名与控制器预期一致（inviter_id/invitee_id/created_at）
# 做法：映射到与 InviteRelation 相同的表，并用 column_property 取别名
class Invite(Base):
    __table__ = InviteRelation.__table__  # 复用同一张表（invite_relations）

    # 将表中现有列取别名为控制器所需的字段名
    inviter_id = column_property(InviteRelation.__table__.c.inviter_tg_id)
    invitee_id = column_property(InviteRelation.__table__.c.invitee_tg_id)
    created_at = column_property(InviteRelation.__table__.c.created_at)

    # 某些统计页面可能会 select 一个“奖励”字段；如果你的表没有对应列，给出一个 0 的占位符
    reward_amount = column_property(literal(0).label("reward_amount"))

    def __repr__(self) -> str:
        return f"<Invite inviter_id={self.inviter_id} invitee_id={self.invitee_id}>"
