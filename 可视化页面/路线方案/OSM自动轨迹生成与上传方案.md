# OSM 自动轨迹生成与上传方案

> **状态**：M1–M4 已实现（2026-11-11）；真实账号 5 点 dry-run 已通过；2026-11-11 完成路线排序/坐标对齐官方（`route_order` + GCJ-02）。2026-11-13 起计分跑固定使用 OSM 避建筑路线，`synthetic` 不再作为可选模式暴露（仅保留给自由跑与协议内部测试）。
> **实现进度**：OSM 预处理、代价栅格、A* 规划器已落地（`running/osm/`、`tools/build_osm_campus.py`、`running/data/`）；官方 schema、outbox、OBS 回读与 CLI 已落地；`MyLocation` 字段已用真实 fixture `rrid=1333592274`（154 点）校准。新增 `running/route_order.py` 移植官方 5 点排序；计分跑先排序再走 OSM A*。CLI 与一键脚本不再暴露 `--path-source`，`auto` 解析为「计分跑 OSM / 自由跑内部合成」。
> **真实数据校正（2026-11-11）**：官方围栏（12 点）与 OSM 校园基本重合；真正异常的是官方 5 点，4 点在围栏外 61–236 m、1 点在围栏内但落在 OSM 建筑内。`running/data/` 已按「官方围栏 + 350 m 外扩」重建（833×641，36 栋建筑、44 条道路）；旧「真实 5 点闭环 943.0 m / 4 圈」按 `lat/lon` 计算，按官方 `glat/glon` 排序的离线 dry-run 为 1352.7 m / 3 圈（见 §2.5）。
> **定位**：协议侧 / CLI 侧的轨迹生成与上传方案；UI 后续接入。页面逻辑仍以 `UI设计说明.md` 为准。
> **依据**：2026-11-10 用户确认的选型；OSM 许昌学院核查（`_analysis/osm_xuchang/REPORT.md`）；官方 7.3.92 反编译实现；现有 `协议/7.3.92/` 代码。
> **替代关系**：本文替代 `可视化页面/路线库方案.md`；旧方案已标记废弃，禁止按旧方案实现。

---

## 0. 结论摘要

- **目标**：输入官方 5 点、围栏、规则和目标距离/时长，自动生成一条**在围栏内、避开 OSM 建筑、沿校园主路优先**的轨迹，并按官方 schema 上传。
- **路径来源**：OSM 校园数据（`way/280173698`）预处理后打包进 APK；运行时离线。计分跑固定 OSM；合成路线只保留给自由跑与协议内部测试。
- **规划方式**：自由空间栅格 A* + 主路网偏好（路网是软约束，不是硬约束）。
- **障碍**：首版只使用 OSM `building=*` 轮廓；不做人工障碍层。
- **上传**：修官方 `LocJsonEntity` + `MyLocation` schema；OBS + `save/record`。
- **失败策略**：无可行路径直接报错，**不回调合成路线、不上传**；上传前对最终采样轨迹再做一次建筑/边界校验。
- **范围**：先跑通协议 / CLI；UI 后续接入。

---

## 1. 背景与目标

### 1.1 背景

现有 `builder.py` 把 5 个官方点用直线连接成多边形，再按 3 秒采样。它的问题是：

- 轨迹是直线多边形，可能穿过建筑、草坪；
- 没有利用校园道路；
- 官方 OBS `run_data` 和 `save_record.allLocJson` 仍是裸坐标数组，官方 App 解析不出轨迹。

OSM 许昌学院数据核查结果：

| 项 | 值 |
| --- | --- |
| OSM 对象 | `way/280173698` |
| 名称 | 许昌学院 / Xuchang University |
| 中心 | `34.0447395, 113.8630331` |
| bbox | `34.0416695–34.0487814 / 113.8574828–113.8691128` |
| 面积 | 73.9 ha |
| 建筑 | 36 栋，全部闭合，占地 9.62% |
| 道路 | 11 条 / 70 段 / 4.12 km |
| 显式人行道 | 0 |
| 主连通分量 | 36 段 / 3.93 km（95.2%） |
| 建筑到路网中位距离 | 57.2 m |
| 围墙 / 水域 / 施工区 / 校门 | 未映射 |
| A* 抽测 | 西→东 850.7 m 可通，道路与建筑 0 相交 |

结论：OSM 坐标与建筑轮廓可用；仅靠道路图不够，因此采用自由空间栅格 + 路网偏好。

### 1.2 目标

1. **全自动**：不需要人工画线，每轮根据 5 点和目标距离/时长自动生成。
2. **避建筑**：路径不得穿越 OSM 建筑轮廓，并保留缓冲。
3. **路网偏好**：优先沿校园主路，必要时允许短距离离开道路。
4. **合规**：在围栏内、经过 5 点、距离/时长匹配、配速/步频在规则内。
5. **官方可解析**：使用 `LocJsonEntity` + `MyLocation` schema，官方 App 能画出轨迹。
6. **可离线运行**：不调用地图 API key；不依赖 Nominatim/Overpass 运行时。
7. **可验证**：dry-run 可导出轨迹；上传后可回读 OBS 对象。

### 1.3 非目标

- 不做人工画线、不做人工障碍层（首版）；
- 不做 UI 编辑器；
- 不接入高德/Mapbox/Google 等地图 API；
- 不做实时 GPS 采集；
- 不保证绕开未映射的围墙、水域、施工区、校门；
- 不承诺路径与真实学生步行完全一致，只保证“粗粒度避建筑 + 路网偏好”。

### 1.4 已确认决策

| 决策 | 选择 |
| --- | --- |
| 目标校区 | `way/280173698`（许昌学院，八一路东段） |
| OSM 数据 | 预处理后打包进 APK |
| 规划方式 | 自由空间栅格 A* + 主路网偏好 |
| 人工障碍 | 首版只用 OSM 建筑，不做人工标记 |
| 官方 schema | 纳入本方案，必须修 |
| 首版范围 | 协议 / CLI 先跑通 |
| 无解处理 | 直接报错 |

---

## 2. 事实与约束

### 2.1 官方点位机制

- 每次跑步由 `/api/v560/get/1/distance/{distance}` 下发 `pointsResModels`（5 点）；
- 客户端生成新的 `uuid`，同一个 `uuid` 用于成绩上报；
- 5 点可能变化，不能硬编码；
- 顺序跑：顺序由官方 `position` 固定；
- 随机跑：可对 5 点做全排列（120 种）；
- 终点跑：只使用终点（首版不处理）。

### 2.2 官方轨迹 schema

官方详情页解析逻辑（反编译）：

```text
get_one_record
  → RunHistoryDetailBean.getAllLocJson()
  → Gson.fromJson(..., LocJsonEntity.class)
  → LocJsonEntity.allLocJson
  → Gson.fromJson(..., List<MyLocation>)
```

OBS 解析逻辑：

```text
OutdoorDecoder.run_data
  → gunzip + base64 decode
  → Gson.fromJson(..., LocJsonEntity.class)
  → LocJsonEntity.allLocJson
  → Gson.fromJson(..., List<MyLocation>)
```

因此：

- `save_record.allLocJson` 必须是 `LocJsonEntity` JSON 字符串；
- OBS `run_data` 必须是同一个 `LocJsonEntity` JSON 的 gzip + base64；
- `MyLocation` 字段是 `lat` / `lng` / `gLat` / `gLng` / `gainTime` 等，不是 `lon` / `time` / `accuracy`。

### 2.3 坐标系统

- OSM：WGS84（EPSG:4326）；
- 官方 5 点同时返回两套坐标：`lat/lon` 是 **BD-09**，`glat/glon` 是 **GCJ-02**；App 的地图、5 点标记、路线排序和轨迹都用 `glat/glon`，`lat/lon` 只在 g 对缺失时作为 Baidu 回退。
- **官方围栏 `GeoPoint.lat/lng` 可能为 0，真实 GCJ 坐标在 `raw.glat/glng`；必须调用 `GeoPoint.gcj_coordinates()`，不能直接用 `.lat/.lng`。**
- 预处理时统一转 GCJ-02；脚本规划/轨迹/排序统一用 `glat/glon`；`fivePointJson` 仍保留官方原始 `lat/lon` 与 `glat/glon`。
- 运行时只处理 GCJ-02，避免混用。

### 2.4 现有代码接口

| 位置 | 现状 | 本方案改动 |
| --- | --- | --- |
| `running/builder.py` | 5 点直线多边形；裸数组 payload | 支持外部 polyline；官方 schema |
| `running/consistency.py` | `sample_route` / `check_track` | 复用；增加容差参数 |
| `running/client.py` | OBS + save/record | 接入 OSM 路径源、outbox、回读 |
| `sport_client.py` | `execute_run` | 增加 `path_source` 等参数 |
| `cli.py` / `running/cli.py` | 基础跑步参数 | 增加 `--path-source`、`--campus`、`--dry-run` 等 |

### 2.5 官方 5 点排序（2026-11-11 对齐）

- 官方 `RunSequenceStartViewModel.calculateRoute()` → `calculateRouteWrapper()`：
  - 枚举 5 点全排列，按 `glat/glon` 直线距离升序排序；
  - `sequenceStrength == 1` 时窗口 `[0.6*dayGoal, 1.0*dayGoal]`，否则 `[0.8*minDistance, min(1.2*minDistance, dayGoal)]`；
  - 优先窗口内随机，否则取低于窗口的最大值，再否则取高于窗口的最小值；`position` 写成 0 基下标。
- 脚本 `running/route_order.py` 移植上述逻辑；OSM A* 只负责排序后两点之间的避建筑几何。官方没有本地折线生成函数，官方轨迹来自真实 GPS 记录。
- **旧数据说明**：此前「真实 5 点闭环 943.0 m / 4 圈」是按 `lat/lon`（BD-09）算的；按官方排序 + `glat/glon` 的离线 dry-run 为 1352.7 m / 3 圈，待真实提交在 App 上确认。

---

## 3. 总体架构

```text
[PC 预处理，一次性]
OSM 原始数据（Overpass / PBF）
  → 校园边界、建筑、道路
  → WGS84 → GCJ-02
  → 简化 + 栅格化 + 代价图
  → campus_xuchang_280173698.json / grid.json
  → 打包进 APK

[运行时]
5 点 + 围栏 + 规则 + 目标距离/时长
  → OsmCampus.load()
  → TrackPlanner:
       点位吸附 → 5 点排序 → 分段 A* → 闭环
       → 距离补足 → 平滑 → 校验
  → TrackSampler:
       3 秒采样 + 速度曲线
  → OfficialTrackSerializer:
       LocJsonEntity + MyLocation
  → OBS 上传
  → save/record
  → 回读验证 / outbox 重试
```

### 3.1 模块划分

```text
协议/7.3.92/running/
├─ osm/
│  ├─ __init__.py
│  ├─ campus.py        # 校园数据加载、坐标转换、几何工具
│  ├─ grid.py          # 栅格 / 代价图加载与查询
│  ├─ planner.py       # 吸附、A*、排序、补距、平滑、校验
│  └─ schema.py        # LocJsonEntity / MyLocation 序列化
├─ data/
│  ├─ campus_xuchang_280173698.json
│  └─ campus_xuchang_280173698_grid.json
├─ outbox.py           # 提交持久化 / 重试 / 回读
├─ builder.py          # 改造：支持外部 polyline + 官方 schema
├─ client.py           # 接入 OSM 路径源
└─ cli.py              # CLI 参数
tools/
└─ build_osm_campus.py # PC 预处理脚本（不随 APK 运行）
```

---

## 4. OSM 数据预处理

### 4.1 数据源

- 首选：Overpass API 按 `way/280173698` 及 bbox 查询；
- 备选：Geofabrik / BBBike 下载 PBF；
- 数据时间、查询语句、许可证记录在 JSON 的 `meta` 中。

### 4.2 输出格式

`campus_xuchang_280173698.json`：

```json
{
  "campus_id": "way/280173698",
  "name": "许昌学院",
  "name_en": "Xuchang University",
  "unid": 17601,
  "center": {"lat": 34.0447395, "lon": 113.8630331},
  "bbox": {
    "minlat": 34.0416695, "minlon": 113.8574828,
    "maxlat": 34.0487814, "maxlon": 113.8691128
  },
  "boundary": [[lat, lon], ...],
  "buildings": [
    {
      "osm_id": 123,
      "name": "",
      "polygon": [[lat, lon], ...]
    }
  ],
  "roads": [
    {
      "osm_id": 456,
      "highway": "residential",
      "name": "文津路",
      "polyline": [[lat, lon], ...]
    }
  ],
  "obstacles": [],
  "meta": {
    "source": "OpenStreetMap",
    "license": "ODbL",
    "timestamp": "2026-11-10T11:09:40Z",
    "coordinate_system": "GCJ-02"
  }
}
```

- 内部统一使用 `[lat, lon]`；
- OSM 原始是 `[lon, lat]`，预处理时转换；
- 建筑多边形保留闭合环；
- 道路保留折线；
- `obstacles` 首版为空；未来放围墙、水域、施工区；
- **2026-11-11 更正**：`campus_id` 仍用 `xuchang_280173698` 作为数据来源标识，但 `boundary` 优先取官方 `getGeoFenceForRun` 围栏（GCJ-02，12 点）；OSM 校园 way 只提供建筑/道路。

### 4.3 坐标转换

- 实现 WGS84 → GCJ-02 标准算法（纯 Python，无外部依赖）；
- 对边界、建筑、道路、5 点统一转换；
- 单元测试：已知点转换误差在合理范围；
- 不把 WGS84 和 GCJ-02 混在同一个轨迹里。

### 4.4 简化与栅格化

1. **简化**：Douglas-Peucker，容差 0.5–1.0 m，减少顶点；
2. **栅格**：分辨率 2 m；
3. **边界**：校园外 = 不可走；
4. **建筑**：建筑多边形覆盖的格子 = 不可走，向外扩 1.5 m 缓冲；
5. **道路**：道路折线覆盖的格子 = 低代价；
6. **路网偏好**：离道路越远，代价越高；
7. **建筑缓冲**：离建筑越近，代价越高（但不阻断）。
8. **输出**：预生成代价栅格，避免 Android 上重复做点-多边形判断。

`campus_xuchang_280173698_grid.json`：

```json
{
  "resolution_m": 2.0,
  "origin": {"lat": 34.0416695, "lon": 113.8574828},
  "width": 620,
  "height": 400,
  "cost_runs": [
    {"value": 255, "count": 1200},
    {"value": 1, "count": 80}
  ],
  "legend": {
    "255": "blocked",
    "1": "road",
    "2": "near_road",
    "3": "open",
    "4": "near_building"
  }
}
```

- 扫描线顺序做 RLE；
- 载入时展开为 `bytearray`；
- 2 m 栅格下 73.9 ha 约 18.5 万格，内存约 185 KB；
- 预处理脚本生成，运行时只读。

### 4.5 数据校验

预处理后必须检查：

- 校园边界闭合；
- 建筑数、道路数、面积与核查报告一致；
- 所有建筑质心在校园内；
- 栅格尺寸与 bbox 匹配；
- 道路/建筑没有因坐标转换产生明显偏移；
- 5 个测试点能吸附到可走格。

---

## 5. 路径规划

### 5.1 输入输出

**输入**：

```python
PlanRequest(
    checkpoints: List[Point],   # 5 个 GCJ-02 点
    boundary: List[Point],
    grid: CostGrid,
    target_distance_m: int,
    target_time_s: int,
    policy: int,                # 随机 / 顺序 / 终点
    run_area_id: int,
)
```

**输出**：

```python
PlanResult(
    polyline: List[Point],      # GCJ-02，闭合
    lap_length_m: float,
    laps: int,
    checkpoints_used: List[Point],
    diagnostics: dict,
)
```

### 5.2 点位吸附

- 5 个点分别吸附到最近的可走格中心；
- 吸附半径默认 30 m；
- 如果点在建筑内：
  - 不移动原始点；在**该点所在建筑内**打通一条单格宽（2 m）4 连通接入通道到最近可走格；
  - 通道不能穿过其它建筑，也不能越出围栏；
  - 通道长度、建筑 id、目标格写入 `diagnostics.access_corridors`；
  - 若 80 m 内找不到不穿其它建筑的出口，报错 `osm_checkpoint_inside_building`；
- 吸附后记录原始点与吸附点距离，超过 5 m 时在诊断日志中标注；
- 最终轨迹必须包含**原始 5 点坐标**（不是吸附点），用短连接线接入；连接线要校验不穿建筑，**打卡点所在建筑的接入通道除外**。

### 5.3 5 点排序

- **顺序跑**：严格按官方 `position` 顺序；
- **随机跑**：5 点全排列（120 种）；
- 对每种顺序：
  - 依次做 A*；
  - 总长度 = 各段之和 + 回到起点的闭环；
  - 选择总长度 ≤ 目标距离且最接近目标距离的顺序；
- 如果所有顺序的最短闭环都 > 目标距离：
  - 报错 `target_distance_too_short`；
- 如果最短闭环远小于目标距离：
  - 允许 `sample_route` 重复闭环；
  - 闭环 < 400 m 时，优先加入一个绕行段，避免过多圈。

### 5.4 A* 代价函数

- 8 邻域；
- 禁止对角穿角：对角移动时，两个相邻正交格都必须可走；
- 单步代价：

```text
step = 网格距离 × cell_cost
cell_cost:
  255 → 不可走
  1   → 道路，最低
  2   → 近路（≤5m），略高
  3   → 空地，较高
  4   → 近建筑（≤3m），最高
```

- 启发式：欧氏距离 × 最低代价，保证可采纳；
- 使用 `heapq`，纯 Python 标准库；
- 记录访问节点数、路径长度，便于诊断。

### 5.5 闭环与距离补足

- 路径默认闭合：`p0 → p1 → ... → p4 → p0`；
- 规划器返回闭环折线；
- `sample_route` 按目标距离循环该闭环；
- 圈数 = `ceil(target_distance / loop_length)`；
- 如果圈数 > 8，说明闭环过短，加入绕行段或报错；
- 绕行段：
  - 在闭环上选择 2–4 个锚点；
  - 向垂直方向偏移，用 A* 连接；
  - 校验仍在围栏内、不穿建筑；
  - 二分调整偏移量，直到长度达标；
- 首版允许重复闭环；绕行段是增强项。

### 5.6 平滑与简化

1. A* 出来的是锯齿格路径；
2. 先做 string-pulling：用视线检测跳过中间点；
3. 再做 Catmull-Rom / Chaikin 平滑；
4. 平滑后逐段做建筑相交检测；
5. 如果平滑导致穿建筑，回退到未平滑版本；
6. 保留 5 个原始点作为必经顶点，不参与平滑偏移。

### 5.7 校验

每条候选路径必须通过：

| 校验 | 规则 |
| --- | --- |
| 围栏 | 所有点 + 所有线段在规划边界内（官方围栏 + 350 m 外扩；官方点本身可能落在官方围栏外） |
| 建筑 | 所有线段与建筑多边形 0 相交（含 1.5 m 缓冲）；打卡点接入通道所在的建筑除外，通道记录在 `diagnostics.access_corridors` |
| 5 点 | 原始 5 点都在路径上（默认 0.1 m，可配置） |
| 长度 | 闭环长度 ≤ 目标距离；采样后总距离 = 目标 |
| 时间 | 采样时间严格递增，总时长 = 目标 |
| 速度 | 平均配速在规则范围内 |
| 自交 | 允许闭环，但不允许异常自交；必要时检测 |

### 5.8 失败处理

- 无路径、点无法吸附、目标距离过短、点在建筑内且无法打通接入通道 → **直接报错**；
- 错误码建议：
  - `osm_campus_not_found`
  - `osm_no_free_cell`
  - `osm_checkpoint_inside_building`
  - `osm_no_route`
  - `osm_target_distance_too_short`
  - `osm_fence_violation`
  - `osm_building_collision`
- 错误信息带诊断：哪两个点之间失败、吸附距离、最近建筑、尝试的顺序数。

---

## 6. 轨迹采样与速度曲线

- 复用 `running/consistency.py` 的 `sample_route`：
  - 输入闭环折线、目标距离、目标时长；
  - 每 3 秒采样；
  - 保留拐点；
- `GpsPoint` 字段：`lat`、`lon`、`time`、`speed`、`accuracy`、`isValid`；
- 速度：
  - 默认按目标距离/时长匀速；
  - 可选：前 60 秒热身、后 60 秒降速、中间轻微波动；
  - 波动必须保持平均配速在学校规则内；
- 步频：
  - 继续使用 `total_steps = distance / 0.72 × random(0.96, 1.04)`；
  - `time_buckets` 生成 10 秒步频/速度分桶；
- 校验：
  - `check_track` 检查端点、时间递增、总距离、5 点；
  - 为 OSM 模式增加 `checkpoint_tolerance_m` 参数，默认 0.1 m；如果 5 点无法精确接入，报错而不是放宽。

---

## 7. 官方 schema 适配

### 7.1 外层 `LocJsonEntity`

```python
points_json = json.dumps([to_my_location(p) for p in gps_points],
                         separators=(",", ":"))
loc_json = json.dumps({
    "useZip": False,
    "allLocJson": points_json
}, separators=(",", ":"))
```

- `save_record["allLocJson"] = loc_json`
- `obs_payload["run_data"] = gzip_base64_encode(loc_json)`
- `fivePointJson` 继续使用 `PointJsonEntity`，保持不变。

### 7.2 `MyLocation` 点

> **2026-11-11 用真实 fixture 校准**（`rrid=1333592274`，154 点）：
> - `lat` / `lng` 固定 **-1.0**，真实 GCJ-02 坐标在 `gLat` / `gLng`；
> - `gainTime` 是本地时间字符串 `YYYY-MM-DD HH:MM:SS`，不是毫秒串；
> - `totalTime` / `validTime` 是**秒**，不是毫秒；
> - `speed` 是**配速（分钟/公里）**，`avgSpeed` 才是 **m/s**；
> - `coorType` 为 `gcj02`，`state` / `locType` 为 `1`，`bdG` 为 `1`；
> - `type` 首点 `5`、末点 `6`；字段集合含 `id`，不含 `ra` / `im` / `mp`。
>
> 以下代码块是设计初稿，实际字段映射以 `协议/7.3.92/running/schema.py` 为准。

```python
def to_my_location(p, index, start_ms, elapsed_ms, cumulative_m):
    return {
        "lat": p.lat,
        "lng": p.lon,
        "gLat": p.lat,
        "gLng": p.lon,
        "gainTime": str(p.time),
        "type": 0,
        "avgSpeed": p.speed,
        "speed": p.speed,
        "locType": 0,
        "radius": p.accuracy,
        "totalTime": elapsed_ms,
        "totalDis": cumulative_m,
        "flag": start_ms,
        "count": index,
        "bdA": 0, "bdD": 0, "bdG": 0, "bdS": 0,
        "state": 0,
        "stepDistance": 0,
        "validTime": elapsed_ms,
        "validDis": cumulative_m,
        "queueNum": 0,
        "ra": "", "im": "", "locationId": "",
        "coorType": "GCJ02", "mp": "", "dtr": 0
    }
```

> **假设**：`gainTime`、`coorType`、`state`、`dtr` 的精确格式尚未用真实官方 `run_data` 验证。实现前必须先抓一条官方轨迹作为 fixture，按 fixture 调整；不要直接上线猜测值。

### 7.3 字段对照

| 官方字段 | 类型 | 实现取值（fixture 校准后） |
| --- | --- | --- |
| `lat` | double | 固定 `-1.0` |
| `lng` | double | 固定 `-1.0` |
| `gLat` | double | 真实 GCJ-02 纬度 |
| `gLng` | double | 真实 GCJ-02 经度 |
| `gainTime` | String | 本地时间字符串 `YYYY-MM-DD HH:MM:SS` |
| `speed` | double | 配速（分钟/公里） |
| `avgSpeed` | double | 米/秒 |
| `radius` | float | 精度，默认 3.0 |
| `totalTime` | long | 相对起点的**秒**数 |
| `totalDis` | double | 累计米数 |
| `flag` | long | 本次跑步起始时间戳（毫秒，常量） |
| `count` | int | 点序号（1 起） |
| `id` | int | 点序号（1 起） |
| `state` | int | `1` |
| `type` | int | 首点 `5`、末点 `6`、中间 `0` |
| `validTime` / `validDis` | long / double | 同 `totalTime` / `totalDis` |
| `coorType` | String | `gcj02` |

### 7.4 一致性要求

- `save_record.allLocJson` 与 OBS `run_data` 必须来自同一个 `loc_json`；
- `useZip` 必须为 `false`；
- `allLocJson` 是字符串，不是数组；
- `fivePointJson` 的 `PointJsonEntity` 外层保持不变；
- `obs_payload["fixed_point_json"]` 与 `save_record["fivePointJson"]` 一致；
- `speed` 仍使用「分钟/公里 ×1000」；
- `rrid` 在 OBS 上传时为空，`save/record` 后再回填到本地 outbox。

---

## 8. 上传与可靠性

### 8.1 流程

```text
1. 拉规则 / 围栏 / 5 点（现有逻辑）
2. 生成轨迹
3. 组装 save_record / obs_payload / object_key
4. 写入 outbox（落盘）
5. 申请 OBS 预签名 URL
6. PUT OBS
7. POST save/record
8. 回查 get_one_record
9. 回读 OBS 对象验证
10. 标记 outbox 完成
```

### 8.2 outbox

- 目录：CLI 默认 `~/.campusrun/outbox/`，可 `--outbox-dir` 覆盖；
- 文件名：`<uuid>.json`；
- 内容：`save_record`、`obs_payload`、`object_key`、`uuid`、`created_at`、`status`、`rrid`；
- 状态：`pending` / `obs_uploaded` / `saved` / `verified` / `failed`；
- 重试：同一 `uuid`，指数退避；
- 幂等：`save/record` 响应丢失时，用 `uuid` / `startTime` 查历史，找到 rrid 就复用，不重复提交。

### 8.3 回读验证

- 用 `/api/obs/temporary/url` 申请 `Get` 方法；
- 下载同一个 `object_key`；
- 实测 OBS 对象是包含 `run_data` / `speed_json` / `step_freq_json` 等字段的外层 JSON；
  先 `json.loads` 外层，再对 `run_data` 做 gunzip + base64 解码；
- `json.loads` 得到 `LocJsonEntity`；
- 解析 `allLocJson`，检查：
  - 点数 > 2；
  - 每个点有 `lng` / `gLat` / `gLng`；
  - 起点/终点时间与 `save_record` 一致；
- 调用 `get_one_record`，检查 `status`、`jsonFromObs`、`rrid`。

### 8.4 与官方补传的关系

- 首版只处理“新跑一次并上传”；
- 不实现“给已有 rrid 补轨迹”；
- 如果后续需要补传，复用同一套 payload 和 outbox，但必须先验证服务端幂等。

---

## 9. 代码改动清单

### 9.1 新增

- `running/osm/__init__.py`
- `running/osm/campus.py`
- `running/osm/grid.py`
- `running/osm/planner.py`
- `running/schema.py`（已实现；与 OSM 解耦，synthetic 也用）
- `running/outbox.py`（已实现）
- `running/data/campus_xuchang_280173698.json`
- `running/data/campus_xuchang_280173698_grid.json`
- `tools/build_osm_campus.py`
- 测试：
  - `test_osm_campus.py`
  - `test_osm_grid.py`
  - `test_osm_planner.py`
  - `test_official_schema.py`

### 9.2 修改

- `running/builder.py`
  - `build_submission_payload` 增加可选 `all_loc_json` / `route_polyline`；
  - 使用 `schema.py` 生成 `allLocJson` 和 OBS `run_data`；
  - 保留现有合成模式。
- `running/client.py`
  - `execute_full_run(..., path_source="auto", campus=None, ...)`；
  - `auto` 解析为「计分跑 OSM / 自由跑内部合成」；OSM 失败直接返回错误；
  - OSM 分支调用 `TrackPlanner`，生成最终采样轨迹后再调用 `validate_track` 兜底；
  - 接入 outbox、回读验证。
- `sport_client.py`
  - `execute_run(..., path_source="auto", campus=...)` 透传。
- `cli.py` / `running/cli.py`
  - 已移除 `--path-source` 选项；计分跑固定 OSM，自由跑内部合成；
  - `--campus`；
  - `--dry-run`；
  - `--export-track`；
  - `--verify-upload`；
  - `--outbox-dir`。
- `协议/修改记录.md`
  - 实现后追加记录。

---

## 10. CLI 设计

> 2026-11-13 更新：`--path-source` 已从 CLI 移除。计分跑固定 OSM 避建筑；
> `synthetic` 只保留为协议内部参数，供自由跑与单元测试使用。

```bash
# 干跑：只生成轨迹，不上传
python cli.py -t <TOKEN> --uid <UID> \
  --run --distance 3000 --time-sec 1060 \
  --campus xuchang_280173698 \
  --dry-run --export-track track.geojson

# 真实提交
python cli.py -t <TOKEN> --uid <UID> \
  --run --distance 3000 --time-sec 1060 \
  --campus xuchang_280173698

# 上传后回读验证
python cli.py -t <TOKEN> --uid <UID> \
  --verify-upload --rrid <rrid>
```

- 计分跑固定 OSM；自由跑没有官方打卡点，内部仍用合成轨迹；
- `--campus` 对应 `running/data/campus_*.json`；
- `--dry-run` 不联网、不写服务器；
- `--export-track` 输出 GeoJSON，便于在地图上检查。

**实现补充（2026-11-11）**：增加离线验证入口 `running/cli.py --plan-track`，
直接传入 GCJ-02 点位即可规划并导出，不需要 Token：

```bash
python running/cli.py --plan-track --campus xuchang_280173698 \
  --distance 3000 --checkpoints "lat,lon;lat,lon;lat,lon;lat,lon;lat,lon" \
  --export-track track.geojson
```

它只读打包数据、不访问网络，用于在没有测试账号时验证避建筑效果。

---

## 11. 测试与验收

### 11.1 单元测试

| 测试 | 断言 |
| --- | --- |
| `test_osm_campus` | 校园边界闭合；36 栋建筑；坐标范围正确 |
| `test_osm_grid` | 建筑格不可走；道路格低代价；边界外不可走 |
| `test_osm_planner` | A* 可通；0 建筑相交；5 点经过；围栏内 |
| `test_osm_planner` | 目标距离过短时报错 |
| `test_osm_planner` | 点在建筑内时打通 2 m 接入通道；无法打通才报错 |
| `test_official_schema` | `LocJsonEntity` 可解析；`MyLocation` 有 `lng/gLat/gLng` |
| 现有 `test_run_consistency` | 不回归 |
| 现有 `test_history_cli` | 不回归 |

### 11.2 干跑验收

- `--dry-run` 生成轨迹；
- 导出 GeoJSON；
- 人工/脚本检查：
  - 轨迹在校园边界内；
  - 不穿 36 栋建筑；
  - 经过 5 点；
  - 长度 = 目标 ±1%；
  - 时长 = 目标；
  - 平均配速在规则内。

### 11.3 真实验收

- 用一个测试账号提交一次；
- 返回 rrid；
- `get_one_record` 的 `status` / `reasonList` 通过；
- 官方 App 详情页能看到轨迹；
- OBS 回读 `LocJsonEntity` 可解析；
- 记录 rrid、账号、时间、结果到 `协议/修改记录.md`。

### 11.4 回归

- 计分跑默认走 OSM；`--path-source` 已移除，旧命令不需要该参数；
- 内部 `synthetic`（自由跑 / 单测）行为不变；
- 现有测试全过；
- 不新增运行时网络依赖（除官方 API/OBS）。

---

## 12. 风险与限制

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| OSM 没有人行道 | 路径可能走主路/空地，不像真实步行 | 自由空间 A* + 路网偏好；后续补历史轨迹 |
| 建筑到路中位 57.2 m | 路网偏好弱，路径可能穿空地 | 接受粗粒度；后续人工障碍/历史轨迹 |
| 围墙/水域/施工区/校门未映射 | 可能穿过不可通行区域 | 首版明确限制；后续人工障碍层 |
| 5 点在建筑内 | 无法既经过点又不进建筑 | 在该建筑内打通 2 m 接入通道；80 m 内无出口才报错 |
| 官方点在围栏外 | 严格按围栏无法经过点位 | 规划边界外扩 350 m（`getGeoFenceForRun+buffer350m`），并记录为对官方围栏的偏离 |
| 官方 schema 字段未完全确认 | 官方 App 仍可能解析失败 | 先抓真实 `run_data` fixture，再定字段 |
| 服务端不接受生成轨迹 | 成绩无效 | 先 dry-run，再单账号受控提交 |
| 重复闭环圈数过多 | 轨迹重复、视觉不自然 | 闭环 < 400 m 时加绕行段；否则报错或接受 |
| 2 m 栅格内存/CPU | 低端机卡顿 | 预处理 RLE；A* 只搜索局部 |
| OSM ODbL | 分发需署名 | APK 内保留 OSM 署名与许可 |
| Nominatim 不可用 | 开发期定位失败 | 运行时不用 Nominatim；只打包预处理数据 |

---

## 13. 里程碑

| 阶段 | 目标 | 交付 |
| --- | --- | --- |
| M1 | OSM 数据预处理 ✅ 2026-11-11 | `campus_*.json`、`grid.json`、`build_osm_campus.py`、数据测试 |
| M2 | 规划器 ✅ 2026-11-11 | `planner.py`、A*、吸附、排序、校验、dry-run、GeoJSON 导出 |
| M3 | 官方 schema ✅ 2026-11-11 | `running/schema.py`、`LocJsonEntity` + `MyLocation`、单测、真实 fixture |
| M4 | 上传链路 ✅ 2026-11-11 | `client.py` 接入、outbox、回读验证；真实提交待做 |
| M5 | 加固 | 错误码、文档、回归、性能、许可证署名 |

---

## 14. 待确认 / 假设

1. ❌→**更正（2026-11-11）**：官方围栏（12 点）与 OSM 校园 `way/280173698` 基本重合（bbox 仅差几十米），二者不是问题。真正异常的是官方 5 点：4 点在围栏外 61–236 m，1 点在围栏内但落在 OSM 建筑 `280782570`（静庐8）内。按官方围栏重建不会解决越界；需要扩展规划边界，或核查 `runAreaModels`/`runAreaId`（GAP-02）是否有北侧独立围栏。`--fence-gcj` 仍保留，用于官方围栏作为权威边界。
2. ✅ 已解决：`gainTime` 是本地时间字符串 `YYYY-MM-DD HH:MM:SS`（fixture `rrid=1333592274`）。
3. ✅ 已解决：`coorType=gcj02`、`state=1`、`locType=1`、`dtr=0.0`、`bdG=1`；`bdA/bdD/bdS` 为传感器值，实现用 0 / 0 / 当前速度占位。
4. 服务端是否校验轨迹与建筑/道路的关系未知；本方案主要解决官方 App 显示与视觉合理性。
5. `runAreaId` 多区域问题（GAP-02）仍存在；首版按当前围栏/区域处理。
6. 首版只支持 `sport_type=1` 计分跑；自由跑沿用现有合成轨迹。
7. 真实新 schema 尚未提交验证；官方 App 详情页能否画出线需一次受控真实提交确认。

---

## 15. 附录

### 15.1 规划伪代码

```python
def plan(req):
    grid = CostGrid.load(req.campus)
    snapped = [grid.snap(p, radius=30) for p in req.checkpoints]
    if any(s is None for s in snapped):
        raise NoRoute("osm_no_free_cell")

    best = None
    for order in orders(req.checkpoints, req.policy):
        legs = []
        for a, b in pairs(order, close=True):
            path = astar(grid, snapped[a], snapped[b])
            if path is None:
                break
            legs.append(path)
        else:
            loop = concat(legs)
            length = polyline_length(loop)
            if length > req.target_distance_m:
                continue
            loop = insert_exact_checkpoints(loop, req.checkpoints)
            loop = smooth(loop)
            validate(loop, req)
            if best is None or abs(length - req.target_distance_m) < abs(best.length - req.target_distance_m):
                best = loop

    if best is None:
        raise NoRoute("osm_no_route")
    return best
```

### 15.2 关键错误码

| 错误码 | 含义 |
| --- | --- |
| `osm_campus_not_found` | 找不到校园数据 |
| `osm_no_free_cell` | 5 点无法吸附到可走区域 |
| `osm_checkpoint_inside_building` | 5 点在建筑内且无法打通接入通道 |
| `osm_no_route` | 两点之间无可行路径 |
| `osm_target_distance_too_short` | 最短闭环仍大于目标距离 |
| `osm_fence_violation` | 路径越出围栏 |
| `osm_building_collision` | 路径与建筑相交 |
| `osm_schema_invalid` | 官方 schema 校验失败 |

### 15.3 不做的事

- 不把 OSM 栅格/建筑数据上传到服务器；
- 不在运行时调用 Nominatim/Overpass；
- 不伪造官方 `recordUrl`；
- 不绕过官方鉴权；
- 不修改官方 App。
