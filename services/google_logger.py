# google_logger.py
# -*- coding: utf-8 -*-
"""
Google Sheets 用户资料记录工具（全表头版本）
- 依赖 gspread 库和 Google service_account 凭证 JSON
- 调用入口：log_user_to_sheet(user, source, chat=None, inviter_user_id=None,
                           joined_via_invite_link=False, note=None)

本版本特性：
1) 统一中文表头，启动时自动校验/修复
2) 追加写入支持 3 次指数退避重试（0.5s / 1s / 2s）
3) 幂等保证（两层策略）：
   - 强幂等：落库 (chat_id, user_id) 主键：mark_member_logged_once / clear_member_logged
   - 轻幂等：进程内存缓存 + 定时批量预读（极少读表）
4) 统一日志；写入失败返回 False，不抛出异常以免影响业务流程
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List

import gspread
from aiogram.types import User, Chat
from gspread.worksheet import Worksheet
from gspread.exceptions import APIError
from gspread.utils import rowcol_to_a1  # 批量范围定位

# 强幂等（数据库）接口
from models.db import (
    mark_member_logged_once,
    clear_member_logged,
)

# === 配置 ===
SHEET_NAME = "红包群成员"                  # 表格名称（需与实际一致）
CREDENTIAL_FILE = "service_account.json"  # 凭证 JSON 路径（相对项目根目录，或改为绝对路径）

# 统一表头（中文）
HEADERS: List[str] = [
    "记录时间(UTC)",
    "来源",
    "用户ID",
    "用户名",
    "名",
    "姓",
    "全名",
    "语言代码",
    "是否机器人",
    "聊天ID",
    "聊天名称",
    "聊天类型",
    "邀请者用户ID",
    "是否通过邀请链接加入",
    "备注",
]

# 认为需要“幂等检查”的来源（入群路径 + 首次交互）
_JOIN_SOURCES = {
    "member_join_message",
    "member_join",
    "member_join_via_invite",
    "member_join_approved",
    "first_seen_in_group",  # 首次交互也按“每群每人仅一行”处理
}

# —— 轻幂等缓存（减少 API/DB 冲击） ——
# 已写入过 Sheet 的 (chat_id, user_id) 集合；进程存活期内有效
_WRITTEN_KEYS: set[tuple[int, int]] = set()
_LAST_HYDRATE_TS: float = 0.0
_HYDRATE_INTERVAL_SEC: float = 120.0  # 间隔 2 分钟最多读表一次，用于冷启动或长期运行的纠偏


# ---------- 内部工具 ----------

def _get_gc():
    """获取 gspread client"""
    return gspread.service_account(filename=CREDENTIAL_FILE)


def _get_worksheet() -> Worksheet:
    """
    打开 Sheet 并返回第一个工作表；若无表头则自动写入。
    注意：这是一次“读表”操作，已经延迟到“确认需要写入”之后再调用。
    """
    gc = _get_gc()
    sh = gc.open(SHEET_NAME)
    ws: Worksheet = sh.sheet1

    # 确保表头
    try:
        values = ws.row_values(1)
        normalized = [v.strip() for v in values] if values else []
        if normalized != HEADERS:
            ws.update("A1", [HEADERS])
            logging.info("[google_logger] headers updated to standard.")
    except Exception as e:
        logging.warning("[google_logger] ensure header failed: %s", e)
        try:
            ws.update("A1", [HEADERS])
        except Exception as e2:
            logging.error("[google_logger] force set header failed: %s", e2)

    return ws


def _header_index_map(ws: Worksheet) -> Dict[str, int]:
    """读取首行，返回 {列名: 列号(1-based)}"""
    headers = ws.row_values(1)
    idx_map: Dict[str, int] = {}
    for i, h in enumerate(headers, start=1):
        idx_map[h.strip()] = i
    return idx_map


def _utc_now_iso() -> str:
    """以 UTC 写入，带毫秒"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _to_yes_no(flag: bool) -> str:
    return "是" if flag else "否"


def _build_row(
    user: User,
    source: str,
    chat: Optional[Chat],
    inviter_user_id: Optional[int],
    joined_via_invite_link: bool,
    note: Optional[str],
) -> List[str]:
    """构造一行数据，按 HEADERS 的顺序"""
    chat_id = getattr(chat, "id", None)
    chat_title = getattr(chat, "title", None)
    chat_type = getattr(chat, "type", None)

    return [
        _utc_now_iso(),
        source or "",
        str(getattr(user, "id", "")) or "",
        getattr(user, "username", "") or "",
        getattr(user, "first_name", "") or "",
        getattr(user, "last_name", "") or "",
        getattr(user, "full_name", "") or f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip(),
        getattr(user, "language_code", "") or "",
        _to_yes_no(bool(getattr(user, "is_bot", False))),
        str(chat_id) if chat_id is not None else "",
        (chat_title or "") if chat_title else "",
        (chat_type or "") if chat_type else "",
        str(inviter_user_id) if inviter_user_id is not None else "",
        _to_yes_no(bool(joined_via_invite_link)),
        note or "",
    ]


def _append_with_retry(ws: Worksheet, row: List[str], retries: int = 3) -> bool:
    """追加一行，带指数退避重试。"""
    delay = 0.5
    for attempt in range(1, retries + 1):
        try:
            ws.append_row(row, value_input_option="RAW")
            return True
        except APIError as e:
            logging.warning("[google_logger] append APIError (attempt %s/%s): %s", attempt, retries, e)
        except Exception as e:
            logging.warning("[google_logger] append failed (attempt %s/%s): %s", attempt, retries, e)

        time.sleep(delay)
        delay *= 2
    return False


# ---------- 轻幂等缓存：批量预读 + 查询 ----------

def _hydrate_written_keys(ws: Worksheet, idx_map: Dict[str, int]):
    """
    批量预读（最多每 _HYDRATE_INTERVAL_SEC 触发一次）：
    一次性读取“用户ID”和“聊天ID”两列，组装 (chat_id, user_id) 集合到内存。
    """
    global _WRITTEN_KEYS, _LAST_HYDRATE_TS
    user_col = idx_map.get("用户ID")
    chat_col = idx_map.get("聊天ID")
    if not user_col or not chat_col:
        return

    rng_user = f"{rowcol_to_a1(2, user_col)}:{rowcol_to_a1(ws.row_count, user_col)}"
    rng_chat = f"{rowcol_to_a1(2, chat_col)}:{rowcol_to_a1(ws.row_count, chat_col)}"
    try:
        cols = ws.batch_get([rng_user, rng_chat])  # [[u1],[u2],...], [[c1],[c2],...]
        users = cols[0] if len(cols) > 0 else []
        chats = cols[1] if len(cols) > 1 else []
        n = max(len(users), len(chats))
        new_keys = set()
        for i in range(n):
            u = (users[i][0].strip() if i < len(users) and users[i] else "")
            c = (chats[i][0].strip() if i < len(chats) and chats[i] else "")
            if u and c:
                try:
                    new_keys.add((int(c), int(u)))
                except Exception:
                    pass
        if new_keys:
            _WRITTEN_KEYS |= new_keys
        _LAST_HYDRATE_TS = time.time()
    except Exception as e:
        logging.warning("[google_logger] hydrate cache failed: %s", e)


def _ensure_cache_fresh(ws: Worksheet, idx_map: Dict[str, int]):
    """必要时刷新内存缓存（冷启动或超过刷新间隔）"""
    now = time.time()
    if not _WRITTEN_KEYS or (now - _LAST_HYDRATE_TS) > _HYDRATE_INTERVAL_SEC:
        _hydrate_written_keys(ws, idx_map)


# ---------- 对外主函数 ----------

def log_user_to_sheet(
    user: User,
    source: str = "start",
    chat: Optional[Chat] = None,
    inviter_user_id: Optional[int] = None,
    joined_via_invite_link: bool = False,
    note: Optional[str] = None,
) -> bool:
    """
    记录用户信息到 Google Sheet

    :param user: aiogram.types.User（必传）
    :param source: 事件来源（"start" / "member_join_message" / "member_join" /
                   "member_join_via_invite" / "member_join_approved" / "first_seen_in_group" 等）
    :param chat:   当前群/私聊上下文（可选，但建议入群记录务必传）
    :param inviter_user_id: 邀请人 tg_id（可选）
    :param joined_via_invite_link: 是否经邀请链接加入（可选）
    :param note:   备注（可选）
    :return: True 写入成功 / False 写入失败（或被幂等检查跳过）
    """
    if not isinstance(user, User):
        logging.warning("[google_logger] invalid user payload: %r", user)
        return False

    # —— 基本信息（不触发任何外部 IO）——
    chat_id = getattr(chat, "id", None)
    user_id = getattr(user, "id", None)

    # 只对“入群/首次交互”等需要唯一性的来源做幂等
    needs_idempotent = source in _JOIN_SOURCES and chat_id is not None and user_id is not None

    # 1) 进程级内存缓存拦截（最快）——避免频繁 DB/网络开销
    if needs_idempotent and (int(chat_id), int(user_id)) in _WRITTEN_KEYS:
        logging.info("[google_logger] skip duplicate (memory): user_id=%s chat_id=%s source=%s",
                     user_id, chat_id, source)
        return False

    # 2) 强幂等：先在数据库尝试登记唯一键；失败说明已存在（跨进程/重启也拦截）
    if needs_idempotent:
        try:
            inserted = mark_member_logged_once(int(chat_id), int(user_id))
            if not inserted:
                # 同步到本进程的缓存，避免后续同进程重复判断
                _WRITTEN_KEYS.add((int(chat_id), int(user_id)))
                logging.info("[google_logger] skip duplicate (db): user_id=%s chat_id=%s source=%s",
                             user_id, chat_id, source)
                return False
        except Exception as e:
            # 数据库异常时，不终止主流程；降级为“继续尝试写表”，但会减少后续读表次数
            logging.warning("[google_logger] mark_member_logged_once failed, degrade to write: %s", e)

    try:
        # 只有需要真正写入时，才打开工作表（一次读操作）
        ws = _get_worksheet()
        idx_map = _header_index_map(ws)

        # （可选）在冷启动/长间隔时，批量预读现有键到内存，利于后续判断
        try:
            _ensure_cache_fresh(ws, idx_map)
        except Exception:
            pass

        # 3) 构造并写入
        row = _build_row(
            user=user,
            source=source,
            chat=chat,
            inviter_user_id=inviter_user_id,
            joined_via_invite_link=joined_via_invite_link,
            note=note,
        )

        ok = _append_with_retry(ws, row, retries=3)
        if ok:
            logging.info(
                "[google_logger] append ok: source=%s user_id=%s chat_id=%s",
                source, user_id, chat_id
            )
            # 写入成功：把键加入本进程缓存
            if needs_idempotent:
                try:
                    _WRITTEN_KEYS.add((int(chat_id), int(user_id)))
                except Exception:
                    pass
            return True

        # 写入失败：若之前已在 DB 登记了唯一键，这里回滚一次，便于稍后重试
        logging.warning("[google_logger] append failed after retries.")
        if needs_idempotent:
            try:
                clear_member_logged(int(chat_id), int(user_id))
            except Exception:
                pass
        return False

    except Exception as e:
        logging.warning("[google_logger] 写入失败: %s", e)
        # 同上：若落库成功但写表失败，回滚唯一键
        if needs_idempotent:
            try:
                clear_member_logged(int(chat_id), int(user_id))
            except Exception:
                pass
        return False
