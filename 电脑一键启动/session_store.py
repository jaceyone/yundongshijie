# -*- coding: utf-8 -*-
"""校园跑 · 会话缓存（登录一次，后续读操作复用 token）

运动世界校园有单设备保护：同一账号在 1 小时内重复登录会被 10121 拒绝
（"此账号已在其他设备登录，原状态未退出"）。所以每次真实登录后把
uid / token / unid 落盘，后续只读查询（历史、档案、规则）直接复用，
不再重复登录。

文件：``电脑一键启动/session.json``（含 token，已 gitignore，不要外传）
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

APP_DIR = Path(__file__).resolve().parent
SESSION_PATH = APP_DIR / "session.json"


def load(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    target = Path(path or SESSION_PATH)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("token") or not data.get("uid"):
        return None
    return data


def save(uid: Any, token: Optional[str], unid: Any = None,
         phone: str = "", name: str = "", campus: str = "",
         path: Optional[Path] = None) -> Path:
    target = Path(path or SESSION_PATH)
    payload = {
        "uid": int(uid),
        "token": str(token or ""),
        "unid": int(unid) if unid is not None else None,
        "phone": phone,
        "name": name,
        "campus": campus,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return target


def age_seconds(session: Dict[str, Any]) -> float:
    raw = str(session.get("saved_at") or "")
    try:
        saved = time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return float("inf")
    return max(0.0, time.time() - saved)


def clear(path: Optional[Path] = None) -> None:
    target = Path(path or SESSION_PATH)
    try:
        target.unlink()
    except OSError:
        pass


def path_for_phone(phone: str, default: Optional[Path] = None) -> Path:
    """按手机号找回该账号已有的会话文件（session.json / session_b.json ...）。

    找不到同号会话时回落到 ``default``（默认 session.json），避免多账号互相覆盖。
    """
    wanted = str(phone or "").strip()
    if wanted:
        for candidate in sorted(APP_DIR.glob("session*.json")):
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and str(data.get("phone") or "").strip() == wanted:
                return candidate
    return Path(default or SESSION_PATH)
