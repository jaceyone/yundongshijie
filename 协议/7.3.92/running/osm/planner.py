# -*- coding: utf-8 -*-
"""避建筑轨迹规划器：吸附 → 5 点排序 → 分段 A* → 闭环 → 校验。

规划目标是「自由空间栅格 A* + 主路网偏好」：栅格里的建筑（含 1.5 m
缓冲）与校园外均为不可走，道路格代价最低，空地次之；A* 只负责找
可行折线，最终轨迹必须精确经过官方 5 点，并用栅格逐段复核不穿建筑。
"""
import heapq
import itertools
import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .campus import (
    CampusData,
    OsmRouteError,
    Point,
    load_campus,
    polyline_length,
)
from .grid import CELL_BLOCKED, CELL_NEAR_BUILDING, Cell, CostGrid

NEIGHBORS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)
SQRT2 = math.sqrt(2.0)
DEDUPE_M = 0.02
DEFAULT_MAX_LAPS = 8
MAX_ASTAR_NODES = 400000


@dataclass
class PlanRequest:
    checkpoints: List[Point]
    target_distance_m: int
    target_time_s: int = 0
    policy: int = 1                 # 1 = 按官方 position 顺序；其它值尝试全排列
    snap_radius_m: float = 30.0
    max_laps: int = DEFAULT_MAX_LAPS
    smooth: bool = True


@dataclass
class PlanResult:
    polyline: List[Point]
    loop_length_m: float
    laps: int
    checkpoints_used: List[Point]
    diagnostics: Dict[str, object] = field(default_factory=dict)


def _dedupe(points: Sequence[Point], tolerance_m: float = DEDUPE_M) -> List[Point]:
    result: List[Point] = []
    for point in points:
        if not result or polyline_length([result[-1], point]) > tolerance_m:
            result.append(point)
    return result


def _chaikin(points: Sequence[Point], iterations: int = 1) -> List[Point]:
    """连续点集的 Chaikin 平滑；首尾点固定，用于不移动必经点。"""
    result = [(float(p[0]), float(p[1])) for p in points]
    for _ in range(iterations):
        if len(result) < 3:
            break
        smoothed = [result[0]]
        for a, b in zip(result, result[1:]):
            smoothed.append((0.75*a[0]+0.25*b[0], 0.75*a[1]+0.25*b[1]))
            smoothed.append((0.25*a[0]+0.75*b[0], 0.25*a[1]+0.75*b[1]))
        smoothed.append(result[-1])
        result = _dedupe(smoothed)
    return result


def _midpoint_latlon(a: Point, b: Point) -> Point:
    return (a[0]+b[0])/2.0, (a[1]+b[1])/2.0


class TrackPlanner:
    """在一份 CampusData + CostGrid 上规划闭环轨迹。"""

    def __init__(self, campus: CampusData, grid: CostGrid):
        self.campus = campus
        self.grid = grid
        self._leg_cache: Dict[Tuple[Cell, Cell], Optional[List[Cell]]] = {}
        self._preserved_legs: set = set()
        self._astar_nodes = 0

    @classmethod
    def from_campus(cls, campus_id: str = "xuchang_280173698",
                    data_dir: Optional[str] = None) -> "TrackPlanner":
        campus = load_campus(campus_id, data_dir)
        grid = CostGrid.load(campus_id, data_dir)
        return cls(campus, grid)

    # ---------- 对外入口 ----------

    def plan(self, checkpoints: Sequence[Point],
             target_distance_m: int,
             policy: int = 1,
             snap_radius_m: float = 30.0,
             max_laps: int = DEFAULT_MAX_LAPS,
             smooth: bool = True) -> PlanResult:
        request = PlanRequest(
            checkpoints=[(float(p[0]), float(p[1])) for p in checkpoints],
            target_distance_m=int(target_distance_m),
            policy=int(policy),
            snap_radius_m=float(snap_radius_m),
            max_laps=int(max_laps),
            smooth=bool(smooth),
        )
        original_grid = self.grid
        try:
            return self._plan(request)
        finally:
            # 接入通道只对本次规划生效，不污染下一次点位。
            self.grid = original_grid

    def _plan(self, request: PlanRequest) -> PlanResult:
        self._preserved_legs.clear()
        self._leg_cache.clear()
        self._astar_nodes = 0
        checkpoints = request.checkpoints
        if not 2 <= len(checkpoints) <= 6:
            raise OsmRouteError("osm_no_route", "需要 2–6 个官方点位")
        if request.target_distance_m <= 0:
            raise OsmRouteError("osm_target_distance_too_short", "目标距离必须为正数")
        for lat, lon in checkpoints:
            if not (math.isfinite(lat) and math.isfinite(lon)):
                raise OsmRouteError("osm_no_free_cell", "点位坐标不是有限数")
        access_corridors = self._ensure_checkpoint_access(checkpoints)

        anchors: List[Cell] = []
        snap_distances: List[float] = []
        for lat, lon in checkpoints:
            snapped = self.grid.snap(lat, lon, radius_m=request.snap_radius_m)
            if snapped is None:
                raise OsmRouteError(
                    "osm_no_free_cell",
                    f"点位 ({lat:.6f}, {lon:.6f}) 在 {request.snap_radius_m:.0f} m 内"
                    "没有可走格（可能在建筑内或校园外）",
                    {"checkpoint": [lat, lon], "snap_radius_m": request.snap_radius_m})
            col, row, distance_m = snapped
            anchors.append((col, row))
            snap_distances.append(round(distance_m, 2))

        orders = self._orders(len(checkpoints), request.policy)
        best: Optional[Tuple[Tuple[int, ...], float, List[List[Cell]], float]] = None
        min_loop = float("inf")
        valid_orders = 0
        for order in orders:
            legs: List[List[Cell]] = []
            failed = False
            for index in range(len(order)):
                a = anchors[order[index]]
                b = anchors[order[(index+1) % len(order)]]
                leg = self._leg(a, b)
                if leg is None:
                    failed = True
                    break
                legs.append(leg)
            if failed:
                continue
            loop_length = sum(self.grid.path_length_m(leg) for leg in legs)
            # 每个官方点都要从吸附点往返接入，计入目标距离。
            spur = 2.0*sum(self._spur_m(checkpoints[i], anchors[i])
                           for i in range(len(checkpoints)))
            total = loop_length + spur
            min_loop = min(min_loop, total)
            # 与 consistency.sample_route 的容差保持一致（+1 mm 以内）。
            if total > request.target_distance_m + 0.001:
                continue
            valid_orders += 1
            if best is None or total > best[1]:
                best = (order, total, legs, loop_length)

        if best is None:
            if min_loop == float("inf"):
                raise OsmRouteError("osm_no_route", "任意点位顺序之间都没有可行路径",
                                    {"orders_tried": len(orders)})
            raise OsmRouteError(
                "osm_target_distance_too_short",
                f"最短闭环 {min_loop:.1f} m 已超过目标 {request.target_distance_m} m",
                {"orders_tried": len(orders), "min_loop_m": round(min_loop, 1)})

        order, _, legs, _ = best
        legs, _ = self._enlarge(legs, request.target_distance_m, request.max_laps)

        drafted = self._compose(order, checkpoints, anchors, legs, request.smooth)
        try:
            self._validate_route(drafted)
        except OsmRouteError:
            if not request.smooth:
                raise
            drafted = self._compose(order, checkpoints, anchors, legs, smooth=False)
            self._validate_route(drafted)

        route_length = polyline_length(drafted)
        if route_length > request.target_distance_m + 0.001:
            if request.smooth:
                drafted = self._compose(order, checkpoints, anchors, legs, smooth=False)
                self._validate_route(drafted)
                route_length = polyline_length(drafted)
            if route_length > request.target_distance_m + 0.001:
                raise OsmRouteError(
                    "osm_target_distance_too_short",
                    f"规划闭环 {route_length:.1f} m 超过目标 {request.target_distance_m} m",
                    {"loop_length_m": round(route_length, 1)})

        laps = max(1, int(math.ceil(request.target_distance_m / route_length)))
        if laps > request.max_laps:
            raise OsmRouteError(
                "osm_loop_too_short",
                f"闭环 {route_length:.1f} m 需要重复 {laps} 圈，超过上限 {request.max_laps}",
                {"loop_length_m": round(route_length, 1), "laps": laps})

        used = [checkpoints[i] for i in order]
        diagnostics = {
            "campus_id": self.campus.campus_id,
            "grid_resolution_m": self.grid.resolution_m,
            "snap_distance_m": snap_distances,
            "orders_tried": len(orders),
            "orders_valid": valid_orders,
            "order": list(order),
            "loop_length_m": round(route_length, 1),
            "laps": laps,
            "astar_nodes": self._astar_nodes,
            "smoothed": request.smooth,
            "access_corridors": access_corridors,
        }
        return PlanResult(polyline=drafted, loop_length_m=route_length,
                          laps=laps, checkpoints_used=used,
                          diagnostics=diagnostics)

    # ---------- 排序 / 接入距离 ----------

    @staticmethod
    def _orders(count: int, policy: int) -> List[Tuple[int, ...]]:
        if policy == 1 or count > 6:
            return [tuple(range(count))]
        # 随机跑：官方只约束 5 个点，全排列 120 种，分腿结果有缓存。
        return list(itertools.permutations(range(count)))

    def _spur_m(self, checkpoint: Point, anchor: Cell) -> float:
        return polyline_length([checkpoint, self.grid.cell_latlon(*anchor)])

    # ---------- 官方点落在建筑内：接入通道 ----------

    def _ensure_checkpoint_access(self, checkpoints: Sequence[Point]) -> List[Dict[str, object]]:
        """官方点位落在 OSM 建筑内时，只在该建筑内打通一条 2 m 宽接入通道。

        官方点位是权威的，不能移动；OSM 建筑轮廓可能覆盖宿舍院落或与点位
        有偏移。这里不放开整栋建筑，只沿一条 4 连通走廊把点位接到最近
        可走格，其它建筑仍严格不可走。通道只对本次规划生效。
        """
        inside = [(index, point, self.campus.building_at(*point))
                  for index, point in enumerate(checkpoints)]
        inside = [item for item in inside if item[2] is not None]
        if not inside:
            return []

        grid = self._clone_grid()
        corridors: List[Dict[str, object]] = []
        for index, point, building in inside:
            corridor = self._carve_corridor(grid, point, building)
            if corridor is None:
                raise OsmRouteError(
                    "osm_checkpoint_inside_building",
                    f"点位 ({point[0]:.6f}, {point[1]:.6f}) 位于建筑 "
                    f"{building.osm_id} 内，且无法打通到可走区域的短通道",
                    {"checkpoint": list(point), "building_id": building.osm_id})
            corridor["checkpoint_index"] = index
            corridors.append(corridor)
        self.grid = grid
        return corridors

    def _clone_grid(self) -> CostGrid:
        grid = self.grid
        return CostGrid(grid.resolution_m, grid.origin, grid.width, grid.height,
                        bytearray(grid.costs), grid.legend, grid.campus_id)

    def _carve_corridor(self, grid: CostGrid, point: Point,
                        building) -> Optional[Dict[str, object]]:
        start = grid.latlon_to_cell(*point)
        if not grid.in_bounds(*start):
            return None
        search_cells = int(math.ceil(80.0/grid.resolution_m))
        sx, sy = grid.cell_center(*start)
        candidates: List[Tuple[float, Cell]] = []
        for row in range(start[1]-search_cells, start[1]+search_cells+1):
            if not 0 <= row < grid.height:
                continue
            for col in range(start[0]-search_cells, start[0]+search_cells+1):
                if not 0 <= col < grid.width or not grid.walkable(col, row):
                    continue
                cx, cy = grid.cell_center(col, row)
                candidates.append((math.hypot(cx-sx, cy-sy), (col, row)))
        candidates.sort(key=lambda item: item[0])
        for _, target in candidates[:200]:
            path = self._staircase(start, target)
            if not self._corridor_valid(grid, path, building):
                continue
            carved = 0
            for col, row in path:
                if grid.cost(col, row) == CELL_BLOCKED:
                    grid.costs[row*grid.width+col] = CELL_NEAR_BUILDING
                    carved += 1
            return {
                "building_id": building.osm_id,
                "target_cell": [target[0], target[1]],
                "corridor_cells": len(path),
                "carved_cells": carved,
                "length_m": round(grid.path_length_m(path), 1),
            }
        return None

    def _corridor_valid(self, grid: CostGrid, path: Sequence[Cell], building) -> bool:
        for col, row in path:
            if not grid.in_bounds(col, row):
                return False
            lat, lon = grid.cell_latlon(col, row)
            if not self.campus.contains(lat, lon):
                return False
            if grid.walkable(col, row):
                continue
            other = self.campus.building_at(lat, lon)
            if other is not None and other.osm_id != building.osm_id:
                return False
        return True

    @staticmethod
    def _staircase(a: Cell, b: Cell) -> List[Cell]:
        """4 连通台阶路径；单格宽通道也能被 A* 的对角禁穿规则通过。"""
        x, y = a
        cells = [(x, y)]
        while (x, y) != b:
            dx, dy = b[0]-x, b[1]-y
            if abs(dx) >= abs(dy):
                x += 1 if dx > 0 else -1
            else:
                y += 1 if dy > 0 else -1
            cells.append((x, y))
        return cells

    # ---------- A* ----------

    def _leg(self, start: Cell, goal: Cell) -> Optional[List[Cell]]:
        key = (start, goal)
        if key not in self._leg_cache:
            self._leg_cache[key] = self._astar(start, goal)
        return self._leg_cache[key]

    def _astar(self, start: Cell, goal: Cell) -> Optional[List[Cell]]:
        grid = self.grid
        if not grid.walkable(*start) or not grid.walkable(*goal):
            return None
        if start == goal:
            return [start]
        res = grid.resolution_m

        def heuristic(cell: Cell) -> float:
            dx = abs(cell[0]-goal[0])
            dy = abs(cell[1]-goal[1])
            return res*(max(dx, dy) + (SQRT2-1.0)*min(dx, dy))

        counter = itertools.count()
        open_heap: List[Tuple[float, int, Cell, float]] = [
            (heuristic(start), next(counter), start, 0.0)]
        best_g: Dict[Cell, float] = {start: 0.0}
        came_from: Dict[Cell, Cell] = {}
        expanded = 0
        while open_heap:
            _, _, current, g_at_push = heapq.heappop(open_heap)
            if g_at_push > best_g.get(current, float("inf")) + 1e-9:
                continue
            if current == goal:
                self._astar_nodes += expanded
                return self._reconstruct(came_from, current)
            expanded += 1
            if expanded > MAX_ASTAR_NODES:
                return None
            cx, cy = current
            for dx, dy in NEIGHBORS:
                nx, ny = cx+dx, cy+dy
                if not grid.walkable(nx, ny):
                    continue
                if dx and dy and not (grid.walkable(cx+dx, cy)
                                      and grid.walkable(cx, cy+dy)):
                    continue  # 禁止对角穿角
                step = res*(SQRT2 if dx and dy else 1.0)
                tentative = best_g[current] + step*grid.cost(nx, ny)
                if tentative + 1e-9 >= best_g.get((nx, ny), float("inf")):
                    continue
                best_g[(nx, ny)] = tentative
                came_from[(nx, ny)] = current
                heapq.heappush(open_heap, (tentative+heuristic((nx, ny)),
                                           next(counter), (nx, ny), tentative))
        return None

    @staticmethod
    def _reconstruct(came_from: Dict[Cell, Cell], current: Cell) -> List[Cell]:
        path = [current]
        while current in came_from:
            current = came_from[current]
            path.append(current)
        path.reverse()
        return path

    # ---------- 折线处理 ----------

    def _line_of_sight(self, a: Cell, b: Cell) -> bool:
        """按半格步长抽样，避免直线擦过不可走格的角。"""
        ax, ay = self.grid.cell_center(*a)
        bx, by = self.grid.cell_center(*b)
        distance = math.hypot(bx-ax, by-ay)
        steps = max(1, int(math.ceil(distance/(self.grid.resolution_m*0.5))))
        for index in range(steps+1):
            fraction = index/steps
            lat, lon = self.grid.projection.latlon(ax + (bx-ax)*fraction,
                                                   ay + (by-ay)*fraction)
            if not self.grid.walkable(*self.grid.latlon_to_cell(lat, lon)):
                return False
        return True

    def _string_pull(self, path: Sequence[Cell]) -> List[Cell]:
        if len(path) <= 2:
            return list(path)
        result = [path[0]]
        index = 0
        last = len(path)-1
        while index < last:
            probe = last
            while probe > index+1 and not self._line_of_sight(path[index], path[probe]):
                probe -= 1
            result.append(path[probe])
            index = probe
        return result

    def _leg_points(self, path: Sequence[Cell], smooth: bool) -> List[Point]:
        if (path[0], path[-1]) in self._preserved_legs:
            # 补长绕行段：拉直/平滑会把人为加的绕行吃掉，原样保留。
            return _dedupe([self.grid.cell_latlon(*cell) for cell in path])
        cells = self._pull_preserving_detour(path)
        points = _dedupe([self.grid.cell_latlon(*cell) for cell in cells])
        if not self._points_walkable(points):
            # 视线拉直偶尔会擦过不可走格的角；退回原始 A* 折线，它逐格可走。
            points = _dedupe([self.grid.cell_latlon(*cell) for cell in path])
        if smooth and len(points) > 2:
            candidate = _chaikin(points)
            if self._points_walkable(candidate):
                return candidate
        return points

    def _pull_preserving_detour(self, path: Sequence[Cell]) -> List[Cell]:
        """先在最远偏离点切一刀再分段拉直，保留绕行凸起。"""
        if len(path) <= 2:
            return list(path)
        ax, ay = self.grid.cell_center(*path[0])
        bx, by = self.grid.cell_center(*path[-1])
        straight = math.hypot(bx-ax, by-ay)
        if self.grid.path_length_m(path) <= straight*1.25 + 1.0:
            return self._string_pull(path)
        best_index, best_distance = 1, -1.0
        for index in range(1, len(path)-1):
            x, y = self.grid.cell_center(*path[index])
            if straight < 1e-6:
                distance = math.hypot(x-ax, y-ay)
            else:
                distance = abs((bx-ax)*(ay-y) - (ax-x)*(by-ay)) / straight
            if distance > best_distance:
                best_distance, best_index = distance, index
        first = self._string_pull(path[:best_index+1])
        second = self._string_pull(path[best_index:])
        return first + second[1:]

    def _points_walkable(self, points: Sequence[Point]) -> bool:
        """按 1 m 步长抽样，确认整条折线落在可走格上。"""
        for a, b in zip(points, points[1:]):
            length = polyline_length([a, b])
            steps = max(1, int(math.ceil(length)))
            for step in range(steps+1):
                fraction = step/steps
                lat = a[0] + (b[0]-a[0])*fraction
                lon = a[1] + (b[1]-a[1])*fraction
                if not self.grid.walkable(*self.grid.latlon_to_cell(lat, lon)):
                    return False
        return True

    def _compose(self, order: Sequence[int], checkpoints: Sequence[Point],
                 anchors: Sequence[Cell], legs: Sequence[Sequence[Cell]],
                 smooth: bool) -> List[Point]:
        count = len(order)
        first = checkpoints[order[0]]
        route: List[Point] = [first]
        for index in range(count):
            checkpoint_index = order[index]
            route.append(self.grid.cell_latlon(*anchors[checkpoint_index]))
            route.extend(self._leg_points(legs[index], smooth)[1:])
            if index+1 < count:
                next_index = order[index+1]
                next_point = checkpoints[next_index]
                next_snap = self.grid.cell_latlon(*anchors[next_index])
                if polyline_length([next_point, next_snap]) > 1e-3:
                    route.append(next_point)
        if polyline_length([route[-1], first]) > 1e-3:
            route.append(first)
        return _dedupe(route)

    # ---------- 闭环补长 ----------

    def _enlarge(self, legs: List[List[Cell]], target: int,
                 max_laps: int) -> Tuple[List[List[Cell]], float]:
        # 留 5% 余量：组成最终折线时去重/接点会略短于纯 A* 长度。
        goal_length = target/float(max_laps)*1.05
        for _ in range(12):
            length = sum(self.grid.path_length_m(leg) for leg in legs)
            if length >= goal_length:
                return legs, length
            order_by_length = sorted(range(len(legs)),
                                     key=lambda i: self.grid.path_length_m(legs[i]),
                                     reverse=True)
            improved = False
            for index in order_by_length:
                start, end = legs[index][0], legs[index][-1]
                old_length = self.grid.path_length_m(legs[index])
                ax, ay = self.grid.cell_center(*start)
                bx, by = self.grid.cell_center(*end)
                dx, dy = bx-ax, by-ay
                norm = math.hypot(dx, dy)
                if norm < 1e-6:
                    continue
                px, py = -dy/norm, dx/norm
                mx, my = (ax+bx)/2.0, (ay+by)/2.0
                candidates: List[Tuple[float, List[Cell]]] = []
                for offset in (40.0, 80.0, 160.0, 320.0, 640.0):
                    for sign in (1.0, -1.0):
                        lat, lon = self.grid.projection.latlon(mx + px*offset*sign,
                                                               my + py*offset*sign)
                        snapped = self.grid.nearest_walkable(
                            lat, lon, radius_m=offset*0.5 + 40.0)
                        if snapped is None:
                            continue
                        anchor = (snapped[0], snapped[1])
                        first = self._leg(start, anchor)
                        second = self._leg(anchor, end)
                        if first is None or second is None:
                            continue
                        candidate = first + second[1:]
                        new_length = self.grid.path_length_m(candidate)
                        if new_length <= old_length + 1.0:
                            continue
                        total = length - old_length + new_length
                        if total > target + 0.001:
                            continue
                        candidates.append((total, candidate))
                if not candidates:
                    continue
                reaching = [item for item in candidates if item[0] >= goal_length]
                chosen = (min(reaching, key=lambda item: item[0]) if reaching
                          else max(candidates, key=lambda item: item[0]))
                legs[index] = chosen[1]
                self._preserved_legs.add((start, end))
                improved = True
                break
            if not improved:
                break
        length = sum(self.grid.path_length_m(leg) for leg in legs)
        return legs, length

    # ---------- 校验 ----------

    def validate_track(self, track: Sequence[Point],
                       allow_building_ids: Sequence[int] = ()) -> None:
        """校验最终采样轨迹：不得穿建筑、不得越出规划边界。

        ``allow_building_ids`` 是官方点落在建筑内时规划器打通的接入通道
        所在建筑；只放行这些建筑，其它建筑仍严格校验。
        """
        if len(track) < 2:
            raise OsmRouteError("osm_no_route", "轨迹至少需要两个点")
        allowed = {int(item) for item in allow_building_ids}
        for lat, lon in track:
            self._validate_track_point(lat, lon, allowed)
        for a, b in zip(track, track[1:]):
            length = polyline_length([a, b])
            steps = max(1, int(math.ceil(length)))
            for step in range(steps + 1):
                fraction = step / steps
                lat = a[0] + (b[0]-a[0])*fraction
                lon = a[1] + (b[1]-a[1])*fraction
                self._validate_track_point(lat, lon, allowed)

    def _validate_track_point(self, lat: float, lon: float,
                              allowed_building_ids) -> None:
        col, row = self.grid.latlon_to_cell(lat, lon)
        if not self.grid.in_bounds(col, row):
            raise OsmRouteError(
                "osm_fence_violation",
                f"轨迹点 ({lat:.6f}, {lon:.6f}) 超出规划边界",
                {"point": [lat, lon]})
        building = self.campus.building_at(lat, lon)
        if building is not None and building.osm_id in allowed_building_ids:
            return
        if self.grid.walkable(col, row):
            return
        if building is not None:
            raise OsmRouteError(
                "osm_building_collision",
                f"轨迹在 ({lat:.6f}, {lon:.6f}) 穿过建筑 {building.osm_id}",
                {"point": [lat, lon], "building_id": building.osm_id})
        raise OsmRouteError(
            "osm_fence_violation",
            f"轨迹点 ({lat:.6f}, {lon:.6f}) 落在校园边界外或建筑缓冲区",
            {"point": [lat, lon]})

    def _validate_route(self, route: Sequence[Point]) -> None:
        for a, b in zip(route, route[1:]):
            length = polyline_length([a, b])
            steps = max(1, int(math.ceil(length)))
            for step in range(steps+1):
                fraction = step/steps
                lat = a[0] + (b[0]-a[0])*fraction
                lon = a[1] + (b[1]-a[1])*fraction
                col, row = self.grid.latlon_to_cell(lat, lon)
                if self.grid.walkable(col, row):
                    continue
                building = self.campus.building_at(lat, lon)
                if building is not None:
                    raise OsmRouteError(
                        "osm_building_collision",
                        f"轨迹在 ({lat:.6f}, {lon:.6f}) 穿过建筑 {building.osm_id}",
                        {"point": [lat, lon], "building_id": building.osm_id})
                raise OsmRouteError(
                    "osm_fence_violation",
                    f"轨迹点 ({lat:.6f}, {lon:.6f}) 落在校园边界外或建筑缓冲区",
                    {"point": [lat, lon]})


def plan_route(checkpoints: Sequence[Point],
               target_distance_m: int,
               campus_id: str = "xuchang_280173698",
               policy: int = 1,
               data_dir: Optional[str] = None,
               **kwargs) -> PlanResult:
    """便捷入口：加载校园数据并规划一次。"""
    planner = TrackPlanner.from_campus(campus_id, data_dir)
    return planner.plan(checkpoints, target_distance_m, policy=policy, **kwargs)


def export_route_geojson(polyline: Sequence[Point], path: str,
                         properties: Optional[Dict[str, object]] = None) -> str:
    """把规划折线写成 GeoJSON，便于在地图上人工检查。"""
    if len(polyline) < 2:
        raise ValueError("轨迹至少需要两个点")
    target = os.path.abspath(os.path.expanduser(str(path)))
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    collection = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": dict(properties or {}),
            "geometry": {
                "type": "LineString",
                "coordinates": [[float(lon), float(lat)] for lat, lon in polyline],
            },
        }],
    }
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(collection, handle, ensure_ascii=False, indent=2)
    return target
