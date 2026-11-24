# -*- coding: utf-8 -*-
"""官方 7.3.92 轨迹 schema：LocJsonEntity + MyLocation。

`sport/record` 的 ``allLocJson`` 与 OBS 的 ``run_data`` 必须是同一个
``LocJsonEntity`` JSON 字符串：外层 ``{"useZip": false, "allLocJson": "<字符串>"}``，
内层是 ``List<MyLocation>``。字段映射集中在本模块，真实 fixture 校准后只改这里。
"""
import base64
import gzip
import json
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .consistency import coordinate, distance

# 官方 App 用 Gson 反序列化的 MyLocation 字段。字段集合以真实官方 run_data
# fixture（rrid 1333592274，154 点）为准；顺序不影响 Gson 解析。
MY_LOCATION_FIELDS = (
    "avgSpeed", "bdA", "bdD", "bdG", "bdS", "coorType", "count", "dtr",
    "flag", "gLat", "gLng", "gainTime", "id", "lat", "lng", "locType",
    "locationId", "queueNum", "radius", "speed", "state", "stepDistance",
    "totalDis", "totalTime", "type", "validDis", "validTime",
)

# 已用真实 fixture 校准的常量。官方记录里 lat/lng 固定 -1，真实坐标在 gLat/gLng；
# speed 是配速（分钟/公里），avgSpeed 才是米/秒。
MY_LOCATION_DEFAULTS = {
    "bdA": 0.0,
    "bdD": 0.0,
    "bdG": 1,
    "bdS": 0.0,
    "coorType": "gcj02",
    "dtr": 0.0,
    "lat": -1.0,
    "lng": -1.0,
    "locType": 1,
    "locationId": "",
    "queueNum": 0,
    "state": 1,
    "stepDistance": 0.0,
    "type": 0,
}


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def to_my_location(point: Any,
                   index: int,
                   start_time_ms: int,
                   elapsed_ms: int,
                   cumulative_m: float,
                   total_points: Optional[int] = None) -> Dict[str, Any]:
    """把一个 GpsPoint 转为官方 MyLocation 字段。

    字段取值以真实官方 run_data fixture 为准：
    - ``lat/lng`` 固定 -1.0，真实 GCJ-02 坐标在 ``gLat/gLng``；
    - ``speed`` 是配速（分钟/公里），``avgSpeed`` 是米/秒；
    - ``totalTime/validTime`` 是相对起点的**秒**，不是毫秒；
    - ``gainTime`` 是本地时间字符串 ``YYYY-MM-DD HH:MM:SS``。
    """
    g_lat, g_lng = coordinate(point.lat, point.lon)
    speed_mps = _finite_float(point.speed, "speed")
    accuracy = _finite_float(point.accuracy, "accuracy")
    # 新字段用默认值保持旧调用兼容；未提供时与以前的 0 / 0 行为一致。
    altitude = _finite_float(getattr(point, "altitude", 0.0), "altitude")
    step_distance = _finite_float(getattr(point, "step_distance", 0.0),
                                  "stepDistance")
    if elapsed_ms < 0:
        raise ValueError("point time precedes run start")
    if cumulative_m < 0:
        raise ValueError("cumulative distance must be nonnegative")
    if step_distance < 0:
        raise ValueError("stepDistance must be nonnegative")

    elapsed_s = int(elapsed_ms // 1000)
    # 官方 speed 字段是分钟/公里；停下来的点官方写 0。
    pace_min_per_km = (1000.0 / (speed_mps * 60.0)) if speed_mps > 0 else 0.0
    if total_points is None:
        total_points = index + 1
    if index == 0:
        point_type = 5
    elif index == total_points - 1:
        point_type = 6
    else:
        point_type = 0

    result: Dict[str, Any] = dict(MY_LOCATION_DEFAULTS)
    result.update({
        "gLat": g_lat,
        "gLng": g_lng,
        "gainTime": time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(point.time / 1000.0)),
        "avgSpeed": round(speed_mps, 2),
        "speed": round(pace_min_per_km, 2),
        # 官方 bdA 是逐点绝对海拔（米），stepDistance 是累计步距（米）。
        "bdA": round(altitude, 3),
        "stepDistance": round(step_distance, 3),
        # 官方 bdS 是逐点速度类传感器值，这里用当前速度占位。
        "bdS": round(speed_mps, 2),
        "radius": accuracy,
        "totalTime": elapsed_s,
        "totalDis": round(float(cumulative_m), 3),
        "flag": int(start_time_ms),
        "count": int(index + 1),
        "id": int(index + 1),
        "type": point_type,
        "validTime": elapsed_s,
        "validDis": round(float(cumulative_m), 3),
    })
    # 保持稳定字段集合：缺字段说明映射表写漏了。
    missing = [name for name in MY_LOCATION_FIELDS if name not in result]
    if missing:
        raise ValueError("missing MyLocation fields: " + ", ".join(missing))
    return {name: result[name] for name in MY_LOCATION_FIELDS}


def build_loc_json(points: Sequence[Any], start_time_ms: int) -> str:
    """生成 LocJsonEntity 字符串；points 按时间升序、已经是 GCJ-02。"""
    if isinstance(start_time_ms, bool) or not isinstance(start_time_ms, int):
        raise ValueError("start_time_ms must be an integer")
    if len(points) < 2:
        raise ValueError("at least two track points are required")

    locations: List[Dict[str, Any]] = []
    cumulative = 0.0
    previous: Optional[Tuple[float, float]] = None
    previous_time: Optional[int] = None
    for index, point in enumerate(points):
        lat, lon = coordinate(point.lat, point.lon)
        timestamp = int(point.time)
        if previous_time is not None and timestamp <= previous_time:
            raise ValueError("track timestamps must strictly increase")
        if previous is not None:
            cumulative += distance(previous, (lat, lon))
        elapsed = timestamp - start_time_ms
        if elapsed < 0:
            raise ValueError("point time precedes run start")
        locations.append(to_my_location(point, index, start_time_ms,
                                         elapsed, cumulative,
                                         total_points=len(points)))
        previous = (lat, lon)
        previous_time = timestamp

    points_json = json.dumps(locations, separators=(",", ":"), ensure_ascii=False)
    return json.dumps({"useZip": False, "allLocJson": points_json},
                      separators=(",", ":"), ensure_ascii=False)


def encode_run_data(loc_json: str) -> str:
    """OBS run_data：同一个 LocJsonEntity 的 gzip + base64。"""
    if not isinstance(loc_json, str) or not loc_json:
        raise ValueError("loc_json must be a nonempty string")
    data = loc_json.encode("utf-8")
    try:
        compressed = gzip.compress(data, mtime=0)
    except TypeError:  # Python < 3.11
        compressed = gzip.compress(data)
    return base64.b64encode(compressed).decode("ascii")


def decode_run_data(payload: str) -> str:
    """回读 OBS run_data；兼容明文 JSON 与 gzip+base64。"""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    text = str(payload).strip()
    if not text:
        raise ValueError("empty run_data")
    if text.startswith("{") or text.startswith("["):
        return text
    try:
        raw = base64.b64decode(text, validate=True)
        return gzip.decompress(raw).decode("utf-8")
    except Exception:
        pass
    try:
        return base64.b64decode(text).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"run_data 不是 gzip+base64 或 JSON: {exc}") from exc


def parse_loc_json(value: Any) -> Dict[str, Any]:
    """解析 LocJsonEntity 或旧版裸数组。

    返回 ``{"kind": "locjsonentity"|"legacy", "outer": ..., "points": [...]}``。
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"loc_json 不是 JSON: {exc}") from exc

    if isinstance(value, list):
        points = _check_points(value)
        return {"kind": "legacy", "outer": None, "points": points}

    if not isinstance(value, dict):
        raise ValueError("loc_json 必须是对象或数组")
    if "allLocJson" not in value:
        raise ValueError("LocJsonEntity 缺少 allLocJson")
    inner = value.get("allLocJson")
    if isinstance(inner, str):
        if not inner.strip():
            raise ValueError("allLocJson 为空")
        inner = json.loads(inner)
    points = _check_points(inner)
    return {"kind": "locjsonentity", "outer": value, "points": points}


def validate_loc_json(value: Any, require_official: bool = True,
                      strict_order: bool = False) -> List[Dict[str, Any]]:
    """校验上传/回读的轨迹；返回 MyLocation 列表。

    官方 OBS 里的点可能因为补传而轻微乱序（fixture rrid 1333592274 就有
    ``totalDis`` 先 12.1 后 0.0 的相邻点），所以默认只校验结构。生成上传报文时
    ``strict_order=True``，保证我们自己写入的轨迹按时间/里程递增。
    """
    parsed = parse_loc_json(value)
    if require_official and parsed["kind"] != "locjsonentity":
        raise ValueError("轨迹不是官方 LocJsonEntity")
    if parsed["kind"] == "locjsonentity":
        outer = parsed["outer"]
        if outer.get("useZip") not in (False, 0):
            raise ValueError("LocJsonEntity.useZip 必须为 false")
    points = parsed["points"]
    for index, point in enumerate(points):
        missing = [name for name in ("lat", "lng", "gLat", "gLng", "gainTime")
                   if name not in point]
        if missing:
            raise ValueError(f"第 {index} 个点缺少字段: {', '.join(missing)}")
        coordinate(point["lat"], point["lng"])
        coordinate(point["gLat"], point["gLng"])
    if strict_order:
        times = [int(point.get("totalTime", 0) or 0) for point in points]
        if any(a > b for a, b in zip(times, times[1:])):
            raise ValueError("MyLocation.totalTime 必须非递减")
        distances = [float(point.get("totalDis", 0) or 0) for point in points]
        if any(a > b + 1e-6 for a, b in zip(distances, distances[1:])):
            raise ValueError("MyLocation.totalDis 必须非递减")
    return points


def my_location_keys(value: Any) -> List[str]:
    """返回第一个点的字段名列表，便于 fixture 对照。"""
    points = parse_loc_json(value)["points"]
    return list(points[0].keys()) if points else []


def _check_points(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError("轨迹至少需要两个点")
    if any(not isinstance(point, dict) for point in value):
        raise ValueError("轨迹点必须是对象")
    return value
