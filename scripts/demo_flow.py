# scripts/demo_flow.py
# -*- coding: utf-8 -*-
"""
演示红包完整流程：
1. 初始化数据库并创建用户
2. 发红包 (3份)
3. 依次抢红包
4. 最后一份后打印排行榜
"""

from decimal import Decimal
from models.db import init_db, get_session
from models.user import get_or_create_user
from models.envelope import create_envelope, grab_share, list_envelope_claims, get_lucky_winner


def main():
    init_db()

    chat_id = -1008888
    sender_id = 91001
    user_ids = [91002, 91003, 91004]

    # 1. 创建用户
    with get_session() as s:
        get_or_create_user(s, tg_id=sender_id, username="sender", lang="zh")
        for uid in user_ids:
            get_or_create_user(s, tg_id=uid, username=f"user{uid}", lang="zh")

    # 2. 发红包（总额 3，3 份）
    with get_session() as s:
        env = create_envelope(
            s,
            chat_id=chat_id,
            sender_tg_id=sender_id,
            mode="POINT",
            total_amount=Decimal("3"),
            shares=3,
            note="demo",
            activate=True,
        )
        eid = env.id
        print(f"🧧 RedPacket created id={eid}, total=3, shares=3")

    # 3. 用户依次抢
    for uid in user_ids:
        amount, token, last = grab_share(eid, uid)
        print(f"👤 user{uid} grabbed {amount} {token} (last={last})")

    # 4. 最后一份后 → 打印排行榜
    claims = list_envelope_claims(eid)
    print("\n📊 Ranking:")
    for c in claims:
        print(f" - user{c.user_tg_id}: {c.amount}")

    lucky = get_lucky_winner(eid)
    if lucky:
        print(f"\n🍀 Lucky winner: user{lucky[0]} with {lucky[1]} POINT")


if __name__ == "__main__":
    main()
