# routers/hongbao.py
# -*- coding: utf-8 -*-
"""
红包交互路由（升级版，仅拼手气随机 + 排行榜显示用户名与幸运之星头像）：
- hb:start / hb:menu  → 进入发红包向导（代理到 menu:send，兼容不同按钮值）
- hb:grab:{eid}       → 抢红包
- 排行榜页：
  * 优先显示 @username；没有 username 时显示可点击姓名（tg://user?id=...），避免只显示纯数字 ID
  * 显示「幸运之星」的头像（能取到时），以 send_photo 形式发送排行榜（caption 为文本）
- 公用方法：
    * send_envelope_message(message, envelope_id, lang) → 在“当前会话”发“立即抢”卡片（兼容旧用法）
    * send_envelope_card_to_chat(bot, chat_id, envelope_id, lang) → 在“指定 chat_id”发卡片（✅ 新增，推荐）
- 新增：hb:mvp_send:{eid} → 由本轮 MVP 复用参数创建并发送下一轮红包（已加入余额校验与原子扣款）
- ✅ 变更：群内排行榜键盘会移除“hb:mvp_send”按钮，仅私聊给 MVP 专属按钮；非 MVP 即使拿到回调也会在入口处被拦截。

【新增】封面选择与附加（仅管理员）：
- hb:cover:pick:{eid}:{chat_id}:{page} → 打开封面选择器（分页）
- hb:cover:preview:{cover_id}          → 在当前会话预览该封面
- hb:cover:use:{eid}:{chat_id}:{cover_id} → 将封面复制/发送到目标 chat，然后发送“立即抢”卡片
- show_cover_picker(message_or_cb, envelope_id, chat_id, lang="zh") → 供其他路由直接调用的便捷入口
"""
from __future__ import annotations
import asyncio
import time
import re
import logging
from typing import Tuple, Optional, List, Dict, Any
from collections import defaultdict
from decimal import Decimal, ROUND_DOWN  # ✅ 金额量化
from html import escape  # ✅ 显示祝福语时做 HTML 转义

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError, TelegramForbiddenError

from core.i18n.i18n import t
from core.utils.keyboards import hb_grab_kb, hb_rank_kb, back_home_kb
from models.envelope import (
    grab_share,
    get_envelope_summary,
    list_envelope_claims,
    get_lucky_winner,
    HBDuplicatedGrab,
    HBFinished,
    HBNotFound,
    HBError,
    Envelope,
    create_envelope,       # ✅ 用于创建新红包
    # —— 新增导入：MVP 私聊“只发一次”的持久化幂等占位 —— #
    claim_mvp_dm_send_token,
    has_mvp_dm_sent,
)
from models.db import get_session
from models.user import User, get_balance, update_balance  # ✅ 引入余额接口

# 【新增】封面数据
from models.cover import list_covers, get_cover_by_id

# —— 关键：向外暴露 router —— #
router = Router()
__all__ = ["router", "send_envelope_card_to_chat", "send_envelope_message", "show_cover_picker"]

log = logging.getLogger("hongbao")
from monitoring.metrics import counter as metrics_counter, histogram as metrics_histogram

_HONGBAO_COUNTER = metrics_counter(
    "hongbao_operation_total",
    "Count of hongbao interactions.",
    label_names=("operation", "status"),
)
_HONGBAO_LATENCY = metrics_histogram(
    "hongbao_operation_seconds",
    "Duration of hongbao interactions (seconds).",
    label_names=("operation", "status"),
)

# ====== 本地内存策略 ======
_THROTTLE: dict[tuple[int, int], float] = {}   # (user_id, eid) -> last_ts
_DUP_TIPPED: set[tuple[int, int]] = set()      # 已对“重复领取”提示过的 (user_id, eid)

THROTTLE_SEC = 1.0
SHORT_RETRY_SEC = 1.0
TOP_N = 10  # 排行展示前 N 名

# ====== 结果面板去重：每个 eid 仅保留一个消息 ======
_ENV_RANK_MSG: dict[int, tuple[int, int]] = {}  # eid -> (chat_id, message_id)
_ENV_RANK_LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# ====== 安全发送封装（止血级）：自动等待 RetryAfter + 按 chat 串行限流 ======
_CHAT_LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# 【新增】管理员判断（与 admin.py 同步策略）
try:
    from settings import is_admin as _is_admin  # 优先根目录 settings
except Exception:
    try:
        from config.settings import is_admin as _is_admin
    except Exception:
        def _is_admin(_uid: int) -> bool:
            return False

async def _wait_retry_after(e: TelegramRetryAfter):
    # 官方告知的秒数 + 1s 缓冲
    await asyncio.sleep(getattr(e, "retry_after", 1) + 1)

async def safe_send_message(bot, chat_id: int, *args, **kwargs):
    """
    在同一个 chat 内串行化发送；命中 RetryAfter 时自动等待并重试；
    对 BadRequest/Forbidden 直接抛出；网络错误做简单退避。
    """
    lock = _CHAT_LOCKS[int(chat_id)]
    async with lock:
        while True:
            try:
                resp = await bot.send_message(chat_id, *args, **kwargs)
                # 轻微节流，避免同群连续操作触发限流
                await asyncio.sleep(1.0)
                return resp
            except TelegramRetryAfter as e:
                await _wait_retry_after(e)
            except (TelegramBadRequest, TelegramForbiddenError):
                raise
            except TelegramNetworkError:
                await asyncio.sleep(2)

async def safe_send_photo(bot, chat_id: int, *args, **kwargs):
    """
    同上，媒体发送稍微慢一点。
    """
    lock = _CHAT_LOCKS[int(chat_id)]
    async with lock:
        while True:
            try:
                resp = await bot.send_photo(chat_id, *args, **kwargs)
                await asyncio.sleep(1.5)
                return resp
            except TelegramRetryAfter as e:
                await _wait_retry_after(e)
            except (TelegramBadRequest, TelegramForbiddenError):
                raise
            except TelegramNetworkError:
                await asyncio.sleep(2)

async def safe_send_animation(bot, chat_id: int, *args, **kwargs):
    lock = _CHAT_LOCKS[int(chat_id)]
    async with lock:
        while True:
            try:
                resp = await bot.send_animation(chat_id, *args, **kwargs)
                await asyncio.sleep(1.5)
                return resp
            except TelegramRetryAfter as e:
                await _wait_retry_after(e)
            except (TelegramBadRequest, TelegramForbiddenError):
                raise
            except TelegramNetworkError:
                await asyncio.sleep(2)

# ========= 回调安全应答（解决 query is too old） =========
async def safe_answer(cb: CallbackQuery, text: str | None = None, show_alert: bool = False):
    """
    安全地应答回调。回调过期（query is too old / id invalid）时忽略，不再抛错。
    """
    try:
        await cb.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as e:
        msg = str(e)
        if "query is too old" in msg or "query ID is invalid" in msg:
            log.warning(f"cb.answer ignored: {msg}")
            return
        raise

# ---------- i18n & 语言 ----------
def _t_first(keys: List[str], lang: str, fallback: str = "") -> str:
    """
    依次尝试 keys 中的文案键，返回第一个命中的；都为空则返回 fallback。
    """
    for k in keys:
        try:
            v = t(k, lang)
            if v:
                return v
        except Exception:
            pass
    return fallback


_SUPPORTED_LANGS = {"zh", "en", "fr", "de", "es", "hi", "vi", "th"}
def _canon_lang(code: str | None) -> str:
    """
    语言规范化：
    - 完整命中：直接返回（如 'fr'）
    - 地区码回退：'fr-ca' -> 'fr'
    - 历史兼容：旧数据 zh/en 仍然有效
    - 兜底：默认 zh（与你项目现状一致），但不再把合法的 fr/de/es/hi/vi/th 压回 zh
    """
    default = "zh"
    if not code:
        return default
    c = str(code).strip().lower().replace("_", "-")
    if not c:
        return default
    if c in _SUPPORTED_LANGS:
        return c
    # 'fr-ca' -> 'fr'
    primary = c.split("-", 1)[0]
    if primary in _SUPPORTED_LANGS:
        return primary
    # 历史兼容
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"
    return default


def _db_lang_or_fallback(user_id: int, fallback_user) -> str:
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u and getattr(u, "language", None):
            return _canon_lang(u.language)
    return _canon_lang(getattr(fallback_user, "language_code", None))


# ------------------- 用户名、头像解析 -------------------
async def _resolve_display_name(
    bot,
    user_id: int,
    group_chat_id: Optional[int] = None
) -> str:
    """
    返回用于 HTML 文本的显示名：
      优先 @username；
      没有 username → 使用可点击姓名（tg://user?id=...）；
      再没有姓名 → 退回纯 ID 字符串。
    """
    # 1) DB 里有 username
    try:
        with get_session() as s:
            u = s.query(User).filter_by(tg_id=int(user_id)).first()
            if u and u.username:
                return f"@{u.username}"
    except Exception:
        pass

    # 2) 从群获取（最可靠）
    if group_chat_id:
        try:
            member = await bot.get_chat_member(group_chat_id, int(user_id))
            u = getattr(member, "user", None)
            if u:
                if getattr(u, "username", None):
                    return f"@{u.username}"
                full_name = " ".join(filter(None, [getattr(u, "first_name", None), getattr(u, "last_name", None)])) or str(user_id)
                return f'<a href="tg://user?id={user_id}">{full_name}</a>'
        except Exception:
            pass

    # 3) 直接拉 user 对象（用户与 bot 有过交互时可取到）
    try:
        u = await bot.get_chat(int(user_id))
        if getattr(u, "username", None):
            return f"@{u.username}"
        full_name = " ".join(filter(None, [getattr(u, "first_name", None), getattr(u, "last_name", None)])) or str(user_id)
        return f'<a href="tg://user?id={user_id}">{full_name}</a>'
    except Exception:
        pass

    # 4) 兜底
    return str(user_id)


async def _get_user_avatar_file_id(bot, user_id: int) -> Optional[str]:
    """
    取用户头像 file_id（第一张）。要求机器人能访问到该用户的头像：
    - 对群成员通常没问题；
    - 若用户隐私设置较严格，有可能取不到。
    """
    try:
        photos = await bot.get_user_profile_photos(int(user_id), limit=1)
        if photos and getattr(photos, "total_count", 0) > 0:
            sizes = photos.photos[0]
            if sizes:
                return sizes[-1].file_id
    except Exception as e:
        log.debug("get_user_avatar_file_id failed for %s: %s", user_id, e)
    return None


def _fmt_amount(token: str, amount: float) -> str:
    """
    展示金额：USDT/TON 保留 2 位小数；POINT 取整。
    """
    tok = (token or "").upper()
    if tok in ("USDT", "TON"):
        return f"{amount:.2f}"
    return str(int(round(amount)))

# ========= 金额量化 & 余额事务工具 =========
def quant_amt(token: str, value) -> Decimal:
    """
    将任意输入金额量化为对应币种的记账精度：
    - USDT/TON -> 0.01
    - 其他（如积分）-> 整数
    """
    tok = (token or "").upper()
    if tok in ("USDT", "TON"):
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    # 积分等
    return Decimal(int(Decimal(str(value))))

def calc_total_need(token: str, amount_total: Decimal, fee_rate: Decimal = Decimal("0")) -> Decimal:
    # 若有手续费，从 settings 里读后传进来；默认 0
    need = amount_total * (Decimal("1") + fee_rate)
    return quant_amt(token, need)

async def precheck_balance(session, user_id: int, token: str, total_need: Decimal) -> bool:
    current = get_balance(session, user_id, token)
    return Decimal(str(current)) >= total_need

def send_envelope_with_debit(user_obj: User, chat_id: int, token: str, amount_total: Decimal, shares: int, memo: str, **kwargs):
    """
    在一个事务里：原子扣款 -> 建红包 -> 提交。
    - 余额不足由 update_balance 抛 ValueError("INSUFFICIENT_BALANCE")。
    - 任一异常会回滚扣款，保证“不扣钱就不发包 / 失败就不扣钱”。
    """
    op = "send"
    start = time.perf_counter()
    try:
        need = calc_total_need(token, amount_total)
        with get_session() as s:
            u = s.query(User).filter_by(id=user_obj.id).first()
            if not u:
                raise ValueError("NO_USER")
            update_balance(s, u, token=token, delta=-need)
            env = create_envelope(
                s,
                chat_id=int(chat_id),
                sender_tg_id=int(u.tg_id),
                mode=token,
                total_amount=amount_total,
                shares=int(shares),
                note=memo or "",
                activate=True,
                **kwargs
            )
            s.commit()

        duration = time.perf_counter() - start
        _HONGBAO_COUNTER.inc(operation=op, status="success")
        _HONGBAO_LATENCY.observe(duration, operation=op, status="success")
        log.info(
            "hongbao.send.success user=%s chat=%s token=%s shares=%s envelope=%s",
            getattr(user_obj, "tg_id", None),
            chat_id,
            token,
            shares,
            getattr(env, "id", None),
        )
        return env
    except ValueError as exc:
        duration = time.perf_counter() - start
        reason = str(exc) or "value_error"
        status = reason.lower().replace(" ", "_")
        _HONGBAO_COUNTER.inc(operation=op, status=status)
        _HONGBAO_LATENCY.observe(duration, operation=op, status=status)
        log.warning(
            "hongbao.send.failed user=%s chat=%s reason=%s",
            getattr(user_obj, "tg_id", None),
            chat_id,
            reason,
        )
        raise
    except Exception:
        duration = time.perf_counter() - start
        _HONGBAO_COUNTER.inc(operation=op, status="unexpected")
        _HONGBAO_LATENCY.observe(duration, operation=op, status="unexpected")
        log.exception(
            "hongbao.send.unexpected user=%s chat=%s",
            getattr(user_obj, "tg_id", None),
            chat_id,
        )
        raise


async def _build_round_rank_text_and_photo(
    bot,
    envelope_id: int,
    lang: str = "zh",
) -> tuple[str, Optional[str]]:
    """
    返回 (排行榜文本, 幸运之星头像 file_id 或 None)
    文本使用 HTML 格式，用户名优先显示 @username，否则使用可点击姓名（tg://user?id=...）。
    """
    # 读取红包所在群 + 祝福语
    with get_session() as s:
        env = s.query(Envelope).filter(Envelope.id == int(envelope_id)).first()
        chat_id = int(getattr(env, "chat_id", 0)) if env else 0
        memo_raw = (getattr(env, "note", "") or "").strip()
    memo = escape(memo_raw)

    # 先拿币种，保证金额格式正确
    try:
        summary = get_envelope_summary(envelope_id) or {}
        token_disp = str(summary.get("mode", "")).upper()
        total = float(summary.get("total_amount") or 0.0)
        shares = int(summary.get("shares") or 0)
    except Exception:
        token_disp = ""
        total, shares = 0.0, 0

    try:
        claims = list_envelope_claims(envelope_id)
    except HBNotFound:
        # 只显示标题 + 祝福语的兜底
        lines = [_t_first(["rank.round_title"], lang, "本轮最佳手气")]
        if memo:
            memo_label = _t_first(["env.memo_label", "hongbao.confirm_page.memo_label"], lang, "📝 祝福语：")
            lines.append(f"{memo_label}{memo}")
        return "\n".join(lines), None

    if not claims:
        lines = [_t_first(["rank.round_title"], lang, "本轮最佳手气")]
        if memo:
            memo_label = _t_first(["env.memo_label", "hongbao.confirm_page.memo_label"], lang, "📝 祝福语：")
            lines.append(f"{memo_label}{memo}")
        return "\n".join(lines), None

    def _get(item: Any, key: str, default=None):
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    lines: List[str] = [_t_first(["rank.round_title"], lang, "本轮最佳手气")]
    # 先补“本轮摘要 + 祝福语”
    if total and shares:
        head_total = _t_first(["hongbao.summary.total", "hongbao_summary.total"], lang, "总额：{amount} {token}，{shares} 份") \
            .format(amount=f"{total:.2f}", token=token_disp, shares=shares)
        lines.append(head_total)
    if memo:
        memo_label = _t_first(["env.memo_label", "hongbao.confirm_page.memo_label"], lang, "📝 祝福语：")
        lines.append(f"{memo_label}{memo}")
    lines.append("")  # 空行

    # Top N
    for i, c in enumerate(claims[:TOP_N], start=1):
        uid = int(_get(c, "user_tg_id") or _get(c, "user_id") or 0)
        amount_val = float(_get(c, "amount") or 0.0)
        disp = await _resolve_display_name(bot, uid, chat_id)
        token_part = f" {token_disp}" if token_disp else ""
        lines.append(f"{i}. {disp} — {_fmt_amount(token_disp, amount_val)}{token_part}")

    # 幸运之星
    lucky_photo_id = None
    try:
        lw = get_lucky_winner(envelope_id)
    except Exception:
        lw = None

    if lw:
        name_disp = await _resolve_display_name(bot, int(lw[0]), chat_id)
        lines.append("")
        lines.append(
            _t_first(["rank.lucky"], lang, "🏅 MVP：{name} ✨ （{amount} {token}）")
            .format(name=name_disp, amount=_fmt_amount(token_disp, float(lw[1])), token=token_disp)
        )
        # 头像
        lucky_photo_id = await _get_user_avatar_file_id(bot, int(lw[0]))

    return "\n".join(lines), lucky_photo_id


def _append_today_button(kb: InlineKeyboardMarkup | None, lang: str) -> InlineKeyboardMarkup:
    """
    （已停用）原本用于在排行榜键盘下追加『📊 今日战绩』。
    现在返回原键盘本身，不再追加任何按钮。
    """
    if isinstance(kb, InlineKeyboardMarkup) and getattr(kb, "inline_keyboard", None):
        return kb
    return InlineKeyboardMarkup(inline_keyboard=[])

# === 仅群内使用：移除任何“hb:mvp_send:*”按钮，避免非 MVP 看见 ===
def _kb_without_mvp(base: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    if not isinstance(base, InlineKeyboardMarkup) or not getattr(base, "inline_keyboard", None):
        return InlineKeyboardMarkup(inline_keyboard=[])
    new_rows: List[List[InlineKeyboardButton]] = []
    for row in base.inline_keyboard:
        new_row: List[InlineKeyboardButton] = []
        for btn in row:
            data = getattr(btn, "callback_data", None)
            if isinstance(data, str) and data.startswith("hb:mvp_send:"):
                continue  # 过滤 MVP 按钮
            new_row.append(btn)
        if new_row:
            new_rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=new_rows)

# === 私聊给 MVP 的专属按钮 ===
def _mvp_dm_keyboard(eid: int, lang: str) -> InlineKeyboardMarkup:
    txt = _t_first(["rank.mvp_send_btn", "hongbao.mvp_send_btn"], lang, "⚡ 复刻一发")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=txt, callback_data=f"hb:mvp_send:{eid}")]
    ])

# ======== 进程内幂等标记（只做快速拦截；真正幂等以数据库为准） ========
_MVP_DM_SENT: set[int] = set()

async def _dm_mvp_once_under_lock(bot, eid: int, lang: str) -> bool:
    """
    仅在已持有 _ENV_RANK_LOCKS[eid] 的上下文中调用：
    - 先检查进程内集合，命中即直接跳过；
    - 再做数据库原子占位（claim_mvp_dm_send_token），只有成功占位的一次才发送 DM；
    - 无论发送结果成功与否，均把 eid 加入进程内集合，达到“最多一次”的目标。
    返回：True=本次拿到资格并尝试发送；False=未发送（此前已发过或并发被他人占位）。
    """
    if eid in _MVP_DM_SENT:
        return False

    # 数据库原子占位：从 False -> True；受影响行=1 说明拿到资格
    try:
        got_token = claim_mvp_dm_send_token(eid)
    except Exception as e:
        # 出现异常时，为避免刷屏，按“认为已占位”处理（只做一次）
        log.warning("claim_mvp_dm_send_token failed for eid=%s: %s", eid, e)
        got_token = False

    sent = False
    if got_token:
        try:
            lw = get_lucky_winner(eid)
            if lw:
                try:
                    await safe_send_message(
                        bot,
                        int(lw[0]),
                        _t_first(["rank.mvp_dm_tip"], lang, "恭喜成为本轮 MVP！"),
                        reply_markup=_mvp_dm_keyboard(eid, lang),
                    )
                    sent = True
                except Exception:
                    # 发送失败不回滚占位，保持“最多一次”
                    pass
        except Exception:
            pass

    # 无论发送是否成功，都标记本进程“已处理过”
    _MVP_DM_SENT.add(eid)
    return sent


# ========= 「按 chat_id 发送」的通用发卡片方法（✅ 新增，推荐在所有场景使用） =========
async def send_envelope_card_to_chat(bot, chat_id: int, envelope_id: int, lang: str = "zh"):
    """
    在目标 chat_id 发送“立即抢”卡片（内含祝福语）。
    """
    # 读摘要 + 祝福语
    try:
        summary = get_envelope_summary(envelope_id)
    except Exception as e:
        log.exception("get_envelope_summary failed: %s", e)
        await safe_send_message(bot, chat_id, _t_first(["common.not_available"], lang, "暂不可用"), reply_markup=back_home_kb(lang))
        return

    with get_session() as s:
        env = s.query(Envelope).filter(Envelope.id == int(envelope_id)).first()
        memo_raw = (getattr(env, "note", "") or "").strip() if env else ""
    memo = escape(memo_raw)

    total = float(summary["total_amount"])
    shares = summary["shares"]
    grabbed = summary["grabbed_shares"]
    left = shares - grabbed
    token = (summary.get("mode") or "").upper()

    parts: List[str] = []
    parts.append(_t_first(["hongbao.summary.title", "hongbao_summary.title"], lang, "本轮总结"))
    parts.append(
        _t_first(["hongbao.summary.total", "hongbao_summary.total"], lang, "总额：{amount} {token}，{shares} 份")
        .format(amount=f"{total:.2f}", token=token, shares=shares)
    )
    if memo:
        memo_label = _t_first(["env.memo_label", "hongbao.confirm_page.memo_label"], lang, "📝 祝福语：")
        parts.append(f"{memo_label}{memo}")
    parts.append(
        _t_first(["hongbao.summary.left", "hongbao_summary.left"], lang, "剩余：{left} 份")
        .format(left=left)
    )

    text = "\n".join(parts)
    await safe_send_message(bot, chat_id, text, parse_mode="HTML", reply_markup=hb_grab_kb(envelope_id, lang))

# ========= 兼容旧代码：仍然接受 Message，发到当前会话 =========
async def send_envelope_message(message: Message, envelope_id: int, lang: str = "zh"):
    """
    兼容旧用法：在 message.chat.id 发送“立即抢”卡片。
    新项目/新场景请优先使用 send_envelope_card_to_chat(bot, chat_id, ...).
    """
    await send_envelope_card_to_chat(message.bot, int(message.chat.id), envelope_id, lang)


# ========= 抢红包 =========
@router.callback_query(F.data.regexp(r"^hb:grab:\d+$"))
async def hb_grab(cb: CallbackQuery):
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    m = re.match(r"^hb:grab:(\d+)$", cb.data or "")
    if not m:
        await safe_answer(cb, _t_first(["errors.bad_request", "common.bad_request"], lang, "请求有误"), show_alert=True)
        return
    eid = int(m.group(1))
    uid = cb.from_user.id

    # ✅ 先 ACK，避免 query 过期
    await safe_answer(cb, None, show_alert=False)

    # 1) 节流：同用户同红包 1s
    now = time.time()
    last = _THROTTLE.get((uid, eid), 0.0)
    if now - last < THROTTLE_SEC:
        await safe_answer(cb, t("common.wait_emoji", lang), show_alert=False)
        return
    _THROTTLE[(uid, eid)] = now

    # 2) 执行抢
    grabbed_amount: Optional[float] = None
    grabbed_token: Optional[str] = None
    last_share = False

    try:
        res = grab_share(eid, uid)
        # 兼容不同返回格式
        if isinstance(res, tuple) and len(res) >= 2:
            grabbed_amount = float(res[0])
            grabbed_token = (res[1] or "").upper()
            last_share = bool(res[2]) if len(res) >= 3 else False
        elif isinstance(res, dict):
            grabbed_amount = float(res.get("amount") or 0.0)
            grabbed_token = (res.get("token") or "").upper()
            last_share = bool(res.get("is_last") or res.get("last") or False)
        else:
            # 保底：查询 token；金额未知时给 0
            summary = get_envelope_summary(eid)
            grabbed_token = (summary.get("mode") or "").upper()
            grabbed_amount = 0.0
    except HBDuplicatedGrab:
        # ✅ 再次点击：只弹出提示窗（不再发私聊）
        dup_txt = _t_first(["hongbao.grab_dup", "hongbao_result.duplicate"], lang, "你已经抢过这个红包啦～")
        await safe_answer(cb, dup_txt, show_alert=True)
        return
    except HBFinished:
        # ✅ 已抢完：弹窗提示 + 只保留一个「本轮最佳手气」面板（MVP 私聊仅一次）
        await safe_answer(cb, _t_first(["hongbao.finished_tip", "hongbao.finished"], lang, "红包已抢完啦～"), show_alert=True)

        async with _ENV_RANK_LOCKS[eid]:
            # —— 仅尝试一次：数据库原子占位 + 进程内集合 —— #
            await _dm_mvp_once_under_lock(cb.message.bot, eid, lang)

            text, photo_id = await _build_round_rank_text_and_photo(cb.message.bot, eid, lang)
            base_kb = hb_rank_kb(eid, lang, show_next=True)
            kb = _kb_without_mvp(_append_today_button(base_kb, lang))

            # 若已有“结果面板”，优先编辑它（文本优先 → 失败回退 caption）
            exist = _ENV_RANK_MSG.get(eid)
            if exist:
                try:
                    await cb.message.bot.edit_message_text(
                        text,
                        chat_id=int(exist[0]),
                        message_id=int(exist[1]),
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception:
                    try:
                        await cb.message.bot.edit_message_caption(
                            chat_id=int(exist[0]),
                            message_id=int(exist[1]),
                            caption=text,
                            parse_mode="HTML",
                            reply_markup=kb,
                        )
                    except Exception:
                        pass
                return

            # 否则尝试编辑当前卡片；失败才新发（文本优先 → 回退 caption）
            edited = False
            try:
                await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
                _ENV_RANK_MSG[eid] = (cb.message.chat.id, cb.message.message_id)
                edited = True
            except Exception:
                try:
                    await cb.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
                    _ENV_RANK_MSG[eid] = (cb.message.chat.id, cb.message.message_id)
                    edited = True
                except Exception:
                    edited = False

            if not edited:
                try:
                    if photo_id:
                        msg = await safe_send_photo(
                            cb.message.bot,
                            cb.message.chat.id,
                            photo=photo_id,
                            caption=text,
                            parse_mode="HTML",
                            reply_markup=kb,
                        )
                    else:
                        msg = await safe_send_message(
                            cb.message.bot,
                            cb.message.chat.id,
                            text,
                            parse_mode="HTML",
                            reply_markup=kb,
                        )
                    _ENV_RANK_MSG[eid] = (msg.chat.id, msg.message_id)
                except Exception:
                    pass
        return
    except HBNotFound:
        await safe_answer(cb, _t_first(["errors.not_found", "common.not_found"], lang, "未找到目标"), show_alert=True)
        return
    except HBError as e:
        # 业务异常信息本身可能就是后端生成的英文码，这里仅前置 ❌ 符号不再本地化
        await safe_answer(cb, f"❌ {e}", show_alert=True)
        return
    except Exception as e:
        log.exception("grab_share failed: %s", e)
        await safe_answer(cb, _t_first(["common.not_available"], lang, "暂不可用"), show_alert=True)
        return

    # 3) 成功到账 → 优先私聊通知（安全发送）
    ok_tpl = _t_first(
        ["hongbao.grab_ok", "hongbao_result.ok"],
        lang,
        "领取成功：{amount} {token}"
    )
    ok_text = ok_tpl.format(amount=_fmt_amount(grabbed_token or "", grabbed_amount or 0.0),
                            token=(grabbed_token or "").upper())
    try:
        await safe_send_message(cb.message.bot, uid, ok_text)
    except Exception:
        await safe_answer(cb, ok_text, show_alert=True)

    # 4) 短重试，刷新“剩余”
    await asyncio.sleep(SHORT_RETRY_SEC)
    try:
        summary = get_envelope_summary(eid)
        total = float(summary["total_amount"])
        shares = summary["shares"]
        grabbed = summary["grabbed_shares"]
        left = shares - grabbed
        token_for_txt = (summary.get("mode") or "").upper()
    except Exception as e:
        log.warning("summary after grab failed: %s", e)
        left = None
        total = 0.0
        shares = 0
        token_for_txt = ""

    # 5) 最后一份 → 排行榜；否则更新“剩余”（两者都兼容文本/媒体）
    if last_share or (left is not None and left <= 0):
        async with _ENV_RANK_LOCKS[eid]:
            await _dm_mvp_once_under_lock(cb.message.bot, eid, lang)

            text, photo_id = await _build_round_rank_text_and_photo(cb.message.bot, eid, lang)
            base_kb = hb_rank_kb(eid, lang, show_next=True)
            kb = _kb_without_mvp(_append_today_button(base_kb, lang))

            exist = _ENV_RANK_MSG.get(eid)
            if exist:
                try:
                    await cb.message.bot.edit_message_text(
                        text,
                        chat_id=int(exist[0]),
                        message_id=int(exist[1]),
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception:
                    try:
                        await cb.message.bot.edit_message_caption(
                            chat_id=int(exist[0]),
                            message_id=int(exist[1]),
                            caption=text,
                            parse_mode="HTML",
                            reply_markup=kb,
                        )
                    except Exception:
                        pass
            else:
                edited = False
                try:
                    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
                    _ENV_RANK_MSG[eid] = (cb.message.chat.id, cb.message.message_id)
                    edited = True
                except Exception:
                    try:
                        await cb.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
                        _ENV_RANK_MSG[eid] = (cb.message.chat.id, cb.message.message_id)
                        edited = True
                    except Exception:
                        edited = False

                if not edited:
                    try:
                        if photo_id:
                            msg = await safe_send_photo(
                                cb.message.bot,
                                cb.message.chat.id,
                                photo=photo_id,
                                caption=text,
                                parse_mode="HTML",
                                reply_markup=kb,
                            )
                        else:
                            msg = await safe_send_message(
                                cb.message.bot,
                                cb.message.chat.id,
                                text,
                                parse_mode="HTML",
                                reply_markup=kb,
                            )
                        _ENV_RANK_MSG[eid] = (msg.chat.id, msg.message_id)
                    except Exception:
                        pass

        await safe_answer(cb, t("common.ok_emoji", lang))
    else:
        # —— 非最后一份时也实时更新剩余份数 + 祝福语 —— #
        with get_session() as s:
            env = s.query(Envelope).filter(Envelope.id == int(eid)).first()
            memo_raw = (getattr(env, "note", "") or "").strip() if env else ""
        memo = escape(memo_raw)

        try:
            parts: List[str] = []
            parts.append(_t_first(["hongbao.summary.title", "hongbao_summary.title"], lang, "本轮总结"))
            parts.append(
                _t_first(["hongbao.summary.total", "hongbao_summary.total"], lang, "总额：{amount} {token}，{shares} 份")
                .format(amount=f"{total:.2f}", token=token_for_txt, shares=shares)
            )
            if memo:
                memo_label = _t_first(["env.memo_label", "hongbao.confirm_page.memo_label"], lang, "📝 祝福语：")
                parts.append(f"{memo_label}{memo}")
            parts.append(
                _t_first(["hongbao.summary.left", "hongbao_summary.left"], lang, "剩余：{left} 份")
                .format(left=left)
            )
            txt = "\n".join(parts)
            # ✅ 文本优先，失败回退为编辑 caption（封面媒体）
            try:
                await cb.message.edit_text(txt, parse_mode="HTML", reply_markup=hb_grab_kb(eid, lang))
            except Exception:
                try:
                    await cb.message.edit_caption(caption=txt, parse_mode="HTML", reply_markup=hb_grab_kb(eid, lang))
                except Exception:
                    pass
        except Exception:
            # 如果编辑失败，不影响后续体验
            pass
        await safe_answer(cb, t("common.ok_emoji", lang))


# ========= MVP 发红包：复用上一轮参数（新增余额校验 + 原子扣款 + 弹窗提示） =========
@router.callback_query(F.data.regexp(r"^hb:mvp_send:(\d+)$"))
async def hb_mvp_send(cb: CallbackQuery):
    """
    由本轮 MVP（最佳手气）复制上一轮参数并创建新红包，然后在“原红包所在群”发出“立即抢”卡片。
    安全性升级：
      - 进入即 ACK 回调，杜绝 query 过期；
      - 复制参数前先做余额预校验；
      - 创建红包采用“先扣款后建包”的单事务；
      - 余额不足或任一异常都会明确提示，并不会发出红包（且以弹窗告知）。
    """
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    m = re.match(r"^hb:mvp_send:(\d+)$", cb.data or "")
    if not m:
        await safe_answer(cb, _t_first(["errors.bad_request", "common.bad_request"], lang, "请求有误"), show_alert=True)
        return

    # ✅ 先 ACK，避免 query 过期
    await safe_answer(cb, _t_first(["common.processing"], lang, "处理中…"), show_alert=False)

    eid = int(m.group(1))
    uid = int(cb.from_user.id)

    # 1) 读取原红包，取参数
    with get_session() as s:
        env = s.query(Envelope).filter(Envelope.id == eid).first()
        if not env:
            await safe_answer(cb, _t_first(["errors.not_found", "common.not_found"], lang, "未找到目标"), show_alert=True)
            return

    # 2) 校验 MVP 身份
    try:
        lw = get_lucky_winner(eid)
    except Exception:
        lw = None
    if not lw or int(lw[0]) != uid:
        await safe_answer(cb, _t_first(["hongbao.errors.only_mvp"], lang, "仅限本轮 MVP 操作"), show_alert=True)
        return

    # 3) 计算需要金额，做预校验
    token = (env.mode.value if hasattr(env.mode, "value") else str(env.mode)).upper()
    amount_total = quant_amt(token, env.total_amount)  # Decimal
    need = calc_total_need(token, amount_total)

    with get_session() as s:
        ok = await precheck_balance(s, uid, token, need)
        if not ok:
            # ✅ 余额不足 —— 弹窗提示（i18n）
            base = _t_first(["env.errors.insufficient"], lang, "余额不足")
            await safe_answer(cb, f"{base} ({need} {token})", show_alert=True)
            return

    # 4) 原子扣款 + 新红包（单事务）
    try:
        # 先把当前用户 ORM 对象拿到（send_envelope_with_debit 内部会在事务里重查一次）
        with get_session() as s:
            u = s.query(User).filter((User.tg_id == uid)).first()
        if not u:
            await safe_answer(cb, _t_first(["errors.not_found"], lang, "未找到目标"), show_alert=True)
            return

        new_env = send_envelope_with_debit(
            user_obj=u,
            chat_id=int(env.chat_id),   # ✅ 新红包仍然发到原群
            token=token,
            amount_total=amount_total,
            shares=int(env.shares),
            memo=env.note or "",
        )
        new_id = int(new_env.id)
    except ValueError as e:
        if str(e) == "INSUFFICIENT_BALANCE":
            base = _t_first(["env.errors.insufficient"], lang, "余额不足")
            await safe_answer(cb, f"{base} ({need} {token})", show_alert=True)
            return
        await safe_answer(cb, f"❌ {str(e)}", show_alert=True)
        return
    except Exception as e:
        log.exception("mvp_send create_envelope failed: %s", e)
        await safe_answer(cb, _t_first(["common.not_available"], lang, "暂不可用"), show_alert=True)
        return

    # 5) ✅ 在“原红包所在群”发“立即抢”卡片
    try:
        await send_envelope_card_to_chat(cb.message.bot, int(env.chat_id), new_id, lang)
    except Exception as e:
        log.warning("send_envelope_card_to_chat failed for new_id=%s: %s", new_id, e)

    # 6) 私聊里给个确认提示
    try:
        await safe_send_message(
            cb.message.bot,
            uid,
            _t_first(["hongbao.mvp.success"], lang, "已按本轮参数再发一轮红包"),
        )
    except Exception:
        pass

    # 7) 回答弹窗
    await safe_answer(cb, _t_first(["hongbao.mvp.success"], lang, "已按本轮参数再发一轮红包"), show_alert=True)


# =====================================================================
# ===============  新增：封面选择 & 附加（仅管理员）  ===================
# =====================================================================

def _cover_pick_keyboard(eid: int, chat_id: int, page: int, lang: str, page_size: int = 6) -> InlineKeyboardMarkup:
    try:
        res = list_covers(page=page, page_size=page_size, only_enabled=True)  # (rows, total)
    except TypeError:
        res = list_covers(page=page, page_size=page_size)
    if isinstance(res, tuple) and len(res) == 2:
        items, total = res
    else:
        items, total = list(res), page * page_size

    rows: List[List[InlineKeyboardButton]] = []
    for c in items:
        cid = int(getattr(c, "id", 0))
        name = getattr(c, "slug", None) or (getattr(c, "title", None) or f"#{getattr(c,'message_id',None)}")
        # 每条两按钮：预览 / 选用
        prev_txt = _t_first(["admin.covers.preview_btn", "common.preview"], lang, "Preview")
        use_txt  = _t_first(["env.cover.use_this", "admin.covers.use_btn"], lang, "Use")
        rows.append([
            InlineKeyboardButton(text=f"🔍 {prev_txt} | #{cid}", callback_data=f"hb:cover:preview:{cid}"),
            InlineKeyboardButton(text=f"✅ {use_txt} | #{cid}", callback_data=f"hb:cover:use:{eid}:{chat_id}:{cid}"),
        ])

        # 再加一行名称展示，方便识别
        rows.append([InlineKeyboardButton(text=f"{name[:48]}", callback_data="noop")])

    # 分页
    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton(text=_t_first(["common.prev"], lang, "« 上一页"), callback_data=f"hb:cover:pick:{eid}:{chat_id}:{page-1}"))
    has_more = page * page_size < int(total)
    if has_more:
        nav.append(InlineKeyboardButton(text=_t_first(["common.next"], lang, "下一页 »"), callback_data=f"hb:cover:pick:{eid}:{chat_id}:{page+1}"))
    if nav:
        rows.append(nav)

    # 关闭
    rows.append([InlineKeyboardButton(text=_t_first(["common.close"], lang, "关闭"), callback_data="admin:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _send_cover_to_chat(bot, target_chat_id: int, cover) -> bool:
    """
    将封面发送到目标 chat：
    - 优先 copy_message(channel_id, message_id)
    - 失败回退：根据 media_type 使用 send_animation / send_photo（file_id）
    - 两者都失败返回 False
    """
    ch_id = getattr(cover, "channel_id", None)
    msg_id = getattr(cover, "message_id", None)
    file_id = getattr(cover, "file_id", None)
    media_type = (getattr(cover, "media_type", None) or "").lower()
    caption = getattr(cover, "title", None) or None

    # 优先 copy
    try:
        if ch_id and msg_id:
            await bot.copy_message(chat_id=int(target_chat_id), from_chat_id=int(ch_id), message_id=int(msg_id))
            return True
    except Exception as e:
        log.warning("copy_message cover failed: %s", e)

    # 回退：直接发
    try:
        if not file_id:
            return False
        if media_type == "animation":
            await safe_send_animation(bot, int(target_chat_id), animation=file_id, caption=caption)
        else:
            await safe_send_photo(bot, int(target_chat_id), photo=file_id, caption=caption)
        return True
    except Exception as e:
        log.warning("send cover by file_id failed: %s", e)
        return False

async def show_cover_picker(owner: Message | CallbackQuery, envelope_id: int, chat_id: int, lang: str = "zh"):
    """
    便捷入口：在当前会话发一条“选择封面”面板。
    可被其他路由（如 menu.py 的发包向导）直接调用。
    """
    text = _t_first(["hongbao.cover.pick_title"], lang, "请选择要附加的封面：")
    kb = _cover_pick_keyboard(int(envelope_id), int(chat_id), page=1, lang=lang)
    if isinstance(owner, Message):
        await owner.answer(text, reply_markup=kb)
    else:
        try:
            await owner.message.edit_text(text, reply_markup=kb)
        except Exception:
            await owner.message.answer(text, reply_markup=kb)

@router.callback_query(F.data.regexp(r"^hb:cover:pick:(\d+):(-?\d+):(\d+)$"))
async def hb_cover_pick(cb: CallbackQuery):
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    # 仅管理员可操作
    if not _is_admin(cb.from_user.id):
        await safe_answer(cb, _t_first(["admin.no_permission"], lang, "⛔ 你没有权限。"), show_alert=True)
        return

    m = re.match(r"^hb:cover:pick:(\d+):(-?\d+):(\d+)$", cb.data or "")
    eid = int(m.group(1))
    chat_id = int(m.group(2))
    page = int(m.group(3))

    text = _t_first(["hongbao.cover.pick_title"], lang, "请选择要附加的封面：")
    kb = _cover_pick_keyboard(eid, chat_id, page=page, lang=lang)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await safe_answer(cb)

@router.callback_query(F.data.regexp(r"^hb:cover:preview:(\d+)$"))
async def hb_cover_preview(cb: CallbackQuery):
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    m = re.match(r"^hb:cover:preview:(\d+)$", cb.data or "")
    cover_id = int(m.group(1))
    c = get_cover_by_id(cover_id)
    if not c:
        await safe_answer(cb, _t_first(["admin.covers.not_found"], lang, "未找到该封面"), show_alert=True)
        return

    ok = await _send_cover_to_chat(cb.message.bot, cb.message.chat.id, c)
    if not ok:
        hint = _t_first(["admin.covers.preview_fail_hint"], lang,
                        "可能原因：\n• 机器人未加入频道或没有“发布消息”权限；\n• 频道ID错误；\n• 记录缺少有效的 file_id。")
        try:
            await cb.message.answer(_t_first(["admin.covers.preview_fail"], lang, "❌ 预览失败。") + "\n\n" + hint)
        except TelegramBadRequest:
            pass
    await safe_answer(cb, _t_first(["admin.covers.preview_ok"], lang, "已发送预览"))

@router.callback_query(F.data.regexp(r"^hb:cover:use:(\d+):(-?\d+):(\d+)$"))
async def hb_cover_use(cb: CallbackQuery):
    """
    选用封面：发送封面到目标 chat，然后发送“立即抢”卡片。
    仅管理员可用，避免普通用户滥用。
    """
    lang = _db_lang_or_fallback(cb.from_user.id, cb.from_user)
    if not _is_admin(cb.from_user.id):
        await safe_answer(cb, _t_first(["admin.no_permission"], lang, "⛔ 你没有权限。"), show_alert=True)
        return

    m = re.match(r"^hb:cover:use:(\d+):(-?\d+):(\d+)$", cb.data or "")
    if not m:
        await safe_answer(cb, _t_first(["errors.bad_request", "common.bad_request"], lang, "请求有误"), show_alert=True)
        return
    eid = int(m.group(1))
    chat_id = int(m.group(2))
    cover_id = int(m.group(3))

    c = get_cover_by_id(cover_id)
    if not c:
        await safe_answer(cb, _t_first(["admin.covers.not_found"], lang, "未找到该封面"), show_alert=True)
        return

    # 1) 先把封面发到目标 chat
    ok = await _send_cover_to_chat(cb.message.bot, chat_id, c)
    if not ok:
        hint = _t_first(["admin.covers.copy_fail_hint"], lang,
                        "可能原因：\n• 机器人未加入频道或没有“发布消息”权限；\n• 频道ID填写错误（应为以 -100 开头的数值ID）。")
        try:
            await cb.message.answer(_t_first(["admin.covers.add_fail"], lang, "❌ 上传失败：{reason}").format(reason="copy/send failed") + "\n\n" + hint)
        except TelegramBadRequest:
            pass
        await safe_answer(cb)
        return

    # 2) 紧接着在目标 chat 发送“立即抢”卡片
    await send_envelope_card_to_chat(cb.message.bot, chat_id, eid, lang)

    # 3) UI 回应
    await safe_answer(cb, _t_first(["hongbao.cover.used_ok"], lang, "封面已附加并发送卡片"))


# ==============================  新增封面功能结束  ==============================
