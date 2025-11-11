# models/ledger.py
# -*- coding: utf-8 -*-
"""
流水账（Ledger）：
- 记录 USDT / TON / 积分(POINT) / 能量(ENERGY) 的变动
- 统一用于“我的记录”与审计对账

口径：
  • amount：正数=收入，负数=支出（保留 6 位小数）
  • token：统一大写（USDT / TON / POINT / ENERGY）
  • type ：见 LedgerType
       - 新规范：RECHARGE / WITHDRAW / HONGBAO_SEND / HONGBAO_GRAB / ADJUSTMENT / RESET / ...
       - 兼容历史：SEND、GRAB、ENVELOPE_GRAB、ENVELOPE_SEND（为了兼容旧库中已有值）
"""

from __future__ import annotations
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Union

from sqlalchemy import (
    Column, Integer, BigInteger, String, DateTime, Enum, Index
)
from sqlalchemy.orm import Session
from sqlalchemy.ext.hybrid import hybrid_property

from .db import Base, get_session, DECIMAL  # 使用 DECIMAL(6) 安全类型
import enum


class LedgerType(str, enum.Enum):
    # —— 核心业务（新规范）——
    RECHARGE = "RECHARGE"                      # 充值入账
    WITHDRAW = "WITHDRAW"                      # 提现支出
    HONGBAO_SEND = "HONGBAO_SEND"              # 发红包（支出）
    HONGBAO_GRAB = "HONGBAO_GRAB"              # 抢红包（收入）

    # —— 活动/福利 —— 
    INVITE_REWARD = "INVITE_REWARD"            # 邀请奖励（积分/能量）
    SIGNIN = "SIGNIN"                          # 每日签到（积分）

    # —— 兑换 —— 
    EXCHANGE_POINTS_TO_PROGRESS = "EXCHANGE_POINTS_TO_PROGRESS"  # 积分兑进度（积分支出）
    EXCHANGE_ENERGY_TO_POINTS = "EXCHANGE_ENERGY_TO_POINTS"      # 能量兑积分（能量支出、积分收入）

    # —— 调整/其它 —— 
    ADJUSTMENT = "ADJUSTMENT"                  # 手工调整/运维补发
    RESET = "RESET"                            # 批量清零（全体/指定），本项目新增
    OTHER = "OTHER"

    # ===== 以下为“兼容历史”的别名（名称保留，值映射到新规范）=====
    # 代码里若还有 LedgerType.SEND / LedgerType.GRAB 的写法，会落到新规范上
    SEND = "HONGBAO_SEND"                      # 代码别名（写库时存值 HONGBAO_SEND）
    GRAB = "HONGBAO_GRAB"                      # 代码别名（写库时存值 HONGBAO_GRAB)

    # 为了兼容“数据库里已经存在的老值”，提供以下成员，便于反序列化：
    ENVELOPE_SEND = "SEND"                     # 旧库可能直接写入了 "SEND"
    ENVELOPE_GRAB = "GRAB"                     # 旧库可能直接写入了 "GRAB"


class Ledger(Base):
    __tablename__ = "ledgers"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 真实底层列：Telegram 用户 ID
    user_tg_id = Column(BigInteger, index=True, nullable=False)

    # 业务分类（Python Enum）；注意上面做了新旧值兼容
    type = Column(Enum(LedgerType), nullable=False, default=LedgerType.OTHER)

    # 资产类型：USDT / TON / POINT / ENERGY
    token = Column(String(16), nullable=False)

    # 本次变动金额（正数=收入，负数=支出）
    amount = Column(DECIMAL(6), nullable=False)  # 使用 DECIMAL(6)：SQLite 下以 TEXT 存储，避免浮点误差

    # 业务引用（例如 envelope_id / order_id）
    ref_type = Column(String(32), nullable=True)   # "ENVELOPE" / "ORDER" / "INVITE" ...
    ref_id = Column(String(64), nullable=True)

    note = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_user_created", "user_tg_id", "created_at"),
        Index("idx_user_token_created", "user_tg_id", "token", "created_at"),
        Index("idx_ref", "ref_type", "ref_id"),
    )

    # --- 别名：兼容历史代码 / 控制器的列名探测 ---
    @hybrid_property
    def tg_id(self) -> int:
        return self.user_tg_id

    @tg_id.expression
    def tg_id(cls):
        return cls.user_tg_id

    @hybrid_property
    def user_id(self) -> int:
        return self.user_tg_id

    @user_id.expression
    def user_id(cls):
        return cls.user_tg_id

    @hybrid_property
    def uid(self) -> int:
        return self.user_tg_id

    @uid.expression
    def uid(cls):
        return cls.user_tg_id

    # --- 兼容：模板/控制器里常用的时间与类型别名 ---
    @hybrid_property
    def ts(self):
        """历史模板里有用 lg.ts 的写法，这里等价于 created_at。"""
        return self.created_at

    @hybrid_property
    def ltype(self) -> Optional[str]:
        """把 Enum 转成字符串，模板里直接 {{ lg.ltype }} 渲染友好。"""
        return self.type.value if self.type else None

    # --- 业务引用字段的友好别名（导出/搜索） ---
    @hybrid_property
    def order_id(self) -> Optional[str]:
        # 简化处理：直接复用 ref_id；如需严格限制为 ref_type=="ORDER"，可在查询层增加条件
        return self.ref_id

    @order_id.expression
    def order_id(cls):
        return cls.ref_id

    @hybrid_property
    def envelope_id(self) -> Optional[str]:
        # 简化：直接复用 ref_id
        return self.ref_id

    @envelope_id.expression
    def envelope_id(cls):
        return cls.ref_id


# ========== 工具函数 ==========
_DEC = Decimal("0.000001")

def _q(x: Union[Decimal, float, int, str]) -> Decimal:
    """统一量化到 6 位小数，避免浮点误差与数据库精度不一致。"""
    return Decimal(str(x)).quantize(_DEC, rounding=ROUND_DOWN)


def _normalize_ledger_type(ltype: Union[LedgerType, str]) -> LedgerType:
    """
    将任意传入的枚举/字符串规范化为 LedgerType。
    兼容以下写法（大小写不敏感）：
      - "HONGBAO_SEND" / "SEND" / LedgerType.SEND / LedgerType.HONGBAO_SEND
      - "HONGBAO_GRAB" / "GRAB" / "ENVELOPE_GRAB" / LedgerType.GRAB / LedgerType.HONGBAO_GRAB
      - "RESET" / LedgerType.RESET
      - 以及其它明确存在于 LedgerType 的名称/值
    未识别则回落为 LedgerType.OTHER。
    """
    if isinstance(ltype, LedgerType):
        return ltype

    key = str(ltype).strip().upper()

    # 直接匹配新规范
    if key in {"HONGBAO_SEND"}:
        return LedgerType.HONGBAO_SEND
    if key in {"HONGBAO_GRAB"}:
        return LedgerType.HONGBAO_GRAB
    if key in {"RESET"}:
        return LedgerType.RESET

    # 历史写法映射到新规范
    if key in {"SEND"}:
        return LedgerType.HONGBAO_SEND
    if key in {"GRAB", "ENVELOPE_GRAB"}:
        return LedgerType.HONGBAO_GRAB

    # 其它可直通的成员
    try:
        # 既支持用“成员名”又支持用“成员值”
        for m in LedgerType:
            if m.name == key or m.value == key:
                return m
    except Exception:
        pass

    return LedgerType.OTHER


def add_ledger_entry(session: Session,
                     *,
                     user_tg_id: int,
                     ltype: Union[LedgerType, str],
                     token: str,
                     amount: Union[Decimal, float, int, str],
                     ref_type: Optional[str] = None,
                     ref_id: Optional[str] = None,
                     note: Optional[str] = None) -> Ledger:
    """
    新增一条流水（不负责更新用户余额；请在同一事务中先更新余额再记账，或两者同事务提交）

    参数：
      - ltype ：可传 LedgerType 或等价字符串
                兼容 "SEND"/"GRAB"/"HONGBAO_SEND"/"HONGBAO_GRAB"/"ENVELOPE_GRAB"/"RESET" 等
      - amount：正数=收入，负数=支出（内部会量化为 6 位小数）
      - token ：会被统一为大写
    返回：
      - 新增的 Ledger ORM 实体（未自动提交）
    """
    entry = Ledger(
        user_tg_id=int(user_tg_id),
        type=_normalize_ledger_type(ltype),
        token=str(token).upper(),
        amount=_q(amount),
        ref_type=ref_type,
        ref_id=str(ref_id) if ref_id is not None else None,
        note=note or "",
    )
    session.add(entry)
    session.flush()
    return entry


def list_recent_ledgers(user_tg_id: int, limit: int = 10) -> List[Dict]:
    """
    读取用户最近 N 条流水（按时间倒序），用于“📜 我的记录”
    """
    with get_session() as s:
        q = (
            s.query(Ledger)
            .filter(Ledger.user_tg_id == int(user_tg_id))
            .order_by(Ledger.created_at.desc())
            .limit(int(limit))
        )
        rows: List[Ledger] = q.all()

        out: List[Dict] = []
        for r in rows:
            out.append({
                "id": int(r.id),
                "type": r.type.value,
                "token": r.token,
                "amount": float(r.amount or 0),
                "ref_type": r.ref_type,
                "ref_id": r.ref_id,
                "note": r.note or "",
                "created_at": r.created_at.isoformat(),
            })
        return out
