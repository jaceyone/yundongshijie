# -*- coding: utf-8 -*-
"""
校园跑 · 取点锚点池

定位
  - 锚点是「官方 5 点接口的取点位置」：每次运行随机选 1 个，作为
    ``/api/v560/get/1/distance/{minDistance}`` 的 latitude/longitude。
  - 官方返回哪 5 点由服务端决定；客户端只决定「从哪取点」。
  - 锚点池允许 1–5 个，推荐 5 个；首版随机不避重（允许连续同一锚点）。

数据
  - ``anchor_points.json``（本地，已 gitignore，含真实校园坐标）；
  - 坐标统一为 GCJ-02，与 AMap 底图和官方取点请求一致。

用法
  python anchor_points.py --self-test
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
PROTOCOL_HOME = REPO_ROOT / "协议" / "7.3.92"
DEFAULT_PATH = APP_DIR / "anchor_points.json"

SCHEMA_VERSION = 1
MAX_ANCHORS = 5
RECOMMENDED_ANCHORS = 5
MIN_ANCHOR_GAP_M = 100.0
CHINA_LAT = (3.0, 54.0)
CHINA_LON = (73.0, 136.0)
EARTH_RADIUS_M = 6371000.0


class AnchorPoolError(ValueError):
    """锚点池格式或坐标不合法。"""


@dataclass
class SelectedAnchor:
    """本次运行选中的锚点。"""
    id: str
    name: str
    glat: float
    glon: float
    seed: str = ""

    @property
    def label(self) -> str:
        ident = str(self.id or "").strip()
        if len(ident) == 2 and ident[0].lower() == "a" and ident[1].isdigit():
            ident = "A" + ident[1]
        elif ident:
            ident = ident.upper()
        return ("%s %s" % (ident, self.name)).strip()

    def to_meta(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "glat": self.glat,
            "glon": self.glon,
            "seed": self.seed,
        }


# --------------------------------------------------------------------------- 几何


def distance_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = math.radians(float(a[0])), math.radians(float(a[1]))
    lat2, lon2 = math.radians(float(b[0])), math.radians(float(b[1]))
    delta = lon2 - lon1
    cosine = (math.sin(lat1) * math.sin(lat2)
              + math.cos(lat1) * math.cos(lat2) * math.cos(delta))
    return EARTH_RADIUS_M * math.acos(max(-1.0, min(1.0, cosine)))


def point_in_polygon(lat: float, lon: float,
                     polygon: Sequence[Sequence[float]]) -> bool:
    if len(polygon) < 3:
        return False
    x, y = float(lon), float(lat)
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = float(polygon[i][1]), float(polygon[i][0])
        xj, yj = float(polygon[j][1]), float(polygon[j][0])
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def load_campus_geometry(campus_id: str) -> Tuple[List[Tuple[float, float]],
                                                  List[List[Tuple[float, float]]]]:
    """读取打包校园数据的 GCJ 边界与建筑多边形；缺失时返回空列表。"""
    path = PROTOCOL_HOME / "running" / "data" / ("campus_%s.json" % campus_id)
    if not path.is_file():
        return [], []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    boundary = [(float(p[0]), float(p[1])) for p in raw.get("boundary", [])
                if isinstance(p, (list, tuple)) and len(p) >= 2]
    buildings: List[List[Tuple[float, float]]] = []
    for building in raw.get("buildings", []) or []:
        polygon = [(float(p[0]), float(p[1])) for p in building.get("polygon", [])
                   if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(polygon) >= 3:
            buildings.append(polygon)
    return boundary, buildings


# --------------------------------------------------------------------------- 读写


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _infer_unid() -> Optional[int]:
    """从最近一次取点报告推断 unid；保存锚点时尚未登录，拿不到账号上下文。"""
    logs_dir = APP_DIR / "logs"
    try:
        reports = sorted(logs_dir.glob("points-*.json"))
    except OSError:
        return None
    for path in reversed(reports):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            value = raw.get("unid")
            if value not in (None, ""):
                return int(value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return None


def default_payload() -> Dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "updated_at": "",
        "unid": None,
        "campus": "",
        "selection": "random",
        "avoid_last": False,
        "last_used_id": "",
        "points": [],
    }


def load_pool(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取锚点池；文件不存在时返回空池，格式错误抛 AnchorPoolError。"""
    target = Path(path or DEFAULT_PATH)
    payload = default_payload()
    if not target.exists():
        return payload
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnchorPoolError("锚点文件读取失败: %s" % exc) from exc
    if not isinstance(raw, dict):
        raise AnchorPoolError("锚点文件顶层必须是对象")
    for key in ("version", "updated_at", "unid", "campus", "selection",
                "avoid_last", "last_used_id"):
        if key in raw:
            payload[key] = raw[key]
    points = raw.get("points", [])
    if not isinstance(points, list):
        raise AnchorPoolError("锚点文件缺少 points 数组")
    payload["points"] = points
    return payload


def _write_pool(payload: Dict[str, Any], path: Optional[Path] = None) -> Path:
    target = Path(path or DEFAULT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)
    return target


def _normalise_point(point: Any, index: int) -> Dict[str, Any]:
    if not isinstance(point, dict):
        raise AnchorPoolError("第 %d 个锚点不是对象" % (index + 1))
    lat = point.get("glat", point.get("lat"))
    lon = point.get("glon", point.get("lon", point.get("lng")))
    if isinstance(lat, bool) or isinstance(lon, bool):
        raise AnchorPoolError("第 %d 个锚点坐标必须是数字" % (index + 1))
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise AnchorPoolError("第 %d 个锚点坐标必须是数字" % (index + 1))
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise AnchorPoolError("第 %d 个锚点坐标必须是有限数值" % (index + 1))
    if not (CHINA_LAT[0] <= lat <= CHINA_LAT[1]
            and CHINA_LON[0] <= lon <= CHINA_LON[1]):
        raise AnchorPoolError("第 %d 个锚点不在中国范围内" % (index + 1))
    ident = str(point.get("id", "") or "").strip()
    if not ident:
        ident = "a%d" % (index + 1)
    name = str(point.get("name", "") or "").strip() or ("锚点 %d" % (index + 1))
    return {"id": ident, "name": name, "glat": lat, "glon": lon}


def normalise_point(point: Any, index: int = 0) -> Dict[str, Any]:
    """规范化单个锚点；编辑器逐项读取损坏文件时使用。"""
    return _normalise_point(point, index)


def normalise_points(points: Sequence[Any]) -> List[Dict[str, Any]]:
    if not isinstance(points, (list, tuple)):
        raise AnchorPoolError("锚点必须是数组")
    if len(points) < 1:
        raise AnchorPoolError("至少需要 1 个锚点")
    if len(points) > MAX_ANCHORS:
        raise AnchorPoolError("最多 %d 个锚点" % MAX_ANCHORS)
    result: List[Dict[str, Any]] = []
    seen = set()
    for index, point in enumerate(points):
        item = _normalise_point(point, index)
        if item["id"] in seen:
            raise AnchorPoolError("锚点 id 重复: %s" % item["id"])
        seen.add(item["id"])
        result.append(item)
    return result


def save_pool(points: Sequence[Any],
              unid: Optional[Any] = None,
              campus: Optional[str] = None,
              path: Optional[Path] = None,
              boundary: Optional[Sequence[Sequence[float]]] = None,
              buildings: Optional[Sequence[Sequence[Sequence[float]]]] = None
              ) -> List[str]:
    """校验并保存锚点池，返回非阻塞警告（例如不足 5 个、落在建筑内）。"""
    try:
        existing = load_pool(path)
    except AnchorPoolError:
        existing = default_payload()
    normalised = normalise_points(points)

    boundary = list(boundary or [])
    for index, point in enumerate(normalised):
        if boundary and not point_in_polygon(
                point["glat"], point["glon"], boundary):
            raise AnchorPoolError(
                "%s 不在校园允许边界内，请移到校内" % point["id"].upper())

    warnings: List[str] = []
    if len(normalised) < RECOMMENDED_ANCHORS:
        warnings.append(
            "推荐画满 %d 个锚点（当前 %d 个）；少于 3 个时很难换出不同点位。"
            % (RECOMMENDED_ANCHORS, len(normalised)))
    for index, point in enumerate(normalised):
        for polygon in buildings or []:
            if point_in_polygon(point["glat"], point["glon"], polygon):
                warnings.append(
                    "%s 在建筑内，建议移到道路上。" % point["id"].upper())
                break
    for i in range(len(normalised)):
        for j in range(i + 1, len(normalised)):
            gap = distance_m(
                (normalised[i]["glat"], normalised[i]["glon"]),
                (normalised[j]["glat"], normalised[j]["glon"]))
            if gap < MIN_ANCHOR_GAP_M:
                warnings.append(
                    "%s 与 %s 相距 %.0f m，太近，可能返回同一组点。"
                    % (normalised[i]["id"].upper(), normalised[j]["id"].upper(), gap))

    resolved_unid = existing.get("unid") if unid is None else int(unid)
    if resolved_unid in (None, ""):
        resolved_unid = _infer_unid()

    payload = default_payload()
    payload.update({
        "version": SCHEMA_VERSION,
        "updated_at": _now(),
        "unid": resolved_unid,
        "campus": (str(campus) if campus is not None
                   else str(existing.get("campus", "") or "")),
        "selection": "random",
        "avoid_last": False,
        "last_used_id": str(existing.get("last_used_id", "") or ""),
        "points": normalised,
    })
    _write_pool(payload, path)
    return warnings


# --------------------------------------------------------------------------- 选择


def _valid_points(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for index, raw in enumerate(payload.get("points") or []):
        try:
            result.append(_normalise_point(raw, index))
        except AnchorPoolError:
            continue
    return result


def select_anchor(path: Optional[Path] = None,
                  rng: Optional[random.Random] = None,
                  avoid_last: Optional[bool] = None) -> Optional[SelectedAnchor]:
    """随机选 1 个锚点并写回 last_used_id；池为空/全部无效时返回 None。

    首版均匀随机、不避重：同一个锚点可以连续选中。``rng`` 便于单测注入。
    """
    payload = load_pool(path)
    points = _valid_points(payload)
    if not points:
        return None
    if avoid_last is None:
        avoid_last = bool(payload.get("avoid_last", False))
    last_used = str(payload.get("last_used_id", "") or "")
    candidates = points
    if avoid_last and len(points) > 1:
        candidates = [point for point in points if point["id"] != last_used] or points

    seed = uuid.uuid4().hex
    chooser = rng or random.Random(seed)
    chosen = chooser.choice(candidates)
    payload["last_used_id"] = chosen["id"]
    payload["updated_at"] = _now()
    # 只保存规范化后的点，避免把损坏条目继续写回。
    payload["points"] = points
    _write_pool(payload, path)
    return SelectedAnchor(
        id=chosen["id"], name=chosen["name"],
        glat=chosen["glat"], glon=chosen["glon"], seed=seed)


def describe_pool(path: Optional[Path] = None) -> Dict[str, Any]:
    """给主窗口显示的一行摘要。"""
    payload = load_pool(path)
    points = _valid_points(payload)
    if not points:
        return {"configured": False, "count": 0, "last_used": None,
                "text": "未配置（使用围栏顶点）"}
    count = len(points)
    last_used = None
    last_id = str(payload.get("last_used_id", "") or "")
    for point in points:
        if point["id"] == last_id:
            last_used = SelectedAnchor(
                id=point["id"], name=point["name"],
                glat=point["glat"], glon=point["glon"])
            break
    text = "随机（A1–A%d）" % count
    if last_used is not None:
        text += " · 上次 %s" % last_used.label
    return {"configured": True, "count": count, "last_used": last_used,
            "text": text}


# --------------------------------------------------------------------------- 自检


def _self_test() -> int:
    import tempfile

    failures = 0

    def check(name: str, function) -> None:
        nonlocal failures
        try:
            function()
            print("[OK]   %s" % name)
        except Exception as exc:  # pragma: no cover - 自检输出
            failures += 1
            print("[FAIL] %s: %s" % (name, exc))

    def test_roundtrip():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "anchor_points.json"
            points = [{"id": "a1", "name": "操场", "glat": 34.04, "glon": 113.86},
                      {"id": "a2", "name": "宿舍", "glat": 34.05, "glon": 113.87}]
            save_pool(points, unid=17601, campus="xuchang_280173698", path=path)
            pool = load_pool(path)
            assert len(pool["points"]) == 2
            assert pool["unid"] == 17601
            assert describe_pool(path)["count"] == 2

    def test_select_no_avoid():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "anchor_points.json"
            save_pool([{"id": "a1", "name": "A", "glat": 34.04, "glon": 113.86}],
                      path=path)
            first = select_anchor(path)
            second = select_anchor(path)
            assert first is not None and second is not None
            assert first.id == second.id == "a1"
            assert load_pool(path)["last_used_id"] == "a1"

    def test_empty_and_invalid():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "anchor_points.json"
            assert select_anchor(path) is None
            path.write_text('{"points":[{"id":"a1","name":"x","glat":"bad",'
                            '"glon":113}]}', encoding="utf-8")
            assert select_anchor(path) is None
            assert describe_pool(path)["configured"] is False

    def test_limits():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "anchor_points.json"
            for points in ([], [{"id": "a%d" % i, "name": "x",
                                 "glat": 34.04, "glon": 113.86} for i in range(6)]):
                try:
                    save_pool(points, path=path)
                except AnchorPoolError:
                    continue
                raise AssertionError("应拒绝 %d 个锚点" % len(points))

    for name, function in (
        ("读写往返", test_roundtrip),
        ("单点池随机", test_select_no_avoid),
        ("空池/坏点回退", test_empty_and_invalid),
        ("1–5 个限制", test_limits),
    ):
        check(name, function)
    print("\n自检完成：%d 项失败" % failures)
    return 1 if failures else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="校园跑 · 取点锚点池")
    parser.add_argument("--self-test", action="store_true", help="离线自检")
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    print(describe_pool()["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
