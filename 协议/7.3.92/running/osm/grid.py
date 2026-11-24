# -*- coding: utf-8 -*-
"""预处理代价栅格：运行时只读，负责坐标 ↔ 格子转换与吸附。

栅格由 `tools/build_osm_campus.py` 生成，存成 RLE（扫描线游程）以便
以较小体积打进 APK；载入时展开成 ``bytearray``。
"""
import json
import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

from .campus import (
    CampusData,
    OsmRouteError,
    LocalProjection,
    grid_data_filename,
    resolve_data_path,
)

# 与预处理脚本、方案文档 §4.4 保持一致。
CELL_BLOCKED = 255
CELL_ROAD = 1
CELL_NEAR_ROAD = 2
CELL_OPEN = 3
CELL_NEAR_BUILDING = 4

LEGEND = {
    str(CELL_BLOCKED): "blocked",
    str(CELL_ROAD): "road",
    str(CELL_NEAR_ROAD): "near_road",
    str(CELL_OPEN): "open",
    str(CELL_NEAR_BUILDING): "near_building",
}

Cell = Tuple[int, int]


class CostGrid:
    """等距投影下的规则栅格；cost < 255 才可走，数值即步行代价。"""

    def __init__(self, resolution_m: float, origin: Tuple[float, float],
                 width: int, height: int, costs: bytearray,
                 legend: Optional[Dict[str, str]] = None,
                 campus_id: str = ""):
        self.resolution_m = float(resolution_m)
        self.origin = (float(origin[0]), float(origin[1]))
        self.width = int(width)
        self.height = int(height)
        self.costs = costs
        self.legend = dict(legend or LEGEND)
        self.campus_id = campus_id
        self.projection = LocalProjection(self.origin)
        if len(costs) != self.width*self.height:
            raise ValueError("栅格数据长度与 width×height 不一致")

    # ---------- 载入 ----------

    @classmethod
    def from_dict(cls, data: Dict) -> "CostGrid":
        try:
            resolution = float(data["resolution_m"])
            origin = (float(data["origin"]["lat"]), float(data["origin"]["lon"]))
            width = int(data["width"])
            height = int(data["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"栅格元数据不完整: {exc}") from None
        total = width*height
        costs = bytearray(total)
        offset = 0
        for run in data.get("cost_runs") or []:
            try:
                value = int(run["value"])
                count = int(run["count"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"栅格游程格式错误: {exc}") from None
            if not 0 <= value <= 255 or count < 0 or offset+count > total:
                raise ValueError("栅格游程越界")
            if count:
                costs[offset:offset+count] = bytes([value])*count
                offset += count
        if offset != total:
            raise ValueError(f"栅格游程覆盖 {offset} 格，预期 {total} 格")
        return cls(resolution, origin, width, height, costs,
                   data.get("legend"), str(data.get("campus_id") or ""))

    @classmethod
    def load(cls, campus_id: str = "xuchang_280173698",
             data_dir: Optional[str] = None) -> "CostGrid":
        path = resolve_data_path(campus_id, grid_data_filename(campus_id), data_dir)
        if not os.path.isfile(path):
            raise OsmRouteError("osm_campus_not_found", f"找不到校园栅格数据: {path}")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return cls.from_dict(json.load(handle))
        except (OSError, ValueError) as exc:
            raise OsmRouteError("osm_campus_not_found",
                                f"校园栅格无法解析: {path}: {exc}") from None

    @classmethod
    def for_campus(cls, campus: CampusData, data_dir: Optional[str] = None) -> "CostGrid":
        return cls.load(campus.campus_id or "xuchang_280173698", data_dir)

    # ---------- 基本查询 ----------

    def in_bounds(self, col: int, row: int) -> bool:
        return 0 <= col < self.width and 0 <= row < self.height

    def cost(self, col: int, row: int) -> int:
        if not self.in_bounds(col, row):
            return CELL_BLOCKED
        return self.costs[row*self.width + col]

    def walkable(self, col: int, row: int) -> bool:
        return self.in_bounds(col, row) and self.costs[row*self.width + col] != CELL_BLOCKED

    def cell_center(self, col: int, row: int) -> Tuple[float, float]:
        return ((col+0.5)*self.resolution_m, (row+0.5)*self.resolution_m)

    def cell_latlon(self, col: int, row: int) -> Tuple[float, float]:
        return self.projection.latlon(*self.cell_center(col, row))

    def latlon_to_cell(self, lat: float, lon: float) -> Cell:
        x, y = self.projection.xy(lat, lon)
        return int(math.floor(x/self.resolution_m)), int(math.floor(y/self.resolution_m))

    def cell_distance_m(self, a: Cell, b: Cell) -> float:
        ax, ay = self.cell_center(*a)
        bx, by = self.cell_center(*b)
        return math.hypot(bx-ax, by-ay)

    def path_length_m(self, cells: Sequence[Cell]) -> float:
        return sum(self.cell_distance_m(a, b) for a, b in zip(cells, cells[1:]))

    # ---------- 吸附 ----------

    def nearest_walkable(self, lat: float, lon: float,
                         radius_m: float = 30.0) -> Optional[Tuple[int, int, float]]:
        """返回最近可走格及距离（米）；搜索半径外的建筑/围栏点返回 None。"""
        x, y = self.projection.xy(lat, lon)
        radius = max(0.0, float(radius_m))
        span = int(math.ceil(radius/self.resolution_m)) + 1
        base_col, base_row = self.latlon_to_cell(lat, lon)
        best = None
        best_distance = float("inf")
        for row in range(base_row-span, base_row+span+1):
            if not 0 <= row < self.height:
                continue
            for col in range(base_col-span, base_col+span+1):
                if not 0 <= col < self.width:
                    continue
                index = row*self.width + col
                if self.costs[index] == CELL_BLOCKED:
                    continue
                cx, cy = self.cell_center(col, row)
                distance = math.hypot(cx-x, cy-y)
                if distance > radius+1e-9:
                    continue
                if distance < best_distance:
                    best_distance = distance
                    best = (col, row, distance)
        return best

    def snap(self, lat: float, lon: float,
             radius_m: float = 30.0) -> Optional[Tuple[int, int, float]]:
        return self.nearest_walkable(lat, lon, radius_m=radius_m)

    def stats(self) -> Dict[str, int]:
        counts = {name: 0 for name in self.legend.values()}
        for value in self.costs:
            counts[self.legend.get(str(value), str(value))] = \
                counts.get(self.legend.get(str(value), str(value)), 0) + 1
        return counts
