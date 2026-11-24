# -*- coding: utf-8 -*-
"""
校园跑 · 官方 5 点采集（只读）

目的
  - 在写路线编辑器之前，先测量“同一组 5 点”到底有多稳定：
    连续取 N 次点，统计唯一集合数、命中率、点间抖动、顺序变化。
  - 只调用取规则/围栏/点位的读接口，不生成轨迹、不上传成绩。

用法
  python route_sampler.py --runs 3 --yes
  python route_sampler.py --runs 10 --wait-limit --yes

注意
  - 会登录你的账号；官方有单设备保护，手机端可能被顶掉或返回 10121。
  - 官方 5 点接口 5 分钟内最多取 3 次（code 10603）；默认遇到限流就停止。
    需要更多样本时加 --wait-limit，程序会等 300 秒再继续。
  - 报告写到 电脑一键启动/logs/（已 gitignore），包含校园坐标，不要公开分享。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
PROTOCOL_HOME = REPO_ROOT / "协议" / "7.3.92"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(PROTOCOL_HOME) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_HOME))

import runner  # noqa: E402
from route_library import (  # noqa: E402
    DEFAULT_EPS_M,
    distance_m,
    match_point_sets,
    points_key,
)

RATE_LIMIT_CODE = "10603"


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(message: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def _rule_summary(rule) -> Dict[str, Any]:
    if rule is None:
        return {}
    return {
        "day_goal": getattr(rule, "day_goal", None),
        "min_distance": getattr(rule, "min_distance", None),
        "speed_top": getattr(rule, "speed_top", None),
        "speed_bottom": getattr(rule, "speed_bottom", None),
        "step_top": getattr(rule, "step_top", None),
        "step_bottom": getattr(rule, "step_bottom", None),
        "sequence_strength": getattr(rule, "sequence_strength", None),
        "policy": (getattr(rule, "raw", {}) or {}).get("policy"),
    }


def _point_brief(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for index, point in enumerate(points):
        result.append(
            {
                "index": index,
                "name": point.get("name") or point.get("pointName"),
                "position": point.get("position"),
                "isFixed": point.get("isFixed"),
                "glat": point.get("glat"),
                "glon": point.get("glon"),
                "lat": point.get("lat"),
                "lng": point.get("lng"),
            }
        )
    return result


def _analyse(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    keys: Dict[str, List[int]] = {}
    for record in records:
        keys.setdefault(record["points_key"], []).append(record["run_index"])

    hit_runs = 0
    seen = set()
    for record in records:
        if record["points_key"] in seen:
            hit_runs += 1
        seen.add(record["points_key"])

    pairwise: List[Dict[str, Any]] = []
    for right in range(len(records)):
        for left in range(right):
            first = records[left]
            second = records[right]
            try:
                result = match_point_sets(
                    first["points"], second["points"], eps_m=1e9
                )
            except Exception:
                result = None
            if result is None:
                continue
            pairwise.append(
                {
                    "a": first["run_index"],
                    "b": second["run_index"],
                    "same_set": result.max_distance_m <= DEFAULT_EPS_M,
                    "max_distance_m": round(result.max_distance_m, 2),
                }
            )

    same_set_values = [
        item["max_distance_m"] for item in pairwise if item["same_set"]
    ]
    different_values = [
        item["max_distance_m"] for item in pairwise if not item["same_set"]
    ]

    orders = {}
    for record in records:
        order = tuple(
            (point.get("name"), point.get("position"))
            for point in record["points"]
        )
        orders.setdefault(order, 0)
        orders[order] += 1

    return {
        "runs_ok": len(records),
        "unique_sets": len(keys),
        "hit_runs": hit_runs,
        "hit_rate": (hit_runs / len(records)) if records else 0.0,
        "duplicate_sets": {
            key: indexes for key, indexes in keys.items() if len(indexes) > 1
        },
        "pairwise": {
            "pairs": len(pairwise),
            "same_set_pairs": len(same_set_values),
            "different_set_pairs": len(different_values),
            "same_set_max_distance_m": (
                round(max(same_set_values), 2) if same_set_values else None
            ),
            "same_set_avg_distance_m": (
                round(sum(same_set_values) / len(same_set_values), 2)
                if same_set_values
                else None
            ),
            "different_set_min_distance_m": (
                round(min(different_values), 2) if different_values else None
            ),
        },
        "order_variants": len(orders),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="校园跑 · 官方 5 点采集（只读）")
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="连续取点次数，默认 3（服务端 5 分钟内最多 3 次）",
    )
    parser.add_argument(
        "--interval", type=float, default=3.0, help="两次取点间隔秒数，默认 3"
    )
    parser.add_argument("--out", default="", help="报告路径，默认 logs/points-*.json")
    parser.add_argument("--check", action="store_true", help="先跑一遍环境自检")
    parser.add_argument(
        "--wait-limit",
        action="store_true",
        help="遇到 10603 时限流等待 300 秒后继续，而不是停止",
    )
    parser.add_argument("--yes", action="store_true", help="跳过登录确认")
    args = parser.parse_args(argv)

    if args.runs <= 0:
        print("--runs 必须大于 0")
        return 2
    if args.interval < 0:
        print("--interval 不能为负数")
        return 2

    if not args.yes:
        if not sys.stdin.isatty():
            print("非交互环境请加 --yes")
            return 2
        answer = input(
            "将登录账号并连续取点 %d 次（间隔 %.0f 秒）。\n"
            "官方有单设备保护，手机端可能被顶掉或返回 10121。继续？[y/N] "
            % (args.runs, args.interval)
        ).strip().lower()
        if answer not in ("y", "yes"):
            return 2

    try:
        config = runner.load_config()
    except runner.ConfigError as exc:
        print("配置错误:", exc)
        return 2

    if args.check:
        checks = runner.preflight(config)
        for check in checks:
            print(("[OK]  " if check.ok else "[FAIL]") + " %s: %s" % (check.name, check.detail))
        if not all(check.ok for check in checks):
            print("环境自检未通过，停止。")
            return 1

    if config.get("node_path"):
        os.environ["NODE_EXECUTABLE"] = str(config["node_path"])
    if config.get("chromium_path"):
        os.environ["CHROMIUM_PATH"] = str(config["chromium_path"])

    from sport_client import SportClient792

    _log("创建客户端...")
    client = SportClient792(device_config={"DeviceId": runner.stable_device_id()})

    _log("登录中（自动极验，可能需要约 10 秒）...")
    try:
        login = client.login(
            config["username"], config["password"], progress_callback=_log
        )
    except Exception as exc:
        print("登录异常: %s: %s" % (type(exc).__name__, exc))
        print("可先运行 python 环境自检.py 检查 Node / Playwright / Chromium。")
        return 1
    if not login.success:
        code = getattr(login, "error_code", None)
        message = getattr(login, "message", "登录失败")
        print("登录失败（code=%s）：%s" % (code, message))
        if str(code) == "10121":
            print("提示：账号单设备保护，请先在其它设备退出，或稍后再试。")
        return 1
    _log("登录成功: uid=%s unid=%s" % (client.uid, client.unid))

    try:
        rule = client.get_run_policy(run_mode=1)
    except Exception as exc:
        print("取学校规则失败: %s: %s" % (type(exc).__name__, exc))
        return 1
    if rule is None:
        print("未取得学校规则，停止。")
        return 1
    try:
        fences = client.get_geo_fences()
    except Exception as exc:
        print("取电子围栏失败: %s: %s" % (type(exc).__name__, exc))
        return 1
    if not fences:
        print("未取得电子围栏，停止。")
        return 1
    fence = fences[0]
    _log(
        "围栏: id=%s name=%s 规则: 最低=%sm 配速=%s~%s"
        % (
            fence.fence_id,
            fence.fence_name,
            rule.min_distance,
            rule.speed_top,
            rule.speed_bottom,
        )
    )

    records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    stopped_reason = ""
    for index in range(args.runs):
        run_uuid = str(uuid.uuid4())
        _log("第 %d/%d 次取点..." % (index + 1, args.runs))
        try:
            points = client.policy.get_checkpoints_for_run(
                fence, uuid_str=run_uuid, rule=rule
            )
        except Exception as exc:
            points = []
            message = "%s: %s" % (type(exc).__name__, exc)
        else:
            message = getattr(client.policy, "last_checkpoint_error", "") or ""

        area_id = int(getattr(client.policy, "last_checkpoint_area_id", -1) or -1)
        if not points:
            _log("失败: %s" % (message or "未返回点位"))
            errors.append(
                {
                    "run_index": index + 1,
                    "time": _now_text(),
                    "uuid": run_uuid,
                    "area_id": area_id,
                    "error": message or "未返回点位",
                }
            )
            if RATE_LIMIT_CODE in (message or ""):
                stopped_reason = "10603 五分钟内最多取点 3 次"
                _log("达到取点频率上限（5 分钟内最多 3 次）。")
                if args.wait_limit and index + 1 < args.runs:
                    _log("按 --wait-limit 等待 300 秒后继续...")
                    time.sleep(300)
                    continue
                break
            continue

        checkpoints = [point.as_checkpoint() for point in points]
        try:
            key = points_key(checkpoints, client.unid, area_id)
        except Exception as exc:
            key = ""
            _log("点位缺少 GCJ 坐标，key 计算失败: %s" % exc)

        records.append(
            {
                "run_index": index + 1,
                "time": _now_text(),
                "uuid": run_uuid,
                "area_id": area_id,
                "points_key": key,
                "points": checkpoints,
                "brief": _point_brief(checkpoints),
            }
        )
        _log(
            "成功: %d 点 key=%s… area=%s"
            % (len(checkpoints), key[:10] if key else "-", area_id)
        )

        if index + 1 < args.runs and args.interval:
            time.sleep(args.interval)

    analysis = _analyse(records)
    report = {
        "generated_at": _now_text(),
        "runs_requested": args.runs,
        "uid": client.uid,
        "unid": client.unid,
        "fence": {"id": fence.fence_id, "name": fence.fence_name},
        "rule": _rule_summary(rule),
        "analysis": analysis,
        "stopped_reason": stopped_reason,
        "errors": errors,
        "records": records,
    }

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = APP_DIR / out_path
    else:
        out_path = runner.LOG_DIR / ("points-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n===== 采集结果 =====")
    print("成功取点: %d / %d" % (len(records), args.runs))
    if stopped_reason:
        print("提前停止: %s" % stopped_reason)
    if errors:
        print("失败: %d 次" % len(errors))
    print("唯一 5 点集合: %d" % analysis["unique_sets"])
    print(
        "重复命中: %d 次（命中率 %.1f%%）"
        % (analysis["hit_runs"], analysis["hit_rate"] * 100)
    )
    pairwise = analysis["pairwise"]
    print(
        "两两比较: %d 对，同一集合 %d 对，不同集合 %d 对"
        % (pairwise["pairs"], pairwise["same_set_pairs"], pairwise["different_set_pairs"])
    )
    if pairwise["same_set_max_distance_m"] is not None:
        print(
            "同一集合的点位抖动: 最大 %.2f m，平均 %.2f m"
            % (
                pairwise["same_set_max_distance_m"],
                pairwise["same_set_avg_distance_m"],
            )
        )
    print("不同集合的最小点距: %s" % pairwise["different_set_min_distance_m"])
    print("顺序变化: %d 种" % analysis["order_variants"])
    print("报告:", out_path)
    print("====================")

    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main())
