# routers/envelope.py
# -*- coding: utf-8 -*-
"""
发红包向导（FSM） + 快捷红包（QUICK） + 深链/接力

更新点（本版改动）：
- 余额读取：优先 User 字段，全部为 0 时回退汇总 Ledger（防止出现负数错账）。
- 严格扣款：金额步先校验余额，再执行扣款；失败则不进入下一步。
- 退款票据：取消或创建失败时按票据幂等退款，避免出现负余额。
- i18n 安全文案：语言包缺键时，使用可读的中英双语兜底。
- 私有群跳转链接：支持 t.me/c/<internal>/<message_id> 生成直达消息链接。
- 媒体封面（可选）：有封面时把确认/总结文字放在 caption 中并挂按钮；无封面则降级为文本卡片。
- 目标会话投放失败（被踢、没权限等）：自动回退到当前会话，不中断流程。
- aiogram v3 键盘：仅使用 InlineKeyboardMarkup(inline_keyboard=...) 的方式构建，兼容 v3。
- ✅ 新增：红包“投放卡片”和“确认页”都显示发包人（可点击提及）。
- ✅ 新增：接力 hb:relay:<eid> 点击后，直接投放到“该 eid 原始所在群”，不再引导到机器人私聊。
- ✅ 新增：确认发送 env:confirm 前，若未发生预扣（如接力跳过金额步），做“余额二次校验+即时扣款”，不足即拦截。
"""

from __future__ import annotations

import re
import asyncio
import logging
import html
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple, Sequence, Dict, Any


from html import escape as _html_escape
from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
)

from services.google_logger import log_user_to_sheet
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from sqlalchemy import func

from config.settings import settings
from config.feature_flags import flags
from core.i18n.i18n import t
from core.utils.keyboards import (
    env_mode_kb, env_distribution_kb, env_location_kb,
    env_confirm_kb, env_back_kb, back_home_kb,
    env_amount_kb, env_shares_kb, env_memo_kb,
    hb_grab_kb,
)
from models.db import get_session
from models.user import (
    User, get_or_create_user, update_balance,
    set_last_target_chat, get_last_target_chat,
)
from models.envelope import (
    create_envelope, get_envelope_summary, get_lucky_winner,
    HBError, Envelope
)
from models.ledger import add_ledger_entry, LedgerType, Ledger

router = Router()
log = logging.getLogger("envelope")


# ================= FSM =================
class SendStates(StatesGroup):
    TG = State()        # 目标群确认/选择
    MODE = State()      # 选择币种
    AMOUNT = State()    # 输入金额
    SHARES = State()    # 输入份数
    DIST = State()      # 兼容保留：随机/固定（常规直跳过）
    LOC = State()       # 兼容保留：here/dm/pick
    PICK_CHAT = State() # 手动输入群
    MEMO = State()      # 祝福语
    CONFIRM = State()   # 确认
    COVER = State()     # 可选封面（仅用户进入时使用）


# ================= 语言及用户 =================
# 放在同一位置，紧挨着 _canon_lang
_SUPPORTED_LANGS = {"zh", "en", "fr", "de", "es", "hi", "vi", "th"}

def _canon_lang(code: str | None) -> str:
    """
    语言规范化：
    - 完整命中：直接返回（如 'fr'）
    - 地区码回退：'fr-ca' -> 'fr'
    - 历史兼容：旧数据 zh/en 仍然有效
    - 兜底：仍旧用 zh（按你项目的默认），但不再把有效多语压成 zh
    """
    default = "zh"
    if not code:
        return default
    c = str(code).strip().lower().replace("_", "-")
    if not c:
        return default
    if c in _SUPPORTED_LANGS:
        return c
    # fr-ca -> fr 这类主码回退
    primary = c.split("-", 1)[0]
    if primary in _SUPPORTED_LANGS:
        return primary
    # 历史兼容
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"
    return default



def _ensure_db_lang(user_id: int, tg_lang_code: str | None, username: str | None = None) -> str:
    """
    优先 DB；DB 为空才落盘 Telegram 语言。
    规范化遵循 _SUPPORTED_LANGS，不再把 fr/de/es/hi/vi/th 压回 zh。
    """
    init_lang = _canon_lang(tg_lang_code)
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first()
        if u is None:
            u = get_or_create_user(s, tg_id=user_id, username=username or None, lang=init_lang)
            s.commit()
            lang = init_lang
        else:
            raw = (u.language or init_lang or "zh").strip().lower()
            # 只做“支持集 + 主码回退”的规范化，不降级到中文
            if raw in _SUPPORTED_LANGS:
                lang = raw
            else:
                primary = raw.replace("_", "-").split("-", 1)[0]
                lang = primary if primary in _SUPPORTED_LANGS else init_lang
            if not u.language:
                u.language = lang
                s.add(u)
                s.commit()
        s.expunge_all()
        return lang



# ================= i18n 安全文案 =================
def _t_first(keys: Sequence[str], lang: str, fallback: str = "") -> str:
    """尝试多键（兼容不同 yml 键名），命中即返回，否则给 fallback。"""
    for k in keys:
        try:
            v = t(k, lang)
            if v:
                return v
        except Exception:
            pass
    return fallback


def _lbl(lang: str, zh: str, en: str) -> str:
    """仅内部兜底用；界面文本均优先走 t()."""
    return zh if lang == "zh" else en


DEFAULT_MEMO_ASK_ZH = (
    "📝 <b>填写祝福语（可选）</b>\n"
    "• 请在下方输入栏键入你想说的话\n"
    "• 支持表情与换行；不想写可点「跳过」继续\n"
    "• 示例：新的一天，大家加油！🎉"
)
DEFAULT_MEMO_ASK_EN = (
    "📝 <b>Optional Greeting</b>\n"
    "• Type your message in the input box below\n"
    "• Emojis and line breaks are supported; tap “Skip” if you prefer not to add one\n"
    "• Example: Have a great day! 🎉"
)


def _safe_i18n_text(key: str, lang: str, fallback_zh: str, fallback_en: str) -> str:
    try:
        val = t(key, lang)
        if val and str(val).strip():
            return val
    except Exception:
        pass
    return fallback_zh if lang == "zh" else fallback_en

# —— 新增：安全读取 flag（兼容 dict-like、_FlagsDict、对象属性）——
def _flag_get(src, key: str, default=None):
    """
    优先按字典取值，其次按属性取值；都没有则返回 default。
    用于兼容 aiogram v3 的 _FlagsDict 以及自定义的 config.feature_flags.flags。
    """
    try:
        # dict-like / _FlagsDict
        return src.get(key, default)
    except Exception:
        # 对象属性或 SimpleNamespace
        return getattr(src, key, default)



# ================= 扣款/退款票据 =================
@dataclass
class DeductTicket:
    token: str
    amount: Decimal
    refunded: bool = False


def _deduct_balance(user_id: int, token: str, amount: Decimal) -> None:
    """立即扣款（amount 必须 > 0；POINT 取整扣）"""
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first() or get_or_create_user(s, tg_id=user_id)
        if token.upper() in ("POINT", "POINTS"):
            update_balance(s, u, "POINT", -int(amount))
        else:
            update_balance(s, u, token.upper(), -Decimal(amount))
        s.commit()


def _refund_balance(user_id: int, ticket: DeductTicket) -> None:
    """按票据退款（幂等：已退款的忽略）"""
    if not ticket or ticket.refunded:
        return
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first() or get_or_create_user(s, tg_id=user_id)
        if ticket.token.upper() in ("POINT", "POINTS"):
            update_balance(s, u, "POINT", int(ticket.amount))
        else:
            update_balance(s, u, ticket.token.upper(), Decimal(ticket.amount))
        add_ledger_entry(
            s,
            user_tg_id=int(user_id),
            ltype=LedgerType.ADJUSTMENT,
            token=ticket.token.upper(),
            amount=Decimal(ticket.amount),
            ref_type="ENVELOPE_CANCEL",
            ref_id=None,
            note="Cancel & Refund before create",
        )
        s.commit()
    ticket.refunded = True


# ================= 实用工具 =================
def _safe_decimal(text: str) -> Optional[Decimal]:
    try:
        d = Decimal(str(text).strip())
        if d <= 0:
            return None
        return d
    except Exception:
        return None


def _is_group(chat_id: int) -> bool:
    """Telegram 群通常为负数 chat_id（-100xxxxx）。"""
    return int(chat_id) < 0


def _fmt_amount_for_display(token: str, amount: Decimal) -> str:
    if token.upper() in ("POINT", "POINTS"):
        return str(int(amount))
    return f"{float(amount):.2f}"


def _compose_summary_text(summary: Dict[str, Any], lang: str) -> str:
    total = summary["total_amount"]
    shares_total = summary["shares"]
    grabbed = summary["grabbed_shares"]
    left = shares_total - grabbed
    title = _t_first(
        ["hongbao.summary.title", "hongbao_summary.title"],
        lang,
        _lbl(lang, "📊 <b>本轮总结</b>", "📊 <b>Round Summary</b>"),
    )
    line_total = (
        t("hongbao.summary.total", lang, amount=f"{float(total):.2f}", token=summary["mode"].upper(), shares=shares_total)
        or t("hongbao_summary.total", lang, amount=f"{float(total):.2f}", token=summary["mode"].upper(), shares=shares_total)
        or _lbl(lang,
                f"💰 总额：{float(total):.2f} {summary['mode'].upper()}，{shares_total} 份",
                f"💰 Total: {float(total):.2f} {summary['mode'].upper()}, {shares_total} shares")
    )
    line_left = (
        t("hongbao.summary.left", lang, left=left)
        or t("hongbao_summary.left", lang, left=left)
        or _lbl(lang, f"📦 剩余：{left} 份", f"📦 Remaining: {left} shares")
    )
    return title + "\n" + line_total + "\n" + line_left


async def _auto_delete(bot, chat_id: int, message_id: int, delay: int = 8):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


# =============== 群标题、跳转链接工具 ===============
async def _chat_display_title(bot, chat_id: int) -> str:
    """
    返回群展示名称：
    - 有 @username 时显示 @username
    - 否则显示群 title
    - 都无时返回简短占位
    """
    try:
        ch = await bot.get_chat(int(chat_id))
        uname = getattr(ch, "username", None)
        title = getattr(ch, "title", None)
        if uname:
            return f"@{uname}"
        if title:
            return title
    except Exception:
        pass
    return "(group)"


def _group_link_for(post_chat_id: int, message_id: Optional[int], username: Optional[str]) -> Optional[str]:
    """
    仅在两种情况下返回直达链接：
    1) 公开群（有 @username）：t.me/<username>/<message_id>
    2) 私有“超级群”（chat_id 以 -100 开头）：t.me/c/<internal>/<message_id>
    普通私有群（负数但不以 -100 开头）不返回链接（返回 None）。
    """
    # 公开群：优先
    if username:
        return f"https://t.me/{username}/{message_id}" if message_id else f"https://t.me/{username}"

    # 私有超级群：chat_id 以 -100 开头
    try:
        cid = int(post_chat_id)
    except Exception:
        return None

    if cid < 0:
        s = str(abs(cid))                  # 例如 "1001234567890"
        if s.startswith("100"):            # 只有超级群才有 /c/ 内部跳转
            internal = s[3:]               # 去掉前缀 "100"
            return f"https://t.me/c/{internal}/{message_id}" if message_id else None

    # 其它情况（普通群无直达消息链接）
    return None


# =============== ✅ 发包人可点击提及 ===============
def _sender_mention(user) -> str:
    """
    生成一个可点击的发包人提及（HTML）。
    user 可以是 Message.from_user 或任何含 id / full_name 的对象。
    """
    uid = getattr(user, "id", None) or getattr(user, "tg_id", None)
    name = (
        getattr(user, "full_name", None)
        or getattr(user, "first_name", None)
        or getattr(user, "username", None)
        or "用户"
    )
    name = html.escape(str(name))
    return f'<a href="tg://user?id={int(uid)}">{name}</a>' if uid else name


# =============== 余额读取兜底（User / Ledger） ===============
def _pick_attr(obj, names: Sequence[str], default=0) -> Any:
    """在一组可能的属性名中选择第一个存在且非 None 的值。"""
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default


def _wallets_from_user_fields(u: User) -> Dict[str, Decimal]:
    """优先从 User 字段读取，支持多种常见字段名。"""
    usdt_raw = _pick_attr(u, ["usdt_balance", "usdt_available", "usdt", "balance_usdt", "usdt_free", "usdt_amount"], 0)
    ton_raw  = _pick_attr(u, ["ton_balance",  "ton_available",  "ton",  "balance_ton",  "ton_free",  "ton_amount" ], 0)
    pts_raw  = _pick_attr(u, ["point_balance","points",         "point","balance_point","point_available","score"], 0)

    def dec(x, is_point=False):
        try:
            return Decimal(int(x)) if is_point else Decimal(str(x))
        except Exception:
            return Decimal(0)

    return {
        "USDT": dec(usdt_raw),
        "TON":  dec(ton_raw),
        "POINT": dec(pts_raw, is_point=True)
    }


def _wallets_from_ledger(user_tg_id: int) -> Dict[str, Decimal]:
    """若 User 字段全为 0 或不存在，则从 Ledger 求和兜底。"""
    res = {"USDT": Decimal(0), "TON": Decimal(0), "POINT": Decimal(0)}
    with get_session() as s:
        rows = (
            s.query(Ledger.token, func.coalesce(func.sum(Ledger.amount), 0))
            .filter(Ledger.user_tg_id == int(user_tg_id))
            .group_by(Ledger.token)
            .all()
        )
    for token, total in rows:
        tok = (token or "").upper()
        try:
            total_dec = Decimal(str(total))
        except Exception:
            total_dec = Decimal(0)
        if tok in res:
            res[tok] = total_dec if tok != "POINT" else Decimal(int(total_dec))
    return res


def _get_wallets_for_user_id(user_tg_id: int) -> Dict[str, Decimal]:
    """统一入口：1) User 字段；2) 若全 0 则 Ledger 求和。"""
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=int(user_tg_id)).first()
        if u:
            f = _wallets_from_user_fields(u)
            if f["USDT"] != 0 or f["TON"] != 0 or f["POINT"] != 0:
                return f
    return _wallets_from_ledger(int(user_tg_id))


# =============== 目标群动作键盘（纯 v3 写法） ===============
def _tg_actions_kb(chat_id: int | None, lang: str) -> InlineKeyboardMarkup:
    rows = []
    if chat_id is not None:
        rows.append([InlineKeyboardButton(
            text=_t_first(["env.tg.use_this"], lang, _lbl(lang, "👉 使用此群继续", "👉 Use this group")),
            callback_data=f"env:tg:use:{int(chat_id)}"
        )])
    else:
        rows.append([InlineKeyboardButton(
            text=_t_first(["env.tg.bind_in_group", "env.tg.go_bind"], lang, _lbl(lang, "🪄 在群里绑定", "🪄 Bind in the group")),
            callback_data="env:tg:bind_help"
        )])
        rows.append([InlineKeyboardButton(
            text=_t_first(["env.tg.manual_pick", "env.loc.pick"], lang, _lbl(lang, "🎯 手动指定群聊", "🎯 Specific group")),
            callback_data="env:loc:pick"
        )])
    rows.append([InlineKeyboardButton(
        text=_t_first(["menu.back"], lang, _lbl(lang, "⬅️ 返回", "⬅️ Back")),
        callback_data="menu:main"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =============== 群定位解析：-100ID / @username / t.me/... ===============
async def _parse_target_chat_id(bot, ctx_message_or_cb, text: str | None) -> Optional[int]:
    """
    支持：
    - 直接数字ID：-100xxxxxxxxxx
    - @username
    - t.me/username
    - https://t.me/c/<internal_chat_id>/<post_id>  （取内部 id * -100）
    - 当前在群里发起：自动取当前 chat.id
    """
    # 当前群内发起：优先取当前 chat.id
    try:
        msg = ctx_message_or_cb.message if isinstance(ctx_message_or_cb, CallbackQuery) else ctx_message_or_cb
        if msg.chat and msg.chat.type in {"group", "supergroup"}:
            return msg.chat.id
    except Exception:
        pass

    if not text:
        return None
    s = text.strip()

    # 直接数字群 id
    if re.fullmatch(r"-100\d{5,}", s):
        return int(s)

    # t.me/c/ 内部链接
    m = re.search(r"t\.me/c/(\d+)/", s)
    if m:
        internal = int(m.group(1))
        return -100 * internal

    # t.me/username 或 @username
    m = re.search(r"(?:t\.me/)?@?([A-Za-z0-9_]{5,})$", s)
    if m:
        username = m.group(1)
        try:
            chat = await bot.get_chat(username)
            return chat.id
        except Exception:
            return None

    return None


# ================== 回调：开始 ==================
@router.callback_query(F.data.in_({"hb:start", "hb:menu"}))
async def hb_start(cb: CallbackQuery, state: FSMContext):
    tg_lang = getattr(cb.from_user, "language_code", None)
    lang = _ensure_db_lang(cb.from_user.id, tg_lang, cb.from_user.username)

    # 在群里点击：不在群里走向导，提示并引导到私聊
    if _is_group(cb.message.chat.id):
        try:
            me = await cb.message.bot.get_me()
            deep_url = f"https://t.me/{me.username}?start=send_g{cb.message.chat.id}" if getattr(me, "username", None) else "https://t.me/"
        except Exception:
            deep_url = "https://t.me/"

        tip = _t_first(["env.dm_hint"], lang,
                       _lbl(lang, "🔒 为保护隐私，已在私聊继续发红包。点下面蓝色按钮进入私聊。", "🔒 For privacy, let's continue in DM. Tap the blue button to proceed."))
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=_t_first(["env.continue_in_dm"], lang, _lbl(lang, "在私聊继续 ➡️", "Continue in DM ➡️")),
            url=deep_url
        )]])
        try:
            m = await cb.message.answer(tip, reply_markup=kb)
            asyncio.create_task(_auto_delete(cb.message.bot, cb.message.chat.id, m.message_id, delay=8))
        except Exception:
            pass

        # 尝试向用户私聊推送首页（若未开启过私聊会失败）
        try:
            with get_session() as s:
                gid, gtitle = get_last_target_chat(s, cb.from_user.id)
            text = (_t_first(["env.tg.choose"], lang) or _lbl(lang, "📌 请选择要使用的目标群：", "📌 Please choose the target group:"))
            if gid:
                chosen_line = _t_first(["env.tg.chosen_bold", "env.tg.chosen"], lang)
                if chosen_line:
                    text += "\n\n" + chosen_line.format(title=(gtitle or str(gid)))
                kb2 = _tg_actions_kb(gid, lang)
            else:
                unbound_line = _t_first(["env.tg.unbound_bold", "env.tg.unbound"], lang)
                tip_line = _t_first(["env.tg.unbound_tip"], lang)
                for ln in (unbound_line, tip_line):
                    if ln:
                        text += "\n\n" + ln
                kb2 = _tg_actions_kb(None, lang)

            await cb.message.bot.send_message(cb.from_user.id, text, reply_markup=kb2, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            pass

    # 首次在群内与机器人交互 → 记一笔（幂等：后文会把 first_seen_in_group 纳入去重范围）
        try:
            log_user_to_sheet(
                cb.from_user,
                source="first_seen_in_group",
                chat=cb.message.chat,
                inviter_user_id=None,
                joined_via_invite_link=False,
                note="first interaction in group (hb:start/hb:menu)"
            )
        except Exception as e:
            log.warning("first_seen log failed (hb_start in group): %s", e)

            await cb.answer()
            return




    # 私聊中点击 → 优先选择/确认目标群
    await state.clear()
    await state.set_state(SendStates.TG)
    with get_session() as s:
        gid, gtitle = get_last_target_chat(s, cb.from_user.id)

    text = (_t_first(["env.tg.choose"], lang) or _lbl(lang, "📌 请选择要使用的目标群：", "📌 Please choose the target group:"))
    if gid:
        chosen_line = _t_first(["env.tg.chosen_bold", "env.tg.chosen"], lang)
        if chosen_line:
            text += "\n\n" + chosen_line.format(title=(gtitle or str(gid)))
        kb = _tg_actions_kb(gid, lang)
    else:
        unbound_line = _t_first(["env.tg.unbound_bold", "env.tg.unbound"], lang)
        tip_line = _t_first(["env.tg.unbound_tip"], lang)
        for ln in (unbound_line, tip_line):
            if ln:
                text += "\n\n" + ln
        kb = _tg_actions_kb(None, lang)

    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    await cb.answer()


# ================== 目标群选择/绑定（回调） ==================
@router.callback_query(F.data.regexp(r"^env:tg:use:(-?\d+)$"))
async def tg_use(cb: CallbackQuery, state: FSMContext):
    """使用已有目标群 → 进入选择币种"""
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    m = re.match(r"^env:tg:use:(-?\d+)$", cb.data or "")
    if not m:
        await cb.answer(_lbl(lang, "无效操作", "Invalid action"), show_alert=True)
        return
    chat_id = int(m.group(1))
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(SendStates.MODE)
    text = (t("env.mode_title", lang) or _lbl(lang, "🔘 请选择币种", "🔘 Please choose a token"))
    try:
        await cb.message.edit_text(text, reply_markup=env_mode_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=env_mode_kb(lang))
    await cb.answer()



@router.callback_query(F.data == "env:tg:bind_help")
async def tg_bind_help(cb: CallbackQuery):
    """给出去群内 /start 的说明与深链回私聊"""
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    try:
        me = await cb.message.bot.get_me()
        deep = f"https://t.me/{me.username}?start=hb" if getattr(me, "username", None) else "https://t.me/"
    except Exception:
        deep = "https://t.me/"
    text = t("env.tg.bind_help", lang) or _lbl(
        lang,
        "🧩 绑定说明：将机器人邀请进你的群并授予发言权限，然后回到这里选择该群；或点击“🎯 手动指定群聊”输入 -100 开头的 chat_id。",
        "🧩 How to bind: invite the bot to your group and grant 'send messages', then return here to select it; or tap '🎯 Specific group' to enter -100 chat_id.",
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_t_first(["env.continue_in_dm"], lang, _lbl(lang, "在私聊继续 ➡️", "Continue in DM ➡️")), url=deep)],
        [InlineKeyboardButton(text=_t_first(["env.tg.manual_pick","env.loc.pick"], lang, _lbl(lang, "🎯 手动指定群聊", "🎯 Specific group")), callback_data="env:loc:pick")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await cb.answer()


# ================== 选择币种 ==================
@router.callback_query(F.data.regexp(r"^env:mode:(USDT|TON|POINT)$"))
async def choose_mode(cb: CallbackQuery, state: FSMContext):
    """
    进入金额步骤：
    - 顶部显示标题 env.amount.ask
    - 第二行显示 env.input_amount_tip（引导可在输入栏直接输入）
    - 第三行显示当前币种 env.current_token（带 <b>{token}</b>）
    - 键盘仅保留快捷金额与返回（在 keyboards.py 中已去掉“自定义/提示”行）
    """
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    m = re.match(r"^env:mode:(USDT|TON|POINT)$", cb.data or "")
    token = m.group(1) if m else "USDT"

    await state.update_data(mode=token)
    await state.set_state(SendStates.AMOUNT)

    title = t("env.amount.ask", lang) or _lbl(lang, "💰 请输入总金额", "💰 Enter total amount")
    hint  = t("env.input_amount_tip", lang) or _lbl(lang, "💡 也可以在下方输入栏直接输入任意金额数字", "💡 You can also type any amount below")
    current = t("env.current_token", lang, token=token) or _lbl(lang, f"当前币种：<b>{token}</b>", f"Current token: <b>{token}</b>")
    ask = f"{title}\n{hint}\n\n{current}"

    kb = env_amount_kb(token, lang)
    try:
        await cb.message.edit_text(ask, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(ask, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


# ================== 金额：公共处理 ==================
async def _process_amount_value(
    message: Message,
    state: FSMContext,
    value_str: str,
    actor_id: int,
    actor_username: Optional[str],
    actor_lang_code: Optional[str],
):
    lang = _ensure_db_lang(actor_id, actor_lang_code, actor_username)
    data = await state.get_data()
    token = (data.get("mode") or "USDT").upper()

    dec = _safe_decimal(value_str)
    if dec is None:
        await message.answer(
            _t_first(["env.errors.invalid_amount", "recharge.invalid_amount"], lang,
                     _lbl(lang, "❌ 金额无效，请重新输入。", "❌ Invalid amount, please re-enter.")))
        await message.answer(t("env.amount.ask", lang), reply_markup=env_amount_kb(token, lang))
        return

    min_amt = Decimal(str(_flag_get(flags, "HB_MIN_AMOUNT", getattr(settings, "HB_MIN_AMOUNT", "0.01"))))
    min_pts = Decimal(str(_flag_get(flags, "HB_POINT_MIN_AMOUNT", getattr(settings, "HB_POINT_MIN_AMOUNT", "1"))))

    # USDT/TON 量化为 2 位小数；POINT 取整
    real_amt = dec.quantize(Decimal("0.00")) if token in ("USDT", "TON") else Decimal(int(dec))

    if (token in ("USDT", "TON") and real_amt < min_amt) or (token == "POINT" and real_amt < min_pts):
        await message.answer(
            _t_first(["env.errors.invalid_amount", "recharge.invalid_amount"], lang,
                     _lbl(lang, "❌ 金额过小，请重新输入。", "❌ Amount too small, please re-enter.")))
        await message.answer(t("env.amount.ask", lang), reply_markup=env_amount_kb(token, lang))
        return

    # 统一读取余额
    bal_map = _get_wallets_for_user_id(actor_id)

    # 余额卡片展示
    try:
        balance_text = t(
            "balance.template", lang,
            usdt=f"{float(bal_map['USDT']):.2f}",
            ton=f"{float(bal_map['TON']):.2f}",
            points=str(int(bal_map['POINT'])),
            token=token,
            real_amt=f"{float(real_amt):.2f}" if token in ("USDT", "TON") else str(int(real_amt)),
        )
        if not balance_text:
            balance_text = (
                f"🧾 {_lbl(lang, '当前余额', 'Balance')}\n"
                f"💵 USDT: {float(bal_map['USDT']):.2f}\n"
                f"🪙 TON: {float(bal_map['TON']):.2f}\n"
                f"⭐ {_lbl(lang, '积分', 'Stars')}: {int(bal_map['POINT'])}\n\n"
                f"🎯 {_lbl(lang, '当前币种：', 'Token: ')}<b>{token}</b>\n"
                f"🧾 {_lbl(lang, '准备扣款：', 'To deduct: ')}"
                f"{f'{float(real_amt):.2f}' if token in ('USDT','TON') else str(int(real_amt))} {token}"
            )
        await message.answer(balance_text, parse_mode="HTML", disable_notification=True)
    except Exception:
        pass

    if bal_map[token] < real_amt:
        await message.answer(
            _t_first(["env.errors.insufficient", "common.not_available"], lang,
                    _lbl(lang, "💳 余额不足，请先充值或降低金额。", "💳 Insufficient balance — please recharge or reduce the amount.")))
        # 正确的“充值中心”标题/回退
        await message.answer(
            _t_first(["menu.recharge", "recharge.title"], lang, _lbl(lang, "💰 充值中心", "💰 Recharge Center")),
            reply_markup=back_home_kb(lang)
        )
        return


    # 扣款
    try:
        _deduct_balance(actor_id, token, real_amt)
    except ValueError as e:
        # 某些环境的 models/user.update_balance 仍可能在余额不足时抛出该错误
        if str(e).upper() == "INSUFFICIENT_BALANCE":
            # 不打印异常堆栈，直接友好提示
            log.warning("deduct failed due to insufficient balance (actor=%s, token=%s, amount=%s)",
                        actor_id, token, real_amt)
            await message.answer(
                _t_first(["env.errors.insufficient"], lang,
                        _lbl(lang, "💳 余额不足，请先充值或降低金额。", "💳 Insufficient balance — please recharge or reduce the amount.")))
            await message.answer(
                _t_first(["menu.recharge", "recharge.title"], lang, _lbl(lang, "💰 充值中心", "💰 Recharge Center")),
                reply_markup=back_home_kb(lang)
            )
            return
        # 其它 ValueError 走通用兜底
        log.warning("deduct failed (ValueError): %s", e)
        await message.answer(_lbl(lang, "扣款失败，请稍后重试。", "Deduction failed. Please try again later."))
        await message.answer(t("menu.back", lang), reply_markup=back_home_kb(lang))
        return
    except Exception as e:
        # 未知异常仍记录堆栈，但给用户友好提示
        log.exception("deduct failed (unexpected): %s", e)
        await message.answer(_lbl(lang, "扣款失败，请稍后重试。", "Deduction failed. Please try again later."))
        await message.answer(t("menu.back", lang), reply_markup=back_home_kb(lang))
        return


    await state.update_data(amount=str(real_amt), ticket={"token": token, "amount": str(real_amt), "refunded": False})
    await state.set_state(SendStates.SHARES)

    # 进入份数步骤
    title = t("env.shares.ask", lang) or _lbl(lang, "📦 请输入份数", "📦 Enter number of shares")
    hint  = t("env.input_shares_tip", lang) or _lbl(lang, "💡 也可以在下方输入栏直接输入任意份数", "💡 You can also type any number below")
    ask = f"{title}\n{hint}"
    await message.answer(ask, parse_mode="HTML", reply_markup=env_shares_kb(token, lang))


# ================== 返回：从金额回到选币种 ==================
@router.callback_query(F.data == "env:back:mode")
async def back_to_mode(cb: CallbackQuery, state: FSMContext):
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    await state.set_state(SendStates.MODE)
    text = (t("env.mode_title", lang) or _lbl(lang, "🔘 请选择币种", "🔘 Please choose a token"))
    try:
        await cb.message.edit_text(text, reply_markup=env_mode_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=env_mode_kb(lang))
    await cb.answer()


# ================== 返回：从份数回到金额 ==================
@router.callback_query(F.data == "env:back:amount")
async def back_to_amount(cb: CallbackQuery, state: FSMContext):
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    data = await state.get_data()
    token = (data.get("mode") or "USDT").upper()
    await state.set_state(SendStates.AMOUNT)

    title = t("env.amount.ask", lang) or _lbl(lang, "💰 请输入总金额", "💰 Enter total amount")
    hint  = t("env.input_amount_tip", lang) or _lbl(lang, "💡 也可以在下方输入栏直接输入任意金额数字", "💡 You can also type any amount below")
    current = t("env.current_token", lang, token=token) or _lbl(lang, f"当前币种：<b>{token}</b>", f"Current token: <b>{token}</b>")
    ask = f"{title}\n{hint}\n\n{current}"
    kb = env_amount_kb(token, lang)
    try:
        await cb.message.edit_text(ask, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(ask, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


# ================== 返回：从祝福语回到份数 ==================
@router.callback_query(F.data == "env:back:shares")
async def back_to_shares(cb: CallbackQuery, state: FSMContext):
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    data = await state.get_data()
    token = (data.get("mode") or "USDT").upper()
    await state.set_state(SendStates.SHARES)

    title = t("env.shares.ask", lang) or _lbl(lang, "📦 请输入份数", "📦 Enter number of shares")
    hint  = t("env.input_shares_tip", lang) or _lbl(lang, "💡 也可以在下方输入栏直接输入任意份数", "💡 You can also type any number below")
    ask = f"{title}\n{hint}"
    try:
        await cb.message.edit_text(ask, parse_mode="HTML", reply_markup=env_shares_kb(token, lang))
    except TelegramBadRequest:
        await cb.message.answer(ask, parse_mode="HTML", reply_markup=env_shares_kb(token, lang))
    await cb.answer()


# ================== 金额：按钮 ==================
@router.callback_query(F.data.regexp(r"^env:(?:amt|amount):(\d+(?:\.\d{1,6})?)$"))
async def amount_pick(cb: CallbackQuery, state: FSMContext):
    m = re.match(r"^env:(?:amt|amount):(\d+(?:\.\d{1,6})?)$", cb.data or "")
    if not m:
        lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
        await cb.answer(_lbl(lang, "无效金额", "Invalid amount"), show_alert=True)
        return
    val = m.group(1)
    await _process_amount_value(
        cb.message, state, val,
        actor_id=cb.from_user.id,
        actor_username=cb.from_user.username,
        actor_lang_code=getattr(cb.from_user, "language_code", None),
    )
    await cb.answer()


@router.callback_query(F.data == "env:amt:custom")
async def amount_custom(cb: CallbackQuery, state: FSMContext):
    # 键盘已去掉“自定义金额”按钮；保留该回调仅为向后兼容
    data = await state.get_data()
    token = (data.get("mode") or "USDT").upper()
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    await cb.message.answer(
        t("recharge.input_custom", lang) or _lbl(lang, "✏️ 请输入自定义金额（直接发送数字）", "✏️ Please enter a custom amount (send the number)"),
        reply_markup=env_amount_kb(token, lang)
    )
    await cb.answer()


# ================== 金额：文本 ==================
@router.message(SendStates.AMOUNT)
async def input_amount(msg: Message, state: FSMContext):
    await _process_amount_value(
        msg, state, str(msg.text or ""),
        actor_id=msg.from_user.id,
        actor_username=msg.from_user.username,
        actor_lang_code=getattr(msg.from_user, "language_code", None),
    )


# ================== 份数：公共处理 ==================
async def _process_shares_value(
    message: Message,
    state: FSMContext,
    value_str: str,
    actor_id: int,
    actor_username: Optional[str],
    actor_lang_code: Optional[str],
):
    lang = _ensure_db_lang(actor_id, actor_lang_code, actor_username)
    data = await state.get_data()
    token = (data.get("mode") or "USDT").upper()

    try:
        shares = int(value_str.strip())
    except Exception:
        await message.answer(
            t("env.errors.invalid_shares", lang) or _lbl(lang, "❌ 份数不正确，请重新输入。", "❌ Invalid shares, please re-enter."),
            reply_markup=env_shares_kb(token, lang)
        )
        return

    min_sh = int(_flag_get(flags, "HB_MIN_SHARES", getattr(settings, "HB_MIN_SHARES", 1)))
    max_sh = int(_flag_get(flags, "HB_MAX_SHARES", getattr(settings, "HB_MAX_SHARES", 100)))
    if shares < min_sh or shares > max_sh:

        await message.answer(
            t("env.errors.invalid_shares", lang) or _lbl(lang, "❌ 份数越界，请重新输入。", "❌ Shares out of range, please re-enter."),
            reply_markup=env_shares_kb(token, lang)
        )
        return

    await state.update_data(shares=shares)
    # ✅ 仅随机
    await state.update_data(dist="random")

    st = await state.get_data()
    has_target = bool(st.get("target_chat_id"))
    if has_target:
        await state.set_state(SendStates.MEMO)
        # —— 入口 1：文本流程（份数后）进入祝福语 —— #
        memo_text = _safe_i18n_text("env.memo.ask", lang, DEFAULT_MEMO_ASK_ZH, DEFAULT_MEMO_ASK_EN)
        await message.answer(memo_text, parse_mode="HTML", reply_markup=env_memo_kb(lang))
    else:
        await state.set_state(SendStates.LOC)
        await message.answer(
            t("env.loc.ask", lang) or _lbl(lang, "📍 请选择投放位置", "📍 Choose where to post"),
            parse_mode="HTML", reply_markup=env_location_kb(lang, allow_current=True, allow_dm=True)
        )


# ================== 份数：按钮 ==================
@router.callback_query(F.data.regexp(r"^env:shares:(\d+)$"))
async def shares_pick(cb: CallbackQuery, state: FSMContext):
    m = re.match(r"^env:shares:(\d+)$", cb.data or "")
    if not m:
        lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
        await cb.answer(_lbl(lang, "无效份数", "Invalid shares"), show_alert=True)
        return
    await _process_shares_value(
        cb.message, state, m.group(1),
        actor_id=cb.from_user.id,
        actor_username=cb.from_user.username,
        actor_lang_code=getattr(cb.from_user, "language_code", None),
    )
    await cb.answer()


@router.callback_query(F.data == "env:shares:custom")
async def shares_custom(cb: CallbackQuery, state: FSMContext):
    # 键盘已去掉“自定义份数”按钮；保留该回调仅为向后兼容
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    await cb.message.answer(
        t("env.input_shares_tip", lang) or _lbl(lang, "✍️ 也可直接发送份数数字", "✍️ You can also send the number of shares directly")
    )
    await cb.answer()


# ================== 份数：文本 ==================
@router.message(SendStates.SHARES)
async def input_shares(msg: Message, state: FSMContext):
    await _process_shares_value(
        msg, state, str(msg.text or ""),
        actor_id=msg.from_user.id,
        actor_username=msg.from_user.username,
        actor_lang_code=getattr(msg.from_user, "language_code", None),
    )


# ================== 分配方式（兼容保留） ==================
@router.callback_query(F.data.regexp(r"^env:dist:(random|fixed)$"))
async def choose_dist(cb: CallbackQuery, state: FSMContext):
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    m = re.match(r"^env:dist:(random|fixed)$", cb.data or "")
    dist = m.group(1) if m else "random"
    await state.update_data(dist=dist)

    st = await state.get_data()
    has_target = bool(st.get("target_chat_id"))
    if has_target:
        await state.set_state(SendStates.MEMO)
        memo_text = _safe_i18n_text("env.memo.ask", lang, DEFAULT_MEMO_ASK_ZH, DEFAULT_MEMO_ASK_EN)
        try:
            await cb.message.edit_text(memo_text, parse_mode="HTML", reply_markup=env_memo_kb(lang))
        except TelegramBadRequest:
            await cb.message.answer(memo_text, reply_markup=env_memo_kb(lang))
    else:
        await state.set_state(SendStates.LOC)
        try:
            await cb.message.edit_text(
                t("env.loc.ask", lang) or _lbl(lang, "📍 请选择投放位置", "📍 Choose where to post"),
                parse_mode="HTML", reply_markup=env_location_kb(lang, allow_current=True, allow_dm=True)
            )
        except TelegramBadRequest:
            await cb.message.answer(
                t("env.loc.ask", lang) or _lbl(lang, "📍 请选择投放位置", "📍 Choose where to post"),
                reply_markup=env_location_kb(lang, allow_current=True, allow_dm=True)
            )
    await cb.answer()


# ================== 选择投放位置（兼容保留） ==================
@router.callback_query(F.data.regexp(r"^env:loc:(here|dm|pick)$"))
async def choose_location(cb: CallbackQuery, state: FSMContext):
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    m = re.match(r"^env:loc:(here|dm|pick)$", cb.data or "")
    loc = m.group(1) if m else "dm"
    await state.update_data(loc=loc)

    if loc == "here":
        target = cb.message.chat.id
        await state.update_data(target_chat_id=target)
        await state.set_state(SendStates.MEMO)
        memo_text = _safe_i18n_text("env.memo.ask", lang, DEFAULT_MEMO_ASK_ZH, DEFAULT_MEMO_ASK_EN)
        await cb.message.edit_text(memo_text, parse_mode="HTML", reply_markup=env_memo_kb(lang))
    elif loc == "pick":
        await state.set_state(SendStates.PICK_CHAT)
        hint = _t_first(["env.loc.pick_tip"], lang,
                        _lbl(lang,
                             "📎 请发送目标群链接 / 用户名 / chat_id（支持 -100ID、@username、t.me/xxx 或 t.me/c/...）。",
                             "📎 Send group link / username / chat_id (-100ID, @username, t.me/xxx or t.me/c/...)."))
        await cb.message.edit_text(hint, parse_mode="HTML", reply_markup=env_back_kb(lang, to="loc"))
    else:
        # dm：目标为自己
        await state.update_data(target_chat_id=cb.from_user.id)
        await state.set_state(SendStates.MEMO)
        memo_text = _safe_i18n_text("env.memo.ask", lang, DEFAULT_MEMO_ASK_ZH, DEFAULT_MEMO_ASK_EN)
        await cb.message.edit_text(memo_text, parse_mode="HTML", reply_markup=env_memo_kb(lang))
    await cb.answer()


# ================== 指定群（支持链接/用户名/ID） ==================
@router.message(SendStates.PICK_CHAT)
async def input_pick_chat(msg: Message, state: FSMContext):
    lang = _ensure_db_lang(msg.from_user.id, getattr(msg.from_user, "language_code", None), msg.from_user.username)

    # 如果在群里触发 /start（含 @bot 形式），视为“首次交互”并补记到表
    if _is_group(msg.chat.id):
        try:
            log_user_to_sheet(
                msg.from_user,
                source="first_seen_in_group",
                chat=msg.chat,
                inviter_user_id=None,
                joined_via_invite_link=False,
                note="first interaction in group (/start)"
            )
        except Exception as e:
            log.warning("first_seen log failed (/start in group): %s", e)


    chat_id = await _parse_target_chat_id(msg.bot, msg, msg.text or "")
    if chat_id is None:
        await msg.answer(
            _t_first(["env.loc.bad_link"], lang,
                     _lbl(lang, "❌ 无法识别该群链接/用户名/ID，请检查格式或先把机器人拉进群。", "❌ Can't parse the group link/username/ID. Please check the format or add the bot to the group.")),
            reply_markup=env_back_kb(lang, to="loc")
        )
        return

    await state.update_data(target_chat_id=int(chat_id))
    # 记忆为默认目标群（有群标题则一起记）
    try:
        ch = await msg.bot.get_chat(int(chat_id))
        title = getattr(ch, "title", None) or getattr(ch, "username", None)
    except Exception:
        title = None
    try:
        with get_session() as s:
            set_last_target_chat(s, msg.from_user.id, int(chat_id), title=title)
            s.commit()
    except Exception as e:
        log.exception("persist target chat failed: %s", e)

    await state.set_state(SendStates.MEMO)
    # 成功提示
    ch_title = title or str(chat_id)
    saved_line = _t_first(["env.tg.parse_ok", "env.tg.preset"], lang)
    if saved_line:
        try:
            await msg.answer(saved_line.format(title=ch_title, chat_id=chat_id), parse_mode="HTML")
        except Exception:
            pass
    # —— pick 成功后进入祝福语 —— #
    memo_text = _safe_i18n_text("env.memo.ask", lang, DEFAULT_MEMO_ASK_ZH, DEFAULT_MEMO_ASK_EN)
    await msg.answer(memo_text, reply_markup=env_memo_kb(lang))


# ================== 祝福语 ==================
@router.callback_query(F.data == "env:memo:skip")
async def memo_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(memo="")
    await to_confirm(cb.message, state, cb.from_user)
    await cb.answer()


@router.message(SendStates.MEMO)
async def input_memo(msg: Message, state: FSMContext):
    memo_raw = str(msg.text or "").strip()
    memo = "" if memo_raw.lower() in ("跳过", "skip") else memo_raw
    await state.update_data(memo=memo)
    await to_confirm(msg, state, msg.from_user)

def _append_memo_line(env, lang: str, lines: list) -> None:
    """
    如果红包对象上存在祝福语，则安全地追加一行：
    “📝 祝福语：{内容}”（HTML 转义，超长裁剪）。
    """
    raw = (getattr(env, "note", "") or getattr(env, "memo", "") or "").strip()
    if not raw:
        return

    # 优先统一走 env.memo_label（语言包里带冒号/空格），再兜底 confirm_page
    label = t("env.memo_label", lang) or t("env.confirm_page.memo_label", lang) or "📝 祝福语："
    show = raw if len(raw) <= 100 else raw[:100] + "…"
    show_safe = _html_escape(show)
    lines.append(f"{label}{show_safe}")




# ================== 渲染确认页 ==================
async def to_confirm(ctx_message: Message, state: FSMContext, actor):
    lang = _ensure_db_lang(actor.id, getattr(actor, "language_code", None), getattr(actor, "username", None))
    data = await state.get_data()
    mode = (data.get("mode") or "USDT").upper()
    amount = Decimal(str(data.get("amount")))
    shares = int(data.get("shares"))
    dist = data.get("dist", "random")
    loc = data.get("loc", "dm")
    target_chat_id = int(data.get("target_chat_id") or actor.id)
    memo = data.get("memo") or ""

    title = t("env.confirm.title", lang) or _lbl(lang, "✅ 请确认参数", "✅ Please confirm the details")
    lab_token  = _t_first(["env.confirm_page.token_label"], lang, _lbl(lang, "🪙 币种：", "🪙 Token:"))
    lab_amount = _t_first(["env.confirm_page.amount_label"], lang, _lbl(lang, "💵 金额：", "💵 Amount:"))
    lab_shares = _t_first(["env.confirm_page.shares_label"], lang, _lbl(lang, "📦 份数：", "📦 Shares:"))
    lab_dist   = _t_first(["env.confirm_page.dist_label"], lang, _lbl(lang, "⚖️ 分配方式：", "⚖️ Distribution:"))
    lab_loc    = _t_first(["env.confirm_page.loc_label"], lang, _lbl(lang, "📍 投放位置：", "📍 Location:"))
    lab_memo   = _t_first(["env.memo_label", "env.confirm_page.memo_label"], lang, _lbl(lang, "📝 祝福语：", "📝 Blessing:"))
    lab_sender = _t_first(["hb.sender","hongbao.sender","env.sender"], lang, _lbl(lang, "👤 发包人：", "👤 Sender: "))  # ✅

    dist_disp = _lbl(lang, "🎲 随机", "🎲 Random") if dist == "random" else _lbl(lang, "🟰 固定（兼容）", "🟰 Fixed (compat)")

    # 使用群标题/用户名，而不是 chat_id
    try:
        loc_display = await _chat_display_title(ctx_message.bot, target_chat_id)
    except Exception:
        loc_display = str(target_chat_id)

    lines = [
        title,
        "────────────────",
        f"• {lab_sender}{_sender_mention(actor)}",  # ✅ 确认页也显示发包人
        f"• {lab_token}{mode}",
        f"• {lab_amount}{_fmt_amount_for_display(mode, amount)}",
        f"• {lab_shares}{shares}",
        f"• {lab_dist}{dist_disp}",
        f"• {lab_loc}{loc_display}",
    ]
    if memo:
        lines.append(f"• {lab_memo}{html.escape(memo)}")

    # 封面展示（仅显示选择结果，不影响流程）
    cover_slug = data.get("cover_slug")
    cover_msg_id = data.get("cover_message_id")
    if cover_slug or cover_msg_id:
        lab_cover = _t_first(["env.confirm_page.cover_label", "env.confirm.cover"], lang, _lbl(lang, "🖼 封面：", "🖼 Cover: "))
        if cover_slug:
            lines.append(f"• {lab_cover}{cover_slug}")
        elif cover_msg_id:
            lines.append(f"• {lab_cover}#{int(cover_msg_id)}")

    await state.set_state(SendStates.CONFIRM)
    await ctx_message.answer("\n".join(lines), parse_mode="HTML", reply_markup=env_confirm_kb(lang))


# ================== 取消（退款） ==================
@router.callback_query(F.data == "env:cancel")
async def env_cancel(cb: CallbackQuery, state: FSMContext):
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    data = await state.get_data()
    ticket = data.get("ticket")
    if ticket and not ticket.get("refunded"):
        _refund_balance(cb.from_user.id, DeductTicket(ticket["token"], Decimal(ticket["amount"]), ticket.get("refunded", False)))
        ticket["refunded"] = True
        await state.update_data(ticket=ticket)

    await state.clear()
    cancelled_text = _t_first(["env.cancelled"], lang, _lbl(lang, "✅ 已取消，款项已原路退回。", "Cancelled. Funds returned."))
    try:
        await cb.message.edit_text(cancelled_text, reply_markup=back_home_kb(lang))
    except TelegramBadRequest:
        await cb.message.answer(cancelled_text, reply_markup=back_home_kb(lang))
    await cb.answer()


# ================== 带封面的“单条媒体卡片”投放 ==================
async def _post_media_card_with_caption(
    cb_or_msg,
    target_chat_id: int,
    cover_info: Dict[str, Any],
    text_html: str,
    kb: InlineKeyboardMarkup,
    lang: str,
) -> tuple[int, Optional[int], Optional[str]]:
    """
    优先把素材频道的原消息复制到目标会话；若只有 file_id 则 send_photo/send_animation；
    失败时回退到当前会话。
    """
    bot = cb_or_msg.message.bot if isinstance(cb_or_msg, CallbackQuery) else cb_or_msg.bot
    fallback_chat_id = cb_or_msg.message.chat.id if isinstance(cb_or_msg, CallbackQuery) else cb_or_msg.chat.id

    cover_channel_id = cover_info.get("cover_channel_id")
    cover_message_id = cover_info.get("cover_message_id")
    cover_file_id    = cover_info.get("cover_file_id")

    known_err = ("chat not found", "not enough rights", "have no rights", "bot was kicked", "bot was blocked", "chat is deactivated")

    async def _post_to(chat_id: int):
        if cover_channel_id and cover_message_id:
            return await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=int(cover_channel_id),
                message_id=int(cover_message_id),
                caption=text_html,
                parse_mode="HTML",
                reply_markup=kb,
            )
        if cover_file_id:
            try:
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=cover_file_id,
                    caption=text_html,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except TelegramBadRequest:
                return await bot.send_animation(
                    chat_id=chat_id,
                    animation=cover_file_id,
                    caption=text_html,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
        return await bot.send_message(chat_id, text_html, parse_mode="HTML", reply_markup=kb)

    try:
        m = await _post_to(target_chat_id)
        return target_chat_id, getattr(m, "message_id", None), None
    except TelegramBadRequest as e:
        err = str(e)
        if any(k in err.lower() for k in known_err):
            m2 = await _post_to(fallback_chat_id)
            return fallback_chat_id, getattr(m2, "message_id", None), err
        raise
    except Exception:
        raise


# ================== 纯文本卡片投放（失败回退） ==================
async def _post_card_with_fallback(cb_or_msg, target_chat_id: int, text_html: str, kb, lang: str) -> tuple[int, Optional[int], Optional[str]]:
    """
    仅文本卡片投放（无封面）：
    优先投放到 target_chat_id；若遇到典型错误（chat not found / rights / was kicked / blocked / deactivated），
    自动回退到当前会话，并在文本顶部加入提示。
    返回：(实际发送的 chat_id, 实际消息 message_id, 错误字符串或 None)
    """
    bot = cb_or_msg.message.bot if isinstance(cb_or_msg, CallbackQuery) else cb_or_msg.bot
    fallback_chat_id = cb_or_msg.message.chat.id if isinstance(cb_or_msg, CallbackQuery) else cb_or_msg.chat.id

    try:
        msg = await bot.send_message(
            chat_id=target_chat_id,
            text=text_html,
            parse_mode="HTML",
            reply_markup=kb,
        )
        return target_chat_id, msg.message_id, None
    except TelegramBadRequest as e:
        err = str(e)
        err_low = err.lower()
        known = ("chat not found", "not enough rights", "have no rights", "bot was kicked", "bot was blocked", "chat is deactivated")
        if any(k in err_low for k in known):
            tip = _t_first(
                ["env.fail.post"],
                lang,
                _lbl(lang, "⚠️ 机器人无法在目标会话发言，已改为当前会话。请先把机器人拉入目标群并授予发言权限。", "⚠️ Bot can't post to the target chat. Posted here instead. Please add the bot to the group and grant permissions."),
            )
            msg2 = await bot.send_message(
                chat_id=fallback_chat_id,
                text=tip + "\n\n" + text_html,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return fallback_chat_id, getattr(msg2, "message_id", None), err
        # 非典型错误，继续抛出
        raise
    except Exception as e:
        log.exception("post failed (unexpected): %s", e)
        raise


# ================== 确认发送 ==================
@router.callback_query(F.data == "env:confirm")
async def env_confirm(cb: CallbackQuery, state: FSMContext):
    # 先响应回调，避免 “query is too old”
    try:
        await cb.answer()
    except Exception:
        pass

    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    data = await state.get_data()

    mode = (data.get("mode") or "USDT").upper()
    amount = Decimal(str(data.get("amount") or "0"))
    shares = int(data.get("shares") or 0)
    memo = data.get("memo") or ""
    target_chat_id = int(data.get("target_chat_id") or cb.from_user.id)

    min_sh = int(_flag_get(flags, "HB_MIN_SHARES", getattr(settings, "HB_MIN_SHARES", 1)))
    if not (mode and amount > 0 and shares >= min_sh):

        await cb.message.edit_text(t("env.errors.not_ready", lang) or _lbl(lang, "参数不完整。", "Parameters incomplete."), reply_markup=back_home_kb(lang))
        return

    # ✅ 若无预扣票据（比如接力直接跳到确认），这里做余额二次校验 + 即时扣款
    ticket = data.get("ticket")
    if not ticket:
        bal_map = _get_wallets_for_user_id(cb.from_user.id)
        # 统一金额精度：USDT/TON 两位、POINT 取整
        need_amt = amount.quantize(Decimal("0.00")) if mode in ("USDT", "TON") else Decimal(int(amount))
        if bal_map.get(mode, Decimal(0)) < need_amt:
            await cb.message.edit_text(
                _t_first(["env.errors.insufficient"], lang,
                        _lbl(lang, "💳 余额不足，请先充值或降低金额。", "💳 Insufficient balance — please recharge or reduce the amount.")),
                reply_markup=back_home_kb(lang)
            )
            return
        try:
            _deduct_balance(cb.from_user.id, mode, need_amt)
            # 票据跟上需要的实际金额字符串（USDT/TON 已量化）
            await state.update_data(ticket={"token": mode, "amount": str(need_amt), "refunded": False})
            data = await state.get_data()
            ticket = data.get("ticket")
        except ValueError as e:
            if str(e).upper() == "INSUFFICIENT_BALANCE":
                log.warning("confirm-step deduct failed due to insufficient balance (actor=%s, token=%s, amount=%s)",
                            cb.from_user.id, mode, need_amt)
                await cb.message.edit_text(
                    _t_first(["env.errors.insufficient"], lang,
                            _lbl(lang, "💳 余额不足，请先充值或降低金额。", "💳 Insufficient balance — please recharge or reduce the amount.")),
                    reply_markup=back_home_kb(lang)
                )
                return
            log.warning("confirm-step deduct failed (ValueError): %s", e)
            await cb.message.edit_text(
                _lbl(lang, "扣款失败，请稍后重试。", "Deduction failed. Please try again later."),
                reply_markup=back_home_kb(lang)
            )
            return
        except Exception as e:
            log.exception("confirm-step deduct failed (unexpected): %s", e)
            await cb.message.edit_text(
                _lbl(lang, "扣款失败，请稍后重试。", "Deduction failed. Please try again later."),
                reply_markup=back_home_kb(lang)
            )
            return


    # 读取封面（若有）
    cover_channel_id = data.get("cover_channel_id")
    cover_message_id = data.get("cover_message_id")
    cover_file_id = data.get("cover_file_id")
    cover_slug = data.get("cover_slug")

    # 创建红包（ACTIVE） + 记流水（扣款已完成）
    try:
        with get_session() as s:
            env = create_envelope(
                s,
                chat_id=target_chat_id,
                sender_tg_id=cb.from_user.id,
                mode=mode,
                total_amount=amount,
                shares=shares,
                note=memo,
                activate=True,
                cover_channel_id=int(cover_channel_id) if cover_channel_id is not None else None,
                cover_message_id=int(cover_message_id) if cover_message_id is not None else None,
                cover_file_id=cover_file_id or None,
                cover_meta={"slug": cover_slug} if cover_slug else None,
            )
            add_ledger_entry(
                s,
                user_tg_id=int(cb.from_user.id),
                ltype=LedgerType.SEND,
                token=mode,
                amount=-amount,  # 负数 = 支出
                ref_type="ENVELOPE",
                ref_id=str(env.id),
                note=(memo or "send envelope"),
            )
            try:
                set_last_target_chat(s, cb.from_user.id, int(target_chat_id))
            except Exception as e:
                log.warning("set_last_target_chat failed: %s", e)
            s.commit()
            eid = int(env.id)
    except HBError:
        ticket = data.get("ticket")
        if ticket and not ticket.get("refunded"):
            _refund_balance(cb.from_user.id, DeductTicket(ticket["token"], Decimal(ticket["amount"]), ticket.get("refunded", False)))
        await state.clear()
        fail_txt = t("env.fail.create", lang) or _lbl(lang, "❌ 创建失败，请稍后再试。", "❌ Creation failed, please try again later.")
        await cb.message.edit_text(fail_txt, reply_markup=back_home_kb(lang))
        return
    except Exception as e:
        log.exception("create_envelope failed: %s", e)
        ticket = data.get("ticket")
        if ticket and not ticket.get("refunded"):
            _refund_balance(cb.from_user.id, DeductTicket(ticket["token"], Decimal(ticket["amount"]), ticket.get("refunded", False)))
        await state.clear()
        fail_txt = t("env.fail.create", lang) or _lbl(lang, "❌ 创建失败，请稍后再试。", "❌ Creation failed, please try again later.")
        await cb.message.edit_text(fail_txt, reply_markup=back_home_kb(lang))
        return

    # 成功创建 → 标记票据不再退款
    ticket = data.get("ticket")
    if ticket:
        ticket["refunded"] = True
        await state.update_data(ticket=ticket)

    await state.clear()

    # 先初始化投放结果变量
    real_chat_id: int = target_chat_id
    posted_msg_id: Optional[int] = None
    err: Optional[str] = None

    try:
        summary = get_envelope_summary(eid)

        # ✅ 投放卡片顶部加入“发包人：@提及”
        sender_lab = _t_first(["hb.sender","hongbao.sender","env.sender"], lang, _lbl(lang, "👤 发包人：", "👤 Sender: "))
        sender_line = f"{sender_lab}{_sender_mention(cb.from_user)}"
        text_html = sender_line + "\n" + _compose_summary_text(summary, lang)

        # ✅ 追加祝福语（直接用本次填写的 memo，避免 summary 不含 note 时丢失；再兜底 DB）
        note = (memo or "").strip()
        if not note:
            try:
                note = (getattr(env, "note", "") or "").strip()  # 刚创建的 env 就有 note
            except Exception:
                note = ""
        if note:
            lab_memo = _t_first(["env.memo_label", "env.confirm_page.memo_label"], lang, _lbl(lang, "📝 祝福语：", "📝 Blessing:"))
            text_html += f"\n• {lab_memo}{html.escape(note)}"



        if cover_channel_id or cover_file_id:
            real_chat_id, posted_msg_id, err = await _post_media_card_with_caption(
                cb,
                target_chat_id,
                {
                    "cover_channel_id": cover_channel_id,
                    "cover_message_id": cover_message_id,
                    "cover_file_id": cover_file_id,
                },
                text_html,
                hb_grab_kb(eid, lang),
                lang,
            )
        else:
            real_chat_id, posted_msg_id, err = await _post_card_with_fallback(
                cb, target_chat_id, text_html, hb_grab_kb(eid, lang), lang
            )

        # “前往红包群”按钮
        group_username = None
        try:
            ch = await cb.message.bot.get_chat(int(real_chat_id))
            group_username = getattr(ch, "username", None)
        except Exception:
            pass
        group_url = _group_link_for(int(real_chat_id), posted_msg_id, group_username)

        rows = []
        if group_url and int(real_chat_id) < 0:
            rows.append([InlineKeyboardButton(text=t("env.open_group_btn", lang) or _lbl(lang, "➡️ 前往红包群", "➡️ Open Group"), url=group_url)])
        rows.append([InlineKeyboardButton(text=t("menu.back", lang) or _lbl(lang, "⬅️ 返回", "⬅️ Back"), callback_data="hb:menu")])
        
        goto_kb = InlineKeyboardMarkup(inline_keyboard=rows)

        # 兜底：普通群或无法生成直达消息链接，但群是公开群（有 username），给群主页链接
        if not group_url and group_username:
            rows.append([InlineKeyboardButton(
                text=t("env.open_group_btn", lang) or _lbl(lang, "➡️ 前往红包群", "➡️ Open Group"),
                url=f"https://t.me/{group_username}"
            )])

        if err:
            warn_txt = _t_first(
                ["env.fail.post"],
                lang,
                _lbl(lang, "⚠️ 红包已创建，但目标会话不可用，已改为当前会话。", "⚠️ Created successfully but target chat failed; posted here instead."),
            )
            await cb.message.edit_text(warn_txt, reply_markup=goto_kb)
        else:
            ok_txt = t("env.success.sent", lang) or _lbl(lang, "✅ 红包已发送！", "✅ Red packet sent!")
            await cb.message.edit_text(ok_txt, reply_markup=goto_kb)

    except Exception as e:
        log.exception("post envelope message failed: %s", e)
        warn_txt = t("env.fail.post", lang) or _lbl(lang, "⚠️ 红包已创建，但投放卡片失败，请检查目标会话。", "⚠️ Created successfully but failed to post the card. Please check the target chat.")
        await cb.message.edit_text(warn_txt, reply_markup=back_home_kb(lang))


# ================== 封面相关（只增不删） ==================
_COVER_PAGE_SIZE = 8  # 简单分页大小


async def _try_post_cover(cb_or_msg, target_chat_id: int, cover_info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    优先使用 copyMessage 复制素材频道消息；兜底使用 sendPhoto(file_id)。
    成功返回 (True, None)；失败返回 (False, err_str)。不抛出异常，保证不阻断红包发送流程。
    """
    bot = cb_or_msg.message.bot if isinstance(cb_or_msg, CallbackQuery) else cb_or_msg.bot

    ch_id = cover_info.get("cover_channel_id") or settings.COVER_CHANNEL_ID
    msg_id = cover_info.get("cover_message_id")
    file_id = cover_info.get("cover_file_id")

    try:
        if ch_id and msg_id:
            try:
                await bot.copy_message(chat_id=target_chat_id, from_chat_id=int(ch_id), message_id=int(msg_id))
                return True, None
            except Exception as e:
                err1 = str(e)
                if file_id:
                    try:
                        await bot.send_photo(chat_id=target_chat_id, photo=file_id)
                        return True, None
                    except Exception as e2:
                        return False, f"copyMessage fail: {err1}; sendPhoto fail: {e2}"
                return False, f"copyMessage fail: {err1}"
        if file_id:
            try:
                await bot.send_photo(chat_id=target_chat_id, photo=file_id)
                return True, None
            except Exception as e3:
                return False, f"sendPhoto fail: {e3}"
    except Exception as e:
        return False, str(e)
    return False, None


@router.callback_query(F.data == "env:cover:choose")
async def cover_choose(cb: CallbackQuery, state: FSMContext):
    """
    进入“封面选择”入口：展示封面分页或引导去素材频道。
    备注：为避免未就绪模块报错，这里惰性导入 keyboards 与 models.cover。
    """
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    await state.set_state(SendStates.COVER)

    # 惰性导入，以免尚未改好 keyboards.py / models.cover 时崩溃
    try:
        from core.utils.keyboards import env_cover_entry_kb, env_cover_list_kb  # type: ignore
    except Exception:
        env_cover_entry_kb = None  # type: ignore
        env_cover_list_kb = None  # type: ignore

    # 读取第一页封面
    covers = []
    total = 0
    try:
        from models.cover import list_covers  # type: ignore
        covers, total = list_covers(page=1, page_size=_COVER_PAGE_SIZE)
    except Exception as e:
        log.info("list_covers not available yet: %s", e)

    # 有列表则展示列表，否则展示入口键盘（让用户去素材频道操作/或跳过）
    if covers and env_cover_list_kb:
        title = t("env.cover.pick_title", lang) or _lbl(lang, "🖼 请选择封面（素材频道）", "🖼 Pick a cover")
        try:
            await cb.message.edit_text(title, reply_markup=env_cover_list_kb(covers, page=1, page_size=_COVER_PAGE_SIZE, lang=lang))
        except TelegramBadRequest:
            await cb.message.answer(title, reply_markup=env_cover_list_kb(covers, page=1, page_size=_COVER_PAGE_SIZE, lang=lang))
    else:
        tip = t("env.cover.entry_tip", lang) or _lbl(lang, "你可以从素材频道选择一个封面，也可“跳过”。", "You can pick a cover from the materials channel, or skip.")
        if env_cover_entry_kb:
            try:
                await cb.message.edit_text(tip, reply_markup=env_cover_entry_kb(lang))
            except TelegramBadRequest:
                await cb.message.answer(tip, reply_markup=env_cover_entry_kb(lang))
        else:
            await cb.message.answer(tip)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^env:cover:page:(\d+)$"))
async def cover_page(cb: CallbackQuery, state: FSMContext):
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    m = re.match(r"^env:cover:page:(\d+)$", cb.data or "")
    page = int(m.group(1)) if m else 1

    try:
        from core.utils.keyboards import env_cover_list_kb  # type: ignore
        from models.cover import list_covers  # type: ignore
    except Exception:
        await cb.answer(_lbl(lang, "暂不可用", "Not available yet"), show_alert=True)
        return

    covers, total = list_covers(page=page, page_size=_COVER_PAGE_SIZE)
    title = t("env.cover.pick_title", lang) or _lbl(lang, "🖼 请选择封面（素材频道）", "🖼 Pick a cover")
    try:
        await cb.message.edit_text(title, reply_markup=env_cover_list_kb(covers, page=page, page_size=_COVER_PAGE_SIZE, lang=lang))
    except TelegramBadRequest:
        await cb.message.answer(title, reply_markup=env_cover_list_kb(covers, page=page, page_size=_COVER_PAGE_SIZE, lang=lang))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^env:cover:set:(\d+)$"))
async def cover_set(cb: CallbackQuery, state: FSMContext):
    """
    选择某个封面：把 channel_id/message_id/file_id/slug 写入 FSM。
    成功后回到确认页（不改变原流程）。
    """
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    m = re.match(r"^env:cover:set:(\d+)$", cb.data or "")
    if not m:
        await cb.answer(_lbl(lang, "无效封面", "Invalid cover"), show_alert=True)
        return
    cover_id = int(m.group(1))

    try:
        from models.cover import get_cover_by_id  # type: ignore
    except Exception:
        await cb.answer(_lbl(lang, "暂不可用", "Not available yet"), show_alert=True)
        return

    try:
        cov = get_cover_by_id(cover_id)
        if not cov:
            await cb.answer(_lbl(lang, "封面不存在", "Cover not found"), show_alert=True)
            return
        await state.update_data(
            cover_channel_id=getattr(cov, "channel_id", None),
            cover_message_id=getattr(cov, "message_id", None),
            cover_file_id=getattr(cov, "file_id", None),
            cover_slug=getattr(cov, "slug", None),
        )
        # 回到确认页
        await to_confirm(cb.message, state, cb.from_user)
    except Exception as e:
        log.warning("get_cover_by_id failed: %s", e)
        await cb.answer(_lbl(lang, "选择失败", "Failed to set cover"), show_alert=True)
        return
    await cb.answer()


@router.callback_query(F.data == "env:cover:skip")
async def cover_skip(cb: CallbackQuery, state: FSMContext):
    """清空封面后返回确认页。"""
    await state.update_data(
        cover_channel_id=None,
        cover_message_id=None,
        cover_file_id=None,
        cover_slug=None,
    )
    await to_confirm(cb.message, state, cb.from_user)
    await cb.answer()


# ================== 深链：/start payload ==================
def _get_env_chat_id(eid: int) -> Optional[int]:
    """
    根据红包ID获取该红包最初发送到的群ID（chat_id）。
    优先：summary 里自带的 chat_id；
    兜底：直接查表 Envelope.id == eid。
    """
    # 1) 先从 summary 里尝试
    try:
        summary = get_envelope_summary(int(eid))
        for k in ("chat_id", "chatId", "group_id", "groupId"):
            if k in summary and summary[k] is not None:
                return int(summary[k])
    except Exception:
        pass

    # 2) 兜底查 DB
    try:
        with get_session() as s:
            env = s.query(Envelope).filter_by(id=int(eid)).first()
            if env and getattr(env, "chat_id", None) is not None:
                return int(env.chat_id)
    except Exception:
        pass

    return None


@router.message(F.text.regexp(r"^/start(?:@\w+)?(?:\s+.*)?$"))
async def deep_start(msg: Message, state: FSMContext):
    # 在群里隐藏用户输入的 /start 或 /start@bot 命令
    try:
        if getattr(msg.chat, "type", "") in {"group", "supergroup"}:
            await msg.delete()  # 需要机器人在群里有“删除消息”权限；失败忽略
    except TelegramBadRequest:
        pass
    except Exception:
        pass

    lang = _ensure_db_lang(msg.from_user.id, getattr(msg.from_user, "language_code", None), msg.from_user.username)
    text = msg.text or ""
    m1 = re.search(r"/start\s+send_g(-?\d+)", text)
    m2 = re.search(r"/start\s+copy_e(\d+)", text)
    m3 = re.search(r"/start\s+quick\b", text)

    if m1:
        # 私聊开启向导并预填目标群
        gid = int(m1.group(1))
        await state.clear()
        await state.set_state(SendStates.MODE)
        await state.update_data(target_chat_id=gid, loc="pick")
        head = (t("env.title", lang) or _lbl(lang, "🧧 发红包向导", "🧧 Red Packet Wizard"))
        note = t("env.preset_chat", lang, chat_id=gid) or _lbl(lang, f"📍 已预设目标群：{gid}", f"📍 Preset target chat: {gid}")
        await msg.answer(
            head + "\n\n" + note + "\n" + (t("env.mode_title", lang) or _lbl(lang, "🔘 请选择币种", "🔘 Please choose a token")),
            parse_mode="HTML", reply_markup=env_mode_kb(lang)
        )
        return

    if m2:
        # 复制某个红包的参数作为模板（仅复制 mode/amount/shares/note；优先投到该轮原群）
        eid = int(m2.group(1))
        try:
            summary = get_envelope_summary(eid)
            mode = (summary.get("mode") or "USDT").upper()
            total_amount = Decimal(str(summary.get("total_amount") or "0"))
            shares = int(summary.get("shares") or 0)
            memo = summary.get("note") or ""
        except Exception:
            await msg.answer(_lbl(lang, "❌ 无法读取模板红包参数。", "❌ Cannot read template envelope."), reply_markup=back_home_kb(lang))
            return

        await state.clear()
        await state.update_data(mode=mode, amount=str(total_amount), shares=shares, dist="random", memo=memo)

        chat_id = _get_env_chat_id(eid)
        if chat_id:
            await state.update_data(target_chat_id=chat_id, loc="here")
            await to_confirm(msg, state, msg.from_user)
        else:
            # 拿不到原群 → 进入选择目标群页
            await state.set_state(SendStates.TG)
            with get_session() as s:
                gid, gtitle = get_last_target_chat(s, msg.from_user.id)
            text = (_t_first(["env.tg.choose"], lang) or _lbl(lang, "📌 请选择要使用的目标群：", "📌 Please choose the target group:"))
            kb = _tg_actions_kb(gid, lang) if gid else _tg_actions_kb(None, lang)
            await msg.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
        return

    if m3:
        # 快速入口：进入选择目标群页面
        await state.clear()
        await state.set_state(SendStates.TG)
        with get_session() as s:
            gid, gtitle = get_last_target_chat(s, msg.from_user.id)
        text = (_t_first(["env.tg.choose"], lang) or _lbl(lang, "📌 请选择要使用的目标群：", "📌 Please choose the target group:"))
        kb = _tg_actions_kb(gid, lang) if gid else _tg_actions_kb(None, lang)
        await msg.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
        return

    # 其它 /start 走菜单
    await msg.answer(t("menu.back", lang) or _lbl(lang, "⬅️ 返回", "⬅️ Back"), reply_markup=back_home_kb(lang))


# ================== ✅ 最佳手气一键接力（不进入私聊循环） ==================
@router.callback_query(F.data.regexp(r"^(?:hb:relay|rank:mvp_send):(\d+)$"))
async def relay_best_send(cb: CallbackQuery):
    """
    来自“最佳手气发红包”按钮的回调。
    逻辑：
      1) 校验触发者必须是该轮 MVP；
      2) 读取该轮参数(mode/amount/shares、原群)；
      3) 校验余额并即时扣款；
      4) 直接在“该轮原群”创建并投放新红包，返回“前往红包群”按钮。
    """
    lang = _ensure_db_lang(cb.from_user.id, getattr(cb.from_user, "language_code", None), cb.from_user.username)
    
    m = re.match(r"^(?:hb:relay|rank:mvp_send):(\d+)$", cb.data or "")
    if not m:
        await cb.answer(_lbl(lang, "无效操作", "Invalid action"), show_alert=True)
        return
    src_eid = int(m.group(1))

    # 群内点击“最佳手气接力” → 视为首次交互进行补记
    try:
        if _is_group(cb.message.chat.id):
            log_user_to_sheet(
                cb.from_user,
                source="first_seen_in_group",
                chat=cb.message.chat,
                inviter_user_id=None,
                joined_via_invite_link=False,
                note="first interaction in group (relay)"
            )
    except Exception as e:
        log.warning("first_seen log failed (relay): %s", e)


    # 读取该轮参数与原群
    try:
        summary = get_envelope_summary(src_eid)
        mode = (summary.get("mode") or "USDT").upper()
        amount = Decimal(str(summary.get("total_amount") or "0"))
        shares = int(summary.get("shares") or 0)
        note = summary.get("note") or ""
    except Exception:
        await cb.answer(_lbl(lang, "❌ 无法读取该轮参数。", "❌ Cannot load round parameters."), show_alert=True)
        return

    # 校验是否 MVP
    try:
        lucky = get_lucky_winner(src_eid)
        lucky_id = int(lucky.get("tg_id") or lucky.get("user_id") or 0)
    except Exception:
        lucky_id = 0
    if lucky_id != int(cb.from_user.id):
        await cb.answer(t("hongbao.errors.only_mvp", lang) or _lbl(lang, "⚠️ 只有最佳手气用户才能继续发红包", "⚠️ Only MVP can continue."), show_alert=True)
        return

    # 找到原群
    target_chat_id = _get_env_chat_id(src_eid)
    if not target_chat_id:
        await cb.answer(_lbl(lang, "❌ 找不到原群，会话已失效。请在私聊走向导。", "❌ Original group not found. Please use the wizard in DM."), show_alert=True)
        return

    # 余额校验 + 扣款
    bal_map = _get_wallets_for_user_id(cb.from_user.id)
    need_amt = amount if mode in ("USDT", "TON") else Decimal(int(amount))
    if bal_map.get(mode, Decimal(0)) < need_amt:
        # 给到“去充值”入口
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("balance.recharge", lang) or _lbl(lang, "💳 去充值", "💳 Recharge"), callback_data="recharge:home")],
            [InlineKeyboardButton(text=t("menu.back", lang) or _lbl(lang, "⬅️ 返回", "⬅️ Back"), callback_data="hb:menu")],
        ])
        await cb.message.edit_text(
            _t_first(["env.errors.insufficient"], lang, _lbl(lang, "💳 余额不足 —— 请先充值或降低金额。", "💳 Insufficient balance — please recharge or reduce the amount.")),
            reply_markup=kb
        )
        await cb.answer()
        return

    try:
        _deduct_balance(cb.from_user.id, mode, need_amt)
    except ValueError as e:
        # 并发/竞态下仍可能出现余额不足，这里抑制堆栈并给友好提示
        if str(e).upper() == "INSUFFICIENT_BALANCE":
            log.warning(
                "relay deduct failed due to insufficient balance (actor=%s, token=%s, amount=%s)",
                cb.from_user.id, mode, need_amt
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t("balance.recharge", lang) or _lbl(lang, "💳 去充值", "💳 Recharge"),
                    callback_data="recharge:home"
                )],
                [InlineKeyboardButton(
                    text=t("menu.back", lang) or _lbl(lang, "⬅️ 返回", "⬅️ Back"),
                    callback_data="hb:menu"
                )],
            ])
            await cb.message.edit_text(
                _t_first(["env.errors.insufficient"], lang,
                        _lbl(lang, "💳 余额不足 —— 请先充值或降低金额。", "💳 Insufficient balance — please recharge or reduce the amount.")),
                reply_markup=kb
            )
            await cb.answer()
            return
        # 其它 ValueError 不打印堆栈
        log.warning("relay deduct failed (ValueError): %s", e)
        await cb.answer(_lbl(lang, "扣款失败，请稍后再试。", "Deduction failed. Try later."), show_alert=True)
        return
    except Exception as e:
        # 未知异常保留堆栈
        log.exception("relay deduct failed (unexpected): %s", e)
        await cb.answer(_lbl(lang, "扣款失败，请稍后再试。", "Deduction failed. Try later."), show_alert=True)
        return


    # 创建红包 + 记账
    try:
        with get_session() as s:
            env = create_envelope(
                s,
                chat_id=target_chat_id,
                sender_tg_id=cb.from_user.id,
                mode=mode,
                total_amount=amount,
                shares=shares,
                note=note,
                activate=True,
            )
            add_ledger_entry(
                s,
                user_tg_id=int(cb.from_user.id),
                ltype=LedgerType.SEND,
                token=mode,
                amount=-amount,
                ref_type="ENVELOPE",
                ref_id=str(env.id),
                note="relay by MVP",
            )
            s.commit()
            new_eid = int(env.id)
    except Exception as e:
        log.exception("relay create_envelope failed: %s", e)
        # 失败退款
        try:
            _refund_balance(cb.from_user.id, DeductTicket(mode, need_amt, False))
        except Exception:
            pass
        await cb.answer(_lbl(lang, "❌ 创建失败，请稍后再试。", "❌ Creation failed. Try again later."), show_alert=True)
        return

    # 投放卡片：直接往原群发
    try:
        new_summary = get_envelope_summary(new_eid)
        sender_lab = _t_first(["hb.sender","hongbao.sender","env.sender"], lang, _lbl(lang, "👤 发包人：", "👤 Sender: "))
        sender_line = f"{sender_lab}{_sender_mention(cb.from_user)}"
        text_html = sender_line + "\n" + _compose_summary_text(new_summary, lang)

        # ✅ 追加祝福语
        note2 = (new_summary.get("note") or "").strip()
        if note2:
            lab_memo = _t_first(["env.confirm_page.memo_label"], lang, _lbl(lang, "📝 祝福语：", "📝 Blessing:"))
            text_html += f"\n• {lab_memo}{html.escape(note2)}"

        real_chat_id, posted_msg_id, err = await _post_card_with_fallback(
            cb, target_chat_id, text_html, hb_grab_kb(new_eid, lang), lang
        )

        # 生成“前往红包群”按钮
        group_username = None
        try:
            ch = await cb.message.bot.get_chat(int(real_chat_id))
            group_username = getattr(ch, "username", None)
        except Exception:
            pass
        url = _group_link_for(int(real_chat_id), posted_msg_id, group_username)

        rows = []
        if url and int(real_chat_id) < 0:
            rows.append([InlineKeyboardButton(text=t("env.open_group_btn", lang) or _lbl(lang, "➡️ 前往红包群", "➡️ Open Group"), url=url)])
        rows.append([InlineKeyboardButton(text=t("menu.back", lang) or _lbl(lang, "⬅️ 返回", "⬅️ Back"), callback_data="hb:menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

        if err:
            warn_txt = _t_first(
                ["env.fail.post"],
                lang,
                _lbl(lang, "⚠️ 红包已创建，但目标会话不可用，已改为当前会话。", "⚠️ Created successfully but target chat failed; posted here instead."),
            )
            await cb.message.edit_text(warn_txt, reply_markup=kb)
        else:
            await cb.message.edit_text(t("env.success.sent", lang) or _lbl(lang, "✅ 红包已发送！", "✅ Red packet sent!"), reply_markup=kb)

    except Exception as e:
        log.exception("relay post failed: %s", e)
        await cb.message.edit_text(
            t("env.fail.post", lang) or _lbl(lang, "⚠️ 红包已创建，但投放卡片失败，请检查目标会话。", "⚠️ Created successfully but failed to post the card. Please check the target chat."),
            reply_markup=back_home_kb(lang)
        )
