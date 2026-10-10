"""语音记忆：记下机器人以语音形式发出的内容，被人引用时能找回来。

纯 Python，不依赖 AstrBot，方便单独测试。这里只管三件事：
1. 按会话存最近的语音文本（内存 + JSON 落盘，重启不丢）；
2. 按「消息 ID」或「发送时间」找回某条语音；
3. 找不到或拿不准时老实说拿不准，绝不乱猜。
"""

import json
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

_WS = re.compile(r"\s+")
_MAX_TEXT = 500  # 单条语音文本最多存多少字，防止 max_chars=0 时存进超长文本
_FILE_VERSION = 1


@dataclass
class VoiceRecord:
    text: str  # 实际朗读出去的文本（已过滤链接、表情等）
    ts: float  # 本机记录时间（秒）
    msg_id: str = ""  # 平台消息 ID；第一次被引用并识别成功后才会填上


@dataclass
class Match:
    kind: str  # "id" 按消息 ID 命中 | "time" 按时间命中 | "ambiguous" 有多条拿不准 | "none" 没找到
    record: "VoiceRecord | None" = None
    candidates: list = field(default_factory=list)


class VoiceMemory:
    def __init__(self, path: "Path | None" = None, max_chats: int = 500):
        self.path = path
        self.max_chats = max_chats
        self.dirty = False
        self._chats: "OrderedDict[str, list[VoiceRecord]]" = OrderedDict()

    # ---------- 写入 ----------
    def add(
        self,
        chat: str,
        text: str,
        *,
        ts: "float | None" = None,
        per_chat: int = 30,
        keep_seconds: float = 7 * 86400,
    ) -> "VoiceRecord | None":
        text = _WS.sub(" ", text or "").strip()
        if not chat or not text:
            return None
        if len(text) > _MAX_TEXT:
            text = text[:_MAX_TEXT] + "…"
        now = time.time() if ts is None else ts
        rec = VoiceRecord(text=text, ts=now)
        records = self._chats.setdefault(chat, [])
        records.append(rec)
        self._chats.move_to_end(chat)
        self._trim(records, per_chat, keep_seconds, now)
        while len(self._chats) > self.max_chats:
            self._chats.popitem(last=False)
        self.dirty = True
        return rec

    def bind(self, rec: VoiceRecord, msg_id: str) -> None:
        """把平台消息 ID 记到这条语音上，以后再被引用就能按 ID 精确命中。"""
        msg_id = str(msg_id or "")
        if msg_id and rec.msg_id != msg_id:
            rec.msg_id = msg_id
            self.dirty = True

    @staticmethod
    def _trim(records: list, per_chat: int, keep_seconds: float, now: float) -> None:
        records[:] = [r for r in records if now - r.ts <= keep_seconds][-max(1, per_chat):]

    # ---------- 查询 ----------
    def recent(
        self, chat: str, limit: int, within_seconds: float, now: "float | None" = None
    ) -> list:
        """最近 within_seconds 秒内的语音，由旧到新，最多 limit 条。"""
        now = time.time() if now is None else now
        records = [r for r in self._chats.get(chat, ()) if now - r.ts <= within_seconds]
        return records[-max(1, limit):]

    def find(
        self,
        chat: str,
        *,
        msg_id: str = "",
        ts: "float | None" = None,
        tolerance: float = 30.0,
        min_gap: float = 3.0,
    ) -> Match:
        """找被引用的那条语音。

        1. 消息 ID 已经绑定过：精确命中；
        2. 否则按「被引用消息的发送时间」找最近的一条（只在还没绑定 ID 的记录里找）；
           如果最近的两条差距太小（< min_gap 秒），说明分不清，返回 ambiguous 而不是瞎猜。
        """
        records = self._chats.get(chat) or []
        msg_id = str(msg_id or "")
        if msg_id:
            for rec in reversed(records):
                if rec.msg_id == msg_id:
                    return Match("id", rec)
        if ts is None:
            return Match("none")
        near = sorted(
            ((abs(r.ts - ts), r) for r in records if not r.msg_id and abs(r.ts - ts) <= tolerance),
            key=lambda pair: pair[0],
        )
        if not near:
            return Match("none")
        if len(near) > 1 and near[1][0] - near[0][0] < min_gap:
            return Match("ambiguous", None, [r for _, r in near])
        return Match("time", near[0][1])

    # ---------- 落盘 ----------
    def snapshot(self) -> dict:
        return {
            "version": _FILE_VERSION,
            "chats": {
                chat: [{"t": r.text, "ts": r.ts, "id": r.msg_id} for r in records]
                for chat, records in self._chats.items()
                if records
            },
        }

    @staticmethod
    def write(path: Path, snapshot: dict) -> None:
        """先写临时文件再替换，写到一半断电也不会把旧文件弄坏。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def save(self) -> None:
        if self.path is None:
            return
        snapshot = self.snapshot()
        self.dirty = False
        self.write(self.path, snapshot)

    def load(self) -> int:
        """从磁盘读回记录，返回读到的条数。文件损坏时挪到 .bad，从空白开始。"""
        if self.path is None or not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            chats = data["chats"]
            loaded: "OrderedDict[str, list[VoiceRecord]]" = OrderedDict()
            for chat, items in chats.items():
                records = []
                for item in items:
                    text = _WS.sub(" ", str(item["t"])).strip()
                    if text:
                        records.append(
                            VoiceRecord(text=text, ts=float(item["ts"]), msg_id=str(item.get("id") or ""))
                        )
                if records:
                    loaded[str(chat)] = records
        except Exception:  # noqa: BLE001
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".bad"))
            except OSError:
                pass
            return 0
        self._chats = loaded
        return sum(len(v) for v in loaded.values())

    def prune(self, per_chat: int, keep_seconds: float) -> None:
        """按当前配置清理过期、超量的记录（启动时调用一次）。"""
        now = time.time()
        for chat in list(self._chats):
            self._trim(self._chats[chat], per_chat, keep_seconds, now)
            if not self._chats[chat]:
                del self._chats[chat]
