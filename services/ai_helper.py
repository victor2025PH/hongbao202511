# services/ai_helper.py
# -*- coding: utf-8 -*-
"""
统一 AI 调用服务：
- 支持 OpenAI 与 OpenRouter（通过 settings.AI_PROVIDER 选择：openai / openrouter）
- 依赖 openai 官方 SDK（>=1.0）：pip install openai>=1.0
"""

from __future__ import annotations

import logging
from typing import List, Tuple, Optional

from config.settings import settings

try:
    from openai import AsyncOpenAI
except Exception:  # 未安装时避免 ImportError 直接炸
    AsyncOpenAI = None  # type: ignore

log = logging.getLogger("ai_helper")


def _mk_client() -> Optional[AsyncOpenAI]:
    """根据配置构造 AsyncOpenAI 客户端；未配置则返回 None。"""
    if AsyncOpenAI is None:
        log.error("openai SDK 未安装。请先: pip install openai>=1.0")
        return None

    provider = (settings.AI_PROVIDER or "openai").lower()

    if provider == "openai":
        if not settings.OPENAI_API_KEY:
            log.error("缺少 OPENAI_API_KEY")
            return None
        return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    if provider == "openrouter":
        if not settings.OPENROUTER_API_KEY:
            log.error("缺少 OPENROUTER_API_KEY")
            return None
        # OpenRouter 兼容 OpenAI SDK：通过 base_url + key
        return AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
        )

    log.error("未知 AI_PROVIDER=%s（可选：openai / openrouter）", provider)
    return None


def _sys_prompt(lang: str) -> str:
    if (lang or "").lower().startswith("en"):
        return (
            "You are a help bot for a Red Packet game on Telegram. "
            "Answer concisely with numbered/bulleted steps. Keep answers scoped to: "
            "sending, grabbing, recharge, withdraw, today's stats, rules, fairness, target groups. "
            "If the question is unrelated, decline briefly and point to the menu."
        )
    return (
        "你是一位 Telegram 红包游戏的帮助机器人。请使用简洁的要点/编号步骤作答，"
        "回答范围聚焦在：发红包、抢红包、充值、提现、今日战绩、规则/公平性、目标群 等。"
        "若问题与功能无关，请简短礼貌拒答，并提示用户回到菜单。"
    )


def _trim_context(context: Optional[List[Tuple[str, str]]], max_items: int = 6, max_chars: int = 2400) -> List[Tuple[str, str]]:
    """
    同时按条数与近似字符数双限裁剪上下文，避免提示过长。
    context: [(role, text)], role in {"system","user","assistant"}
    """
    if not context:
        return []
    ctx = context[-max_items:]
    total = 0
    trimmed: List[Tuple[str, str]] = []
    for role, text in reversed(ctx):  # 从最近往回加
        s = text or ""
        total += len(s)
        if total > max_chars and trimmed:
            break
        trimmed.append((role, s))
    trimmed.reverse()
    return trimmed


async def ai_answer(
    question: str,
    lang: str = "zh",
    user_id: Optional[int] = None,
    context: Optional[List[Tuple[str, str]]] = None,
) -> Optional[str]:
    """
    调用大模型返回答案文本。
    返回 None 表示请求失败（上层可用 i18n 的 help.ai_fallback 兜底）。
    """
    client = _mk_client()
    if client is None:
        return None

    messages = [{"role": "system", "content": _sys_prompt(lang)}]
    for role, text in _trim_context(context):
        # 只接受 user/assistant/system 三种
        r = role if role in {"user", "assistant", "system"} else "user"
        messages.append({"role": r, "content": text})
    messages.append({"role": "user", "content": (question or "").strip()})

    # 选择模型
    provider = (settings.AI_PROVIDER or "openai").lower()
    if provider == "openrouter":
        model = settings.OPENROUTER_MODEL or "openai/gpt-4o-mini"
    else:
        model = settings.OPENAI_MODEL or "gpt-4o-mini"

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            max_tokens=settings.AI_MAX_TOKENS,
            timeout=settings.AI_TIMEOUT,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            log.warning("AI 返回空内容")
            return None
        return content
    except Exception as e:
        # 常见错误：超时、鉴权失败、模型名错误、配额不足等
        log.exception("AI 调用异常: %s", e)
        return None
