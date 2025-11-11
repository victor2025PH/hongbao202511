# tests/test_models.py
# -*- coding: utf-8 -*-
"""
核心模型与数据流测试：
- 初始化数据库
- 用户创建/更新
- 发红包 & 抢红包（当次排行榜）
- 充值订单入账
- 流水记录校验
"""
from decimal import Decimal

from models.db import init_db, get_session
from models.user import User, get_or_create_user
from models.envelope import (
    create_envelope,
    grab_share,
    list_envelope_claims,
    get_envelope_summary,
    get_lucky_winner,
    HBFinished,
    HBDuplicatedGrab,
)
from models.ledger import Ledger, LedgerType
from models.recharge import create_order, mark_success, get_order


def setup_module():
    # 初始化数据库（如不存在则创建）
    init_db()


def test_user_create_and_update():
    with get_session() as s:
        u = get_or_create_user(s, tg_id=10001, username="alice", lang="zh")
        assert u.tg_id == 10001
        # 更新用户名/语言
        u2 = get_or_create_user(s, tg_id=10001, username="alice_updated", lang="en")
        assert u2.username == "alice_updated"
        assert u2.language == "en"


def test_envelope_grab_and_ranking_flow():
    # 创建两个用户
    with get_session() as s:
        get_or_create_user(s, tg_id=20001, username="sender", lang="zh")
        get_or_create_user(s, tg_id=20002, username="bob", lang="zh")
        get_or_create_user(s, tg_id=20003, username="carol", lang="zh")

    # 创建红包（3 份，总额 3.000000 POINT）
    with get_session() as s:
        env = create_envelope(
            s,
            chat_id=7777,
            sender_tg_id=20001,
            mode="POINT",
            total_amount=Decimal("3"),
            shares=3,
            note="test",
            activate=True,
        )
        eid = env.id
        assert eid > 0

    # 用户 2 抢
    first_claim = grab_share(eid, 20002)
    assert first_claim["token"] == "POINT"
    assert first_claim["is_last"] is False
    amount1 = first_claim["amount"]

    # 同一用户重复抢 → 应报重复
    try:
        grab_share(eid, 20002)
        assert False, "should raise HBDuplicatedGrab"
    except HBDuplicatedGrab:
        pass

    # 用户 3 抢
    second_claim = grab_share(eid, 20003)
    assert second_claim["token"] == "POINT"
    assert second_claim["is_last"] is False
    amount2 = second_claim["amount"]

    # 最后一份：发包人也可以抢（仅用于测试）
    third_claim = grab_share(eid, 20001)
    assert third_claim["token"] == "POINT"
    assert third_claim["is_last"] is True  # 已是最后一份
    amount3 = third_claim["amount"]

    # 再抢应报已结束
    try:
        grab_share(eid, 20001)
        assert False, "should raise HBFinished"
    except HBFinished:
        pass

    # 汇总检查
    summary = get_envelope_summary(eid)
    assert summary["grabbed_shares"] == 3
    total_claimed = amount1 + amount2 + amount3
    assert abs(summary["total_amount"] - total_claimed) < Decimal("0.000001")

    # 排行榜（按金额降序，金额最大且最早的为运气王）
    claims = list_envelope_claims(eid)
    assert len(claims) == 3
    lucky = get_lucky_winner(eid)
    assert lucky is not None
    assert isinstance(lucky[0], int)
    assert isinstance(lucky[1], Decimal)


def test_recharge_success_and_ledger():
    # 创建用户
    with get_session() as s:
        get_or_create_user(s, tg_id=30001, username="dave", lang="en")

    # 创建订单（USDT 12.34）
    order = create_order(user_id=30001, amount=Decimal("12.34"), token="USDT")
    assert order.id > 0

    # 标记成功
    ok = mark_success(order.id, tx_hash="0xabc123")
    assert ok is True

    # 查单应为 SUCCESS
    o2 = get_order(order.id)
    assert o2 is not None and o2.status.name == "SUCCESS"

    # 流水存在且为 RECHARGE
    with get_session() as s:
        ledgers = (
            s.query(Ledger)
            .filter(Ledger.user_tg_id == 30001)
            .order_by(Ledger.created_at.desc())
            .all()
        )
        assert any(l.type == LedgerType.RECHARGE for l in ledgers)
