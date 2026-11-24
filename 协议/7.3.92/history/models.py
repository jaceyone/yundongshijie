# -*- coding: utf-8 -*-
from dataclasses import dataclass, field
from typing import Optional, Dict, Any


def _reason_complete(reason: Any) -> bool:
    """单项达标条件是否通过；兼容 completeStatus 与 complete 两种字段。"""
    if not isinstance(reason, dict):
        return False
    if "completeStatus" in reason:
        try:
            return int(reason.get("completeStatus") or 0) == 1
        except (TypeError, ValueError):
            return False
    return bool(reason.get("complete"))


def format_pace(pace_min: Any) -> str:
    """把服务端返回的 speed（分钟/公里）格式化为 m'ss"/km。"""
    try:
        value = float(pace_min)
    except (TypeError, ValueError):
        return "-"
    if value <= 0:
        return "-"
    total_seconds = int(round(value * 60))
    return f"{total_seconds // 60}'{total_seconds % 60:02d}\""


def format_scaled_speed(speed_milli_min_per_km: Any) -> str:
    """把上报字段 speed（分钟/公里 × 1000）格式化为 m'ss"/km。"""
    try:
        return format_pace(float(speed_milli_min_per_km) / 1000.0)
    except (TypeError, ValueError):
        return "-"


@dataclass
class RunRecordDetail:
    """单次跑步详情模型 (/api/v70320/runnings/get_one_record)"""
    rrid: int
    sport_type: int
    total_distance: float
    valid_distance: float
    total_time: int
    valid_time: int
    calorie: int
    status: int
    is_valid: bool
    uncomplete_reason: int
    start_time: int
    stop_time: int
    avg_speed: str
    avg_step_freq: int
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> "RunRecordDetail":
        status = int(data.get("status", 0))
        un_reason = int(data.get("unCompleteReason", 0))
        reasons = data.get("reasonList")
        reasons_ok = (
            all(_reason_complete(item) for item in reasons)
            if isinstance(reasons, list) else True
        )
        # 官方 App 语义：status == 0 才是达标；status 1/2 表示服务端校验失败，
        # 即使 unCompleteReason 仍为 0。
        is_valid = (
            status == 0
            and un_reason == 0
            and data.get("complete") is not False
            and reasons_ok
        )
        return cls(
            rrid=int(data.get("rrid", 0) or data.get("id", 0)),
            sport_type=int(data.get("sportType", 1)),
            total_distance=float(data.get("totalDis", 0.0) or data.get("distance", 0.0)),
            valid_distance=float(data.get("validDis", 0.0)),
            total_time=int(data.get("totalTime", 0) or data.get("costTime", 0)),
            valid_time=int(data.get("validTime", 0)),
            calorie=int(data.get("calorie", 0)),
            status=status,
            is_valid=is_valid,
            uncomplete_reason=un_reason,
            start_time=int(data.get("startTime", 0)),
            stop_time=int(data.get("stopTime", 0)),
            avg_speed=str(data.get("speed", "-")),
            avg_step_freq=int(data.get("avgStepFreq", 0)),
            raw=data
        )

def parse_history_records(data):
    """Accept the current list response and the older recordList envelope."""
    if isinstance(data, str):
        import json
        data = json.loads(data)
    if data is None:
        return []
    if isinstance(data, dict):
        if "recordList" not in data:
            raise ValueError("历史响应对象缺少 recordList")
        data = data["recordList"]
        if data is None:
            return []
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError("历史记录应为对象列表")
    return data


def history_record_status(record):
    """与官方 App 对齐：status 0=达标，1=未达标，2=异常。"""
    if record.get("isValid") is True:
        return "达标"
    if record.get("isValid") is False:
        return "未达标"
    status = record.get("status")
    complete = record.get("complete")
    if status == 0:
        if complete is True:
            return "达标"
        if complete is False:
            return "未达标"
    elif status == 1:
        return "未达标"
    elif status == 2:
        return "不达标，存在异常"
    return f"状态码={status}"
