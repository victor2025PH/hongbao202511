# web_admin/controllers/invites.py
from __future__ import annotations

import math
from typing import Optional
from sqlalchemy.orm import aliased

from fastapi import APIRouter, Depends, Request, Query, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func, desc, or_

from core.i18n.i18n import t
from web_admin.deps import db_session, require_admin
from models.user import User
from sqlalchemy import cast, String

import importlib

def _load_invite_model():
    """
    动态加载 Invite 模型，兼容多种项目命名与路径。
    返回 ORM 模型类，例如 models.invite.Invite / models.invite.Invitation / models.referral.Referral
    """
    candidates = [
        ("models.invite", "Invite"),
        ("models.invite", "Invitation"),
        ("models.invite_model", "Invite"),
        ("models.referral", "Referral"),
    ]
    last_err = None
    for mod, cls in candidates:
        try:
            m = importlib.import_module(mod)
            if hasattr(m, cls):
                return getattr(m, cls)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(
        "Invite 模型未找到。请确认存在以下其一："
        "models.invite.Invite / models.invite.Invitation / models.invite_model.Invite / models.referral.Referral"
        + (f"；最后导入错误：{last_err}" if last_err else "")
    )

def _resolve_columns(InviteModel):
    """
    从 Invite 模型上解析关键列，支持多别名：
      - INVITER: inviter_id / referrer_id / from_id
      - INVITEE: invitee_id / user_id / to_id
      - CREATED: created_at / created / ts
      - REWARD : reward_amount / reward / bonus （可选）
    """
    def _col(model, *names):
        for n in names:
            if hasattr(model, n):
                return getattr(model, n)
        return None

    INVITER = _col(InviteModel, "inviter_id", "referrer_id", "from_id")
    INVITEE = _col(InviteModel, "invitee_id", "user_id", "to_id")
    CREATED = _col(InviteModel, "created_at", "created", "ts")
    REWARD  = _col(InviteModel, "reward_amount", "reward", "bonus")

    if not all([INVITER, INVITEE, CREATED]):
        raise RuntimeError("Invite 模型缺少关键字段：需要 inviter_id/referrer_id/from_id、invitee_id/user_id/to_id、created_at/created/ts 之一")

    return INVITER, INVITEE, CREATED, REWARD


router = APIRouter(prefix="/admin/invites", tags=["admin-invites"])

PAGE_SIZE = 30


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def invites_page(
    req: Request,
    db=Depends(db_session),
    sess=Depends(require_admin),
    page: int = 1,
    inviter: Optional[str] = Query(None, description="tg_id or @username"),
    invitee: Optional[str] = Query(None, description="tg_id or @username"),
    start: Optional[str] = None,   # ISO 日期
    end: Optional[str] = None,     # ISO 日期
    q: Optional[str] = None,       # 模糊查：任一侧 id/用户名
):
    # 运行时加载 Invite 模型与关键列
    Invite = _load_invite_model()
    INVITER, INVITEE, CREATED, REWARD = _resolve_columns(Invite)

    # 基础联结
    qbase = (
        db.query(Invite, 
                 func.coalesce(REWARD, 0).label("reward"),
                 User)  # 右侧统一用“被邀请人”的User
        .join(User, User.tg_id == INVITEE)
        .order_by(desc(CREATED))
    )

    # 过滤：inviter / invitee by id or @username
    def _apply_user_filter(qry, side: str, token: Optional[str]):
        if not token:
            return qry
        token = token.strip()
        if token.startswith("@"):
            uname = token.lstrip("@").lower()
            if side == "inviter":
                # 需要联结邀请人与用户表各一次；偷个懒：子查询
                sub = db.query(User.tg_id).filter(
                    or_(User.username == uname, User.username == uname.lower(), User.username == uname.upper())
                ).subquery()
                return qry.filter(INVITER.in_(db.query(sub.c.tg_id)))
            else:
                return qry.filter(or_(
                    User.username == uname,
                    User.username == uname.lower(),
                    User.username == uname.upper(),
                ))
        else:
            try:
                uid = int(token)
                return qry.filter((INVITER == uid) if side == "inviter" else (INVITEE == uid))
            except ValueError:
                return qry

    qbase = _apply_user_filter(qbase, "inviter", inviter)
    qbase = _apply_user_filter(qbase, "invitee", invitee)

    # 时间
    import datetime as dt
    def _dt(s: Optional[str]):
        if not s:
            return None
        try:
            if len(s) == 10:
                return dt.datetime.fromisoformat(s)
            return dt.datetime.fromisoformat(s)
        except Exception:
            return None
    sdt = _dt(start)
    edt = _dt(end)
    if sdt:
        qbase = qbase.filter(CREATED >= sdt)
    if edt:
        # 给日粒度收尾加一天
        qbase = qbase.filter(CREATED < (edt + dt.timedelta(days=1) if len(end or "") == 10 else edt))

    # 模糊查（两侧）
    if q:
        like = f"%{q.strip()}%"
        # 左侧 inviter 需要额外联结一次 User 表，别把主联结污染了
        U2 = aliased(User)
        qbase = qbase.join(U2, U2.tg_id == INVITER).filter(
            or_(
                User.username.ilike(like),
                U2.username.ilike(like),
                cast(User.tg_id, String).ilike(like),
                cast(U2.tg_id, String).ilike(like),
            )
        )

    total = qbase.count()
    rows = qbase.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    # 汇总：总邀请数、总奖励
    total_invites = db.query(func.count(1)).select_from(Invite).scalar() or 0
    total_reward = db.query(func.coalesce(func.sum(REWARD), 0)).select_from(Invite).scalar() if REWARD is not None else 0

    return req.app.state.templates.TemplateResponse(
        "invites.html",
        {
            "request": req,
            "title": t("admin.nav.invites"),
            "nav_active": "invites",
            "rows": rows,  # (Invite, reward, InviteeUser)
            "page": page,
            "total_pages": total_pages,
            "filters": {
                "inviter": inviter or "",
                "invitee": invitee or "",
                "start": start or "",
                "end": end or "",
                "q": q or "",
            },
            "stat": {
                "total": int(total_invites),
                "reward": total_reward or 0,
            }
        },
    )

@router.get("/top", response_class=HTMLResponse)
def invites_top(
    req: Request,
    db=Depends(db_session),
    sess=Depends(require_admin),
    limit: int = 50,
):
    # 运行时加载 Invite 模型与关键列
    Invite = _load_invite_model()
    INVITER, INVITEE, CREATED, REWARD = _resolve_columns(Invite)

    # 榜单：按邀请人数倒序
    agg = (
        db.query(INVITER.label("inviter_id"), func.count(1).label("cnt"), func.coalesce(func.sum(REWARD), 0).label("reward"))
        .group_by(INVITER)
        .order_by(desc("cnt"))
        .limit(limit)
        .all()
    )
    # 拉用户名
    inviter_ids = [row.inviter_id for row in agg]
    users = {u.tg_id: u for u in db.query(User).filter(User.tg_id.in_(inviter_ids)).all()}
    return req.app.state.templates.TemplateResponse(
        "invites_top.html",
        {
            "request": req,
            "title": t("admin.nav.invites"),
            "nav_active": "invites",
            "rows": agg,
            "users": users,
        },
    )
