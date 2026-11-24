# -*- coding: utf-8 -*-
"""校园跑 · 直角路线（拐角≈90°）生成、校验与预演

为什么单独做一份：
- 手动路线库里的折线是手画的平滑折线，长度不够时按原路折返（180° 掉头），
  轨迹看起来像来回蹭；
- OSM 自动规划走的是道路中心线，拐角是弯的；
- 本模块在校园代价栅格上只走东西/南北（四邻域 A* + 显式矩环），折线简化后
  每个拐角都是 90°；长度用「矩环尺寸」搜索，保证 ≤ 目标距离且尽量接近。

只读 `协议/7.3.92` 的 running.osm（栅格/校园数据）与 running.consistency
（距离、采样、校验），不修改协议任何文件。
"""

from __future__ import annotations

import heapq
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

APP_DIR = Path(__file__).resolve().parent
PROTOCOL_HOME = APP_DIR.parent / "协议" / "7.3.92"
if str(PROTOCOL_HOME) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_HOME))

from running.consistency import distance  # noqa: E402
from running.osm import CostGrid, TrackPlanner, load_campus  # noqa: E402
from running.osm.campus import LocalProjection, point_in_polygon  # noqa: E402
from running.osm.grid import (  # noqa: E402
    CELL_BLOCKED,
    CELL_NEAR_BUILDING,
    CELL_NEAR_ROAD,
    CELL_OPEN,
    CELL_ROAD,
)

CAMPUS_ID = "xuchang_280173698"

# 格子上的行走代价：尽量拉平，只轻推「走路面」，否则 A* 会为了省钱来回横跳。
CELL_COST = {
    CELL_ROAD: 1.0,
    CELL_NEAR_ROAD: 1.05,
    CELL_OPEN: 1.2,
    CELL_NEAR_BUILDING: 3.0,
}
# 每拐一次弯的额外代价（单位≈米/格）；值大 → 走直、少拐，拐角才像人。
TURN_PENALTY = 26.0
RIGHT_ANGLE_TOL = 15.0  # 统计「直角」用的容差（度）
DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1))
Cell = Tuple[int, int]
Point = Tuple[float, float]


class RectRouteError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- 工具


def simplify_collinear(points: Sequence[Point], cross_tol: float = 2e-3) -> List[Point]:
    """去掉同向共线的中间点，只保留拐角与端点。"""
    if len(points) < 3:
        return [tuple(p) for p in points]
    out = [tuple(points[0])]
    for index in range(1, len(points) - 1):
        a, b, c = out[-1], tuple(points[index]), tuple(points[index + 1])
        ax, ay = b[1] - a[1], b[0] - a[0]
        bx, by = c[1] - b[1], c[0] - b[0]
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la < 1e-12 or lb < 1e-12:
            continue
        cross = ax * by - ay * bx
        dot = ax * bx + ay * by
        if abs(cross) / (la * lb) < cross_tol and dot > 0:
            continue
        out.append(b)
    out.append(tuple(points[-1]))
    return out


def _point_segment_m(p: Point, a: Point, b: Point) -> float:
    """点到线段的距离（米，校园尺度用等距近似）。"""
    cos0 = math.cos(math.radians(float(p[0])))
    ax, ay = (a[1] - p[1]) * cos0, a[0] - p[0]
    bx, by = (b[1] - p[1]) * cos0, b[0] - p[0]
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-18:
        return math.hypot(ax, ay) * math.pi / 180.0 * 6371000.0
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(px, py) * math.pi / 180.0 * 6371000.0


def drop_small_spurs(points: Sequence[Point], protect: Sequence[Point],
                     max_spur_m: float = 40.0, blocked=None) -> List[Point]:
    """删掉短促的 180° 回冲点（掉头），打卡点与它所在的两段线不动。

    ``blocked`` 是可选的「这段线穿了多少个不可走格」函数：删点后的新线段
    不能比原来两段更糟，否则会拐着拐着切进建筑。
    """
    route = [tuple(p) for p in points]
    changed = True
    while changed:
        changed = False
        for index in range(1, len(route) - 1):
            a, b, c = route[index - 1], route[index], route[index + 1]
            angle_ab = math.atan2(b[0] - a[0], b[1] - a[1])
            angle_bc = math.atan2(c[0] - b[0], c[1] - b[1])
            delta = abs(math.degrees(angle_ab - angle_bc)) % 360.0
            delta = min(delta, 360.0 - delta)
            if delta < 155.0:
                continue
            if distance(a, b) > max_spur_m:
                continue
            if any(distance(point, b) <= 2.0 for point in protect):
                continue
            # 被删掉的两段线如果压着某个打卡点，删了就少走一个点
            stray = False
            for point in protect:
                near_dropped = min(_point_segment_m(point, a, b),
                                   _point_segment_m(point, b, c)) <= 0.8
                near_kept = _point_segment_m(point, a, c) <= 0.8
                if near_dropped and not near_kept:
                    stray = True
                    break
            if stray:
                continue
            if blocked is not None:
                before = blocked(a, b) + blocked(b, c)
                after = blocked(a, c)
                if after > before:
                    continue
            route.pop(index)
            changed = True
            break
    return route


def corner_degrees(points: Sequence[Point]) -> List[float]:
    """每个内部顶点的转向角度（0–180；180≈掉头）。按米制投影算，避免经纬度比例失真。"""
    if not points:
        return []
    cos0 = math.cos(math.radians(float(points[0][0])))

    def xy(point):
        return (point[1] * cos0, point[0])

    values = []
    for index in range(1, len(points) - 1):
        a, b, c = xy(points[index - 1]), xy(points[index]), xy(points[index + 1])
        ax, ay = b[0] - a[0], b[1] - a[1]
        bx, by = c[0] - b[0], c[1] - b[1]
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la < 1e-9 or lb < 1e-9:
            continue
        cos = max(-1.0, min(1.0, (ax * bx + ay * by) / (la * lb)))
        values.append(math.degrees(math.acos(cos)))
    return values


def right_angle_stats(points: Sequence[Point]) -> Dict[str, float]:
    corners = corner_degrees(points)
    total = len(corners)
    right = sum(1 for value in corners if abs(value - 90.0) <= RIGHT_ANGLE_TOL)
    uturn = sum(1 for value in corners if value >= 150.0)
    return {
        "corners": total,
        "right": right,
        "right_ratio": round(right / total, 3) if total else 1.0,
        "u_turns": uturn,
        "min_deg": round(min(corners), 1) if corners else 0.0,
        "max_deg": round(max(corners), 1) if corners else 0.0,
    }


def polyline_length(points: Sequence[Point]) -> float:
    return sum(distance(a, b) for a, b in zip(points, points[1:]))


def campus_center(campus_id: str = CAMPUS_ID) -> Point:
    """校园中心（campus JSON 的 center，GCJ-02）。取点/矩环都尽量围着它。"""
    return load_campus(campus_id).center


def fence_polygon_from_area(area) -> List[Point]:
    """把 policy 的 GeoFenceArea 转成 [(lat, lon), ...]（GCJ-02）。"""
    points: List[Point] = []
    for point in getattr(area, "polygon_points", []) or []:
        try:
            lat, lon = point.gcj_coordinates()
        except Exception:  # noqa: BLE001
            lat, lon = getattr(point, "lat", None), getattr(point, "lng", None)
        if lat is None or lon is None:
            continue
        points.append((float(lat), float(lon)))
    return points


# --------------------------------------------------------------------------- 栅格 A*


@dataclass
class GridPlanner:
    """四邻域 A*：只走东西/南北，拐角天然 90°。

    ``set_fence()`` 之后，围栏外的格子一律当不可走（校园栅格本身是围栏
    外扩 350 m，不设这条约束会跑到学校外面）。
    """

    campus_id: str = CAMPUS_ID
    grid: Optional[CostGrid] = None
    campus: object = None
    fence: Optional[List[Point]] = None

    def __post_init__(self):
        if self.grid is None:
            self.grid = CostGrid.load(self.campus_id)
        if self.campus is None:
            self.campus = load_campus(self.campus_id)
        self.projection: LocalProjection = self.grid.projection
        self.width = self.grid.width
        self.height = self.grid.height
        self.res = float(self.grid.resolution_m)
        self._costs = self.grid.costs
        self._fast = [0.0 if value == CELL_BLOCKED else CELL_COST.get(value, 3.0)
                      for value in range(256)]
        self._fast[CELL_BLOCKED] = math.inf
        self._outside_fence: Optional[bytearray] = None

    # -- 围栏 -------------------------------------------------------------
    def set_fence(self, polygon: Optional[Sequence[Point]]) -> None:
        """把围栏外的格子标记为不可走；None 表示不限（旧行为）。"""
        self.fence = [(float(p[0]), float(p[1])) for p in (polygon or [])] or None
        self._outside_fence = None
        if self.fence is not None:
            self._build_fence_mask()

    def _build_fence_mask(self) -> None:
        polygon = self.fence or []
        mask = bytearray(self.width * self.height)
        if len(polygon) < 3:
            self._outside_fence = mask
            return
        lats = [p[0] for p in polygon]
        lons = [p[1] for p in polygon]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        for row in range(self.height):
            for col in range(self.width):
                lat, lon = self.cell_point(col, row)
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon \
                        and point_in_polygon(lat, lon, polygon):
                    continue
                mask[row * self.width + col] = 1
        self._outside_fence = mask

    def inside_fence(self, lat: float, lon: float) -> bool:
        if self.fence is None:
            return True
        col, row = self.grid.latlon_to_cell(lat, lon)
        if col < 0 or row < 0 or col >= self.width or row >= self.height:
            return False
        if self._outside_fence is None:
            return point_in_polygon(lat, lon, self.fence)
        return not self._outside_fence[row * self.width + col]

    # -- 坐标工具 ---------------------------------------------------------
    def xy(self, lat: float, lon: float) -> Tuple[float, float]:
        return self.projection.xy(lat, lon)

    def latlon(self, x: float, y: float) -> Point:
        return self.projection.latlon(x, y)

    def in_fence(self, lat: float, lon: float) -> bool:
        return point_in_polygon(lat, lon, self.campus.boundary)

    def cost(self, col: int, row: int) -> float:
        if col < 0 or row < 0 or col >= self.width or row >= self.height:
            return math.inf
        if self._outside_fence is not None and \
                self._outside_fence[row * self.width + col]:
            return math.inf
        return self._fast[self._costs[row * self.width + col]]

    def walkable(self, col: int, row: int) -> bool:
        return not math.isinf(self.cost(col, row))

    def cell_xy(self, col: int, row: int) -> Tuple[float, float]:
        return ((col + 0.5) * self.res, (row + 0.5) * self.res)

    def cell_point(self, col: int, row: int) -> Point:
        return self.latlon(*self.cell_xy(col, row))

    def nearest_cell(self, lat: float, lon: float, radius_m: float = 45.0) -> Cell:
        x, y = self.xy(lat, lon)
        base_col = int(math.floor(x / self.res))
        base_row = int(math.floor(y / self.res))
        span = int(math.ceil(radius_m / self.res)) + 1
        best = None
        for row in range(base_row - span, base_row + span + 1):
            for col in range(base_col - span, base_col + span + 1):
                if not self.walkable(col, row):
                    continue
                cx, cy = self.cell_xy(col, row)
                gap = math.hypot(cx - x, cy - y)
                if gap > radius_m:
                    continue
                if best is None or gap < best[0]:
                    best = (gap, col, row)
        if best is None:
            raise RectRouteError(
                "rect_snap_failed",
                "坐标 (%.6f, %.6f) 附近 %.0f m 内没有可走格" % (lat, lon, radius_m))
        return (best[1], best[2])

    # -- 单段四邻域 A* ----------------------------------------------------
    def path_cells(self, start: Cell, goal: Cell) -> List[Cell]:
        if start == goal:
            return [start]
        if not self.walkable(*goal):
            raise RectRouteError("rect_goal_blocked", "终点落在不可走格 %s" % (goal,))
        width = self.width
        costs = self._costs
        fast = self._fast
        outside = self._outside_fence
        open_heap = [(0.0, 0.0, start[0], start[1], -1)]
        best: Dict[int, float] = {}
        parent: Dict[int, int] = {}
        goal_state = -1
        gx, gy = goal
        while open_heap:
            _, cost, col, row, direction = heapq.heappop(open_heap)
            state_key = ((row * width + col) << 3) | (direction + 1)
            if cost > best.get(state_key, math.inf) + 1e-9:
                continue
            if col == gx and row == gy:
                goal_state = state_key
                break
            for index, (dc, dr) in enumerate(DIRS):
                nc, nr = col + dc, row + dr
                if nc < 0 or nr < 0 or nc >= width or nr >= self.height:
                    continue
                if outside is not None and outside[nr * width + nc]:
                    continue
                step = fast[costs[nr * width + nc]]
                if math.isinf(step):
                    continue
                turn = 0.0 if direction in (-1, index) else TURN_PENALTY
                value = cost + step + turn
                next_key = ((nr * width + nc) << 3) | (index + 1)
                if value + 1e-9 < best.get(next_key, math.inf):
                    best[next_key] = value
                    parent[next_key] = state_key
                    heapq.heappush(open_heap,
                                   (value + abs(gx - nc) + abs(gy - nr), value, nc, nr, index))
        if goal_state < 0:
            raise RectRouteError(
                "rect_no_path", "栅格里找不到 %s → %s 的四邻域通路" % (start, goal))
        cells = []
        key = goal_state
        while key != (((start[1] * width + start[0]) << 3) | 0):
            row, col = divmod(key >> 3, width)
            cells.append((col, row))
            key = parent[key]
        cells.append(start)
        cells.reverse()
        return cells

    def cells_to_points(self, cells: Sequence[Cell]) -> List[Point]:
        return [self.cell_point(col, row) for col, row in cells]

    def blocked_samples(self, a: Point, b: Point) -> int:
        """一段线按 1 m 采样后落在不可走格里的点数（用于比较两段线优劣）。"""
        ax, ay = self.xy(*a)
        bx, by = self.xy(*b)
        steps = max(1, int(math.ceil(math.hypot(bx - ax, by - ay))))
        bad = 0
        for index in range(steps + 1):
            f = index / steps
            x = ax + (bx - ax) * f
            y = ay + (by - ay) * f
            if not self.walkable(int(math.floor(x / self.res)),
                                 int(math.floor(y / self.res))):
                bad += 1
        return bad

    # -- 精确点接入（保持打卡点原坐标作为顶点） --------------------------
    def attach(self, point: Point, cell: Cell,
               prefer_axis: Optional[int] = None) -> List[Point]:
        """从精确坐标接 L 形短线到格心；prefer_axis 用于减少 180° 回冲。"""
        px, py = self.xy(*point)
        cx, cy = self.cell_xy(*cell)
        variants = [
            [(px, cy), (cx, cy)],   # 先南北后东西
            [(cx, py), (cx, cy)],   # 先东西后南北
        ]
        scored = []
        for legs in variants:
            ok = True
            prev = (px, py)
            for nx, ny in legs:
                steps = max(1, int(math.ceil(math.hypot(nx - prev[0], ny - prev[1]) / self.res)))
                for step in range(steps + 1):
                    f = step / steps
                    x = prev[0] + (nx - prev[0]) * f
                    y = prev[1] + (ny - prev[1]) * f
                    col = int(math.floor(x / self.res))
                    row = int(math.floor(y / self.res))
                    if not self.walkable(col, row):
                        ok = False
                        break
                if not ok:
                    break
                prev = (nx, ny)
            length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                         for a, b in zip([(px, py)] + legs, legs))
            penalty = 0.0
            if prefer_axis is not None and legs:
                last = legs[-1]
                axis = 0 if abs(last[0] - px) >= abs(last[1] - py) else 1
                # 与即将走的方向同轴时才算「顺」，否则可能回冲
                if axis != prefer_axis:
                    penalty = 60.0
            scored.append((0 if ok else 1, length + penalty, legs))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [self.latlon(*xy) for xy in scored[0][2]]

    def connect(self, a: Point, b: Point,
                protect: Sequence[Point] = ()) -> List[Point]:
        """精确点 a → 精确点 b 的直角折线（两端保留原坐标）。"""
        a_cell = self.nearest_cell(*a)
        b_cell = self.nearest_cell(*b)
        cells = self.path_cells(a_cell, b_cell)
        first_axis = None
        if len(cells) >= 2:
            first_axis = 0 if cells[1][0] != cells[0][0] else 1
        last_axis = None
        if len(cells) >= 2:
            last_axis = 0 if cells[-1][0] != cells[-2][0] else 1
        route = [a]
        route += self.attach(a, a_cell, prefer_axis=first_axis)
        route += self.cells_to_points(cells)
        route += list(reversed(self.attach(b, b_cell, prefer_axis=last_axis)))
        route.append(b)
        return simplify_collinear(route)


# --------------------------------------------------------------------------- 矩环


@dataclass
class Ring:
    c1: int
    c2: int
    r1: int
    r2: int
    res: float = 2.0

    @property
    def width_m(self) -> float:
        return (self.c2 - self.c1) * self.res

    @property
    def height_m(self) -> float:
        return (self.r2 - self.r1) * self.res

    @property
    def perimeter_m(self) -> float:
        return 2.0 * (self.width_m + self.height_m)

    def corners(self) -> List[Cell]:
        return [(self.c1, self.r1), (self.c2, self.r1),
                (self.c2, self.r2), (self.c1, self.r2)]


# --------------------------------------------------------------------------- 路线生成


@dataclass
class RectRouteResult:
    polyline: List[Point]
    ordered_checkpoints: List[Dict]
    length_m: float
    target_m: float
    ring: Optional[Ring]
    legs: Dict[str, float] = field(default_factory=dict)
    corners: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return ("直角路线 %.1f m / 目标 %d m｜拐角 %d 个（直角 %d，占比 %.0f%%）"
                "｜U 形掉头 %d" % (
                    self.length_m, self.target_m, self.corners.get("corners", 0),
                    self.corners.get("right", 0),
                    100.0 * self.corners.get("right_ratio", 0.0),
                    self.corners.get("u_turns", 0)))


class RectRouteBuilder:
    """在校园栅格上给当次官方 5 点铺一条直角闭环。"""

    def __init__(self, campus_id: str = CAMPUS_ID, target_m: int = 3000,
                 log=None, center_weight: float = 0.75,
                 fence: Optional[Sequence[Point]] = None):
        self.planner = GridPlanner(campus_id)
        if fence:
            self.planner.set_fence(fence)
        self.campus_id = campus_id
        self.target_m = int(target_m)
        self.log = log or (lambda message: None)
        # 矩环中心：以校园中心为主，稍微往官方点那边偏一点，避免接入段过长。
        self.center_weight = max(0.0, min(1.0, float(center_weight)))

    def ring_center(self, points: Sequence[Point]) -> Point:
        """矩环中心；center_weight=0.75 表示 75% 校园中心 + 25% 官方点质心。"""
        campus_lat, campus_lon = self.planner.campus.center
        lat = sum(p[0] for p in points) / len(points)
        lon = sum(p[1] for p in points) / len(points)
        w = self.center_weight
        return (campus_lat * w + lat * (1 - w), campus_lon * w + lon * (1 - w))

    # -- 官方点解析 -------------------------------------------------------
    @staticmethod
    def _coord(point: Dict) -> Point:
        lat = point.get("glat", point.get("lat"))
        lon = point.get("glon", point.get("glng", point.get("lon", point.get("lng"))))
        if lat is None or lon is None:
            raise RectRouteError("rect_bad_checkpoint", "打卡点缺少 glat/glon")
        return float(lat), float(lon)

    def order_checkpoints(self, raw: Sequence[Dict]) -> List[Dict]:
        """按围绕质心的极角排序，得到一个不自交的内圈遍历顺序。"""
        items = []
        for index, point in enumerate(raw):
            lat, lon = self._coord(point)
            items.append((index, point, lat, lon))
        lat0 = sum(item[2] for item in items) / len(items)
        lon0 = sum(item[3] for item in items) / len(items)
        cos_lat = math.cos(math.radians(lat0))
        enriched = []
        for index, point, lat, lon in items:
            angle = math.atan2(lat - lat0, (lon - lon0) * cos_lat)
            enriched.append((angle, index, point))
        enriched.sort(key=lambda item: item[0])
        return [dict(item[2]) for item in enriched]

    # -- 环尺寸 -----------------------------------------------------------
    def _ring_for(self, centroid: Point, scale: float) -> Optional[Ring]:
        grid = self.planner
        cc, cr = grid.nearest_cell(*centroid, radius_m=60.0)
        budget = float(self.target_m)
        half_sum = budget / 4.0 * 1.06  # 周长 4*(hw+hh)
        aspect = 1.5
        hw = int(round(half_sum * scale * aspect / (1 + aspect) / grid.res))
        hh = int(round(half_sum * scale / (1 + aspect) / grid.res))
        if hw < 6 or hh < 6:
            return None
        c1, c2 = max(2, cc - hw), min(grid.width - 3, cc + hw)
        r1, r2 = max(2, cr - hh), min(grid.height - 3, cr + hh)
        # 环的四个角吸到最近可走格，保证 A* 有起点
        corners = []
        for cell in ((c1, r1), (c2, r1), (c2, r2), (c1, r2)):
            try:
                corners.append(grid.nearest_cell(*grid.cell_point(*cell), radius_m=30.0))
            except RectRouteError:
                corners = []
                break
        if not corners:
            return None
        cols = [c[0] for c in corners]
        rows = [c[1] for c in corners]
        ring = Ring(min(cols), max(cols), min(rows), max(rows), grid.res)
        if ring.width_m < 40 or ring.height_m < 40:
            return None
        return ring

    # -- 组装 -------------------------------------------------------------
    def _assemble(self, points: List[Point], ring: Optional[Ring],
                  inner_len: float, legs: Dict[str, float]) -> List[Point]:
        if ring is None:
            route: List[Point] = [points[0]]
            for index in range(len(points)):
                leg = self.planner.connect(points[index], points[(index + 1) % len(points)])
                legs["cp%d→cp%d" % (index + 1, (index + 1) % len(points) + 1)] = polyline_length(leg)
                route += leg[1:]
            return route

        corners = [self.planner.cell_point(*cell) for cell in ring.corners()]
        dists = [self.planner.nearest_cell(*corner) for corner in corners]
        entry_index = 0
        entry_cell = dists[0]
        # 选离第一个打卡点最近的角作为入口
        best = None
        for index, corner in enumerate(corners):
            gap = abs(self.planner.xy(*corner)[0] - self.planner.xy(*points[0])[0]) + \
                  abs(self.planner.xy(*corner)[1] - self.planner.xy(*points[0])[1])
            if best is None or gap < best[0]:
                best = (gap, index)
        entry_index = best[1]
        order = [corners[(entry_index + i) % 4] for i in range(4)] + [corners[entry_index]]

        lead_in = self.planner.connect(points[0], order[0])
        legs["接入"] = polyline_length(lead_in)
        ring_route: List[Point] = [order[0]]
        ring_len = 0.0
        for a, b in zip(order, order[1:]):
            leg = self.planner.connect(a, b)
            ring_len += polyline_length(leg)
            ring_route += leg[1:]
        legs["矩环"] = ring_len
        lead_out = self.planner.connect(order[-1], points[1])
        legs["引出"] = polyline_length(lead_out)
        legs["内圈"] = inner_len

        route = [points[0]] + lead_in[1:] + ring_route[1:] + lead_out[1:]
        for index in range(1, len(points)):
            leg = self.planner.connect(points[index], points[(index + 1) % len(points)])
            route += leg[1:]
        return route

    def build(self, raw_checkpoints: Sequence[Dict], verify: bool = True) -> RectRouteResult:
        if len(raw_checkpoints) < 2:
            raise RectRouteError("rect_bad_checkpoint", "官方点位不足 2 个")
        ordered = self.order_checkpoints(raw_checkpoints)
        points = [self._coord(point) for point in ordered]
        centroid = self.ring_center(points)
        self.log("矩环中心: (%.6f, %.6f)（校园中心 %.0f%% + 官方点质心 %.0f%%）"
                 % (centroid[0], centroid[1],
                    100 * self.center_weight, 100 * (1 - self.center_weight)))

        legs: Dict[str, float] = {}
        inner: List[Point] = [points[0]]
        for index in range(len(points)):
            leg = self.planner.connect(points[index], points[(index + 1) % len(points)])
            legs["cp%d→cp%d" % (index + 1, (index + 1) % len(points) + 1)] = polyline_length(leg)
            inner += leg[1:]
        inner_len = polyline_length(inner)

        best_route = inner
        best_ring = None
        best_len = inner_len
        cache: Dict[float, Tuple[Optional[List[Point]], Optional[Ring], Dict[str, float], float]] = {}

        def finalize(raw_route: Sequence[Point]) -> List[Point]:
            cleaned = simplify_collinear(raw_route)
            cleaned = drop_small_spurs(cleaned, points, max_spur_m=70.0,
                                       blocked=self.planner.blocked_samples)
            out: List[Point] = []
            for point in cleaned:
                if not out or distance(out[-1], point) > 1e-6:
                    out.append(point)
            return out

        def trial(scale: float):
            key = round(scale, 4)
            if key in cache:
                return cache[key]
            ring = self._ring_for(centroid, scale)
            if ring is None:
                cache[key] = (None, None, {}, 0.0)
                return cache[key]
            trial_legs: Dict[str, float] = {}
            try:
                route = self._assemble(points, ring, inner_len, trial_legs)
                route = finalize(route)
            except RectRouteError as exc:
                self.log("矩环 %.2f 组装失败: %s" % (scale, exc.message))
                cache[key] = (None, None, {}, 0.0)
                return cache[key]
            length = polyline_length(route)
            self.log("矩环候选 %.2f: %.0fx%.0f m 周长 %.0f m → 路线 %.1f m"
                     % (scale, ring.width_m, ring.height_m, ring.perimeter_m, length))
            cache[key] = (route, ring, trial_legs, length)
            return cache[key]

        scales = [0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        fits: List[float] = []
        for scale in scales:
            route, ring, trial_legs, length = trial(scale)
            if route is None:
                continue
            if length <= self.target_m + 0.001:
                fits.append(scale)
                if length > best_len + 0.5:
                    best_route, best_ring, best_len = route, ring, length
                    legs = trial_legs
        if fits:
            lo = max(fits)
            bigger = [s for s in scales if s > lo]
            if bigger:
                hi = min(bigger)
                for _ in range(5):
                    mid = (lo + hi) / 2.0
                    route, ring, trial_legs, length = trial(mid)
                    if route is None:
                        break
                    if length <= self.target_m + 0.001:
                        lo = mid
                        if length > best_len + 0.5:
                            best_route, best_ring, best_len = route, ring, length
                            legs = trial_legs
                    else:
                        hi = mid
        if best_ring is None:
            self.log("矩环: 没有可用尺寸，只用官方点内圈 %.1f m" % inner_len)
        else:
            self.log("矩环定稿: %.0fx%.0f m 周长 %.0f m，路线 %.1f m"
                     % (best_ring.width_m, best_ring.height_m,
                        best_ring.perimeter_m, best_len))

        route = finalize(best_route)
        length = polyline_length(route)
        result = RectRouteResult(
            polyline=route,
            ordered_checkpoints=ordered,
            length_m=length,
            target_m=self.target_m,
            ring=best_ring,
            legs=legs,
            corners=right_angle_stats(route),
        )
        missing = [i + 1 for i, point in enumerate(points)
                   if not any(distance(point, vertex) <= 0.1 for vertex in route)]
        if missing:
            raise RectRouteError("rect_checkpoint_missing",
                                 "路线没有精确经过打卡点 %s" % missing)
        if length > self.target_m + 0.001:
            raise RectRouteError("rect_route_too_long",
                                 "折线 %.1f m 超过目标 %d m" % (length, self.target_m))
        if verify:
            self.verify(route, raw_checkpoints)
        return result

    def verify(self, route: Sequence[Point], raw_checkpoints: Sequence[Dict]) -> None:
        """用协议同一套栅格校验（穿建筑/越界），按 1 m 采样。"""
        planner = TrackPlanner.from_campus(self.campus_id)
        allowed = set()
        for point in raw_checkpoints:
            lat, lon = self._coord(point)
            building = planner.campus.building_at(lat, lon)
            if building is not None:
                allowed.add(int(building.osm_id))
        try:
            planner.validate_track([(lat, lon) for lat, lon in route],
                                   allow_building_ids=allowed)
        except Exception as exc:  # noqa: BLE001
            raise RectRouteError("rect_track_invalid", str(exc)) from None


# --------------------------------------------------------------------------- 预演图


def render_preview(result: RectRouteResult, path: str,
                   checkpoint_names: Optional[Sequence[str]] = None,
                   scale: int = 2) -> str:
    """把校园栅格 + 建筑 + 边界 + 直角路线画成 PNG，肉眼确认拐角。"""
    from PIL import Image, ImageDraw

    planner = GridPlanner()
    grid = planner.grid
    width, height = grid.width * scale, grid.height * scale
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)

    colors = {
        CELL_ROAD: (232, 226, 210),
        CELL_NEAR_ROAD: (243, 240, 230),
        CELL_OPEN: (250, 250, 246),
        CELL_NEAR_BUILDING: (236, 236, 226),
        CELL_BLOCKED: (196, 202, 208),
    }
    for row in range(grid.height):
        for col in range(grid.width):
            value = grid.cost(col, row)
            draw.rectangle(
                [col * scale, (grid.height - 1 - row) * scale,
                 (col + 1) * scale - 1, (grid.height - row) * scale - 1],
                fill=colors.get(value, (255, 255, 255)))

    def to_px(lat: float, lon: float) -> Tuple[float, float]:
        x, y = planner.xy(lat, lon)
        return x / grid.resolution_m * scale, (grid.height - 1 - y / grid.resolution_m) * scale

    for building in planner.campus.buildings:
        polygon = [to_px(lat, lon) for lat, lon in building.polygon]
        if len(polygon) >= 3:
            draw.polygon(polygon, outline=(200, 90, 90))
    boundary = [to_px(lat, lon) for lat, lon in planner.campus.boundary]
    draw.line(boundary + [boundary[0]], fill=(40, 140, 70), width=2)
    route_px = [to_px(lat, lon) for lat, lon in result.polyline]
    draw.line(route_px, fill=(240, 120, 0), width=3, joint="curve")
    for index in range(1, len(result.polyline) - 1):
        px, py = to_px(*result.polyline[index])
        draw.ellipse([px - 3, py - 3, px + 3, py + 3], fill=(200, 40, 40))
    for index, point in enumerate(result.ordered_checkpoints):
        lat, lon = RectRouteBuilder._coord(point)
        px, py = to_px(lat, lon)
        draw.ellipse([px - 6, py - 6, px + 6, py + 6], outline=(20, 60, 200), width=2)
        name = (checkpoint_names or [])[index] if checkpoint_names else str(index + 1)
        draw.text((px + 8, py - 6), str(name), fill=(20, 60, 200))

    target = os.path.abspath(path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    image.save(target)
    return target


# --------------------------------------------------------------------------- 采样模拟


@dataclass
class _SimPoint:
    lat: float
    lon: float
    time: int
    speed: float = 0.0


@dataclass
class _SimFixed:
    lat: float
    lon: float

    def route_coordinates(self):
        return (self.lat, self.lon)


def simulate(route: Sequence[Point], target_m: int, time_sec: int,
             checkpoints: Sequence[Dict]) -> Dict[str, object]:
    """不上网，按协议同一套采样 + 校验跑一遍（等价演练）。"""
    from running.consistency import check_track, sample_route

    fixed = []
    required = []
    for point in checkpoints:
        lat, lon = RectRouteBuilder._coord(point)
        fixed.append(_SimFixed(lat, lon))
        required.append((lat, lon))
    samples = sample_route(list(route), target_m, time_sec, 0,
                           require_all=True, seed=4, required_points=required)
    points = []
    for lat, lon, ts in samples:
        speed = 2.5
        points.append(_SimPoint(lat, lon, ts, speed))
    actual = check_track(points, target_m, time_sec, 0, time_sec * 1000, fixed)
    return {"points": len(samples), "actual_length_m": round(actual, 2),
            "duration_s": time_sec}


# --------------------------------------------------------------------------- CLI


def _load_points(path: str, library_id: str = "") -> List[Dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "entries" in data:
        entries = data["entries"]
        if library_id:
            for entry in entries:
                if str(entry.get("route_id", "")).startswith(library_id):
                    return list(entry.get("points") or [])
            raise SystemExit("路线库里找不到 %s" % library_id)
        return list(entries[0].get("points") or [])
    if isinstance(data, dict) and "points" in data:
        return list(data["points"])
    return list(data)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="校园跑 · 直角路线预演")
    parser.add_argument("--points", default=str(APP_DIR / "route_library.json"),
                        help="打卡点来源：路线库 JSON / 点集 JSON")
    parser.add_argument("--route-id", default="", help="从路线库里按 route_id 前缀取点")
    parser.add_argument("--target", type=int, default=3056, help="目标距离（米）")
    parser.add_argument("--time", type=int, default=1222, help="目标时长（秒）")
    parser.add_argument("--out", default=str(APP_DIR / "logs" / "rect_route_preview.png"))
    parser.add_argument("--no-simulate", action="store_true", help="跳过采样模拟")
    args = parser.parse_args(argv)

    points = _load_points(args.points, args.route_id)
    builder = RectRouteBuilder(target_m=args.target, log=print)
    result = builder.build(points)
    print(result.summary())
    print("分段:", {k: round(v, 1) for k, v in result.legs.items()})
    print("拐角统计:", result.corners)
    for warning in result.warnings:
        print("[警告]", warning)
    if not args.no_simulate:
        print("采样模拟:", simulate(result.polyline, args.target, args.time,
                                    result.ordered_checkpoints))
    png = render_preview(result, args.out)
    print("预演图:", png)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
