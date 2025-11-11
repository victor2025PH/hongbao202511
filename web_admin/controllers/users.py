from __future__ import annotations
print("[users] loaded from:", __file__)

from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List

import csv
import io
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from web_admin.deps import require_admin, csrf_protect, issue_csrf  # CSRF 注入/校验
from web_admin.constants import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX  # 维持：上限常量

# 统一从 models 命名空间导入
from models.db import get_db
from models.user import User
from models.ledger import Ledger

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


# ---------- 工具函数 ----------

def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    # 支持 "2025-01-01" / "2025-01-01 12:00:00"
    fmts = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except Exception:
            continue
    return None


def _parse_user_ref(s: str) -> Tuple[str, str]:
    """
    返回 (field, value)
    - '123456' -> ('tg_id', '123456')
    - '@name'  -> ('username', 'name')
    """
    s = s.strip()
    if not s:
        return ("", "")
    if s.startswith("@"):
        return ("username", s[1:])
    # 纯数字按 tg_id 匹配
    if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
        return ("tg_id", s)
    # 兜底：也按 username 试试
    return ("username", s)


def _paginate(page: int, page_size: int = PAGE_SIZE_DEFAULT) -> Tuple[int, int]:
    page = max(1, int(page or 1))
    limit = page_size
    offset = (page - 1) * page_size
    return limit, offset


def _build_users_query(
    db: Session,
    *,
    q: str,
    token: str,
    min_b: str,
    max_b: str,
    active_since: str,
    created_start: str,
    created_end: str,
    invited_by: str,
):
    """
    生成用户查询语句与“创建时间列”集合（用于排序），供列表/导出共用。
    """
    stmt = select(User)
    where = []

    # q: id or @username
    if q:
        f, val = _parse_user_ref(q)
        if f == "tg_id":
            where.append(User.tg_id == int(val))
        elif f == "username":
            where.append(User.username.ilike(f"%{val}%"))
        else:
            where.append(or_(User.username.ilike(f"%{q}%"), User.tg_id == q))

    # 最近活跃（动态列）
    dt = _parse_date(active_since)
    if dt:
        active_cols = [
            c for c in (
                getattr(User, "last_active_at", None),
                getattr(User, "active_at", None),
            ) if c is not None
        ]
        if active_cols:
            where.append(or_(*[col >= dt for col in active_cols]))

    # 注册区间（动态列）
    cs = _parse_date(created_start)
    ce = _parse_date(created_end)
    created_cols = [
        c for c in (
            getattr(User, "created_at", None),
            getattr(User, "created", None),
        ) if c is not None
    ]
    if cs and created_cols:
        where.append(or_(*[col >= cs for col in created_cols]))
    if ce and created_cols:
        ce2 = ce + timedelta(days=1)
        where.append(or_(*[col < ce2 for col in created_cols]))

    # 邀请人
    if invited_by:
        f, val = _parse_user_ref(invited_by)
        if f == "tg_id":
            where.append(User.invited_by == int(val))
        elif f == "username":
            inviter_q = select(User.tg_id).where(User.username.ilike(f"%{val}%"))
            inviter_ids = [row[0] for row in db.execute(inviter_q).all()]
            if inviter_ids:
                where.append(User.invited_by.in_(inviter_ids))
            else:
                # 明确置空结果
                where.append(User.invited_by == -99999999)

    # 余额范围：只有当 token 与 min/max 至少一个存在时才执行
    if token and (min_b or max_b):
        sub = (
            select(
                Ledger.user_id.label("uid"),
                func.coalesce(func.sum(Ledger.amount), 0).label("bal"),
            )
            .where(Ledger.token == token)
            .group_by(Ledger.user_id)
            .subquery()
        )
        stmt = stmt.join(sub, sub.c.uid == User.tg_id, isouter=True)
        if min_b:
            try:
                where.append(func.coalesce(sub.c.bal, 0) >= float(min_b))
            except Exception:
                pass
        if max_b:
            try:
                where.append(func.coalesce(sub.c.bal, 0) <= float(max_b))
            except Exception:
                pass

    if where:
        stmt = stmt.where(and_(*where))

    # 排序：按创建时间倒序（动态列兜底）
    if created_cols:
        stmt = stmt.order_by(desc(func.coalesce(*created_cols)))
    else:
        stmt = stmt.order_by(desc(func.now()))

    return stmt, created_cols


# ---------- 列表页 ----------

@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def users_list(
    request: Request,
    q: str = Query("", description="用户ID 或 @用户名"),
    token: str = Query("", description="余额筛选币种，如 USDT/TON/POINT"),
    min_b: str = Query("", description="最小余额"),
    max_b: str = Query("", description="最大余额"),
    active_since: str = Query("", description="最近活跃起始 2025-01-01"),
    created_start: str = Query("", description="注册起始"),
    created_end: str = Query("", description="注册结束"),
    invited_by: str = Query("", description="邀请人ID或@用户名"),
    page: int = Query(1, ge=1),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
    db: Session = Depends(get_db),
):
    """
    用户列表，支持模糊筛选与分页。
    余额范围筛选：仅当传入 token + min/max 时，通过 Ledger 聚合子查询过滤。
    """
    # 复用统一构建
    stmt, created_cols = _build_users_query(
        db,
        q=q, token=token, min_b=min_b, max_b=max_b,
        active_since=active_since, created_start=created_start,
        created_end=created_end, invited_by=invited_by,
    )

    # 统计总数
    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    # 分页
    limit, offset = _paginate(page, page_size)
    stmt_paged = stmt.limit(limit).offset(offset)

    rows = [r[0] for r in db.execute(stmt_paged).all()]

    # 模板需要的筛选回显
    filters = {
        "q": q or "",
        "token": token or "",
        "min_b": min_b or "",
        "max_b": max_b or "",
        "active_since": active_since or "",
        "created_start": created_start or "",
        "created_end": created_end or "",
        "invited_by": invited_by or "",
    }

    # 可选币种列表，和账本保持一致
    tokens = ["USDT", "TON", "POINT"]
    total_pages = (total + page_size - 1) // page_size

    # ✅ 生成 CSRF，供列表模板里的行内操作表单使用（封禁/改角色情况下）
    csrf_token = issue_csrf(request)

    return request.app.state.templates.TemplateResponse(
        "users_list.html",
        {
            "request": request,
            "rows": rows,
            "filters": filters,
            "tokens": tokens,
            "page": page,
            "total_pages": total_pages,
            "csrf_token": csrf_token,
        },
    )


# ---------- 导出：CSV / JSON（沿用当前筛选） ----------

@router.get("/export.csv", dependencies=[Depends(require_admin)])
def users_export_csv(
    request: Request,
    q: str = Query("", description="用户ID 或 @用户名"),
    token: str = Query("", description="余额筛选币种，如 USDT/TON/POINT"),
    min_b: str = Query("", description="最小余额"),
    max_b: str = Query("", description="最大余额"),
    active_since: str = Query("", description="最近活跃起始 2025-01-01"),
    created_start: str = Query("", description="注册起始"),
    created_end: str = Query("", description="注册结束"),
    invited_by: str = Query("", description="邀请人ID或@用户名"),
    db: Session = Depends(get_db),
):
    stmt, _ = _build_users_query(
        db,
        q=q, token=token, min_b=min_b, max_b=max_b,
        active_since=active_since, created_start=created_start,
        created_end=created_end, invited_by=invited_by,
    )
    rows: List[User] = [r[0] for r in db.execute(stmt).all()]

    # 组织 CSV
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["tg_id", "username", "invited_by", "created_at", "last_active_at"])
    for u in rows:
        created_at = getattr(u, "created_at", getattr(u, "created", None))
        last_active_at = getattr(u, "last_active_at", getattr(u, "active_at", None))
        writer.writerow([
            getattr(u, "tg_id", ""),
            getattr(u, "username", ""),
            getattr(u, "invited_by", ""),
            created_at or "",
            last_active_at or "",
        ])
    buf.seek(0)
    filename = "users_export.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export.json", dependencies=[Depends(require_admin)])
def users_export_json(
    request: Request,
    q: str = Query("", description="用户ID 或 @用户名"),
    token: str = Query("", description="余额筛选币种，如 USDT/TON/POINT"),
    min_b: str = Query("", description="最小余额"),
    max_b: str = Query("", description="最大余额"),
    active_since: str = Query("", description="最近活跃起始 2025-01-01"),
    created_start: str = Query("", description="注册起始"),
    created_end: str = Query("", description="注册结束"),
    invited_by: str = Query("", description="邀请人ID或@用户名"),
    db: Session = Depends(get_db),
):
    stmt, _ = _build_users_query(
        db,
        q=q, token=token, min_b=min_b, max_b=max_b,
        active_since=active_since, created_start=created_start,
        created_end=created_end, invited_by=invited_by,
    )
    rows: List[User] = [r[0] for r in db.execute(stmt).all()]

    data = []
    for u in rows:
        created_at = getattr(u, "created_at", getattr(u, "created", None))
        last_active_at = getattr(u, "last_active_at", getattr(u, "active_at", None))
        data.append({
            "tg_id": getattr(u, "tg_id", None),
            "username": getattr(u, "username", None),
            "invited_by": getattr(u, "invited_by", None),
            "created_at": created_at.isoformat(sep=" ") if hasattr(created_at, "isoformat") else (created_at or None),
            "last_active_at": last_active_at.isoformat(sep=" ") if hasattr(last_active_at, "isoformat") else (last_active_at or None),
        })
    return JSONResponse(data)


# ---------- 详情页 ----------

@router.get("/{user_ref}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def user_detail(
    request: Request,
    user_ref: str,
    token: str = Query("", description="过滤流水的币种"),
    page: int = Query(1, ge=1),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
    db: Session = Depends(get_db),
):
    """
    用户详情：
    - 顶部展示基础信息与余额（用 Ledger 聚合计算余额兜底）
    - 下方展示账本流水，支持按 token 过滤与分页
    """
    # 解析 user_ref
    f, val = _parse_user_ref(user_ref)
    if f == "tg_id":
        u = db.execute(select(User).where(User.tg_id == int(val))).scalars().first()
    elif f == "username":
        u = db.execute(select(User).where(User.username == val)).scalars().first()
    else:
        # 兜底再尝试
        try:
            u = db.execute(select(User).where(User.tg_id == int(user_ref))).scalars().first()
        except Exception:
            u = db.execute(select(User).where(User.username == user_ref)).scalars().first()

    if not u:
        # 简单 404 提示：回退到列表页
        return request.app.state.templates.TemplateResponse(
            "users_list.html",
            {
                "request": request,
                "rows": [],
                "filters": {"q": user_ref},
                "tokens": ["USDT", "TON", "POINT"],
                "page": 1,
                "total_pages": 1,
                "csrf_token": issue_csrf(request),  # 即便 404 也签发，方便回到列表页继续操作
            },
            status_code=404,
        )

    # 余额聚合
    bal_stmt = (
        select(Ledger.token, func.coalesce(func.sum(Ledger.amount), 0))
        .where(Ledger.user_id == u.tg_id)
        .group_by(Ledger.token)
    )
    balances: Dict[str, float] = {row[0]: float(row[1]) for row in db.execute(bal_stmt).all()}

    # 流水明细
    where_lg = [Ledger.user_id == u.tg_id]
    if token:
        where_lg.append(Ledger.token == token)

    lg_stmt = select(Ledger).where(and_(*where_lg))

    # 按账本时间倒序（动态列）
    lg_time_cols = [
        c for c in (
            getattr(Ledger, "created_at", None),
            getattr(Ledger, "ts", None),
        ) if c is not None
    ]
    if lg_time_cols:
        lg_stmt = lg_stmt.order_by(desc(func.coalesce(*lg_time_cols)))
    else:
        lg_stmt = lg_stmt.order_by(desc(func.now()))

    total_lg = db.execute(
        select(func.count()).select_from(lg_stmt.subquery())
    ).scalar_one()

    limit, offset = _paginate(page, page_size)
    lg_stmt = lg_stmt.limit(limit).offset(offset)
    ledgers = [r[0] for r in db.execute(lg_stmt).all()]

    tokens = ["USDT", "TON", "POINT"]
    total_pages = (total_lg + page_size - 1) // page_size

    # ✅ 详情页也签发 CSRF，便于“封禁/改角/改余额”等行内操作表单使用
    csrf_token = issue_csrf(request)

    return request.app.state.templates.TemplateResponse(
        "user_detail.html",
        {
            "request": request,
            "u": u,
            "tokens": tokens,
            "balances": balances,
            "ledgers": ledgers,
            "token": token,
            "page": page,
            "total_pages": total_pages,
            "csrf_token": csrf_token,
        },
    )
