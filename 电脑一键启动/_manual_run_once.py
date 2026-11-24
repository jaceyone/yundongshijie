# -*- coding: utf-8 -*-
"""一次性：用路线库非交互地跑一次真实提交（验证配速图修复）。用完即删。"""
import sys

sys.path.insert(0, ".")

import route_library as route_lib
import runner


def main():
    config = runner.load_config()

    def provider(context):
        def val(name, default=None):
            if isinstance(context, dict):
                return context.get(name, default)
            return getattr(context, name, default)

        points = (val("raw_checkpoints") or val("checkpoints")
                  or val("points") or [])
        target = int(val("target_distance_m", config.get("distance", 3000)))
        unid = val("unid", None)
        area_id = int(val("area_id", -1) or -1)
        if not points:
            print("ROUTE: provider 没有拿到当次点位", flush=True)
            return None

        library = route_lib.RouteLibrary()
        matches = library.find_by_points(points, unid, area_id, target)
        if not matches:
            print("ROUTE: 路线库未命中（unid=%s area=%s target=%s）"
                  % (unid, area_id, target), flush=True)
            library.record_miss(points, unid, area_id, target,
                                reason="no_match")
            return None

        best = matches[0]
        expanded = route_lib.expand_polyline_to_distance(
            best.adjusted_polyline, target)
        if float(best.entry.length_m) > target + 0.001:
            try:
                route_lib.checkpoint_order(expanded, points)
            except ValueError:
                print("ROUTE: 命中但截断后缺少打卡点，放弃", flush=True)
                return None
        library.mark_used(best.entry.route_id, target)
        print("ROUTE: 命中 %s 模板=%.1f m 趟数=%.2f 偏差=%.1f m"
              % (best.entry.route_id[:8], float(best.entry.length_m),
                 best.laps, best.max_point_distance_m), flush=True)
        return ([[float(lat), float(lon)] for lat, lon in expanded],
                best.ordered_checkpoints)

    result = runner.run(config, dry_run=False, route_provider=provider)
    print(result.message, flush=True)
    print("日志:", result.log_path, flush=True)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
