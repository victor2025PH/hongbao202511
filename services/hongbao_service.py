# services/hongbao_service.py
# -*- coding: utf-8 -*-
"""
红包业务服务层：
- create_envelope(user_id, token, total_amount, total_count)
- grab_envelope(user_id, envelope_id) -> (成功?, 文案, 剩余数量)
- close_envelope_if_finished(envelope_id) -> 排行榜文案
"""

from __future__ import annotations
from decimal import Decimal
from typing import Tuple, Optional, List

from sqlalchemy.orm import Session

from models.db import get_session
from models.user import User, update_balance
from models.ledger import add_ledger_entry, LedgerType
from models.envelope import Envelope, GrabRecord, EnvelopeStatus
from core.i18n.i18n import t


# ========== 发红包 ==========

def create_envelope(user_id: int, token: str, total_amount: Decimal, total_count: int,
                    lang: str = "zh") -> Tuple[bool, str, Optional[int]]:
    """
    发红包：扣款并生成 Envelope
    返回 (成功?, 文案, envelope_id)
    """
    with get_session() as s:
        # 检查余额
        u = s.query(User).filter_by(tg_id=user_id).first()
        if not u:
            return False, t("hongbao.no_user", lang), None

        bal = u.get_balance(token)
        if bal < total_amount:
            return False, t("hongbao.not_enough", lang, token=token), None

        # 扣款
        update_balance(s, u, token, -total_amount)
        add_ledger_entry(
            s, user_tg_id=user_id, ltype=LedgerType.SEND,
            token=token, amount=-total_amount, ref_type="ENVELOPE", ref_id="NEW",
            note=f"发红包 {total_amount} {token}"
        )

        # 新建红包
        env = Envelope(
            sender_tg_id=user_id,
            token=token,
            total_amount=Decimal(total_amount),
            total_count=total_count,
            remain_amount=Decimal(total_amount),
            remain_count=total_count,
            status=EnvelopeStatus.OPEN,
        )
        s.add(env)
        s.flush()
        return True, t("hongbao.created", lang, amount=total_amount, count=total_count, token=token), env.id


# ========== 抢红包 ==========

def grab_envelope(user_id: int, envelope_id: int, lang: str = "zh") -> Tuple[bool, str, int]:
    """
    抢红包逻辑：
    - 检查是否已抢过
    - 随机金额分配（或均分）
    - 更新 Envelope / GrabRecord
    - 返回 (成功?, 文案, 剩余数量)
    """
    import random
    with get_session() as s:
        env = s.query(Envelope).get(envelope_id)
        if not env or env.status != EnvelopeStatus.OPEN:
            return False, t("hongbao.finished", lang), 0

        # 检查是否抢过
        grabbed = s.query(GrabRecord).filter_by(envelope_id=env.id, user_tg_id=user_id).first()
        if grabbed:
            return False, t("hongbao.already", lang), env.remain_count

        # 分配金额
        if env.remain_count == 1:
            amt = env.remain_amount
        else:
            max_amt = float(env.remain_amount) / env.remain_count * 2
            amt = Decimal(str(round(random.uniform(0.01, max_amt), 2)))
            if amt > env.remain_amount:
                amt = env.remain_amount

        # 更新红包
        env.remain_amount -= amt
        env.remain_count -= 1
        if env.remain_count <= 0 or env.remain_amount <= 0:
            env.status = EnvelopeStatus.CLOSED

        # 记录领取
        rec = GrabRecord(envelope_id=env.id, user_tg_id=user_id, amount=amt)
        s.add(rec)

        # 更新余额
        u = s.query(User).filter_by(tg_id=user_id).first() or User(tg_id=user_id)
        s.add(u)
        update_balance(s, u, env.token, amt)
        add_ledger_entry(
            s, user_tg_id=user_id, ltype=LedgerType.GRAB,
            token=env.token, amount=amt, ref_type="ENVELOPE", ref_id=str(env.id),
            note="抢红包"
        )

        return True, t("hongbao.grabbed", lang, amount=amt, token=env.token), env.remain_count


# ========== 完结 & 排行榜 ==========

def close_envelope_if_finished(envelope_id: int, lang: str = "zh") -> Optional[str]:
    """
    如果红包已领完 → 返回排行榜文案
    """
    with get_session() as s:
        env = s.query(Envelope).get(envelope_id)
        if not env or env.status != EnvelopeStatus.CLOSED:
            return None

        # 抓取所有记录
        recs: List[GrabRecord] = (
            s.query(GrabRecord).filter_by(envelope_id=envelope_id).order_by(GrabRecord.amount.desc()).all()
        )
        if not recs:
            return None

        lines = [t("rank.title", lang)]
        for r in recs:
            user_line = t("rank.item", lang, user=r.user_tg_id, amount=float(r.amount), token=env.token)
            lines.append(user_line)

        lucky = recs[0]
        lines.append("")
        lines.append(t("rank.lucky", lang, user=lucky.user_tg_id, amount=float(lucky.amount), token=env.token))

        return "\n".join(lines)
