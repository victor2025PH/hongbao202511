# routers/today.py
# -*- coding: utf-8 -*-
"""
今日战绩（仅统计“抢到”的数据），按『用户时区』自然日 00:00–23:59：
- today:me / today:main  → 展示今日抢红包汇总（分币种：USDT/TON/积分）
- /today                 → 文本命令

兼容点：
- Ledger.amount / Ledger.value 金额字段名差异
- 同时匹配新旧流水枚举：HONGBAO_GRAB / GRAB / ENVELOPE_GRAB
- User.timezone 若无则回退 settings.TIMEZONE（默认 Asia/Manila）
"""

from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest

from core.i18n.i18n import t
from core.utils.keyboards import back_home_kb
from config.settings import settings
from models.db import get_session
from models.ledger import Ledger, LedgerType
from models.user import User

router = Router()
log = logging.getLogger("today")


# ---------- 语言 & 时区 ----------
def _canon_lang(code: str | None) -> str:
    if not code:
        return "zh"
    c = str(code).strip().lower()
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"
    return "zh"

def _db_lang(user_id: int, fallback_user) -> str:
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u and getattr(u, "language", None):
            return _canon_lang(u.language)
    return _canon_lang(getattr(fallback_user, "language_code", None))

def _user_tz(user_id: int) -> ZoneInfo:
    tz_name = None
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u and getattr(u, "timezone", None):
            tz_name = str(u.timezone).strip()
    if not tz_name:
        tz_name = getattr(settings, "TIMEZONE", None) or "Asia/Manila"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        log.warning("today: invalid timezone '%s', fallback to Asia/Manila", tz_name)
        return ZoneInfo("Asia/Manila")

def _today_range_for_user(user_id: int):
    tz = _user_tz(user_id)
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), tz


# ---------- 兼容 LedgerType 枚举 ----------
def _grab_types():
    """
    统一匹配“抢红包入账”的所有可能值：
      - 新规范：HONGBAO_GRAB
      - 旧兼容：GRAB / ENVELOPE_GRAB
    """
    names = ("HONGBAO_GRAB", "GRAB", "ENVELOPE_GRAB")
    arr = []
    for n in names:
        if hasattr(LedgerType, n):
            arr.append(getattr(LedgerType, n))
    # 兜底
    return arr or [LedgerType.HONGBAO_GRAB]


# ---------- 金额 & 币种 ----------
def _amount_attr() -> str:
    if hasattr(Ledger, "amount"):
        return "amount"
    if hasattr(Ledger, "value"):
        return "value"
    return "amount"

def _token_name_for_display(token: str, lang: str) -> str:
    u = (token or "").upper()
    if u in ("POINT", "POINTS"):
        return t("asset.points", lang) or "积分"
    if u == "USDT":
        return t("asset.usdt", lang) or "USDT"
    if u == "TON":
        return t("asset.ton", lang) or "TON"
    return u or "—"

def _fmt_amount_by_token(token: str, amount: float) -> str:
    tok = (token or "").upper()
    if tok in ("USDT", "TON"):
        return f"{amount:.2f}"
    # POINT / 其它 → 取整展示
    return str(int(round(amount)))


# ---------- 主处理：today:me / today:main ----------
@router.callback_query(F.data.in_({"today:me", "today:main"}))
async def today_me(cb: CallbackQuery):
    lang = _db_lang(cb.from_user.id, cb.from_user)
    uid = cb.from_user.id

    start_utc, end_utc, tz = _today_range_for_user(uid)
    amount_field = _amount_attr()
    types = _grab_types()

    with get_session() as s:
        rows = (
            s.query(Ledger)
            .filter(Ledger.user_tg_id == uid)
            .filter(Ledger.type.in_(types))
            .filter(Ledger.created_at >= start_utc, Ledger.created_at < end_utc)
            .all()
        )

    grab_count = len(rows)
    by_token: dict[str, float] = {}
    if amount_field:
        for r in rows:
            tok = (getattr(r, "token", None) or "").upper() or "UNKNOWN"
            val = float(getattr(r, amount_field) or 0.0)
            by_token[tok] = by_token.get(tok, 0.0) + val

    header = t("today.title", lang) or "📊 Today’s Record"
    date_line = t(
        "today.date_local",
        lang,
        start=start_utc.astimezone(tz).strftime("%Y-%m-%d 00:00"),
        end=(end_utc - timedelta(seconds=1)).astimezone(tz).strftime("%Y-%m-%d 23:59"),
        tz=str(tz),
    ) or f"🕒 {start_utc.astimezone(tz):%Y-%m-%d} (Local)"

    lines = [
        header,
        "────────────────",
        date_line,
        t("today.grabbed_only", lang, count=grab_count) or f"🤲 Grabbed {grab_count} packets",
    ]

    if grab_count == 0:
        empty = t("today.empty", lang) or "📭 No records yet."
        lines.append("")
        lines.append(empty)
    else:
        if by_token:
            lines.append("")
            for tok, amt in by_token.items():
                lines.append(f"• {_token_name_for_display(tok, lang)} = {_fmt_amount_by_token(tok, amt)}")

    text = "\n".join(lines)
    kb = back_home_kb(lang)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


# ---------- 命令：/today ----------
@router.message(F.text.regexp(r"^/today$"))
async def today_cmd(msg: Message):
    lang = _db_lang(msg.from_user.id, msg.from_user)
    uid = msg.from_user.id

    start_utc, end_utc, tz = _today_range_for_user(uid)
    amount_field = _amount_attr()
    types = _grab_types()

    with get_session() as s:
        rows = (
            s.query(Ledger)
            .filter(Ledger.user_tg_id == uid)
            .filter(Ledger.type.in_(types))
            .filter(Ledger.created_at >= start_utc, Ledger.created_at < end_utc)
            .all()
        )

    grab_count = len(rows)
    by_token: dict[str, float] = {}
    if amount_field:
        for r in rows:
            tok = (getattr(r, "token", None) or "").upper() or "UNKNOWN"
            val = float(getattr(r, amount_field) or 0.0)
            by_token[tok] = by_token.get(tok, 0.0) + val

    header = t("today.title", lang) or "📊 Today’s Record"
    date_line = t(
        "today.date_local",
        lang,
        start=start_utc.astimezone(tz).strftime("%Y-%m-%d 00:00"),
        end=(end_utc - timedelta(seconds=1)).astimezone(tz).strftime("%Y-%m-%d 23:59"),
        tz=str(tz),
    ) or f"🕒 {start_utc.astimezone(tz):%Y-%m-%d} (Local)"

    lines = [
        header,
        "────────────────",
        date_line,
        t("today.grabbed_only", lang, count=grab_count) or f"🤲 Grabbed {grab_count} packets",
    ]

    if grab_count == 0:
        empty = t("today.empty", lang) or "📭 No records yet."
        lines.append("")
        lines.append(empty)
    else:
        if by_token:
            lines.append("")
            for tok, amt in by_token.items():
                lines.append(f"• {_token_name_for_display(tok, lang)} = {_fmt_amount_by_token(tok, amt)}")

    text = "\n".join(lines)
    await msg.answer(text, parse_mode="HTML", reply_markup=back_home_kb(lang))
