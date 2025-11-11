# web_admin/controllers/tags.py
from __future__ import annotations

from collections import Counter
from typing import List, Set

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Column, Integer, String, DateTime, func, desc
from sqlalchemy.orm import declarative_base

from core.i18n.i18n import t
from web_admin.deps import db_session, require_admin
from models.cover import Cover

router = APIRouter(prefix="/admin/tags", tags=["admin-tags"])
Base = declarative_base()


# --- ORM: 禁用标签表（轻量级） ---
class DisabledTag(Base):
    __tablename__ = "admin_disabled_tags"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tag = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


def _ensure_table(db):
    engine = db.get_bind()
    Base.metadata.create_all(engine)


def _get_disabled_set(db) -> Set[str]:
    rows = db.query(DisabledTag.tag).order_by(desc(DisabledTag.id)).all()
    return {t for (t,) in rows}


def _extract_tags(txt: str | None) -> List[str]:
    if not txt:
        return []
    # 你封面表里 tags 存的是用逗号拼的干净词，不折磨自己
    out = []
    for part in txt.split(","):
        tag = part.strip().lstrip("#")
        if tag:
            out.append(tag)
    return out


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def tags_page(
    req: Request,
    db=Depends(db_session),
    sess=Depends(require_admin),
    top: int = 100,
):
    _ensure_table(db)

    # 统计热门标签（只看 active 封面，免得把废片也算进去）
    q = db.query(Cover).order_by(Cover.id.desc())
    try:
        q = q.filter(Cover.active.is_(True))
    except Exception:
        pass

    cnt = Counter()
    for c in q.all():
        cnt.update(_extract_tags(getattr(c, "tags", "")))

    hot = cnt.most_common(top)
    disabled = _get_disabled_set(db)

    return req.app.state.templates.TemplateResponse(
        "tags.html",
        {
            "request": req,
            "title": t("admin.tags.title"),
            "nav_active": "tags",
            "hot": hot,                 # [(tag, count)]
            "disabled": disabled,       # set()
        },
    )


@router.post("/disable", response_class=RedirectResponse)
def tag_disable(
    req: Request,
    tag: str = Form(...),
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    _ensure_table(db)
    tag = tag.strip().lstrip("#")
    if not tag:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))
    exists = db.query(DisabledTag).filter(DisabledTag.tag == tag).first()
    if not exists:
        db.add(DisabledTag(tag=tag))
        db.commit()
    return RedirectResponse("/admin/tags", status_code=303)


@router.post("/enable", response_class=RedirectResponse)
def tag_enable(
    req: Request,
    tag: str = Form(...),
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    _ensure_table(db)
    tag = tag.strip().lstrip("#")
    if not tag:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))
    db.query(DisabledTag).filter(DisabledTag.tag == tag).delete()
    db.commit()
    return RedirectResponse("/admin/tags", status_code=303)
