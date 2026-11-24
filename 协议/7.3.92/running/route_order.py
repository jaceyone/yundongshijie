# -*- coding: utf-8 -*-
"""官方 5 点排序：RunSequenceStartViewModel.calculateRouteWrapper 的 Python 移植。

官方只做「5 点排列 + 直线距离筛选」，不生成折线；折线仍由 OSM 规划器负责。
距离必须使用 ``glat/glon``（AMap GCJ-02），不能用 ``lat/lng``（BD-09），
否则会与官方地图上的点位错开约 911 m。
"""
import itertools
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .consistency import distance

# 官方 RunRuleModel.sequenceStrength == 1 时使用 dayGoal 窗口；
# 否则使用 minDistance 窗口（RunSequenceStartViewModel.java:104-139）。
STRENGTH_LOW_FACTOR = 0.6
STRENGTH_HIGH_FACTOR = 1.0
NORMAL_LOW_FACTOR = 0.8
NORMAL_HIGH_FACTOR = 1.2


@dataclass
class OrderedCheckpoints:
    """官方排序结果；points 已带 0 基 position。"""
    points: List[Dict[str, Any]]
    distance_m: float
    low_m: float
    high_m: float
    mode: str  # within / below / above / original


def _coord(point: Any) -> tuple:
    """取官方路线坐标：优先 glat/glon，缺失时回退 lat/lng。"""
    if isinstance(point, dict):
        lat = point.get("glat", point.get("lat"))
        lon = point.get("glon", point.get("lng", point.get("lon")))
    else:
        route = getattr(point, "route_coordinates", None)
        if callable(route):
            return route()
        glat = getattr(point, "gcj_coordinates", None)
        if callable(glat):
            return glat()
        lat = getattr(point, "lat", None)
        lon = getattr(point, "lon", getattr(point, "lng", None))
    if lat is None or lon is None:
        raise ValueError("checkpoint is missing glat/glon and lat/lng")
    return float(lat), float(lon)


def _path_distance(order: Sequence[Any]) -> float:
    """官方 getDistance：只累加相邻点，不闭合首尾。"""
    return sum(distance(_coord(a), _coord(b))
               for a, b in zip(order, order[1:]))


def _window(day_goal: float, min_distance: float,
            sequence_strength: int) -> tuple:
    """官方 calculateRouteWrapper 的 [low, high] 距离窗口。"""
    day_goal = float(day_goal)
    min_distance = float(min_distance)
    if int(sequence_strength) == 1:
        return STRENGTH_LOW_FACTOR * day_goal, STRENGTH_HIGH_FACTOR * day_goal
    if min_distance > 0:
        high = min(NORMAL_HIGH_FACTOR * min_distance, day_goal)
    else:
        high = NORMAL_HIGH_FACTOR * min_distance
    return NORMAL_LOW_FACTOR * min_distance, high


def select_official_order(checkpoints: Sequence[Any],
                          day_goal: float,
                          min_distance: float,
                          sequence_strength: int = 0,
                          rng: Optional[random.Random] = None) -> OrderedCheckpoints:
    """按官方规则选一个 5 点顺序。

    - 枚举全部排列，按 ``glat/glon`` 直线距离升序排序；
    - ``sequenceStrength == 1``：窗口 ``[0.6*dayGoal, 1.0*dayGoal]``；
    - 否则：窗口 ``[0.8*minDistance, min(1.2*minDistance, dayGoal)]``；
    - 优先窗口内随机；否则取低于窗口的最大值；再否则取高于窗口的最小值；
    - 最后回退原顺序。``position`` 按官方 ``calculateRoute`` 写成 0 基下标。
    """
    points = [dict(point) if isinstance(point, dict) else point
              for point in checkpoints]
    if len(points) < 2:
        raise ValueError("official route ordering needs at least two checkpoints")
    low, high = _window(day_goal, min_distance, sequence_strength)

    candidates = []
    for order in itertools.permutations(points):
        candidates.append((_path_distance(order), order))
    candidates.sort(key=lambda item: item[0])

    within = [item for item in candidates if low <= item[0] <= high]
    below = [item for item in candidates if item[0] < low]
    above = [item for item in candidates if item[0] > high]

    chooser = rng if rng is not None else random.Random()
    if within:
        distance_m, chosen = chooser.choice(within)
        mode = "within"
    elif below:
        distance_m, chosen = below[-1]
        mode = "below"
    elif above:
        distance_m, chosen = above[0]
        mode = "above"
    else:
        chosen = tuple(points)
        distance_m = _path_distance(chosen)
        mode = "original"

    ordered = []
    for index, point in enumerate(chosen):
        value = dict(point)
        value["position"] = index
        ordered.append(value)
    return OrderedCheckpoints(points=ordered, distance_m=float(distance_m),
                              low_m=float(low), high_m=float(high),
                              mode=mode)
