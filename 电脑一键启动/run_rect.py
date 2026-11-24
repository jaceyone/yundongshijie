# -*- coding: utf-8 -*-
"""校园跑 · 直角路线正式提交（拐角≈90°，有真实闭环路线）

    python run_rect.py --config config.json          # 韩泳泽：预演 + 提交，一次登录
    python run_rect.py --config config_b.json        # 刘梦娜
    python run_rect.py --config config.json --stop-after-preflight   # 只预演

流程（一次登录全做完，避免单设备保护 10121）：
  登录 → 存 token → 拉规则/围栏 → 取当天官方 5 点 → 现场铺直角闭环并校验
  → 通过则立刻用同一个客户端提交（execute_run 内部会再取一次点位，
  官方 5 点接口 5 分钟内允许 3 次）。

路线由 `rect_route.RectRouteBuilder` 生成：校园栅格上四邻域 A*（只走东西/南北）
+ 外圈矩环，折线简化后拐角全是 90°，长度取「≤ 目标距离」的最大装法。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import anchor_points as anchor_mod  # noqa: E402
import preset_route  # noqa: E402
import rect_route  # noqa: E402
import runner as base  # noqa: E402


def pick_anchor(start_index: int = -1, center_first: bool = True,
                prefer_id: str = ""):
    """返回按顺序轮转的锚点候选；center_first 时离校园中心近的排前面。"""
    try:
        pool = anchor_mod.load_pool()
        points = anchor_mod._valid_points(pool)
    except Exception:
        return []
    if not points:
        return []
    prepared = []
    for item in points:
        prepared.append({
            "anchor_gcj": (float(item["glat"]), float(item["glon"])),
            "anchor_id": str(item.get("id") or ""),
            "anchor_name": str(item.get("name") or ""),
        })
    if center_first:
        center = rect_route.campus_center()
        for item in prepared:
            item["center_gap_m"] = rect_route.distance(center, item["anchor_gcj"])
        prepared.sort(key=lambda item: item["center_gap_m"])
    wanted = str(prefer_id or "").strip().lower()
    if wanted:
        front = [item for item in prepared if item["anchor_id"].lower() == wanted]
        prepared = front + [item for item in prepared if item not in front]
    index = start_index if start_index >= 0 else 0
    return prepared[index:] + prepared[:index]


def make_provider(target_m: int, phone: str, log, out_dir: Path, route_ctx=None):
    state = {}
    ctx = route_ctx if route_ctx is not None else {}

    def save_artifacts(result):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)
        png = rect_route.render_preview(
            result, str(out_dir / ("run-rect-%s-%s.png" % (phone[-4:], stamp))),
            checkpoint_names=[str(item.get("name") or "")
                              for item in result.ordered_checkpoints])
        log("路线预览图: %s" % png)
        (out_dir / ("run-rect-%s-%s.json" % (phone[-4:], stamp))).write_text(
            json.dumps({"length_m": result.length_m, "corners": result.corners,
                        "legs": result.legs, "polyline": result.polyline,
                        "checkpoints": result.ordered_checkpoints},
                       ensure_ascii=False, indent=2), encoding="utf-8")

    def provider(context):
        raws = [dict(item) for item in context.raw_checkpoints]
        fence = ctx.get("fence")
        target = int(target_m)
        if ctx.get("preset"):
            hit = preset_route.try_preset(
                raws, int(getattr(context, "unid", 0) or 0),
                int(getattr(context, "area_id", -1) or -1), target,
                fence=fence, log=log)
            if hit is not None:
                polyline, ordered, route_id = hit
                state["preset"] = route_id
                state["length"] = rect_route.polyline_length([tuple(p) for p in polyline])
                return polyline, ordered
            log("预设路线不可用，改用围栏内直角路线")
        builder = rect_route.RectRouteBuilder(target_m=target, log=log, fence=fence)
        result = builder.build(raws, verify=True)
        log("直角路线: " + result.summary())
        log("分段: %s" % {key: round(value, 1) for key, value in result.legs.items()})
        log("拐角: %s" % result.corners)
        state["result"] = result
        try:
            save_artifacts(result)
        except Exception as exc:  # noqa: BLE001
            log("[警告] 路线预览保存失败: %s" % exc)
        return ([[float(lat), float(lon)] for lat, lon in result.polyline],
                result.ordered_checkpoints)

    return provider, state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="校园跑 · 直角路线提交")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--distance", type=int, default=0)
    parser.add_argument("--time", type=int, default=0)
    parser.add_argument("--stop-after-preflight", action="store_true",
                        help="只做到「取点 + 铺线 + 采样校验」，不提交")
    parser.add_argument("--no-preflight", action="store_true", help="跳过预演直接提交")
    parser.add_argument("--no-anchor", action="store_true", help="不用锚点池")
    parser.add_argument("--anchor-index", type=int, default=-1,
                        help="指定锚点池里的第几个（0 基）；默认按时间轮转")
    parser.add_argument("--auto-anchor", action="store_true",
                        help="预演时自动换锚点重试（最多 3 个，避开落在禁走格的打卡点）")
    parser.add_argument("--no-center-first", action="store_true",
                        help="不按校园中心排序锚点（默认离中心近的先用）")
    parser.add_argument("--preset", action="store_true",
                        help="优先用路线库里的预设路线（命中且不越围栏才用）")
    parser.add_argument("--allow-outside-fence", action="store_true",
                        help="允许路线越过学校官方围栏（默认严格限制在围栏内）")
    parser.add_argument("--prefer-anchor", default="",
                        help="优先使用的锚点 id（如 a5），用于贴近预设路线库的点位")
    parser.add_argument("--use-session", action="store_true",
                        help="复用 session*.json 里的 token，不再登录（避开 10121）")
    args = parser.parse_args(argv)

    base._patch_subprocess_no_window()
    config = base.load_config(Path(args.config))
    if args.distance:
        config["distance"] = int(args.distance)
    if args.time:
        config["time_sec"] = int(args.time)
    phone = str(config["username"])
    if not base.gate_allows(phone, config):
        print("账号 %s 不在本机白名单里（config.json 的 allowed_phones）" % phone)
        return 2

    if not os.environ.get("CAMPUSRUN_SKIP_COOLDOWN"):
        wait = base._remaining_cooldown_sec()
        if wait > 0:
            time.sleep(min(wait, 90))

    if str(base.PROTOCOL_HOME) not in sys.path:
        sys.path.insert(0, str(base.PROTOCOL_HOME))
    if config.get("node_path"):
        os.environ["NODE_EXECUTABLE"] = str(config["node_path"])
    if config.get("chromium_path"):
        os.environ["CHROMIUM_PATH"] = str(config["chromium_path"])

    base.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = base.LOG_DIR / ("rect-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(message: str) -> None:
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), message)
        try:
            log_file.write(line + "\n")
        except Exception:
            pass
        if sys.stdout is not None:
            print(line, flush=True)

    anchor = None
    anchor_kwargs = {}
    candidates = []
    if not args.no_anchor:
        candidates = pick_anchor(args.anchor_index,
                                 center_first=not args.no_center_first,
                                 prefer_id=args.prefer_anchor)
        if candidates:
            first = candidates[0]
            anchor = first["anchor_gcj"]
            anchor_kwargs = {k: first[k] for k in ("anchor_id", "anchor_name")}
            gap = first.get("center_gap_m")
            log("取点锚点: %s (%.6f, %.6f)%s" % (
                first["anchor_name"] or first["anchor_id"], anchor[0], anchor[1],
                "" if gap is None else "，距校园中心 %.0f m" % gap))

    device_id = base.resolve_device_id(config)
    route_ctx = {"fence": None, "preset": bool(args.preset)}
    provider, state = make_provider(int(config["distance"]), phone, log,
                                    base.LOG_DIR, route_ctx=route_ctx)

    try:
        from sport_client import SportClient792  # noqa: WPS433

        client = SportClient792(device_config={"DeviceId": device_id})
        log("设备 ID: %s" % device_id)
        if args.use_session:
            session_path = base.session_path(config)
            session = json.loads(session_path.read_text(encoding="utf-8"))
            if str(session.get("phone")) != phone:
                log("会话文件 %s 不属于本账号" % session_path.name)
                return 3
            client.set_credentials(session.get("uid"), session.get("token"),
                                   session.get("unid"))
            log("复用会话: uid=%s（未重新登录）" % session.get("uid"))
        else:
            log("登录中（自动极验，无需手动操作）...")
            login = client.login(phone, config["password"], progress_callback=log)
            if not login.success:
                log(base._friendly_login_error(getattr(login, "error_code", None),
                                               getattr(login, "message", "登录失败")))
                return 1
            log("登录成功: uid=%s unid=%s" % (client.uid, client.unid))
            try:
                import session_store  # noqa: WPS433
                saved = session_store.save(
                    uid=client.uid, token=getattr(client, "token", None), unid=client.unid,
                    phone=phone, name=getattr(getattr(login, "user", None), "name", "") or "",
                    campus=getattr(getattr(login, "user", None), "campus_name", "") or "",
                    path=session_store.path_for_phone(phone))
                log("会话已缓存（%s）" % Path(saved).name)
            except Exception as exc:  # noqa: BLE001
                log("[警告] 会话缓存失败: %s" % exc)

        rule = client.get_run_policy(run_mode=1)
        if rule is None:
            log("未取得学校规则，停止提交: %s" % (client.policy.last_policy_error or ""))
            return 1
        log("学校规则: 最低 %s m · 单日 %s m" % (rule.min_distance, rule.day_goal))

        fences = client.get_geo_fences()
        if not fences:
            log("未取到电子围栏，无法保证不跑出学校，停止")
            return 1
        if not args.allow_outside_fence:
            polygon = rect_route.fence_polygon_from_area(fences[0])
            route_ctx["fence"] = polygon or None
            if polygon:
                lats = [p[0] for p in polygon]
                lons = [p[1] for p in polygon]
                log("学校围栏: %d 点 · 纬度 %.5f~%.5f · 经度 %.5f~%.5f（路线限制在围栏内）"
                    % (len(polygon), min(lats), max(lats), min(lons), max(lons)))
            else:
                log("[警告] 围栏多边形为空，无法限制路线范围")
        else:
            log("[警告] 已允许路线越出围栏（--allow-outside-fence）")

        if not args.no_preflight:
            attempts = candidates[:3] if args.auto_anchor else candidates[:1]
            if not attempts:
                attempts = [{"anchor_gcj": None, "anchor_id": "", "anchor_name": ""}]
            pre = None
            raws = []
            pre_kind = ""
            for attempt_index, candidate in enumerate(attempts, 1):
                label = candidate.get("anchor_name") or candidate.get("anchor_id") or "围栏顶点"
                live = client.policy.get_checkpoints_for_run(
                    fences[0], uuid_str=str(uuid.uuid4()), rule=rule,
                    anchor_gcj=candidate.get("anchor_gcj"))
                if not live:
                    log("取点失败（锚点 %s）: %s" % (label,
                                                client.policy.last_checkpoint_error or "未知"))
                    continue
                trial_raws = [point.as_checkpoint() for point in live]
                if args.preset:
                    hit = preset_route.try_preset(
                        trial_raws, int(client.unid or 0),
                        int(getattr(client.policy, "last_checkpoint_area_id", -1) or -1),
                        int(config["distance"]), fence=route_ctx["fence"], log=log)
                    if hit is not None:
                        polyline, ordered, route_id = hit
                        pre = rect_route.RectRouteResult(
                            polyline=[tuple(p) for p in polyline],
                            ordered_checkpoints=ordered,
                            length_m=rect_route.polyline_length([tuple(p) for p in polyline]),
                            target_m=int(config["distance"]),
                            ring=None,
                            legs={},
                            corners=rect_route.right_angle_stats([tuple(p) for p in polyline]),
                        )
                        raws = trial_raws
                        pre_kind = "预设路线 %s" % route_id[:8]
                        anchor = candidate.get("anchor_gcj")
                        anchor_kwargs = {k: candidate[k] for k in ("anchor_id", "anchor_name")}
                        log("预演采用预设路线 %s（第 %d 次尝试）" % (route_id[:8], attempt_index))
                        break
                builder = rect_route.RectRouteBuilder(
                    target_m=int(config["distance"]), log=log,
                    fence=route_ctx["fence"])
                try:
                    trial = builder.build(trial_raws, verify=True)
                except rect_route.RectRouteError as exc:
                    log("锚点 %s 这组点位铺不出合规路线（第 %d 次尝试）: %s"
                        % (label, attempt_index, exc.message))
                    continue
                pre, raws = trial, trial_raws
                pre_kind = "直角路线"
                anchor = candidate.get("anchor_gcj")
                anchor_kwargs = {k: candidate[k] for k in ("anchor_id", "anchor_name")}
                log("采用锚点 %s（第 %d 次尝试）" % (label, attempt_index))
                break
            if pre is None:
                log("试过的锚点都铺不出合规路线；等 5 分钟后再试（取点接口 5 分钟限 3 次）")
                return 1
            log("今天官方点位 %d 个:" % len(raws))
            for index, item in enumerate(raws):
                log("  %d. %s (%.6f, %.6f)" % (
                    index + 1, item.get("name") or item.get("pointName") or "?",
                    float(item["glat"]), float(item["glon"])))
            log("预演（%s）: %s" % (pre_kind, pre.summary()))
            if pre.legs:
                log("预演分段: %s" % {k: round(v, 1) for k, v in pre.legs.items()})
            try:
                log("预演采样: %s" % rect_route.simulate(
                    pre.polyline, int(config["distance"]), int(config["time_sec"]),
                    pre.ordered_checkpoints))
            except Exception as exc:  # noqa: BLE001
                log("预演采样未通过: %s" % exc)
            try:
                rect_route.render_preview(
                    pre, str(base.LOG_DIR / ("rect-preflight-%s-%s.png"
                                             % (phone[-4:], time.strftime("%Y%m%d-%H%M%S")))),
                    checkpoint_names=[str(item.get("name") or "")
                                      for item in pre.ordered_checkpoints])
            except Exception as exc:  # noqa: BLE001
                log("[警告] 预演图保存失败: %s" % exc)
            if args.stop_after_preflight:
                log("已按要求停在预演阶段（未提交）")
                return 0

        log("提交中（轨迹 / OBS / 结算 / 回查）...")
        run_res = client.execute_run(
            target_distance_m=int(config["distance"]),
            target_time_sec=int(config["time_sec"]),
            sport_type=1,
            progress_callback=log,
            campus=config.get("campus") or "xuchang_280173698",
            dry_run=False,
            route_provider=provider,
            anchor_gcj=anchor,
            **anchor_kwargs,
        )
        if not run_res.success:
            log(base._friendly_run_error(getattr(run_res, "error_code", None),
                                         getattr(run_res, "message", "跑步提交失败")))
            return 1
        summary = "rrid=%s · %.0f m / %s s · %s" % (
            getattr(run_res, "rrid", None), float(getattr(run_res, "total_distance", 0) or 0),
            getattr(run_res, "total_time", 0),
            "达标" if getattr(run_res, "is_valid", False) else "未达标")
        log("提交成功: " + summary)
        route = state.get("result")
        if route is not None:
            log("路线: " + route.summary())
        elif state.get("preset"):
            log("路线: 预设路线 %s · %.1f m（围栏内）" % (
                str(state["preset"])[:8], float(state.get("length") or 0)))
        print(summary)
        return 0
    except Exception as exc:  # noqa: BLE001
        log("异常: " + traceback.format_exc())
        log(base._friendly_exception(exc))
        return 1
    finally:
        try:
            log_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
