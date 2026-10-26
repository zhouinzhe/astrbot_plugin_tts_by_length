"""网页「设置」页的后端逻辑。

纯 Python，不依赖 AstrBot，方便单独测试。这里只管三件事：
1. 读 _conf_schema.json，把所有设置项连同当前值整理给网页——网页按它自动生成表单，
   以后在 _conf_schema.json 里加设置项，网页不用改；
2. 网页提交修改时，按类型逐项校验、转换；只要有一项不合法，整批都不保存，并告诉网页是哪几项、为什么；
3. 把通过校验的修改写进配置对象（写盘由调用方负责），写盘失败时可以整批退回。
"""

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    from .voice_library import VoiceLibraryError, parse_voice_id
except ImportError:  # 不是以包的方式加载时的兜底
    from voice_library import VoiceLibraryError, parse_voice_id

_TYPES = ("bool", "int", "float", "string", "text", "list")
MISSING = object()  # 配置文件里原来没有这一项

# 不让网页把密钥原样读出来：只告诉网页「已设置」和末四位
SECRET_PATHS = frozenset({"fish.api_key"})

# 数值范围：和 main.py 里 _settings() / _fish_config() 的限制保持一致
# （那边超出范围会被悄悄夹回去，这里提前拦下来并告诉用户）。
# 格式：路径 -> (最小, 最大, 步长, 用滑块)
_NUMBER_RULES = {
    "basic.max_chars": (0, 10000, 1, False),  # 0 = 不限制
    "basic.min_chars": (0, 10000, 1, False),
    "basic.trigger_probability": (0.0, 1.0, 0.05, True),
    "basic.tts_timeout": (0.0, 600.0, 1, False),  # 0 = 不限时
    "fish_advanced.speed": (0.5, 2.0, 0.05, True),
    "fish_advanced.volume": (-30.0, 30.0, 1, True),
    "fish_advanced.temperature": (0.0, 1.0, 0.05, True),
    "fish_advanced.top_p": (0.0, 1.0, 0.05, True),
    "fish_advanced.retries": (0, 3, 1, False),
    "emotion.max_tags": (0, 50, 1, False),
    "memory.size": (1, 200, 1, False),
    "memory.keep_hours": (1, 24 * 365, 1, False),
    "memory.match_tolerance": (3.0, 600.0, 1, False),
    "memory.recent_count": (1, 10, 1, False),
    "memory.recent_minutes": (1, 1440, 1, False),
}
_FALLBACK_RANGE = (-1_000_000, 1_000_000, 1, False)  # 以后新增数值项忘了登记范围时的兜底

# 某一项只有在另一项取特定值时才生效：网页里据此把它淡化显示（仍然可以改）
_EXTRA_DEPENDS = {
    "emotion.extra_tags": [("emotion.tag_mode", ("whitelist",))],
    "emotion.prompt": [("emotion.inject_prompt", (True,))],
    "memory.recent_keywords": [("memory.recent_mode", ("keywords",))],
    "memory.recent_minutes": [("memory.recent_mode", ("keywords", "always"))],
    "memory.recent_note": [("memory.recent_mode", ("keywords", "always"))],
    "memory.quote_note": [("memory.inject_quote", (True,))],
    "memory.match_tolerance": [("memory.inject_quote", (True,))],
}

_PLACEHOLDERS = {
    "fish.api_key": "粘贴 Fish 的 API Key",
    "fish.reference_id": "音色 ID，或直接粘贴 Fish 音色页面地址；留空用 Fish 默认音色",
    "fish_advanced.extra_body": '{"key": "value"}',
    "basic.skip_platforms": "每行一个，例如：\nwebchat\ntelegram",
    "emotion.extra_tags": "每行一个标签名，不带方括号，例如：\nsoftly laughing",
    "reading.custom_filter_patterns": "每行一条正则，例如：\n（.*?）\n&&.+?&&",
    "memory.recent_keywords": "每行一个关键词",
}
_MONO_PATHS = frozenset(
    {"fish.api_base", "fish.reference_id", "fish_advanced.extra_body", "reading.custom_filter_patterns"}
)

# 文本长度上限，防止误操作塞进超大内容
_MAX_STRING = 500
_MAX_TEXT = 4000
_MAX_LIST = 100
_ITEM_MAX = {"emotion.extra_tags": 40, "memory.recent_keywords": 50, "basic.skip_platforms": 50}
_DEFAULT_ITEM_MAX = 300
_KEY_PATTERN = re.compile(r"[\x21-\x7e]{8,256}")  # 可见 ASCII，无空白：密钥会放进 HTTP 请求头
_URL_PATTERN = re.compile(r"https?://[^\s/$.?#][^\s]*", re.IGNORECASE)
_REGEX_SAMPLE = "你好，今天天气不错 hello 123"


class SettingError(ValueError):
    """某一项的值不合法。消息直接显示在那一项下面。"""


@dataclass
class Result:
    errors: dict = field(default_factory=dict)  # 路径 -> 原因（有任何一项就整批不保存）
    warnings: list = field(default_factory=list)  # 不拦着、但值得提醒的事
    changed: dict = field(default_factory=dict)  # 路径 -> (原值或 MISSING, 新值)


# ---------- 读 schema ----------
def load_schema(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _groups(schema: dict):
    for gkey, gdef in schema.items():
        if isinstance(gdef, dict) and gdef.get("type") == "object":
            yield gkey, gdef


def _items(schema: dict) -> dict:
    """路径（组.项）-> (组, 项名, 项定义)。类型不认识的项不给网页改。"""
    found = {}
    for gkey, gdef in _groups(schema):
        for key, item in (gdef.get("items") or {}).items():
            if isinstance(item, dict) and item.get("type") in _TYPES:
                found[f"{gkey}.{key}"] = (gkey, key, item)
    return found


def _split_title(text: str, fallback: str) -> tuple:
    """「⚙️ 基础设置」-> ("⚙️", "基础设置")。开头没有图标就只有标题。"""
    text = str(text or fallback).strip()
    head, _, rest = text.partition(" ")
    if rest and not re.search(r"\w", head):
        return head, rest.strip()
    return "", text


def _option_list(item: dict) -> list:
    options = item.get("options")
    if not isinstance(options, list) or not options:
        return []
    labels = item.get("labels")
    if not (isinstance(labels, list) and len(labels) == len(options)):
        labels = options
    return [{"value": value, "label": str(label)} for value, label in zip(options, labels)]


def _short_label(item: dict, value) -> str:
    """选项标签里冒号、括号前面的那一截：「keywords：提到关键词时补充（推荐）」-> keywords。"""
    for option in _option_list(item):
        if option["value"] == value:
            return re.split(r"[：（(]", option["label"], maxsplit=1)[0].strip() or str(value)
    return str(value)


# ---------- 当前值 ----------
def _stored(config, group: str, key: str):
    section = config.get(group)
    if isinstance(section, dict) and key in section:
        return section[key]
    return MISSING


def _display_value(item: dict, raw):
    """把配置里存的值整理成网页能直接用的值；存的是错类型（比如手改过配置文件）就退回默认值。"""
    kind, default = item["type"], item.get("default")
    if raw is MISSING:
        raw = default
    if kind == "bool":
        return raw if isinstance(raw, bool) else bool(default)
    if kind in ("int", "float"):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raw = default
        return int(raw) if kind == "int" else float(raw)
    if kind in ("string", "text"):
        return raw if isinstance(raw, str) else str(default or "")
    if isinstance(raw, (list, tuple)):  # list
        return [str(x) for x in raw if str(x)]
    return [str(x) for x in (default or [])]


def _dependencies(schema: dict, path: str, group: str, key: str) -> list:
    rules = []
    if group in ("fish", "fish_advanced") and path != "fish.backend":
        rules.append(("fish.backend", ("fish",)))
    if group == "emotion" and key != "enabled":
        rules.append(("emotion.enabled", (True,)))
    if group == "memory" and key != "enabled":
        rules.append(("memory.enabled", (True,)))
    rules.extend(_EXTRA_DEPENDS.get(path, ()))
    return [rule for rule in rules if rule[0] in _items(schema)]


def _needs_text(schema: dict, rules: list) -> str:
    """「开启「识别情绪标签」后生效」这类提示。"""
    index = _items(schema)
    parts = []
    for target, allowed in rules:
        _, _, item = index[target]
        title = _split_title(item.get("description"), target)[1]
        if item["type"] == "bool":
            parts.append(f"开启「{title}」后生效" if allowed[0] else f"关闭「{title}」后生效")
        else:
            names = "」或「".join(_short_label(item, value) for value in allowed)
            parts.append(f"「{title}」选「{names}」时生效")
    return "；".join(parts)


def _describe_item(schema: dict, config, gkey: str, key: str, item: dict) -> dict:
    path = f"{gkey}.{key}"
    kind = item["type"]
    raw = _stored(config, gkey, key)
    out = {
        "key": key,
        "path": path,
        "type": kind,
        "title": _split_title(item.get("description"), key)[1],
        "hint": str(item.get("hint") or ""),
        "default": _display_value(item, MISSING),
        "value": _display_value(item, raw),
    }
    if kind in ("int", "float"):
        lo, hi, step, slider = _NUMBER_RULES.get(path, _FALLBACK_RANGE)
        out.update(min=lo, max=hi, step=step, slider=slider)
    options = _option_list(item)
    if options:
        out["options"] = options
    if path in SECRET_PATHS:
        stored = raw if isinstance(raw, str) else ""
        out["secret"] = True
        out["value"] = ""
        out["is_set"] = bool(stored.strip())
        out["preview"] = ("••••" + stored.strip()[-4:]) if len(stored.strip()) >= 12 else "已设置"
    if path in _PLACEHOLDERS:
        out["placeholder"] = _PLACEHOLDERS[path]
    if path in _MONO_PATHS:
        out["mono"] = True
    rules = _dependencies(schema, path, gkey, key)
    if rules:
        out["depends"] = [{"path": target, "in": list(allowed)} for target, allowed in rules]
        out["needs"] = _needs_text(schema, rules)
    return out


def describe(schema: dict, config) -> list:
    """全部设置分组和设置项，带当前值；网页按它生成表单。"""
    groups = []
    for gkey, gdef in _groups(schema):
        icon, title = _split_title(gdef.get("description"), gkey)
        items = [
            _describe_item(schema, config, gkey, key, item)
            for key, item in (gdef.get("items") or {}).items()
            if isinstance(item, dict) and item.get("type") in _TYPES
        ]
        if items:
            groups.append({"key": gkey, "icon": icon, "title": title, "hint": str(gdef.get("hint") or ""), "items": items})
    return groups


# ---------- 校验 ----------
def _coerce_bool(value):
    if not isinstance(value, bool):
        raise SettingError("需要开或关")
    return value


def _number(value, path: str, kind: str):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingError("请填写数字")
    if not math.isfinite(value):
        raise SettingError("请填写有限的数字")
    lo, hi, _, _ = _NUMBER_RULES.get(path, _FALLBACK_RANGE)
    if kind == "int":
        if isinstance(value, float):
            if not value.is_integer():
                raise SettingError("需要整数")
            value = int(value)
    else:
        value = float(value)
    if value < lo or value > hi:
        shown = [f"{x:g}" for x in (lo, hi)]
        raise SettingError(f"范围是 {shown[0]} ~ {shown[1]}")
    return value


def _check_extra_body(text: str, warnings: list) -> None:
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise SettingError(f"不是有效的 JSON：{exc}") from None
    if not isinstance(parsed, dict):
        raise SettingError('必须是 JSON 对象，例如 {"latency": "balanced"}')
    if "text" in parsed:
        warnings.append("「额外请求参数」里的 text 会被忽略：朗读的文字只能由插件决定")


def _coerce_string(value, path: str, item: dict, warnings: list, title: str):
    if not isinstance(value, str):
        raise SettingError("需要文字")
    text = value.strip()
    kind, default = item["type"], str(item.get("default") or "")
    options = _option_list(item)
    if options:
        if text not in {option["value"] for option in options}:
            raise SettingError("没有这个选项")
        return text
    limit = _MAX_TEXT if kind == "text" else _MAX_STRING
    if len(text) > limit:
        raise SettingError(f"太长了（最多 {limit} 个字）")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text and default:  # 留空 = 用默认内容（代码里也是这样处理的）
        warnings.append(f"「{title}」留空，已恢复为默认内容")
        return default
    if not text:
        return ""
    if path == "fish.api_base":
        if not _URL_PATTERN.fullmatch(text):
            raise SettingError("需要以 http:// 或 https:// 开头的地址")
    elif path == "fish.reference_id":
        try:
            return parse_voice_id(text)
        except VoiceLibraryError as exc:
            raise SettingError(str(exc)) from None
    elif path == "fish_advanced.extra_body":
        _check_extra_body(text, warnings)
    elif path == "memory.quote_note" and "{text}" not in text:
        warnings.append("「引用语音时补充的说明」里没有 {text}，大模型看不到语音的内容")
    elif path == "memory.recent_note" and "{list}" not in text:
        warnings.append("「最近语音的说明」里没有 {list}，大模型看不到语音的内容")
    return text


def _check_patterns(patterns: list, warnings: list) -> None:
    bad = []
    for index, pattern in enumerate(patterns, 1):
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            bad.append(f"第 {index} 条不是有效的正则：{exc}")
            continue
        if compiled.search("[happy]"):
            warnings.append(f"过滤规则 {pattern!r} 会匹配 [happy] 这类方括号，情绪标签可能在被识别前就被删掉")
        if not compiled.sub("", _REGEX_SAMPLE).strip():
            warnings.append(f"过滤规则 {pattern!r} 会把整句话都删掉，之后所有回复都没有东西可朗读")
    if bad:
        raise SettingError("；".join(bad))


def _coerce_list(value, path: str):
    if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) for x in value):
        raise SettingError("需要一组文字")
    limit = _ITEM_MAX.get(path, _DEFAULT_ITEM_MAX)
    cleaned = []
    for entry in value:
        text = entry.strip()
        if path == "emotion.extra_tags":
            text = text.strip("[] ").strip()  # 「不带方括号」：写成 [laughing] 也认
        if text:
            cleaned.append(text)
    cleaned = list(dict.fromkeys(cleaned))  # 去重，保持顺序
    if len(cleaned) > _MAX_LIST:
        raise SettingError(f"最多 {_MAX_LIST} 项")
    for text in cleaned:
        if len(text) > limit:
            raise SettingError(f"每项最多 {limit} 个字：{text[:12]}…")
    return cleaned


def _coerce(path: str, item: dict, raw, warnings: list):
    kind = item["type"]
    title = _split_title(item.get("description"), path)[1]
    if kind == "bool":
        return _coerce_bool(raw)
    if kind in ("int", "float"):
        return _number(raw, path, kind)
    if kind in ("string", "text"):
        return _coerce_string(raw, path, item, warnings, title)
    value = _coerce_list(raw, path)
    if path == "reading.custom_filter_patterns":
        _check_patterns(value, warnings)
    return value


def _coerce_secret(raw) -> str:
    """密钥：只允许可见的 ASCII 字符，不能有空格（它会原样放进 HTTP 请求头）。"""
    if not isinstance(raw, str):
        raise SettingError("需要文字")
    text = raw.strip()
    if text and not _KEY_PATTERN.fullmatch(text):
        raise SettingError("API Key 应该是 8 位以上的字母数字，不能有空格或中文")
    return text


# ---------- 应用 ----------
def _same(old, new) -> bool:
    """值没变。True == 1 在 Python 里成立，但开关和数字不能混为一谈；30 和 30.0 则算一样。"""
    return old == new and isinstance(old, bool) == isinstance(new, bool)


def apply(schema: dict, config, values: dict) -> Result:
    """校验 values（{组: {项: 新值}}）并写进 config。有任何一项不合法就一项都不写。

    密钥项（fish.api_key）：传 None 表示不改，传空字符串表示清除。
    """
    result = Result()
    index = _items(schema)
    pending = {}  # 路径 -> (组, 项名, 新值)

    for gkey, group_values in (values or {}).items():
        if not isinstance(group_values, dict):
            result.errors[str(gkey)] = "格式不对"
            continue
        for key, raw in group_values.items():
            path = f"{gkey}.{key}"
            entry = index.get(path)
            if entry is None:
                result.errors[path] = "没有这个设置项"
                continue
            group, name, item = entry
            if path in SECRET_PATHS:
                if raw is None:
                    continue
                try:
                    new = _coerce_secret(raw)
                except SettingError as exc:
                    result.errors[path] = str(exc)
                    continue
            else:
                try:
                    new = _coerce(path, item, raw, result.warnings)
                except SettingError as exc:
                    result.errors[path] = str(exc)
                    continue
            old = _stored(config, group, name)
            if old is not MISSING and _same(old, new):
                continue  # 没变
            if old is MISSING and new == _display_value(item, MISSING):
                continue  # 配置文件里本来就没写，值又等于默认值
            pending[path] = (group, name, new, old)

    if result.errors:
        result.warnings.clear()
        return result

    for path, (group, name, new, old) in pending.items():
        section = config.get(group)
        if not isinstance(section, dict):
            section = {}
            config[group] = section
        section[name] = new
        result.changed[path] = (old, new)
    return result


def rollback(config, changed: dict) -> None:
    """写盘失败时，把 apply() 做的修改整批退回。"""
    for path, (old, _new) in changed.items():
        group, _, name = path.partition(".")
        section = config.get(group)
        if not isinstance(section, dict):
            continue
        if old is MISSING:
            section.pop(name, None)
        else:
            section[name] = old
