# locales/strings.py
# -*- coding: utf-8 -*-
"""
多语言词条（中文 / 英文）：
- STRINGS[lang][key]
- 用 get_string(lang, key, **kwargs) 获取文案
"""

from __future__ import annotations
from typing import Dict

STRINGS: Dict[str, Dict[str, str]] = {
    "zh": {
        "menu_send": "🧧 发红包",
        "menu_recharge": "💳 充值",
        "menu_today": "📊 今日战绩",
        "menu_assets": "💰 我的资产",
        "menu_records": "📜 我的记录",
        "menu_welfare": "🎁 福利中心",
        "menu_admin": "⚙️ 管理面板",
        "menu_settings": "🔧 个人设置",
        "menu_language": "🌐 语言切换",

        "welcome": "👋 欢迎 {username}！\n请选择功能：",
        "balance": "💰 资产总览：\nUSDT: {usdt:.2f}\nTON: {ton:.2f}\n积分: {points}\n能量: {energy}",

        "grabbed": "🎉 你抢到了 {amount:.2f} {token}！",
        "already_grabbed": "⚠️ 你已经领取过该红包。",
        "finished": "🚫 红包已抢完。",

        "rank_title": "🏆 本轮排行榜",
        "rank_item": "👤 {user}: {amount:.2f} {token}",
        "lucky": "🍀 运气最佳: {user} 抢到 {amount:.2f} {token}",

        "invite_title": "🎯 邀请有奖",
        "invite_progress": "当前进度: {percent}%\n已邀请: {count} 人\n积分: {points} | 能量: {energy}",
        "invite_new": "🎉 新用户 {user} 通过你的邀请加入！进度+{percent_inc}%。",

        "recharge_title": "💳 充值中心",
        "recharge_order": "已生成订单 #{id}\n金额: {amount:.2f} {token}\n请在 {expire} 前完成支付。",
        "recharge_success": "✅ 充值成功！到账 {amount:.2f} {token}",
        "recharge_failed": "❌ 充值失败，请重试。",
        "recharge_expired": "⌛ 订单已过期。",
    },

    "en": {
        "menu_send": "🧧 Send Red Packet",
        "menu_recharge": "💳 Recharge",
        "menu_today": "📊 Today’s Stats",
        "menu_assets": "💰 My Assets",
        "menu_records": "📜 My Records",
        "menu_welfare": "🎁 Welfare Center",
        "menu_admin": "⚙️ Admin Panel",
        "menu_settings": "🔧 Settings",
        "menu_language": "🌐 Switch Language",

        "welcome": "👋 Welcome {username}!\nPlease choose an option:",
        "balance": "💰 Balance:\nUSDT: {usdt:.2f}\nTON: {ton:.2f}\nPoints: {points}\nEnergy: {energy}",

        "grabbed": "🎉 You grabbed {amount:.2f} {token}!",
        "already_grabbed": "⚠️ You already claimed this red packet.",
        "finished": "🚫 This red packet is finished.",

        "rank_title": "🏆 Round Ranking",
        "rank_item": "👤 {user}: {amount:.2f} {token}",
        "lucky": "🍀 Lucky Winner: {user} got {amount:.2f} {token}",

        "invite_title": "🎯 Invite Rewards",
        "invite_progress": "Progress: {percent}%\nInvited: {count}\nPoints: {points} | Energy: {energy}",
        "invite_new": "🎉 New user {user} joined via your invite! Progress +{percent_inc}%.",

        "recharge_title": "💳 Recharge Center",
        "recharge_order": "Order #{id}\nAmount: {amount:.2f} {token}\nPlease pay before {expire}.",
        "recharge_success": "✅ Recharge successful! {amount:.2f} {token} credited.",
        "recharge_failed": "❌ Recharge failed, please try again.",
        "recharge_expired": "⌛ Order expired.",
    }
}


def get_string(lang: str, key: str, **kwargs) -> str:
    """
    获取多语言文案；若缺失则回退到英文
    """
    if lang not in STRINGS:
        lang = "en"
    s = STRINGS[lang].get(key) or STRINGS["en"].get(key) or key
    try:
        return s.format(**kwargs)
    except Exception:
        return s
