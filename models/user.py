# -*- coding: utf-8 -*-
"""
用户模型：
- 用户基础信息：tg_id、username、language、role
- 资产余额：usdt / ton / point / energy
- 目标群记录：last_target_chat_id / last_target_chat_title（用于“发红包”默认投放群）
- 工具函数：
    get_or_create_user
    update_balance              # ✅ 统一的加/扣余额入口，已内置“余额不能为负”的硬校验
    can_spend                   # ✅ 扣款前快速判断是否足额（不落库、不创建用户）
    get_balance                 # ✅ 读取单一币种余额（USDT/TON -> Decimal，POINT/ENERGY -> int）
    get_balance_summary
    set_last_target_chat
    get_last_target_chat

兼容性说明：
1) `update_balance` 兼容旧参数名 `delta`（例如 admin_adjust 里用 delta=...）。
2) `update_balance(..., write_ledger=True)` 可选写流水；默认 False 以避免与上层重复记账。
3) 金额统一量化到 6 位小数（USDT/TON），POINT/ENERGY 按整数处理。
"""

from __future__ import annotations
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Union, Tuple, Any

from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Enum
from sqlalchemy.orm import Session, synonym

from .db import Base, get_session, DECIMAL  # 使用 DECIMAL 安全类型
# 用于默认语言（兼容两种目录结构）
try:
    from config.settings import settings  # 优先：config/settings.py
except Exception:  # 回退：根目录 settings.py
    from settings import settings  # type: ignore

import enum

# 可选：流水（仅在 write_ledger=True 时使用，默认不触发以避免重复记账）
try:
    from .ledger import add_ledger_entry, LedgerType  # 轻耦合：不在模块导入阶段强依赖使用
except Exception:  # pragma: no cover
    add_ledger_entry = None  # type: ignore
    LedgerType = None        # type: ignore


# ---------- 枚举 ----------
class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


# ---------- ORM ----------
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tg_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String(64), nullable=True)
    language = Column(String(8), default="zh")
    role = Column(Enum(UserRole), default=UserRole.USER, nullable=False)

    # 金额类：用 DECIMAL(6)（SQLite 下 TEXT 保存，消除 Decimal 告警，精度保真）
    usdt_balance = Column(DECIMAL(6), default=0)
    ton_balance = Column(DECIMAL(6), default=0)

    # 计数类：整型
    point_balance = Column(Integer, default=0)
    energy_balance = Column(Integer, default=0)

    # 最近绑定/使用的“目标群”（用于发红包默认群；仅冗余展示，不影响 Telegram 实际权限）
    last_target_chat_id = Column(BigInteger, nullable=True)          # 例如 -100xxxxxxxxxx
    last_target_chat_title = Column(String(128), nullable=True)      # 群名，便于 UI 展示

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # ✅ 兼容旧代码：提供别名 `created` 指向 `created_at`（使用 synonym 消除 SAWarning）
    created = synonym("created_at")

    def touch(self) -> None:
        self.updated_at = datetime.utcnow()


# ---------- 内部工具 ----------
_DEC_6 = Decimal("0.000001")

def _q6(x: Union[Decimal, float, int]) -> Decimal:
    """量化到 6 位小数，向下取整，避免浮点误差。"""
    return Decimal(str(x)).quantize(_DEC_6, rounding=ROUND_DOWN)

# ✅ 升级：支持 8 种语言 + 地区码规范化
_LANG_SET = {"zh", "en", "fr", "de", "es", "hi", "vi", "th"}

# —— 允许用 settings.SUPPORTED_LANGS 扩展支持语言（若存在）——
try:
    _SUP = getattr(settings, "SUPPORTED_LANGS", None)
    if _SUP:
        _LANG_SET |= {str(x).split("-")[0].lower() for x in _SUP}
except Exception:
    pass

def _canon_lang(code: Optional[str]) -> str:
    """
    规范化语言码：
    1) 空值 → settings.DEFAULT_LANG 或 zh
    2) 完整码命中（如 fr-ca 在 _LANG_SET 里）直接返回
    3) 主码命中（fr-ca → fr）
    4) 历史兼容：前缀 zh / en 分别回落到 zh / en
    5) 其余回落默认
    """
    default_lang = (getattr(settings, "DEFAULT_LANG", None) or "zh").split("-")[0].lower()
    if not code:
        return default_lang

    c = str(code).strip().lower().replace("_", "-")
    if not c:
        return default_lang

    # 完整命中（允许你未来把 'pt-br' 也加进 _LANG_SET）
    if c in _LANG_SET:
        return c

    # 主码命中
    primary = c.split("-", 1)[0]
    if primary in _LANG_SET:
        return primary

    # 历史兼容
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"

    return default_lang

def _canon_token(token: str) -> str:
    return str(token or "").upper()

def _field_of(token_up: str) -> str:
    """把币种映射到模型字段名。"""
    if token_up == "USDT":
        return "usdt_balance"
    if token_up == "TON":
        return "ton_balance"
    if token_up in ("POINT", "POINTS"):
        return "point_balance"
    if token_up == "ENERGY":
        return "energy_balance"
    raise ValueError(f"Unknown token type: {token_up}")


# ---------- 读或建用户 ----------
def get_or_create_user(
    session: Session,
    tg_id: int,
    username: Optional[str] = None,
    lang: Optional[str] = None,
    role: Optional[UserRole] = None,
) -> 'User':
    """
    读取或创建用户：
    - 首次创建：language 使用传入 lang 的规范化值，否则用 settings.DEFAULT_LANG
    - 后续：若 username/lang 发生变化，会更新对应字段
    """
    user = session.query(User).filter_by(tg_id=tg_id).first()
    if user:
        changed = False
        if username and user.username != username:
            user.username = username
            changed = True
        if lang and user.language != _canon_lang(lang):
            user.language = _canon_lang(lang)
            changed = True
        if changed:
            user.touch()
            session.add(user)
        return user

    user = User(
        tg_id=int(tg_id),
        username=username,
        language=_canon_lang(lang),
        role=role or UserRole.USER,
    )
    session.add(user)
    session.flush()  # 需要 id 的地方可立即使用
    return user


# ---------- 可用性判断（只读预检，不创建用户） ----------
def can_spend(
    session: Session,
    user: Union['User', int],
    token: str,
    amount: Union[Decimal, float, int],
) -> Tuple[bool, Union[Decimal, int]]:
    """
    判断用户余额是否足以扣除 amount（不会落库、不会创建用户）。
    返回 (ok, remain_after)
    - 对 USDT/TON：使用 Decimal，量化到 6 位
    - 对 POINT/ENERGY：整型
    """
    token_up = _canon_token(token)
    field = _field_of(token_up)

    if isinstance(user, int):
        u = session.query(User).filter_by(tg_id=int(user)).first()
        if not u:
            # 预检：用户不存在时按 0 余额处理，不创建记录
            if token_up in ("USDT", "TON"):
                need = _q6(amount)
                remain = _q6(0) - need
                return (remain >= 0, remain)
            else:
                need_i = int(amount)
                remain_i = 0 - need_i
                return (remain_i >= 0, remain_i)
    else:
        u = user

    cur = getattr(u, field) or 0
    if token_up in ("USDT", "TON"):
        need = _q6(amount)
        remain = Decimal(str(cur)) - need
        return (remain >= 0, remain)
    else:
        need = int(amount)
        remain = int(cur) - need
        return (remain >= 0, remain)


# ---------- 新增：读取单一币种余额 ----------
def get_balance(
    session: Session,
    user: Union['User', int],
    token: str,
) -> Union[Decimal, int]:
    """
    读取用户的单一币种余额（不做变更，不提交事务）。
    - USDT/TON：返回 Decimal（量化至 6 位，便于与 Decimal 比较/运算）
    - POINT/ENERGY：返回 int

    用途：预校验/比较，如：
        need = Decimal("98.54")
        bal = get_balance(session, uid, "USDT")   # -> Decimal
        if bal >= need: ...
    """
    token_up = _canon_token(token)
    field = _field_of(token_up)

    if isinstance(user, int):
        u = session.query(User).filter_by(tg_id=int(user)).first()
        if not u:
            # 未创建则视为 0（但不落库）
            if token_up in ("USDT", "TON"):
                return _q6(0)
            else:
                return 0
    else:
        u = user

    cur = getattr(u, field) or 0
    if token_up in ("USDT", "TON"):
        # 统一用 Decimal 返回，避免上层 float 比较误差
        return _q6(cur)
    else:
        return int(cur)


# ---------- 统一余额更新（硬闸：余额不能为负） ----------
def update_balance(
    session: Session,
    user: Union['User', int],
    token: str,
    amount: Union[Decimal, float, int, None] = None,
    *,
    # —— 兼容旧调用：允许 delta=... 作为别名（admin_adjust 里使用了 delta）——
    delta: Union[Decimal, float, int, None] = None,
    # —— 可选流水写入（默认 False，避免与上层重复记账）——
    write_ledger: bool = False,
    ltype: Optional["LedgerType | str"] = None,
    ref_type: Optional[str] = None,
    ref_id: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """
    更新用户余额（正负皆可），并可选地写一条 Ledger 流水（默认不写）；
    ❗已内置硬校验：更新后余额不得为负，若将为负则抛 ValueError('INSUFFICIENT_BALANCE')。

    - token: USDT / TON / POINT / ENERGY（不区分大小写；POINT/ENERGY 取整）
    - USDT/TON 统一量化 6 位小数
    - 事务由调用方控制（建议与业务对象/流水在同一事务内）

    兼容性：
    - 若调用方传了 delta=xx 且 amount 未给，则以 delta 值作为调整金额（向后兼容旧代码）。
    - 若同时传入 amount 与 delta，将以 amount 为准。

    返回：
    - dict 结构，包含 token 与更新后的四种余额快照，便于上层记录或调试。
    """
    # —— 1) 解析金额 ——
    if amount is None and delta is None:
        raise ValueError("update_balance requires 'amount' or legacy 'delta'")
    if amount is None and delta is not None:
        amount = delta  # 兼容旧参数名
    assert amount is not None

    # —— 2) 解析用户对象（容错：允许直接传 tg_id）——
    if isinstance(user, int):
        u = session.query(User).filter_by(tg_id=int(user)).first()
        if not u:
            u = get_or_create_user(session, tg_id=int(user))
    else:
        u = user

    token_up = _canon_token(token)
    field = _field_of(token_up)

    # 当前值
    cur_val = getattr(u, field) or 0

    # —— 3) 计算新值并做“不能为负”的硬校验 ——
    ledger_amount_norm: Union[Decimal, int]  # 与余额调整一致的规范化数值，用于流水
    if token_up in ("USDT", "TON"):
        adj = _q6(amount)
        ledger_amount_norm = adj
        new_val = Decimal(str(cur_val)) + adj
        if new_val < 0:
            raise ValueError("INSUFFICIENT_BALANCE")
        setattr(u, field, new_val)
    else:
        adj_i = int(amount)
        ledger_amount_norm = adj_i
        new_i = int(cur_val) + adj_i
        if new_i < 0:
            raise ValueError("INSUFFICIENT_BALANCE")
        setattr(u, field, new_i)

    u.touch()
    session.add(u)

    # —— 4) 可选：写入流水（默认不写；避免与上层重复）——
    if write_ledger:
        if add_ledger_entry is None or LedgerType is None:
            raise RuntimeError("ledger module not available; cannot write ledger")
        # 选择流水类型
        lt = ltype
        if lt is None:
            # 默认当作“调整”
            lt = getattr(LedgerType, "ADJUSTMENT") if hasattr(LedgerType, "ADJUSTMENT") else "ADJUSTMENT"
        # 记账金额与余额变动采用“同一规范化数值”，确保账实完全一致
        add_ledger_entry(
            session,
            user_tg_id=int(u.tg_id),
            ltype=lt,
            token=token_up,
            amount=ledger_amount_norm if isinstance(ledger_amount_norm, Decimal) else Decimal(ledger_amount_norm),
            ref_type=ref_type,
            ref_id=str(ref_id) if ref_id is not None else None,
            note=note or "",
        )

    # —— 5) 返回快照（便于日志/调试/上层展示；不影响数据库精度）——
    return {
        "token": token_up,
        "usdt": float(u.usdt_balance or 0),
        "ton": float(u.ton_balance or 0),
        "point": int(u.point_balance or 0),
        "energy": int(u.energy_balance or 0),
    }


# ---------- 余额摘要 ----------
def get_balance_summary(tg_id: int) -> Dict[str, float]:
    """
    返回用户余额摘要 dict: {usdt, ton, point, energy}
    （以 float/int 友好展示；并不改变数据库精度）
    """
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=int(tg_id)).first()
        if not u:
            return {"usdt": 0.0, "ton": 0.0, "point": 0, "energy": 0}
        return {
            "usdt": float(u.usdt_balance or 0),
            "ton": float(u.ton_balance or 0),
            "point": int(u.point_balance or 0),
            "energy": int(u.energy_balance or 0),
        }


# ---------- 最近目标群 ----------
def set_last_target_chat(session: Session, tg_id: int, chat_id: int, title: Optional[str] = None) -> None:
    """
    记录/更新用户最近使用（绑定）的目标群：
    - chat_id: 群/超群/频道 ID（一般为负数，如 -100xxxxxxxxxx）
    - title:   群名（可选，仅用于 UI 展示）
    说明：不做权限校验；请在上层逻辑做“机器人是否在群里且可发言”的预检。
    """
    user = session.query(User).filter_by(tg_id=int(tg_id)).first()
    if not user:
        user = get_or_create_user(session, tg_id=int(tg_id))

    # 字段兼容：如果历史库还没有这两列，SQLAlchemy 仍会有属性；若出现异常，上层应捕获
    user.last_target_chat_id = int(chat_id)
    if title:
        user.last_target_chat_title = str(title)[:128]  # 保护上限
    user.touch()
    session.add(user)
    # 不在此处 commit，由调用方控制事务


def get_last_target_chat(session: Session, tg_id: int) -> Tuple[Optional[int], Optional[str]]:
    """
    读取用户最近绑定/使用的目标群（若未设置则返回 (None, None)）
    """
    user = session.query(User).filter_by(tg_id=int(tg_id)).first()
    if not user:
        return (None, None)
    # 字段兼容：若老库缺列，属性值可能为 None
    return (getattr(user, "last_target_chat_id", None), getattr(user, "last_target_chat_title", None))


# ======================================================================
# ===============  新增：昵称/语言等资料的全局同步入口  ====================
# ======================================================================

def _display_name_from_tg(tg_user) -> str:
    """
    统一计算 Telegram 的“显示名”：
    优先 full_name → 其次 first+last → 其次（历史）name → 最后 username
    """
    # aiogram 的 User 对象带 full_name 属性（拼好的 First + Last）
    full_name = getattr(tg_user, "full_name", None)
    if full_name:
        s = str(full_name).strip()
        if s:
            return s

    first = getattr(tg_user, "first_name", "") or ""
    last  = getattr(tg_user, "last_name", "") or ""
    if first or last:  # ✅ 修正“或”为 or
        return f"{first} {last}".strip()

    # 兼容：有些项目历史上把显示名存到 name
    name = getattr(tg_user, "name", None)
    if name:
        s = str(name).strip()
        if s:
            return s

    return (getattr(tg_user, "username", "") or "").strip()


def upsert_user_from_tg(tg_user) -> 'User':
    """
    把 Telegram 用户对象入库/更新（可在“每次交互”调用）：
    - 同步 username / first_name / last_name / full_name / language
    - 统一“显示名”写入 full_name（若模型存在 name 字段也会一并写入）
    - 刷新 updated_at
    - 若你的 users 表暂时没有这些列，代码会自动跳过对应赋值，不会报错
    """
    tg_id = int(getattr(tg_user, "id"))
    now = datetime.utcnow()

    # 预取字段（若 tg_user 没有该属性，统一用 None）
    username: Optional[str]   = getattr(tg_user, "username", None)
    first_name: Optional[str] = getattr(tg_user, "first_name", None)
    last_name: Optional[str]  = getattr(tg_user, "last_name", None)
    language: Optional[str]   = (
        getattr(tg_user, "language_code", None)
        or getattr(tg_user, "language", None)
    )
    display_name: str = _display_name_from_tg(tg_user)

    with get_session() as s:
        u: Optional[User] = s.query(User).filter(User.tg_id == tg_id).first()
        created = False
        if not u:
            u = User(tg_id=tg_id, created_at=now)
            created = True
            s.add(u)

        # 软写入：如果模型没有该列，不报错，直接跳过
        def set_attr(obj, field: str, value):
            if hasattr(obj, field):
                setattr(obj, field, value)

        # 同步基础资料
        set_attr(u, "username", username)
        set_attr(u, "first_name", first_name)
        set_attr(u, "last_name", last_name)
        set_attr(u, "language", language)      # 若表里没有 language，会被跳过
        set_attr(u, "full_name", display_name) # 统一显示名写入 full_name
        set_attr(u, "name", display_name)      # 老历史项目可能用 name
        set_attr(u, "updated_at", now)

        # 可选：首次建档时也把 last_active_at 补上（若你模型有）
        if hasattr(u, "last_active_at") and u.last_active_at is None:
            u.last_active_at = now

        s.commit()
        if created:
            s.refresh(u)
        return u
