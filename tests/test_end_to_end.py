# tests/test_end_to_end.py
# -*- coding: utf-8 -*-
"""
端到端红包流程测试：
模拟 /start → 发红包 → 用户依次抢 → 最后一份触发排行榜。
不依赖真实 Telegram API，仅测试模型与业务逻辑。
"""

from decimal import Decimal
from models.db import init_db, get_session
from models.user import get_or_create_user
from models.envelope import create_envelope, grab_share, list_envelope_claims, get_lucky_winner, HBFinished


def setup_module():
    init_db()


def test_full_redpacket_round():
    chat_id = -100123456
    sender_id = 60001
    user_ids = [60002, 60003, 60004]

    # 注册用户
    with get_session() as s:
        get_or_create_user(s, tg_id=sender_id, username="sender", lang="zh")
        for uid in user_ids:
            get_or_create_user(s, tg_id=uid, username=f"user{uid}", lang="zh")

    # 发红包（3 份，总额 3.00）
    with get_session() as s:
        env = create_envelope(
            s,
            chat_id=chat_id,
            sender_tg_id=sender_id,
            mode="POINT",
            total_amount=Decimal("3"),
            shares=3,
            note="E2E test",
            activate=True,
        )
        eid = env.id
        assert eid > 0

    # 三个用户依次抢
    last_flag = False
    for uid in user_ids:
        claim = grab_share(eid, uid)
        assert claim["token"] == "POINT"
        if claim["is_last"]:
            last_flag = True

    # 最后一份后应触发排行榜
    assert last_flag is True
    claims = list_envelope_claims(eid)
    assert len(claims) == 3

    lucky = get_lucky_winner(eid)
    assert lucky is not None
    uid, amount = lucky
    assert isinstance(uid, int)
    assert amount > 0

    # 已抢完，再次抢应报 HBFinished
    try:
        grab_share(eid, sender_id)
        assert False, "Expected HBFinished"
    except HBFinished:
        pass
