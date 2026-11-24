# -*- coding: utf-8 -*-
"""
校园跑 · 手动路线库与校验原型

定位
  - 这是 `电脑一键启动` 侧的路线库/校验原型，不修改 `协议/`。
  - 路线格式：开放折线 ``[[lat, lon], ...]``（GCJ-02），不要求首尾闭合。
  - 路线短于目标时，`expand_polyline_to_distance()` 沿原路反向折返补齐，
    不生成手画折线之外的坐标；交给协议前折线长度已等于目标距离。
  - 5 个官方打卡点必须是路线上的精确顶点（默认 0.1 m），否则 `check_track`
    会报 “a checkpoint is missing from the track”。

用法
  python route_library.py --self-test
  python route_library.py --list
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
PROTOCOL_HOME = REPO_ROOT / "协议" / "7.3.92"
DEFAULT_LIBRARY_PATH = APP_DIR / "route_library.json"
DEFAULT_HISTORY_PATH = APP_DIR / "route_history.jsonl"

SCHEMA_VERSION = 1
DEFAULT_EPS_M = 5.0
DEFAULT_CHECKPOINT_TOLERANCE_M = 0.1
DEFAULT_MAX_LAPS = 8.0
DEFAULT_DENSE_VERTEX_SPACING_M = 3.0
MAX_ALLOWED_BUILDING_RUN_M = 30.0
EARTH_RADIUS_M = 6371000.0


# --------------------------------------------------------------------------- 几何


def _is_bad_coord(value) -> bool:
    if value is None or isinstance(value, bool):
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(number) or number == -1.0


def _coordinate_from_dict(point: Dict[str, Any]) -> Tuple[float, float, str]:
    """优先 GCJ-02（glat/glon）；缺失时回退原始 lat/lng，并标记为 raw。"""
    if not isinstance(point, dict):
        raise ValueError("point must be an object")
    lat = point.get("glat", point.get("gLat"))
    lon = point.get("glon", point.get("gLng", point.get("glng")))
    if not _is_bad_coord(lat) and not _is_bad_coord(lon):
        return float(lat), float(lon), "gcj"
    lat = point.get("lat", point.get("latitude"))
    lon = point.get("lng", point.get("lon", point.get("longitude")))
    if _is_bad_coord(lat) or _is_bad_coord(lon):
        raise ValueError("point has no usable coordinates")
    return float(lat), float(lon), "raw"


def normalize_point(point: Any, require_gcj: bool = True) -> Tuple[float, float]:
    """把官方点/tuple 转成 (lat, lon)；默认要求 GCJ。"""
    if isinstance(point, (list, tuple)):
        if len(point) != 2:
            raise ValueError("point tuple must be [lat, lon]")
        lat, lon = float(point[0]), float(point[1])
        if _is_bad_coord(lat) or _is_bad_coord(lon):
            raise ValueError("point tuple has invalid coordinates")
        return lat, lon
    lat, lon, frame = _coordinate_from_dict(point)
    if require_gcj and frame != "gcj":
        raise ValueError("checkpoint is missing glat/glon (GCJ-02)")
    return lat, lon


def normalize_points(points: Sequence[Any], require_gcj: bool = True) -> List[Tuple[float, float]]:
    return [normalize_point(point, require_gcj=require_gcj) for point in points]


def normalize_polyline(polyline: Sequence[Any]) -> List[Tuple[float, float]]:
    return normalize_points(polyline, require_gcj=False)


def distance_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, (float(a[0]), float(a[1])))
    lat2, lon2 = map(math.radians, (float(b[0]), float(b[1])))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return EARTH_RADIUS_M * 2 * math.asin(min(1.0, math.sqrt(max(0.0, h))))


def polyline_length(points: Sequence[Tuple[float, float]]) -> float:
    return sum(distance_m(a, b) for a, b in zip(points, points[1:]))


def interpolate_point(
    a: Tuple[float, float], b: Tuple[float, float], fraction: float
) -> Tuple[float, float]:
    fraction = max(0.0, min(1.0, float(fraction)))
    return (
        a[0] + (b[0] - a[0]) * fraction,
        a[1] + (b[1] - a[1]) * fraction,
    )


def traversal_points(
    polyline: Sequence[Any], target_distance_m: float
) -> List[Tuple[float, float]]:
    """返回实际会被走到的几何；与提交几何同源。

    直接复用 `expand_polyline_to_distance()`：路线短于目标时沿原路反向折返，
    一趟往返已经覆盖全部线段与两个掉头点，再多趟不会引入新的几何。
    """
    pts = normalize_polyline(polyline)
    if len(pts) < 2:
        return pts
    total = polyline_length(pts)
    if total <= 0:
        return pts
    try:
        target = float(target_distance_m)
    except (TypeError, ValueError):
        return pts
    if target <= 0:
        return pts
    return expand_polyline_to_distance(pts, min(target, total * 2.0))


def simplify_dense_vertices(
    polyline: Sequence[Any],
    checkpoints: Sequence[Any] = (),
    min_spacing_m: float = DEFAULT_DENSE_VERTEX_SPACING_M,
) -> List[Tuple[float, float]]:
    """去掉与上一个保留顶点间距小于 ``min_spacing_m`` 的顶点。

    整秒采样无法表示短于 1 秒的折点：保留会让配速出现尖峰，跳过会让弦长
    短于目标并触发补偿。保存前把这类手画噪声顶点去掉。官方打卡点和首尾
    顶点始终保留。
    """
    pts = normalize_polyline(polyline)
    if len(pts) < 3:
        return pts
    protected = set()
    for checkpoint in normalize_points(checkpoints, require_gcj=True):
        for index, point in enumerate(pts):
            if distance_m(checkpoint, point) <= DEFAULT_CHECKPOINT_TOLERANCE_M:
                protected.add(index)
    result = [pts[0]]
    last_index = len(pts) - 1
    for index in range(1, len(pts)):
        if index == last_index or index in protected:
            result.append(pts[index])
            continue
        if distance_m(result[-1], pts[index]) >= min_spacing_m:
            result.append(pts[index])
    return result


def checkpoint_vertex_indices(
    polyline: Sequence[Any],
    checkpoints: Sequence[Any],
    tolerance_m: float = DEFAULT_CHECKPOINT_TOLERANCE_M,
) -> Tuple[List[int], List[float]]:
    """返回每个打卡点对应的折线顶点下标（按打卡点输入顺序）。"""
    pts = normalize_polyline(polyline)
    cps = normalize_points(checkpoints, require_gcj=True)
    if not cps:
        raise ValueError("at least one checkpoint is required")
    if len(pts) < 2:
        raise ValueError("route needs at least two points")

    indices: List[int] = []
    distances: List[float] = []
    for checkpoint_index, checkpoint in enumerate(cps):
        best_index = -1
        best_distance = float("inf")
        for vertex_index, vertex in enumerate(pts):
            value = distance_m(checkpoint, vertex)
            if value < best_distance:
                best_distance = value
                best_index = vertex_index
        if best_index < 0 or best_distance > tolerance_m:
            raise ValueError(
                "checkpoint %d is %.2f m from the nearest route vertex "
                "(tolerance %.2f m); insert the exact checkpoint into the route"
                % (checkpoint_index + 1, best_distance, tolerance_m)
            )
        indices.append(best_index)
        distances.append(best_distance)
    return indices, distances


def _truncate_polyline(
    points: Sequence[Tuple[float, float]], target_distance_m: float
) -> List[Tuple[float, float]]:
    if not points:
        return []
    result = [points[0]]
    accumulated = 0.0
    for a, b in zip(points, points[1:]):
        segment = distance_m(a, b)
        if accumulated + segment >= target_distance_m:
            result.append(
                interpolate_point(
                    a, b, (target_distance_m - accumulated) / segment
                )
            )
            return result
        result.append(b)
        accumulated += segment
    return result


def expand_polyline_to_distance(
    polyline: Sequence[Any], target_distance_m: float
) -> List[Tuple[float, float]]:
    """把已画路线沿原路折返展开到目标距离。

    - 路线短于目标：``p0 → pn → p0 → pn …``，按路程精确停在目标距离；
    - 路线等于目标：原样返回；
    - 路线长于目标：截断到目标（调用方应先阻止这种路线）。

    除目标距离处的插值终点外，不产生手画折线之外的坐标。
    """
    pts = normalize_polyline(polyline)
    pts = [
        point
        for index, point in enumerate(pts)
        if index == 0 or distance_m(point, pts[index - 1]) > 1e-6
    ]
    if len(pts) < 2:
        return pts
    try:
        target = float(target_distance_m)
    except (TypeError, ValueError):
        return pts
    if target <= 0:
        return pts
    total = polyline_length(pts)
    if total <= 0:
        return pts
    if target <= total + 1e-9:
        return _truncate_polyline(pts, target)

    result = list(pts)
    need = target - total
    forward = False
    guard = 0
    while need > 1e-9 and guard < 200000:
        seq = pts[1:] if forward else list(reversed(pts[:-1]))
        for point in seq:
            segment = distance_m(result[-1], point)
            if segment <= 1e-9:
                continue
            if segment >= need:
                result.append(
                    interpolate_point(result[-1], point, need / segment)
                )
                return result
            result.append(point)
            need -= segment
            guard += 1
            if guard >= 200000:
                break
        forward = not forward
    return result


def checkpoint_order(
    polyline: Sequence[Any],
    checkpoints: Sequence[Any],
    tolerance_m: float = DEFAULT_CHECKPOINT_TOLERANCE_M,
) -> Tuple[List[int], List[float]]:
    """按路线顶点顺序返回打卡点下标；每个点必须是 0.1 m 内的顶点。"""
    indices, distances = checkpoint_vertex_indices(
        polyline, checkpoints, tolerance_m
    )
    pairs = sorted(
        ((vertex_index, checkpoint_index) for checkpoint_index, vertex_index
         in enumerate(indices)),
        key=lambda item: (item[0], item[1]),
    )
    order = [checkpoint_index for _, checkpoint_index in pairs]
    ordered_distances = [distances[checkpoint_index] for _, checkpoint_index in pairs]
    return order, ordered_distances


# --------------------------------------------------------------------------- 点位匹配


@dataclass
class MatchResult:
    max_distance_m: float
    mapping: List[int]  # current[i] <-> stored[mapping[i]]


def match_point_sets(
    current: Sequence[Any],
    stored: Sequence[Any],
    eps_m: float = DEFAULT_EPS_M,
) -> Optional[MatchResult]:
    """判断两组 5 点是否同一集合；顺序无关，要求双向最近匹配。"""
    try:
        a = normalize_points(current, require_gcj=True)
        b = normalize_points(stored, require_gcj=True)
    except ValueError:
        return None
    if not a or len(a) != len(b):
        return None

    n = len(a)
    if n <= 7:
        best: Optional[Tuple[float, List[int]]] = None
        for permutation in itertools.permutations(range(n)):
            worst = 0.0
            for index, stored_index in enumerate(permutation):
                value = distance_m(a[index], b[stored_index])
                if value > worst:
                    worst = value
                if worst > eps_m:
                    break
            else:
                if best is None or worst < best[0]:
                    best = (worst, list(permutation))
                if worst == 0.0:
                    break
        if best is None or best[0] > eps_m:
            return None
        return MatchResult(max_distance_m=best[0], mapping=best[1])

    # 点数较多时退化为贪心；官方的 5 点不会走到这里。
    used = set()
    mapping: List[int] = []
    worst = 0.0
    for point in a:
        candidates = [
            (distance_m(point, b[index]), index)
            for index in range(n)
            if index not in used
        ]
        if not candidates:
            return None
        value, index = min(candidates)
        if value > eps_m:
            return None
        used.add(index)
        mapping.append(index)
        worst = max(worst, value)
    return MatchResult(max_distance_m=worst, mapping=mapping)


def points_key(
    points: Sequence[Any],
    unid: Optional[Any],
    area_id: Optional[Any],
    decimals: int = 5,
) -> str:
    """点位集索引 key；仅供去重/展示，实际匹配仍用 EPS。"""
    coords = normalize_points(points, require_gcj=True)
    quantized = sorted(
        (round(lat, decimals), round(lon, decimals)) for lat, lon in coords
    )
    payload = "%s|%s|" % (
        int(unid or 0),
        int(area_id if area_id is not None else -1),
    )
    payload += ";".join(
        ("%.*f,%.*f" % (decimals, lat, decimals, lon)) for lat, lon in quantized
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def anchor_meta(anchor: Any) -> Optional[Dict[str, Any]]:
    """把锚点对象/字典规范化为路线库元数据；只用于追溯，不参与匹配。"""
    if not anchor:
        return None
    if isinstance(anchor, dict):
        ident = anchor.get("anchor_id", anchor.get("id"))
        name = anchor.get("anchor_name", anchor.get("name", ""))
        lat = anchor.get("anchor_glat", anchor.get("glat", anchor.get("lat")))
        lon = anchor.get("anchor_glon", anchor.get("glon", anchor.get("lon")))
        seed = anchor.get("seed", anchor.get("selection_seed", ""))
    else:
        ident = getattr(anchor, "id", getattr(anchor, "anchor_id", None))
        name = getattr(anchor, "name", getattr(anchor, "anchor_name", ""))
        lat = getattr(anchor, "glat", getattr(anchor, "anchor_glat", None))
        lon = getattr(anchor, "glon", getattr(anchor, "anchor_glon", None))
        seed = getattr(anchor, "seed", "")
    if ident is None and lat is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    return {
        "id": str(ident or ""),
        "name": str(name or ""),
        "glat": lat,
        "glon": lon,
        "seed": str(seed or ""),
    }


def _anchor_fields(anchor: Any) -> Dict[str, Any]:
    meta = anchor_meta(anchor)
    if not meta:
        return {}
    return {
        "anchor_id": meta["id"],
        "anchor_glat": meta["glat"],
        "anchor_glon": meta["glon"],
    }


# --------------------------------------------------------------------------- 校验


@dataclass
class RouteValidation:
    ok: bool
    length_m: float
    laps: float
    order: List[int] = field(default_factory=list)
    checkpoint_distances_m: List[float] = field(default_factory=list)
    allowed_building_ids: List[int] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    traversal_point_count: int = 0


def _format_osm_error(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    message = getattr(exc, "message", str(exc))
    return ("%s: %s" % (code, message)) if code else str(message)


def _max_run_inside_buildings(planner, track, allowed: set) -> float:
    """估算连续处于“允许建筑”内的最大距离，用于警告过长的接入通道。"""
    if planner is None or not allowed:
        return 0.0
    longest = 0.0
    current = 0.0
    for a, b in zip(track, track[1:]):
        segment = distance_m(a, b)
        if segment <= 0:
            continue
        steps = max(1, int(math.ceil(segment)))
        step_length = segment / steps
        for step in range(steps + 1):
            fraction = step / steps
            lat = a[0] + (b[0] - a[0]) * fraction
            lon = a[1] + (b[1] - a[1]) * fraction
            building = planner.campus.building_at(lat, lon)
            if building is not None and int(building.osm_id) in allowed:
                current += step_length
                longest = max(longest, current)
            else:
                current = 0.0
    return longest


def validate_route(
    polyline: Sequence[Any],
    checkpoints: Sequence[Any],
    target_distance_m: int,
    planner=None,
    allow_building_ids: Iterable[int] = (),
    max_laps: float = DEFAULT_MAX_LAPS,
    checkpoint_tolerance_m: float = DEFAULT_CHECKPOINT_TOLERANCE_M,
) -> RouteValidation:
    """校验手动路线；planner 为 None 时只做几何/距离/点位校验。"""
    errors: List[str] = []
    warnings: List[str] = []
    try:
        pts = normalize_polyline(polyline)
    except Exception as exc:
        return RouteValidation(ok=False, length_m=0.0, laps=0.0, errors=[str(exc)])

    if len(pts) < 2:
        errors.append("路线至少需要 2 个点")
    length = polyline_length(pts)
    if length <= 0:
        errors.append("路线长度必须大于 0")
    duplicate_vertices = sum(
        1 for a, b in zip(pts, pts[1:]) if distance_m(a, b) <= 1e-9
    )
    if duplicate_vertices:
        errors.append("路线有 %d 个连续重复顶点；请删除后保存" % duplicate_vertices)
    try:
        target = int(target_distance_m)
    except (TypeError, ValueError):
        target = 0
    if target <= 0:
        errors.append("目标距离必须是正整数")
    if length > target + 0.001:
        errors.append(
            "路线 %.1f m 长于目标 %d m；协议会报 target distance is too short"
            % (length, target)
        )
    laps = (target / length) if length > 0 else float("inf")
    if laps > max_laps:
        warnings.append(
            "目标距离需要约 %.1f 趟，轨迹会明显重复；建议画更长或调整目标"
            % laps
        )

    order: List[int] = []
    checkpoint_distances: List[float] = []
    try:
        order, checkpoint_distances = checkpoint_order(
            pts, checkpoints, checkpoint_tolerance_m
        )
        if len(set(order)) != len(order):
            warnings.append("有打卡点落在同一个顶点附近，经过顺序可能不唯一")
    except Exception as exc:
        errors.append(str(exc))

    allowed = {int(item) for item in allow_building_ids}
    traversal: List[Tuple[float, float]] = []
    if planner is not None:
        try:
            for checkpoint in normalize_points(checkpoints, require_gcj=True):
                building = planner.campus.building_at(checkpoint[0], checkpoint[1])
                if building is not None:
                    allowed.add(int(building.osm_id))
        except Exception as exc:
            warnings.append("无法计算打卡点所在建筑: %s" % exc)

        traversal = traversal_points(pts, target)
        try:
            planner.validate_track(traversal, allow_building_ids=sorted(allowed))
        except Exception as exc:
            errors.append(_format_osm_error(exc))
        try:
            corridor = _max_run_inside_buildings(planner, traversal, allowed)
            if corridor > MAX_ALLOWED_BUILDING_RUN_M:
                warnings.append(
                    "路线在打卡点建筑内连续走了约 %.1f m，超过建议的接入通道长度 %.0f m"
                    % (corridor, MAX_ALLOWED_BUILDING_RUN_M)
                )
        except Exception as exc:
            warnings.append("接入通道长度检查失败: %s" % exc)
    else:
        warnings.append("未加载校园数据，未检查建筑/边界")

    return RouteValidation(
        ok=not errors,
        length_m=length,
        laps=laps,
        order=order,
        checkpoint_distances_m=checkpoint_distances,
        allowed_building_ids=sorted(allowed),
        errors=errors,
        warnings=warnings,
        traversal_point_count=len(traversal),
    )


def load_planner(campus: str = "xuchang_280173698"):
    """按需加载协议侧的 TrackPlanner；失败时返回 None。"""
    if str(PROTOCOL_HOME) not in sys.path:
        sys.path.insert(0, str(PROTOCOL_HOME))
    try:
        from running.osm import TrackPlanner  # type: ignore
    except Exception:
        return None
    try:
        return TrackPlanner.from_campus(campus)
    except Exception:
        return None


# --------------------------------------------------------------------------- 路线库


@dataclass
class RouteEntry:
    route_id: str
    group_id: str
    points_key: str
    unid: int
    area_id: int
    campus: str
    points: List[Dict[str, Any]]
    order: List[int]
    polyline: List[List[float]]
    length_m: float
    vertex_indices: List[int] = field(default_factory=list)
    is_closed: bool = False
    status: str = "valid"
    source: str = "manual"
    notes: str = ""
    # 创建/最近一次保存时使用的取点锚点；只用于追溯，不参与匹配。
    anchor: Optional[Dict[str, Any]] = None
    created_at: str = ""
    last_used_at: str = ""
    last_validated_at: str = ""
    validation: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RouteEntry":
        fields = {
            "route_id",
            "group_id",
            "points_key",
            "unid",
            "area_id",
            "campus",
            "points",
            "order",
            "polyline",
            "length_m",
            "vertex_indices",
            "is_closed",
            "status",
            "source",
            "notes",
            "anchor",
            "created_at",
            "last_used_at",
            "last_validated_at",
            "validation",
            "schema_version",
        }
        return cls(**{key: raw[key] for key in fields if key in raw})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RouteMatch:
    entry: RouteEntry
    max_point_distance_m: float
    laps: float
    ordered_checkpoints: List[Dict[str, Any]]
    adjusted_polyline: List[List[float]]


class RouteLibrary:
    """JSON 路线库；同一点位集允许存多条不同长度的路线。"""

    def __init__(
        self,
        path: Optional[Path] = None,
        history_path: Optional[Path] = None,
        eps_m: float = DEFAULT_EPS_M,
    ):
        self.path = Path(path or DEFAULT_LIBRARY_PATH)
        self.history_path = Path(history_path or DEFAULT_HISTORY_PATH)
        self.eps_m = float(eps_m)
        self.entries: List[RouteEntry] = []
        self.load()

    def load(self) -> None:
        self.entries = []
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("路线库读取失败: %s" % exc) from exc
        values = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(values, list):
            raise ValueError("路线库格式错误：缺少 entries 数组")
        for value in values:
            if not isinstance(value, dict):
                continue
            try:
                self.entries.append(RouteEntry.from_dict(value))
            except TypeError:
                continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": _now(),
            "entries": [entry.to_dict() for entry in self.entries],
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def _append_history(self, event: Dict[str, Any]) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(event)
        record.setdefault("timestamp", _now())
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _same_namespace(self, entry: RouteEntry, unid, area_id) -> bool:
        if unid is not None and int(unid) > 0 and int(entry.unid) > 0:
            if int(entry.unid) != int(unid):
                return False
        if area_id is not None:
            wanted = int(area_id)
            if wanted >= 0 and int(entry.area_id) >= 0 and int(entry.area_id) != wanted:
                return False
        return True

    def _build_match(
        self,
        entry: RouteEntry,
        points: Sequence[Any],
        target_distance_m: int,
        planner=None,
        revalidate: bool = True,
        max_laps: float = DEFAULT_MAX_LAPS,
    ) -> Optional[RouteMatch]:
        """把当次点位 rebase 进条目；结构不可复用或复核失败时返回 None。"""
        result = match_point_sets(points, entry.points, self.eps_m)
        if result is None:
            return None

        stored_to_current = {
            stored_index: current_index
            for current_index, stored_index in enumerate(result.mapping)
        }
        ordered = []
        try:
            for checkpoint_index in entry.order:
                ordered.append(points[stored_to_current[checkpoint_index]])
        except (KeyError, IndexError):
            return None

        # 复用前 rebase：把当次点位坐标替换进旧折线的对应顶点。
        # 官方点位可能有几米抖动；不替换的话，check_track 的 0.1 m
        # 精度会直接失败。
        try:
            vertex_indices = list(entry.vertex_indices)
            if len(vertex_indices) != len(entry.points):
                vertex_indices, _ = checkpoint_vertex_indices(
                    entry.polyline, entry.points
                )
            adjusted = [list(point) for point in entry.polyline]
            touched = set()
            for stored_index, vertex_index in enumerate(vertex_indices):
                current_index = stored_to_current.get(stored_index)
                if current_index is None:
                    raise ValueError("checkpoint mapping is incomplete")
                if vertex_index in touched:
                    raise ValueError("two checkpoints map to the same vertex")
                touched.add(vertex_index)
                lat, lon = normalize_point(points[current_index], require_gcj=True)
                adjusted[vertex_index] = [lat, lon]
        except (ValueError, IndexError):
            return None

        if revalidate and planner is not None:
            report = validate_route(
                adjusted,
                points,
                target_distance_m,
                planner=planner,
                max_laps=max_laps,
            )
            if not report.ok:
                return None

        length = float(entry.length_m)
        laps = (int(target_distance_m) / length) if length > 0 else float("inf")
        return RouteMatch(
            entry=entry,
            max_point_distance_m=result.max_distance_m,
            laps=laps,
            ordered_checkpoints=ordered,
            adjusted_polyline=adjusted,
        )

    def find(
        self,
        points: Sequence[Any],
        unid: Optional[Any],
        area_id: Optional[Any],
        target_distance_m: int,
        max_laps: float = DEFAULT_MAX_LAPS,
        planner=None,
        revalidate: bool = True,
    ) -> List[RouteMatch]:
        """严格模式：状态/长度/趟数/复核全部通过才算可用路线。"""
        matches: List[RouteMatch] = []
        for entry in self.entries:
            if entry.status != "valid":
                continue
            if not self._same_namespace(entry, unid, area_id):
                continue
            if entry.length_m <= 0 or entry.length_m > int(target_distance_m) + 0.001:
                continue
            laps = int(target_distance_m) / entry.length_m
            if laps > max_laps:
                continue
            match = self._build_match(
                entry, points, target_distance_m, planner, revalidate, max_laps
            )
            if match is not None:
                matches.append(match)

        matches.sort(
            key=lambda item: (
                item.laps,
                _sort_time(item.entry.last_used_at),
                _sort_time(item.entry.created_at),
            )
        )
        return matches

    @staticmethod
    def _relaxed_rank(match: RouteMatch, target_distance_m: int):
        length = float(match.entry.length_m)
        usable = 0 < length <= int(target_distance_m) + 0.001
        return (
            0 if usable else 1,
            (int(target_distance_m) / length) if usable else (length - target_distance_m),
            match.max_point_distance_m,
        )

    def find_by_points(
        self,
        points: Sequence[Any],
        unid: Optional[Any],
        area_id: Optional[Any],
        target_distance_m: int,
    ) -> List[RouteMatch]:
        """宽松模式：只按 5 点位置命中。

        不再要求 status=valid、长度不超过目标、趟数 <= 8 或重新校验通过；
        仍要求命名空间一致、5 点在 EPS 内且能把当次坐标 rebase 进旧折线。
        多个变体时：长度装得下目标的优先，其次趟数少，再按点位偏差小、
        最近使用、最近创建排序。路线是否可用由调用方按目标距离判断。
        """
        target = max(1, int(target_distance_m))
        matches: List[RouteMatch] = []
        for entry in self.entries:
            if entry.length_m <= 0:
                continue
            if not self._same_namespace(entry, unid, area_id):
                continue
            match = self._build_match(
                entry, points, target, planner=None, revalidate=False
            )
            if match is not None:
                matches.append(match)

        # 稳定排序：先按最近使用倒序，再按可用性/趟数/点位偏差正序。
        matches.sort(
            key=lambda item: (
                _sort_time(item.entry.last_used_at),
                _sort_time(item.entry.created_at),
            ),
            reverse=True,
        )
        matches.sort(key=lambda item: self._relaxed_rank(item, target))
        return matches

    def add(
        self,
        points: Sequence[Dict[str, Any]],
        polyline: Sequence[Any],
        unid: Optional[Any],
        area_id: Optional[Any],
        campus: str,
        order: Optional[Sequence[int]] = None,
        notes: str = "",
        validation: Optional[RouteValidation] = None,
        anchor: Any = None,
    ) -> RouteEntry:
        normalized_points = normalize_points(points, require_gcj=True)
        pts = normalize_polyline(polyline)
        if len(pts) < 2:
            raise ValueError("route needs at least two points")
        if order is None:
            order, _ = checkpoint_order(pts, points)
        order = [int(item) for item in order]
        try:
            vertex_indices, _ = checkpoint_vertex_indices(pts, points)
        except ValueError:
            vertex_indices = []

        group_id = ""
        for entry in self.entries:
            if not self._same_namespace(entry, unid, area_id):
                continue
            if match_point_sets(points, entry.points, self.eps_m) is not None:
                group_id = entry.group_id
                break
        key = points_key(points, unid, area_id)
        if not group_id:
            group_id = key

        length = polyline_length(pts)
        closed = len(pts) >= 2 and distance_m(pts[0], pts[-1]) <= 1e-6
        now = _now()
        entry = RouteEntry(
            route_id=uuid.uuid4().hex,
            group_id=group_id,
            points_key=key,
            unid=int(unid or 0),
            area_id=int(area_id if area_id is not None else -1),
            campus=str(campus or ""),
            points=[dict(point) for point in points],
            order=order,
            polyline=[[float(lat), float(lon)] for lat, lon in pts],
            length_m=length,
            vertex_indices=vertex_indices,
            is_closed=closed,
            status="valid" if (validation is None or validation.ok) else "needs_review",
            source="manual",
            notes=str(notes or ""),
            anchor=anchor_meta(anchor),
            created_at=now,
            last_used_at="",
            last_validated_at=now if validation is not None and validation.ok else "",
            validation=asdict(validation) if validation is not None else {},
        )
        self.entries.append(entry)
        self.save()
        self._append_history(
            {
                "event": "add",
                "route_id": entry.route_id,
                "group_id": entry.group_id,
                "points_key": entry.points_key,
                "length_m": round(entry.length_m, 2),
                "status": entry.status,
                "target_distance_m": None,
                **_anchor_fields(anchor),
            }
        )
        return entry

    def get(self, route_id: str) -> Optional[RouteEntry]:
        for entry in self.entries:
            if entry.route_id == route_id:
                return entry
        return None

    def set_notes(self, route_id: str, notes: str) -> bool:
        """只改备注，不重算几何/校验状态。"""
        entry = self.get(route_id)
        if entry is None:
            return False
        entry.notes = str(notes or "")
        self.save()
        self._append_history(
            {
                "event": "notes",
                "route_id": entry.route_id,
                "group_id": entry.group_id,
                "points_key": entry.points_key,
                "notes": entry.notes,
            }
        )
        return True

    def update(
        self,
        route_id: str,
        polyline: Sequence[Any],
        points: Optional[Sequence[Dict[str, Any]]] = None,
        notes: Optional[str] = None,
        validation: Optional[RouteValidation] = None,
        anchor: Any = None,
    ) -> RouteEntry:
        """用编辑后的折线覆盖已有条目，并重算长度/顺序/顶点映射。

        点位集默认沿用条目里保存的 5 点；编辑器保存时总会把当前点位传进来。
        """
        entry = self.get(route_id)
        if entry is None:
            raise ValueError("路线不存在: %s" % route_id)
        pts = normalize_polyline(polyline)
        if len(pts) < 2:
            raise ValueError("route needs at least two points")
        route_points = (
            [dict(point) for point in points]
            if points is not None
            else [dict(point) for point in entry.points]
        )
        order, _ = checkpoint_order(pts, route_points)
        try:
            vertex_indices, _ = checkpoint_vertex_indices(pts, route_points)
        except ValueError:
            vertex_indices = []

        entry.points = route_points
        entry.polyline = [[float(lat), float(lon)] for lat, lon in pts]
        entry.length_m = polyline_length(pts)
        entry.order = [int(item) for item in order]
        entry.vertex_indices = vertex_indices
        entry.is_closed = len(pts) >= 2 and distance_m(pts[0], pts[-1]) <= 1e-6
        if notes is not None:
            entry.notes = str(notes)
        if anchor is not None:
            entry.anchor = anchor_meta(anchor)
        if validation is not None:
            entry.validation = asdict(validation)
            entry.status = "valid" if validation.ok else "needs_review"
            entry.last_validated_at = _now() if validation.ok else entry.last_validated_at
        self.save()
        self._append_history(
            {
                "event": "update",
                "route_id": entry.route_id,
                "group_id": entry.group_id,
                "points_key": entry.points_key,
                "length_m": round(entry.length_m, 2),
                "status": entry.status,
                **_anchor_fields(anchor),
            }
        )
        return entry

    def delete(self, route_id: str) -> bool:
        for index, entry in enumerate(self.entries):
            if entry.route_id != route_id:
                continue
            removed = self.entries.pop(index)
            self.save()
            self._append_history(
                {
                    "event": "delete",
                    "route_id": removed.route_id,
                    "group_id": removed.group_id,
                    "points_key": removed.points_key,
                    "length_m": round(removed.length_m, 2),
                }
            )
            return True
        return False

    def mark_used(self, route_id: str, target_distance_m: Optional[int] = None,
                  anchor: Any = None) -> None:
        now = _now()
        for entry in self.entries:
            if entry.route_id == route_id:
                entry.last_used_at = now
                entry.last_validated_at = now
                self.save()
                self._append_history(
                    {
                        "event": "use",
                        "route_id": route_id,
                        "group_id": entry.group_id,
                        "points_key": entry.points_key,
                        "length_m": round(entry.length_m, 2),
                        "target_distance_m": target_distance_m,
                        **_anchor_fields(anchor),
                    }
                )
                return

    def record_miss(
        self,
        points: Sequence[Any],
        unid: Optional[Any],
        area_id: Optional[Any],
        target_distance_m: int,
        reason: str = "no_match",
        anchor: Any = None,
    ) -> None:
        try:
            key = points_key(points, unid, area_id)
        except ValueError:
            key = ""
        self._append_history(
            {
                "event": "miss",
                "points_key": key,
                "target_distance_m": target_distance_m,
                "reason": reason,
                **_anchor_fields(anchor),
            }
        )

    def list_entries(self) -> List[Dict[str, Any]]:
        result = []
        for entry in self.entries:
            result.append(
                {
                    "route_id": entry.route_id,
                    "group_id": entry.group_id,
                    "unid": entry.unid,
                    "area_id": entry.area_id,
                    "length_m": round(entry.length_m, 1),
                    "order": list(entry.order),
                    "status": entry.status,
                    "notes": entry.notes,
                    "created_at": entry.created_at,
                    "last_used_at": entry.last_used_at,
                }
            )
        return result


def _sort_time(value: str) -> str:
    # ISO 时间字符串可直接比较；空值排最后。
    return value or "0000-00-00T00:00:00"


# --------------------------------------------------------------------------- 自检


def _find_test_segment(planner) -> Tuple[List[Tuple[float, float]], float]:
    """在打包校园数据里找一段 5 点、长度适中的可走道路，供离线测试。"""
    campus_path = PROTOCOL_HOME / "running" / "data" / "campus_xuchang_280173698.json"
    raw = json.loads(campus_path.read_text(encoding="utf-8"))
    for road in raw.get("roads", []):
        polyline = [
            (float(lat), float(lon)) for lat, lon in road.get("polyline", [])
        ]
        for index in range(len(polyline) - 4):
            segment = polyline[index : index + 5]
            length = polyline_length(segment)
            if length < 80 or length > 800:
                continue
            try:
                planner.validate_track(segment)
            except Exception:
                continue
            return segment, length
    raise RuntimeError("找不到可用于自检的校园道路段")


def _self_test() -> int:
    failures: List[str] = []

    def check(name, function):
        try:
            function()
            print("[PASS] %s" % name)
        except Exception as exc:
            print("[FAIL] %s: %s" % (name, exc))
            failures.append(name)

    base_points = [
        {"glat": 34.040000, "glon": 113.860000, "name": "P1"},
        {"glat": 34.040500, "glon": 113.860000, "name": "P2"},
        {"glat": 34.040500, "glon": 113.860500, "name": "P3"},
        {"glat": 34.040000, "glon": 113.860500, "name": "P4"},
        {"glat": 34.039700, "glon": 113.860200, "name": "P5"},
    ]

    def test_key_order():
        forward = points_key(base_points, 17601, 8)
        backward = points_key(list(reversed(base_points)), 17601, 8)
        assert forward == backward, "key should be order-insensitive"

    def test_match_eps():
        shifted = [dict(point) for point in base_points]
        shifted[0]["glat"] += 0.000027  # 约 3 m
        result = match_point_sets(base_points, shifted, eps_m=5.0)
        assert result is not None, "3 m shift should match"
        shifted[0]["glat"] += 0.00018  # 再约 20 m
        assert match_point_sets(base_points, shifted, eps_m=5.0) is None

    def point_to_polyline_m(point, polyline):
        """点到折线的最小距离；自检用，不参与生产逻辑。"""
        cos_lat = math.cos(math.radians(point[0]))
        px, py = point[1] * cos_lat, point[0]
        best = float("inf")
        for a, b in zip(polyline, polyline[1:]):
            ax, ay = a[1] * cos_lat, a[0]
            bx, by = b[1] * cos_lat, b[0]
            dx, dy = bx - ax, by - ay
            length_sq = dx * dx + dy * dy
            if length_sq <= 1e-18:
                value = math.hypot(px - ax, py - ay)
            else:
                t = max(
                    0.0,
                    min(
                        1.0,
                        ((px - ax) * dx + (py - ay) * dy) / length_sq,
                    ),
                )
                value = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            best = min(best, value)
        return best * math.radians(1.0) * EARTH_RADIUS_M

    def test_traversal():
        # 非共线三点：两点共线时分不清直线返回与折返。
        line = [(0.0, 0.0), (0.0, 0.001), (0.0004, 0.0012)]
        length = polyline_length(line)
        assert length > 100
        walked = traversal_points(line, length + 50)
        assert len(walked) > 3
        # 目标比路线长 50 m：先正向走完，再沿原路反向折返 50 m。
        assert abs(distance_m(walked[-1], line[-1]) - 50) < 1
        assert abs(distance_m(walked[-1], line[0]) - (length - 50)) < 1
        assert all(
            point_to_polyline_m(point, line) <= 0.01 for point in walked
        )
        assert walked == traversal_points(line, min(length + 50, 2 * length))
        assert walked == expand_polyline_to_distance(line, min(length + 50, 2 * length))

    def route_with_offset(points, offset):
        coords = [(float(p["glat"]), float(p["glon"])) for p in points]
        if offset <= 0:
            return coords
        route = []
        for a, b in zip(coords, coords[1:]):
            route.append(a)
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            dlat, dlon = b[0] - a[0], b[1] - a[1]
            norm = math.hypot(dlat, dlon) or 1.0
            route.append(
                (mid[0] - dlon / norm * offset, mid[1] + dlat / norm * offset)
            )
        route.append(coords[-1])
        return route

    def test_expand_to_target():
        line = [(0.0, 0.0), (0.0, 0.001), (0.0004, 0.0012)]
        length = polyline_length(line)
        expanded = expand_polyline_to_distance(line, length * 2.5)
        assert len(expanded) > 4, expanded
        assert abs(polyline_length(expanded) - length * 2.5) < 0.01, polyline_length(expanded)
        assert expanded[0] == line[0]
        assert all(
            point_to_polyline_m(point, line) <= 0.01 for point in expanded
        ), "expanded points must lie on the drawn polyline"
        same = expand_polyline_to_distance(line, length)
        assert abs(polyline_length(same) - length) < 0.01
        truncated = expand_polyline_to_distance(line, length * 0.5)
        assert truncated == _truncate_polyline(line, length * 0.5)

    def test_expand_edge_cases():
        line = [(0.0, 0.0), (0.0, 0.001), (0.0004, 0.0012)]
        length = polyline_length(line)
        # T = 2L：终点回到起点，方向转回正向。
        doubled = expand_polyline_to_distance(line, length * 2)
        assert distance_m(doubled[-1], line[0]) <= 0.01, doubled[-1]
        assert abs(polyline_length(doubled) - length * 2) < 0.01
        # T = 3L + 10：三趟后在终点掉头，多出的 10 m 沿最后一段反向。
        odd = expand_polyline_to_distance(line, length * 3 + 10)
        assert abs(polyline_length(odd) - (length * 3 + 10)) < 0.01
        expected = interpolate_point(
            line[-1], line[-2], 10.0 / distance_m(line[-1], line[-2])
        )
        assert distance_m(odd[-1], expected) <= 0.01, odd[-1]
        # 连续重复顶点 / 两点 / L=0 不抛异常。
        repeated = [(0.0, 0.0), (0.0, 0.0), (0.0, 0.001)]
        assert expand_polyline_to_distance(repeated, 500)
        two = [(0.0, 0.0), (0.0, 0.001)]
        assert abs(polyline_length(expand_polyline_to_distance(two, 500)) - 500) < 0.01
        zero = [(0.0, 0.0), (0.0, 0.0)]
        assert expand_polyline_to_distance(zero, 500) == [(0.0, 0.0)]

    def test_simplify_dense_vertices():
        line = [
            (0.0, 0.0),
            (0.0, 0.001),
            (0.0005, 0.001),       # 与上一点 55 m
            (0.0005, 0.001025),    # 与上一点 2.78 m，应去掉
            (0.0005, 0.0010313),   # 与上一点 0.70 m，是打卡点，保留
            (0.0005, 0.0018),
        ]
        checkpoints = [
            {"glat": 0.0, "glon": 0.0},
            {"glat": 0.0005, "glon": 0.0010313},
        ]
        cleaned = simplify_dense_vertices(line, checkpoints)
        assert line[3] not in cleaned, cleaned
        assert line[4] in cleaned, cleaned
        assert cleaned[0] == line[0] and cleaned[-1] == line[-1]
        assert len(cleaned) == 5, cleaned

    def test_library_variants():
        with __import__("tempfile").TemporaryDirectory() as temporary:
            library = RouteLibrary(
                Path(temporary) / "route_library.json",
                Path(temporary) / "route_history.jsonl",
            )
            entries = []
            for offset in (0.0, 0.0005, 0.0010):
                entries.append(
                    library.add(
                        base_points,
                        route_with_offset(base_points, offset),
                        17601,
                        8,
                        "xuchang_280173698",
                    )
                )
            lengths = sorted(entry.length_m for entry in entries)
            assert lengths[0] < lengths[1] < lengths[2], lengths
            target = int(lengths[1]) + 10
            assert lengths[2] > target, (lengths, target)
            matches = library.find(base_points, 17601, 8, target)
            assert matches, "should find a variant"
            assert matches[0].entry.length_m <= target
            assert abs(matches[0].entry.length_m - lengths[1]) < 1e-6, matches[0].entry.length_m
            no_match = library.find(base_points, 17601, 8, 50)
            assert not no_match, "route longer than target should be rejected"

            # 复用前 rebase：3 m 抖动也必须把当次坐标替换进折线。
            shifted = [dict(point) for point in base_points]
            shifted[0]["glat"] += 0.000027
            shifted_matches = library.find(shifted, 17601, 8, target)
            assert shifted_matches, "jittered points should still hit"
            order, distances = checkpoint_order(
                shifted_matches[0].adjusted_polyline, shifted
            )
            assert order == shifted_matches[0].entry.order, (order, shifted_matches[0].entry.order)
            assert max(distances) <= 0.1, distances

    def test_library_management():
        with __import__("tempfile").TemporaryDirectory() as temporary:
            library = RouteLibrary(
                Path(temporary) / "route_library.json",
                Path(temporary) / "route_history.jsonl",
            )
            entry = library.add(
                base_points,
                route_with_offset(base_points, 0.0),
                17601,
                8,
                "xuchang_280173698",
                notes="old",
                anchor={"id": "a3", "name": "教学楼",
                        "glat": 34.04, "glon": 113.86, "seed": "seed-1"},
            )
            assert entry.anchor and entry.anchor["id"] == "a3"
            longer = route_with_offset(base_points, 0.0005)
            updated = library.update(
                entry.route_id, longer, base_points, notes="new",
                anchor={"id": "a4", "name": "食堂",
                        "glat": 34.05, "glon": 113.87},
            )
            assert updated.route_id == entry.route_id
            assert library.get(entry.route_id) is updated
            assert updated.anchor and updated.anchor["id"] == "a4"
            assert updated.notes == "new"
            assert abs(updated.length_m - polyline_length(longer)) < 1e-6
            assert updated.vertex_indices == entry.vertex_indices or updated.vertex_indices

            assert library.set_notes(entry.route_id, "renamed")
            assert library.get(entry.route_id).notes == "renamed"

            assert library.delete(entry.route_id)
            assert library.get(entry.route_id) is None
            assert not library.delete(entry.route_id)

            records = [
                json.loads(line)
                for line in (Path(temporary) / "route_history.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            events = [record["event"] for record in records]
            assert events == ["add", "update", "notes", "delete"], events
            assert records[0]["anchor_id"] == "a3"
            assert records[1]["anchor_id"] == "a4"

    def test_relaxed_hit():
        with __import__("tempfile").TemporaryDirectory() as temporary:
            library = RouteLibrary(
                Path(temporary) / "route_library.json",
                Path(temporary) / "route_history.jsonl",
            )
            short_entry = library.add(
                base_points,
                route_with_offset(base_points, 0.0),
                17601,
                8,
                "xuchang_280173698",
            )
            short_entry.status = "needs_review"
            long_entry = library.add(
                base_points,
                route_with_offset(base_points, 0.0015),
                17601,
                8,
                "xuchang_280173698",
            )
            long_entry.status = "invalid"
            target = 300
            assert short_entry.length_m <= target < long_entry.length_m, (
                short_entry.length_m,
                long_entry.length_m,
            )

            # 严格模式仍按状态/长度过滤；宽松模式只看点位，两个变体都命中。
            assert not library.find(base_points, 17601, 8, target)
            matches = library.find_by_points(base_points, 17601, 8, target)
            assert {match.entry.route_id for match in matches} == {
                short_entry.route_id,
                long_entry.route_id,
            }
            # 能装下目标的变体排第一。
            assert matches[0].entry.route_id == short_entry.route_id, matches[0].entry.route_id

            # 命名空间和点位位置仍是硬条件。
            assert not library.find_by_points(base_points, 17602, 8, target)
            moved = [dict(point) for point in base_points]
            moved[0]["glat"] += 0.001
            assert not library.find_by_points(moved, 17601, 8, target)

            # 趟数远超上限也仍然命中（由调用方决定是否可用）。
            assert library.find_by_points(base_points, 17601, 8, 100000)

    def test_real_campus_validation():
        planner = load_planner("xuchang_280173698")
        if planner is None:
            print("[SKIP] real campus validation: protocol data not available")
            return
        segment, length = _find_test_segment(planner)
        checkpoints = [{"glat": lat, "glon": lon, "name": "P%d" % index}
                       for index, (lat, lon) in enumerate(segment)]
        target = int(math.ceil(length))
        report = validate_route(segment, checkpoints, target, planner=planner)
        assert report.ok, report.errors
        assert report.order == [0, 1, 2, 3, 4], report.order
        too_short = validate_route(segment, checkpoints, int(math.floor(length)) - 10,
                                   planner=planner)
        assert not too_short.ok, "target shorter than route should fail"
        assert any("长于目标" in item for item in too_short.errors), too_short.errors

    check("points_key is order-insensitive", test_key_order)
    check("EPS matching (3 m in, 20 m out)", test_match_eps)
    check("traversal follows the drawn polyline", test_traversal)
    check("expand drawn route to target distance", test_expand_to_target)
    check("expand edge cases (2L / 3L+10 / duplicates)", test_expand_edge_cases)
    check("simplify dense drawn vertices", test_simplify_dense_vertices)
    check("library variants and distance filter", test_library_variants)
    check("library update / notes / delete", test_library_management)
    check("relaxed hit only by checkpoint positions", test_relaxed_hit)
    check("real campus route validation", test_real_campus_validation)

    if failures:
        print("\n失败 %d 项: %s" % (len(failures), "、".join(failures)))
        return 1
    print("\n全部自检通过。")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="校园跑 · 手动路线库原型")
    parser.add_argument("--self-test", action="store_true", help="运行离线自检")
    parser.add_argument("--list", action="store_true", help="列出路线库条目")
    parser.add_argument("--library", default="", help="路线库 JSON 路径")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    library = RouteLibrary(args.library or None)
    if args.list:
        entries = library.list_entries()
        if not entries:
            print("路线库为空：", library.path)
            return 0
        for entry in entries:
            print(
                "%s  %.1f m  order=%s  status=%s  last_used=%s  %s"
                % (
                    entry["route_id"][:8],
                    entry["length_m"],
                    entry["order"],
                    entry["status"],
                    entry["last_used_at"] or "-",
                    entry["notes"],
                )
            )
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
