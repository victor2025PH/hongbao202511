# services/ai_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import asyncio
from typing import Optional, List, Dict

from config import settings

# 懒加载依赖，避免未安装时报错
_openai_client = None

def _ensure_openai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI  # pip install openai>=1.0
        _openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _openai_client

async def ask_ai(
    question: str,
    lang: str = "zh",
    user_ctx: Optional[Dict] = None,
) -> str:
    """
    统一的 AI 调用入口：
    - question: 用户问题
    - lang: 影响系统提示词（让模型用中文/英文回答）
    - user_ctx: 可传入用户ID、最近行为等（可拼进 system/context）
    """
    provider = (settings.AI_PROVIDER or "openai").lower()
    sys_prompt = (
        "你是一个红包游戏机器人，回答要简洁、分步、贴近本机器人功能。"
        "如果问题与本功能无关，礼貌简短拒答并给出可用的菜单指引。"
        "优先使用术语：发红包、抢红包、充值、提现、今日战绩、规则、目标群。"
    )
    if lang.startswith("en"):
        sys_prompt = (
            "You are a Red Packet game bot. Answer concisely with clear steps."
            "If the question is unrelated, politely decline and point to menu entries."
            "Use terms: Send, Grab, Recharge, Withdraw, Today, Rules, Target Group."
        )

    # 组装 messages
    messages = [
        {"role": "system", "content": sys_prompt},
    ]
    if user_ctx:
        messages.append({"role": "system", "content": f"[context]{user_ctx}"})
    messages.append({"role": "user", "content": question})

    if provider == "openai":
        if not settings.OPENAI_API_KEY:
            return "❗ 尚未配置 OPENAI_API_KEY。请在 .env 中设置。"
        client = _ensure_openai()
        try:
            resp = await asyncio.to_thread(
                client.chat.completions.create,
                model=settings.OPENAI_MODEL,
                messages=messages,
                temperature=0.3,
                timeout=settings.AI_TIMEOUT,
                max_tokens=settings.AI_MAX_TOKENS,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            return f"❌ AI 调用失败：{e}"

    elif provider == "openrouter":
        # 兼容 OpenRouter：同样使用 openai SDK，只是走 base_url+key
        if not settings.OPENROUTER_API_KEY:
            return "❗ 尚未配置 OPENROUTER_API_KEY。请在 .env 中设置。"
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=settings.OPENROUTER_API_KEY,
                base_url="https://openrouter.ai/api/v1",
            )
            resp = await asyncio.to_thread(
                client.chat.completions.create,
                model=settings.OPENROUTER_MODEL,
                messages=messages,
                temperature=0.3,
                timeout=settings.AI_TIMEOUT,
                max_tokens=settings.AI_MAX_TOKENS,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            return f"❌ AI 调用失败：{e}"

    else:
        return "⚠️ 未知的 AI_PROVIDER，请设置为 openai 或 openrouter。"
