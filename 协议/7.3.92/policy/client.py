# -*- coding: utf-8 -*-
import math
import os
import sys
import json
import time
import uuid
import hashlib
from typing import Optional, Dict, Any, Union, List, Sequence, Tuple

cur_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(cur_dir)
for p in [cur_dir, parent_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.client import BaseNetSecClient
from .config import (
    URL_RUN_POLICY,
    URL_INDOOR_POLICY,
    URL_GEO_FENCE,
    URL_SEMESTER_INFO,
    URL_SEMESTER_SUMMARY,
    URL_RUN_GOALS
)
from .models import RunRule, GeoFenceArea, GeoPoint, SemesterInfo
from core.config import HOST_RUN


def normalise_anchor_gcj(anchor: Any) -> Tuple[float, float]:
    """把锚点输入规范化为 GCJ-02 的 (lat, lon)。

    支持 ``(lat, lon)`` 序列与 ``{"glat"/"lat", "glon"/"lon"}`` 字典；
    只接受中国范围内的有限数值，避免把错误坐标发给官方取点接口。
    """
    if isinstance(anchor, dict):
        lat = anchor.get("glat", anchor.get("lat"))
        lon = anchor.get("glon", anchor.get("lon", anchor.get("lng")))
    elif isinstance(anchor, (list, tuple)) and len(anchor) == 2:
        lat, lon = anchor
    else:
        raise ValueError("锚点必须是 (纬度, 经度)")
    if isinstance(lat, bool) or isinstance(lon, bool):
        raise ValueError("锚点坐标必须是数字")
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("锚点坐标必须是数字")
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError("锚点坐标必须是有限数值")
    if not (3.0 <= lat <= 54.0 and 73.0 <= lon <= 136.0):
        raise ValueError("锚点坐标不在中国范围内")
    return lat, lon


def _point_in_polygon(lat: float, lon: float,
                      polygon: Sequence[Tuple[float, float]]) -> bool:
    """射线法；polygon 为 [(lat, lon), ...]，首尾可闭合可不闭合。"""
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


def fence_gcj_polygon(fence: Any) -> List[Tuple[float, float]]:
    """取围栏的 GCJ-02 多边形；缺少 GCJ 坐标的点跳过。

    官方围栏的 lat/lon 是 BD-09、glat/glon 才是 GCJ-02；锚点也是 GCJ，
    所以只用 glat/glon 做包含判断，混用两套坐标会产生数百米误差。
    """
    polygon: List[Tuple[float, float]] = []
    for point in getattr(fence, "polygon_points", None) or []:
        try:
            polygon.append(point.gcj_coordinates())
        except (ValueError, AttributeError, TypeError):
            continue
    return polygon


class PolicyClient(BaseNetSecClient):
    """运动世界校园 7.3.92 跑步规则与地理围栏客户端"""

    # 规则接口偶发超时/连接被重置；一次网络抖动不应废掉整次登录+极验。
    # 业务错误（401/403 等）不重试，只有网络错误、5xx、空 data 才重试。
    POLICY_ATTEMPTS = 3
    POLICY_RETRY_DELAYS = (1.5, 3.0)

    def __init__(self,
                 uid: Optional[Union[int, str]] = None,
                 token: Optional[str] = None,
                 unid: Optional[Union[int, str]] = None,
                 device_config: Optional[Dict[str, Any]] = None):
        super().__init__(uid=uid, token=token, unid=unid, device_config=device_config)
        self.last_checkpoint_area_id: int = -1
        self.last_checkpoint_error: Optional[str] = None
        self.last_checkpoint_start_time: Optional[int] = None
        self.last_checkpoint_anchor: Optional[Dict[str, Any]] = None
        self.last_policy_error: Optional[str] = None

    @staticmethod
    def _describe_policy_failure(res: Any, elapsed: float, attempt: int, attempts: int) -> str:
        """把规则接口的失败原因压缩成一行，供日志与失败文案使用。"""
        if not isinstance(res, dict):
            detail = f"响应类型异常 ({type(res).__name__})"
        else:
            bits = []
            if res.get("status") is not None:
                bits.append(f"HTTP {res['status']}")
            if res.get("error") is not None:
                bits.append(f"code={res['error']}")
            message = res.get("message")
            if message:
                bits.append(str(message))
            if not bits:
                bits.append("响应缺少 error/data")
            detail = " ".join(bits)
        return f"{detail}（第 {attempt}/{attempts} 次，耗时 {elapsed:.1f}s）"

    @staticmethod
    def _policy_failure_retryable(res: Any) -> bool:
        if not isinstance(res, dict):
            return True
        status = res.get("status")
        if isinstance(status, int):
            if 400 <= status < 500 and status != 429:
                return False
            if status >= 500 or status == 429:
                return True
        elif status is not None:
            return True
        code = res.get("error")
        # -1 是 execute_request 对网络异常/超时的统一返回值；10000 但 data 为空
        # 也按服务端偶发处理。其余业务错误码不重试。
        if code is None or code == -1 or code == 10000:
            return True
        return False

    def get_run_policy(self, run_mode: int = 1, unid: Optional[Union[int, str]] = None) -> Optional[RunRule]:
        """获取指定运动模式的跑步规则（配速、步频、单次/单日里程、有效时段等）。

        失败时返回 None，并把最后一次失败原因写入 ``last_policy_error``；
        调用方应把它带进失败文案，避免只报「未取得学校规则」而看不到真实原因。
        """
        url = URL_INDOOR_POLICY if run_mode == 5 else URL_RUN_POLICY
        unid_val = int(unid or self.unid or 0)
        body = {
            "runMode": run_mode,
            "ruleUpdateTime": 0,
            "geoFenceUpdateTime": 0,
            "selectUnid": unid_val,
            "operateType": 0
        }
        attempts = max(1, int(self.POLICY_ATTEMPTS))
        last_error = None
        self.last_policy_error = None
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            res = self.execute_request("POST", url, body_plain=body)
            elapsed = time.monotonic() - started
            if isinstance(res, dict) and res.get("error") == 10000 and "data" in res and res["data"]:
                return RunRule.from_api_data(res["data"])
            last_error = self._describe_policy_failure(res, elapsed, attempt, attempts)
            if attempt >= attempts or not self._policy_failure_retryable(res):
                break
            delay = self.POLICY_RETRY_DELAYS[min(attempt - 1, len(self.POLICY_RETRY_DELAYS) - 1)]
            time.sleep(delay)
        self.last_policy_error = last_error
        return None

    def get_geo_fence(self, update_time: int = 0) -> List[GeoFenceArea]:
        """拉取学校围栏边界；实时点位由 get_run_checkpoints 单独获取。"""
        body = {
            "geoFenceUpdateTime": update_time
        }
        res = self.execute_request("POST", URL_GEO_FENCE, body_plain=body)
        fences = []
        if isinstance(res, dict) and res.get("error") == 10000 and "data" in res and res["data"]:
            d = res["data"]
            raw_list = d.get("geoFences", []) or []
            for item in raw_list:
                fences.append(GeoFenceArea.from_api_data(item))
        return fences

    def get_semester_info(self, run_mode: int = 1) -> Optional[SemesterInfo]:
        """查询学生本学期已完成公里数与剩余目标"""
        body = {
            "runMode": run_mode
        }
        res = self.execute_request("POST", URL_SEMESTER_INFO, body_plain=body)
        if isinstance(res, dict) and res.get("error") == 10000 and "data" in res and res["data"]:
            return SemesterInfo.from_api_data(res["data"])
        return None

    def get_semester_summary(self, sid: int = 0) -> Dict[str, Any]:
        """查询学期总体结算汇总"""
        return self.execute_request("POST", URL_SEMESTER_SUMMARY, body_plain={"sid": sid})

    def get_run_goals(self) -> Dict[str, Any]:
        """查询本学期考核指标与目标"""
        return self.execute_request("POST", URL_RUN_GOALS, body_plain={})


    def get_distance_route_points(self, sport_type: int = 1, distance_m: int = 1000,
                                  latitude: Optional[float] = None,
                                  longitude: Optional[float] = None,
                                  run_area_id: int = -1,
                                  uuid_str: Optional[str] = None,
                                  runec: str = "") -> dict:
        """Query point issuance with a GCJ anchor, not fence boundary points.

        Android requestFivePoint fixes the path key to 1. Source:
        Coaixy/android_wh_physicalfitness_analyse @ 91dc299a, classes
        CommonApiManager$requestFivePoint$1 and Utils.atlasSign.
        """
        if sport_type != 1:
            raise ValueError("点位查询仅支持计分跑，路径 key 固定为 1")
        if isinstance(distance_m, bool) or not isinstance(distance_m, int) or distance_m <= 0:
            raise ValueError("点位查询距离必须是正整数")
        anchor = GeoPoint.from_api_data({"lat": latitude, "lon": longitude})
        if not self.uid or not self.token or int(self.unid or 0) <= 0:
            raise ValueError("点位查询需要有效 UID、Token 和学校 UNID")
        if isinstance(run_area_id, bool) or not isinstance(run_area_id, int) or run_area_id < -1:
            raise ValueError("runAreaId 必须为 -1 或非负整数")
        if not isinstance(runec, str):
            raise ValueError("runec must be a string")
        url = f"{HOST_RUN}/api/v560/get/1/distance/{distance_m}"
        salt = "2slhe02lsfiwowlcixisla_sls-_slaor"
        sign = hashlib.md5((url.replace("https://", "http://", 1)+salt).encode("utf-8")).hexdigest()
        body = {"longitude": anchor.lng, "latitude": anchor.lat,
                "selectedUnid": int(self.unid), "uuid": uuid_str or str(uuid.uuid4()),
                "runec": runec, "runAreaId": run_area_id, "sign": sign}
        return self.execute_request("POST", url, body_plain=body)

    def get_checkpoints_for_run(self, fence: GeoFenceArea,
                                uuid_str: Optional[str] = None,
                                rule: Optional[RunRule] = None,
                                anchor_gcj: Optional[Any] = None) -> List[GeoPoint]:
        """Use the school minimum distance and selected area, as RunStartViewModel does.

        ``anchor_gcj`` 是本次运行的取点锚点（GCJ-02）；未提供时回退到围栏
        多边形第一个顶点（旧行为）。锚点必须在官方围栏内，否则不发请求。
        """
        self.last_checkpoint_error = None
        self.last_checkpoint_start_time = None
        self.last_checkpoint_area_id = -1
        self.last_checkpoint_anchor = None
        try:
            if rule is None:
                rule = self.get_run_policy(run_mode=1)
            if rule is None or rule.min_distance <= 0:
                raise ValueError("未取得有效学校规则，无法确认点位查询距离")
            area_id = -1
            areas = rule.raw.get("runAreaModels")
            if areas:
                valid = [area for area in areas if area.get("valid")]
                selected = [area for area in valid if area.get("select")]
                if len(selected) == 1:
                    area = selected[0]
                elif len(valid) == 1:
                    area = valid[0]
                else:
                    raise ValueError("学校有多个跑步区域，尚未明确选择区域")
                if fence.fence_id not in area.get("fenceIds", []):
                    raise ValueError("当前围栏不属于选定跑步区域")
                area_id = int(area["id"])
            if anchor_gcj is not None:
                latitude, longitude = normalise_anchor_gcj(anchor_gcj)
                polygon = fence_gcj_polygon(fence)
                if len(polygon) >= 3 and not _point_in_polygon(
                        latitude, longitude, polygon):
                    raise ValueError("锚点不在官方围栏内，请重新绘制")
                source = "anchor"
            else:
                if not fence.polygon_points:
                    raise ValueError("围栏没有有效锚点")
                latitude, longitude = fence.polygon_points[0].gcj_coordinates()
                source = "fence"
            self.last_checkpoint_area_id = area_id
            self.last_checkpoint_anchor = {
                "lat": latitude, "lon": longitude, "source": source}
            return self.get_run_checkpoints(
                rule.min_distance, latitude, longitude, area_id, uuid_str,
                anchor_gcj=anchor_gcj)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            self.last_checkpoint_error = f"五点查询未完成: {exc}"
            return []

    def get_run_checkpoints(self, distance_m: int,
                            latitude: Optional[float] = None,
                            longitude: Optional[float] = None,
                            run_area_id: int = -1, uuid_str: Optional[str] = None,
                            runec: str = "",
                            anchor_gcj: Optional[Any] = None) -> List[GeoPoint]:
        """请求官方 5 点；``anchor_gcj`` 提供时覆盖 latitude/longitude。"""
        self.last_checkpoint_error = None
        self.last_checkpoint_start_time = None
        self.last_checkpoint_anchor = None
        try:
            if anchor_gcj is not None:
                latitude, longitude = normalise_anchor_gcj(anchor_gcj)
                source = "anchor"
            elif latitude is None or longitude is None:
                raise ValueError("缺少取点锚点坐标")
            else:
                source = "caller"
            self.last_checkpoint_anchor = {
                "lat": float(latitude), "lon": float(longitude), "source": source}
            result = self.get_distance_route_points(
                distance_m=distance_m, latitude=latitude, longitude=longitude,
                run_area_id=run_area_id, uuid_str=uuid_str, runec=runec)
            if not isinstance(result, dict) or result.get("error") != 10000:
                code = result.get("error", -1) if isinstance(result, dict) else -1
                message = result.get("message", "无错误说明") if isinstance(result, dict) else "响应不是对象"
                self.last_checkpoint_error = f"五点接口 code={code}: {message}"
                return []
            data = result.get("data", result)
            if isinstance(data, str):
                data = json.loads(data)
            if not isinstance(data, dict) or not isinstance(data.get("pointsResModels"), list):
                raise ValueError("五点响应缺少 pointsResModels 数组")
            values = data["pointsResModels"]
            if not values:
                raise ValueError("五点接口返回的 pointsResModels 为空")
            points = [GeoPoint.from_api_data(point) for point in values]
            self.last_checkpoint_start_time = data.get("sportStartTime")
            return points
        except (TypeError, ValueError, KeyError) as exc:
            self.last_checkpoint_error = f"五点请求或响应无法解析: {exc}"
            return []

