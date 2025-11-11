# tests/test_services.py
# -*- coding: utf-8 -*-
"""
业务服务层测试：
- 邀请进度（拼多多模式）
- 充值服务（mock）
"""

from __future__ import annotations

import os
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

# 选用 sqlite 脚本级数据库，避免污染现有 PostgreSQL 数据
_TEST_DB = Path("test_services.sqlite")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
# 强制使用 mock provider，避免网络请求
os.environ.setdefault("RECHARGE_PROVIDER", "mock")

# SQLite 默认不支持 Decimal 绑定，手动注册
sqlite3.register_adapter(Decimal, lambda d: str(d))

from models.db import engine, get_session, init_db  # noqa: E402
from models.invite import get_progress as model_get_progress  # noqa: E402
from models.user import User, update_balance  # noqa: E402
from services import invite_service, recharge_service  # noqa: E402


@pytest.fixture(autouse=True)
def _setup_database():
    init_db()
    tables = (
        "invite_relations",
        "invite_progress",
        "ledger",
        "users",
    )
    with engine.begin() as conn:
        for table in tables:
            try:
                conn.execute(text(f"DELETE FROM {table}"))
            except Exception:
                pass
    yield
    with engine.begin() as conn:
        for table in tables:
            try:
                conn.execute(text(f"DELETE FROM {table}"))
            except Exception:
                pass
    engine.dispose()


def test_invite_progress_flow():
    inviter_id = 40001
    invitee_ids = [40002, 40003, 40004, 40005]

    # 建立邀请人账户
    with get_session() as session:
        inviter = User(tg_id=inviter_id, point_balance=0)
        session.merge(inviter)
        session.commit()

    # 初始进度应为 0%
    msg, percent = invite_service.get_invite_progress_text(inviter_id, lang="zh")
    assert isinstance(percent, int)
    assert 0 <= percent <= 100
    assert msg

    # 连续邀请 4 人
    for uid in invitee_ids:
        added = invite_service.add_invite_and_rewards(inviter_id, uid, give_extra_points=False)
        assert added is True

    progress = model_get_progress(inviter_id)
    assert progress["invited_count"] == len(invitee_ids)
    assert progress["progress_percent"] >= len(invitee_ids)

    # 补充足够积分后尝试兑换进度
    with get_session() as session:
        inviter = session.query(User).filter_by(tg_id=inviter_id).one()
        update_balance(session, inviter, "POINT", 6000)  # 默认 1000 分/进度，充足即可
        session.commit()

    ok, _, after = invite_service.redeem_points_to_progress(inviter_id, lang="zh")
    assert ok is True
    assert after > progress["progress_percent"]


def test_recharge_service_mock():
    user_id = 50001
    amount = Decimal("20.50")

    order = recharge_service.new_order(user_id=user_id, token="USDT", amount=amount, provider="mock")
    assert order.id > 0
    assert Decimal(str(order.amount)) == amount
    assert order.payment_url  # mock provider 会给出支付链接

    assert recharge_service.mark_order_success(order.id, tx_hash="0xmocktx") is True

    stored = recharge_service.get_order(order.id)
    assert stored is not None
    assert stored.status.name == "SUCCESS"
