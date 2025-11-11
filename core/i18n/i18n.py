# core/i18n/i18n.py
# -*- coding: utf-8 -*-
"""
国际化工具：
- 从 core/i18n/messages/{lang}.yml 载入多语言词条
- t(key, lang, **kwargs)  → 返回翻译并进行格式化（级联回退）
- t_first([keys], lang, **kwargs) → 从一组候选键里按顺序取第一个有效翻译
- t_non_empty(key, default, lang, **kwargs) → 翻译缺失时返回给定默认值
- t_chain([keys], default, lang, **kwargs)  → t_first 的默认值版本
- i18n.self_check(...)    → 检查语言包与（可选）代码中使用键的一致性，并检测顶级键重复
- i18n.reload()           → 清空缓存以便热重载 yml
- i18n.available_languages() → 返回已存在的语言文件列表

改动要点：
1) _canon_lang 由“只认 zh/en”改为“动态识别 messages 目录下的语言”，并内置支持 fr/de/es/hi/vi/th。
2) _KNOWN_LANGS 预置上述语言集合，并在运行时与 messages/*.yml 自动合并。
3) 其他逻辑保持不变，回退顺序：当前语言 → en → zh → 空串。
"""

from __future__ import annotations
import os
import re
import threading
from functools import lru_cache
from typing import Dict, Any, Iterable, List, Sequence, Tuple, Set

try:
    import yaml  # PyYAML 是 aiogram 常见依赖环境里可用的
except Exception:  # 兜底：无 yaml 时给出简化错误
    yaml = None  # type: ignore


# 语言包目录
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_MSG_DIR = os.path.join(_BASE_DIR, "messages")

# 读写锁（多线程安全）
_LOCK = threading.RLock()

# 默认与回退语言
_DEFAULT_LANG = "zh"
_FALLBACK_LANG = "en"

# 可识别的语言列表（会在运行时依据 messages/*.yml 自动扩展）
# 这里预置 zh/en 以及将要新增的 fr/de/es/hi/vi/th，避免“只认两种语言”的硬回退。
_KNOWN_LANGS = {"zh", "en", "fr", "de", "es", "hi", "vi", "th"}


def _read_yaml(path: str) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed. Please add 'pyyaml' to requirements.")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # 扁平化嵌套（以 key1.key2 形式存储）
    flat: Dict[str, Any] = {}

    def _flatten(prefix: str, obj: Any):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _flatten(f"{prefix}.{k}" if prefix else str(k), v)
        else:
            flat[prefix] = obj

    _flatten("", data)
    return flat


def _list_lang_files() -> List[str]:
    """列出 messages 目录下可用的语言（去掉扩展名）"""
    try:
        files = [fn for fn in os.listdir(_MSG_DIR) if fn.endswith(".yml")]
    except FileNotFoundError:
        return []
    langs = sorted({os.path.splitext(fn)[0] for fn in files})
    return langs


def _all_known_langs() -> Set[str]:
    """
    运行时可用语言集合：预置 _KNOWN_LANGS ∪ messages 目录扫描结果。
    这样新增 *.yml 不需要改代码即可识别。
    """
    # 合并运行时扫描的语言
    dynamic = set(_list_lang_files())
    return set(_KNOWN_LANGS) | dynamic


def _canon_lang(code: str | None) -> str:
    """
    将各种语言标记规范化为可用语言：
    1) 先用 messages/*.yml 和 _KNOWN_LANGS 计算“可用集合”
    2) 优先匹配完整标签（如 pt-br），否则取主子标签（pt）
    3) 兼容历史：首选 zh/en 前缀匹配，其余未知回退默认语言
    """
    if not code:
        return _DEFAULT_LANG
    c = str(code).strip().lower().replace("_", "-")
    if not c:
        return _DEFAULT_LANG

    available = _all_known_langs()

    # 完整命中（如 "fr" 或 "pt-br"）
    if c in available:
        return c

    # 主子标签命中（"fr-xx" → "fr"）
    primary = c.split("-", 1)[0]
    if primary in available:
        return primary

    # 历史兼容（避免旧逻辑下的奇怪退回）
    if c.startswith("zh"):
        return "zh"
    if c.startswith("en"):
        return "en"

    return _DEFAULT_LANG


@lru_cache(maxsize=32)
def _load_messages(lang: str) -> Dict[str, str]:
    """
    加载指定语言的扁平化词典，带 LRU 缓存。
    对外保留（tests 中会调用）
    """
    with _LOCK:
        path = os.path.join(_MSG_DIR, f"{lang}.yml")
        mapping = _read_yaml(path)
        # 统一转为 str
        return {str(k): str(v) for k, v in mapping.items()}


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """
    翻译函数（安全格式化 + 级联回退）：
    - 当前语言（规范化）→ 英文 → 中文 → 空串
    - 不再返回形如 "[zh|en:key]" 的占位，避免在 UI 上露出占位符
    - format(**kwargs) 时若缺参数，自动忽略而不是抛错
    """
    cur_lang = _canon_lang(lang)
    # 当前语言
    cur = _load_messages(cur_lang)
    if key in cur:
        try:
            return cur[key].format(**kwargs) if kwargs else cur[key]
        except Exception:
            return cur[key]

    # 回退到英文
    if cur_lang != _FALLBACK_LANG:
        fb = _load_messages(_FALLBACK_LANG)
        if key in fb:
            try:
                return fb[key].format(**kwargs) if kwargs else fb[key]
            except Exception:
                return fb[key]

    # 再回退到中文（当当前语言是英文时就跳过这一步）
    if cur_lang != _DEFAULT_LANG and _DEFAULT_LANG != _FALLBACK_LANG:
        zh_map = _load_messages(_DEFAULT_LANG)
        if key in zh_map:
            try:
                return zh_map[key].format(**kwargs) if kwargs else zh_map[key]
            except Exception:
                return zh_map[key]

    # 全部缺失 → 返回空串，避免 UI 出现占位符
    return ""


def t_first(keys: Sequence[str], lang: str | None = None, **kwargs) -> str:
    """
    多键回退：按顺序尝试一组键，返回第一个有值的翻译（支持格式化）。
    典型场景：按钮/状态存在多个常见命名（success/paid/completed）时的兼容。
    """
    for k in keys:
        val = t(k, lang, **kwargs)
        if val:
            return val
    return ""


# ===== 业务友好型兜底方法 =====
def t_non_empty(key: str, default: str, lang: str | None = None, **kwargs) -> str:
    """
    单键兜底：等价于 (t(key, lang, **kwargs) or default)，但保证返回 str 且 strip 后非空。
    用于“正文类文案”避免传空给 Telegram / 前端渲染。
    """
    val = t(key, lang, **kwargs)
    val = (val or "").strip()
    return val if val else str(default)


def t_chain(keys: Sequence[str], default: str, lang: str | None = None, **kwargs) -> str:
    """
    多键兜底：等价于 (t_first(keys, lang, **kwargs) or default)。
    典型用法：t_chain(["balance.title", "asset.title", "balance_page.title"], "💼 我的资产", lang)
    """
    val = t_first(keys, lang, **kwargs)
    val = (val or "").strip()
    return val if val else str(default)


class _I18NDiag:
    """
    自检/工具集合：
      - self_check(scan_paths: Iterable[str] | None = None, examples: int = 10)
          1) 比对 zh.yml 与 en.yml 的键集合
          2) （可选）扫描代码中的 t("xx.yy") / t_first([...]) 使用，报告哪些键在语言包中缺失
          3) 检测各语言文件的“顶级键重复”（例如同文件中两次出现 'balance:'），
             这类重复在 YAML 解析时会被后者静默覆盖
      - reload()：清除缓存以便热重载 yml
      - available_languages()：返回 messages 目录下可用语言列表
    """

    _RE_T_KEY = re.compile(r"""(?P<fn>\bt\()\s*["'](?P<key>[a-zA-Z0-9_.]+)["']""")
    _RE_TFIRST_KEYS = re.compile(
        r"""t_first\(\s*\[\s*(?P<keys>(?:"[a-zA-Z0-9_.]+"\s*,\s*)*"[a-zA-Z0-9_.]+")\s*\]"""
    )
    # 顶级键检测：匹配 0 缩进处形如 "key:" 的行（忽略注释与空行）
    _RE_TOP_KEY = re.compile(r"""^(?P<key>[A-Za-z0-9_]+)\s*:\s*(?:#.*)?$""")

    @staticmethod
    def _scan_top_level_keys(file_path: str) -> Tuple[Set[str], List[str]]:
        """
        扫描 yml 文本，返回 (顶级键集合, 重复键列表)。
        注意：这是基于文本的启发式检测，不解析 YAML 语义，仅用于提前暴露明显的重复。
        """
        keys_seen: Set[str] = set()
        dups: List[str] = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    if not line or line.lstrip().startswith("#"):
                        continue
                    # 只看 0 缩进
                    if line[:1].isspace():
                        continue
                    m = _I18NDiag._RE_TOP_KEY.match(line)
                    if not m:
                        continue
                    k = m.group("key")
                    if k in keys_seen:
                        dups.append(k)
                    else:
                        keys_seen.add(k)
        except Exception:
            pass
        return keys_seen, dups

    def self_check(self, scan_paths: Iterable[str] | None = None, examples: int = 10) -> str:
        try:
            langs = _list_lang_files()
            if not langs:
                return "[i18n] Self-check: no language files found."

            # 注册可用语言
            # 这里与 messages/*.yml 合并，以便 available_languages 与 _canon_lang 同步更新
            _KNOWN_LANGS.update(langs)

            zh = set(_load_messages("zh").keys()) if "zh" in langs else set()
            en = set(_load_messages("en").keys()) if "en" in langs else set()

            missing_in_en = sorted(list(zh - en))
            missing_in_zh = sorted(list(en - zh))

            lines: List[str] = ["[i18n] Self-check report:"]
            lines.append(f" - available languages: {', '.join(langs)}")
            lines.append(f" - zh keys: {len(zh)}")
            lines.append(f" - en keys: {len(en)}")

            if not missing_in_en and not missing_in_zh:
                lines.append(" - ✅ zh & en keys are consistent.")
            else:
                if missing_in_en:
                    lines.append(f" - ⚠️ Missing in en: {len(missing_in_en)}")
                    if examples > 0:
                        lines.append(f"   e.g. {missing_in_en[:examples]}")
                if missing_in_zh:
                    lines.append(f" - ⚠️ Missing in zh: {len(missing_in_zh)}")
                    if examples > 0:
                        lines.append(f"   e.g. {missing_in_zh[:examples]}")

            # 语言文件顶级键重复检测
            lines.append(" - top-level duplicate keys per language:")
            for lang in ["zh", "en"]:
                if lang not in langs:
                    lines.append(f"   * {lang}.yml: (file not found)")
                    continue
                path = os.path.join(_MSG_DIR, f"{lang}.yml")
                # 扫描顶级键并输出重复
                keys_seen, dups = self._scan_top_level_keys(path)
                if dups:
                    lines.append(f"   * {lang}.yml: ❗ duplicates found -> {sorted(set(dups))}")
                else:
                    lines.append(f"   * {lang}.yml: ✅ no top-level duplicates (found {len(keys_seen)} keys)")

            # 代码扫描（可选）
            if scan_paths:
                used_keys = set()
                used_key_sets: List[Tuple[str, List[str]]] = []

                def _scan_file(fp: str):
                    try:
                        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                            s = f.read()
                    except Exception:
                        return
                    for m in self._RE_T_KEY.finditer(s):
                        used_keys.add(m.group("key"))
                    for m in self._RE_TFIRST_KEYS.finditer(s):
                        arr = m.group("keys")
                        keys = [x.strip().strip("'\"") for x in arr.split(",")]
                        used_key_sets.append((fp, keys))
                        for k in keys:
                            used_keys.add(k)

                for root in scan_paths:
                    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
                        for fn in filenames:
                            if not fn.endswith((".py", ".pyi", ".txt")):
                                continue
                            _scan_file(os.path.join(dirpath, fn))

                zh_missing_used = sorted([k for k in used_keys if k not in zh])
                en_missing_used = sorted([k for k in used_keys if k not in en])

                lines.append(f" - scan paths: {', '.join(scan_paths)}")
                lines.append(f" - used i18n keys in code: {len(used_keys)}")
                if zh_missing_used:
                    lines.append(f" - ❗ used-but-missing in zh: {len(zh_missing_used)}")
                    if examples > 0:
                        lines.append(f"   e.g. {zh_missing_used[:examples]}")
                if en_missing_used:
                    lines.append(f" - ❗ used-but-missing in en: {len(en_missing_used)}")
                    if examples > 0:
                        lines.append(f"   e.g. {en_missing_used[:examples]}")

            return "\n".join(lines)
        except Exception as e:
            return f"[i18n] Self-check failed: {e!r}"

    def reload(self) -> None:
        """清空缓存，以便在运行中热重载 yml。"""
        with _LOCK:
            _load_messages.cache_clear()

    def available_languages(self) -> List[str]:
        """返回 messages 目录下可用语言列表（不含扩展名）"""
        return _list_lang_files()


# 对外导出
i18n = _I18NDiag()

__all__ = ["t", "t_first", "t_non_empty", "t_chain", "i18n", "_load_messages"]
