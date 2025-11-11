# services/invite_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Tuple

from core.i18n.i18n import t
from config.feature_flags import flags

from models.db import get_session
from models.user import User, update_balance
from models.ledger import add_ledger_entry, LedgerType

from models.invite import add_invite, get_progress, update_progress  # type: ignore
try:
    from models.invite import InviteProgress  # type: ignore
except Exception:
    InviteProgress = None  # type: ignore

def _FF(name: str, default):
    return getattr(flags, name, default)

def _emoji_bar(percent: int, slots: int = 10) -> str:
    p = max(0, min(100, int(percent)))
    step = max(1, 100 // (slots or 10))
    filled = min(slots, p // step)
    return "🟩" * filled + "⬜" * (slots - filled)

def build_invite_progress_message(percent: int, lang: str = "zh") -> str:
    p = max(0, min(100, int(percent)))
    bar = _emoji_bar(p)
    if p >= 100: phase_text = t("phase.100", lang)
    elif p >= 99: phase_text = t("phase.99", lang)
    elif p >= 95: phase_text = t("phase.95_98", lang)
    elif p >= 90: phase_text = t("phase.90_94", lang)
    elif p >= 80: phase_text = t("phase.80_89", lang)
    else: phase_text = t("phase.80_89", lang)
    title = t("welfare.invite_title", lang)
    body = t("welfare.invite_compact", lang, percent=p, bar=bar, phase_text=phase_text)
    extra = "\n\n" + (t("welfare.invite_rules", lang) or "") if 0 < p < 3 else ""
    return f"{title}\n\n{body}{extra}"

def get_invite_progress_text(inviter_id: int, lang: str = "zh") -> Tuple[str, int]:
    prog = get_progress(inviter_id)
    percent = int(prog.get("progress_percent", 0))
    text = build_invite_progress_message(percent, lang=lang)
    return text, percent

def add_invite_and_rewards(inviter_id: int, invitee_id: int, *, give_extra_points: bool = True) -> bool:
    ok = add_invite(inviter_id, invitee_id)
    if not ok: return False
    extra_pts = int(_FF("INVITE_EXTRA_POINTS", 0))
    if give_extra_points and extra_pts > 0:
        with get_session() as s:
            u = s.query(User).filter_by(tg_id=inviter_id).first() or User(tg_id=inviter_id)
            s.add(u)
            update_balance(s, u, "POINT", extra_pts)
            add_ledger_entry(s, user_tg_id=inviter_id, ltype=LedgerType.INVITE_REWARD,
                             token="POINT", amount=extra_pts, ref_type="INVITE",
                             ref_id=str(invitee_id), note="邀请奖励（积分）")
            s.commit()
    return True

def redeem_points_to_progress(user_id: int, *, lang: str = "zh") -> Tuple[bool, str, int]:
    need = max(1, int(_FF("POINTS_PER_PROGRESS", 1000)))
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first() or User(tg_id=user_id)
        s.add(u)
        have_points = int(u.point_balance or 0)
        max_steps = int(have_points // need)
        if max_steps <= 0:
            return (False, t("welfare.exchange.not_enough", lang, resource=t("asset.points", lang) or "Points"),
                    int(get_progress(user_id).get("progress_percent", 0)))
        cost_points = need * max_steps
        update_balance(s, u, "POINT", -int(cost_points))
        add_ledger_entry(s, user_tg_id=user_id, ltype=LedgerType.EXCHANGE_POINTS_TO_PROGRESS,
                         token="POINT", amount=-int(cost_points), ref_type="INVITE",
                         ref_id="REDEEM", note=f"积分兑换进度（-{int(cost_points)} POINT）")
        prog_before = int(get_progress(user_id).get("progress_percent", 0))
        prog_after = min(100, prog_before + max_steps)
        from sqlalchemy import update as sql_update
        if InviteProgress is not None:
            s.execute(sql_update(InviteProgress)
                      .where(InviteProgress.inviter_tg_id == user_id)
                      .values(progress_percent=prog_after))
        threshold = int(_FF("ENERGY_REWARD_AT_PROGRESS", 2))
        energy_amt = int(_FF("ENERGY_REWARD_AMOUNT", 1000))
        crossed = prog_before < threshold <= prog_after
        if crossed and energy_amt > 0:
            update_balance(s, u, "ENERGY", energy_amt)
            add_ledger_entry(s, user_tg_id=user_id, ltype=LedgerType.INVITE_REWARD,
                             token="ENERGY", amount=energy_amt, ref_type="INVITE",
                             ref_id="REWARD", note=f"跨 {threshold}% 奖励能量（+{energy_amt}）")
            try:
                update_progress(user_id, delta_energy=energy_amt)  # 可选统计
            except Exception:
                pass
        s.commit()
        text = t("welfare.exchange.success_progress", lang, value=max_steps) or f"✅ +{max_steps}%"
        return True, text, prog_after

def redeem_energy_to_points(user_id: int, *, lang: str = "zh") -> Tuple[bool, str]:
    ratio = max(1, int(_FF("ENERGY_TO_POINTS_RATIO", 1000)))
    value = max(1, int(_FF("ENERGY_TO_POINTS_VALUE", 100)))
    with get_session() as s:
        u = s.query(User).filter_by(tg_id=user_id).first() or User(tg_id=user_id)
        s.add(u)
        have_energy = int(u.energy_balance or 0)
        if have_energy < ratio:
            return False, t("welfare.exchange.not_enough", lang, resource=t("labels.energy", lang) or "Energy")
        update_balance(s, u, "ENERGY", -ratio)
        update_balance(s, u, "POINT", value)
        add_ledger_entry(s, user_tg_id=user_id, ltype=LedgerType.EXCHANGE_ENERGY_TO_POINTS,
                         token="ENERGY", amount=-ratio, ref_type="INVITE",
                         ref_id="EXCHANGE", note=f"能量兑换积分（-{ratio} ENERGY）")
        add_ledger_entry(s, user_tg_id=user_id, ltype=LedgerType.EXCHANGE_ENERGY_TO_POINTS,
                         token="POINT", amount=value, ref_type="INVITE",
                         ref_id="EXCHANGE", note=f"能量兑换积分（+{value} POINT）")
        s.commit()
        return True, t("welfare.exchange.success_points", lang, value=value) or f"✅ +{value} Points"

def build_invite_share_link(user_id: int) -> str:
    prefix = _FF("INVITE_LINK_PREFIX", "https://t.me/your_bot?start=invite_")
    return f"{prefix}{user_id}"
