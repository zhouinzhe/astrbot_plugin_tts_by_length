"""Fish 音色库：存多个「名称 → 音色 ID + 备注」，并记住当前用的是哪一个。

纯 Python，不依赖 AstrBot，方便单独测试。这里只管：
1. 名称、音色 ID、备注的整理和校验（音色 ID 可以直接贴音色页面地址）；
2. 增、删、改备注、切换，名称不分大小写，也可以用列表里的序号；
3. JSON 落盘（先写临时文件再替换）、读回（文件损坏时挪到 .bad 从空白开始）。
"""

import contextlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

MAX_VOICES = 50
_NAME_MAX = 24
_NOTE_MAX = 60
_ID_PATTERN = re.compile(r"[A-Za-z0-9_\-]{16,64}")  # Fish 的音色 ID 是 32 位字母数字，这里放宽一点，只拦明显的手误
_FILE_VERSION = 1


class VoiceLibraryError(ValueError):
    """输入有问题（名称 / 音色 ID / 备注不合法、找不到、库满了）。消息可以直接回给用户。"""


@dataclass
class Voice:
    id: str
    note: str = ""


def parse_voice_id(text) -> str:
    """取出音色 ID：可以直接给 ID，也可以贴整个音色页面地址（https://fish.audio/m/<ID>）。"""
    value = str(text or "").strip().strip("<>\"'")
    value = re.split(r"[?#]", value, maxsplit=1)[0].rstrip("/").rsplit("/", 1)[-1]
    if not _ID_PATTERN.fullmatch(value):
        raise VoiceLibraryError("音色 ID 看起来不对：应该是音色页面地址末尾那串 32 位字母数字，也可以直接贴整个地址")
    return value


def clean_name(text) -> str:
    name = str(text or "").strip()
    if not name:
        raise VoiceLibraryError("名称不能为空")
    if len(name) > _NAME_MAX:
        raise VoiceLibraryError(f"名称太长了（最多 {_NAME_MAX} 个字）")
    if any(ch.isspace() for ch in name):
        raise VoiceLibraryError("名称里不能有空格")
    if name.isdigit():
        raise VoiceLibraryError("名称不能是纯数字，否则会和列表序号混淆")
    return name


def clean_note(text) -> str:
    note = " ".join(str(text or "").split())  # 换行、连续空格都压成一个空格
    if len(note) > _NOTE_MAX:
        raise VoiceLibraryError(f"备注太长了（最多 {_NOTE_MAX} 个字）")
    return note


class VoiceLibrary:
    def __init__(self, path: "Path | None" = None):
        self.path = path
        self._voices: "dict[str, Voice]" = {}  # 名称 -> 音色（保持添加顺序）
        self._active = ""  # 当前使用的名称；空 = 没选

    # ---------- 查询 ----------
    @property
    def active_name(self) -> str:
        return self._active if self._active in self._voices else ""

    @property
    def active_id(self) -> str:
        """当前选中的音色 ID；没选就是空字符串（调用方再退回到设置页里的默认音色）。"""
        voice = self._voices.get(self._active)
        return voice.id if voice else ""

    def items(self) -> "list[tuple[str, Voice]]":
        return list(self._voices.items())

    def get(self, key) -> "tuple[str, Voice]":
        name = self._resolve(key)
        return name, self._voices[name]

    def __len__(self) -> int:
        return len(self._voices)

    def _resolve(self, key) -> str:
        """名称（不分大小写）或列表序号（从 1 开始） -> 库里的名称。"""
        key = str(key or "").strip()
        if key in self._voices:
            return key
        low = key.casefold()
        for name in self._voices:
            if name.casefold() == low:
                return name
        if key.isdigit() and 1 <= int(key) <= len(self._voices):
            return list(self._voices)[int(key) - 1]
        raise VoiceLibraryError(f"音色库里没有「{key}」，发送「列表」可以看到已保存的音色")

    # ---------- 修改（内存里改，调用方负责 save） ----------
    def add(self, name, voice, note=None) -> "tuple[str, bool]":
        """保存一个音色，同名（不分大小写）就覆盖。返回 (名称, 是不是覆盖)。

        note 不传（None）：新音色没有备注，覆盖时沿用原来的备注；传空字符串 = 清空备注。
        """
        name = clean_name(name)
        voice_id = parse_voice_id(voice)
        note = None if note is None else clean_note(note)
        try:
            name = self._resolve(name)  # 已有同名：沿用原来的写法，避免出现只差大小写的两条
        except VoiceLibraryError:
            if len(self._voices) >= MAX_VOICES:
                raise VoiceLibraryError(f"音色库最多存 {MAX_VOICES} 个，先删掉不用的") from None
        old = self._voices.get(name)
        self._voices[name] = Voice(voice_id, old.note if old and note is None else (note or ""))
        return name, old is not None

    def set_note(self, key, note) -> str:
        name = self._resolve(key)
        self._voices[name].note = clean_note(note)
        return name

    def use(self, key) -> str:
        name = self._resolve(key)
        self._active = name
        return name

    def clear_active(self) -> None:
        self._active = ""

    def remove(self, key) -> str:
        name = self._resolve(key)
        del self._voices[name]
        if self._active == name:
            self._active = ""
        return name

    # ---------- 落盘 ----------
    def save(self) -> None:
        if self.path is None:
            return
        voices = list(self._voices.items())  # 先拿一份快照：save 常在线程里跑，别在遍历字典的时候被别处改了
        data = {
            "version": _FILE_VERSION,
            "active": self.active_name,
            "voices": [{"name": name, "id": v.id, "note": v.note} for name, v in voices],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # 写到一半断电也不会弄坏旧文件

    def load(self) -> int:
        """读回已保存的音色，返回条数。文件损坏时挪到 .bad，从空白开始。"""
        if self.path is None or not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            voices = {
                clean_name(item["name"]): Voice(parse_voice_id(item["id"]), str(item.get("note") or "")[:_NOTE_MAX])
                for item in data["voices"]
            }
            active = str(data.get("active") or "")
        except Exception:  # noqa: BLE001
            with contextlib.suppress(OSError):
                os.replace(self.path, self.path.with_name(self.path.name + ".bad"))
            return 0
        self._voices = voices
        self._active = active if active in voices else ""
        return len(voices)
