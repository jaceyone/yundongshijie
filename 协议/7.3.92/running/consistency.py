"""Local geometry and totals checks; no network or protocol assumptions."""
import bisect
import math
import random

EARTH_RADIUS_M = 6371000.0

# 官方 MyLocation 的 totalTime / gainTime 只有秒级精度，采样点必须落在整秒上。
SAMPLE_INTERVAL_MS = 3000
# 顶点间隔小于这个值时跳过（只隔 1–3 m），避免 1 秒配速台阶。
MIN_SAMPLE_GAP_MS = 2500
GRID_CLEAR_MS = 2000
# 手动路线会先按原路折返展开到目标距离；采样补偿多走的几米应沿原路反向，
# 而不是从终点直线跳回起点。只对「长度≈目标且首尾不重合」的折线生效。
FOLDBACK_TOLERANCE_M = 0.5
FOLDBACK_MARGIN_M = 60.0


def coordinate(lat, lon):
    if isinstance(lat, bool) or isinstance(lon, bool):
        raise ValueError("coordinates must be finite numbers")
    lat, lon = float(lat), float(lon)
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordinates are out of range")
    return lat, lon


def distance(a, b):
    lat1, lon1 = map(math.radians, coordinate(*a))
    lat2, lon2 = map(math.radians, coordinate(*b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return EARTH_RADIUS_M * 2 * math.asin(min(1.0, math.sqrt(max(0.0, h))))


def interpolate(a, b, fraction):
    if fraction <= 0:
        return a
    if fraction >= 1:
        return b
    angle = distance(a, b) / EARTH_RADIUS_M
    if angle < 1e-12:
        return a
    if abs(math.sin(angle)) < 1e-12:
        raise ValueError("antipodal route points cannot be interpolated")
    xyz = []
    for lat, lon in (a, b):
        lat, lon = math.radians(lat), math.radians(lon)
        xyz.append((math.cos(lat)*math.cos(lon), math.cos(lat)*math.sin(lon), math.sin(lat)))
    wa, wb = math.sin((1-fraction)*angle)/math.sin(angle), math.sin(fraction*angle)/math.sin(angle)
    x, y, z = (wa*xyz[0][i] + wb*xyz[1][i] for i in range(3))
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def validate_totals(seconds, steps, start, stop, meters):
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise ValueError("duration must be a positive integer")
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 0:
        raise ValueError("steps must be a nonnegative integer")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, stop)) or stop-start != seconds*1000:
        raise ValueError("timestamps do not match totalTime")
    if isinstance(meters, bool) or not isinstance(meters, (int, float)) or not math.isfinite(meters) or meters <= 0:
        raise ValueError("distance must be a finite positive number")


def _pace_profile_weights(seconds, seed):
    """1 秒分辨率的相对速度权重：低频平滑起伏 + 起止热身/降速。

    幅值控制在 ±5–8%，让配速图有轻微起伏，但不会像逐桶独立随机那样出现台阶。
    """
    count = max(1, int(seconds))
    rng = random.Random(int(seed))
    components = [
        (rng.uniform(0.02, 0.045), rng.uniform(70.0, 260.0),
         rng.uniform(0.0, 2.0 * math.pi))
        for _ in range(3)
    ]
    warmup = min(30.0, count * 0.1)
    cooldown = min(30.0, count * 0.1)
    weights = []
    for index in range(count):
        x = float(index)
        factor = 1.0
        for amplitude, period, phase in components:
            factor += amplitude * math.sin(2.0 * math.pi * x / period + phase)
        midpoint = x + 0.5
        if warmup > 0 and midpoint < warmup:
            factor *= 0.95 + 0.05 * (midpoint / warmup)
        remaining = count - midpoint
        if cooldown > 0 and remaining < cooldown:
            factor *= 0.95 + 0.05 * max(0.0, remaining) / cooldown
        weights.append(max(0.7, factor))
    return weights


def _profile_distance_curve(seconds, meters, seed):
    """第 k 秒对应的累计距离（长度 seconds+1），末项精确等于 meters。"""
    weights = _pace_profile_weights(seconds, seed)
    cumulative = [0.0]
    for weight in weights:
        cumulative.append(cumulative[-1] + weight)
    total = cumulative[-1]
    if total <= 0:
        return [float(meters) * index / float(max(1, int(seconds)))
                for index in range(int(seconds) + 1)]
    return [float(meters) * value / total for value in cumulative]


def _second_for_distance(curve, target):
    """在 1 秒距离曲线上反查目标距离对应的秒数（浮点）。"""
    index = bisect.bisect_left(curve, target)
    index = min(max(index, 1), len(curve) - 1)
    low, high = curve[index - 1], curve[index]
    if high <= low:
        return float(index)
    return (index - 1) + (target - low) / (high - low)


def _sample_track(route, traverse_m, nominal_m, seconds, seed, min_spacing_m,
                  required=()):
    """在 route 上按整秒时间轴走 traverse_m 米；返回 (lat, lon, elapsed_ms)。

    时间轴只由 ``nominal_m`` 决定，``traverse_m`` 只影响路线终点。这样补偿
    总距离时，采样时刻不会随迭代跳动，补偿才能收敛。``required`` 里的坐标
    （打卡点）一定会被采样，不受顶点间距过滤影响。
    """
    # Keep corners as samples so interpolation cannot cut across the route.
    vertices = [route[0]]
    cumulative = [0.0]
    edge = 0
    while cumulative[-1] < traverse_m - 1e-8:
        a, b = route[edge % len(route)], route[(edge+1) % len(route)]
        length = distance(a, b)
        remaining = traverse_m-cumulative[-1]
        if length >= remaining:
            vertices.append(interpolate(a, b, remaining/length))
            cumulative.append(float(traverse_m))
        else:
            vertices.append(b)
            cumulative.append(cumulative[-1]+length)
        edge += 1
        if edge > 100000:
            raise ValueError("route has too many segments")
    duration_ms = seconds*1000
    if duration_ms < MIN_SAMPLE_GAP_MS:
        raise ValueError("duration is too short for second-resolution sampling")

    # 1) 平滑速度剖面 → 时间到距离的曲线；顶点按“理想时刻”吸附到整秒。
    #    顶点筛选只看路线距离间隔：这样补偿总距离时锚点集合不会来回翻转。
    curve = _profile_distance_curve(seconds, nominal_m, seed)
    required = [coordinate(*point) for point in required]
    ideal_anchors = [(0.0, 0.0)]
    required_distances = set()
    last_distance = 0.0
    for traveled, vertex in zip(cumulative[1:-1], vertices[1:-1]):
        is_required = any(distance(point, vertex) <= 0.1
                          for point in required)
        if not is_required:
            if traveled - last_distance < min_spacing_m:
                # 顶点太密（只隔 1–3 m），跳过不影响几何；避免 1 秒配速台阶。
                continue
            if traveled > nominal_m - min_spacing_m:
                continue
        ideal_ms = _second_for_distance(curve, traveled) * 1000.0
        if is_required and len(ideal_anchors) > 1 and ideal_anchors[-1][1] not in required_distances \
                and ideal_ms - ideal_anchors[-1][0] < 1000.0:
            # 打卡点必须保留；离上一个锚点太近时合并掉上一个，避免 0/1 秒区间。
            ideal_anchors.pop()
        ideal_anchors.append((ideal_ms, float(traveled)))
        last_distance = traveled
        if is_required:
            required_distances.add(float(traveled))
    while len(ideal_anchors) > 1 and traverse_m - ideal_anchors[-1][1] < min_spacing_m:
        if ideal_anchors[-1][1] in required_distances:
            break
        ideal_anchors.pop()
    # 补偿距离 > 目标距离时（`traverse` 被迭代放大），顶点按曲线换算的时间会
    # 超过总时长，直接落到时间轴上会让末点时间越过 duration，触发
    # 「track endpoints do not match run timestamps」。这里先裁掉越过总时长的
    # 顶点锚点（打卡点锚点仍在 0.1 m 校验范围内），再补终点锚点。
    ideal_anchors = [entry for entry in ideal_anchors
                     if entry[0] < duration_ms - 0.5] or ideal_anchors[:1]
    ideal_anchors.append((float(duration_ms), float(traverse_m)))

    # 2) 把每个区间按整秒分配，并用最大余数法保证总和精确等于 duration：
    #    每个区间最多偏 1 秒，误差不会在整条时间轴上累积到最后一段。
    spans = [later[0] - earlier[0]
             for earlier, later in zip(ideal_anchors, ideal_anchors[1:])]
    floors = [int(span // 1000) * 1000 for span in spans]
    remainders = [span - floor for span, floor in zip(spans, floors)]
    missing = (duration_ms - sum(floors)) // 1000
    order = sorted(range(len(spans)), key=lambda index: remainders[index],
                   reverse=True)
    rounded = set(order[:max(0, missing)])
    anchors = [(0, 0.0)]
    elapsed = 0
    for index, (_, traveled) in enumerate(ideal_anchors[1:]):
        elapsed += floors[index] + (1000 if index in rounded else 0)
        anchors.append((elapsed, traveled))
    anchors[-1] = (duration_ms, float(traverse_m))
    anchor_times = [ms for ms, _ in anchors]
    anchor_distances = [value for _, value in anchors]

    # 2) 采样时间 = 整秒 3 秒网格 + 顶点锚点；剔除贴着锚点的网格点。
    grid = list(range(0, duration_ms, SAMPLE_INTERVAL_MS))
    kept = [ms for ms in grid
            if all(abs(ms - anchor) >= GRID_CLEAR_MS for anchor in anchor_times)]
    times = sorted(set(kept) | set(anchor_times) | {0, duration_ms})

    def distance_at(elapsed_ms):
        index = bisect.bisect_right(anchor_times, elapsed_ms) - 1
        index = min(max(index, 0), len(anchors) - 2)
        begin, end = anchor_times[index], anchor_times[index + 1]
        if end <= begin:
            return anchor_distances[index]
        fraction = (elapsed_ms - begin) / float(end - begin)
        return (anchor_distances[index]
                + (anchor_distances[index + 1] - anchor_distances[index]) * fraction)

    # 3) 按距离在折线上取点；顶点锚点会精确落在原折线顶点上。
    result = []
    last_index = len(vertices) - 2
    for elapsed in times:
        traveled = distance_at(elapsed)
        index = min(last_index, max(0, bisect.bisect_right(cumulative, traveled) - 1))
        low, high = cumulative[index], cumulative[index + 1]
        fraction = 0.0 if high <= low else (traveled - low) / (high - low)
        lat, lon = interpolate(vertices[index], vertices[index + 1],
                               min(1.0, max(0.0, fraction)))
        result.append((lat, lon, elapsed))
    return result


def _chord_length(samples):
    return sum(distance(a[:2], b[:2]) for a, b in zip(samples, samples[1:]))


def _foldback_point(route, extra_m):
    """沿已画的折线继续 ``extra_m``，返回终点之外仍在折线上的点。

    手动路线的终点通常落在原折线的某一段中间；展开后的折线里仍保留着
    那一段的完整版本（第一趟正向走过）。先在折线里找一条穿过终点、且
    在行进方向还有余量的线段，沿它延伸；找不到时再沿原路反向走。
    """
    end = route[-1]
    previous = route[-2]
    cos_lat = math.cos(math.radians(end[0]))
    vx = (end[1] - previous[1]) * cos_lat
    vy = end[0] - previous[0]
    for index in range(len(route) - 2):
        a, b = route[index], route[index + 1]
        ax = (a[1] - end[1]) * cos_lat
        ay = a[0] - end[0]
        bx = (b[1] - end[1]) * cos_lat
        by = b[0] - end[0]
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-18:
            continue
        t = -(ax * dx + ay * dy) / length_sq
        if not (1e-9 < t < 1.0 - 1e-9):
            continue
        px, py = ax + t * dx, ay + t * dy
        offset = math.hypot(px, py) * math.pi / 180.0 * EARTH_RADIUS_M
        if offset > 0.05:
            continue
        for candidate in (a, b):
            cx = (candidate[1] - end[1]) * cos_lat
            cy = candidate[0] - end[0]
            if cx * vx + cy * vy <= 0:
                continue
            segment = distance(end, candidate)
            if segment >= extra_m:
                return interpolate(end, candidate, extra_m / segment)
    remaining = float(extra_m)
    later = route[-1]
    for index in range(len(route) - 2, -1, -1):
        earlier = route[index]
        segment = distance(earlier, later)
        if segment >= remaining:
            if segment <= 1e-12:
                return earlier
            return interpolate(earlier, later, 1.0 - remaining / segment)
        remaining -= segment
        later = earlier
    earlier = route[0]
    for index in range(1, len(route)):
        later = route[index]
        segment = distance(earlier, later)
        if segment >= remaining:
            if segment <= 1e-12:
                return later
            return interpolate(earlier, later, remaining / segment)
        remaining -= segment
        earlier = later
    return route[-1]


def _is_foldback_route(route, target):
    """折线长度≈目标且首尾不重合：手动路线已按原路折返展开到目标。"""
    length = sum(distance(a, b) for a, b in zip(route, route[1:]))
    if abs(length - target) > FOLDBACK_TOLERANCE_M:
        return False
    return distance(route[0], route[-1]) > 0.5


def _extend_for_foldback(route, target):
    """折返路线补一个沿原路反向的采样点，供补偿循环多走几米。

    手动路线交给协议前已经展开到目标距离。整秒采样会切掉拐角，补偿循环
    需要多走 1~2 m；对开放折线，原来的 ``edge % len(route)`` 会从终点直线
    跳回起点，产生手画折线之外的坐标且难以收敛。这里只延长**采样路径**，
    追加点仍在原折线上，不改变上传几何。
    """
    if not _is_foldback_route(route, target):
        return route
    point = _foldback_point(route, FOLDBACK_MARGIN_M + 1.0)
    if distance(route[-1], point) <= 1e-9:
        return route
    return list(route) + [point]


def _solve_foldback_traverse(route, target, seconds, seed, min_spacing, required):
    """在折返路线上找弦长≈目标的补偿距离。

    补偿距离超过目标后，轨迹沿原路反向继续；弦长随补偿距离非单调变化，
    固定点迭代会来回震荡。这里从目标距离向外扫描，遇到变号再局部二分。
    """
    def chord_at(traverse):
        samples = _sample_track(route, traverse, target, seconds, seed,
                                min_spacing, required)
        return _chord_length(samples), samples

    chord, samples = chord_at(float(target))
    if abs(target - chord) <= 0.02:
        return samples
    # 从目标距离向外扫描，取第一个弦长命中的补偿距离：补偿最小，末段
    # 配速也最接近正常值。弦长随补偿距离非单调，不能直接对整个区间二分。
    start = float(target)
    step = 0.05
    previous_gap = target - chord
    steps = int(FOLDBACK_MARGIN_M / step)
    for index in range(1, steps + 1):
        traverse = start + index * step
        value, candidate = chord_at(traverse)
        gap = target - value
        if abs(gap) <= 0.02:
            return candidate
        if previous_gap * gap < 0:
            low = start + (index - 1) * step
            high = traverse
            low_gap = previous_gap
            mid_samples = candidate
            for _ in range(30):
                mid = (low + high) / 2.0
                mid_value, mid_samples = chord_at(mid)
                mid_gap = target - mid_value
                if abs(mid_gap) <= 0.02:
                    break
                if mid_gap * low_gap > 0:
                    low, low_gap = mid, mid_gap
                else:
                    high = mid
            return mid_samples
        previous_gap, samples = gap, candidate
    return samples


def sample_route(waypoints, meters, seconds, start, require_all=True, seed=0,
                 required_points=()):
    """把路线采样成整秒时间戳的轨迹点。

    ``MyLocation.totalTime`` / ``gainTime`` 只有秒级精度，所以所有采样点的
    时间戳都取整秒：3 秒网格 + 折线顶点吸附到最近整秒，并剔除离顶点不足
    ``GRID_CLEAR_MS`` 的网格点，避免 0–1 秒的假配速尖刺。

    ``seed`` 驱动一条 ±5–8% 的平滑速度剖面；总距离、总时长、终点和折线
    顶点（打卡点）位置仍然精确。被跳过的密集小顶点会让弦长略短于路线，
    这里用迭代把实际走过路线补偿到弦长精确等于 ``meters``。
    """
    validate_totals(seconds, 0, start, start + seconds*1000, meters)
    route = []
    for point in waypoints:
        point = coordinate(*point)
        if not route or distance(route[-1], point) > 1e-6:
            route.append(point)
    if len(route) > 1 and distance(route[0], route[-1]) <= 1e-6:
        route.pop()
    if len(route) < 2:
        raise ValueError("at least two distinct route points are required")
    minimum = sum(distance(a, b) for a, b in zip(route, route[1:]))
    if require_all and meters + .001 < minimum:
        raise ValueError("target distance is too short to visit the supplied points")

    target = float(meters)
    # 手动路线（展开后长度=目标）的补偿段沿原路反向，不直线跳回起点。
    foldback = _is_foldback_route(route, target)
    if foldback:
        route = _extend_for_foldback(route, target)
    # 2.5 秒对应的路线长度；用“目标平均速度”而不是每次迭代的速度，
    # 保证补偿总距离时锚点集合稳定。
    min_spacing = MIN_SAMPLE_GAP_MS / 1000.0 * target / float(seconds)
    if foldback:
        samples = _solve_foldback_traverse(
            route, target, seconds, seed, min_spacing, required_points)
    else:
        traverse = target
        samples = _sample_track(route, traverse, target, seconds, seed,
                                min_spacing, required_points)
        chord = _chord_length(samples)
        if chord > 1e-6 and abs(target - chord) > 0.02:
            # 第一步：闭环路线的弦长与补偿距离近似成正比，用比例估一个落脚点。
            traverse = traverse * target / chord
            samples = _sample_track(route, traverse, target, seconds, seed,
                                    min_spacing, required_points)
            chord = _chord_length(samples)
        if abs(target - chord) > 0.02 and chord > 1e-6:
            # 第二步：3 秒整秒网格会让弦长按「跨过一个拐角」跳变，比例步可能正好
            # 落在台阶上。先粗扫 ±20 m（0.5 m 步），再在最优附近细扫 0.05 m。
            best_traverse, best_samples, best_gap = traverse, samples, abs(target - chord)

            def probe(value):
                candidate = _sample_track(route, value, target, seconds, seed,
                                          min_spacing, required_points)
                return abs(target - _chord_length(candidate)), candidate

            offsets = list(range(-40, 0)) + list(range(1, 41))
            for index in offsets:
                value = traverse + index * 0.5
                if value <= 0:
                    continue
                gap, candidate = probe(value)
                if gap < best_gap:
                    best_traverse, best_samples, best_gap = value, candidate, gap
                    if gap <= 0.02:
                        break
            if best_gap > 0.02:
                for index in list(range(-10, 0)) + list(range(1, 11)):
                    value = best_traverse + index * 0.05
                    if value <= 0:
                        continue
                    gap, candidate = probe(value)
                    if gap < best_gap:
                        best_traverse, best_samples, best_gap = value, candidate, gap
                        if gap <= 0.02:
                            break
            samples = best_samples
    return [(lat, lon, start + elapsed) for lat, lon, elapsed in samples]


def check_track(points, meters, seconds, start, stop, fixed_points=()):
    validate_totals(seconds, 0, start, stop, meters)
    if len(points) < 2 or points[0].time != start or points[-1].time != stop:
        raise ValueError("track endpoints do not match run timestamps")
    for point in points:
        coordinate(point.lat, point.lon)
        if not isinstance(point.time, int) or not math.isfinite(point.speed) or point.speed < 0:
            raise ValueError("invalid track timestamp or speed")
    if any(a.time >= b.time for a, b in zip(points, points[1:])):
        raise ValueError("track timestamps must strictly increase")
    actual = sum(distance((a.lat, a.lon), (b.lat, b.lon)) for a, b in zip(points, points[1:]))
    if abs(actual-meters) > max(.1, meters*.0001):
        raise ValueError(f"track length {actual:.3f}m differs from totalDis {meters}m")
    for fixed in fixed_points:
        # 官方地图/路线使用 GCJ-02；FivePoint.lat/lon 是原始 BD-09。
        route_fixed = fixed.route_coordinates() if hasattr(fixed, "route_coordinates") else (fixed.lat, fixed.lon)
        if not any(distance(route_fixed, (point.lat, point.lon)) <= .1 for point in points):
            raise ValueError("a checkpoint is missing from the track")
    return actual


def time_buckets(seconds, steps, start, stop, meters):
    validate_totals(seconds, steps, start, stop, meters)
    duration_ms = seconds*1000
    cents = round(meters*100)
    for begin in range(0, duration_ms, 10000):
        end = min(begin+10000, duration_ms)
        count = steps*end//duration_ms - steps*begin//duration_ms
        length = (cents*end//duration_ms - cents*begin//duration_ms)/100
        yield start+begin, start+end, count, length
