# tests/test_i18n.py
# -*- coding: utf-8 -*-
"""
测试国际化语言包加载与翻译函数
"""

import pytest
from core.i18n.i18n import t, _load_messages


def test_load_messages_keys_consistency():
    zh = _load_messages("zh")
    en = _load_messages("en")

    # 键集合一致
    zh_keys = set(zh.keys())
    en_keys = set(en.keys())
    assert zh_keys == en_keys, f"zh.yml 和 en.yml 键不一致: {zh_keys ^ en_keys}"


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_translate_basic(lang):
    assert "红包" in t("menu.send", "zh")
    assert "Send Packet" in t("menu.send", "en")

    text = t("hongbao.summary.total", lang, amount="3", token="POINT", shares=5)
    assert "3" in text


def test_fallback_language():
    # 不存在的 key 应返回空串
    raw = t("nonexistent.key", "zh")
    assert raw == ""

    # 不存在语言时，回退到 fallback
    msg = t("menu.send", "fr")
    assert isinstance(msg, str)
    assert msg.strip()
