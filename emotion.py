"""Fish Audio S2 情绪标签：识别、整理、去除。

纯 Python，不依赖 AstrBot，方便单独测试。S2 模型用方括号写情绪/语气，例如
``[happy] 今天天气真好``、``[sad][whispering] 我想你了``。这个模块负责三件事：

1. 认出哪些方括号是情绪标签（不误伤 ``[CQ:at,...]``、``[1]``、``arr[i]`` 之类）；
2. 送去合成前整理标签（全角括号换成半角、限制数量）；
3. 不转语音、发文字时把标签去掉（代码块和行内代码里的方括号不动）。
"""

import re
from dataclasses import dataclass

# ---------- Fish Audio 文档里列出的标签（小写） ----------
_BASIC = (
    "happy sad angry excited calm nervous confident surprised satisfied delighted "
    "scared worried upset frustrated depressed empathetic embarrassed disgusted moved "
    "proud relaxed grateful curious sarcastic"
).split()
_ADVANCED = (
    "disdainful unhappy anxious hysterical indifferent uncertain doubtful confused "
    "disappointed regretful guilty ashamed jealous envious hopeful optimistic pessimistic "
    "nostalgic lonely bored contemptuous sympathetic compassionate determined resigned"
).split()
_TONES = ("in a hurry tone", "shouting", "screaming", "whispering", "soft tone", "emphasis")
_EFFECTS = (
    "laughing", "chuckling", "sobbing", "crying loudly", "sighing", "groaning",
    "panting", "gasping", "yawning", "snoring", "clear throat",
)
_SPECIAL = ("audience laughing", "background laughter", "crowd laughing", "break", "long-break")
# 文档示例里出现过的其他写法
_SEEN = (
    "friendly", "narrator", "mysterious", "relieved", "enthusiastic", "encouraging",
    "urgent", "furious", "terrified", "ecstatic", "interested",
)
# 文档里的强度修饰：[slightly sad] [very excited] [extremely angry]
_MODIFIERS = ("slightly", "very", "extremely", "a bit", "somewhat")

KNOWN_TAGS = frozenset((*_BASIC, *_ADVANCED, *_TONES, *_EFFECTS, *_SPECIAL, *_SEEN))

MODES = ("any", "whitelist")

# 方括号（半角或全角）里 1~40 个字符，不含嵌套括号和换行。
# 前面不能紧跟字母/数字/下划线/右圆括号（排除 arr[i]、f(x)[0]），后面不能紧跟 "("（排除 Markdown 链接）。
# 前面紧跟 "]" 的情况在 _tags() 里单独判断：只有它前面是上一个标签时才算（[sad][whispering]），m[i][j] 不算。
_TAG = re.compile(r"(?<![A-Za-z0-9_\)])[\[［]([^\[\]［］\r\n]{1,40})[\]］](?!\()")
_HAS_LETTER = re.compile(r"[^\W\d_]")  # 至少有一个字母/汉字，排除 [1]、[2-3]、[...]
_CODE_SPLIT = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)


@dataclass(frozen=True)
class EmotionRules:
    mode: str = "any"  # any：任何短方括号都当标签（S2 支持自然语言描述）；whitelist：只认已知标签
    extra: frozenset = frozenset()  # 白名单模式下额外认可的标签（小写）
    max_tags: int = 6  # 一条语音最多保留几个标签；0 = 不限制


def make_rules(mode: str = "any", extra=(), max_tags: int = 6) -> EmotionRules:
    mode = mode if mode in MODES else "any"
    cleaned = set()
    for item in extra or ():
        tag = str(item).strip().strip("[]［］").strip().lower()
        if tag:
            cleaned.add(" ".join(tag.split()))
    try:
        limit = max(0, int(max_tags))
    except (TypeError, ValueError):
        limit = 6
    return EmotionRules(mode=mode, extra=frozenset(cleaned), max_tags=limit)


def is_tag(inner: str, rules: EmotionRules) -> bool:
    """方括号里的内容是不是情绪标签。"""
    text = " ".join((inner or "").split())
    if not text or not _HAS_LETTER.search(text):
        return False
    low = text.lower()
    if low.startswith(("cq:", "at:")) or "://" in low or low.startswith("www."):
        return False
    if rules.mode != "whitelist":
        return True
    known = KNOWN_TAGS | rules.extra  # 只算一次，别在循环里反复合并
    if low in known:
        return True
    return any(low.startswith(mod + " ") and low[len(mod) + 1 :] in known for mod in _MODIFIERS)


def _tags(text: str, rules: EmotionRules):
    prev_end = -1  # 上一个被认出的标签的结尾位置
    for m in _TAG.finditer(text):
        if m.start() > 0 and text[m.start() - 1] in "]］" and m.start() != prev_end:
            continue  # 紧跟在一个不是标签的 "]" 后面，比如 m[i][j]
        if is_tag(m.group(1), rules):
            prev_end = m.end()
            yield m


def count(text: str, rules: EmotionRules) -> int:
    return sum(1 for _ in _tags(text or "", rules))


def normalize(text: str, rules: EmotionRules) -> str:
    """送去合成的版本：标签保留，但统一成半角 [xxx]，超过 max_tags 的多余标签丢掉。"""
    text = text or ""
    out, pos, kept = [], 0, 0
    for m in _tags(text, rules):
        out.append(text[pos : m.start()])
        if rules.max_tags <= 0 or kept < rules.max_tags:
            kept += 1
            out.append("[" + " ".join(m.group(1).split()) + "]")
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def strip(text: str, rules: EmotionRules) -> str:
    """把标签去掉。标签位于行首 / 空白之后时，连它后面的一个空格一起去掉，避免留下多余空格。"""
    text = text or ""
    out, pos, last = [], 0, ""
    for m in _tags(text, rules):
        seg = text[pos : m.start()]
        if seg:
            out.append(seg)
            last = seg[-1]
        end = m.end()
        if (not last or last.isspace()) and end < len(text) and text[end] in " \t":
            end += 1
        pos = end
    out.append(text[pos:])
    return "".join(out)


def strip_outside_code(text: str, rules: EmotionRules) -> str:
    """发文字时用：代码块和行内代码里的方括号保持原样，其余地方去掉标签。"""
    text = text or ""
    if "[" not in text and "［" not in text:
        return text
    parts = _CODE_SPLIT.split(text)
    for i in range(0, len(parts), 2):  # 偶数位是普通文字，奇数位是代码
        parts[i] = strip(parts[i], rules)
    return "".join(parts)
