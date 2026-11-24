# -*- coding: utf-8 -*-
import argparse
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

cur_dir = os.path.dirname(os.path.abspath(__file__))
if cur_dir not in sys.path:
    sys.path.insert(0, cur_dir)

from sport_client import SportClient792
from history.models import parse_history_records, history_record_status
from running.outbox import Outbox


def main():
    parser = argparse.ArgumentParser(description="运动世界校园 7.3.92 纯协议套件")
    parser.add_argument("-u", "--username", help="登录账号 (手机号)")
    parser.add_argument("-p", "--password", help="登录密码")
    parser.add_argument("-t", "--token", help="已有登录 Token")
    parser.add_argument("--uid", help="已有用户 UID")
    parser.add_argument("--unid", help="已有学校 UNID")
    parser.add_argument("--sync-time", action="store_true", help="执行服务端时间对齐")
    parser.add_argument("--rules", action="store_true", help="查询跑步规则与电子围栏")
    parser.add_argument("--checkpoints", action="store_true", help="单独查询官方计分跑点位，不上传成绩")
    parser.add_argument("--profile", action="store_true", help="查询学生个人档案")
    parser.add_argument("--history", action="store_true", help="查询最近历史记录与打卡日历")

    # 跑步相关选项
    parser.add_argument("--run", action="store_true", help="触发一键全时序跑步与结算")
    parser.add_argument("--mode", type=int, default=1, choices=[1, 6], help="运动类型: 1计分跑 6自由跑")
    parser.add_argument("--distance", type=int, default=3000, help="跑步目标距离 (米，默认 3000)")
    parser.add_argument("--time-sec", type=int, default=1060, help="跑步耗时 (秒，默认 1060)")
    parser.add_argument("--dry-run", action="store_true", help="只生成/校验轨迹，不上传 OBS、不写服务器")
    parser.add_argument("--campus", default="xuchang_280173698",
                        help="OSM 校园 id（对应 running/data/campus_<id>.json）")
    parser.add_argument("--export-track", help="把最终 GPS 点导出为 GeoJSON 的路径")
    parser.add_argument("--outbox-dir", help="提交落盘目录（默认 ~/.campusrun/outbox）")
    parser.add_argument("--no-verify", action="store_true", help="跳过上传后的 OBS 轨迹回读")
    parser.add_argument("--verify-upload", action="store_true", help="回读指定 rrid/object-key 的 OBS 轨迹")
    parser.add_argument("--rrid", type=int, help="回读或查询使用的成绩流水号")
    parser.add_argument("--object-key", help="OBS object key（回读时可替代 outbox 查找）")
    parser.add_argument("--retry-outbox", help="从指定 uuid 的 outbox 继续/重试提交")

    # 广告相关选项
    parser.add_argument("--watch-ad", action="store_true", help="模拟看广告与激励视频曝光链路")
    parser.add_argument("--ad-pos", default="4", choices=["1", "2", "4"], help="广告位: 1Banner 2弹窗 4激励视频(默认)")
    parser.add_argument("--ad-duration", type=int, default=15, help="广告观看耗时 (秒，默认 15)")

    args = parser.parse_args()
    if args.dry_run:
        args.run = True

    client = SportClient792(uid=args.uid, token=args.token, unid=args.unid)

    # 1. 登录认证
    if args.username and args.password:
        res = client.login(args.username, args.password, progress_callback=print)
        if not res.success:
            print(f"登录失败: code={res.error_code} 原因={res.message}")
            return
        print(f"登录成功: token={client.token} uid={client.uid} unid={client.unid}")

    # 2. 时钟对齐
    if args.sync_time or (not args.rules and not args.checkpoints and not args.profile
                          and not args.history and not args.run and not args.watch_ad
                          and not args.verify_upload and not args.retry_outbox):
        st = client.sync_server_time()
        print(f"时钟同步: 服务端时间={st.server_datetime} 偏斜={st.offset_seconds:+.3f}s")

    # 3. 个人档案
    if args.profile:
        if not client.token:
            print("个人档案: 失败 原因=缺少Token")
        else:
            prof = client.get_user_profile()
            if prof:
                print(f"个人档案: 姓名={prof.name} 性别={prof.sex} 学校={prof.campus_name} 学院={prof.depart} 班级={prof.grade_class} 学号={prof.student_id}")
            else:
                print("个人档案: 失败 原因=查询为空")

    # 4. 跑步规则与围栏
    if args.rules:
        if not client.token or not client.uid:
            print("跑步规则: 失败 原因=缺少Token或UID")
        else:
            rule = client.get_run_policy(run_mode=args.mode)
            fences = client.get_geo_fences()
            mode_name = "计分跑" if args.mode == 1 else "自由跑"
            if rule:
                print(f"跑步规则: 模式={mode_name} 单日上限={rule.day_goal}m 单次最低={rule.min_distance}m 配速={rule.speed_top}~{rule.speed_bottom}分/km 步频={rule.step_top}~{rule.step_bottom}步/分 围栏数={len(fences)}")
            else:
                print(f"跑步规则: 失败 模式={mode_name} 原因=规则查询为空")

    if args.checkpoints:
        if not client.token:
            print("官方点位: 失败 原因=缺少Token")
        else:
            fences = client.get_geo_fences()
            usable = [fence for fence in fences if fence.polygon_points]
            if not usable:
                print("官方点位: 失败 原因=未取得有效围栏锚点")
            else:
                points = client.policy.get_checkpoints_for_run(usable[0])
                if points:
                    print(f"官方点位: 成功 来源=pointsResModels 点数={len(points)}；未上传成绩")
                else:
                    print(f"官方点位: 失败 原因={client.policy.last_checkpoint_error}")

    # 5. 广告模拟
    if args.watch_ad:
        ad_res = client.watch_ad(
            ad_position=args.ad_pos,
            duration_sec=args.ad_duration,
            progress_callback=print
        )
        if ad_res.success:
            print(f"广告模拟: 成功 位置={args.ad_pos} 事件数={ad_res.events_count} 耗时={args.ad_duration}s")
        else:
            print(f"广告模拟: 失败 位置={args.ad_pos} 原因={ad_res.message}")

    # 6. 历史记录
    if args.history:
        if not client.token or not client.uid:
            print("历史记录: 失败 原因=缺少Token或UID")
        else:
            h_res = client.get_history_records(page_num=1, page_size=5)
            if isinstance(h_res, dict) and h_res.get("error") == 10000:
                try:
                    recs = parse_history_records(h_res.get("data"))
                except (TypeError, ValueError) as exc:
                    print(f"历史记录: 失败 原因=响应解析失败: {exc}")
                else:
                    print(f"历史记录: 条数={len(recs)}")
                    for r in recs:
                        status_str = history_record_status(r)
                        print(f"记录明细: rrid={r.get('rrid')} 里程={r.get('totalDis')}m 耗时={r.get('totalTime')}s 状态={status_str}")
            else:
                err_msg = h_res.get("message") if isinstance(h_res, dict) else "网络异常"
                print(f"历史记录: 失败 原因={err_msg}")

    # 7. 拟人跑步提交
    if args.run:
        if not client.token or not client.uid:
            print("跑步提交: 失败 原因=缺少Token或UID")
        else:
            mode_name = "计分跑" if args.mode == 1 else "自由跑"
            run_res = client.execute_run(
                target_distance_m=args.distance,
                target_time_sec=args.time_sec,
                sport_type=args.mode,
                progress_callback=print,
                campus=args.campus,
                dry_run=args.dry_run,
                export_track=args.export_track,
                outbox_dir=args.outbox_dir,
                verify_upload=not args.no_verify,
            )
            if run_res.success:
                if run_res.dry_run:
                    print(f"演练完成: 模式={mode_name} 里程={run_res.total_distance}m "
                          f"耗时={run_res.total_time}s 点数={run_res.track_points} "
                          f"轨迹导出={args.export_track or '-'}")
                else:
                    valid_str = "达标" if run_res.is_valid else "未达标"
                    if run_res.verified is True:
                        verify_str = "已验证"
                    elif run_res.verified is False:
                        verify_str = f"回读失败({run_res.verification_error})"
                    else:
                        verify_str = "未验证"
                    print(f"跑步提交: 成功 模式={mode_name} rrid={run_res.rrid} "
                          f"里程={run_res.total_distance}m 耗时={run_res.total_time}s "
                          f"状态={valid_str} 轨迹={verify_str} outbox={run_res.outbox_path}")
            else:
                print(f"跑步提交: 失败 模式={mode_name} code={run_res.error_code} 原因={run_res.message}")

    # 8. 上传轨迹回读
    if args.verify_upload:
        if not client.token or not client.uid:
            print("上传回读: 失败 原因=缺少Token或UID")
        else:
            object_key = args.object_key
            entry = None
            if args.rrid:
                try:
                    entry = Outbox(args.outbox_dir).find_by_rrid(args.rrid)
                except Exception:
                    entry = None
                if entry and not object_key:
                    object_key = entry.get("object_key")
            if not object_key:
                print("上传回读: 失败 原因=缺少 object_key（可用 --object-key 或由 outbox 按 rrid 找到）")
            else:
                check = client.running.verify_uploaded_track(
                    object_key, rrid=args.rrid,
                    uuid_str=(entry or {}).get("uuid"),
                    start_time_ms=((entry or {}).get("save_record") or {}).get("startTime"))
                if check.get("ok"):
                    print(f"上传回读: 成功 点数={check.get('points')} objectKey={object_key}")
                else:
                    print(f"上传回读: 失败 原因={check.get('error')}")

    # 9. outbox 重试
    if args.retry_outbox:
        if not client.token or not client.uid:
            print("outbox 重试: 失败 原因=缺少Token或UID")
        else:
            retry_res = client.running.submit_from_outbox(
                args.retry_outbox,
                outbox_dir=args.outbox_dir,
                verify_upload=not args.no_verify,
                progress_callback=print,
            )
            if retry_res.success:
                print(f"outbox 重试: 成功 rrid={retry_res.rrid} "
                      f"轨迹={'已验证' if retry_res.verified else '未验证'} "
                      f"outbox={retry_res.outbox_path}")
            else:
                print(f"outbox 重试: 失败 code={retry_res.error_code} 原因={retry_res.message}")


if __name__ == "__main__":
    main()
