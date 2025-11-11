# web_admin/controllers/covers.py
from __future__ import annotations

import os
import re
import math
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Request,
    UploadFile,
    File,
    HTTPException,
)
from fastapi.responses import HTMLResponse, RedirectResponse

from core.i18n.i18n import t
from web_admin.deps import require_admin

# 只通过 models 层提供的函数操作数据（内部自管会话）
from models.cover import (
    Cover,
    list_covers,             # list_covers(page, page_size, active=None, q=None)
    add_cover,               # add_cover(channel_id, message_id, file_id, media_type, slug, title, tags, creator_tg_id)
    delete_cover,            # delete_cover(cover_id)
    toggle_cover_active,     # toggle_cover_active(cover_id, to=None)
)

router = APIRouter(prefix="/admin/covers", tags=["admin-covers"])

# -------- 列表页 --------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def covers_list_page(
    req: Request,
    sess = Depends(require_admin),
    page: int = 1,
    per_page: int = 12,
    q: Optional[str] = None,
    active: Optional[bool] = None,   # 改名：only_enabled -> active
):
    q = (q or "").strip() or None

    # models.cover.list_covers 支持 (active=None/True/False)
    rows, total = list_covers(page=page, page_size=per_page, active=active, q=q)
    total_pages = max(1, math.ceil(total / per_page)) if per_page else 1

    return req.app.state.templates.TemplateResponse(
        "covers_list.html",
        {
            "request": req,
            "title": t("admin.covers.title"),
            "covers": rows,
            "page": page,
            "total_pages": total_pages,
            "q": q or "",
            "active": active,              # 改名：only_enabled -> active
            "nav_active": "covers",
        },
    )

# -------- 上传封面（本地文件 -> 存路径到 file_id）--------
@router.post("/upload", response_class=RedirectResponse)
def covers_upload(
    req: Request,
    title: str = Form(...),
    file: UploadFile = File(...),
    sess = Depends(require_admin),
):
    if not file:
        raise HTTPException(status_code=400, detail="No file")

    # 提取 #标签
    tags = [x.lstrip("#") for x in re.findall(r"#([\w\u4e00-\u9fffA-Za-z0-9_]+)", title)]
    # 简单 slug
    raw_slug = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "").strip()).strip("-").lower()
    slug = (raw_slug[:50] or "cover")

    # 存到静态目录
    static_dir = os.getenv("STATIC_DIR", "static")
    uploads_dir = os.path.join(static_dir, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    # 防重复文件名：slug-原文件名
    safe_name = re.sub(r"[\\/:*?\"<>|]+", "_", file.filename or "cover.bin")
    rel_path = f"uploads/{slug}-{safe_name}"
    abs_path = os.path.join(static_dir, rel_path)

    # 写文件
    with open(abs_path, "wb") as f:
        f.write(file.file.read())

    # 归一化 content_type -> media_type
    ct = (file.content_type or "").lower()
    media_type = "photo"
    if "gif" in ct or "animation" in ct:
        media_type = "animation"
    elif "video" in ct:
        media_type = "video"

    # 将 file_id 存为 URL 友好的 Web 路径，避免 Windows 反斜杠
    web_path = f"static/{rel_path}".replace("\\", "/")

    # 记库：本地文件没有频道上下文，channel_id / message_id 取 0
    add_cover(
        channel_id=0,
        message_id=0,
        file_id=web_path,  # 模板可直接 <img src="/static/...">
        media_type=media_type,
        slug=slug,
        title=(title or "").strip(),
        tags=",".join(tags) if tags else None,
        creator_tg_id=(sess.get("tg_id") if isinstance(sess, dict) else None),  # 统一使用 tg_id
    )

    return RedirectResponse("/admin/covers", status_code=303)

# -------- 上下架 --------
@router.post("/{cover_id}/toggle", response_class=RedirectResponse)
def covers_toggle(
    req: Request,
    cover_id: int,
    sess = Depends(require_admin),
):
    toggle_cover_active(int(cover_id))
    return RedirectResponse("/admin/covers", status_code=303)

# -------- 删除 --------
@router.post("/{cover_id}/delete", response_class=RedirectResponse)
def covers_delete(
    req: Request,
    cover_id: int,
    sess = Depends(require_admin),
):
    delete_cover(int(cover_id))
    return RedirectResponse("/admin/covers", status_code=303)
