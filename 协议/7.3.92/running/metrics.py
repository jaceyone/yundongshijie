# -*- coding: utf-8 -*-
"""官方详情页指标：海拔 bdA、爬升 totalAscent、步距 stepDistance、每公里 lap。

纯标准库实现，所有随机量都由 uuid 派生的固定种子驱动，同一 uuid 可复现。
单位严格对齐官方 7.3.92：

- ``bdA`` 绝对海拔（米）；
- ``totalAscent``：对 bdA 做三点中值平滑后 ``int(max-min)``（反编译
  ``utils/o0000oo``，端点为原值复制），不小于 0；
- ``LapsBean.duration`` / ``cumulativeDuration`` 秒；每段用时按随机权重生成，
  再用学校配速上下限约束，总和精确等于总时长；
- 完整 1 km：``avgPace = duration/60``（分/公里）、``avgStride = 100000/step``（厘米）；
- 不足 1 km：``avgPace = duration*1000/(distance*60)``、
  ``avgStride = distance*100/step``；
- ``avgCadence = step*60/duration``（步/分）；
- ``stepDistance`` 是累计步距，与 ``speedPerTenSec`` 同源展开，末点等于总距离；
- ``elevationGain`` 为本段 bdA 正增量累加。
"""

import bisect
import hashlib
import math
import random
from typing import List, Optional, Sequence

from .consistency import distance

# 旧版固定步幅；现在仅作为 build_submission_payload 的兼容默认参数保留，
# stepDistance 已改为按 10 秒距离桶展开，不再使用固定步幅。
DEFAULT_STRIDE_M = 0.68
BUCKET_MS = 10000
MIN_ALTITUDE_M = 35.0
MAX_ALTITUDE_M = 80.0
LAP_DISTANCE_M = 1000.0

# LapsBean 的 JSON 字段顺序（Gson 按字段名序列化，顺序仅用于可读性）。
LAP_FIELDS = (
    "lapIndex", "distance", "duration", "cumulativeDuration", "step",
    "avgPace", "avgCadence", "avgStride", "endAltAbs", "endAltRel",
    "elevationGain", "isFullLap", "flag", "id",
)


def derive_seed(uuid_str: str, stream: int = 0) -> int:
    """从 uuid 派生稳定 64 位种子；stream 区分海拔/速度/步频序列。"""
    material = f"{uuid_str}:{int(stream)}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def elevation_profile(count: int, seed: int) -> List[float]:
    """生成校园海拔曲线：基准 45–55 m、平滑起伏 + 少量尖峰，夹在 35–80 m。

    结果保留 3 位小数，与 MyLocation.bdA 的写入精度一致。
    """
    if count <= 0:
        return []
    rng = random.Random(seed)
    base = rng.uniform(45.0, 55.0)
    components = [
        (rng.uniform(1.5, 4.0), rng.uniform(1.0, 3.0), rng.uniform(0.0, 2.0 * math.pi))
        for _ in range(3)
    ]
    spikes = {}
    if count >= 20:
        for _ in range(max(1, count // 150)):
            index = rng.randrange(count)
            spikes[index] = rng.uniform(2.0, 4.0) * (-1.0 if rng.random() < 0.5 else 1.0)

    values: List[float] = []
    denominator = float(count - 1) if count > 1 else 1.0
    for index in range(count):
        x = index / denominator
        altitude = base
        for amplitude, cycles, phase in components:
            altitude += amplitude * math.sin(2.0 * math.pi * cycles * x + phase)
        altitude += rng.gauss(0.0, 0.35)
        altitude += spikes.get(index, 0.0)
        altitude = min(MAX_ALTITUDE_M, max(MIN_ALTITUDE_M, altitude))
        values.append(round(altitude, 3))
    return values


def _median3(first: float, middle: float, last: float) -> float:
    return max(min(first, middle), min(max(first, middle), last))


def smooth_altitudes(values: Sequence[float]) -> List[float]:
    """官方三点平滑（中值滤波）；首尾用原值复制，与反编译实现一致。"""
    result: List[float] = []
    count = len(values)
    for index, value in enumerate(values):
        first = values[index - 1] if index > 0 else value
        last = values[index + 1] if index + 1 < count else value
        result.append(_median3(first, value, last))
    return result


def total_ascent(values: Sequence[float]) -> int:
    """官方兜底爬升：三点平滑后 int(max-min)，不小于 0（Java 截断）。"""
    if len(values) < 2:
        return 0
    smoothed = smooth_altitudes(values)
    return int(max(0.0, max(smoothed) - min(smoothed)))


def bucket_slices(duration_sec: int) -> List[tuple]:
    """10 秒窗口 [(begin_ms, end_ms)]，与 time_buckets 的切片边界一致。"""
    total_ms = int(duration_sec) * 1000
    return [(begin, min(begin + BUCKET_MS, total_ms))
            for begin in range(0, total_ms, BUCKET_MS)]


def fluctuation_weights(duration_sec: int, seed: int, count: int,
                        amplitude: float = 0.08,
                        warmup_s: float = 60.0,
                        cooldown_s: float = 60.0,
                        smooth: bool = True) -> List[float]:
    """每个 10 秒桶的权重：前 60 s 热身、后 60 s 降速，中间按 amplitude 起伏。

    ``smooth=True`` 时对噪声做三点平滑（旧行为）；``smooth=False`` 保留逐桶
    独立随机，让详情页的步频/步幅柱状图呈现明显随机波动。
    """
    rng = random.Random(seed)
    raw = [rng.uniform(-1.0, 1.0) for _ in range(count)]
    if smooth:
        smoothed: List[float] = []
        for index in range(count):
            low, high = max(0, index - 1), min(count, index + 2)
            smoothed.append(sum(raw[low:high]) / float(high - low))
    else:
        smoothed = raw

    weights: List[float] = []
    for index in range(count):
        begin = index * BUCKET_MS
        end = min(begin + BUCKET_MS, int(duration_sec) * 1000)
        midpoint = (begin + end) / 2000.0
        factor = 1.0
        if warmup_s > 0 and midpoint < warmup_s:
            factor *= 0.86 + 0.14 * (midpoint / warmup_s)
        tail_start = max(0.0, float(duration_sec) - cooldown_s)
        if cooldown_s > 0 and duration_sec > tail_start and midpoint > tail_start:
            fraction = min(1.0, (midpoint - tail_start) / cooldown_s)
            factor *= 1.0 - 0.12 * fraction
        factor *= 1.0 + amplitude * smoothed[index]
        weights.append(max(0.4, factor))
    return weights


def apportion_units(total_units: int, weights: Sequence[float],
                    durations_ms: Sequence[int]) -> List[int]:
    """按权重把整数配额分到各桶，保证总和精确等于 total_units。"""
    if total_units < 0:
        raise ValueError("total_units must be nonnegative")
    if len(weights) != len(durations_ms):
        raise ValueError("weights and durations must have the same length")
    cumulative = [0]
    for weight, duration in zip(weights, durations_ms):
        scaled = max(0, int(round(float(weight) * 1_000_000)))
        cumulative.append(cumulative[-1] + scaled * max(0, int(duration)))
    total_weight = cumulative[-1]
    if total_weight <= 0 or total_units == 0:
        result = [0] * len(weights)
        if result:
            result[-1] = total_units
        return result

    result: List[int] = []
    previous = 0
    for value in cumulative[1:]:
        current = total_units * value // total_weight
        result.append(current - previous)
        previous = current
    result[-1] += total_units - previous
    return result


def bucket_distances(total_distance_m: float, duration_sec: int,
                     seed: int, amplitude: float = 0.24,
                     smooth: bool = False) -> List[float]:
    """按波动权重生成 speedPerTenSec 距离（米），总和精确等于总距离。

    默认使用逐桶独立随机（``smooth=False``）和较宽振幅，让 App 的
    配速/步幅柱状图有可见起伏；总和仍精确等于目标距离。
    """
    slices = bucket_slices(duration_sec)
    weights = fluctuation_weights(duration_sec, seed, len(slices),
                                  amplitude=amplitude, smooth=smooth)
    units = apportion_units(int(round(float(total_distance_m) * 100)),
                            weights, [end - begin for begin, end in slices])
    return [unit / 100.0 for unit in units]


def bucket_steps(total_steps: int, duration_sec: int, seed: int,
                 amplitude: float = 0.20,
                 smooth: bool = False) -> List[int]:
    """按波动权重生成 stepsPerTenSec 步数，总和精确等于总步数。

    默认振幅 0.20 且逐桶独立随机；3000 m / 1060 s 下典型步频
    max-min 约 80–110 步/分，满足明显波动的要求。
    """
    slices = bucket_slices(duration_sec)
    weights = fluctuation_weights(duration_sec, seed, len(slices),
                                  amplitude=amplitude, smooth=smooth)
    return apportion_units(int(total_steps), weights,
                           [end - begin for begin, end in slices])


def cumulative_from_buckets(times: Sequence[int], start_time_ms: int,
                            bucket_lengths: Sequence[float],
                            total_time_sec: int) -> List[float]:
    """把 10 秒桶距离展开成每个 GPS 点的累计步距。

    末点精确等于总距离，保证单调递增。用于 ``MyLocation.stepDistance``，
    使详情页的步幅图与 ``speedPerTenSec`` / ``stepsPerTenSec`` 同源。
    """
    if not times:
        return []
    if not bucket_lengths:
        return [0.0] * len(times)
    slices = bucket_slices(total_time_sec)
    if len(slices) != len(bucket_lengths):
        raise ValueError("bucket lengths do not match duration")
    prefix = [0.0]
    for value in bucket_lengths:
        prefix.append(prefix[-1] + float(value))
    total = prefix[-1]
    result: List[float] = []
    for timestamp in times:
        elapsed = max(0, int(timestamp) - int(start_time_ms))
        index = min(len(slices) - 1, elapsed // BUCKET_MS)
        begin, end = slices[index]
        fraction = 0.0 if end <= begin else (elapsed - begin) / float(end - begin)
        value = prefix[index] + float(bucket_lengths[index]) * min(1.0, max(0.0, fraction))
        result.append(min(total, max(0.0, value)))
    result[-1] = total
    for index in range(1, len(result)):
        if result[index] < result[index - 1]:
            result[index] = result[index - 1]
    return result


def cumulative_steps(times: Sequence[int], total_steps: int) -> List[int]:
    """按时间把累计步数分配到尾点，末点精确等于 total_steps。

    每个点尽量至少分到 1 步（总数足够时），因此典型轨迹上 stepDistance 严格递增。
    """
    count = len(times)
    if count == 0:
        return []
    total_ms = int(times[-1]) - int(times[0])
    if total_ms <= 0 or total_steps <= 0:
        return [0] * count

    minimum = 1 if total_steps >= count - 1 else 0
    remaining = total_steps - minimum * (count - 1)
    result: List[int] = []
    for index, timestamp in enumerate(times):
        elapsed = max(0, int(timestamp) - int(times[0]))
        result.append(minimum * index + (remaining * elapsed) // total_ms)
    result[-1] = int(total_steps)
    for index in range(1, count):
        if result[index] < result[index - 1]:
            result[index] = result[index - 1]
    return result


def _sample_at_distance(cumulative: Sequence[float], times: Sequence[int],
                        altitudes: Sequence[float], gains: Sequence[float],
                        target_m: float) -> tuple:
    """在轨迹上按累计距离插值出 (时间, 海拔, 累计正爬升)。"""
    if target_m <= 0:
        return int(times[0]), float(altitudes[0]), float(gains[0])
    if target_m >= cumulative[-1]:
        return int(times[-1]), float(altitudes[-1]), float(gains[-1])

    index = bisect.bisect_right(cumulative, target_m) - 1
    index = min(max(index, 0), len(cumulative) - 2)
    segment = cumulative[index + 1] - cumulative[index]
    fraction = 0.0 if segment <= 0 else (target_m - cumulative[index]) / segment
    timestamp = times[index] + fraction * (times[index + 1] - times[index])
    altitude = altitudes[index] + fraction * (altitudes[index + 1] - altitudes[index])
    gain = gains[index] + max(0.0, altitude - altitudes[index])
    return timestamp, altitude, gain


def _lap_durations(lap_distances: Sequence[float], total_time_sec: int,
                   seed: int, pace_bounds: Optional[tuple]) -> List[int]:
    """生成有随机起伏、且每段配速都在规则内的每公里用时。

    总用时严格等于 ``total_time_sec``；当只有 1 段时无法制造段间差，
    直接返回总用时。
    """
    count = len(lap_distances)
    if count <= 0:
        return []
    if count == 1:
        return [int(total_time_sec)]
    pace_min, pace_max = pace_bounds or (4.0, 12.0)
    pace_min = max(0.1, float(pace_min))
    pace_max = max(pace_min, float(pace_max))
    rng = random.Random(seed)
    weights = [rng.uniform(0.86, 1.14) for _ in range(count)]
    total_weight = sum(weights)
    raw = [float(total_time_sec) * weight / total_weight for weight in weights]
    minimums = [max(1, int(math.ceil(pace_min * distance_m / 1000.0 * 60.0)))
                for distance_m in lap_distances]
    maximums = [max(minimums[index],
                    int(math.floor(pace_max * distance_m / 1000.0 * 60.0)))
                for index, distance_m in enumerate(lap_distances)]
    if sum(minimums) > total_time_sec or sum(maximums) < total_time_sec:
        raise ValueError("目标用时无法同时满足各段配速规则")
    durations = [min(max(int(round(value)), minimums[index]), maximums[index])
                 for index, value in enumerate(raw)]
    difference = int(total_time_sec) - sum(durations)
    order = list(range(count))
    guard = 0
    while difference != 0 and guard < 10000:
        guard += 1
        rng.shuffle(order)
        moved = False
        for index in order:
            if difference > 0 and durations[index] < maximums[index]:
                step = min(difference, maximums[index] - durations[index])
                durations[index] += step
                difference -= step
                moved = True
            elif difference < 0 and durations[index] > minimums[index]:
                step = min(-difference, durations[index] - minimums[index])
                durations[index] -= step
                difference += step
                moved = True
            if difference == 0:
                break
        if not moved:
            break
    if difference != 0:
        raise ValueError("目标用时无法同时满足各段配速规则")
    return durations


def build_laps(points: Sequence, total_distance_m: float, total_time_sec: int,
               total_steps: int, start_time_ms: int,
               altitudes: Optional[Sequence[float]] = None,
               lap_seed: Optional[int] = None,
               pace_bounds: Optional[tuple] = None) -> List[dict]:
    """按真实轨迹切每 1 km + 尾段，生成官方 LapsBean 字段。

    ``points`` 需要 lat/lon/time；海拔默认取 ``point.altitude``，也可由
    ``altitudes`` 覆盖（用于 loc_json 重放）。步数按各段时长比例分配，
    总和精确等于 ``total_steps``。
    """
    count = len(points)
    if count < 2:
        return []
    if total_time_sec <= 0:
        raise ValueError("total_time_sec must be positive")
    if total_steps < 0:
        raise ValueError("total_steps must be nonnegative")

    times = [int(point.time) for point in points]
    if altitudes is None:
        alts = [float(getattr(point, "altitude", 0.0) or 0.0) for point in points]
    else:
        alts = [float(value or 0.0) for value in altitudes]
        if len(alts) != count:
            raise ValueError("altitudes length must match points")

    cumulative = [0.0]
    gains = [0.0]
    for index in range(1, count):
        previous, current = points[index - 1], points[index]
        cumulative.append(cumulative[-1] + distance(
            (previous.lat, previous.lon), (current.lat, current.lon)))
        delta = alts[index] - alts[index - 1]
        gains.append(gains[-1] + (delta if delta > 0 else 0.0))

    distance_total = float(total_distance_m)
    full_laps = int(math.floor((distance_total + 1e-9) / LAP_DISTANCE_M))
    tail_distance = distance_total - full_laps * LAP_DISTANCE_M
    if tail_distance <= 1e-6:
        tail_distance = 0.0

    boundaries = [index * LAP_DISTANCE_M for index in range(1, full_laps + 1)]
    if tail_distance > 0:
        boundaries.append(distance_total)
    lap_distances = [LAP_DISTANCE_M] * full_laps
    if tail_distance > 0:
        lap_distances.append(tail_distance)
    if not lap_distances:
        return []

    samples = [_sample_at_distance(cumulative, times, alts, gains, boundary)
               for boundary in boundaries]

    # 每公里用时按随机权重生成，再用规则配速约束；总和精确等于总时长。
    # App 详情页的“每公里数据”读取的是 laps_json，因此这里不必与均匀
    # 采样的 GPS 时间戳完全一致。
    durations = _lap_durations(
        lap_distances, int(total_time_sec),
        0 if lap_seed is None else int(lap_seed), pace_bounds)
    cumulative_durations: List[int] = []
    running = 0
    for duration in durations:
        running += int(duration)
        cumulative_durations.append(min(int(total_time_sec), running))
    cumulative_durations[-1] = int(total_time_sec)
    for index in range(len(cumulative_durations) - 2, -1, -1):
        if cumulative_durations[index] > cumulative_durations[index + 1]:
            cumulative_durations[index] = cumulative_durations[index + 1]

    cumulative_step_counts = [
        int(total_steps) * duration // int(total_time_sec)
        for duration in cumulative_durations
    ]
    if cumulative_step_counts:
        cumulative_step_counts[-1] = int(total_steps)

    base_altitude = alts[0]
    laps: List[dict] = []
    previous_duration = 0
    previous_steps = 0
    previous_gain = 0.0
    for index, (_, altitude, gain) in enumerate(samples):
        is_full = index < full_laps
        lap_distance = lap_distances[index]
        duration = cumulative_durations[index] - previous_duration
        step = cumulative_step_counts[index] - previous_steps

        if duration > 0:
            avg_cadence = step * 60.0 / duration
        else:
            avg_cadence = 0.0
        if is_full:
            avg_pace = duration / 60.0 if duration > 0 else 0.0
            avg_stride = 100000.0 / step if step > 0 else 0.0
        else:
            avg_pace = (duration * 1000.0 / (lap_distance * 60.0)
                        if duration > 0 and lap_distance > 0 else 0.0)
            avg_stride = (lap_distance * 100.0 / step if step > 0 else 0.0)

        laps.append({
            "lapIndex": index + 1,
            "distance": round(lap_distance, 3),
            "duration": int(duration),
            "cumulativeDuration": int(cumulative_durations[index]),
            "step": int(step),
            "avgPace": round(avg_pace, 3),
            "avgCadence": round(avg_cadence, 3),
            "avgStride": round(avg_stride, 3),
            "endAltAbs": round(altitude, 3),
            "endAltRel": round(altitude - base_altitude, 3),
            "elevationGain": round(max(0.0, gain - previous_gain), 3),
            "isFullLap": bool(is_full),
            "flag": int(start_time_ms),
            "id": index + 1,
        })
        previous_duration = cumulative_durations[index]
        previous_steps = cumulative_step_counts[index]
        previous_gain = gain
    return laps
