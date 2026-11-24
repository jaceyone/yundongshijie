import base64
import gzip
import hashlib
import json
import math
import time
import uuid
from typing import List, Tuple, Dict, Any, Optional, Union

from .consistency import sample_route, check_track, validate_totals, distance
from .config import SPORT_STATIC_SALT
from .metrics import (
    DEFAULT_STRIDE_M,
    build_laps,
    bucket_distances,
    bucket_slices,
    bucket_steps,
    cumulative_from_buckets,
    derive_seed,
    elevation_profile,
    total_ascent,
)
from .models import GpsPoint, FivePoint, StepBucket, SpeedBucket
from .schema import build_loc_json, encode_run_data, validate_loc_json


def compute_sport_signature(field_map: Dict[str, Union[str, int, float, bool, None]],
                            salt: str = SPORT_STATIC_SALT) -> str:
    """计算 31 参数运动学加盐 MD5 校验签名"""
    valid_items = []
    for k, v in field_map.items():
        if v is None:
            continue
        if k.lower() == "signature":
            continue
        valid_items.append((k, str(v).lower() if isinstance(v, bool) else str(v)))
    
    valid_items.sort(key=lambda x: (x[0].lower(), x[0]))
    canonical_str = "&".join(f"{k}={v}" for k, v in valid_items)
    to_hash = (canonical_str + salt).encode("utf-8")
    return hashlib.md5(to_hash).hexdigest().lower()


def compute_original_sign(field_map: Dict[str, Union[str, int, float, bool, None]]) -> str:
    """计算原始参数键值对拼接串"""
    valid_items = []
    for k, v in field_map.items():
        if v is None:
            continue
        if k.lower() == "signature":
            continue
        valid_items.append((k, str(v).lower() if isinstance(v, bool) else str(v)))
    
    valid_items.sort(key=lambda x: x[0])
    return "&".join(f"{k}={v}" for k, v in valid_items)


def gzip_base64_encode(data: Union[str, bytes]) -> str:
    """Gzip 压缩并转为 Base64 字符串"""
    if isinstance(data, str):
        data_bytes = data.encode("utf-8")
    else:
        data_bytes = data
    compressed = gzip.compress(data_bytes)
    return base64.b64encode(compressed).decode("ascii")


class RunningTrackBuilder:
    """运动轨迹、传感器步频分桶与 7.3.92 报文组装构建器"""

    def __init__(self, uid: int, unid: int, sport_type: int = 1, uuid_str: Optional[str] = None):
        self.uid = int(uid)
        self.unid = int(unid)
        # sport_type: 1 为计分跑（校园考核），6 为自由跑（阳光跑/课外自由锻炼）
        self.sport_type = int(sport_type)
        self.uuid_str = uuid_str or str(uuid.uuid4())

    def generate_track_around_points(self,
                                    center_lat: float,
                                    center_lng: float,
                                    checkpoints: Optional[List[Dict[str, Any]]] = None,
                                    total_distance_m: int = 3000,
                                    total_time_sec: int = 1060,
                                    start_time_ms: int = 0,
                                    route_polyline: Optional[List[Tuple[float, float]]] = None,
                                    altitude_seed: Optional[int] = None,
                                    route_points: Optional[List[Tuple[float, float]]] = None) -> Tuple[List[GpsPoint], List[FivePoint]]:
        """根据模式 (sportType=1 计分跑 vs sportType=6 自由跑) 生成真实平滑的 GPS 坐标序列。

        ``route_polyline`` 是外部路线折线（GCJ-02 的 [lat, lon] 序列）；提供时优先
        使用它，供 OSM 规划器或外部路线文件接入。``route_points`` 是官方 5 点排序后
        的 GCJ-02 坐标；没有折线时用它作为 waypoint。5 个官方点仍会通过 check_track
        校验必须精确经过，不会被吸附或移动。
        """
        validate_totals(total_time_sec, 0, start_time_ms,
                        start_time_ms + total_time_sec*1000, total_distance_m)
        points = []

        waypoints = []
        five_points = []
        
        # 1. 计分跑 (sportType=1)：必须绑定打卡点
        if self.sport_type == 1 and checkpoints and len(checkpoints) > 0:
            for idx, cp in enumerate(checkpoints[:5]):
                # lat/lng 是官方 BD-09；路线/地图必须用 glat/glon（GCJ-02）。
                c_lat = float(cp.get("lat", cp.get("latitude", center_lat)))
                c_lng = float(cp.get("lng", cp.get("longitude", center_lng)))
                r_lat = float(cp.get("glat", c_lat))
                r_lng = float(cp.get("glon", cp.get("glng", c_lng)))
                waypoints.append((r_lat, r_lng))
                five_points.append(FivePoint(
                    lat=c_lat,
                    lon=c_lng,
                    route_lat=r_lat,
                    route_lon=r_lng,
                    isFixed=cp.get("isFixed", 1),
                    isPass=True,
                    pointName=str(cp.get("name") or cp.get("pointName") or f"P_{idx+1}"),
                    position=cp.get("position", idx + 1),
                    raw={key: cp[key] for key in ("id", "flag", "state", "glat", "glon", "glng") if key in cp}
                ))
        elif self.sport_type == 1:
            # 计分跑默认标准操场 5 必经点
            radius_deg = 0.00135
            for i in range(5):
                ang = (2 * math.pi * i) / 5
                c_lat = center_lat + radius_deg * math.cos(ang)
                c_lng = center_lng + radius_deg * math.sin(ang)
                waypoints.append((c_lat, c_lng))
                five_points.append(FivePoint(
                    lat=round(c_lat, 6),
                    lon=round(c_lng, 6),
                    isFixed=1,
                    isPass=True,
                    pointName=f"CheckPoint_{i+1}",
                    position=i + 1
                ))
        else:
            # 2. 自由跑 (sportType=6)：无必经打卡点约束 (fivePoints为空)，生成自然公路环线
            radius_deg = 0.0020
            for i in range(8):
                ang = (2 * math.pi * i) / 8
                c_lat = center_lat + radius_deg * math.cos(ang)
                c_lng = center_lng + radius_deg * math.sin(ang)
                waypoints.append((c_lat, c_lng))
            five_points = []

        # Sample by traveled distance and keep every route corner.
        # Fixed checkpoint coordinates are never moved to fit the target.
        if route_polyline is not None:
            waypoints = []
            for point in route_polyline:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError("route_polyline points must be [lat, lon] pairs")
                waypoints.append((float(point[0]), float(point[1])))
            if len(waypoints) < 2:
                raise ValueError("route_polyline requires at least two points")
        elif route_points is not None:
            waypoints = []
            for point in route_points:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError("route_points must be [lat, lon] pairs")
                waypoints.append((float(point[0]), float(point[1])))
            if len(waypoints) < 2:
                raise ValueError("route_points requires at least two points")
        elif five_points:
            # 策略：路线用 GCJ-02（glat/glon），fivePointJson 保留原始 BD-09。
            waypoints = [point.route_coordinates() for point in five_points]
        samples = sample_route(waypoints, total_distance_m, total_time_sec, start_time_ms,
                               require_all=bool(five_points),
                               seed=derive_seed(self.uuid_str, 4),
                               required_points=[point.route_coordinates()
                                                for point in five_points])
        for index, (lat, lon, timestamp) in enumerate(samples):
            if index:
                previous = samples[index-1]
                speed = distance(previous[:2], (lat, lon))*1000/(timestamp-previous[2])
            else:
                speed = total_distance_m/total_time_sec
            points.append(GpsPoint(lat=lat, lon=lon, time=timestamp,
                                   speed=speed, accuracy=3.0, isValid=True))

        # 同一次运行可复现：海拔波动从 uuid 派生种子。
        seed = (derive_seed(self.uuid_str, 0) if altitude_seed is None
                else int(altitude_seed))
        for point, value in zip(points, elevation_profile(len(points), seed)):
            point.altitude = value

        # 单点 speed / avgSpeed 直接取相邻点距离 ÷ 时间：轨迹已是整秒时间轴，
        # 几何速度天然平滑，不再叠加 10 秒随机桶（否则详情页配速图会出现台阶）。
        check_track(points, total_distance_m, total_time_sec, start_time_ms,
                    start_time_ms+total_time_sec*1000, five_points)

        return points, five_points

    def build_submission_payload(self,
                                total_distance_m: int,
                                total_time_sec: int,
                                total_steps: int,
                                start_time_ms: int,
                                stop_time_ms: int,
                                gps_points: List[GpsPoint],
                                five_points: List[FivePoint],
                                sel_distance_m: int = 2000,
                                sel_run_time_s: int = 0,
                                calorie: int = 135,
                                avg_power: int = 150,
                                run_area_id: int = -1,
                                geo_fences_json: Optional[str] = None,
                                policy: int = 1,
                                loc_json: Optional[str] = None,
                                stride_m: float = DEFAULT_STRIDE_M,
                                pace_bounds: Optional[Tuple[float, float]] = None) -> Tuple[dict, dict, str]:
        """组装完整的 save/record 报文、OBS 上传大对象与相对路径。

        ``loc_json`` 可传入已生成的官方 LocJsonEntity（例如从 outbox 重放）；
        未提供时按当前 GPS 点生成。无论哪种，save_record 与 OBS run_data 必须同源。
        """
        validate_totals(total_time_sec, total_steps, start_time_ms, stop_time_ms, total_distance_m)
        check_track(gps_points, total_distance_m, total_time_sec, start_time_ms, stop_time_ms, five_points)

        # 10 秒分桶只生成一次，speedPerTenSec / stepsPerTenSec / stepDistance
        # 必须同源，否则详情页的配速、步频、步幅图会互相矛盾。
        bucket_lengths = bucket_distances(total_distance_m, total_time_sec,
                                          derive_seed(self.uuid_str, 1))
        bucket_counts = bucket_steps(total_steps, total_time_sec,
                                     derive_seed(self.uuid_str, 2))

        # 上传轨迹的海拔：优先使用已有 loc_json（outbox 重放），否则取 GpsPoint。
        if loc_json is not None:
            parsed_points = validate_loc_json(loc_json, require_official=True,
                                              strict_order=True)
            altitudes = [float(point.get("bdA", 0.0) or 0.0)
                         for point in parsed_points]
            if len(altitudes) != len(gps_points):
                altitudes = [float(getattr(point, "altitude", 0.0) or 0.0)
                             for point in gps_points]
        else:
            altitudes = [float(getattr(point, "altitude", 0.0) or 0.0)
                         for point in gps_points]
            # 累计步距与 speedPerTenSec 同源：把每 10 秒距离展开到每个点。
            # 详情页的步幅图因此等于“距离/步数”，并保留宽幅随机波动；
            # stride_m 仅保留为旧调用兼容参数。
            for point, value in zip(
                    gps_points,
                    cumulative_from_buckets(
                        [item.time for item in gps_points], start_time_ms,
                        bucket_lengths, total_time_sec)):
                point.step_distance = round(value, 3)

        ascent_value = total_ascent(altitudes)
        laps = build_laps(gps_points, total_distance_m, total_time_sec,
                          total_steps, start_time_ms, altitudes=altitudes,
                          lap_seed=derive_seed(self.uuid_str, 3),
                          pace_bounds=pace_bounds)

        complete = (total_distance_m >= sel_distance_m) and (total_time_sec >= sel_run_time_s)
        un_complete_reason = 0 if complete else 10
        avg_step_freq = round(total_steps * 60.0 / total_time_sec) if total_time_sec > 0 else 165
        # 官方 UploadFormatEntity.setSpeed() 接收“分钟/公里 × 1000”，
        # 不是“米/秒 × 1000”。用错单位会让服务端把 5'53" 读成 2'50"，
        # 并触发配速校验失败。
        speed_val = (
            int(round((total_time_sec / 60.0) / (total_distance_m / 1000.0), 2) * 1000)
            if total_time_sec > 0 and total_distance_m > 0 else 6000
        )

        # 1. 签名参数字典 (严格按照 7.3.92 规则)
        sign_map = {
            "address": "",
            "avgPower": avg_power,
            "avgStepFreq": avg_step_freq,
            "calorie": calorie,
            "complete": complete,
            "errorCode": 0,
            "faceCheck": 0,
            "geeToken": "",
            "getPrize": False,
            "goalId": "null",
            "policy": 1,
            "selDistance": sel_distance_m,
            "selRunTime": sel_run_time_s,
            "selectedUnid": self.unid,
            "speed": speed_val,
            "sportType": self.sport_type,
            "startTime": start_time_ms,
            "status": 1,
            "stopTime": stop_time_ms,
            "themeId": 0,
            "totalAscent": ascent_value,
            "totalDis": total_distance_m,
            "totalSteps": total_steps,
            "totalTime": total_time_sec,
            "uid": self.uid,
            "unCompleteReason": un_complete_reason,
            "unauthorized": 0,
            "useMobilityTools": 0,
            "uuid": self.uuid_str,
            "validDis": total_distance_m,
            "validTime": total_time_sec
        }
        sign_map["policy"] = policy
        signature = compute_sport_signature(sign_map)
        original_sign = compute_original_sign(sign_map)

        # 2. 生成 10 秒步频与速度分桶（带热身/降速/噪声，但总和精确不变）
        speed_buckets = []
        step_buckets = []
        slices = bucket_slices(total_time_sec)
        for index, (begin, end) in enumerate(slices):
            speed_buckets.append(SpeedBucket(bucket_lengths[index],
                                             start_time_ms + begin,
                                             start_time_ms + end,
                                             queueNum=index + 1))
            step_buckets.append(StepBucket(bucket_counts[index],
                                           start_time_ms + begin,
                                           start_time_ms + end,
                                           queueNum=index + 1))

        if loc_json is None:
            loc_json = build_loc_json(gps_points, start_time_ms)
        else:
            validate_loc_json(loc_json, require_official=True, strict_order=True)
        all_loc_json = loc_json
        five_pt_json = json.dumps([p.to_dict() for p in five_points], separators=(",", ":")) if five_points else "[]"
        # Android CreditRunPIml serializes PointJsonEntity, not the point array.
        # The inner fivePointJson remains a JSON string in this envelope.
        if self.sport_type == 1:
            envelope = {"useZip": False, "fivePointJson": five_pt_json,
                        "runAreaId": run_area_id, "freedomShowFence": False}
            if geo_fences_json is not None:
                envelope["geoFencesJson"] = geo_fences_json
            five_pt_json = json.dumps(envelope, separators=(",", ":"))
        speed_json = json.dumps([b.to_dict() for b in speed_buckets], separators=(",", ":"))
        step_json = json.dumps([b.to_dict() for b in step_buckets], separators=(",", ":"))
        laps_json = json.dumps(laps, separators=(",", ":"), ensure_ascii=False)

        # 3. 组装 save/record 报文体
        first_pt = gps_points[0] if gps_points else GpsPoint(0.0, 0.0, start_time_ms)
        save_record = {
            "address": "",
            "allLocJson": all_loc_json,
            "avgPower": avg_power,
            "avgStepFreq": avg_step_freq,
            "calorie": calorie,
            "complete": complete,
            "errorCode": 0,
            "faceCheck": 0,
            "fivePointJson": five_pt_json,
            "geeToken": "",
            "getPrize": False,
            "goalId": None,
            "isUpload": False,
            "latitude": first_pt.lat,
            "longitude": first_pt.lon,
            "maxRunTime": 7200,
            "minSteps": 2000,
            "originalSign": original_sign,
            "policy": 1,
            "recordUrl": "",
            "roomId": 0,
            "segmentJson": "[]",
            "selDistance": sel_distance_m,
            "selRunTime": sel_run_time_s,
            "selectedUnid": self.unid,
            "signature": signature,
            "speed": speed_val,
            "speedPerTenSec": [b.to_dict() for b in speed_buckets],
            "sportType": self.sport_type,
            "startTime": start_time_ms,
            "status": 1,
            "stepsPerTenSec": [b.to_dict() for b in step_buckets],
            "stopTime": stop_time_ms,
            "themeId": 0,
            "totalAscent": ascent_value,
            "totalDis": total_distance_m,
            "totalSteps": total_steps,
            "totalTime": total_time_sec,
            "uid": self.uid,
            "unCompleteReason": un_complete_reason,
            "unauthorized": 0,
            "useMobilityTools": 0,
            "uuid": self.uuid_str,
            "validDis": total_distance_m,
            "validTime": total_time_sec
        }

        save_record["policy"] = policy

        # 4. 组装 OBS Gzip-Base64 载荷
        obs_payload = {
            "rrid": gzip_base64_encode(""),
            "uid": gzip_base64_encode(str(self.uid)),
            "uuid": gzip_base64_encode(self.uuid_str),
            "run_data": encode_run_data(all_loc_json),
            "step_freq_json": gzip_base64_encode(step_json),
            "speed_json": gzip_base64_encode(speed_json),
            "segment_json": gzip_base64_encode("[]"),
            "fixed_point_json": gzip_base64_encode(five_pt_json),
            "runFaceCheck": gzip_base64_encode(""),
            "extension_json": gzip_base64_encode(""),
            "laps_json": gzip_base64_encode(laps_json)
        }

        # 5. 生成 OBS 目标对象 Key
        hour_str = time.strftime("%Y%m%d%H", time.localtime(start_time_ms / 1000.0))
        object_key = f"run_data/{hour_str}/{self.uuid_str}.json"

        return save_record, obs_payload, object_key
