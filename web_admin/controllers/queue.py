# web_admin/controllers/queue.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Depends, Form, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from sqlalchemy import Column, Integer, String, Text, DateTime, func, desc
from sqlalchemy.orm import declarative_base

from core.i18n.i18n import t
from web_admin.deps import db_session, require_admin

# 正确路径：从 services 包导入导出函数
from services.export_service import (
    export_all_records,
    export_all_users_detail,
    export_all_users_and_ledger,
    export_some_users_and_ledger,
)

Base = declarative_base()
router = APIRouter(prefix="/admin/queue", tags=["admin-queue"])

# ===== ORM: 简易导出任务表 =====
class ExportJob(Base):
    __tablename__ = "admin_export_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(40), nullable=False)               # ALL_USERS_LEDGER / USERS_ONLY / LEDGER_ONLY / SELECTED_MERGED
    status = Column(String(20), nullable=False, default="PENDING")  # PENDING/RUNNING/DONE/FAILED
    params = Column(Text, nullable=True)                    # JSON：{"users":[...]}
    result_path = Column(Text, nullable=True)
    error = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

def _ensure_table(db):
    engine = db.get_bind()
    Base.metadata.create_all(engine)


# ===== helpers =====
def _split_users(raw: str) -> List[str]:
    if not raw:
        return []
    parts = []
    for token in raw.replace("\n", ",").replace("\t", ",").split(","):
        token = token.strip()
        if token:
            parts.append(token)
    return parts


def _run_export_job(job_id: int, base_dir: Optional[Path] = None):
    """
    后台任务执行器（在请求线程之外跑）。
    dbmaker = models.db.get_session 传进来的工厂函数（这里从 deps 用闭包带进来）。
    """
    from models.db import get_session  # 避免循环引用
    # 拿一个新的独立会话，别用请求里的
    with get_session() as db:
        job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
        if not job:
            return
        job.status = "RUNNING"
        job.error = None
        db.commit()

        try:
            kind = job.kind.upper()
            params = json.loads(job.params or "{}")
            path = None

            if kind == "ALL_USERS_LEDGER":
                path = export_all_users_and_ledger(fmt="xlsx")
            elif kind == "USERS_ONLY":
                path = export_all_users_detail(fmt="xlsx")
            elif kind == "LEDGER_ONLY":
                path = export_all_records(fmt="xlsx")
            elif kind == "SELECTED_MERGED":
                members = list(params.get("users") or [])
                if not members:
                    raise ValueError("users required")
                path = export_some_users_and_ledger(members=members, fmt="xlsx")
            else:
                raise ValueError(f"unsupported kind: {kind}")

            if not path:
                raise RuntimeError("export returned empty path")

            # 绝对路径校验
            abs_path = Path(path).resolve()
            if not abs_path.exists():
                raise RuntimeError("export file missing")

            job.status = "DONE"
            job.result_path = str(abs_path)
            db.commit()

        except Exception as e:
            job.status = "FAILED"
            job.error = str(e)
            db.commit()


# ===== routes =====

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def queue_page(req: Request, db=Depends(db_session), sess=Depends(require_admin), page: int = 1, per_page: int = 20):
    _ensure_table(db)
    q = db.query(ExportJob).order_by(desc(ExportJob.id))
    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = max(1, (total + per_page - 1) // per_page)

    return req.app.state.templates.TemplateResponse(
        "queue.html",
        {
            "request": req,
            "title": t("admin.queue.title"),
            "nav_active": "queue",
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
        },
    )


@router.post("/enqueue", response_class=RedirectResponse)
def queue_enqueue(
    req: Request,
    background: BackgroundTasks,
    kind: str = Form(...),            # ALL_USERS_LEDGER / USERS_ONLY / LEDGER_ONLY / SELECTED_MERGED
    users: str = Form(""),            # 仅 SELECTED_MERGED 使用
    db=Depends(db_session),
    sess=Depends(require_admin),
):
    _ensure_table(db)

    kind = kind.strip().upper()
    if kind not in {"ALL_USERS_LEDGER", "USERS_ONLY", "LEDGER_ONLY", "SELECTED_MERGED"}:
        raise HTTPException(status_code=400, detail=t("admin.errors.validation"))

    payload: Dict[str, Any] = {}
    if kind == "SELECTED_MERGED":
        targets = _split_users(users)
        if not targets:
            raise HTTPException(status_code=400, detail=t("admin.errors.validation"))
        payload["users"] = targets

    job = ExportJob(kind=kind, status="PENDING", params=json.dumps(payload, ensure_ascii=False))
    db.add(job)
    db.commit()
    db.refresh(job)

    # 后台执行
    background.add_task(_run_export_job, job.id)

    return RedirectResponse("/admin/queue", status_code=303)


@router.get("/status")
def queue_status(id: int, db=Depends(db_session), sess=Depends(require_admin)):
    _ensure_table(db)
    job = db.query(ExportJob).filter(ExportJob.id == id).first()
    if not job:
        raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))
    return JSONResponse(
        {
            "id": job.id,
            "status": job.status,
            "result_path": job.result_path,
            "error": job.error,
        }
    )


@router.get("/download/{job_id}", response_class=FileResponse)
def queue_download(job_id: int, db=Depends(db_session), sess=Depends(require_admin)):
    _ensure_table(db)
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))
    if job.status != "DONE" or not job.result_path:
        raise HTTPException(status_code=400, detail="Not ready")
    path = Path(job.result_path).resolve()
    if not path.exists():
        raise HTTPException(status_code=404, detail=t("admin.errors.not_found"))
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
