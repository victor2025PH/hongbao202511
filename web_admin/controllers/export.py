# web_admin/controllers/export.py
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Set

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse

# i18n：优先用根目录 i18n.py，其次 fallback 到你之前写的 core 目录
try:
    from i18n import t
except Exception:
    from core.i18n.i18n import t  # type: ignore

from web_admin.deps import require_admin

# 统一入口：只从 services 命名空间拿导出函数
from services import export_service as es

# 为了把 @username / tg_id 文本解析成真实 tg_id，这里直接查表
from models.db import get_session
from models.user import User
from sqlalchemy import func  # 若文件中用到了 func，请确保导入

router = APIRouter(prefix="/admin/export", tags=["admin-export"])

EXPORT_DIR = Path(os.getenv("EXPORT_DIR", "exports")).resolve()


def _split_users(raw: str) -> List[str]:
    """
    支持逗号、空白、换行分隔；ID 或 @username 都行。
    返回原始 token 列表（不去重，后续解析）。
    """
    if not raw:
        return []
    parts: List[str] = []
    for ch in [",", "，", ";", "；", "\r", "\n", "\t", " "]:
        raw = raw.replace(ch, ",")
    for token in raw.split(","):
        token = token.strip()
        if token:
            parts.append(token)
    return parts


def _resolve_targets_to_tg_ids(tokens: List[str]) -> List[int]:
    """
    把输入的混合 tokens（'123', '@alice', 'bob'）解析成现有用户的 tg_id 列表。
    - 纯数字：按 tg_id 匹配
    - 其他：按 username 精确匹配（大小写不敏感；去掉开头的 @）
    查不到的直接忽略。最终去重、排序。
    """
    if not tokens:
        return []

    want_ids: Set[int] = set()
    names: Set[str] = set()

    # 先把原始输入分类
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit():
            want_ids.add(int(tok))
        else:
            n = tok[1:] if tok.startswith("@") else tok
            n = n.strip().lower()
            if n:
                names.add(n)

    # 开会话查库
    s = get_session()
    try:
        # 用户名 → tg_id
        if names:
            lower_names = list(names)
            rows = (
                s.query(User.tg_id)
                .filter(func.lower(User.username).in_(lower_names))
                .all()
            )
            for (uid,) in rows:
                if uid is not None:
                    want_ids.add(int(uid))

        # 数字 ID 兜底校验：过滤掉数据库里不存在的
        if want_ids:
            rows = (
                s.query(User.tg_id)
                .filter(User.tg_id.in_(list(want_ids)))
                .all()
            )
            want_ids = {int(uid) for (uid,) in rows if uid is not None}
    finally:
        try:
            s.close()
        except Exception:
            pass

    return sorted(want_ids)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def export_page(req: Request, sess=Depends(require_admin)):
    """
    导出首页：渲染 export.html（你已经有模板就不用我操心了）
    """
    return req.app.state.templates.TemplateResponse(
        "export.html",
        {
            "request": req,
            "title": t("admin.export.title"),
            "nav_active": "export",
        },
    )


# ---- 全量导出（按钮组） ----
@router.post("/all", response_class=FileResponse)
def export_all(kind: str = Form(...), sess=Depends(require_admin)):
    """
    kind:
      - users_ledger  -> 一个文件：Users 全量 + Ledger 全量（两个工作表）
      - users_only    -> 仅 Users 列表
      - ledger_only   -> 仅 Ledger 全量
    """
    if kind == "users_ledger":
        path = es.export_all_users_and_ledger(fmt="xlsx")
    elif kind == "users_only":
        path = es.export_all_users_detail(fmt="xlsx")
    elif kind == "ledger_only":
        path = es.export_all_records(fmt="xlsx")
    else:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))

    if not path:
        raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))
    abs_path = Path(path).resolve()
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))
    return FileResponse(
        abs_path,
        filename=abs_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---- 指定用户导出（文本框） ----
@router.post("/users", response_class=FileResponse)
def export_selected_users(
    req: Request,
    users: str = Form(""),
    mode: str = Form("merged"),
    sess=Depends(require_admin),
):
    """
    mode:
      - merged  -> 多用户合并到一个 Excel（Users + Ledger 两个工作表）
      - split   -> 为每个用户各导出一个 Excel，然后跳回页面（ZIP/队列以后再做）
    """
    tokens = _split_users(users)
    if not tokens:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))

    tg_ids = _resolve_targets_to_tg_ids(tokens)
    if not tg_ids:
        raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))

    if mode == "merged":
        # 我们的 export_service 接口支持多种实参名称，这里显式传 tg_ids
        path = es.export_some_users_and_ledger(tg_ids=tg_ids, fmt="xlsx")
        if not path:
            raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))
        abs_path = Path(path).resolve()
        if not abs_path.exists():
            raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))
        return FileResponse(
            abs_path,
            filename=abs_path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    elif mode == "split":
        # 逐个导出，页面提示去 exports 目录拿；真正打包 ZIP 我们后续交给 queue 做异步
        count = 0
        for uid in tg_ids:
            p = es.export_one_user_full(uid, fmt="xlsx")
            if p:
                count += 1
        # 回到页面，给“完成”提示
        return RedirectResponse(url="/admin/export?done=1&n=%d" % count, status_code=303)

    else:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))
