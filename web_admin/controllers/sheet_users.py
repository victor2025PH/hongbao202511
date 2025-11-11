# web_admin/controllers/sheet_users.py
from __future__ import annotations
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, Request, Query, Form, Body, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse

from web_admin.deps import require_admin
from services.sheet_users import (
    list_rows, get_row, update_row, EDITABLE_COLUMNS,
    export_rows_as_csv, append_audit, export_audit_as_csv
)

router = APIRouter(prefix="/admin/sheet-users", tags=["admin-sheet-users"])
PAGE_SIZE = 50


def _nav(req: Request):
    # 给 base.html 的导航高亮使用
    req.state.nav_active = "sheet_users"
    return {"nav_active": "sheet_users", "title": "用户资料（Google Sheet）"}


def _editor_from_session(sess: Any) -> str:
    return getattr(sess, "username", None) or getattr(sess, "name", None) or str(getattr(sess, "id", "")) or "admin"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def page_list(
    req: Request,
    sess=Depends(require_admin),
    page: int = 1,
    per_page: int = PAGE_SIZE,
    q_user_id: Optional[str] = Query(None, alias="用户ID"),
    q_username: Optional[str] = Query(None, alias="用户名"),
    q_chat_id: Optional[str] = Query(None, alias="聊天ID"),
    q_source: Optional[str] = Query(None, alias="来源"),
):
    filters: Dict[str, str] = {}
    if q_user_id:
        filters["用户ID"] = q_user_id
    if q_username:
        filters["用户名"] = q_username
    if q_chat_id:
        filters["聊天ID"] = q_chat_id
    if q_source:
        filters["来源"] = q_source

    # 尝试加载数据；如果环境缺依赖，给出友好提示不崩溃
    error: Optional[str] = None
    try:
        rows, total, headers = list_rows(page=page, per_page=per_page, filters=filters)
    except Exception as e:
        rows, total, headers = [], 0, []
        error = f"加载 Google Sheet 失败：{e}"

    total_pages = max(1, (total + per_page - 1) // per_page)

    def _q(k, v):
        return f"&{k}={v}" if v else ""

    export_url = (
        "/admin/sheet-users/export"
        f"?page={page}&per_page={per_page}"
        f"{_q('用户ID', q_user_id)}{_q('用户名', q_username)}{_q('聊天ID', q_chat_id)}{_q('来源', q_source)}"
    )

    ctx = {
        "request": req,
        "rows": rows,
        "page": page,
        "total_pages": total_pages,
        "headers": headers,
        "editable": EDITABLE_COLUMNS,
        "export_url": export_url,
        "filters": {
            "用户ID": q_user_id or "",
            "用户名": q_username or "",
            "聊天ID": q_chat_id or "",
            "来源": q_source or "",
        },
        "error": error,
        **_nav(req),
    }
    return req.app.state.templates.TemplateResponse("sheet_users.html", ctx)


@router.get("/edit", response_class=HTMLResponse)
def page_edit(
    req: Request,
    sess=Depends(require_admin),
    row: int = Query(..., ge=2),
):
    data, headers = get_row(row)
    ctx = {
        "request": req,
        "row": row,
        "data": data,
        "headers": headers,
        "editable": EDITABLE_COLUMNS,
        **_nav(req),
        "title": f"编辑第 {row} 行",
    }
    return req.app.state.templates.TemplateResponse("sheet_users_edit.html", ctx)


@router.post("/edit")
async def do_edit(
    req: Request,
    sess=Depends(require_admin),
    row: int = Form(...),
):
    form = await req.form()
    payload = {k: v for k, v in form.items() if k not in ("row",)}
    before, _ = get_row(row)
    update_row(row, payload)
    editor = _editor_from_session(sess)
    for k, new_v in payload.items():
        old_v = before.get(k, "")
        if str(old_v) != str(new_v):
            append_audit(row=row, field=k, old=old_v, new=new_v, editor=editor)
    return RedirectResponse(url="/admin/sheet-users?ok=updated", status_code=303)


# 行内编辑（AJAX）
@router.post("/inline")
async def inline_edit(
    req: Request,
    sess=Depends(require_admin),
    body: Dict[str, Any] = Body(...),
):
    row = int(body.get("row") or 0)
    field = str(body.get("field") or "").strip()
    value = str(body.get("value") or "")
    if row < 2:
        raise HTTPException(status_code=400, detail="invalid row")
    if field not in EDITABLE_COLUMNS:
        raise HTTPException(status_code=400, detail="field not editable")

    before, _ = get_row(row)
    update_row(row, {field: value})
    editor = _editor_from_session(sess)
    old_v = before.get(field, "")
    if str(old_v) != str(value):
        append_audit(row=row, field=field, old=old_v, new=value, editor=editor)
    return JSONResponse({"ok": 1, "row": row, "field": field, "value": value})


# CSV 导出（数据）
@router.get("/export")
def export_csv(
    req: Request,
    sess=Depends(require_admin),
    page: int = 1,
    per_page: int = PAGE_SIZE,
    q_user_id: Optional[str] = Query(None, alias="用户ID"),
    q_username: Optional[str] = Query(None, alias="用户名"),
    q_chat_id: Optional[str] = Query(None, alias="聊天ID"),
    q_source: Optional[str] = Query(None, alias="来源"),
):
    filters: Dict[str, str] = {}
    if q_user_id:
        filters["用户ID"] = q_user_id
    if q_username:
        filters["用户名"] = q_username
    if q_chat_id:
        filters["聊天ID"] = q_chat_id
    if q_source:
        filters["来源"] = q_source

    stream = export_rows_as_csv(filters=filters)
    return StreamingResponse(
        stream,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=sheet_users.csv"},
    )


# CSV 导出（审计）
@router.get("/export-audit")
def export_audit(sess=Depends(require_admin)):
    stream = export_audit_as_csv()
    return StreamingResponse(
        stream,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=sheet_users_audit.csv"},
    )
