# -*- coding: utf-8 -*-
"""预设路线（手动路线库）匹配 + 围栏校验

给 `run_rect.py --preset` 用：先按当天官方 5 点去 `route_library.json` 找预设折线，
命中而且整条折线都在学校官方围栏内才采用；否则交回直角路线兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

APP_DIR = Path(__file__).resolve().parent
PROTOCOL_HOME = APP_DIR.parent / "协议" / "7.3.92"
for path in (str(APP_DIR), str(PROTOCOL_HOME)):
    if path not in sys.path:
        sys.path.insert(0, path)

import route_library as route_lib  # noqa: E402
from running.consistency import distance  # noqa: E402
from running.osm.campus import point_in_polygon  # noqa: E402

Point = Tuple[float, float]


def fence_ok(points: Sequence[Point], fence: Optional[Sequence[Point]],
             step_m: float = 1.0) -> bool:
    """折线是否整条落在围栏内（按 1 m 采样）。"""
    if not fence or len(fence) < 3:
        return True
    for index, (lat, lon) in enumerate(points):
        if not point_in_polygon(lat, lon, fence):
            return False
        if not index:
            continue
        prev = points[index - 1]
        segment = distance(prev, (lat, lon))
        steps = max(1, int(segment / step_m))
        for step in range(steps + 1):
            f = step / steps
            plat = prev[0] + (lat - prev[0]) * f
            plon = prev[1] + (lon - prev[1]) * f
            if not point_in_polygon(plat, plon, fence):
                return False
    return True


def try_preset(points: Sequence[Dict], unid: int, area_id: int, target_m: int,
               fence: Optional[Sequence[Point]] = None, log=None):
    """命中预设路线就返回 (polyline, ordered_checkpoints, route_id)，否则 None。"""
    log = log or (lambda message: None)
    try:
        library = route_lib.RouteLibrary()
        matches = library.find_by_points(list(points), unid, area_id, int(target_m))
    except Exception as exc:  # noqa: BLE001
        log("预设路线: 读取路线库失败 %s" % exc)
        return None
    if not matches:
        log("预设路线: 今天这 5 点在路线库里没有匹配")
        return None

    usable = [m for m in matches
              if 0 < float(m.entry.length_m) <= int(target_m) + 0.001]
    if not usable:
        log("预设路线: 命中 %d 条，但模板长度都超过目标 %d m" % (len(matches), target_m))
        return None

    reasons: List[str] = []
    for match in usable:
        try:
            filled = route_lib.expand_polyline_to_distance(
                match.adjusted_polyline, int(target_m))
        except Exception as exc:  # noqa: BLE001
            reasons.append("折返展开失败(%s)" % exc)
            continue
        if not fence_ok(filled, fence):
            reasons.append("越出学校围栏")
            continue
        ordered = [dict(item) for item in match.ordered_checkpoints]
        route_id = str(match.entry.route_id)
        log("预设路线: 命中 %s 模板 %.1f m，折返到 %d m，且在围栏内"
            % (route_id[:8], float(match.entry.length_m), int(target_m)))
        return [[float(lat), float(lon)] for lat, lon in filled], ordered, route_id

    log("预设路线: 命中 %d 条但都不可用（%s）" % (len(usable), "；".join(reasons[:3])))
    return None
