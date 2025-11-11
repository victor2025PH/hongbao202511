# routers/rank.py
# -*- coding: utf-8 -*-
"""
排行榜路由（保留两块）：
A. 本轮最佳手气排行榜（当前红包 TopN + 运气王）
B. 『📊 今日战绩』按钮（跳到用户当天统计，只看“抢到”）

- rank:round:{envelope_id}    → 展示该红包的本轮排行榜（并附『📊 今日战绩』按钮）
- /start rank_{envelope_id}   → 深链直达该红包排行榜
- rank:main                   → 兼容总榜入口（提示已下线，引导去『📊 今日战绩』）
"""

from __future__ import annotations
import re
import logging
from typing import Any, List

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from core.i18n.i18n import t
from core.utils.keyboards import hb_rank_kb, back_home_kb
from models.envelope import (
    list_envelope_claims,
    get_lucky_winner,
    get_envelope_summary,
    HBNotFound,
)
from models.user import User
from models.db import get_session
from config.feature_flags import flags

router = Router()
log = logging.getLogger("rank")
TOP_N = 10  # 展示前 N 名


# ---------- 语言与用户展示 ----------
def _canon_lang(code: str | None) -> str:
    if not code:
        return "zh"
    c = str(code).strip().lower()
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"
    return "zh"


def _db_lang_or_fallback(user_id: int, fallback_user) -> str:
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u and getattr(u, "language", None):
            return _canon_lang(u.language)
    return _canon_lang(getattr(fallback_user, "language_code", None))


def _user_display(user_id: int) -> str:
    """优先展示 @username；没有则回退为 ID 字符串"""
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u and getattr(u, "username", None):
            return f"@{u.username}"
    return str(user_id)


def _t_first(keys: List[str], lang: str, fallback: str = "") -> str:
    """依次尝试 keys 中的文案键，返回第一个命中的；都为空则返回 fallback。"""
    for k in keys:
        try:
            v = t(k, lang)
            if v:
                return v
        except Exception:
            pass
    return fallback


def _fmt_amount(token: str, amount: float) -> str:
    """展示金额：USDT/TON 保留 2 位小数；POINT 取整。"""
    tok = (token or "").upper()
    if tok in ("USDT", "TON"):
        return f"{amount:.2f}"
    return str(int(round(amount)))


# ---------- 文本构建 ----------
def _build_round_rank_text(envelope_id: int, lang: str = "zh") -> str:
    """构造当次红包排行榜文本（TopN + 运气王）"""
    try:
        claims = list_envelope_claims(envelope_id)
    except HBNotFound:
        return _t_first(["rank.none"], lang, "😅 Nobody grabbed yet — be the first!")

    if not claims:
        return _t_first(["rank.none"], lang, "😅 Nobody grabbed yet — be the first!")

    # 尝试取得币种（USDT/TON/POINT），用于格式化金额
    token_disp = ""
    try:
        summary = get_envelope_summary(envelope_id) or {}
        mode = str(summary.get("mode", "")).upper()
        token_disp = mode if mode in ("USDT", "TON", "POINT") else ""
    except Exception:
        token_disp = ""

    # 兼容 ORM 或 dict
    def _get(item: Any, key: str, default=None):
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    lines = [_t_first(["rank.round_title"], lang, "🏆 <b>Round Ranking</b>")]
    for i, c in enumerate(claims[:TOP_N], start=1):
        uid = int(_get(c, "user_tg_id") or _get(c, "user_id") or 0)
        user_disp = _user_display(uid) if uid else str(_get(c, "user_tg_id") or _get(c, "user_id") or "")
        try:
            amount_val = float(_get(c, "amount") or 0.0)
        except Exception:
            amount_val = 0.0
        lines.append(f"{i}. {user_disp} — {_fmt_amount(token_disp, amount_val)}{(' ' + token_disp) if token_disp else ''}")

    # 运气王
    try:
        lucky = get_lucky_winner(envelope_id)  # (user_id, amount) 或 None
    except HBNotFound:
        lucky = None

    if lucky:
        name = _user_display(int(lucky[0]))
        amount_s = _fmt_amount(token_disp, float(lucky[1]))
        lines.append("")
        # 若 token 取不到，就用空串，保证占位安全
        lucky_line = _t_first(
            ["rank.lucky"],
            lang,
            f"🍀 Lucky winner: {name} ({amount_s} {token_disp})"
        ).format(name=name, amount=amount_s, token=token_disp)
        lines.append(lucky_line)

    return "\n".join(lines)


def _append_today_button(kb: InlineKeyboardMarkup | None, lang: str) -> InlineKeyboardMarkup:
    """
    在原有的排行榜键盘下追加一行『📊 今日战绩』按钮（回调 today:me）。
    若 hb_rank_kb 已内建该按钮，则这行会成为“额外的第二个入口”，不影响使用。
    """
    title = _t_first(["today.button"], lang, "📊 Today")
    btn = InlineKeyboardButton(text=title, callback_data="today:me")
    if isinstance(kb, InlineKeyboardMarkup) and kb.inline_keyboard:
        rows = list(kb.inline_keyboard)
        rows.append([btn])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


# ===== 回调入口：rank:round:{eid} =====
@router.callback_query(F.data.regexp(r"^rank:round:\d+$"))
async def rank_round(cb: CallbackQuery):
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    m = re.match(r"^rank:round:(\d+)$", cb.data or "")
    if not m:
        await cb.answer(_t_first(["errors.bad_request"], lang, "bad request"))
        return
    eid = int(m.group(1))

    text = _build_round_rank_text(eid, lang)
    base_kb = hb_rank_kb(eid, lang, show_next=True)
    kb = _append_today_button(base_kb, lang)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


# ===== 深链入口：/start rank_{eid} =====
@router.message(F.text.regexp(r"^/start(\s+|)rank_\d+$"))
async def deeplink_rank(msg: Message):
    lang = _db_lang_or_fallback(msg.from_user.id, msg.from_user)
    m = re.match(r"^/start(?:\s+|)rank_(\d+)$", msg.text or "")
    if not m:
        return
    eid = int(m.group(1))

    text = _build_round_rank_text(eid, lang)
    base_kb = hb_rank_kb(eid, lang, show_next=True)
    kb = _append_today_button(base_kb, lang)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ===== 兼容旧『总榜』入口：rank:main → 受全局开关控制，默认引导去『今日战绩』 =====
@router.callback_query(F.data == "rank:main")
async def rank_main(cb: CallbackQuery):
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)

    # 目前不实现全局榜单，统一引导到“今日战绩”
    tip = _t_first(
        ["rank.global_offline"],
        lang,
        "📉 Global leaderboard is offline.\nTap below to view your 📊 Today’s stats."
    )

    # 若未来开放全局榜，可根据 flags.ENABLE_RANK_GLOBAL 分支到实际实现
    if flags.get("ENABLE_RANK_GLOBAL", False) is False:
        tip = _t_first(["rank.global_offline"], lang, tip)
    else:
        tip = _t_first(["rank.global_offline"], lang, tip)

    today_btn = InlineKeyboardButton(text=_t_first(["today.button"], lang, "📊 Today"), callback_data="today:me")
    back_kb = back_home_kb(lang)
    rows = list(back_kb.inline_keyboard) if back_kb and back_kb.inline_keyboard else []
    rows.insert(0, [today_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else InlineKeyboardMarkup(inline_keyboard=[[today_btn]])

    try:
        await cb.message.edit_text(tip, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(tip, parse_mode="HTML", reply_markup=kb)
    await cb.answer()
