# -*- coding: utf-8 -*-
import argparse
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

cur_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(cur_dir)
for p in [cur_dir, parent_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from running.client import RunningClient
from running.osm import OsmRouteError, TrackPlanner, export_route_geojson
from running.outbox import Outbox


def parse_checkpoints(text):
    points = []
    for chunk in str(text or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(",")
        if len(parts) != 2:
            raise ValueError(f"点位格式应为 lat,lon：{chunk}")
        points.append((float(parts[0]), float(parts[1])))
    if len(points) < 2:
        raise ValueError("至少需要 2 个点位")
    return points


def main():
    parser = argparse.ArgumentParser(description="运动世界 7.3.92 跑步提交与 OBS 归档终端")
    parser.add_argument("-t", "--token", help="登录 Token")
    parser.add_argument("-u", "--uid", help="用户 UID")
    parser.add_argument("--unid", help="学校 UNID")
    parser.add_argument("-d", "--distance", type=int, default=3000, help="跑步目标距离 (米，默认 3000)")
    parser.add_argument("-s", "--seconds", type=int, default=1060, help="跑步总耗时 (秒，默认 1060)")
    parser.add_argument("-m", "--mode", type=int, default=1, choices=[1, 2, 3, 6], help="运动类型: 1计分跑 6自由跑")
    parser.add_argument("--campus", default="xuchang_280173698",
                        help="OSM 校园 id（对应 running/data/campus_<id>.json）")
    parser.add_argument("--plan-track", action="store_true",
                        help="离线规划避建筑轨迹：不联网、不需要 Token")
    parser.add_argument("--checkpoints",
                        help="--plan-track 的点位，格式 'lat,lon;lat,lon;...'（GCJ-02）")
    parser.add_argument("--dry-run", action="store_true", help="只生成/校验轨迹，不上传 OBS、不写服务器")
    parser.add_argument("--export-track", help="把最终 GPS 点导出为 GeoJSON 的路径")
    parser.add_argument("--outbox-dir", help="提交落盘目录（默认 ~/.campusrun/outbox）")
    parser.add_argument("--no-verify", action="store_true", help="跳过上传后的 OBS 轨迹回读")
    parser.add_argument("--verify-upload", action="store_true", help="回读指定 rrid/object-key 的 OBS 轨迹")
    parser.add_argument("--rrid", type=int, help="回读或查询使用的成绩流水号")
    parser.add_argument("--object-key", help="OBS object key（回读时可替代 outbox 查找）")
    parser.add_argument("--retry-outbox", help="从指定 uuid 的 outbox 继续/重试提交")
    args = parser.parse_args()

    if args.plan_track:
        try:
            checkpoints = parse_checkpoints(args.checkpoints)
        except ValueError as exc:
            parser.error(f"--checkpoints 无效: {exc}")
        try:
            planner = TrackPlanner.from_campus(args.campus)
            plan = planner.plan(checkpoints, args.distance, policy=1)
        except OsmRouteError as exc:
            print(f"轨迹规划: 失败 [{exc.code}] {exc.message}")
            return
        print(f"轨迹规划: 成功 校园={args.campus} 闭环={plan.loop_length_m:.1f}m "
              f"圈数={plan.laps} 点数={len(plan.polyline)} "
              f"吸附={plan.diagnostics.get('snap_distance_m')}")
        if args.export_track:
            target = export_route_geojson(plan.polyline, args.export_track, {
                "campus": args.campus,
                "loop_length_m": round(plan.loop_length_m, 1),
                "laps": plan.laps,
                "coordinate_system": "GCJ-02",
            })
            print(f"轨迹导出: 成功 路径={target}")
        return

    if not args.token or not args.uid:
        parser.error("非 --plan-track 模式需要 -t/--token 与 -u/--uid")

    client = RunningClient(uid=args.uid, token=args.token, unid=args.unid)

    if args.verify_upload:
        object_key = args.object_key
        if not object_key and args.rrid:
            entry = Outbox(args.outbox_dir).find_by_rrid(args.rrid)
            if entry:
                object_key = entry.get("object_key")
        if not object_key:
            print("上传回读: 失败 原因=缺少 object_key")
            return
        check = client.verify_uploaded_track(object_key, rrid=args.rrid)
        if check.get("ok"):
            print(f"上传回读: 成功 点数={check.get('points')} objectKey={object_key}")
        else:
            print(f"上传回读: 失败 原因={check.get('error')}")
        return

    if args.retry_outbox:
        res = client.submit_from_outbox(
            args.retry_outbox,
            outbox_dir=args.outbox_dir,
            verify_upload=not args.no_verify,
            progress_callback=print,
        )
        print(("outbox 重试: 成功" if res.success else "outbox 重试: 失败")
              + f" rrid={res.rrid} 原因={res.message}")
        return

    res = client.execute_full_run(
        target_distance_m=args.distance,
        target_time_sec=args.seconds,
        sport_type=args.mode,
        progress_callback=print,
        path_source="auto",
        campus=args.campus,
        dry_run=args.dry_run,
        export_track=args.export_track,
        outbox_dir=args.outbox_dir,
        verify_upload=not args.no_verify,
    )

    mode_name = "计分跑" if args.mode == 1 else "自由跑"
    if res.success:
        if res.dry_run:
            print(f"演练完成: 模式={mode_name} 里程={res.total_distance}m "
                  f"耗时={res.total_time}s 点数={res.track_points} "
                  f"轨迹导出={args.export_track or '-'}")
        else:
            valid_str = "达标" if res.is_valid else "未达标"
            verify_str = ("已验证" if res.verified is True
                          else f"回读失败({res.verification_error})"
                          if res.verified is False else "未验证")
            print(f"跑步提交: 成功 模式={mode_name} rrid={res.rrid} 里程={res.total_distance}m "
                  f"耗时={res.total_time}s 状态={valid_str} 轨迹={verify_str} "
                  f"outbox={res.outbox_path}")
    else:
        print(f"跑步提交: 失败 模式={mode_name} code={res.error_code} 原因={res.message}")


if __name__ == "__main__":
    main()
