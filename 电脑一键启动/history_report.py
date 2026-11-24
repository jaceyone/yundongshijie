# -*- coding: utf-8 -*-
"""校园跑 · 历史记录查看

优先复用 ``session.json`` 里的 token（只读查询不需要重新登录，也避开
「单设备保护 10121」）；token 失效时才用 config.json 的账号密码登录一次，
并把新 token 存回 session.json。

用法：
    python history_report.py                 # 最近 20 条 + 本月日历 + 学期进度
    python history_report.py --pages 3       # 取 3 页
    python history_report.py --wait 420      # 先等 7 分钟再登录（等解锁）
    python history_report.py --json out.json # 原始数据落盘
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import runner as base  # noqa: E402
import session_store  # noqa: E402

PROTOCOL_HOME = base.PROTOCOL_HOME
if str(PROTOCOL_HOME) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_HOME))


def _log(message: str) -> None:
    print(message, flush=True)


def make_client(config: Dict[str, Any], session_path: str = ""):
    from sport_client import SportClient792  # noqa: WPS433

    if config.get("node_path"):
        import os
        os.environ["NODE_EXECUTABLE"] = str(config["node_path"])
    if config.get("chromium_path"):
        import os
        os.environ["CHROMIUM_PATH"] = str(config["chromium_path"])

    session = session_store.load(Path(session_path) if session_path else None)
    if session:
        age = session_store.age_seconds(session)
        _log("[会话] 复用本地 token（%.0f 分钟前登录）uid=%s"
             % (age / 60.0, session.get("uid")))
        client = SportClient792(uid=session["uid"], token=session["token"],
                                unid=session.get("unid"))
        probe = client.get_history_records(page_num=1, page_size=1)
        if isinstance(probe, dict) and probe.get("error") in (10000, None):
            return client, session
        _log("[会话] token 已失效（%s），改用账号密码登录"
             % (probe.get("message") if isinstance(probe, dict) else probe))
        if not session_path:
            session_store.clear()

    client = SportClient792(device_config={"DeviceId": base.stable_device_id()})
    _log("[登录] %s ..." % config["username"])
    result = client.login(config["username"], config["password"], progress_callback=_log)
    if not result.success:
        raise SystemExit(base._friendly_login_error(
            getattr(result, "error_code", None), getattr(result, "message", "")))
    user = result.user
    path = session_store.save(
        uid=user.uid, token=user.token, unid=user.unid,
        phone=config["username"], name=user.name, campus=user.campus_name)
    _log("[登录] 成功 %s uid=%s，token 已缓存到 %s" % (user.name, user.uid, path.name))
    return client, {"uid": user.uid, "unid": user.unid, "name": user.name,
                    "campus": user.campus_name, "token": user.token}


def fmt_seconds(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "-"
    return "%d:%02d" % divmod(total, 60)


def fmt_time(ms: Any) -> str:
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return "-"
    if value > 10 ** 12:
        value //= 1000
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def render_records(rows: List[Dict[str, Any]]) -> None:
    from history.models import history_record_status  # noqa: WPS433

    if not rows:
        _log("（没有记录）")
        return
    header = "%-3s %-16s %8s %8s %10s %8s %-6s %-12s" % (
        "#", "开跑时间", "距离m", "时长", "配速", "步频", "判定", "rrid")
    _log(header)
    _log("-" * len(header))
    total_distance = 0.0
    valid_count = 0
    for index, row in enumerate(rows, 1):
        distance = float(row.get("totalDis") or row.get("validDis") or 0)
        seconds = row.get("totalTime") or row.get("validTime") or 0
        pace = "-"
        try:
            if distance > 0 and seconds > 0:
                minutes = float(seconds) / 60.0 / (float(distance) / 1000.0)
                pace = "%d'%02d\"" % (int(minutes), int(round((minutes % 1) * 60)))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        status = history_record_status(row)
        if status == "达标":
            valid_count += 1
        total_distance += distance
        _log("%-3d %-16s %8.0f %8s %10s %8s %-6s %-12s" % (
            index, fmt_time(row.get("startTime")), distance, fmt_seconds(seconds),
            pace, row.get("avgStepFreq") or "-", status, row.get("rrid") or "-"))
    _log("-" * len(header))
    _log("合计 %d 次 · 达标 %d 次 · 总里程 %.2f km"
         % (len(rows), valid_count, total_distance / 1000.0))


def render_summary(client) -> None:
    try:
        info = client.policy.get_semester_info(run_mode=1)
    except Exception as exc:  # noqa: BLE001
        _log("[学期] 查询失败: %s" % exc)
        return
    if info is None:
        return
    raw = getattr(info, "raw", {}) or {}
    name = raw.get("sname") or getattr(info, "semester_name", "") or "-"
    _log("")
    _log("[学期] %s  %s → %s" % (name, fmt_time(raw.get("startDate")),
                                 fmt_time(raw.get("endDate"))))
    _log("    semesterValid=%s（有效里程，单位由服务端定）  semesterGoal=%s  goalMethod=%s"
         % (raw.get("semesterValid"), raw.get("semesterGoal"), raw.get("goalMethod")))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="校园跑 · 历史记录")
    parser.add_argument("--pages", type=int, default=1, help="抓取页数（每页 20 条）")
    parser.add_argument("--json", help="把原始数据写到指定文件")
    parser.add_argument("--wait", type=int, default=0, help="先等待多少秒再查询")
    parser.add_argument("--session", default="",
                        help="指定会话文件（如 session_b.json），默认 session.json")
    args = parser.parse_args(argv)

    if args.wait > 0:
        _log("等待 %d 秒后开始（避开单设备保护锁）..." % args.wait)
        time.sleep(args.wait)

    config = base.load_config()
    client, session = make_client(config, args.session)

    rows: List[Dict[str, Any]] = []
    raw_pages: List[Any] = []
    for page in range(1, max(1, args.pages) + 1):
        res = client.get_history_records(page_num=page, page_size=20)
        raw_pages.append(res)
        if not isinstance(res, dict) or res.get("error") != 10000:
            _log("[历史] 第 %d 页查询失败: %s" % (page, res))
            break
        from history.models import parse_history_records  # noqa: WPS433
        batch = parse_history_records(res.get("data"))
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 20:
            break

    owner = session.get("name") or "-"
    _log("")
    _log("==== %s · uid %s · %s ====" % (owner, session.get("uid"),
                                         session.get("campus") or "-"))
    _log("")
    render_records(rows)
    render_summary(client)

    if args.json:
        Path(args.json).write_text(
            json.dumps({"session": {k: v for k, v in session.items() if k != "token"},
                        "pages": raw_pages}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        _log("原始数据: %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
