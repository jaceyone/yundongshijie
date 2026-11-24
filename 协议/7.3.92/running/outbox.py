# -*- coding: utf-8 -*-
"""跑步提交 outbox：落盘、状态推进与按 rrid 找回。

上传链路在申请 OBS 地址之前先把完整报文落盘，避免 OBS 已写、结算响应丢失时
无法重试或无法回读。目录默认 ``~/.campusrun/outbox``，每个 uuid 一个 JSON 文件。
"""
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

STATUS_PENDING = "pending"
STATUS_OBS_UPLOADED = "obs_uploaded"
STATUS_SAVED = "saved"
STATUS_VERIFIED = "verified"
STATUS_FAILED = "failed"

VALID_STATUSES = (
    STATUS_PENDING,
    STATUS_OBS_UPLOADED,
    STATUS_SAVED,
    STATUS_VERIFIED,
    STATUS_FAILED,
)

_UUID_RE = re.compile(r"^[A-Za-z0-9_.-]{8,64}$")


def default_outbox_dir() -> str:
    return os.path.expanduser("~/.campusrun/outbox")


class OutboxError(Exception):
    """outbox 条目缺失或内容无效。"""


class Outbox:
    """一个目录即一个 outbox；写入使用临时文件 + 原子替换。"""

    def __init__(self, directory: Optional[str] = None):
        self.directory = Path(directory or default_outbox_dir())

    def path_for(self, uuid_str: str) -> Path:
        name = str(uuid_str)
        if not _UUID_RE.match(name):
            raise OutboxError(f"非法 uuid: {name!r}")
        return self.directory / f"{name}.json"

    def write_pending(self,
                      uuid_str: str,
                      save_record: Dict[str, Any],
                      obs_payload: Dict[str, Any],
                      object_key: str,
                      created_at_ms: Optional[int] = None) -> Path:
        path = self.path_for(uuid_str)
        entry = {
            "uuid": str(uuid_str),
            "object_key": str(object_key),
            "created_at_ms": int(created_at_ms if created_at_ms is not None
                                 else time.time() * 1000),
            "updated_at_ms": int(time.time() * 1000),
            "status": STATUS_PENDING,
            "rrid": None,
            "last_error": "",
            "save_record": save_record,
            "obs_payload": obs_payload,
        }
        self._atomic_write(path, entry)
        return path

    def load(self, uuid_str: str) -> Dict[str, Any]:
        path = self.path_for(uuid_str)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                entry = json.load(handle)
        except FileNotFoundError:
            raise OutboxError(f"outbox 条目不存在: {path}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise OutboxError(f"outbox 条目无法读取: {exc}") from exc
        if not isinstance(entry, dict):
            raise OutboxError("outbox 条目必须是对象")
        return entry

    def update(self, uuid_str: str, **fields: Any) -> Dict[str, Any]:
        entry = self.load(uuid_str)
        if "status" in fields and fields["status"] not in VALID_STATUSES:
            raise OutboxError(f"未知状态: {fields['status']!r}")
        entry.update(fields)
        entry["updated_at_ms"] = int(time.time() * 1000)
        self._atomic_write(self.path_for(uuid_str), entry)
        return entry

    def mark(self, uuid_str: str, status: str,
             rrid: Optional[Any] = None,
             error: str = "") -> Dict[str, Any]:
        fields: Dict[str, Any] = {"status": status, "last_error": str(error or "")}
        if rrid is not None:
            fields["rrid"] = rrid
        return self.update(uuid_str, **fields)

    def find_by_rrid(self, rrid: Any) -> Optional[Dict[str, Any]]:
        wanted = str(rrid)
        for entry in self.iter_entries():
            if str(entry.get("rrid")) == wanted:
                return entry
        return None

    def iter_entries(self) -> Iterable[Dict[str, Any]]:
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    entry = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(entry, dict):
                yield entry

    @staticmethod
    def _atomic_write(path: Path, entry: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                        prefix=path.name + ".",
                                        suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, str(path))
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass
