# services/sheet_users.py
from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple, Iterable

import csv
import io
import os
from datetime import datetime, timezone

# gspread 延迟导入：只有真正更新数据或计算列地址时才导入，
# 这样即使环境未安装 gspread，列表页也能优雅提示而非崩溃。
def _ensure_gspread():
    try:
        import gspread  # type: ignore
        return gspread
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "缺少依赖 gspread/google-auth，或运行环境无法访问 PyPI。"
            "请先安装：pip install gspread google-auth。"
            f" 原始错误：{e}"
        )

# 兼容两种工程放置：优先 services.google_logger，其次根模块 google_logger
try:
    from services.google_logger import _get_worksheet  # type: ignore
except Exception:  # pragma: no cover
    from services.google_logger import _get_worksheet  # type: ignore

# 允许编辑的白名单字段（其余只读，避免误改主键/时间）
EDITABLE_COLUMNS = {
    "来源",
    "用户名",
    "名",
    "姓",
    "全名",
    "语言代码",
    "是否机器人",
    "聊天名称",
    "聊天类型",
    "邀请者用户ID",
    "是否通过邀请链接加入",
    "备注",
}

def _header_index_map(ws) -> Dict[str, int]:
    headers = ws.row_values(1)
    return {(h or "").strip(): idx for idx, h in enumerate(headers, start=1)}

def list_rows(
    page: int = 1,
    per_page: int = 50,
    filters: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], int, List[str]]:
    """
    读取表数据并做内存过滤 + 分页。
    返回 (当前页 rows, 总行数, 表头 headers)
    行数据包含特殊键 "__row"（行号，从 2 起）。
    """
    ws = _get_worksheet()
    headers: List[str] = [(h or "").strip() for h in ws.row_values(1)]
    values = ws.get_all_values()[1:]  # 去掉首行
    rows: List[Dict[str, Any]] = []

    f = {k: (v or "").strip() for k, v in (filters or {}).items() if (v or "").strip()}
    for i, line in enumerate(values, start=2):
        item = {"__row": i}
        for idx, h in enumerate(headers):
            item[h] = line[idx] if idx < len(line) else ""
        matched = True
        for k, v in f.items():
            if v not in str(item.get(k, "")):
                matched = False
                break
        if matched:
            rows.append(item)

    total = len(rows)
    start = max(0, (page - 1) * per_page)
    end = start + per_page
    return rows[start:end], total, headers

def get_row(row_number: int) -> Tuple[Dict[str, Any], List[str]]:
    ws = _get_worksheet()
    header: List[str] = [(h or "").strip() for h in ws.row_values(1)]
    values = ws.row_values(row_number)
    data = {"__row": row_number}
    for idx, h in enumerate(header):
        data[h] = values[idx] if idx < len(values) else ""
    return data, header

def update_row(row_number: int, payload: Dict[str, str]) -> None:
    """
    只允许改白名单列；header 行不可改；其余列忽略。
    """
    if row_number <= 1:
        raise ValueError("cannot edit header row")
    ws = _get_worksheet()
    idx_map = _header_index_map(ws)

    gspread = _ensure_gspread()
    requests = []
    for col_name, new_val in payload.items():
        name = (col_name or "").strip()
        if name not in EDITABLE_COLUMNS:
            continue
        col = idx_map.get(name)
        if not col:
            continue
        a1 = gspread.utils.rowcol_to_a1(row_number, col)
        requests.append({"range": a1, "values": [[str(new_val)]]})

    if requests:
        ws.batch_update(requests, value_input_option="RAW")

# =========================
# 导出 & 审计
# =========================
def export_rows_as_csv(filters: Optional[Dict[str, str]] = None) -> Iterable[bytes]:
    """将（可筛选的）全量数据导出为 CSV（流式）"""
    rows, total, headers = list_rows(page=1, per_page=10**9, filters=filters or {})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["__row", *headers])
    for r in rows:
        w.writerow([r.get("__row", "")] + [r.get(h, "") for h in headers])
    yield buf.getvalue().encode("utf-8")

_AUDIT_FILE = os.getenv("SHEET_USERS_AUDIT_FILE", "storage/audit_sheet_users.csv")
_AUDIT_HEADERS = ["time_utc", "row", "field", "old", "new", "editor"]

def _ensure_audit_file():
    os.makedirs(os.path.dirname(_AUDIT_FILE), exist_ok=True)
    if not os.path.exists(_AUDIT_FILE) or os.path.getsize(_AUDIT_FILE) == 0:
        with open(_AUDIT_FILE, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(_AUDIT_HEADERS)

def append_audit(*, row: int, field: str, old: str, new: str, editor: str):
    """
    追加一行审计记录到 CSV 文件。
    若你想写入 DB，可在此处替换为 ORM insert，并复用导出接口。
    """
    _ensure_audit_file()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(_AUDIT_FILE, "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([ts, row, field, old, new, editor])

def export_audit_as_csv() -> Iterable[bytes]:
    _ensure_audit_file()
    # 流式读出，避免一次性加载
    with open(_AUDIT_FILE, "r", encoding="utf-8", newline="") as f:
        for chunk in iter(lambda: f.read(64 * 1024), ""):
            yield chunk.encode("utf-8")
