import asyncio
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from .voice_memory import VoiceMemory
except ImportError:  # 不是以包的方式加载时的兜底
    from voice_memory import VoiceMemory

_PLUGIN_NAME = "astrbot_plugin_tts_by_length"

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]*)>?[^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]*)>?[^)]*\)")
# 只匹配 ASCII 的 URL 字符：URL 后面紧跟中文时不会把中文吞掉；末尾的标点（. , ) 等）也不会算进链接
_URL = re.compile(
    r"(?:https?://|ftp://|www\.)[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]*[A-Za-z0-9\-_~/#=&%]",
    re.IGNORECASE,
)
_URL_HEAD = re.compile(r"^(?:https?://|ftp://|www\.)", re.IGNORECASE)
_MD_MARK = re.compile(r"[*`~#>]+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+")
_SPACES = re.compile(r"\s+")
_SPEAKABLE = re.compile(r"\w")  # 至少有一个字母/数字/汉字，否则没东西可读

# 文本形式的 @：@123456（QQ 号）、AstrBot 的 [At:123456]、OneBot 的 [CQ:at,qq=123456]。
# 前面不能紧跟字母数字或 . / : 等，避免误伤邮箱（a@b.com）和链接（/@user）。
# 不匹配 "@昵称"：昵称没有固定边界，容易把 "@了@了" 这类正常句子误判成艾特。
_AT_TEXT = re.compile(
    r"\[At:[^\]]*\]"
    r"|\[CQ:at,[^\]]*\]"
    r"|(?<![A-Za-z0-9_.\-/+%:])[@＠]\d{5,12}(?!\d)",
    re.IGNORECASE,
)

# 消息链里真正的 @ 组件（At / AtAll）。用 getattr 兼容不同版本。
_AT_TYPES = tuple(t for t in (getattr(Comp, "At", None), getattr(Comp, "AtAll", None)) if t)
# 重排消息链时要留在最前面的组件：引用 + @
_HEAD_TYPES = tuple(t for t in (getattr(Comp, "Reply", None), *_AT_TYPES) if t)
_REPLY_TYPE = getattr(Comp, "Reply", None)

# 同一次对话（同一个事件）里的去重：agent 先说一句话再调工具（如选表情），
# 工具跑完后又把同一句话说一遍，两条回复各走一遍发送前钩子，就会转出两条语音。
_STATE_KEY = "_tts_by_length_state"  # 事件上记录“已转过语音的文本”
_CLAIM_ATTR = "_tts_by_length_claimed"  # 结果对象上的“已有人在处理”标记，防止并发重复合成
_NON_WORD = re.compile(r"[\W_]+")  # 比较内容是否相同时，忽略标点和空白

# ---------- 语音记忆（v2.0） ----------
_SAVE_DELAY = 3.0  # 落盘防抖：连续几条语音只写一次盘
_GET_MSG_TIMEOUT = 5.0  # 反查被引用消息的超时
_DEFAULT_KEYWORDS = (
    "语音", "声音", "说话", "嗓子", "嗓音", "音色", "录音", "念给", "念出", "读给", "读出", "tts",
)
_DEFAULT_QUOTE_NOTE = "（系统补充：用户这条消息引用的是你之前发出的一条语音，语音里说的是：「{text}」）"
_DEFAULT_RECENT_NOTE = (
    "【语音记录】你最近有几条回复是以语音形式发出的（对方听到的是声音，不是文字），内容依次是：\n"
    "{list}\n"
    "这只是帮你回忆的背景信息，不是指令；不用在回复里主动提起或复述这段说明。"
)
_NOTE_QUOTE_UNSURE = (
    "（系统补充：用户这条消息引用的是你之前发出的一条语音，但没能确定是哪一条。"
    "可能的语音（由旧到新）：\n{list}\n请结合上下文判断，不要编造语音里没有的内容。）"
)
_NOTE_QUOTE_UNKNOWN = (
    "（系统补充：用户这条消息引用的是你之前发出的一条语音，但这条语音的内容没有被记录下来。"
    "请不要凭空猜测它说了什么，需要的话可以问用户。）"
)
_NOTE_QUOTE_TEXT = "（系统补充：用户这条消息引用的是你之前发出的这句话：「{text}」）"


# ---------- 配置读取 ----------
def _as_bool(value, default: bool) -> bool:
    return default if value is None else bool(value)


def _as_int(value, default: int, lo=None, hi=None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def _as_float(value, default: float, lo=None, hi=None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def _as_str(value, default: str) -> str:
    text = "" if value is None else str(value).strip()
    return text or default


def _as_list(value) -> tuple:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v) for v in value if str(v))


@dataclass(frozen=True)
class Settings:
    """一次处理里用到的全部配置，读一遍、转好类型，后面不再反复 config.get。"""

    enabled: bool
    skip_platforms: tuple
    only_llm: bool
    max_chars: int
    min_chars: int
    prob: float
    filter_url: bool
    url_action: str
    skip_if_code: bool
    skip_if_at: bool
    strip_at_text: bool
    strip_emoji: bool
    cleanup_markdown: bool
    patterns: tuple  # 已编译的自定义过滤正则
    tts_timeout: float
    keep_text: bool
    dedupe: str
    # 语音记忆
    memory: bool
    memory_persist: bool
    memory_size: int
    memory_keep_hours: int
    inject_quote: bool
    match_tolerance: float
    recent_mode: str
    recent_count: int
    recent_minutes: int
    recent_keywords: tuple
    quote_note: str
    recent_note: str


@dataclass
class _Plan:
    """决定转语音之后的材料：原文、朗读文本、提取到的链接。"""

    raw: str
    spoken: str
    urls: list


@dataclass
class _Quoted:
    """通过 get_msg 反查到的被引用消息信息。"""

    sender: str
    ts: "float | None"
    voice: bool


# ---------- 文本处理 ----------
def _prepare(raw: str, cfg: Settings) -> tuple:
    """把原文整理成适合朗读的文本。

    返回 (朗读文本, 提取到的链接, 原文是否含代码块, 正文里是否有文本形式的 @)。
    长度判断也基于朗读文本，所以很长的链接不会让短回复变成"长文本"。
    """
    urls: list = []
    text = raw

    has_code = bool(_CODE_FENCE.search(text))
    text = _CODE_FENCE.sub(" ", text)

    # Markdown 图片 / 链接：图片整体丢弃，链接只读文字部分
    def _md_image(m: re.Match) -> str:
        if _URL_HEAD.match(m.group(1)):
            urls.append(m.group(1))
        return " "

    def _md_link(m: re.Match) -> str:
        if _URL_HEAD.match(m.group(2)):
            urls.append(m.group(2))
        return m.group(1)

    text = _MD_IMAGE.sub(_md_image, text)
    text = _MD_LINK.sub(_md_link, text)

    # 裸链接
    if cfg.filter_url:
        urls.extend(_URL.findall(text))
        text = _URL.sub("", text)
    else:
        urls = []

    # 先检测再过滤：检测结果交给上层决定要不要整条发文字
    has_at = bool(_AT_TEXT.search(text))
    if cfg.strip_at_text:
        text = _AT_TEXT.sub("", text)

    if cfg.cleanup_markdown:
        text = _MD_MARK.sub("", text)
    if cfg.strip_emoji:
        text = _EMOJI.sub("", text)

    for pattern in cfg.patterns:
        text = pattern.sub("", text)

    text = _SPACES.sub(" ", text).strip()
    return text, list(dict.fromkeys(urls)), has_code, has_at


# ---------- 引用 / 语音记忆的小工具 ----------
def _call_str(obj, name: str) -> str:
    fn = getattr(obj, name, None)
    if not callable(fn):
        return ""
    try:
        return str(fn() or "")
    except Exception:  # noqa: BLE001
        return ""


def _chat_key(event: AstrMessageEvent) -> str:
    """按「聊天」记语音：群聊用群号，私聊用对方 ID。

    不直接用 unified_msg_origin：开了「群聊会话隔离」后它会带上发言人，
    A 触发的语音被 B 引用时就查不到了。
    """
    platform = _call_str(event, "get_platform_id") or _call_str(event, "get_platform_name")
    group = _call_str(event, "get_group_id")
    if group:
        return f"{platform}:g:{group}"
    return f"{platform}:p:{_call_str(event, 'get_sender_id')}"


def _clean_id(value) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text in ("0", "None") else text


def _plain_text(chain) -> str:
    return "".join((getattr(c, "text", "") or "") for c in chain if isinstance(c, Comp.Plain)).strip()


def _first_reply(event: AstrMessageEvent):
    if _REPLY_TYPE is None:
        return None
    message = getattr(getattr(event, "message_obj", None), "message", None) or []
    for comp in message:
        if isinstance(comp, _REPLY_TYPE):
            return comp
    return None


def _shorten(text: str, limit: int = 300) -> str:
    text = _SPACES.sub(" ", text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _mentions(text: str, keywords: tuple) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in keywords)


def _ago(seconds: float) -> str:
    if seconds < 45:
        return "刚刚"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} 分钟前"
    if seconds < 86400:
        return f"{round(seconds / 3600)} 小时前"
    return f"{round(seconds / 86400)} 天前"


def _fmt_list(records, now: "float | None" = None) -> str:
    now = time.time() if now is None else now
    return "\n".join(f"- {_ago(now - r.ts)}：「{_shorten(r.text)}」" for r in records)


def _request_contains(req, snippet: str) -> bool:
    """框架有没有已经把这段内容带进本次请求（只看本轮的 prompt，不看历史）。"""
    key = _SPACES.sub("", snippet)[:24]
    if not key:
        return False
    texts = [getattr(req, "prompt", None), getattr(req, "system_prompt", None)]
    for part in getattr(req, "extra_user_content_parts", None) or []:
        texts.append(getattr(part, "text", None))
    return any(key in _SPACES.sub("", t) for t in texts if isinstance(t, str))


def _append_prompt(req, note: str) -> None:
    prompt = getattr(req, "prompt", None) or ""
    if note in prompt:
        return
    req.prompt = f"{prompt.rstrip()}\n\n{note}" if prompt.strip() else note


def _append_system(req, note: str) -> None:
    system = getattr(req, "system_prompt", None) or ""
    if note in system:
        return
    req.system_prompt = f"{system.rstrip()}\n\n{note}" if system.strip() else note


async def _get_msg(event: AstrMessageEvent, message_id: str) -> "_Quoted | None":
    """用 OneBot 的 get_msg 反查被引用消息：发送者、真实发送时间、是不是语音。

    不直接用 Reply 组件里的 time：适配器填的不一定是原消息的发送时间。
    """
    call = getattr(getattr(event, "bot", None), "call_action", None)
    if not callable(call):
        return None
    mid = int(message_id) if message_id.lstrip("-").isdigit() else message_id
    try:
        data = await asyncio.wait_for(call("get_msg", message_id=mid), _GET_MSG_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[tts_by_length] get_msg 失败: {exc}")
        return None
    if not isinstance(data, dict):
        return None
    sender = data.get("sender")
    uid = sender.get("user_id") if isinstance(sender, dict) else data.get("user_id")
    raw_ts = data.get("time")
    ts = float(raw_ts) if isinstance(raw_ts, (int, float)) and raw_ts > 0 else None
    message = data.get("message")
    if isinstance(message, list):
        voice = any(isinstance(s, dict) and s.get("type") == "record" for s in message)
    else:
        voice = "[CQ:record" in str(message or "")
    return _Quoted(sender=_clean_id(uid), ts=ts, voice=voice)


def _resolve_data_dir() -> Path:
    """插件数据目录：放在 data/plugin_data 下，更新、重装插件都不会丢。"""
    try:
        from astrbot.api.star import StarTools

        try:
            return Path(StarTools.get_data_dir())
        except Exception:  # noqa: BLE001
            return Path(StarTools.get_data_dir(_PLUGIN_NAME))
    except Exception:  # noqa: BLE001
        pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME
    except Exception:  # noqa: BLE001
        return Path("data") / "plugin_data" / _PLUGIN_NAME


class TTSLengthGate(Star):
    """按回复长度决定是否转语音：短文本走 TTS，长文本直接发文字。

    在 on_decorating_result（消息发送前）判断长度，自己调用当前会话的 TTS Provider。
    使用时请把 AstrBot 自带 TTS 的「触发概率」设为 0，避免它对长文本再转一遍。
    朗读前会过滤链接、代码块、表情符号等不适合读出来的内容。

    v2.0 起带「语音记忆」：记下每条语音说了什么，别人引用这条语音时告诉大模型。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._pat_raw: tuple = ()
        self._pat_compiled: tuple = ()
        self._bad_patterns: set = set()
        self._save_task = None

        path = None
        try:
            path = _resolve_data_dir() / "voice_memory.json"
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[tts_by_length] 无法确定数据目录，语音记忆不落盘: {exc}")
        self.memory = VoiceMemory(path)
        cfg = self._settings()
        if cfg.memory and cfg.memory_persist:
            try:
                count = self.memory.load()
                self.memory.prune(cfg.memory_size, cfg.memory_keep_hours * 3600)
                if count:
                    logger.info(f"[tts_by_length] 已读回 {count} 条语音记录")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[tts_by_length] 读取语音记录失败，从空白开始: {exc}")

    # ---------- 配置 ----------
    def _compiled_patterns(self, raws: tuple) -> tuple:
        if raws == self._pat_raw:
            return self._pat_compiled
        compiled = []
        for pattern in raws:
            try:
                compiled.append(re.compile(pattern))
            except re.error as exc:
                if pattern not in self._bad_patterns:  # 每条无效规则只提醒一次，不刷屏
                    self._bad_patterns.add(pattern)
                    logger.warning(f"[tts_by_length] 自定义过滤正则无效 {pattern!r}: {exc}")
        self._pat_raw, self._pat_compiled = raws, tuple(compiled)
        return self._pat_compiled

    def _settings(self) -> Settings:
        c = self.config
        keywords = c.get("recent_keywords")
        recent_mode = _as_str(c.get("recent_mode"), "keywords")
        return Settings(
            enabled=_as_bool(c.get("enabled"), True),
            skip_platforms=_as_list(c.get("skip_platforms")),
            only_llm=_as_bool(c.get("only_llm_result"), True),
            max_chars=_as_int(c.get("max_chars"), 80),
            min_chars=_as_int(c.get("min_chars"), 1),
            prob=_as_float(c.get("trigger_probability"), 1.0, lo=0.0, hi=1.0),
            filter_url=_as_bool(c.get("filter_url"), True),
            url_action=_as_str(c.get("url_action"), "append_links"),
            skip_if_code=_as_bool(c.get("skip_if_code"), True),
            skip_if_at=_as_bool(c.get("skip_if_at"), True),
            strip_at_text=_as_bool(c.get("strip_at_text"), True),
            strip_emoji=_as_bool(c.get("strip_emoji"), True),
            cleanup_markdown=_as_bool(c.get("cleanup_markdown"), True),
            patterns=self._compiled_patterns(_as_list(c.get("custom_filter_patterns"))),
            tts_timeout=_as_float(c.get("tts_timeout"), 30.0, lo=0.0),
            keep_text=_as_bool(c.get("keep_text"), False),
            dedupe=_as_str(c.get("dedupe_same_event"), "drop"),
            memory=_as_bool(c.get("voice_memory"), True),
            memory_persist=_as_bool(c.get("memory_persist"), True),
            memory_size=_as_int(c.get("memory_size"), 30, lo=1, hi=200),
            memory_keep_hours=_as_int(c.get("memory_keep_hours"), 168, lo=1, hi=24 * 365),
            inject_quote=_as_bool(c.get("inject_quote"), True),
            match_tolerance=_as_float(c.get("match_tolerance"), 30.0, lo=3.0, hi=600.0),
            recent_mode=recent_mode if recent_mode in ("keywords", "always", "off") else "keywords",
            recent_count=_as_int(c.get("recent_count"), 3, lo=1, hi=10),
            recent_minutes=_as_int(c.get("recent_minutes"), 30, lo=1, hi=1440),
            recent_keywords=_as_list(keywords) if keywords is not None else _DEFAULT_KEYWORDS,
            quote_note=_as_str(c.get("quote_note_template"), _DEFAULT_QUOTE_NOTE),
            recent_note=_as_str(c.get("recent_note_template"), _DEFAULT_RECENT_NOTE),
        )

    # ---------- TTS 调用 ----------
    async def _synthesize(self, event: AstrMessageEvent, text: str, timeout: float) -> "str | None":
        umo = event.unified_msg_origin
        try:
            getter = getattr(self.context, "get_using_tts_provider_async", None)
            if getter is not None:
                provider = await getter(umo=umo)
            else:  # 旧版本
                provider = self.context.get_using_tts_provider(umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[tts_by_length] 获取 TTS Provider 失败: {exc}")
            return None
        if provider is None:
            logger.debug("[tts_by_length] 当前会话没有可用的 TTS Provider，保持文字")
            return None

        try:
            call = provider.get_audio(text)
            path = await (asyncio.wait_for(call, timeout) if timeout > 0 else call)
            return path or None
        except asyncio.TimeoutError:
            logger.warning(f"[tts_by_length] TTS 超过 {timeout:g} 秒未返回，保持文字")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[tts_by_length] TTS 合成失败，保持文字: {exc}")
            return None

    # ---------- 同一次对话去重 ----------
    @staticmethod
    def _voiced_texts(event: AstrMessageEvent) -> list:
        """这个事件里已经转过语音的文本（存在事件的 extra 上，随事件结束消失）。"""
        state = None
        getter = getattr(event, "get_extra", None)
        if callable(getter):
            try:
                state = getter(_STATE_KEY)
            except Exception:  # noqa: BLE001
                state = None
        if state is None:
            state = getattr(event, _STATE_KEY, None)
        if not isinstance(state, list):
            state = []
            setter = getattr(event, "set_extra", None)
            try:
                if callable(setter):
                    setter(_STATE_KEY, state)
                else:
                    setattr(event, _STATE_KEY, state)
            except Exception:  # noqa: BLE001
                pass
        return state

    @staticmethod
    def _drop_duplicate(event: AstrMessageEvent, result) -> None:
        """丢掉重复回复里的文字。表情图片等其他内容保留，没有内容可发时整条不发。"""
        kept = [c for c in result.chain if not isinstance(c, Comp.Plain)]
        if any(not isinstance(c, _HEAD_TYPES) for c in kept):
            result.chain = kept
            return
        try:
            clear = getattr(event, "clear_result", None)
            if callable(clear):
                clear()
            else:
                result.chain = []
        except Exception:  # noqa: BLE001
            result.chain = []

    # ---------- 判断：这条回复要不要转语音 ----------
    @staticmethod
    def _decide(chain, cfg: Settings) -> "_Plan | None":
        """返回 None 表示保持文字；否则返回朗读材料。"""
        # 已经有语音段（别的插件或内置 TTS 处理过）就不再处理
        if any(isinstance(c, Comp.Record) for c in chain):
            return None

        # 消息链里带 @ 组件：语音没法 @ 人，整条发文字
        if cfg.skip_if_at and any(isinstance(c, _AT_TYPES) for c in chain):
            logger.debug("[tts_by_length] 消息链含 @ 组件，直接发文字")
            return None

        raw = "".join((getattr(c, "text", "") or "") for c in chain if isinstance(c, Comp.Plain))
        spoken, urls, has_code, has_at = _prepare(raw, cfg)

        if cfg.skip_if_at and has_at:
            logger.debug("[tts_by_length] 正文含 @ 文本，直接发文字")
            return None
        if has_code and cfg.skip_if_code:
            logger.debug("[tts_by_length] 含代码块，直接发文字")
            return None
        if urls and cfg.url_action == "send_text":
            logger.debug("[tts_by_length] 含链接且 url_action=send_text，直接发文字")
            return None
        if not _SPEAKABLE.search(spoken):
            return None

        length = len(spoken)
        if cfg.max_chars > 0 and length > cfg.max_chars:
            logger.debug(f"[tts_by_length] {length} > {cfg.max_chars} 字，直接发文字")
            return None
        if length < cfg.min_chars:
            return None
        return _Plan(raw=raw, spoken=spoken, urls=urls)

    @staticmethod
    def _build_chain(chain, plan: _Plan, voice, cfg: Settings) -> list:
        """引用 / @ 留在最前面（保持原有顺序），然后是语音，其余组件（如图片）跟在后面。"""
        head = [c for c in chain if isinstance(c, _HEAD_TYPES)]
        rest = [c for c in chain if not isinstance(c, (Comp.Plain, Comp.Record) + _HEAD_TYPES)]
        new_chain = list(head)
        if cfg.keep_text:
            new_chain.append(Comp.Plain(text=plan.raw, convert=False))
        new_chain.append(voice)
        if plan.urls and not cfg.keep_text and cfg.url_action == "append_links":
            # 语音里不读链接，但把链接以文字形式跟在语音后面，避免用户拿不到
            new_chain.append(Comp.Plain(text="\n".join(plan.urls), convert=False))
        new_chain.extend(rest)
        return new_chain

    # ---------- 核心钩子：发送前判断并转语音 ----------
    # 优先级放低：让改写文本的插件（如表情插件）先处理，TTS 最后再判断
    @filter.on_decorating_result(priority=-100)
    async def gate_tts(self, event: AstrMessageEvent):
        cfg = self._settings()
        if not cfg.enabled:
            return
        if event.get_platform_name() in cfg.skip_platforms:
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        if cfg.only_llm:
            is_llm = getattr(result, "is_llm_result", None)
            if callable(is_llm) and not is_llm():
                return

        plan = self._decide(result.chain, cfg)
        if plan is None:
            return

        # 同一条结果已经有人在合成（并发重复调用）：交给先到的那个，这里不动它
        if getattr(result, _CLAIM_ATTR, False):
            return

        key = _NON_WORD.sub("", plan.spoken).lower()
        voiced = self._voiced_texts(event) if cfg.dedupe != "off" and key else None
        if voiced is not None and key in voiced:
            logger.info(f"[tts_by_length] 本轮已转过相同内容的语音，重复回复处理方式：{cfg.dedupe}")
            if cfg.dedupe != "keep_text":
                self._drop_duplicate(event, result)
            return

        if cfg.prob < 1.0 and random.random() > cfg.prob:
            return

        # 先占位再合成：合成要几秒，这期间进来的重复调用能看到标记
        try:
            setattr(result, _CLAIM_ATTR, True)
        except Exception:  # noqa: BLE001
            pass
        if voiced is not None:
            voiced.append(key)

        def _release() -> None:
            if voiced is not None and key in voiced:
                voiced.remove(key)

        path = await self._synthesize(event, plan.spoken, cfg.tts_timeout)
        if not path:
            _release()
            return

        try:
            voice = Comp.Record.fromFileSystem(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[tts_by_length] 构造语音消息失败: {exc}")
            _release()
            return

        result.chain = self._build_chain(result.chain, plan, voice, cfg)
        self._remember(event, plan.spoken, cfg)

        limit = f"≤ {cfg.max_chars}" if cfg.max_chars > 0 else "（不限长）"
        logger.info(f"[tts_by_length] {len(plan.spoken)} 字 {limit}，已转为语音")

    # ---------- 语音记忆：记账 ----------
    def _remember(self, event: AstrMessageEvent, spoken: str, cfg: Settings) -> None:
        """记下这条语音说了什么。任何出错都只记日志，不能影响语音本身发出去。"""
        if not cfg.memory:
            return
        try:
            chat = _chat_key(event)
            self.memory.add(
                chat, spoken, per_chat=cfg.memory_size, keep_seconds=cfg.memory_keep_hours * 3600
            )
            self._schedule_save(cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[tts_by_length] 记录语音失败（已忽略）: {exc}")

    def _schedule_save(self, cfg: Settings) -> None:
        if not cfg.memory_persist or self.memory.path is None:
            return
        task = self._save_task
        if task is not None and not task.done():
            return  # 已经有一个等着落盘的任务，它会把最新内容一起写
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._save_task = loop.create_task(self._save_later())

    async def _save_later(self) -> None:
        await asyncio.sleep(_SAVE_DELAY)
        while self.memory.dirty:
            snapshot = self.memory.snapshot()
            self.memory.dirty = False
            try:
                await asyncio.to_thread(VoiceMemory.write, self.memory.path, snapshot)
            except Exception as exc:  # noqa: BLE001
                self.memory.dirty = True
                logger.warning(f"[tts_by_length] 保存语音记录失败，下次再试: {exc}")
                return

    async def terminate(self):
        """插件卸载 / 重载时把还没落盘的记录写掉。"""
        task = self._save_task
        if task is not None and not task.done():
            task.cancel()
        try:
            if self.memory.dirty and self.memory.path is not None and self._settings().memory_persist:
                self.memory.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[tts_by_length] 退出时保存语音记录失败: {exc}")

    # ---------- 语音记忆：给大模型补上下文 ----------
    @filter.on_llm_request()
    async def inject_voice_context(self, event: AstrMessageEvent, req):
        cfg = self._settings()
        if not cfg.enabled or not cfg.memory:
            return
        if event.get_platform_name() in cfg.skip_platforms:
            return
        try:
            await self._inject(event, req, cfg)
        except Exception as exc:  # noqa: BLE001  语音记忆出任何问题都不能影响正常对话
            logger.warning(f"[tts_by_length] 补充语音上下文失败（已忽略）: {exc}")

    async def _inject(self, event: AstrMessageEvent, req, cfg: Settings) -> None:
        chat = _chat_key(event)

        note = await self._quote_note(event, req, chat, cfg) if cfg.inject_quote else None
        if note:
            _append_prompt(req, note)
            return  # 已经说清楚引用的是哪条，不再重复补「最近语音」

        if cfg.recent_mode == "off":
            return
        if cfg.recent_mode == "keywords" and not _mentions(
            getattr(event, "message_str", "") or "", cfg.recent_keywords
        ):
            return
        records = self.memory.recent(chat, cfg.recent_count, cfg.recent_minutes * 60)
        if records:
            _append_system(req, cfg.recent_note.replace("{list}", _fmt_list(records)))

    async def _quote_note(self, event: AstrMessageEvent, req, chat: str, cfg: Settings) -> "str | None":
        """用户引用了机器人自己的消息时，返回要补给大模型的说明；其他情况返回 None。"""
        reply = _first_reply(event)
        if reply is None:
            return None

        mid = _clean_id(getattr(reply, "id", ""))
        # 这个消息 ID 以前认出来过：直接命中，连接口都不用调
        if mid:
            hit = self.memory.find(chat, msg_id=mid)
            if hit.kind == "id":
                logger.info("[tts_by_length] 引用识别：按消息 ID 命中语音记录")
                return cfg.quote_note.replace("{text}", hit.record.text)

        self_id = _call_str(event, "get_self_id")
        sender = _clean_id(getattr(reply, "sender_id", ""))
        if sender and self_id and sender != self_id:
            return None  # 引用的不是机器人自己的消息

        chain = list(getattr(reply, "chain", None) or [])
        voice = any(isinstance(c, Comp.Record) for c in chain)
        known_self = bool(sender and self_id and sender == self_id)

        if chain and not voice:
            # 机器人自己的文字消息：框架一般已经把引用内容带进请求了，没带才补
            if not known_self:
                return None
            quoted = _plain_text(chain) or str(getattr(reply, "message_str", "") or "")
            if quoted and not _request_contains(req, quoted):
                return _NOTE_QUOTE_TEXT.replace("{text}", _shorten(quoted))
            return None

        # 语音，或者适配器没能解析出内容：反查被引用消息，拿到真实发送时间
        quoted_ts = None
        if event.get_platform_name() == "aiocqhttp":
            if mid:
                info = await _get_msg(event, mid)
                if info is not None:
                    if info.sender and self_id and info.sender != self_id:
                        return None
                    known_self = known_self or bool(info.sender and info.sender == self_id)
                    voice = voice or info.voice
                    quoted_ts = info.ts
        else:
            raw_time = getattr(reply, "time", 0)
            quoted_ts = float(raw_time) if isinstance(raw_time, (int, float)) and raw_time > 0 else None

        if not (known_self and voice):
            return None  # 确认不了这是机器人自己发的语音：什么都不加，保持原样

        match = self.memory.find(
            chat, msg_id=mid, ts=quoted_ts, tolerance=cfg.match_tolerance
        )
        if match.kind in ("id", "time"):
            if match.kind == "time" and mid:
                self.memory.bind(match.record, mid)  # 记住 ID，下次再引用就是精确命中
                self._schedule_save(cfg)
            logger.info(f"[tts_by_length] 引用识别：命中语音记录（{match.kind}）")
            return cfg.quote_note.replace("{text}", match.record.text)

        if match.kind == "ambiguous":
            candidates = sorted(match.candidates, key=lambda r: r.ts)[-cfg.recent_count:]
            logger.info("[tts_by_length] 引用识别：时间相近的语音不止一条，没有猜，列出候选")
            return _NOTE_QUOTE_UNSURE.replace("{list}", _fmt_list(candidates))

        if quoted_ts is None:
            records = self.memory.recent(chat, cfg.recent_count, float("inf"))
            if records:
                logger.info("[tts_by_length] 引用识别：拿不到被引用消息的时间，列出最近的语音")
                return _NOTE_QUOTE_UNSURE.replace("{list}", _fmt_list(records))
        logger.info("[tts_by_length] 引用识别：没有对应的语音记录（可能是升级前发的或已过期）")
        return _NOTE_QUOTE_UNKNOWN
