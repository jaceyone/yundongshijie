# -*- coding: utf-8 -*-
"""校园跑 · 随机一键开跑（距离 / 配速 / 锚点 / 路线全随机）

点一下就跑「今天的一次」，四个维度都随机，尽量贴近真人：

- 距离：3000–4000 m（10 m 步进）随机，且不低于学校 ``minDistance``；
- 配速：在学校规则区间内随机，但收进 5'00"–8'00"/km 的人味区间，
  并留 2–4% 余量避开配速上下限（撞上限会被判「过快/过慢」）；
- 取点：锚点池（``anchor_points.json``）里随机 1 个 —— 不同锚点 → 服务端
  返回不同 5 点 → 不同路线；
- 路线：拿当次官方 5 点去路线库匹配，**命中多条时随机挑一条**，再随机决定
  正向 / 反向（反向时点位顺序一起翻，``position`` 由协议侧按返回顺序重建）；
- 兜底：路线库匹配不到时，改成 OSM 避建筑规划（日志会写明，不静默中止）。

约束：不改 ``协议/7.3.92``；日志、冷却、错误翻译、设备 ID 全部复用 ``runner.py``。
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import anchor_points as anchor_mod
import route_library as route_lib
import runner as base

APP_DIR = base.APP_DIR
PROTOCOL_HOME = base.PROTOCOL_HOME
LOG_DIR = base.LOG_DIR

# ---- 随机区间 -------------------------------------------------------------
DISTANCE_MIN = 3000
DISTANCE_MAX = 4000
DISTANCE_STEP = 10

# 配速（分钟/公里）的「人味」区间；最终还会和学校规则取交集
HUMAN_PACE_FAST = 5.0
HUMAN_PACE_SLOW = 8.0
PACE_MARGIN = 0.04          # 离学校配速上下限留 4%
REVERSE_PROBABILITY = 0.5   # 反向跑的概率
OSM_FALLBACK = True         # 路线库匹配不到时改走 OSM
MAX_ATTEMPTS = 3            # 轨迹采样未收敛时重抽的上限（每次换一组随机参数）

# 白名单统一维护在 runner.py 的 gate_allows()；config.json 的 allowed_phones 留空即不限制。


class RandomRunError(Exception):
    """随机开跑的本地校验失败。"""


@dataclass
class Plan:
    """一次随机出来的「今天这份」。"""

    distance_m: int
    time_sec: int
    pace_min_km: float
    anchor: Any = None
    route_source: str = ""
    route_id: str = ""
    reversed_direction: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def pace_text(self) -> str:
        whole = int(self.pace_min_km)
        seconds = int(round((self.pace_min_km - whole) * 60))
        if seconds == 60:
            whole, seconds = whole + 1, 0
        return "%d'%02d\"" % (whole, seconds)

    @property
    def summary(self) -> str:
        minutes, seconds = divmod(int(self.time_sec), 60)
        anchor_label = getattr(self.anchor, "label", "-") or "-"
        route_text = self.route_source or "待定"
        if self.route_id:
            route_text += " %s" % self.route_id[:8]
        if self.reversed_direction:
            route_text += " (反向)"
        return "距离 %d m · 时长 %d:%02d · 配速 %s/km · 锚点 %s · 路线 %s" % (
            self.distance_m, minutes, seconds, self.pace_text,
            anchor_label, route_text)


# ---- 随机抽取 -------------------------------------------------------------


def pace_window(pace_bounds: Tuple[float, float]) -> Tuple[float, float]:
    """把学校配速区间收成「人味」窗口（分钟/公里）。"""
    try:
        fast, slow = float(pace_bounds[0]), float(pace_bounds[1])
    except (TypeError, ValueError, IndexError):
        fast, slow = 4.0, 12.0
    if fast <= 0 or slow <= 0 or fast >= slow:
        fast, slow = 4.0, 12.0

    low = max(fast * (1 + PACE_MARGIN), min(HUMAN_PACE_FAST, slow * (1 - PACE_MARGIN)))
    high = min(slow * (1 - PACE_MARGIN), max(HUMAN_PACE_SLOW, fast * (1 + PACE_MARGIN)))
    if high - low < 0.15:  # 学校区间很窄时退回「留余量」的原区间
        low, high = fast * (1 + PACE_MARGIN / 2), slow * (1 - PACE_MARGIN / 2)
    if high <= low:
        low, high = fast, slow
    return low, high


def pick_distance(rng: random.Random, rule: Any) -> int:
    minimum = int(getattr(rule, "min_distance", 0) or 0)
    low = max(DISTANCE_MIN, minimum)
    high = max(low, DISTANCE_MAX)
    return rng.choice(list(range(low, high + 1, DISTANCE_STEP)))


def pick_time(rng: random.Random, distance_m: int, pace_bounds: Tuple[float, float],
              minimum_time_s: int = 0) -> Tuple[int, float]:
    low, high = pace_window(pace_bounds)
    pace = rng.uniform(low, high)
    seconds = int(round(distance_m / 1000.0 * pace * 60.0))
    if minimum_time_s and seconds < minimum_time_s:
        pace = minimum_time_s / 60.0 / (distance_m / 1000.0)
        seconds = int(round(distance_m / 1000.0 * pace * 60.0))
    return seconds, pace


def pick_anchor(rng: random.Random) -> Optional[Any]:
    """从锚点池随机取 1 个；池子不可用时返回 None（回退围栏顶点）。

    偏好（2026-11-22 用户要求）：尽量在校园中心取点。先按到校园中心的距离
    排序，只在最近的 2 个里随机，避免把路线甩到校园边缘。
    """
    try:
        pool = anchor_mod.load_pool()
        points = anchor_mod._valid_points(pool)
    except Exception:
        points = []
    if not points:
        return None
    try:
        import rect_route  # noqa: WPS433  延迟导入，避免启动时加载栅格

        center = rect_route.campus_center()
        points = sorted(
            points,
            key=lambda item: rect_route.distance(
                center, (float(item["glat"]), float(item["glon"]))))[:2]
    except Exception:
        pass
    chosen = rng.choice(points)
    return anchor_mod.SelectedAnchor(
        id=str(chosen.get("id", "")),
        name=str(chosen.get("name", "")),
        glat=float(chosen["glat"]),
        glon=float(chosen["glon"]),
        seed="random-run",
    )


def _osm_or_none(log: Callable[[str], None]):
    if not OSM_FALLBACK:
        return None
    try:
        if str(PROTOCOL_HOME) not in sys.path:
            sys.path.insert(0, str(PROTOCOL_HOME))
        from running.models import RouteDecision  # noqa: WPS433
    except Exception:
        return None
    log("路线来源: 改用 OSM 避建筑自动规划（复用本次官方点位，不重新取点）")
    return RouteDecision.osm()


def make_provider(plan: Plan, rng: random.Random,
                  log: Callable[[str], None]) -> Callable[[Any], Any]:
    """路线回调：当次官方 5 点 → 路线库随机一条 → 折返补齐 → 可选反向。"""

    def provider(context: Any):
        points = list(getattr(context, "raw_checkpoints", []) or [])
        target = int(plan.distance_m)
        unid = getattr(context, "unid", None)
        area_id = int(getattr(context, "area_id", -1) or -1)

        if not points:
            log("路线库: provider 没拿到当次官方点位")
            return _osm_or_none(log)

        try:
            library = route_lib.RouteLibrary()
            matches = library.find_by_points(points, unid, area_id, target)
        except Exception as exc:
            log("路线库: 读取失败 %s" % exc)
            matches = []

        usable = [m for m in matches
                  if 0 < float(m.entry.length_m) <= target + 0.001]
        if not usable:
            log("路线库: 本次 5 点没有可用预设路线（命中 %d 条，目标 %d m）"
                % (len(matches), target))
            plan.route_source = "osm"
            return _osm_or_none(log)

        pick = rng.choice(usable)
        try:
            filled = route_lib.expand_polyline_to_distance(
                pick.adjusted_polyline, target)
        except Exception as exc:
            log("路线库: 折返补齐失败 %s；改走 OSM" % exc)
            plan.route_source = "osm"
            return _osm_or_none(log)

        ordered = [dict(item) for item in pick.ordered_checkpoints]
        plan.reversed_direction = rng.random() < REVERSE_PROBABILITY
        if plan.reversed_direction:
            filled = list(reversed(filled))
            ordered = list(reversed(ordered))
            log("路线方向: 反向")
        else:
            log("路线方向: 正向")

        plan.route_source = "手动路线库"
        plan.route_id = str(pick.entry.route_id)
        plan.notes.append("route=%s" % plan.route_id[:8])
        log("路线库: 候选 %d 条，本次随机选中 %s 模板=%.1f m 折线=%d 点 目标=%d m"
            % (len(usable), plan.route_id[:8], float(pick.entry.length_m),
               len(filled), target))

        try:
            anchor_meta = plan.anchor.to_meta() if plan.anchor else None
            library.mark_used(pick.entry.route_id, target, anchor_meta)
        except Exception:
            pass

        return ([[float(lat), float(lon)] for lat, lon in filled], ordered)

    return provider


# ---- 主流程 ---------------------------------------------------------------


def run_once(config: Dict[str, Any],
             dry_run: bool = False,
             progress_callback: Optional[Callable[[str], None]] = None,
             seed: Optional[int] = None) -> "base.RunResult":
    """登录 → 取规则 → 随机四项 → 执行。异常都收敛成 RunResult。"""
    rng = random.Random(seed) if seed is not None else random.Random()

    base._patch_subprocess_no_window()

    if not os.environ.get("CAMPUSRUN_SKIP_COOLDOWN"):
        wait = base._remaining_cooldown_sec()
        if wait > 0:
            return base.RunResult(
                success=False, dry_run=dry_run,
                message="请求过于频繁：距上次运行结束仅 %.0f 秒，请再等 %.0f 秒。"
                        % (base.MIN_RUN_INTERVAL_SEC - wait, wait),
                distance=int(config.get("distance", 0)),
                time_sec=int(config.get("time_sec", 0)),
            )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / ("random-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
    started = time.time()
    try:
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError as exc:
        return base.RunResult(
            success=False, dry_run=dry_run,
            message="无法写入日志: %s" % exc,
            distance=int(config.get("distance", 0)),
            time_sec=int(config.get("time_sec", 0)),
        )

    def log(message: str) -> None:
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), message)
        try:
            log_file.write(line + "\n")
        except Exception:
            pass
        if progress_callback is not None:
            try:
                progress_callback(line)
            except Exception:
                pass
        elif sys.stdout is not None:
            try:
                print(line, flush=True)
            except Exception:
                pass

    plan: Optional[Plan] = None

    def finish(success: bool, message: str, **kwargs) -> "base.RunResult":
        elapsed = time.time() - started
        log("结束（%s，%.1fs）: %s" % ("成功" if success else "失败", elapsed, message))
        try:
            log_file.close()
        except Exception:
            pass
        return base.RunResult(
            success=success, dry_run=dry_run, message=message,
            distance=int(plan.distance_m) if plan else int(config.get("distance", 0)),
            time_sec=int(plan.time_sec) if plan else int(config.get("time_sec", 0)),
            log_path=str(log_path), elapsed_sec=elapsed, **kwargs)

    try:
        phone = str(config.get("username") or "").strip()
        if not base.gate_allows(phone, config):
            return finish(
                False,
                "账号 %s 不在本机白名单里。"
                "要允许该账号，把手机号加进 config.json 的 \"allowed_phones\"，"
                "或留空该字段表示不限制。" % (phone or "（空）"))
        if str(PROTOCOL_HOME) not in sys.path:
            sys.path.insert(0, str(PROTOCOL_HOME))
        if config.get("node_path"):
            os.environ["NODE_EXECUTABLE"] = str(config["node_path"])
        if config.get("chromium_path"):
            os.environ["CHROMIUM_PATH"] = str(config["chromium_path"])

        from sport_client import SportClient792  # noqa: WPS433

        campus = str(config.get("campus") or "xuchang_280173698")
        log("随机开跑：距离 %d–%d m / 配速随机 / 锚点随机 / 路线随机"
            % (DISTANCE_MIN, DISTANCE_MAX))
        device_id = base.resolve_device_id(config)
        client = SportClient792(device_config={"DeviceId": device_id})
        log("设备 ID: %s" % device_id)

        plan = Plan(distance_m=0, time_sec=0, pace_min_km=0.0,
                    anchor=pick_anchor(rng))
        if plan.anchor is not None:
            log("取点锚点: %s (%.6f, %.6f) 来源=锚点池随机"
                % (plan.anchor.label, plan.anchor.glat, plan.anchor.glon))
        else:
            log("取点锚点: 锚点池为空，回退围栏第一个顶点")

        log("登录中（自动极验，无需手动操作）...")
        login_res = client.login(config["username"], config["password"],
                                 progress_callback=log)
        if not login_res.success:
            code = getattr(login_res, "error_code", None)
            message = getattr(login_res, "message", "登录失败")
            return finish(False, base._friendly_login_error(code, message),
                          error_code=code)
        log("登录成功: uid=%s unid=%s" % (client.uid, client.unid))
        try:  # 缓存 token，后续只读查询不必再登录（避开单设备保护）
            import session_store  # noqa: WPS433
            user = getattr(login_res, "user", None)
            phone = str(config.get("username") or "")
            saved = session_store.save(
                uid=client.uid, token=getattr(client, "token", None),
                unid=client.unid, phone=phone,
                name=getattr(user, "name", "") or "",
                campus=getattr(user, "campus_name", "") or "",
                path=session_store.path_for_phone(phone))
            log("会话已缓存（%s）" % Path(saved).name)
        except Exception as exc:  # noqa: BLE001
            log("[警告] 会话缓存失败: %s" % exc)

        rule = client.get_run_policy(run_mode=1)
        if rule is None:
            return finish(False, "未取得学校规则，停止提交")

        pace_bounds = (4.0, 12.0)
        try:
            from running.client import _pace_bounds  # noqa: WPS433
            pace_bounds = _pace_bounds(rule)
        except Exception:
            pass
        minimum_time = int((getattr(rule, "raw", {}) or {}).get("runRuleModel", {})
                           .get("minRunTime", 0) or 0)

        log("学校规则: 最低距离=%sm 单日目标=%sm 配速区间=%s~%s/km"
            % (getattr(rule, "min_distance", "-"), getattr(rule, "day_goal", "-"),
               pace_bounds[0], pace_bounds[1]))

        result = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            plan.distance_m = pick_distance(rng, rule)
            plan.time_sec, plan.pace_min_km = pick_time(
                rng, plan.distance_m, pace_bounds, minimum_time)
            plan.route_source = ""
            plan.route_id = ""
            plan.reversed_direction = False
            if attempt == 1:
                log("本次随机: " + plan.summary)
            else:
                log("第 %d 次重抽: %s" % (attempt, plan.summary))

            provider = make_provider(plan, rng, log)
            result = client.execute_run(
                target_distance_m=plan.distance_m,
                target_time_sec=plan.time_sec,
                sport_type=1,
                progress_callback=log,
                campus=campus,
                dry_run=dry_run,
                route_provider=provider,
                anchor_gcj=((plan.anchor.glat, plan.anchor.glon)
                            if plan.anchor is not None else None),
                anchor_id=(plan.anchor.id if plan.anchor is not None else None),
                anchor_name=(plan.anchor.name if plan.anchor is not None else None),
            )
            if getattr(result, "success", False):
                break
            failure = str(getattr(result, "message", ""))
            if attempt < MAX_ATTEMPTS and "本地一致性校验失败" in failure:
                log("轨迹采样未收敛（%s），重新随机后重试" % failure)
                continue
            break
        ok = bool(getattr(result, "success", False))
        message = getattr(result, "message", "")
        if ok and not dry_run:
            message = "%s｜%s" % (message, plan.summary)
        object_key = getattr(result, "object_key", None)
        outbox_path = getattr(result, "outbox_path", None)
        if object_key:
            log("OBS 对象: %s" % object_key)
        if outbox_path:
            log("Outbox: %s" % outbox_path)
        return finish(
            ok, message,
            rrid=getattr(result, "rrid", None),
            is_valid=getattr(result, "is_valid", None),
            error_code=getattr(result, "error_code", None),
        )
    except Exception as exc:  # noqa: BLE001
        return finish(False, base._friendly_exception(exc))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="校园跑 · 随机一键开跑")
    parser.add_argument("--dry-run", action="store_true", help="只登录并生成轨迹，不提交")
    parser.add_argument("--seed", type=int, default=None, help="固定随机种子（复现用）")
    parser.add_argument("--config", default="", help="指定配置文件，默认 config.json")
    args = parser.parse_args(argv)

    try:
        config = base.load_config(Path(args.config) if args.config else None)
    except base.ConfigError as exc:
        print("配置错误:", exc)
        return 2

    result = run_once(config, dry_run=args.dry_run, seed=args.seed)
    print(result.message)
    print("日志:", result.log_path)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
