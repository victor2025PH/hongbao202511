# -*- coding: utf-8 -*-
from __future__ import annotations

from decimal import Decimal
import uuid
import os
from pathlib import Path
import sqlite3

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

os.environ["DATABASE_URL"] = "sqlite:///./test_regression.sqlite"
os.environ.pop("FLAG_ENABLE_PUBLIC_GROUPS", None)
Path("test_regression.sqlite").unlink(missing_ok=True)

sqlite3.register_adapter(Decimal, lambda d: str(d))

from models.db import get_session, init_db
from models.envelope import Envelope, EnvelopeShare
from models.ledger import Ledger
from models.user import User, get_or_create_user, update_balance
from routers.balance import reset_all_balances, reset_selected_balances
from routers.hongbao import (
    _kb_without_mvp,
    calc_total_need,
    send_envelope_with_debit,
)
from services import recharge_service as rs
from web_admin.services import audit_service


def setup_module() -> None:
    init_db()


def _clear_tables() -> None:
    with get_session() as s:
        s.query(EnvelopeShare).delete()
        s.query(Envelope).delete()
        s.query(Ledger).delete()
        s.query(User).delete()


def test_kb_without_mvp_removes_buttons() -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Keep", callback_data="hb:grab:1")],
            [InlineKeyboardButton(text="MVP", callback_data="hb:mvp_send:42")],
        ]
    )
    cleaned = _kb_without_mvp(kb)
    callbacks = [
        btn.callback_data
        for row in cleaned.inline_keyboard
        for btn in row
        if getattr(btn, "callback_data", None)
    ]
    assert "hb:mvp_send:42" not in callbacks
    assert "hb:grab:1" in callbacks


def test_send_envelope_with_debit_deducts_balance() -> None:
    _clear_tables()
    uid = 30001
    with get_session() as s:
        user = get_or_create_user(s, tg_id=uid, username="sender", lang="zh")
        update_balance(s, user, "USDT", Decimal("10"))
        user_id = user.id

    dummy = type("UserStub", (), {"id": user_id})
    env = send_envelope_with_debit(dummy, chat_id=-100, token="USDT", amount_total=Decimal("3"), shares=3, memo="test")
    assert env.id is not None

    with get_session() as s:
        refreshed = s.query(User).filter(User.id == user_id).one()
        remaining = Decimal(str(refreshed.usdt_balance or 0))
        expected = Decimal("10") - calc_total_need("USDT", Decimal("3"))
        assert remaining == expected


def test_send_envelope_with_debit_insufficient_balance() -> None:
    _clear_tables()
    uid = 30002
    with get_session() as s:
        user = get_or_create_user(s, tg_id=uid, username="sender2", lang="zh")
        user_id = user.id

    dummy = type("UserStub", (), {"id": user_id})
    with pytest.raises(ValueError) as exc:
        send_envelope_with_debit(dummy, chat_id=-100, token="USDT", amount_total=Decimal("1"), shares=1, memo="fail")
    assert "INSUFFICIENT_BALANCE" in str(exc.value)


def test_reset_all_balances_creates_ledger_entries() -> None:
    _clear_tables()
    with get_session() as s:
        u = User(tg_id=40001, usdt_balance=Decimal("5.5"), ton_balance=Decimal("2"), point_balance=10)
        s.add(u)

    result = reset_all_balances(note="batch", operator_id=99)
    assert result["affected_users"] == 1
    assert Decimal(result["usdt_total"]) == Decimal("5.500000")

    with get_session() as s:
        refreshed = s.query(User).filter(User.tg_id == 40001).one()
        assert Decimal(str(refreshed.usdt_balance or 0)) == 0
        ledgers = s.query(Ledger).filter(Ledger.user_tg_id == 40001).all()
        tokens = {lg.token for lg in ledgers}
        assert tokens == {"USDT", "TON", "POINT"}


def test_reset_selected_balances_partial_success() -> None:
    _clear_tables()
    with get_session() as s:
        a = User(tg_id=50001, usdt_balance=Decimal("1.2"), ton_balance=Decimal("0"), point_balance=5)
        b = User(tg_id=50002, usdt_balance=Decimal("0"), ton_balance=Decimal("3"), point_balance=0)
        s.add_all([a, b])

    result = reset_selected_balances(user_ids=[50001, 50003], note="select", operator_id=7)
    assert result["success_count"] == 1
    assert result["fail_count"] == 1
    assert result["errors_by_user"][50003] == "NOT_FOUND"

    with get_session() as s:
        remaining = s.query(User).filter(User.tg_id == 50001).one()
        assert Decimal(str(remaining.usdt_balance or 0)) == 0
        ledgers = s.query(Ledger).filter(Ledger.user_tg_id == 50001).all()
        assert len(ledgers) >= 1


def test_recharge_ensure_payment_fallback(monkeypatch) -> None:
    _clear_tables()
    init_db()

    rs._NP_API_KEY = "dummy"
    rs._NP_FORCE_LEGACY = False

    class DummyResp:
        def __init__(self, status_code: int, payload: Dict[str, str]):
            self.status_code = status_code
            self._payload = payload
            self.text = str(payload)

        def json(self) -> Dict[str, str]:
            return self._payload

    calls = {"count": 0}

    def fake_post(url, headers=None, data=None, timeout=None):
        calls["count"] += 1
        if "invoice" in url:
            return DummyResp(500, {"error": "invoice failed"})
        suffix = uuid.uuid4().hex
        return DummyResp(
            200,
            {
                "payment_id": f"PAY-{suffix}",
                "pay_address": f"ADDR-{suffix}",
                "pay_amount": "10.0",
                "pay_currency": "usdttrc20",
            },
        )

    monkeypatch.setattr(rs.requests, "post", fake_post)

    order = rs.new_order(user_id=60001, token="USDT", amount=Decimal("10"))
    ensured = rs.ensure_payment(order)

    assert getattr(ensured, "payment_url")
    assert calls["count"] == 2


def test_audit_service_records_entries() -> None:
    audit_service.clear_audit_entries()
    assert audit_service.record_audit("export_all", operator=123, payload={"status": "success"}) is True
    entries = audit_service.list_audit_entries()
    assert len(entries) == 1
    assert entries[0].action == "export_all"
    assert entries[0].payload["status"] == "success"


def test_audit_service_success_and_failure() -> None:
    audit_service.clear_audit_entries()
    audit_service.record_audit("export_all", 1, {"status": "success"})
    audit_service.record_audit("export_all", 1, {"status": "failed"})
    statuses = [entry.payload["status"] for entry in audit_service.list_audit_entries(action="export_all")]
    assert statuses == ["success", "failed"]


def test_audit_service_non_admin_not_recorded() -> None:
    audit_service.clear_audit_entries()
    result = audit_service.record_audit("export_all", 0, {"status": "success"})
    assert result is False
    assert audit_service.list_audit_entries() == []


def test_audit_service_filter_and_sort() -> None:
    audit_service.clear_audit_entries()
    audit_service.record_audit("export_all", 1, {"order": 1})
    audit_service.record_audit("reset_all_balances", 1, {})
    audit_service.record_audit("export_all", 2, {"order": 2})
    filtered = audit_service.list_audit_entries(action="export_all", reverse=True)
    orders = [entry.payload["order"] for entry in filtered]
    assert orders == [2, 1]


def test_audit_service_idempotent() -> None:
    audit_service.clear_audit_entries()
    first = audit_service.record_audit("export_all", 1, {"status": "success"})
    second = audit_service.record_audit("export_all", 1, {"status": "success"})
    assert first is True
    assert second is False
    assert len(audit_service.list_audit_entries()) == 1

