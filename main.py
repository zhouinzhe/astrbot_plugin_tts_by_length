import asyncio
import random
import re

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

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


class TTSLengthGate(Star):
    """按回复长度决定是否转语音：短文本走 TTS，长文本直接发文字。

    在 on_decorating_result（消息发送前）判断长度，自己调用当前会话的 TTS Provider。
    使用时请把 AstrBot 自带 TTS 的「触发概率」设为 0，避免它对长文本再转一遍。
    朗读前会过滤链接、代码块、表情符号等不适合读出来的内容。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ---------- 配置读取 ----------
    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _float(self, key: str, default: float, lo=None, hi=None) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
        return value

    def _bool(self, key: str, default: bool) -> bool:
        return bool(self.config.get(key, default))

    # ---------- 文本处理 ----------
    def _prepare(self, raw: str) -> tuple[str, list[str], bool, bool]:
        """把原文整理成适合朗读的文本。

        返回 (朗读文本, 提取到的链接, 原文是否含代码块, 正文里是否有文本形式的 @)。
        长度判断也基于朗读文本，所以很长的链接不会让短回复变成"长文本"。
        """
        urls: list[str] = []
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
        if self._bool("filter_url", True):
            urls.extend(_URL.findall(text))
            text = _URL.sub("", text)
        else:
            urls = []

        # 先检测再过滤：检测结果交给上层决定要不要整条发文字
        has_at = bool(_AT_TEXT.search(text))
        if self._bool("strip_at_text", True):
            text = _AT_TEXT.sub("", text)

        if self._bool("cleanup_markdown", True):
            text = _MD_MARK.sub("", text)
        if self._bool("strip_emoji", True):
            text = _EMOJI.sub("", text)

        for pattern in self.config.get("custom_filter_patterns") or []:
            try:
                text = re.sub(pattern, "", text)
            except re.error as exc:
                logger.warning(f"[tts_by_length] 自定义过滤正则无效 {pattern!r}: {exc}")

        text = _SPACES.sub(" ", text).strip()
        return text, list(dict.fromkeys(urls)), has_code, has_at

    # ---------- TTS 调用 ----------
    async def _synthesize(self, event: AstrMessageEvent, text: str) -> str | None:
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

        timeout = self._float("tts_timeout", 30.0, lo=0.0)
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

    # ---------- 核心钩子 ----------
    # 优先级放低：让改写文本的插件（如表情插件）先处理，TTS 最后再判断
    @filter.on_decorating_result(priority=-100)
    async def gate_tts(self, event: AstrMessageEvent):
        if not self._bool("enabled", True):
            return
        if event.get_platform_name() in (self.config.get("skip_platforms") or []):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        if self._bool("only_llm_result", True):
            is_llm = getattr(result, "is_llm_result", None)
            if callable(is_llm) and not is_llm():
                return

        # 已经有语音段（别的插件或内置 TTS 处理过）就不再处理
        if any(isinstance(c, Comp.Record) for c in result.chain):
            return

        # 消息链里带 @ 组件：语音没法 @ 人，整条发文字
        skip_at = self._bool("skip_if_at", True)
        if skip_at and any(isinstance(c, _AT_TYPES) for c in result.chain):
            logger.debug("[tts_by_length] 消息链含 @ 组件，直接发文字")
            return

        raw = "".join(
            (getattr(c, "text", "") or "") for c in result.chain if isinstance(c, Comp.Plain)
        )
        spoken, urls, has_code, has_at = self._prepare(raw)

        if skip_at and has_at:
            logger.debug("[tts_by_length] 正文含 @ 文本，直接发文字")
            return

        if has_code and self._bool("skip_if_code", True):
            logger.debug("[tts_by_length] 含代码块，直接发文字")
            return

        url_action = str(self.config.get("url_action", "append_links"))
        if urls and url_action == "send_text":
            logger.debug("[tts_by_length] 含链接且 url_action=send_text，直接发文字")
            return

        if not _SPEAKABLE.search(spoken):
            return

        length = len(spoken)
        max_chars = self._int("max_chars", 80)
        min_chars = self._int("min_chars", 1)

        if max_chars > 0 and length > max_chars:
            logger.debug(f"[tts_by_length] {length} > {max_chars} 字，直接发文字")
            return
        if length < min_chars:
            return

        prob = self._float("trigger_probability", 1.0, lo=0.0, hi=1.0)
        if prob < 1.0 and random.random() > prob:
            return

        path = await self._synthesize(event, spoken)
        if not path:
            return

        try:
            voice = Comp.Record.fromFileSystem(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[tts_by_length] 构造语音消息失败: {exc}")
            return

        keep_text = self._bool("keep_text", False)
        # 引用 / @ 留在最前面（保持原有顺序），然后是语音，其余组件（如图片）跟在后面
        head = [c for c in result.chain if isinstance(c, _HEAD_TYPES)]
        rest = [
            c
            for c in result.chain
            if not isinstance(c, (Comp.Plain, Comp.Record) + _HEAD_TYPES)
        ]
        new_chain = list(head)
        if keep_text:
            new_chain.append(Comp.Plain(text=raw, convert=False))
        new_chain.append(voice)
        if urls and not keep_text and url_action == "append_links":
            # 语音里不读链接，但把链接以文字形式跟在语音后面，避免用户拿不到
            new_chain.append(Comp.Plain(text="\n".join(urls), convert=False))
        new_chain.extend(rest)
        result.chain = new_chain

        limit = f"≤ {max_chars}" if max_chars > 0 else "（不限长）"
        logger.info(f"[tts_by_length] {length} 字 {limit}，已转为语音")
