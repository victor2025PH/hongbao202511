# -*- coding: utf-8 -*-
"""
红包封面素材库模型与操作方法（兼容后台控制器）
- 保留你原有的增强版字段与查询能力（频道定位、file_id 兜底、tags/slug、热迁移等）
- 新增兼容函数：
    * toggle_cover_active(cover_id, to: bool|None=None) -> bool
    * list_covers(page, page_size, active: bool|None=None, q: str|None=None)
      ↑ 这是控制器常用签名；内部转调本文件的高级查询。
- 同时保留：
    * add_cover(...) / delete_cover(...) / set_cover_enabled(...) / update_cover_meta(...)
    * list_covers_simple(...) / get_cover_by_id(...) / get_cover(...) 别名

用法对齐 web_admin/controllers/covers.py：
from models.cover import Cover, add_cover, delete_cover, list_covers, toggle_cover_active
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional, Iterable, Tuple

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    DateTime,
    Text,
    UniqueConstraint,
    Index,
    desc,
    func,
    text,
    or_,
)

from .db import Base, get_session


class Cover(Base):
    """
    素材频道封面缓存表

    设计要点：
    - 频道内的每一条素材消息（通常是图片/动画）在这里登记为一条封面记录；
    - 优先用 (channel_id, message_id) 唯一标识原始素材，确保可通过 copyMessage 复用；
    - file_id 作为兜底（当 copyMessage 不可用时可以 sendPhoto/Animation/Video）；
    - media_type 标识兜底媒体类型：photo / animation / video；
    - slug 用于在 UI 上做简短标识（如 #summer2025），可为空；
    - enabled 用于快速上下架某条封面，不必删除记录；
    - creator_tg_id 记录管理员或操作者，便于审计；
    - created_at / updated_at 记录时间线。
    """
    __tablename__ = "covers"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 原始素材定位（用于优先 copyMessage）
    channel_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)

    # 兜底：媒体 file_id（当无法 copyMessage 时回退 sendPhoto/Animation/Video）
    file_id = Column(String(256), nullable=True)

    # 媒体类型：photo / animation / video（兜底发送时决定 send* API）
    media_type = Column(String(16), nullable=True)

    # 展示/检索用的元信息（可选）
    slug = Column(String(64), nullable=True, index=True)   # 简短标签，如 "summer2025"
    title = Column(Text, nullable=True)                    # 来自 caption 的文本等
    tags = Column(Text, nullable=True)                     # 逗号分隔或 JSON 字符串（简单起见 Text）

    enabled = Column(Boolean, nullable=False, default=True)

    # 操作者 & 时间
    creator_tg_id = Column(BigInteger, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("channel_id", "message_id", name="uq_cover_channel_msg"),
        Index("idx_cover_created", "created_at"),
    )

    # ----------- 常用属性访问（安全兜底） -----------
    @property
    def safe_slug(self) -> str:
        return (self.slug or "").strip()

    @property
    def has_file(self) -> bool:
        return bool(self.file_id and self.file_id.strip())

    # === 新增：可读标题 & 预览 URL（供前端展示） ===
    @property
    def display_title(self) -> str:
        """
        给前端用的展示标题：
        - 优先 title（去首尾空白）
        - 其次 slug
        - 否则用 “#<id>”
        """
        t = (self.title or "").strip()
        if t:
            return t
        s = (self.slug or "").strip()
        if s:
            return s
        return f"#{self.id}"

    @property
    def preview_url(self) -> str:
        """
        供前端 <img src="..."> 使用的预览地址。
        由控制器提供 /admin/covers/{id}/preview 路由来实际输出图片字节。
        """
        return f"/admin/covers/{int(self.id)}/preview"

    @property
    def as_copy_source(self) -> Tuple[int, int]:
        """返回可用于 copyMessage 的 (channel_id, message_id)"""
        return int(self.channel_id), int(self.message_id)

    def to_dict(self) -> dict:
        # 增补 display_title / preview_url 字段
        return {
            "id": self.id,
            "channel_id": int(self.channel_id),
            "message_id": int(self.message_id),
            "file_id": self.file_id,
            "media_type": self.media_type,
            "slug": self.slug,
            "title": self.title,
            "tags": self.tags,
            "enabled": bool(self.enabled),
            "creator_tg_id": int(self.creator_tg_id) if self.creator_tg_id is not None else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "display_title": self.display_title,
            "preview_url": self.preview_url,
        }

    def __repr__(self) -> str:
        return (
            f"<Cover id={self.id} ch={self.channel_id} msg={self.message_id} "
            f"slug={self.slug!r} media={self.media_type!r} enabled={self.enabled}>"
        )


# =====================（可选）热迁移：在 SQLite 上补齐缺失列 =====================

def ensure_cover_schema() -> None:
    """
    在 SQLite 上为 covers 表补齐缺失列（热迁移），可安全反复调用。
    在应用启动、Base.metadata.create_all() 之后调用一次即可。
    不在此处自动调用，避免与你的初始化时序耦合。
    """
    # 新增热迁移兜底，防旧库缺列报错
    with get_session() as s:
        bind = s.get_bind()
        if not bind:
            return
        if bind.dialect.name != "sqlite":
            # 其它方言通常建议使用 Alembic 正式迁移
            return

        # 查询当前列
        cols = s.execute(text("PRAGMA table_info(covers)")).fetchall()
        existing = {c[1] for c in cols}  # 第二列是列名

        # 逐列补齐
        if "creator_tg_id" not in existing:
            s.execute(text("ALTER TABLE covers ADD COLUMN creator_tg_id BIGINT;"))
        if "created_at" not in existing:
            s.execute(text("ALTER TABLE covers ADD COLUMN created_at DATETIME;"))
        if "updated_at" not in existing:
            s.execute(text("ALTER TABLE covers ADD COLUMN updated_at DATETIME;"))
        if "media_type" not in existing:
            s.execute(text("ALTER TABLE covers ADD COLUMN media_type VARCHAR(16);"))
        s.commit()


# ===================== 内部小工具 =====================

def _extract_tags(text: str) -> List[str]:
    """从 title 中提取 #tag，和手工 tags 合并时会用到。"""
    if not text:
        return []
    found = re.findall(r"#([\w\u4e00-\u9fff]+)", text, flags=re.IGNORECASE)
    return [t.strip().lower() for t in found if t.strip()]


def _norm_tags(tags: Optional[str | List[str]], title: Optional[str]) -> str:
    """把传入 tags 与 title 中的 #tag 归一化合并为小写、逗号分隔字符串。"""
    pool: List[str] = []
    if isinstance(tags, list):
        pool.extend(tags)
    elif isinstance(tags, str):
        pool.extend([x.strip() for x in tags.split(",") if x.strip()])
    pool.extend(_extract_tags(title or ""))

    seen = set()
    uniq: List[str] = []
    for x in pool:
        k = x.strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(k)
    return ",".join(uniq)


# ===================== 查询接口（核心版 + 兼容包装） =====================

def list_covers_core(
    page: int = 1,
    page_size: int = 6,
    *,
    only_enabled: bool = True,
    search: Optional[str] = None,
) -> Tuple[List[Cover], int]:
    """
    分页列出封面，按创建时间倒序。
    返回：(rows, total)
    - rows: 当前页数据
    - total: 满足筛选条件的总数（用于分页）
    """
    p = max(1, int(page))
    sz = max(1, int(page_size))
    with get_session() as s:
        q = s.query(Cover)
        if only_enabled:
            q = q.filter(Cover.enabled.is_(True))
        if search:
            kw = f"%{search.strip()}%"
            q = q.filter(
                or_(
                    Cover.slug.ilike(kw),
                    Cover.title.ilike(kw),
                    Cover.tags.ilike(kw),
                )
            )

        total = q.order_by(None).with_entities(func.count(Cover.id)).scalar()
        rows = (
            q.order_by(desc(Cover.created_at), desc(Cover.id))
             .offset((p - 1) * sz)
             .limit(sz)
             .all()
        )
        s.expunge_all()
        return list(rows), int(total or 0)


def list_covers_simple(
    page: int = 1,
    page_size: int = 10,
) -> Tuple[List[Tuple[int, str, Optional[str]]], int]:
    """轻量列表：返回 (id, slug, title)，便于在按钮上展示。"""
    rows, total = list_covers_core(page, page_size, only_enabled=True)
    data = [(r.id, r.safe_slug, (r.title or "")) for r in rows]
    return data, total


def count_covers(*, only_enabled: bool = True) -> int:
    with get_session() as s:
        q = s.query(func.count(Cover.id))
        if only_enabled:
            q = q.filter(Cover.enabled.is_(True))
        return int(q.scalar() or 0)


def get_cover_by_id(cover_id: int) -> Optional[Cover]:
    """根据主键读取单条封面（包含禁用的）。"""
    with get_session() as s:
        row = s.query(Cover).filter(Cover.id == int(cover_id)).first()
        if row:
            s.expunge(row)
        return row


# 兼容旧调用名
def get_cover(cover_id: int) -> Optional[Cover]:
    return get_cover_by_id(cover_id)


# ===================== 写入/维护接口 =====================

def ensure_unique_slug(slug: str) -> str:
    """简单 slug 去重：若已存在同名，则在后缀追加递增数字。"""
    base = (slug or "").strip()
    if not base:
        return base
    with get_session() as s:
        i = 0
        cur = base
        while True:
            exists = s.query(Cover.id).filter(Cover.slug == cur).first()
            if not exists:
                return cur
            i += 1
            cur = f"{base}-{i}"


def upsert_from_channel_post(
    *,
    channel_id: int,
    message_id: int,
    file_id: Optional[str] = None,
    media_type: Optional[str] = None,
    slug: Optional[str] = None,
    title: Optional[str] = None,
    tags: Optional[str] = None,
    enabled: bool = True,
    creator_tg_id: Optional[int] = None,
) -> Cover:
    """
    将素材频道的一条消息登记/更新为封面。
    - 根据 (channel_id, message_id) 唯一约束做“存在则更新，不存在则创建”。
    - 若提供 slug，则确保唯一。
    - media_type 建议：photo/animation/video
    """
    with get_session() as s:
        row = (
            s.query(Cover)
            .filter(Cover.channel_id == int(channel_id), Cover.message_id == int(message_id))
            .first()
        )
        if row is None:
            row = Cover(
                channel_id=int(channel_id),
                message_id=int(message_id),
                file_id=file_id or None,
                media_type=(media_type or None),
                slug=ensure_unique_slug(slug) if slug else None,
                title=(title or None),
                tags=(tags or None),
                enabled=bool(enabled),
                creator_tg_id=int(creator_tg_id) if creator_tg_id is not None else None,
            )
            s.add(row)
        else:
            # 增量更新
            if file_id is not None:
                row.file_id = file_id or None
            if media_type is not None:
                row.media_type = media_type or None
            if slug is not None:
                row.slug = ensure_unique_slug(slug) if slug else None
            if title is not None:
                row.title = title
            if tags is not None:
                row.tags = tags
            row.enabled = bool(enabled)
            if creator_tg_id is not None and not row.creator_tg_id:
                row.creator_tg_id = int(creator_tg_id)
        s.commit()
        s.refresh(row)
        s.expunge(row)
        return row


def add_cover(
    *,
    channel_id: int,
    message_id: int,
    file_id: Optional[str] = None,
    media_type: Optional[str] = None,
    slug: Optional[str] = None,
    title: Optional[str] = None,
    tags: Optional[str] = None,
    creator_tg_id: Optional[int] = None,
) -> Cover:
    """
    管理员直接新增一条封面素材。
    - 会对 slug 做唯一化处理（若提供）
    - media_type 建议：photo/animation/video
    """
    with get_session() as s:
        row = Cover(
            channel_id=int(channel_id),
            message_id=int(message_id),
            file_id=file_id or None,
            media_type=(media_type or None),
            slug=ensure_unique_slug(slug) if slug else None,
            title=(title or None),
            tags=(tags or None),
            enabled=True,
            creator_tg_id=int(creator_tg_id) if creator_tg_id is not None else None,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        s.expunge(row)
        return row


def update_cover_meta(
    cover_id: int,
    *,
    slug: Optional[str] = None,
    title: Optional[str] = None,
    tags: Optional[str] = None,
    file_id: Optional[str] = None,
    media_type: Optional[str] = None,
) -> Optional[Cover]:
    """更新封面的元信息（可选参数不为 None 时才更新）。"""
    with get_session() as s:
        row = s.query(Cover).filter(Cover.id == int(cover_id)).first()
        if not row:
            return None
        if slug is not None:
            row.slug = ensure_unique_slug(slug) if slug else None
        if title is not None:
            row.title = title
        if tags is not None:
            row.tags = tags
        if file_id is not None:
            row.file_id = file_id or None
        if media_type is not None:
            row.media_type = media_type or None
        s.commit()
        s.refresh(row)
        s.expunge(row)
        return row


def set_cover_enabled(cover_id: int, enabled: bool) -> bool:
    """上/下架单条封面。"""
    with get_session() as s:
        row = s.query(Cover).filter(Cover.id == int(cover_id)).first()
        if not row:
            return False
        row.enabled = bool(enabled)
        s.commit()
        return True


def bulk_disable(cover_ids: Iterable[int]) -> int:
    """批量下架指定封面，返回受影响行数。"""
    ids = [int(i) for i in cover_ids] if cover_ids else []
    if not ids:
        return 0
    with get_session() as s:
        q = s.query(Cover).filter(Cover.id.in_(ids))
        count = 0
        for row in q.all():
            if row.enabled:
                row.enabled = False
                s.add(row)
                count += 1
        s.commit()
        return count


def delete_cover(cover_id: int) -> bool:
    """
    管理员删除一条封面素材。
    注意：若希望保留历史痕迹，建议用 bulk_disable/软删除；此函数为“物理删除”。
    """
    with get_session() as s:
        row = s.query(Cover).filter(Cover.id == int(cover_id)).first()
        if not row:
            return False
        s.delete(row)
        s.commit()
        return True


# ===================== 兼容控制器的包装层 =====================

def toggle_cover_active(cover_id: int, to: Optional[bool] = None) -> bool:
    """
    兼容控制器的 API：
    - to=None 时，取反
    - to=True/False 时，显式设置
    """
    with get_session() as s:
        row = s.query(Cover).filter(Cover.id == int(cover_id)).first()
        if not row:
            return False
        row.enabled = (not row.enabled) if to is None else bool(to)
        s.commit()
        return True


def list_covers_compat(
    page: int = 1,
    page_size: int = 24,
    *,
    active: Optional[bool] = None,
    q: Optional[str] = None,
) -> Tuple[List[Cover], int]:
    """
    兼容控制器签名的包装：
      - active=None   -> 不限
      - active=True   -> 仅启用
      - active=False  -> 仅禁用
      - q -> search
    """
    # 修正签名 & 转调核心实现，解决 only_enabled 形参不匹配
    if active is None:
        rows, total = list_covers_core(page, page_size, only_enabled=False, search=q)
    elif active is True:
        rows, total = list_covers_core(page, page_size, only_enabled=True, search=q)
    else:
        # 仅禁用：先全量取，再在内存中过滤（量大时可再拆 SQL 分支）
        rows_all, total_all = list_covers_core(page, page_size, only_enabled=False, search=q)
        rows = [r for r in rows_all if not r.enabled]
        total = total_all
    return rows, total


# ---- 对外导出（控制器调用） ----
def list_covers(page: int = 1, page_size: int = 24, active: Optional[bool] = None, q: Optional[str] = None):
    """
    对外统一导出：兼容控制器调用的签名。
    """
    # 统一入口
    return list_covers_compat(page, page_size, active=active, q=q)

# 读起来更直观的别名（等价）
list_covers_for_admin = list_covers
