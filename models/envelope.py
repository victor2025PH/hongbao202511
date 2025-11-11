# models/envelope.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import json  # ✅ 用于 cover_meta 的序列化
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from time import perf_counter
from typing import Optional, List, Tuple, Dict, Any

from sqlalchemy import (
    Column, Integer, BigInteger, String, DateTime, Boolean, Text,
    Enum as SAEnum, func, UniqueConstraint, Index, update  # ✅ 引入 update 以做原子占位
)
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError  # ✅ 修正缩进错误

from .db import Base, get_session, DECIMAL   # 使用自定义 DECIMAL，保障 SQLite 精度与无告警
# === 余额与流水 ===
from monitoring.metrics import counter as metrics_counter, histogram as metrics_histogram
from .user import get_or_create_user, update_balance
from .ledger import add_ledger_entry, LedgerType


# ================= 业务异常 =================
class HBError(Exception):
    """通用业务异常"""
    pass


class HBNotFound(HBError):
    """红包不存在"""
    pass


class HBDuplicatedGrab(HBError):
    """重复领取同一红包"""
    pass


class HBFinished(HBError):
    """红包已领完/已关闭"""
    pass


# ================= 枚举 =================
class HBMode(str, Enum):
    USDT = "USDT"
    TON = "TON"
    POINT = "POINT"


# ================== LedgerType 兼容适配 ==================
def _pick_ledger_type(*names: str) -> LedgerType:
    """
    传入一组候选名字，返回 LedgerType 中第一个存在的枚举项。
    若都不存在，则退回到枚举的第一个成员（尽量不抛错，以免影响业务）。
    """
    for n in names:
        if hasattr(LedgerType, n):
            return getattr(LedgerType, n)
    # 兜底：取第一个成员
    try:
        return list(LedgerType)[0]
    except Exception:
        # 极端情况：LedgerType 不可迭代或异常，最后再试一个常见名
        return getattr(LedgerType, "RECHARGE") if hasattr(LedgerType, "RECHARGE") else getattr(LedgerType, "ADJUSTMENT")


# 抢红包流水类型候选（按优先级从高到低）
_LEDGER_TYPE_GRAB = _pick_ledger_type(
    "HONGBAO_GRAB", "HONGBAO_RECEIVE", "GRAB", "RECEIVE", "HONGBAO_GET", "RED_PACKET_GRAB"
)
# 发红包流水类型候选（如你后续在这里也要记“发出”流水可复用）
_LEDGER_TYPE_SEND = _pick_ledger_type(
    "HONGBAO_SEND", "SEND", "CREATE", "HONGBAO_CREATE", "RED_PACKET_SEND"
)


# ================= ORM 模型 =================

log = logging.getLogger("hongbao.envelope")

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
class Envelope(Base):
    """
    红包主表
    """
    __tablename__ = "envelopes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, index=True, nullable=False)          # 目标群/私聊 chat_id
    sender_tg_id = Column(BigInteger, index=True, nullable=False)     # 发包人
    mode = Column(SAEnum(HBMode), nullable=False, default=HBMode.USDT)
    total_amount = Column(DECIMAL(6), nullable=False)                 # 总金额（Decimal，保留 6 位）
    shares = Column(Integer, nullable=False)                          # 总份数
    note = Column(Text, nullable=True)                                # 祝福语
    # —— 封面相关字段（保持与你现有工程一致）——
    cover_channel_id = Column(BigInteger, nullable=True)   # 素材频道 ID
    cover_message_id = Column(BigInteger, nullable=True)   # 素材频道消息 ID
    cover_file_id    = Column(String(256), nullable=True)  # 文件 ID 兜底
    cover_meta       = Column(Text, nullable=True)         # JSON/slug 等扩展信息

    status = Column(String(20), nullable=False, default="active")     # active/closed/cancelled
    is_finished = Column(Boolean, nullable=False, default=False)
    # ✅ 新增：本轮 MVP 私聊是否已发送（配合 db.init_db() 的轻量迁移）
    mvp_dm_sent = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    activated_at = Column(DateTime, nullable=True)

    def mark_finished(self):
        self.is_finished = True
        self.status = "closed"


class EnvelopeShare(Base):
    """
    抢到的份额记录
    """
    __tablename__ = "envelope_shares"

    id = Column(Integer, primary_key=True, autoincrement=True)
    envelope_id = Column(Integer, index=True, nullable=False)
    user_tg_id = Column(BigInteger, index=True, nullable=False)
    amount = Column(DECIMAL(6), nullable=False, default=Decimal("0"))  # 单份金额（USDT/TON：保留 6 位；POINT：整数）
    grabbed_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        # 防止并发下同一用户重复领取同一红包
        UniqueConstraint("envelope_id", "user_tg_id", name="uq_env_user"),
        Index("idx_env_time", "envelope_id", "grabbed_at"),
    )


# ================= 工具/数据类 =================
@dataclass
class EnvelopeSummary:
    id: int
    mode: HBMode
    total_amount: Decimal
    shares: int
    grabbed_shares: int
    # 这里不强制包含封面字段；发送时通常直接读取主记录或调用 get_envelope_cover()


def _to_decimal(x) -> Decimal:
    return Decimal(str(x))


def _q6(x: Decimal) -> Decimal:
    """保留 6 位小数（用于 USDT/TON），向下取整避免超额累进误差"""
    return Decimal(str(x)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


# 随机分配辅助
def _min_unit(token: str) -> Decimal:
    """USDT/TON 的最小单位为 0.000001；POINT 为 1"""
    return Decimal("0.000001") if token.upper() in ("USDT", "TON") else Decimal("1")


def _to_json_str(meta: Any) -> Optional[str]:
    """把 dict/list 等序列化为 JSON 字符串；已是字符串则原样返回；None 返回 None。"""
    if meta is None:
        return None
    if isinstance(meta, str):
        return meta
    try:
        return json.dumps(meta, ensure_ascii=False)
    except Exception:
        return None


# ================= 基础 CRUD / 查询 =================
def create_envelope(
    db: Session,
    *,
    chat_id: int,
    sender_tg_id: int,
    mode: str | HBMode,
    total_amount: Decimal | float | int | str,
    shares: int,
    note: str = "",
    activate: bool = True,
    # ---------- 封面相关入参（可选） ----------
    cover_channel_id: Optional[int] = None,
    cover_message_id: Optional[int] = None,
    cover_file_id: Optional[str] = None,
    cover_meta: Optional[Any] = None,
) -> Envelope:
    """
    创建红包主记录。

    支持的封面参数：
    - cover_channel_id / cover_message_id：若具备，发送时优先使用 bot.copyMessage
    - cover_file_id：作为 sendPhoto 的兜底
    - cover_meta：任意 JSON 可序列化对象（例如 tags/尺寸等），保存为 JSON 字符串
    """
    if isinstance(mode, str):
        mode = HBMode(mode.upper())
    amt = _to_decimal(total_amount)
    if amt <= 0:
        raise HBError("invalid total_amount")
    if shares <= 0:
        raise HBError("shares must be positive")

    env = Envelope(
        chat_id=int(chat_id),
        sender_tg_id=int(sender_tg_id),
        mode=mode,
        total_amount=_q6(amt),
        shares=int(shares),
        note=note or "",
        status="active" if activate else "pending",
        activated_at=datetime.utcnow() if activate else None,
        # ✅ 封面字段透传
        cover_channel_id=int(cover_channel_id) if cover_channel_id is not None else None,
        cover_message_id=int(cover_message_id) if cover_message_id is not None else None,
        cover_file_id=cover_file_id or None,
        cover_meta=_to_json_str(cover_meta),
    )
    db.add(env)
    db.flush()  # 立刻获得 id
    return env


def _get_env(db: Session, envelope_id: int) -> Envelope:
    env = db.query(Envelope).filter(Envelope.id == int(envelope_id)).first()
    if not env:
        raise HBNotFound(f"envelope {envelope_id} not found")
    return env


def _lock_env(db: Session, envelope_id: int) -> Envelope:
    """
    尝试对红包主记录加行级锁（FOR UPDATE）。
    - 在支持的数据库（PostgreSQL 等）可防止并发超领；
    - SQLite 会忽略该锁，不报错（因此仍需唯一约束兜底）。
    """
    q = db.query(Envelope).filter(Envelope.id == int(envelope_id))
    try:
        env = q.with_for_update(nowait=False, of=Envelope).first()
    except Exception:
        env = q.first()
    if not env:
        raise HBNotFound(f"envelope {envelope_id} not found")
    return env


def get_envelope_summary(envelope_id: int) -> Dict[str, Any]:
    """
    返回：{ id, mode(str), total_amount(Decimal), shares(int), grabbed_shares(int) }
    说明：封面信息通常发送前直接查主记录；此处维持原接口，避免影响旧逻辑。
    """
    with get_session() as s:
        env = _get_env(s, envelope_id)
        grabbed = s.query(func.count(EnvelopeShare.id)).filter(
            EnvelopeShare.envelope_id == env.id
        ).scalar() or 0
        return {
            "id": int(env.id),
            "mode": env.mode.value,
            "total_amount": _to_decimal(env.total_amount),
            "shares": int(env.shares),
            "grabbed_shares": int(grabbed),
        }


def get_envelope_cover(envelope_id: int) -> Dict[str, Any]:
    """
    读取红包的封面信息（为发送阶段/确认页提供数据）
    返回：
      {
        "cover_channel_id": int|None,
        "cover_message_id": int|None,
        "cover_file_id": str|None,
        "cover_meta": str|None  # JSON 字符串
      }
    """
    with get_session() as s:
        env = _get_env(s, envelope_id)
        return {
            "cover_channel_id": env.cover_channel_id,
            "cover_message_id": env.cover_message_id,
            "cover_file_id": env.cover_file_id,
            "cover_meta": env.cover_meta,
        }


def list_envelope_claims(envelope_id: int) -> List[Dict[str, Any]]:
    """
    返回该红包的所有领取记录（按金额从大到小，再按时间从早到晚），字典列表：
    [{ "user_tg_id": int, "amount": Decimal, "grabbed_at": datetime }, ...]
    之所以返回 dict，是为了兼容 routers/hongbao.py 的 c.get(...) 访问方式。
    """
    with get_session() as s:
        _ = _get_env(s, envelope_id)  # 不存在会抛 HBNotFound
        rows: List[Tuple[int, Decimal, datetime]] = (
            s.query(EnvelopeShare.user_tg_id, EnvelopeShare.amount, EnvelopeShare.grabbed_at)
            .filter(EnvelopeShare.envelope_id == int(envelope_id))
            .order_by(EnvelopeShare.amount.desc(), EnvelopeShare.grabbed_at.asc())
            .all()
        )
        return [{"user_tg_id": int(uid), "amount": _to_decimal(amt), "grabbed_at": ts} for uid, amt, ts in rows]


def get_lucky_winner(envelope_id: int) -> Optional[Tuple[int, Decimal]]:
    """
    金额最大者为“运气王”；同额取最早抢到者。
    返回 (user_tg_id, amount) 或 None
    """
    with get_session() as s:
        _ = _get_env(s, envelope_id)
        rows = (
            s.query(
                EnvelopeShare.user_tg_id,
                EnvelopeShare.amount,
                EnvelopeShare.grabbed_at
            )
            .filter(EnvelopeShare.envelope_id == int(envelope_id))
            .all()
        )
        if not rows:
            return None
        # Python 层严格选择：先比金额，再比时间
        best = max(rows, key=lambda r: (_to_decimal(r[1]), -r[2].timestamp()))
        return int(best[0]), _to_decimal(best[1])


# ================= 抢红包核心（本版：仅拼手气/随机） =================
def _sum_claimed_amount(db: Session, envelope_id: int) -> Decimal:
    total = db.query(func.coalesce(func.sum(EnvelopeShare.amount), 0)).filter(
        EnvelopeShare.envelope_id == int(envelope_id)
    ).scalar()
    return _to_decimal(total or 0)


def _claimed_count(db: Session, envelope_id: int) -> int:
    return int(
        db.query(func.count(EnvelopeShare.id)).filter(
            EnvelopeShare.envelope_id == int(envelope_id)
        ).scalar() or 0
    )


def _is_user_claimed(db: Session, envelope_id: int, user_tg_id: int) -> bool:
    return bool(
        db.query(EnvelopeShare.id).filter(
            EnvelopeShare.envelope_id == int(envelope_id),
            EnvelopeShare.user_tg_id == int(user_tg_id)
        ).first()
    )


def _rand_decimal(low: Decimal, high: Decimal, quant: Decimal) -> Decimal:
    """
    生成 [low, high] 间的随机 Decimal，最后量化到 quant（如 0.000001）。
    """
    import random
    if high <= low:
        return low
    # 使用随机数生成，再量化避免精度漂移
    r = Decimal(str(random.random()))
    val = low + (high - low) * r
    return val.quantize(quant, rounding=ROUND_DOWN)


def grab_share(envelope_id: int, user_tg_id: int) -> Dict[str, Any]:
    """
    抢红包（仅随机/拼手气）：
    - 尝试对 Envelope 加 FOR UPDATE 锁（支持的库会生效）
    - 检测红包状态、重复领取
    - 非最后一份：使用“二倍均值法”随机金额
        • USDT/TON：最小单位 0.000001，上界 = min(2*均值, 安全上限)
        • POINT：最小单位 1，上界 = min(2*均值, 安全上限) 并取整
      其中“安全上限”确保剩余份数每份至少留出一个最小单位；
    - 最后一份：领取全部剩余并标记完成
    - 成功后：**在同一事务**内写领取记录 + 给抢到者加余额(update_balance) + 记一条“抢到”流水
    返回 dict：{ "amount": Decimal, "token": str, "is_last": bool }
    可能抛出：HBNotFound / HBDuplicatedGrab / HBFinished / HBError
    """
    op = "grab"
    start = perf_counter()
    try:
        with get_session() as s:
            env = _lock_env(s, envelope_id)

            if env.is_finished or env.status != "active":
                raise HBFinished("envelope is finished")

            if _is_user_claimed(s, envelope_id, user_tg_id):
                raise HBDuplicatedGrab("duplicated")

            claimed = _claimed_count(s, envelope_id)
            if claimed >= env.shares:
                # 保险：标记完成
                env.mark_finished()
                s.add(env)
                s.commit()
                raise HBFinished("envelope is finished")

            # 计算剩余份数与金额
            remaining_shares = int(env.shares) - claimed
            remaining_amount = _to_decimal(env.total_amount) - _sum_claimed_amount(s, envelope_id)
            if remaining_shares <= 0 or remaining_amount <= 0:
                env.mark_finished()
                s.add(env)
                s.commit()
                raise HBFinished("envelope is finished")

            token = env.mode.value.upper()
            is_last = (remaining_shares == 1)

            # ---------- 随机分配 ----------
            if is_last:
                amount = _q6(remaining_amount) if token in ("USDT", "TON") else Decimal(int(remaining_amount))
            else:
                unit = _min_unit(token)
                mean = remaining_amount / Decimal(remaining_shares)
                safe_cap = remaining_amount - unit * Decimal(remaining_shares - 1)
                two_mean = mean * Decimal("2")
                upper = two_mean if two_mean <= safe_cap else safe_cap
                if upper < unit:
                    upper = unit

                if token in ("USDT", "TON"):
                    amount = _rand_decimal(unit, upper, Decimal("0.000001"))
                    if amount < unit:
                        amount = unit
                    amount = _q6(amount)
                else:
                    low_i = int(unit)
                    up_i = int(upper)
                    if up_i < low_i:
                        up_i = low_i
                    import random
                    amount = Decimal(random.randint(low_i, up_i))

            share = EnvelopeShare(
                envelope_id=int(env.id),
                user_tg_id=int(user_tg_id),
                amount=_q6(amount) if token in ("USDT", "TON") else Decimal(int(amount)),
            )
            s.add(share)

            if is_last:
                env.mark_finished()
                s.add(env)

            _ = get_or_create_user(s, tg_id=int(user_tg_id))
            credit = _q6(amount) if token in ("USDT", "TON") else Decimal(int(amount))

            try:
                update_balance(
                    s,
                    user=int(user_tg_id),
                    token=token,
                    amount=credit,
                    write_ledger=False,
                )
            except TypeError:
                u = get_or_create_user(s, tg_id=int(user_tg_id))
                update_balance(
                    s,
                    u,
                    token=token,
                    delta=credit,
                )

            add_ledger_entry(
                s,
                user_tg_id=int(user_tg_id),
                ltype=_LEDGER_TYPE_GRAB,
                token=token,
                amount=credit,
                ref_type="ENVELOPE",
                ref_id=str(env.id),
                note=f"Grab envelope #{env.id}",
            )

            try:
                s.commit()
            except IntegrityError as e:
                s.rollback()
                raise HBDuplicatedGrab("duplicated") from e

        payload = {
            "amount": _to_decimal(share.amount),
            "token": token,
            "is_last": bool(is_last),
        }

        duration = perf_counter() - start
        _HONGBAO_COUNTER.inc(operation=op, status="success")
        _HONGBAO_LATENCY.observe(duration, operation=op, status="success")
        log.info(
            "hongbao.grab.success envelope=%s user=%s amount=%s token=%s last=%s",
            envelope_id,
            user_tg_id,
            payload["amount"],
            payload["token"],
            payload["is_last"],
        )
        return payload
    except HBDuplicatedGrab:
        duration = perf_counter() - start
        _HONGBAO_COUNTER.inc(operation=op, status="duplicate")
        _HONGBAO_LATENCY.observe(duration, operation=op, status="duplicate")
        log.warning("hongbao.grab.duplicate envelope=%s user=%s", envelope_id, user_tg_id)
        raise
    except HBFinished:
        duration = perf_counter() - start
        _HONGBAO_COUNTER.inc(operation=op, status="finished")
        _HONGBAO_LATENCY.observe(duration, operation=op, status="finished")
        log.warning("hongbao.grab.finished envelope=%s user=%s", envelope_id, user_tg_id)
        raise
    except HBError as exc:
        duration = perf_counter() - start
        status = exc.args[0] if exc.args else "hb_error"
        status = status.replace(" ", "_").lower()
        _HONGBAO_COUNTER.inc(operation=op, status=status)
        _HONGBAO_LATENCY.observe(duration, operation=op, status=status)
        log.warning(
            "hongbao.grab.failed envelope=%s user=%s status=%s",
            envelope_id,
            user_tg_id,
            status,
        )
        raise
    except Exception:
        duration = perf_counter() - start
        _HONGBAO_COUNTER.inc(operation=op, status="unexpected")
        _HONGBAO_LATENCY.observe(duration, operation=op, status="unexpected")
        log.exception("hongbao.grab.unexpected envelope=%s user=%s", envelope_id, user_tg_id)
        raise


# ================= 其它实用函数（可选） =================
def add_envelope_claim(
    db: Session,
    *,
    envelope_id: int,
    user_tg_id: int,
    amount: Decimal | float | int | str,
) -> EnvelopeShare:
    """
    手动写入一条领取记录（通常不直接使用，grab_share 已封装）
    """
    env = _get_env(db, envelope_id)
    amt = _to_decimal(amount)
    share = EnvelopeShare(
        envelope_id=int(env.id),
        user_tg_id=int(user_tg_id),
        amount=_q6(amt) if env.mode.value.upper() in ("USDT", "TON") else Decimal(int(amt)),
    )
    db.add(share)
    grabbed = _claimed_count(db, envelope_id) + 1
    if grabbed >= env.shares:
        env.mark_finished()
        db.add(env)
    db.commit()
    db.refresh(share)
    return share


def count_grabbed(db: Session, envelope_id: int) -> int:
    return _claimed_count(db, envelope_id)


def close_if_finished(db: Session, envelope_id: int) -> Envelope:
    env = _get_env(db, envelope_id)
    grabbed = _claimed_count(db, envelope_id)
    if grabbed >= env.shares:
        env.mark_finished()
        db.add(env)
        db.commit()
    return env


# ================= MVP 私聊幂等：占位与读取（供 routers/hongbao.py 调用） =================
def has_mvp_dm_sent(envelope_id: int) -> bool:
    """
    读取 envelopes.mvp_dm_sent（True/False）
    """
    with get_session() as s:
        env = _get_env(s, envelope_id)
        return bool(getattr(env, "mvp_dm_sent", False))


def claim_mvp_dm_send_token(envelope_id: int) -> bool:
    """
    原子占位：尝试把 envelopes.mvp_dm_sent 从 False -> True
    - 成功（受影响行数为 1）说明拿到了“本次发送资格”，应当执行真实发送；
    - 失败（0 行）说明已被其他进程/实例占位或之前发过，应跳过发送。
    说明：
    - 使用 UPDATE ... WHERE mvp_dm_sent = 0 做幂等占位，兼容 SQLite / MySQL / Postgres；
    - 这里不回退占位（即使后续发送失败也不回滚），产品策略是“最多发送一次”。
    """
    with get_session() as s:
        result = s.execute(
            update(Envelope)
            .where(
                Envelope.id == int(envelope_id),
                Envelope.mvp_dm_sent.is_(False),
            )
            .values(mvp_dm_sent=True)
        )
        # get_session() 退出会兜底 commit；此处可不手动提交
        return (result.rowcount or 0) > 0
