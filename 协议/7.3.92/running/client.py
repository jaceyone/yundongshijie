# -*- coding: utf-8 -*-
import json
import math
import os
import sys
import time
import urllib.request
import random
import uuid
from dataclasses import dataclass
from typing import Optional, Dict, Any, Union, List, Callable

cur_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(cur_dir)
for p in [cur_dir, parent_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.client import BaseNetSecClient
from profile.client import ProfileClient
from policy.client import PolicyClient, normalise_anchor_gcj
from history.client import HistoryClient
from history.models import format_scaled_speed, parse_history_records
from telemetry.client import TelemetryClient
from .config import (
    URL_OBS_TEMPORARY_URL,
    URL_SAVE_RECORD,
    URL_RUN_DETAIL_CONFIG,
    OBS_BUCKET_NAME
)
from .models import RunningSubmissionResult, RouteDecision
from .builder import RunningTrackBuilder
from .consistency import distance
from .osm import OsmRouteError, TrackPlanner
from .outbox import (
    Outbox,
    STATUS_FAILED,
    STATUS_OBS_UPLOADED,
    STATUS_PENDING,
    STATUS_SAVED,
    STATUS_VERIFIED,
)
from .route_order import select_official_order
from .schema import decode_run_data, validate_loc_json


def _pace_bounds(rule) -> tuple:
    """从学校规则取出配速上下限（分钟/公里），无法解析时用 4.0–12.0。"""
    try:
        fast = float(getattr(rule, "speed_top", 0))
        slow = float(getattr(rule, "speed_bottom", 0))
    except (TypeError, ValueError):
        return (4.0, 12.0)
    if fast <= 0 or slow <= 0 or fast == slow:
        return (4.0, 12.0)
    return (min(fast, slow), max(fast, slow))


MANUAL_CHECKPOINT_TOLERANCE_M = 0.1
MANUAL_POINT_MATCH_M = 5.0


class ManualRouteError(ValueError):
    """手动路线 provider 返回值的可读错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class RouteContext:
    """传给 route_provider 的本次运行上下文（取点之后、排序/规划之前）。"""

    rule: Any
    fence: Any
    raw_checkpoints: List[Dict[str, Any]]
    unid: int
    area_id: int
    run_uuid: str
    target_distance_m: int
    target_time_sec: int
    campus: Optional[str] = None
    # 本次取点锚点（GCJ-02）与来源标识；只用于展示/记录，不参与路线匹配。
    anchor_gcj: Optional[Any] = None
    anchor_id: Optional[str] = None
    anchor_name: Optional[str] = None


def _point_coordinate(point: Any) -> tuple:
    """取打卡点路线坐标：优先 glat/glon（GCJ-02），回退 lat/lng。"""
    if isinstance(point, dict):
        lat = point.get("glat", point.get("lat"))
        lon = point.get("glon", point.get("glng", point.get("lon", point.get("lng"))))
    else:
        route = getattr(point, "route_coordinates", None)
        if callable(route):
            return route()
        lat = getattr(point, "lat", None)
        lon = getattr(point, "lon", getattr(point, "lng", None))
    if lat is None or lon is None:
        raise ValueError("打卡点缺少 glat/glon 与 lat/lng")
    return float(lat), float(lon)


def _normalise_manual_polyline(value: Any) -> List[tuple]:
    """校验 provider 折线：有限坐标、至少 2 个顶点、去掉连续重复点。"""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ManualRouteError("manual_route_invalid", "折线至少需要 2 个顶点")
    route: List[tuple] = []
    for index, item in enumerate(value):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ManualRouteError(
                "manual_route_invalid", f"第 {index + 1} 个顶点不是 [lat, lon]")
        try:
            lat, lon = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            raise ManualRouteError(
                "manual_route_invalid", f"第 {index + 1} 个顶点坐标不是数字")
        if (not math.isfinite(lat) or not math.isfinite(lon)
                or not (-90 <= lat <= 90) or not (-180 <= lon <= 180)):
            raise ManualRouteError(
                "manual_route_invalid", f"第 {index + 1} 个顶点坐标超出范围")
        if not route or distance(route[-1], (lat, lon)) > 1e-6:
            route.append((lat, lon))
    if len(route) < 2:
        raise ManualRouteError("manual_route_invalid", "折线去重后不足 2 个顶点")
    return route


def _polyline_length(points: List[tuple]) -> float:
    return sum(distance(a, b) for a, b in zip(points, points[1:]))


def _validate_manual_checkpoints(raw: List[Dict[str, Any]],
                                 ordered: Any) -> List[Dict[str, Any]]:
    """校验 provider 返回的点位顺序，并用当次原始点位重建 0 基 position。"""
    if not isinstance(ordered, (list, tuple)) or len(ordered) != len(raw):
        raise ManualRouteError(
            "manual_route_checkpoint_mismatch",
            f"返回点位数 {len(ordered) if isinstance(ordered, (list, tuple)) else '?'} "
            f"与当次官方点位数 {len(raw)} 不一致")
    if len(raw) < 2:
        raise ManualRouteError("manual_route_checkpoint_mismatch", "官方点位不足 2 个")

    used = set()
    result: List[Dict[str, Any]] = []
    for index, point in enumerate(ordered):
        if not isinstance(point, dict):
            raise ManualRouteError(
                "manual_route_checkpoint_mismatch", f"第 {index + 1} 个点位不是对象")
        try:
            lat, lon = _point_coordinate(point)
        except (TypeError, ValueError):
            raise ManualRouteError(
                "manual_route_checkpoint_mismatch", f"第 {index + 1} 个点位缺少坐标")
        matched = None
        point_id = point.get("id")
        if point_id is not None:
            for candidate_index, candidate in enumerate(raw):
                if candidate_index in used:
                    continue
                if candidate.get("id") == point_id:
                    matched = candidate_index
                    break
        if matched is None:
            best = None
            for candidate_index, candidate in enumerate(raw):
                if candidate_index in used:
                    continue
                try:
                    candidate_lat, candidate_lon = _point_coordinate(candidate)
                except (TypeError, ValueError):
                    continue
                gap = distance((lat, lon), (candidate_lat, candidate_lon))
                if gap <= MANUAL_POINT_MATCH_M and (best is None or gap < best[0]):
                    best = (gap, candidate_index)
            if best is not None:
                matched = best[1]
        if matched is None:
            raise ManualRouteError(
                "manual_route_checkpoint_mismatch",
                f"第 {index + 1} 个返回点位与当次官方点位不匹配")
        used.add(matched)
        item = dict(raw[matched])
        item["position"] = index
        result.append(item)

    if len(used) != len(raw):
        raise ManualRouteError(
            "manual_route_checkpoint_mismatch", "返回点位没有覆盖全部当次官方点位")
    return result


def _checkpoint_building_ids(planner: TrackPlanner,
                             checkpoints: List[Dict[str, Any]]) -> set:
    """官方点落在 OSM 建筑内时，只放行这些建筑的短接入通道。"""
    allowed = set()
    for checkpoint in checkpoints:
        try:
            lat, lon = _point_coordinate(checkpoint)
        except (TypeError, ValueError):
            continue
        building = planner.campus.building_at(lat, lon)
        if building is not None:
            allowed.add(int(building.osm_id))
    return allowed


def resolve_path_source(path_source: str, sport_type: int) -> str:
    """把 ``auto`` 解析成实际路线来源。

    计分跑固定使用 OSM 避建筑路线；自由跑没有官方打卡点，内部仍用合成
    轨迹。``synthetic`` / ``osm`` 只保留给协议内部测试与显式调用，CLI 与
    一键脚本不再暴露这个开关。
    """
    value = str(path_source or "auto").strip().lower()
    if value == "auto":
        return "osm" if int(sport_type) == 1 else "synthetic"
    return value


class RunningClient(BaseNetSecClient):
    """运动世界校园 7.3.92 跑步提交与 OBS 上传客户端"""

    def __init__(self,
                 uid: Optional[Union[int, str]] = None,
                 token: Optional[str] = None,
                 unid: Optional[Union[int, str]] = None,
                 device_config: Optional[Dict[str, Any]] = None):
        super().__init__(uid=uid, token=token, unid=unid, device_config=device_config)
        self.telemetry = TelemetryClient(device_config=self.device_config)
        self.profile = ProfileClient(uid=uid, token=token, unid=unid, device_config=self.device_config)
        self.policy = PolicyClient(uid=uid, token=token, unid=unid, device_config=self.device_config)
        self.history = HistoryClient(uid=uid, token=token, unid=unid, device_config=self.device_config)

    def set_credentials(self, uid: Optional[Union[int, str]], token: Optional[str], unid: Optional[Union[int, str]] = None):
        super().set_credentials(uid, token, unid)
        self.profile.set_credentials(uid, token, unid)
        self.policy.set_credentials(uid, token, unid)
        self.history.set_credentials(uid, token, unid)

    def request_obs_url(self, object_key: str, method: str = "Put",
                        log: Optional[Callable[[str], None]] = None) -> dict:
        """向网关申请华为云 OBS 预签名地址（Put 上传 / Get 回读）。

        该请求只换取预签名 URL，不写任何服务端状态，可安全重试。
        """
        body = {
            "bucketName": OBS_BUCKET_NAME,
            "objectKey": object_key,
            "method": method,
            "contentType": "text/plain"
        }
        res = self.execute_request("POST", URL_OBS_TEMPORARY_URL, body_plain=body,
                                   retries=2, log=log)
        if isinstance(res, dict) and res.get("error") == 10000 and "data" in res:
            data = res["data"]
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    pass
            return {"error": 10000, "data": data}
        return res if isinstance(res, dict) else {"error": -1, "message": str(res)}

    def request_obs_upload_url(self, object_key: str,
                               log: Optional[Callable[[str], None]] = None) -> dict:
        """向网关申请华为云 OBS 预签名上传地址 (/api/obs/temporary/url)"""
        return self.request_obs_url(object_key, method="Put", log=log)

    def request_obs_download_url(self, object_key: str,
                                 log: Optional[Callable[[str], None]] = None) -> dict:
        """向网关申请华为云 OBS 预签名下载地址。"""
        return self.request_obs_url(object_key, method="Get", log=log)

    def upload_to_obs(self, signed_url: str, signed_headers: dict, payload: dict) -> dict:
        """直接以 PUT 请求向华为云 OBS 存储桶异步推送轨迹与步频载荷"""
        data_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = dict(signed_headers or {"Content-Type": "text/plain"})
        req = urllib.request.Request(signed_url, data=data_bytes, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=30) as resp:
                if 200 <= resp.status < 300:
                    return {"error": 10000, "message": "OBS 上传成功"}
                return {"error": resp.status, "message": f"OBS HTTP {resp.status}"}
        except urllib.error.HTTPError as e:
            return {"error": e.code, "message": f"OBS 上传失败: {e.reason}"}
        except Exception as e:
            return {"error": -1, "message": f"OBS 网络异常: {e}"}

    def download_from_obs(self, signed_url: str,
                          signed_headers: Optional[dict] = None) -> dict:
        """通过预签名 GET 地址回读 OBS 对象。"""
        req = urllib.request.Request(signed_url,
                                     headers=dict(signed_headers or {}),
                                     method="GET")
        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=30) as resp:
                data = resp.read()
                if 200 <= resp.status < 300:
                    return {"error": 10000, "data": data}
                return {"error": resp.status, "message": f"OBS HTTP {resp.status}"}
        except urllib.error.HTTPError as e:
            return {"error": e.code, "message": f"OBS 回读失败: {e.reason}"}
        except Exception as e:
            return {"error": -1, "message": f"OBS 回读网络异常: {e}"}

    def verify_uploaded_track(self,
                              object_key: Optional[str] = None,
                              expected_loc_json: Optional[str] = None,
                              rrid: Optional[int] = None,
                              uuid_str: Optional[str] = None,
                              start_time_ms: Optional[int] = None) -> dict:
        """回读 OBS run_data，校验 LocJsonEntity 可解析且与本地报文同源。

        官方 App 先按 ``run_data/<yyyyMMddHH>/<uuid>.json`` 下载，失败再按
        ``run_data/<rrid/1000000>/<rrid>.json`` 回退。这里按同样的顺序尝试；
        仍兼容脚本自己的 ``object_key``。
        """
        keys: List[str] = []
        if uuid_str and start_time_ms is not None:
            hour = time.strftime("%Y%m%d%H",
                                 time.localtime(int(start_time_ms) / 1000.0))
            keys.append(f"run_data/{hour}/{uuid_str}.json")
        if rrid is not None:
            keys.append(f"run_data/{int(rrid) // 1000000}/{rrid}.json")
        if object_key:
            keys.append(str(object_key))
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {"ok": False,
                    "error": "缺少 OBS 键（提供 object_key、uuid+startTime 或 rrid）"}

        errors: List[str] = []
        loc_json = None
        points = None
        used_key = None
        for key in keys:
            res = self.request_obs_download_url(key)
            if res.get("error") != 10000 or not res.get("data"):
                errors.append(f"{key}: {res.get('message', '下载凭证失败')}")
                continue
            info = res["data"] if isinstance(res["data"], dict) else {}
            signed_url = info.get("signedUrl")
            if not signed_url:
                errors.append(f"{key}: 缺少 signedUrl")
                continue
            dl = self.download_from_obs(
                signed_url,
                info.get("actualSignedRequestHeaders") or {})
            if dl.get("error") != 10000:
                errors.append(f"{key}: {dl.get('message', 'OBS 回读失败')}")
                continue
            try:
                text = dl["data"].decode("utf-8", errors="replace")
                # OBS 对象是包含 run_data 等字段的外层 JSON；run_data 自身才是
                # gzip+base64 的 LocJsonEntity。也兼容直接下载 run_data 的情况。
                outer = None
                try:
                    outer = json.loads(text)
                except json.JSONDecodeError:
                    outer = None
                if isinstance(outer, dict) and "run_data" in outer:
                    candidate = decode_run_data(outer["run_data"])
                else:
                    candidate = decode_run_data(text)
                candidate_points = validate_loc_json(candidate, require_official=True)
            except (TypeError, ValueError, UnicodeDecodeError, KeyError) as exc:
                errors.append(f"{key}: run_data 解析失败: {exc}")
                continue
            loc_json = candidate
            points = candidate_points
            used_key = key
            break

        if loc_json is None or points is None:
            return {"ok": False,
                    "error": "OBS 回读失败: " + "; ".join(errors),
                    "keys": keys}

        if expected_loc_json is not None:
            try:
                expected = validate_loc_json(expected_loc_json, require_official=True)
                expected_obj = (json.loads(expected_loc_json)
                                if isinstance(expected_loc_json, str)
                                else expected_loc_json)
                same = json.loads(loc_json) == expected_obj
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                return {"ok": False, "error": f"本地轨迹校验失败: {exc}"}
            if len(points) != len(expected):
                return {"ok": False,
                        "error": f"回读点数 {len(points)} != 本地 {len(expected)}"}
            if not same:
                return {"ok": False, "error": "OBS run_data 与 save_record.allLocJson 不一致"}

        record = None
        if rrid is not None:
            try:
                record = self.history.get_one_record(rrid=rrid)
            except Exception as exc:
                return {"ok": False, "error": f"成绩详情回查失败: {exc}",
                        "points": len(points), "loc_json": loc_json}
        return {"ok": True, "points": len(points), "object_key": used_key,
                "loc_json": loc_json, "record": record}

    @staticmethod
    def export_track_geojson(gps_points: List[Any], path: str,
                             properties: Optional[dict] = None) -> str:
        """把轨迹写成 GeoJSON LineString，便于在地图上人工检查。"""
        if not gps_points:
            raise ValueError("没有可导出的轨迹点")
        feature = {
            "type": "Feature",
            "properties": dict(properties or {}),
            "geometry": {
                "type": "LineString",
                "coordinates": [[float(p.lon), float(p.lat)] for p in gps_points],
            },
        }
        collection = {"type": "FeatureCollection", "features": [feature]}
        target = os.path.abspath(os.path.expanduser(str(path)))
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(collection, handle, ensure_ascii=False, indent=2)
        return target

    def submit_record(self, save_body: dict) -> dict:
        """核心跑步记录结算上报 (/api/v70260/runnings/save/record)

        不自动重试：请求可能已被服务端写入，盲目重发会产生重复成绩；
        重试时先由 ``_find_existing_rrid`` 按 startTime 找回已有记录。
        """
        return self.execute_request("POST", URL_SAVE_RECORD, body_plain=save_body)

    def get_rundetail_config(self, sport_type: int = 1) -> dict:
        """拉取跑步详情页配置 (/api/v70340/activity/rundetail/config)"""
        return self.execute_request("GET", f"{URL_RUN_DETAIL_CONFIG}?sportType={sport_type}",
                                    retries=1)

    def execute_full_run(self,
                         target_distance_m: int = 3000,
                         target_time_sec: int = 1060,
                         sport_type: int = 1,
                         progress_callback: Optional[Callable[[str], None]] = None,
                         route_polyline: Optional[List[Any]] = None,
                         path_source: str = "auto",
                         campus: Optional[str] = None,
                         dry_run: bool = False,
                         export_track: Optional[str] = None,
                         outbox_dir: Optional[str] = None,
                         verify_upload: bool = True,
                         route_provider: Optional[Callable[[RouteContext], Any]] = None,
                         anchor_gcj: Optional[Any] = None,
                         anchor_id: Optional[str] = None,
                         anchor_name: Optional[str] = None
                         ) -> RunningSubmissionResult:
        """按照 7.3.92 实机抓包全时序规范执行拟人跑步结算全流程。

        - ``route_polyline``：外部路线（GCJ-02 的 [lat, lon] 序列），优先于合成点；
        - ``path_source``：``auto``（默认：计分跑走 OSM 避建筑，自由跑走内部合成）、
          ``osm`` 或 ``synthetic``（后两者仅保留给协议内部测试与显式调用）
          （用打包的 OSM 校园栅格规划避建筑闭环，仅支持计分跑）；
        - ``campus``：OSM 校园 id，默认 ``xuchang_280173698``；
        - ``dry_run``：生成并校验轨迹、可导出 GeoJSON，但不申请 OBS、不写服务器；
        - ``export_track``：把最终 GPS 点写成 GeoJSON，便于地图检查；
        - ``outbox_dir``：提交落盘目录，默认 ``~/.campusrun/outbox``；
        - ``verify_upload``：结算后回读 OBS run_data，确认官方 schema 可解析；
        - ``route_provider``：手动路线回调。拿到当次官方点位后、官方排序/OSM
          规划之前调用，返回 ``(polyline, ordered_checkpoints)``、``RouteDecision``
          或 ``None``。返回折线时跳过官方排序与 OSM 规划，并按返回顺序写 position；
          ``RouteDecision.osm()`` 复用本次点位改走 OSM，``RouteDecision.cancel()``
          不提交。
        - ``anchor_gcj``：本次取点锚点（GCJ-02）。提供时官方 5 点接口用它作为
          ``latitude/longitude``；未提供时回退围栏第一个顶点（旧行为）。
        - ``anchor_id`` / ``anchor_name``：锚点来源标识，只写日志与路线库历史。
        """
        def log(msg: str):
            if progress_callback:
                progress_callback(msg)

        if not self.uid or not self.token:
            return RunningSubmissionResult(success=False, message="缺少鉴权凭据 (uid/token)")
        if route_provider is not None and int(sport_type) != 1:
            return RunningSubmissionResult(
                success=False, message="route_provider 只支持计分跑 (sportType=1)")

        anchor = None
        if anchor_gcj is not None:
            try:
                anchor = normalise_anchor_gcj(anchor_gcj)
            except (TypeError, ValueError) as exc:
                return RunningSubmissionResult(
                    success=False, message=f"anchor_invalid: {exc}")

        # Use the same school policy for point issuance and record construction.
        rule = self.policy.get_run_policy(run_mode=sport_type)
        if rule is None:
            detail = getattr(self.policy, "last_policy_error", None)
            suffix = f"（{detail}）" if detail else ""
            return RunningSubmissionResult(
                success=False, message=f"未取得学校规则，停止提交{suffix}")
        try:
            minimum_distance = rule.min_distance
            minimum_time = int((rule.raw.get("runRuleModel") or {}).get("minRunTime", 0))
            policy = int(rule.raw.get("policy", 1))
            pace_bounds = _pace_bounds(rule)
            if minimum_distance <= 0 or minimum_time < 0:
                raise ValueError("学校距离或时长规则无效")
        except (TypeError, ValueError, AttributeError) as exc:
            return RunningSubmissionResult(success=False, message=f"学校规则无法解析: {exc}")
        log(f"学校规则: 最低距离={minimum_distance}m 最低时长={minimum_time}s")

        # 目标距离只设下限：学校最低里程。允许超过 day_goal / 3000 m。
        try:
            target_distance_m = int(target_distance_m)
            target_time_sec = int(target_time_sec)
        except (TypeError, ValueError):
            return RunningSubmissionResult(
                success=False, message="目标距离/时长必须是整数")
        if target_distance_m <= 0 or target_time_sec <= 0:
            return RunningSubmissionResult(
                success=False, message="目标距离/时长必须大于 0")
        if minimum_distance > 0 and target_distance_m < minimum_distance:
            return RunningSubmissionResult(
                success=False,
                message=f"目标距离 {target_distance_m} m 低于学校最低 "
                        f"{minimum_distance} m，停止提交")

        # 1. 冷启动时间校准与行为埋点（演练模式不写行为事件）
        st1 = self.profile.get_server_time()
        t_now_ms = st1.server_timestamp_ms
        log(f"时钟校准: 成功 时间戳={t_now_ms} 偏斜={st1.offset_seconds:+.3f}s")
        if not dry_run and self.unid:
            self.telemetry.send_startup_pv(uid=int(self.uid), unid=int(self.unid))
            self.telemetry.send_page_exposure(unid=int(self.unid))

        # 2. 拉取电子围栏与打卡点。官方 5 点同时带 lat/lng（BD-09）与 glat/glon（GCJ-02）。
        fences = self.policy.get_geo_fence()
        run_uuid = str(uuid.uuid4())
        checkpoints = []
        center_lat, center_lng = 0.0, 0.0
        if fences and len(fences) > 0:
            f = fences[0]
            checkpoints = [cp.as_checkpoint() for cp in f.check_points]
            if f.polygon_points:
                try:
                    center_lat, center_lng = f.polygon_points[0].gcj_coordinates()
                except ValueError:
                    # 官方围栏 lat/lng 可能为 0，真实坐标在 glat/glon。
                    log("围栏关联: 警告 围栏缺少 GCJ 坐标，无法作为路线中心")
            # 手动路线必须基于本次运行当天取到的点位，不使用固定快照。
            needs_live = sport_type == 1 and (
                route_provider is not None
                or anchor is not None
                or not checkpoints
                or any(cp.get("glat") is None or cp.get("glon") is None
                       for cp in checkpoints))
            if needs_live:
                if anchor is not None:
                    label = anchor_name or anchor_id or "自定义"
                    log("取点锚点: %s (%.6f, %.6f) 来源=锚点池"
                        % (label, anchor[0], anchor[1]))
                live = self.policy.get_checkpoints_for_run(
                    f, uuid_str=run_uuid, rule=rule, anchor_gcj=anchor)
                if live:
                    checkpoints = [point.as_checkpoint() for point in live]
                    log(f"官方点位: 成功 来源=pointsResModels 点数={len(checkpoints)}")
                else:
                    log(f"官方点位: 失败 原因={self.policy.last_checkpoint_error}")
                    if route_provider is not None:
                        # 手动路线必须基于当次点位，不能退回围栏里的旧 passPoints。
                        return RunningSubmissionResult(
                            success=False,
                            message="manual_route_checkpoint_mismatch: 未取得当天官方点位，"
                                    f"停止手动路线（{self.policy.last_checkpoint_error}）")
                    if anchor is not None:
                        # 锚点是用户显式指定的取点位置；失败不静默退回围栏顶点。
                        return RunningSubmissionResult(
                            success=False,
                            message="取点锚点请求失败: "
                                    f"{self.policy.last_checkpoint_error}")
            log(f"围栏关联: 成功 名称={f.fence_name} 点数={len(checkpoints)}")
        else:
            log("围栏关联: 未命中，官方计分跑无法生成合法路线")

        # 2.5 路线来源解析；手动 provider 在官方点位就绪后立即调用。
        resolved_source = resolve_path_source(path_source, sport_type)
        if resolved_source not in ("synthetic", "osm"):
            return RunningSubmissionResult(
                success=False, message=f"未知路径源: {path_source}")

        route_points = None
        if sport_type == 1:
            if len(checkpoints) < 2:
                return RunningSubmissionResult(
                    success=False,
                    message="官方点位不足 2 个，停止提交（不再回退到 (0,0) 合成路线）")
            missing = [cp for cp in checkpoints
                       if cp.get("glat") is None or cp.get("glon") is None]
            if missing:
                return RunningSubmissionResult(
                    success=False,
                    message="官方点位缺少 glat/glon（GCJ-02）坐标，停止提交")

            manual_route = None
            if route_provider is not None:
                context = RouteContext(
                    rule=rule,
                    fence=fences[0] if fences else None,
                    raw_checkpoints=[dict(cp) for cp in checkpoints],
                    unid=int(self.unid or 0),
                    area_id=int(getattr(self.policy, "last_checkpoint_area_id", -1) or -1),
                    run_uuid=run_uuid,
                    target_distance_m=int(target_distance_m),
                    target_time_sec=int(target_time_sec),
                    campus=campus or "xuchang_280173698",
                    anchor_gcj=anchor,
                    anchor_id=anchor_id,
                    anchor_name=anchor_name,
                )
                log(f"手动路线: 等待 provider 选择路线 点位={len(checkpoints)} "
                    f"目标={target_distance_m}m")
                try:
                    decision = route_provider(context)
                except Exception as exc:
                    return RunningSubmissionResult(
                        success=False,
                        message=f"manual_route_invalid: 路线回调异常 "
                                f"{type(exc).__name__}: {exc}")
                if isinstance(decision, RouteDecision):
                    mode = str(decision.mode or "").strip().lower()
                    if mode == "cancel":
                        log("路线决策: 用户取消本次提交")
                        return RunningSubmissionResult(
                            success=False,
                            message="manual_route_cancelled: 用户取消本次提交")
                    if mode == "osm":
                        resolved_source = "osm"
                        log("路线决策: 改用 OSM 自动规划（复用本次官方点位，不重新取点）")
                    elif mode == "manual":
                        manual_route = (decision.polyline,
                                        decision.ordered_checkpoints)
                    else:
                        return RunningSubmissionResult(
                            success=False,
                            message=f"manual_route_invalid: 未知路线决策 "
                                    f"{decision.mode}")
                elif decision is None:
                    return RunningSubmissionResult(
                        success=False, message="manual_route_missing: 手动模式未返回路线")
                else:
                    try:
                        manual_polyline, manual_ordered = decision
                    except (TypeError, ValueError):
                        return RunningSubmissionResult(
                            success=False,
                            message="manual_route_invalid: provider 必须返回 "
                                    "(polyline, ordered_checkpoints) 或 RouteDecision")
                    manual_route = (manual_polyline, manual_ordered)

                if manual_route is not None:
                    try:
                        manual_polyline, manual_ordered = manual_route
                        route_polyline = _normalise_manual_polyline(manual_polyline)
                        if _polyline_length(route_polyline) > target_distance_m + 0.001:
                            raise ManualRouteError(
                                "manual_route_too_short",
                                f"折线 {_polyline_length(route_polyline):.1f} m "
                                f"长于目标 {target_distance_m} m")
                        checkpoints = _validate_manual_checkpoints(
                            checkpoints, manual_ordered)
                    except ManualRouteError as exc:
                        return RunningSubmissionResult(
                            success=False, message=str(exc))
                    route_points = [(cp["glat"], cp["glon"]) for cp in checkpoints]
                    resolved_source = "manual"
                    log(f"手动路线: 成功 折线={_polyline_length(route_polyline):.1f}m "
                        f"点数={len(route_polyline)} "
                        f"顺序={[cp.get('position') for cp in checkpoints]}")

            if manual_route is None:
                try:
                    ordering = select_official_order(
                        checkpoints, day_goal=rule.day_goal,
                        min_distance=rule.min_distance,
                        sequence_strength=rule.sequence_strength,
                        rng=random.Random(run_uuid))
                except ValueError as exc:
                    return RunningSubmissionResult(
                        success=False, message=f"官方 5 点排序失败: {exc}")
                checkpoints = ordering.points
                route_points = [(cp["glat"], cp["glon"]) for cp in checkpoints]
                log(f"官方排序: mode={ordering.mode} 直线距离={ordering.distance_m:.1f}m "
                    f"窗口={ordering.low_m:.0f}-{ordering.high_m:.0f}m "
                    f"顺序={[cp.get('position') for cp in checkpoints]}")
        elif center_lat == 0.0 and center_lng == 0.0:
            return RunningSubmissionResult(
                success=False,
                message="未取得有效围栏 GCJ 坐标，停止生成自由跑路线")

        osm_planner = None
        osm_corridor_buildings = set()
        if resolved_source == "osm" and route_polyline is None:
            if sport_type != 1:
                return RunningSubmissionResult(
                    success=False, message="OSM 路径源首版只支持计分跑 (sportType=1)")
            if len(checkpoints) < 2:
                return RunningSubmissionResult(
                    success=False, message="OSM 路线规划失败: 官方点位不足 2 个")
            campus_id = campus or "xuchang_280173698"
            try:
                planner = TrackPlanner.from_campus(campus_id)
                # 官方顺序已经由 route_order 决定；policy=1 让 planner 保持输入顺序，
                # 不再在 A* 层重复做全排列。
                plan = planner.plan(route_points, target_distance_m, policy=1)
            except OsmRouteError as exc:
                return RunningSubmissionResult(
                    success=False,
                    message=f"OSM 路线规划失败 [{exc.code}]: {exc.message}",
                    raw={"osm_error": exc.code, "diagnostics": exc.diagnostics})
            osm_planner = planner
            route_polyline = plan.polyline
            corridors = plan.diagnostics.get("access_corridors") or []
            osm_corridor_buildings = {
                int(corridor["building_id"]) for corridor in corridors
                if corridor.get("building_id") is not None
            }
            if corridors:
                log(f"OSM接入通道: {len(corridors)} 个官方点在建筑内，已打通短通道")
            log(f"OSM路线: 成功 校园={campus_id} 闭环={plan.loop_length_m:.1f}m "
                f"圈数={plan.laps} 点数={len(plan.polyline)} "
                f"最大吸附={max(plan.diagnostics.get('snap_distance_m') or [0]):.1f}m")
        elif resolved_source == "osm":
            log("路线来源: OSM 避建筑（使用外部折线）")
        elif resolved_source == "manual":
            log("路线来源: 手动路线库")
        else:
            log("路线来源: 内部合成（自由跑/显式调用）")

        # 3. 拟人运动学轨迹生成。服务端会把 startTime 取整到秒；这里先对齐，
        #    保证 save_record 与 OBS 的 yyyyMMddHH 键在整点边界一致。
        stop_time_ms = int(round(t_now_ms / 1000.0)) * 1000
        start_time_ms = stop_time_ms - (target_time_sec * 1000)
        total_steps = int((target_distance_m / 0.72) * random.uniform(0.96, 1.04))

        builder = RunningTrackBuilder(uid=int(self.uid), unid=int(self.unid or 0),
                                      sport_type=sport_type, uuid_str=run_uuid)
        try:
            gps_pts, five_pts = builder.generate_track_around_points(
                center_lat=center_lat,
                center_lng=center_lng,
                checkpoints=checkpoints,
                total_distance_m=target_distance_m,
                total_time_sec=target_time_sec,
                start_time_ms=start_time_ms,
                route_polyline=route_polyline,
                route_points=route_points,
            )
        except (TypeError, ValueError) as exc:
            return RunningSubmissionResult(success=False, message=f"本地一致性校验失败: {exc}")

        # 上传前兜底：OSM / 手动路线都对最终采样轨迹再做一次建筑/边界校验。
        # 官方点落在建筑内时，只放行这些建筑的短接入通道。
        if resolved_source in ("osm", "manual"):
            prefix = "OSM" if resolved_source == "osm" else "手动路线"
            try:
                validator = osm_planner or TrackPlanner.from_campus(
                    campus or "xuchang_280173698")
                allowed = (
                    osm_corridor_buildings
                    if resolved_source == "osm"
                    else _checkpoint_building_ids(validator, checkpoints))
                validator.validate_track(
                    [(point.lat, point.lon) for point in gps_pts],
                    allow_building_ids=allowed,
                )
            except OsmRouteError as exc:
                code = exc.code
                if resolved_source == "manual":
                    code = {
                        "osm_building_collision": "manual_route_building_collision",
                        "osm_fence_violation": "manual_route_fence_violation",
                    }.get(exc.code, "manual_route_invalid")
                return RunningSubmissionResult(
                    success=False,
                    message=f"{prefix} 轨迹校验失败 [{code}]: {exc.message}",
                    raw={"osm_error": exc.code, "diagnostics": exc.diagnostics})
            except Exception as exc:
                return RunningSubmissionResult(
                    success=False,
                    message=f"{prefix} 轨迹校验失败: {exc}")

        try:
            save_record, obs_payload, obj_key = builder.build_submission_payload(
                total_distance_m=target_distance_m,
                total_time_sec=target_time_sec,
                total_steps=total_steps,
                start_time_ms=start_time_ms,
                stop_time_ms=stop_time_ms,
                gps_points=gps_pts,
                five_points=five_pts,
                sel_distance_m=minimum_distance,
                sel_run_time_s=minimum_time,
                policy=policy,
                run_area_id=self.policy.last_checkpoint_area_id,
                geo_fences_json=json.dumps([f.raw for f in fences], separators=(",", ":")),
                pace_bounds=pace_bounds,
            )
        except (TypeError, ValueError) as exc:
            return RunningSubmissionResult(success=False, message=f"本地一致性校验失败: {exc}")
        log(f"轨迹合成: 成功 距离={target_distance_m}m 耗时={target_time_sec}s 点数={len(gps_pts)} 步数={total_steps} 配速={format_scaled_speed(save_record['speed'])}")

        if export_track:
            exported = self.export_track_geojson(
                gps_pts, export_track, {
                    "uuid": run_uuid,
                    "sportType": sport_type,
                    "distance_m": target_distance_m,
                    "duration_s": target_time_sec,
                    "point_count": len(gps_pts),
                    "coordinate_system": "GCJ-02",
                })
            log(f"轨迹导出: 成功 路径={exported}")

        if dry_run:
            log("演练完成: 已生成轨迹，未申请 OBS、未写服务器")
            return RunningSubmissionResult(
                success=True,
                message="演练完成：未提交成绩",
                total_distance=float(target_distance_m),
                total_time=int(target_time_sec),
                dry_run=True,
                object_key=obj_key,
                track_points=len(gps_pts),
            )

        # 4. 先落盘 outbox，再上传，避免 OBS 已写而结算响应丢失。
        outbox = Outbox(outbox_dir)
        try:
            outbox_path = outbox.write_pending(run_uuid, save_record, obs_payload, obj_key)
        except Exception as exc:
            return RunningSubmissionResult(success=False, message=f"outbox 落盘失败: {exc}")
        log(f"Outbox: 已落盘 路径={outbox_path}")

        try:
            result = self.submit_from_outbox(
                run_uuid, outbox_dir=outbox_dir,
                verify_upload=verify_upload,
                progress_callback=progress_callback)
        except Exception as exc:
            return RunningSubmissionResult(
                success=False, message=f"上传链路异常: {exc}",
                object_key=obj_key, outbox_path=str(outbox_path))
        result.dry_run = False
        result.track_points = len(gps_pts)
        result.object_key = obj_key
        result.outbox_path = str(outbox_path)
        return result

    def _find_existing_rrid(self, save_record: dict) -> Optional[int]:
        """按 startTime 找回已写入服务端的记录，避免重试产生重复成绩。"""
        start = save_record.get("startTime")
        if start is None:
            return None
        try:
            res = self.history.get_run_records(
                page_num=1, page_size=20,
                sport_type=int(save_record.get("sportType", 1) or 1))
        except Exception:
            return None
        if not isinstance(res, dict) or res.get("error") != 10000:
            return None
        try:
            rows = parse_history_records(res.get("data"))
        except (TypeError, ValueError):
            return None
        for row in rows:
            if str(row.get("startTime")) == str(start):
                value = row.get("rrid") or row.get("id")
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        return None
        return None

    def submit_from_outbox(self, uuid_str: str,
                           outbox_dir: Optional[str] = None,
                           verify_upload: bool = True,
                           progress_callback: Optional[Callable[[str], None]] = None) -> RunningSubmissionResult:
        """从 outbox 继续/重试一次提交；saved 之后只做回读验证。"""
        def log(msg: str):
            if progress_callback:
                progress_callback(msg)

        outbox = Outbox(outbox_dir)
        try:
            entry = outbox.load(uuid_str)
        except Exception as exc:
            return RunningSubmissionResult(success=False, message=f"outbox 读取失败: {exc}")
        save_record = entry.get("save_record") or {}
        obs_payload = entry.get("obs_payload") or {}
        obj_key = entry.get("object_key") or ""
        status = entry.get("status") or STATUS_PENDING
        total_distance = float(save_record.get("totalDis", 0) or 0)
        total_time = int(save_record.get("totalTime", 0) or 0)
        sport_type = int(save_record.get("sportType", 1) or 1)

        if status == STATUS_VERIFIED:
            return RunningSubmissionResult(
                success=True, rrid=entry.get("rrid"), error_code=10000,
                message="该提交已上传并验证完成",
                total_distance=total_distance, total_time=total_time,
                is_valid=True, verified=True, object_key=obj_key,
                outbox_path=str(outbox.path_for(uuid_str)),
            )

        if status in (STATUS_PENDING, STATUS_FAILED):
            obs_res = self.request_obs_upload_url(object_key=obj_key, log=log)
            if obs_res.get("error") != 10000 or not obs_res.get("data"):
                err = obs_res.get("message", "OBS凭证申请失败")
                outbox.mark(uuid_str, STATUS_FAILED, error=err)
                log(f"OBS上传: 失败 原因={err}")
                return RunningSubmissionResult(
                    success=False,
                    message=(f"申请 OBS 上传凭证失败: {err}"
                             "（上游边缘节点连接异常；轨迹已保存，可稍后重试）"),
                    raw=obs_res, object_key=obj_key,
                    outbox_path=str(outbox.path_for(uuid_str)))
            obs_info = obs_res["data"]
            signed_url = obs_info.get("signedUrl")
            signed_headers = (obs_info.get("actualSignedRequestHeaders")
                              or {"Content-Type": "text/plain"})
            put_res = self.upload_to_obs(signed_url, signed_headers, obs_payload)
            if put_res.get("error") != 10000:
                err = put_res.get("message", "推送失败")
                outbox.mark(uuid_str, STATUS_FAILED, error=err)
                log(f"OBS上传: 失败 原因={err}")
                return RunningSubmissionResult(
                    success=False, message=f"推送到 OBS 失败: {err}",
                    raw=put_res, object_key=obj_key,
                    outbox_path=str(outbox.path_for(uuid_str)))
            outbox.mark(uuid_str, STATUS_OBS_UPLOADED)
            status = STATUS_OBS_UPLOADED
            log("OBS上传: 成功 状态=已同步")

        if status == STATUS_OBS_UPLOADED:
            # 结算响应可能丢失而服务端已写入；用 startTime 找回已有 rrid，避免重复提交。
            existing_rrid = self._find_existing_rrid(save_record)
            if existing_rrid is not None:
                rrid = existing_rrid
                outbox.mark(uuid_str, STATUS_SAVED, rrid=rrid)
                log(f"成绩结算: 复用已有 rrid={rrid}")
                sub_res = {"data": {"rrid": rrid}}
            else:
                sub_res = self.submit_record(save_record)
            if existing_rrid is None:
                if not isinstance(sub_res, dict) or sub_res.get("error") != 10000:
                    code = (sub_res.get("error", -1)
                            if isinstance(sub_res, dict) else -1)
                    msg = (sub_res.get("message", "结算上报失败")
                           if isinstance(sub_res, dict) else str(sub_res))
                    outbox.mark(uuid_str, STATUS_FAILED, error=msg)
                    log(f"成绩结算: 失败 code={code} 原因={msg}")
                    return RunningSubmissionResult(
                        success=False, error_code=code, message=msg, raw=sub_res,
                        object_key=obj_key,
                        outbox_path=str(outbox.path_for(uuid_str)))
                data = sub_res.get("data") or {}
                rrid = data.get("rrid") or data.get("id")
                outbox.mark(uuid_str, STATUS_SAVED, rrid=rrid)
                log(f"成绩结算: 成功 rrid={rrid}")
        else:
            rrid = entry.get("rrid")
            sub_res = {"data": {"rrid": rrid}} if rrid is not None else {}

        # 6. 跑后实时成绩与状态回查
        time.sleep(0.5)
        record_detail = None
        if rrid:
            try:
                record_detail = self.history.get_one_record(rrid=rrid)
            except Exception as exc:
                log(f"成绩回查: 失败 原因={exc}")
        is_valid = record_detail.is_valid if record_detail else True

        verified = None
        verification_error = ""
        if verify_upload:
            check = self.verify_uploaded_track(
                obj_key, expected_loc_json=save_record.get("allLocJson"),
                rrid=rrid, uuid_str=save_record.get("uuid"),
                start_time_ms=save_record.get("startTime"))
            if check.get("ok"):
                verified = True
                outbox.mark(uuid_str, STATUS_VERIFIED, rrid=rrid)
                log(f"OBS回读: 成功 点数={check.get('points')}")
            else:
                verified = False
                verification_error = str(check.get("error", "回读失败"))
                outbox.update(uuid_str, last_error=verification_error)
                log(f"OBS回读: 失败 原因={verification_error}")

        # 跑后触发详情配置与看盘回刷
        try:
            self.get_rundetail_config(sport_type=sport_type)
        except Exception:
            pass
        if self.unid:
            try:
                self.telemetry.send_page_exposure(unid=int(self.unid))
            except Exception:
                pass

        log(f"判定结果: {'达标' if is_valid else '未达标'}")
        return RunningSubmissionResult(
            success=True,
            rrid=rrid,
            error_code=10000,
            message="跑步提交与结算完成",
            total_distance=total_distance,
            total_time=total_time,
            is_valid=is_valid,
            raw=sub_res,
            verified=verified,
            object_key=obj_key,
            outbox_path=str(outbox.path_for(uuid_str)),
            verification_error=verification_error,
        )
