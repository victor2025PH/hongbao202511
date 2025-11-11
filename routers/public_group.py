from __future__ import annotations

import logging
from typing import List

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config.settings import is_admin as _is_admin
from models.db import get_session
from services.public_group_service import (
    PublicGroupError,
    create_group,
    join_group,
    list_groups,
    pin_group,
    unpin_group,
)
from services.public_group_activity import (
    get_active_campaign_summaries,
    get_active_campaign_detail,
)

router = Router()
log = logging.getLogger("public_group")


def _format_group_brief(group) -> str:
    parts: List[str] = []
    if group.is_pinned and group.pinned_until:
        parts.append("📌 Pinned")
    parts.append(f"#{group.id} {group.name}")
    if group.language:
        parts.append(f"[{group.language}]")
    if group.tags:
        parts.append(" / ".join(f"#{t}" for t in group.tags))
    if group.members_count:
        parts.append(f"👥 {group.members_count}")
    if group.entry_reward_enabled and group.entry_reward_points:
        parts.append(f"🎁 +{group.entry_reward_points}")
    return " ".join(parts)


def _is_admin_user(user_id: int) -> bool:
    try:
        return _is_admin(user_id)
    except Exception:
        return False


def _build_group_keyboard(group) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="🔗 Join", url=group.invite_link),
            InlineKeyboardButton(text="✅ I joined", callback_data=f"public_group:joined:{group.id}"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("groups"))
async def cmd_groups(msg: Message):
    with get_session() as session:
        groups = list_groups(session, limit=5)
        campaigns = get_active_campaign_summaries(session)

    if not groups:
        await msg.answer("No public groups yet. You can start one with /group_create (admin only).")
        return

    lines: List[str] = ["Here are the latest public groups:"]
    if campaigns:
        lines.append("🎉 Active campaigns:")
        for campaign in campaigns:
            detail = get_active_campaign_detail(
                session,
                activity_id=campaign["id"],
                user_tg_id=msg.from_user.id,
            ) or campaign

            card = (detail.get("front_card") or {}) if isinstance(detail, dict) else {}
            title = card.get("title") or detail.get("name") or "Campaign"
            subtitle = card.get("subtitle") or detail.get("description")
            badge = card.get("badge") or detail.get("highlight_badge")
            headline = detail.get("headline")
            countdown_text = detail.get("countdown_text")
            eligible = detail.get("eligible", True)
            has_participated = detail.get("has_participated", False)

            line = f" - {title}"
            if badge:
                line += f" [{badge}]"
            if not eligible:
                line += " (已額滿或已參與)"
            elif has_participated:
                line += " (您已參與)"
            if subtitle:
                line += f": {subtitle}"
            lines.append(line)

            if headline and (not subtitle or headline not in subtitle):
                lines.append(f"   {headline}")
            if countdown_text and countdown_text not in (subtitle or "", headline or ""):
                lines.append(f"   {countdown_text}")

        rules = detail.get("rules") or []
            if rules:
                for item in rules[:3]:
                    label = item.get("label") or item.get("key")
                    value = item.get("value")
                    remaining = item.get("remaining")
                if value is None and remaining is None:
                    continue
                parts = []
                if value is not None:
                    parts.append(f"{label}: {value}")
                    if remaining is not None:
                        parts.append(f"剩餘 {remaining}")
                if parts:
                    lines.append("   " + " / ".join(str(p) for p in parts))

            cta_label = card.get("cta_label")
            cta_link = card.get("cta_link")
            if cta_label and cta_link:
                lines.append(f"   {cta_label}: {cta_link}")
        lines.append("")
    for idx, group in enumerate(groups, start=1):
        lines.append(f"{idx}. {_format_group_brief(group)}")
        if group.description:
            lines.append(f"   {group.description[:140]}")

    await msg.answer("\n".join(lines), disable_web_page_preview=True)
    # 附上第一个群组的快速按钮，其余通过列表消息覆盖
    first_group = groups[0]
    try:
        await msg.answer(
            f"Quick join for **{first_group.name}**",
            reply_markup=_build_group_keyboard(first_group),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        await msg.answer(
            f"Join here: {first_group.invite_link}",
            disable_web_page_preview=True,
        )


@router.message(Command("group_create"))
async def cmd_group_create(msg: Message):
    if not _is_admin_user(msg.from_user.id):
        await msg.answer("Only administrators can create public groups from here.")
        return

    payload = (msg.text or "").split(maxsplit=1)
    if len(payload) < 2:
        await msg.answer("Usage: /group_create <name> | <invite_link> | [tags,comma,separated] | [description]")
        return

    raw_args = payload[1]
    parts = [p.strip() for p in raw_args.split("|")]
    if len(parts) < 2:
        await msg.answer("Please provide at least a name and invite link, separated by '|'.")
        return

    name = parts[0]
    invite_link = parts[1]
    tags = parts[2].split(",") if len(parts) >= 3 and parts[2] else []
    description = parts[3] if len(parts) >= 4 else None

    with get_session() as session:
        try:
            group, risk = create_group(
                session,
                creator_tg_id=msg.from_user.id,
                name=name,
                invite_link=invite_link,
                description=description,
                tags=tags,
            )
        except PublicGroupError as e:
            await msg.answer(f"Failed to create group: {e}")
            return
        except Exception as e:
            log.exception("group_create failed: %s", e)
            await msg.answer("Unexpected error while creating group.")
            return

    risk_note = ""
    if risk.flags:
        risk_note = f"\nRisk flags: {', '.join(risk.flags)}"

    await msg.answer(
        f"Group #{group.id} created successfully! Status: {group.status.value}{risk_note}"
    )


@router.message(Command("group_pin"))
async def cmd_group_pin(msg: Message):
    if not _is_admin_user(msg.from_user.id):
        await msg.answer("Pin operation is restricted to admins.")
        return

    payload = (msg.text or "").split()
    if len(payload) < 2:
        await msg.answer("Usage: /group_pin <group_id> [hours]")
        return

    group_id = payload[1]
    hours = int(payload[2]) if len(payload) >= 3 and payload[2].isdigit() else None

    with get_session() as session:
        try:
            group = pin_group(
                session,
                group_id=int(group_id),
                operator_tg_id=msg.from_user.id,
                duration_hours=hours,
            )
        except PublicGroupError as e:
            await msg.answer(f"Pin failed: {e}")
            return
        except Exception as e:
            log.exception("group_pin failed: %s", e)
            await msg.answer("Unexpected error while pinning group.")
            return

    await msg.answer(
        f"Group #{group.id} pinned until {group.pinned_until.isoformat() if group.pinned_until else 'N/A'}"
    )


@router.message(Command("group_unpin"))
async def cmd_group_unpin(msg: Message):
    if not _is_admin_user(msg.from_user.id):
        await msg.answer("Unpin operation is restricted to admins.")
        return

    payload = (msg.text or "").split()
    if len(payload) < 2:
        await msg.answer("Usage: /group_unpin <group_id>")
        return

    group_id = payload[1]

    with get_session() as session:
        try:
            group = unpin_group(session, group_id=int(group_id))
        except PublicGroupError as e:
            await msg.answer(f"Unpin failed: {e}")
            return
        except Exception as e:
            log.exception("group_unpin failed: %s", e)
            await msg.answer("Unexpected error while unpinning group.")
            return

    await msg.answer(f"Group #{group.id} unpinned.")


@router.callback_query(F.data.startswith("public_group:joined:"))
async def cb_public_group_joined(cb: CallbackQuery):
    parts = cb.data.split(":")
    if len(parts) != 3:
        await _safe_answer(cb, "Invalid callback payload.")
        return

    group_id = parts[2]
    with get_session() as session:
        try:
            result = join_group(
                session,
                group_id=int(group_id),
                user_tg_id=cb.from_user.id,
            )
        except PublicGroupError as e:
            await _safe_answer(cb, f"Join failed: {e}", show_alert=True)
            return
        except Exception as e:
            log.exception("join_group failed: %s", e)
            await _safe_answer(cb, "Unexpected error, please try later.", show_alert=True)
            return

    reward_text = ""
    if result["reward_claimed"]:
        reward_text = f"\nReward: +{result['reward_points']} points 🎉"
    elif result["reward_status"] == "skipped":
        reward_text = "\nReward not available, but thanks for joining!"

    await _safe_answer(cb, f"Great! Logged your join.{reward_text}", show_alert=True)


async def _safe_answer(cb: CallbackQuery, text: str, *, show_alert: bool = False) -> None:
    try:
        await cb.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest:
        pass
    except Exception:
        pass

