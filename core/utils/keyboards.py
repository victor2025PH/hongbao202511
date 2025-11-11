# core/utils/keyboards.py
# -*- coding: utf-8 -*-
"""
纯 i18n 版键盘：所有可见文案尽量从多语言 yml 读取。
为避免当前语言包缺少 lang.<code> 键导致按钮文本为空，本文件对语言选择菜单做了“国旗+本地名”兜底。

本文件还包含：
- 主菜单（含帮助中心）
- 发包向导键盘（币种/金额/份数/分配/投放/确认/封面）
- 充值、提现、福利、资产、管理员相关键盘
- 目标群选择与绑定
"""

from __future__ import annotations
from typing import Iterable, Optional, List, Sequence, Tuple, Any
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from core.i18n.i18n import t
from config.feature_flags import flags
from config.settings import settings

# -----------------------------
# 基础工具
# -----------------------------
def _kb(rows: List[List[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _btn(text: str | None, data: str, *, url: Optional[str] = None) -> InlineKeyboardButton:
    # 词条缺失时 text 可能为 None；兜底为空字符串，避免 Telegram 报错
    if url:
        return InlineKeyboardButton(text=(text or ""), url=url)
    return InlineKeyboardButton(text=(text or ""), callback_data=data)

def _tt(key: str, lang: str) -> str:
    return t(key, lang) or ""

def _tt_first(keys: Sequence[str], lang: str) -> str:
    for k in keys:
        v = t(k, lang)
        if v:
            return v
    return ""

def _ttf(key: str, lang: str, **kwargs) -> str:
    try:
        s = t(key, lang, **kwargs)
    except TypeError:
        s = t(key, lang)
    return s or ""

def _token_display(token: str, lang: str) -> str:
    tok = (token or "").upper()
    if tok == "USDT":
        return _tt("asset.usdt", lang)
    if tok == "TON":
        return _tt("asset.ton", lang)
    return _tt("asset.points", lang)  # POINT/Stars

# -----------------------------
# 语言显示名兜底
# -----------------------------
_LANG_PRETTY = {
    "zh": "🇨🇳 简体中文",
    "en": "🇺🇸 English",
    "fr": "🇫🇷 Français",
    "de": "🇩🇪 Deutsch",
    "es": "🇪🇸 Español",
    "hi": "🇮🇳 हिन्दी",
    "vi": "🇻🇳 Tiếng Việt",
    "th": "🇹🇭 ไทย",
}
_SUPPORTED_LANGS = ["zh", "en", "fr", "de", "es", "hi", "vi", "th"]

def _lang_label(code: str, ui_lang: str) -> str:
    # 优先用当前 UI 语言包的 lang.<code>；没有就用内置本地名兜底
    v = t(f"lang.{code}", ui_lang)
    return v if v else _LANG_PRETTY.get(code, code)

# -----------------------------
# 通用：返回主页
# -----------------------------
def back_home_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([[ _btn(_tt("menu.back", lang), "menu:main") ]])

# -----------------------------
# 语言选择（使用兜底）
# -----------------------------
def language_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for code in _SUPPORTED_LANGS:
        row.append(_btn(_lang_label(code, lang), f"lang:{code}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([ _btn(_tt("menu.back", lang), "menu:main") ])
    return _kb(rows)

# -----------------------------
# 主菜单（含帮助中心）
# -----------------------------
def main_menu(lang: str = "zh", is_admin: bool = False) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [
            _btn(_tt("menu.send", lang), "hb:start"),
            _btn(_tt("menu.recharge", lang), "recharge:main"),
        ],
        [
            _btn(_tt("menu.today", lang), "today:me"),
            _btn(_tt("menu.assets", lang), "balance:main"),
        ],
    ]
    if getattr(flags, "ENABLE_WELFARE", True):
        rows.append([
            _btn(_tt("menu.welfare", lang), "wf:main"),
            _btn(_tt("menu.language", lang), "lang:menu"),
        ])
    else:
        rows.append([ _btn(_tt("menu.language", lang), "lang:menu") ])

    rows.append([ _btn(_tt("menu.help", lang), "help:main") ])

    if is_admin:
        rows.append([ _btn(_tt("menu.admin", lang), "admin:main") ])
    return _kb(rows)

# -----------------------------
# 管理面板
# -----------------------------
def admin_menu(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [ _btn(_tt("admin.stats", lang), "admin:stats") ],
        [ _btn(_tt("admin.settings", lang), "admin:settings") ],
        [ _btn(_tt_first(["admin.adjust.button", "admin.adjust"], lang), "admin:adjust") ],
        [ _btn(_tt_first(["admin.covers.menu_title", "admin.covers"], lang), "admin:covers") ],
        [ _btn(_tt_first(["admin.export.button", "admin.export.title", "admin.export_text", "admin.export"], lang), "admin:export") ],
        # ===== 新增：危险操作分区（清零功能） =====
        [ _btn(_tt_first(["admin.reset.btn.all", "admin.reset.all"], lang) or "⚠️ 批量清零（全体）", "admin:reset_all") ],
        [ _btn(_tt_first(["admin.reset.btn.select", "admin.reset.select"], lang) or "🧹 指定清零（按ID/@）", "admin:reset_select") ],
        # ===== 返回 =====
        [ _btn(_tt("menu.back", lang), "menu:main") ],
    ])

def admin_export_scope_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [ _btn(_tt("admin.export.by_user", lang), "admin:export:by_user") ],
        [ _btn(_tt("admin.export.all", lang), "admin:export:all") ],
        [ _btn(_tt("menu.back", lang), "admin:main") ],
    ])

def admin_export_user_confirm_kb(user_id: int, lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [ _btn(_tt("admin.export.confirm_user", lang), f"admin:export:user:{user_id}") ],
        [ _btn(_tt("menu.back", lang), "admin:export") ],
    ])

def admin_covers_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [ _btn(_tt("admin.covers.upload_btn", lang), "admin:covers:add") ],
        [ _btn(_tt("admin.covers.delete_btn", lang), "admin:covers:del") ],
        [ _btn(_tt("menu.back", lang), "admin:main") ],
    ])

# -----------------------------
# 红包交互（群内卡片）
# -----------------------------
def hb_grab_kb(envelope_id: int, lang: str = "zh") -> InlineKeyboardMarkup:
    grab_text  = _tt_first(["hb.grab", "hb.start"], lang)
    relay_text = _tt_first(["hb.relay", "hb.mvp_send", "env.mvp_send_btn"], lang)
    refresh_tx = _tt_first(["common.refresh", "hb.refresh"], lang)
    return _kb([
        [ _btn(grab_text,  f"hb:grab:{envelope_id}") ],
        [ _btn(relay_text, f"hb:relay:{envelope_id}") ],
        [ _btn(refresh_tx, f"hb:refresh:{envelope_id}") ],
    ])

def hb_rank_kb(envelope_id: int, lang: str = "zh", show_next: bool = True) -> InlineKeyboardMarkup:
    return _kb([])

# -----------------------------
# 发包向导键盘
# -----------------------------
def env_mode_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [
            _btn(_tt("env.mode.usdt", lang), "env:mode:USDT"),
            _btn(_tt("env.mode.ton",  lang), "env:mode:TON"),
        ],
        [ _btn(_tt("env.mode.point", lang), "env:mode:POINT") ],
        [ _btn(_tt("menu.back", lang), "menu:main") ],
    ])

def env_amount_kb(token: str = "", lang: str = "zh") -> InlineKeyboardMarkup:
    quicks = list(getattr(flags, "ENV_QUICK_AMOUNTS", (1, 5, 10, 20, 50, 100)))
    rows: List[List[InlineKeyboardButton]] = []; row: List[InlineKeyboardButton] = []
    for i, n in enumerate(quicks, 1):
        row.append(_btn(str(n), f"env:amt:{n}"))
        if i % 3 == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([ _btn(_tt("menu.back", lang), "env:back:mode") ])
    return _kb(rows)

def env_shares_kb(token: str = "", lang: str = "zh") -> InlineKeyboardMarkup:
    quicks = list(getattr(flags, "ENV_QUICK_SHARES", (2, 3, 5, 8, 10, 15)))
    rows: List[List[InlineKeyboardButton]] = []; row: List[InlineKeyboardButton] = []
    for i, n in enumerate(quicks, 1):
        row.append(_btn(str(n), f"env:shares:{n}"))
        if i % 3 == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([ _btn(_tt("menu.back", lang), "env:back:amount") ])
    return _kb(rows)

def env_memo_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    rows.append([ InlineKeyboardButton(text=_tt("common.skip", lang), callback_data="env:memo:skip") ])
    rows.append([ InlineKeyboardButton(text=_tt("menu.back", lang), callback_data="env:back:shares") ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def env_distribution_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    rows.append([ _btn(_tt("env.dist.random", lang), "env:dist:random") ])
    if getattr(flags, "ENABLE_FIXED_DISTRIBUTION", False):
        rows[0].append(_btn(_tt("env.dist.fixed", lang), "env:dist:fixed"))
    rows.append([ _btn(_tt("menu.back", lang), "env:back:shares") ])
    return _kb(rows)

def env_location_kb(lang: str = "zh", allow_current: bool = True, allow_dm: bool = True) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if allow_current:
        rows.append([ _btn(_tt("env.loc.here", lang), "env:loc:here") ])
    if allow_dm:
        rows.append([ _btn(_tt("env.loc.dm", lang), "env:loc:dm") ])
    rows.append([ _btn(_tt("env.loc.pick", lang), "env:loc:pick") ])
    rows.append([ _btn(_tt("menu.back", lang), "env:back:dist") ])
    return _kb(rows)

def env_confirm_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [ _btn(_tt("env.send", lang), "env:confirm") ],
        [ _btn(_tt_first(["env.cover.change", "env.cover.choose"], lang), "env:cover:choose") ],
        [ _btn(_tt("env.cancel", lang), "env:cancel") ],
    ])

def env_back_kb(lang: str = "zh", to: str = "menu") -> InlineKeyboardMarkup:
    mapping = {
        "menu": "menu:main",
        "mode": "env:back:mode",
        "amount": "env:back:amount",
        "shares": "env:back:shares",
        "dist": "env:back:dist",
        "loc": "env:back:loc",
        "confirm": "env:back:confirm",
    }
    return _kb([[ _btn(_tt("menu.back", lang), mapping.get(to, "menu:main")) ]])

# -----------------------------
# 充值中心
# -----------------------------
def recharge_main_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = [ _btn(_token_display("USDT", lang), "recharge:new:USDT") ]
    if getattr(settings, "RECHARGE_ENABLE_TON", True):
        row.append(_btn(_token_display("TON", lang), "recharge:new:TON"))
    rows.append(row)
    rows.append([ _btn(_token_display("POINT", lang), "recharge:new:POINT") ])
    rows.append([ _btn(_tt("menu.back", lang), "menu:main") ])
    return _kb(rows)

def recharge_amount_kb(lang: str = "zh", quicks: Optional[Iterable[int]] = None) -> InlineKeyboardMarkup:
    if quicks is None:
        quicks = getattr(flags, "RECHARGE_QUICK_AMOUNTS", (10, 50, 100, 200))
    lst = list(quicks)
    rows: List[List[InlineKeyboardButton]] = []; row: List[InlineKeyboardButton] = []
    for i, n in enumerate(lst, 1):
        row.append(_btn(str(n), f"recharge:amt:{n}"))
        if i % 3 == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([ _btn(_tt("recharge.custom", lang), "recharge:amt:custom") ])
    rows.append([ _btn(_tt("menu.back", lang), "recharge:main") ])
    return _kb(rows)

def recharge_order_kb(order_id: int, lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [ _btn(_tt("recharge.refresh", lang), f"recharge:refresh:{order_id}") ],
        [ _btn(_tt("menu.back", lang), "recharge:main") ],
    ])

def recharge_invoice_kb(order_id: int, lang: str = "zh", payment_url: Optional[str] = None) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if payment_url:
        rows.append([ _btn(_tt("recharge.link", lang) or "🔗 支付链接", "noop", url=payment_url) ])
    rows.append([
        _btn(_tt("recharge.copy_addr", lang) or "📋 复制地址", f"recharge:copy_addr:{order_id}"),
        _btn(_tt("recharge.copy_amount", lang) or "📋 复制金额", f"recharge:copy_amt:{order_id}"),
    ])
    rows.append([ _btn(_tt("recharge.refresh", lang) or "🔄 刷新状态", f"recharge:refresh:{order_id}") ])
    rows.append([ _btn(_tt("menu.back", lang) or "⬅️ 返回", "recharge:main") ])
    return _kb(rows)

def recharge_loading_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([[ _btn(_tt("menu.back", lang), "recharge:main") ]])

# -----------------------------
# 福利中心
# -----------------------------
def welfare_menu(lang: str = "zh") -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [ _btn(_tt("welfare.signin_btn", lang), "wf:signin") ],
    ]
    if getattr(flags, "ENABLE_INVITE", True):
        rows[0].append(_btn(_tt("welfare.invite_btn", lang), "wf:invite"))
    rows.append([
        _btn(_tt("welfare.promo_btn", lang), "wf:promo"),
        _btn(_tt("welfare.rules_btn", lang), "wf:rules"),
    ])
    rows.append([ _btn(_tt("menu.back", lang), "menu:main") ])
    return _kb(rows)

def invite_progress_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [
            _btn(_tt("welfare.invite_share_btn", lang), "wf:invite:share"),
            _btn(_tt("welfare.invite_redeem_btn", lang), "wf:invite:redeem"),
        ],
        [ _btn(_tt("menu.back", lang), "wf:main") ],
    ])

def invite_main_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [
            _btn(_tt("welfare.invite_share_btn", lang), "invite:share"),
            _btn(_tt("welfare.invite_redeem_btn", lang), "invite:redeem"),
        ],
        [ _btn(_tt("menu.back", lang), "wf:main") ],
    ])

# -----------------------------
# 我的资产
# -----------------------------
def asset_menu(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [
            _btn(_token_display("USDT", lang), "balance:USDT"),
            _btn(_token_display("TON", lang), "balance:TON"),
        ],
        [ _btn(_token_display("POINT", lang), "balance:POINT") ],
        [
            _btn(_tt("balance.recharge", lang), "recharge:main"),
            _btn(_tt("balance.withdraw", lang), "withdraw:main"),
        ],
        [ _btn(_tt("menu.back", lang), "menu:main") ],
    ])

# -----------------------------
# 目标群选择与绑定
# -----------------------------
def target_group_current_kb(title: str, chat_id: int, lang: str = "zh") -> InlineKeyboardMarkup:
    use_text = _tt_first(["env.tg.use_this", "env.tg.use"], lang) or "👉 使用此群继续"
    change_text = _tt_first(["env.tg.change", "env.tg.change"], lang) or "🔄 更换目标群"  # 修复：原版本未定义 change_text
    return _kb([
        [ _btn(use_text, f"env:tg:use:{chat_id}") ],
        [ _btn(change_text, "env:tg:change") ],
        [ _btn(_tt("menu.back", lang), "menu:main") ],
    ])

def target_group_select_kb(items: List[Tuple[int, str]], lang: str = "zh") -> InlineKeyboardMarkup:
    if not items:
        return target_group_unbound_kb(lang)
    rows: List[List[InlineKeyboardButton]] = []
    for chat_id, title in items:
        title_show = (title or str(chat_id))[:48]
        rows.append([ _btn(title_show, f"env:tg:{chat_id}") ])
    rows.append([ _btn(_tt_first(["env.tg.bind_new", "env.tg.add_group"], lang), "env:tg:bind_help") ])
    rows.append([ _btn(_tt("menu.back", lang), "menu:main") ])
    return _kb(rows)

def target_group_unbound_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    go_bind = _tt_first(["env.tg.go_bind", "env.tg.bind_in_group"], lang) or "🪄 在群里绑定"
    manual = _tt_first(["env.tg.manual_pick", "env.loc.pick"], lang) or "🎯 手动指定群聊"
    return _kb([
        [ _btn(go_bind, "env:tg:bind_help") ],
        [ _btn(manual, "env:loc:pick") ],
        [ _btn(_tt("menu.back", lang), "menu:main") ],
    ])

# -----------------------------
# 红包封面选择
# -----------------------------
def env_cover_source_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [ _btn(_tt("env.cover.from_channel", lang), "env:cover:channel") ],
        [ _btn(_tt("env.cover.none", lang), "env:cover:none") ],
        [ _btn(_tt("menu.back", lang), "env:back:shares") ],
    ])

def env_cover_entry_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    return env_cover_source_kb(lang)

def _cover_tuple_from_item(item: Any) -> Tuple[str, str]:
    if hasattr(item, "id"):
        cid = str(getattr(item, "id", "0"))
        title = getattr(item, "title", None) or getattr(item, "slug", None)
        if not title:
            mid = getattr(item, "message_id", None)
            if mid is not None:
                try:
                    title = f"#%d" % int(mid)
                except Exception:
                    title = f"#{mid}"
        return (cid, str(title or ""))
    if isinstance(item, (tuple, list)) and len(item) >= 1:
        cid = str(item[0]); title = item[1] if len(item) >= 2 else ""
        return (cid, str(title or ""))
    return ("0", "")

def env_cover_list_kb(
    items: List[Any],
    page: int = 1,
    page_size: int = 9,
    lang: str = "zh",
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    norm_items: List[Tuple[str, str]] = [_cover_tuple_from_item(x) for x in items]
    if not has_prev and page > 1:
        has_prev = True
    if not has_next and len(norm_items) >= int(page_size):
        has_next = True

    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for i, (cover_id, title) in enumerate(norm_items[:page_size], 1):
        text = (title or _tt("env.cover.item", lang))[:24]
        row.append(_btn(text, f"env:cover:set:{cover_id}"))
        if i % 3 == 0:
            rows.append(row); row = []
    if row: rows.append(row)

    nav: List[InlineKeyboardButton] = []
    if has_prev:
        nav.append(_btn(_tt("env.cover.prev", lang), f"env:cover:page:{max(page-1,1)}"))
    if has_next:
        nav.append(_btn(_tt("env.cover.next", lang), f"env:cover:page:{page+1}"))
    if nav: rows.append(nav)

    rows.append([ _btn(_tt("menu.back", lang), "env:cover:source") ])
    return _kb(rows)

def env_cover_selected_kb(cover_id: str, lang: str = "zh") -> InlineKeyboardMarkup:
    return _kb([
        [ _btn(_tt("env.cover.use_this", lang), f"env:cover:confirm:{cover_id}") ],
        [ _btn(_tt("env.cover.repick", lang), "env:cover:channel") ],
        [ _btn(_tt("env.cover.none", lang), "env:cover:none") ],
        [ _btn(_tt("menu.back", lang), "env:back:shares") ],
    ])
