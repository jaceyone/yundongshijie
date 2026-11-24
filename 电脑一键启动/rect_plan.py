# -*- coding: utf-8 -*-
"""校园跑 · 直角路线排练（只读取点，不生成轨迹、不提交）

用途：在真正提交之前，先确认「今天这 5 点」能被铺成一条 ≤ 目标距离、
拐角≈90°、且通过栅格校验的闭环。复用 `session*.json` 里仍有效的 token，
不再登录（避开单设备保护 10121）；token 失效时用 `run_rect.py` 直接跑。

    python rect_plan.py --config config.json
    python rect_plan.py --config config_b.json --target 3000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import anchor_points as anchor_mod  # noqa: E402
import rect_route  # noqa: E402
import runner as base  # noqa: E402


def _session_for(config: dict) -> Path:
    """会话文件：默认 session.json；config 里 session_file 可换名。"""
    return base.session_path(config)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="校园跑 · 直角路线排练（只读取点）")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--target", type=int, default=0, help="目标距离；默认取 config")
    parser.add_argument("--time", type=int, default=0, help="目标时长；默认取 config")
    parser.add_argument("--out", default=str(APP_DIR / "logs"))
    args = parser.parse_args(argv)

    config = base.load_config(Path(args.config))
    phone = str(config["username"])
    if not base.gate_allows(phone, config):
        print("账号 %s 不在本机白名单里（config.json 的 allowed_phones）" % phone)
        return 2
    target = int(args.target or config["distance"])
    duration = int(args.time or config["time_sec"])
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    path = _session_for(config)
    if not path.is_file():
        print("缺少会话文件 %s；请直接用 run_rect.py 登录取点" % path.name)
        return 3
    session = json.loads(path.read_text(encoding="utf-8"))
    if str(session.get("phone")) != phone:
        print("会话文件 %s 不属于 %s" % (path.name, phone))
        return 3

    if str(base.PROTOCOL_HOME) not in sys.path:
        sys.path.insert(0, str(base.PROTOCOL_HOME))
    if config.get("node_path"):
        import os
        os.environ["NODE_EXECUTABLE"] = str(config["node_path"])
    if config.get("chromium_path"):
        import os
        os.environ["CHROMIUM_PATH"] = str(config["chromium_path"])

    from sport_client import SportClient792  # noqa: WPS433

    device_id = base.resolve_device_id(config)
    client = SportClient792(uid=session.get("uid"), token=session.get("token"),
                            unid=session.get("unid"),
                            device_config={"DeviceId": device_id})
    print("账号: %s（%s）· 设备 %s" % (phone, session.get("name") or "?", device_id))

    rule = client.get_run_policy(run_mode=1)
    if rule is None:
        print("未取得学校规则: %s" % (client.policy.last_policy_error or ""))
        return 4
    print("学校规则: 最低 %s m · 单日 %s m" % (rule.min_distance, rule.day_goal))

    fences = client.get_geo_fences()
    if not fences:
        print("未取到电子围栏")
        return 4
    fence = fences[0]

    anchor = None
    try:
        pool = anchor_mod.load_pool()
        points_pool = anchor_mod._valid_points(pool)
        if points_pool:
            chosen = points_pool[int(time.time()) % len(points_pool)]
            anchor = (float(chosen["glat"]), float(chosen["glon"]))
            print("取点锚点: %s (%.6f, %.6f)" % (chosen.get("name") or chosen.get("id"),
                                              anchor[0], anchor[1]))
    except Exception as exc:  # noqa: BLE001
        print("锚点池不可用（回退围栏顶点）: %s" % exc)

    live = client.policy.get_checkpoints_for_run(
        fence, uuid_str=str(uuid.uuid4()), rule=rule, anchor_gcj=anchor)
    if not live:
        print("取点失败: %s" % (client.policy.last_checkpoint_error or "未知"))
        return 4
    raws = [point.as_checkpoint() for point in live]
    print("官方点位 %d 个:" % len(raws))
    for index, item in enumerate(raws):
        print("  %d. %s (%.6f, %.6f)" % (index + 1, item.get("name") or item.get("pointName"),
                                        float(item["glat"]), float(item["glon"])))

    builder = rect_route.RectRouteBuilder(target_m=target, log=print)
    result = builder.build(raws, verify=True)
    print(result.summary())
    print("分段:", {key: round(value, 1) for key, value in result.legs.items()})
    print("拐角:", result.corners)
    print("采样模拟:", rect_route.simulate(result.polyline, target, duration,
                                        result.ordered_checkpoints))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    png = rect_route.render_preview(
        result, str(out_dir / ("rect-%s-%s.png" % (phone[-4:], stamp))),
        checkpoint_names=[str(item.get("name") or "") for item in result.ordered_checkpoints])
    geojson = out_dir / ("rect-%s-%s.geojson" % (phone[-4:], stamp))
    feature = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"target_m": target, "length_m": round(result.length_m, 1),
                       "corners": result.corners},
        "geometry": {"type": "LineString",
                     "coordinates": [[lon, lat] for lat, lon in result.polyline]},
    }]}
    geojson.write_text(json.dumps(feature, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / ("rect-%s-%s.json" % (phone[-4:], stamp))).write_text(json.dumps({
        "phone": phone, "target_m": target, "time_sec": duration,
        "length_m": result.length_m, "legs": result.legs, "corners": result.corners,
        "polyline": result.polyline,
        "checkpoints": result.ordered_checkpoints,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("预演图:", png)
    print("GeoJSON:", geojson)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
