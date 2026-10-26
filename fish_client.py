"""Fish Audio TTS 客户端（POST /v1/tts）。

只用标准库（urllib，放在线程里跑），不依赖 AstrBot，也不需要额外安装任何包，方便单独测试。
支持把 api_base 指向自建的 Cloudflare Worker 反代：请求头（含 model）原样转发即可。
"""

import asyncio
import contextlib
import http.client
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE = "https://api.fish.audio"
_USER_AGENT = "astrbot_plugin_fish_tts/3.5.1"  # 有些 CDN 会拦截默认的 Python-urllib
_FORMATS = ("mp3", "wav", "opus")
_LATENCIES = ("normal", "balanced", "low")
_FILE_PREFIX = "fish_tts_"
_KEEP_SECONDS = 3600  # 语音文件保留多久；发出去之后就没用了
_CLEAN_EVERY = 600  # 最多每 10 分钟清理一次
_BODY_PREVIEW = 300
_MAX_WAIT = 120.0  # 配置为“不限时”时的兜底等待上限（秒）


class FishError(Exception):
    """Fish 接口返回了错误，或者没拿到可用的音频。"""

    def __init__(self, message: str, status: int = 0, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class FishConfig:
    api_key: str
    api_base: str = DEFAULT_BASE
    reference_id: str = ""
    model: str = "s2.1-pro"
    format: str = "mp3"
    temperature: float = 0.0  # 0 = 不传，用 Fish 的默认值
    top_p: float = 0.0  # 0 = 不传
    speed: float = 1.0  # 1.0 = 不传
    volume: float = 0.0  # 0 = 不传（单位 dB）
    latency: str = "normal"  # normal 不传
    normalize: bool = True  # True 不传
    extra_body: dict = field(default_factory=dict)  # 原样并入请求体（不能覆盖 text）
    retries: int = 1  # 网络错误 / 5xx 时额外重试几次
    timeout: float = 30.0  # 总预算（秒），0 = 不限时


def endpoint(api_base: str) -> str:
    """把 api_base 补全成 /v1/tts 的完整地址；Worker 地址、带或不带 /v1 都能用。"""
    base = (api_base or DEFAULT_BASE).strip().rstrip("/")
    if base.endswith("/v1/tts"):
        return base
    if base.endswith("/v1"):
        return base + "/tts"
    return base + "/v1/tts"


def build_payload(text: str, cfg: FishConfig) -> dict:
    """只发有必要的字段：没改过的参数不传，让 Fish 用自己的默认值。"""
    fmt = cfg.format if cfg.format in _FORMATS else "mp3"
    payload: dict = {"text": text, "format": fmt}
    if cfg.reference_id:
        payload["reference_id"] = cfg.reference_id
    if cfg.temperature > 0:
        payload["temperature"] = min(1.0, cfg.temperature)
    if cfg.top_p > 0:
        payload["top_p"] = min(1.0, cfg.top_p)
    prosody: dict = {}
    if abs(cfg.speed - 1.0) > 1e-6:
        prosody["speed"] = max(0.5, min(2.0, cfg.speed))
    if abs(cfg.volume) > 1e-6:
        prosody["volume"] = cfg.volume
    if prosody:
        payload["prosody"] = prosody
    if cfg.latency in _LATENCIES and cfg.latency != "normal":
        payload["latency"] = cfg.latency
    if not cfg.normalize:
        payload["normalize"] = False
    for key, value in (cfg.extra_body or {}).items():
        if key != "text":  # 正文只能由插件决定
            payload[key] = value
    return payload


def build_headers(cfg: FishConfig) -> dict:
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if cfg.model:
        headers["model"] = cfg.model
    return headers


def _post(url: str, headers: dict, body: bytes, timeout: float) -> tuple:
    """阻塞式 POST，在线程里跑。返回 (状态码, Content-Type, 响应体)。"""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", "") or "", resp.read()
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        except Exception:  # noqa: BLE001
            data = b""
        return exc.code, exc.headers.get("Content-Type", "") if exc.headers else "", data


def _preview(data: bytes) -> str:
    return data[:_BODY_PREVIEW].decode("utf-8", errors="replace").replace("\n", " ").strip()


class FishTTS:
    def __init__(self, out_dir: "Path | None"):
        self.out_dir = out_dir
        self._last_clean = 0.0

    async def synthesize(self, text: str, cfg: FishConfig) -> Path:
        """合成语音，返回音频文件路径。失败抛 FishError。"""
        if not cfg.api_key:
            raise FishError("没有配置 Fish API Key（fish_api_key）")
        if self.out_dir is None:
            raise FishError("无法确定语音文件的保存目录")

        payload = build_payload(text, cfg)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        data = await self._request(endpoint(cfg.api_base), build_headers(cfg), body, cfg)

        path = self.out_dir / f"{_FILE_PREFIX}{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.{payload['format']}"
        await asyncio.to_thread(self._save, path, data)
        return path

    async def _request(self, url: str, headers: dict, body: bytes, cfg: FishConfig) -> bytes:
        # timeout = 0 表示“不限时”，但底层连接不能真的无限等下去，这里兜底 120 秒
        deadline = time.monotonic() + (cfg.timeout if cfg.timeout > 0 else _MAX_WAIT)
        attempts = 1 + max(0, cfg.retries)
        last: "Exception | None" = None
        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                break
            try:
                return await self._once(url, headers, body, remaining)
            except FishError as exc:
                if not exc.retryable:
                    raise
                last = exc
            except (OSError, http.client.HTTPException) as exc:  # URLError、超时、连接错误都是 OSError
                last = FishError(f"网络错误：{exc}", retryable=True)
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5)
        raise last if isinstance(last, FishError) else FishError("请求超时或没有可用的重试时间")

    @staticmethod
    async def _once(url: str, headers: dict, body: bytes, timeout: float) -> bytes:
        status, ctype, data = await asyncio.to_thread(_post, url, headers, body, timeout)
        if status != 200:
            raise FishError(f"HTTP {status}：{_preview(data)}", status=status, retryable=status >= 500)
        if ctype.lower().startswith(("application/json", "text/")):
            # 200 但返回的是文字：多半是反代或网关吐出来的错误页，不是音频
            raise FishError(f"返回的不是音频（{ctype}）：{_preview(data)}", status=status)
        if not data:
            raise FishError("返回了空的音频", status=status, retryable=True)
        return data

    def _save(self, path: Path, data: bytes) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._cleanup()

    def _cleanup(self) -> None:
        """顺手清掉一小时前的旧语音文件。"""
        now = time.time()
        if now - self._last_clean < _CLEAN_EVERY:
            return
        self._last_clean = now
        with contextlib.suppress(OSError):
            for item in self.out_dir.glob(f"{_FILE_PREFIX}*"):
                with contextlib.suppress(OSError):
                    if now - item.stat().st_mtime > _KEEP_SECONDS:
                        item.unlink()
