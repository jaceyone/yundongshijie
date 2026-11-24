# -*- coding: utf-8 -*-
"""
校园跑 · 手动路线编辑器（Tkinter Canvas）

特点
  - 离线矢量底图：允许边界、建筑、道路，全部来自打包的校园 JSON；无瓦片、无 key。
  - 开放折线：不要求闭环；条件为「路线长度 <= 目标距离」。
  - 路线短于目标时，超出模板的部分沿原路反向折返，橙色虚线画在蓝线之上。
  - 5 个官方点必须作为精确顶点插入；靠近点时自动吸附。
  - 保存前调用 route_library.validate_route()，校验建筑/边界/点位/长度。

独立运行
  python route_editor.py --latest-sampler --target 3000
  python route_editor.py --points-file logs/points-xxx.json --target 3000

被主窗口调用
  editor = RouteEditor(parent, checkpoints=..., target_distance=..., ...)
  editor.show_modal()
  result = editor.result

从路线库管理页编辑已有路线
  editor = RouteEditor(..., initial_route=entry.polyline,
                       initial_notes=entry.notes,
                       editing_route_id=entry.route_id)
  保存时覆盖原条目，而不是新增变体。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, simpledialog

# Windows 高 DPI，避免界面发虚；被主窗口调用时重复设置会被忽略。
try:
    from ctypes import windll

    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
PROTOCOL_HOME = REPO_ROOT / "协议" / "7.3.92"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import amap_tiles  # noqa: E402
import route_library as rl  # noqa: E402

BG = "#f3f4f6"
CARD = "#ffffff"
BORDER = "#e5e7eb"
TEXT = "#111827"
MUTED = "#6b7280"
BLUE = "#2563eb"
GREEN = "#16a34a"
GREEN_DARK = "#15803d"
RED = "#dc2626"
ORANGE = "#d97706"
MAP_BG = "#f8fafc"
BUILDING_FILL = "#e5e7eb"
BUILDING_LINE = "#cbd5e1"
ROAD_LINE = "#cbd5e1"
BOUNDARY_LINE = "#93c5fd"

SNAP_CHECKPOINT_PX = 14
SNAP_ROAD_M = 12.0
MIN_VERTEX_DISTANCE_M = 1.0
INSERT_CHECKPOINT_MAX_M = 30.0

METERS_PER_DEG_LAT = 111320.0


# --------------------------------------------------------------------------- 投影


class MapProjection:
    """以校园中心为原点的等距圆柱近似，输出米制世界坐标。"""

    def __init__(self, center_lat: float, center_lon: float):
        self.center_lat = float(center_lat)
        self.center_lon = float(center_lon)
        self.cos_lat = math.cos(math.radians(self.center_lat))

    def latlon_to_world(self, lat: float, lon: float) -> Tuple[float, float]:
        x = (float(lon) - self.center_lon) * METERS_PER_DEG_LAT * self.cos_lat
        y = (float(lat) - self.center_lat) * METERS_PER_DEG_LAT
        return x, y

    def world_to_latlon(self, x: float, y: float) -> Tuple[float, float]:
        lat = self.center_lat + y / METERS_PER_DEG_LAT
        lon = self.center_lon + x / (METERS_PER_DEG_LAT * self.cos_lat)
        return lat, lon


class ViewTransform:
    """世界坐标（米）与画布像素之间的变换。"""

    def __init__(self, canvas: tk.Canvas, projection: MapProjection):
        self.canvas = canvas
        self.projection = projection
        self.scale = 0.5  # 像素/米
        self.view_x = 0.0
        self.view_y = 0.0

    def size(self) -> Tuple[int, int]:
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        return width, height

    def latlon_to_screen(self, lat: float, lon: float) -> Tuple[float, float]:
        x, y = self.projection.latlon_to_world(lat, lon)
        width, height = self.size()
        return (
            width / 2.0 + (x - self.view_x) * self.scale,
            height / 2.0 - (y - self.view_y) * self.scale,
        )

    def screen_to_latlon(self, sx: float, sy: float) -> Tuple[float, float]:
        width, height = self.size()
        x = self.view_x + (sx - width / 2.0) / self.scale
        y = self.view_y - (sy - height / 2.0) / self.scale
        return self.projection.world_to_latlon(x, y)

    def screen_distance(self, first: Tuple[float, float], second: Tuple[float, float]) -> float:
        return math.hypot(first[0] - second[0], first[1] - second[1])

    def fit(self, boundary: Sequence[Sequence[float]], padding: float = 40.0) -> None:
        self.fit_points(boundary, padding_m=0.0, pixels_padding=padding)

    def fit_points(
        self,
        points: Sequence[Sequence[float]],
        padding_m: float = 0.0,
        pixels_padding: float = 40.0,
    ) -> None:
        if not points:
            return
        world = [self.projection.latlon_to_world(lat, lon) for lat, lon in points]
        min_x = min(item[0] for item in world) - padding_m
        max_x = max(item[0] for item in world) + padding_m
        min_y = min(item[1] for item in world) - padding_m
        max_y = max(item[1] for item in world) + padding_m
        width, height = self.size()
        span_x = max(1.0, max_x - min_x)
        span_y = max(1.0, max_y - min_y)
        self.scale = max(
            0.05,
            min(
                (width - 2 * pixels_padding) / span_x,
                (height - 2 * pixels_padding) / span_y,
            ),
        )
        self.view_x = (min_x + max_x) / 2.0
        self.view_y = (min_y + max_y) / 2.0

    def zoom(self, factor: float, sx: float, sy: float) -> None:
        lat, lon = self.screen_to_latlon(sx, sy)
        world_x, world_y = self.projection.latlon_to_world(lat, lon)
        self.scale = max(0.05, min(20.0, self.scale * factor))
        width, height = self.size()
        self.view_x = world_x - (sx - width / 2.0) / self.scale
        self.view_y = world_y + (sy - height / 2.0) / self.scale

    def pan(self, dx: float, dy: float) -> None:
        self.view_x -= dx / self.scale
        self.view_y += dy / self.scale


# --------------------------------------------------------------------------- 校园数据


class CampusMap:
    def __init__(self, campus_id: str):
        self.campus_id = campus_id
        path = PROTOCOL_HOME / "running" / "data" / ("campus_%s.json" % campus_id)
        if not path.is_file():
            raise FileNotFoundError("找不到校园数据: %s" % path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.raw = raw
        self.name = raw.get("name", campus_id)
        self.boundary: List[Tuple[float, float]] = [
            (float(point[0]), float(point[1])) for point in raw.get("boundary", [])
        ]
        self.buildings: List[Dict[str, Any]] = []
        for building in raw.get("buildings", []):
            polygon = [
                (float(point[0]), float(point[1]))
                for point in building.get("polygon", [])
            ]
            if len(polygon) >= 3:
                self.buildings.append(
                    {
                        "osm_id": building.get("osm_id"),
                        "name": building.get("name", ""),
                        "polygon": polygon,
                    }
                )
        self.roads: List[Dict[str, Any]] = []
        for road in raw.get("roads", []):
            polyline = [
                (float(point[0]), float(point[1]))
                for point in road.get("polyline", [])
            ]
            if len(polyline) >= 2:
                self.roads.append(
                    {
                        "osm_id": road.get("osm_id"),
                        "name": road.get("name", ""),
                        "polyline": polyline,
                    }
                )
        if self.boundary:
            center_lat = sum(point[0] for point in self.boundary) / len(self.boundary)
            center_lon = sum(point[1] for point in self.boundary) / len(self.boundary)
        else:
            center_lat = float((raw.get("center") or {}).get("lat", 0))
            center_lon = float((raw.get("center") or {}).get("lon", 0))
        self.projection = MapProjection(center_lat, center_lon)

    def road_segments(self) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
        result = []
        for road in self.roads:
            polyline = road["polyline"]
            result.extend(zip(polyline, polyline[1:]))
        return result

    def nearest_road_point(
        self, lat: float, lon: float, max_distance_m: float = SNAP_ROAD_M
    ) -> Optional[Tuple[float, float]]:
        """把点击点吸附到最近道路；用局部平面近似计算点到线段距离。"""
        best_point = None
        best_distance = max_distance_m
        cos_lat = self.projection.cos_lat
        for (a_lat, a_lon), (b_lat, b_lon) in self.road_segments():
            ax = (a_lon - lon) * METERS_PER_DEG_LAT * cos_lat
            ay = (a_lat - lat) * METERS_PER_DEG_LAT
            bx = (b_lon - lon) * METERS_PER_DEG_LAT * cos_lat
            by = (b_lat - lat) * METERS_PER_DEG_LAT
            dx = bx - ax
            dy = by - ay
            length_sq = dx * dx + dy * dy
            if length_sq <= 1e-9:
                px, py = ax, ay
            else:
                t = max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
                px = ax + t * dx
                py = ay + t * dy
            value = math.hypot(px, py)
            if value < best_distance:
                best_distance = value
                best_point = (
                    lat + py / METERS_PER_DEG_LAT,
                    lon + px / (METERS_PER_DEG_LAT * cos_lat),
                )
        return best_point


# --------------------------------------------------------------------------- 编辑器


class RouteEditor(tk.Toplevel):
    def __init__(
        self,
        master,
        checkpoints: Sequence[Dict[str, Any]],
        target_distance_m: int = 3000,
        min_target_distance_m: int = 0,
        max_target_distance_m: int = 0,
        campus_id: str = "xuchang_280173698",
        unid: Optional[int] = None,
        area_id: int = -1,
        library: Optional[rl.RouteLibrary] = None,
        planner=None,
        modal: bool = False,
        on_close=None,
        initial_route: Optional[Sequence[Any]] = None,
        initial_notes: str = "",
        editing_route_id: Optional[str] = None,
        anchor: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(master)
        self.checkpoints = [dict(point) for point in checkpoints]
        self.target_distance_m = int(target_distance_m)
        self.min_target_distance_m = max(0, int(min_target_distance_m or 0))
        # 0 表示不设上限；允许目标距离超过学校 day_goal / 3000 m。
        self.max_target_distance_m = max(0, int(max_target_distance_m or 0))
        self.campus_id = campus_id
        self.unid = unid
        self.area_id = int(area_id)
        self.library = library
        self.planner = planner
        self.on_close = on_close
        self.initial_notes = str(initial_notes or "")
        self.editing_route_id = editing_route_id
        # 本次取点锚点；只写入路线库历史/条目元数据，不参与路线匹配。
        self.anchor = dict(anchor) if anchor else None
        self.result: Optional[Dict[str, Any]] = None

        self.campus = CampusMap(campus_id)
        self.view: Optional[ViewTransform] = None
        self.tile_layer = amap_tiles.AmapTileLayer(APP_DIR / "cache" / "amap")
        self.satellite_var = tk.BooleanVar(value=False)
        self._closed = False
        self.route: List[Tuple[float, float]] = []
        if initial_route:
            try:
                self.route = rl.normalize_polyline(initial_route)
            except ValueError:
                self.route = []
        self.undo_stack: List[List[Tuple[float, float]]] = []
        self.redo_stack: List[List[Tuple[float, float]]] = []
        self.mode = tk.StringVar(value="add")
        self.snap_road = tk.BooleanVar(value=False)
        self.drag_index: Optional[int] = None
        self.pan_last: Optional[Tuple[float, float]] = None
        self.locked_vertices = set()

        self.title("校园跑 · 编辑路线" if editing_route_id else "校园跑 · 手动画路线")
        self.configure(bg=BG)
        self.minsize(1060, 700)
        self._center_window(1200, 790)
        self._build_ui()
        self.view = ViewTransform(self.canvas, self.campus.projection)
        self.after(80, self._initial_fit)
        self.after(200, self._poll_tiles)
        if modal:
            self.transient(master)
            self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    # ------------------------------------------------------------------ UI

    def _center_window(self, width: int, height: int) -> None:
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        self.geometry(
            "%dx%d+%d+%d"
            % (width, height, (screen_w - width) // 2, (screen_h - height) // 2)
        )

    def _build_ui(self) -> None:
        try:
            default_font = tkfont.nametofont("TkDefaultFont")
            default_font.configure(family="Microsoft YaHei UI", size=10)
        except Exception:
            pass

        toolbar = tk.Frame(self, bg=BG)
        toolbar.pack(fill="x", padx=12, pady=(10, 4))

        tk.Label(toolbar, text="模式", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(
            side="left"
        )
        for value, label in (("add", "加点"), ("edit", "编辑")):
            tk.Radiobutton(
                toolbar,
                text=label,
                value=value,
                variable=self.mode,
                bg=BG,
                fg=TEXT,
                activebackground=BG,
                selectcolor=CARD,
                font=("Microsoft YaHei UI", 10),
                command=self.refresh,
            ).pack(side="left", padx=(4, 0))

        self._toolbar_button(toolbar, "撤销", self.undo).pack(side="left", padx=(12, 0))
        self._toolbar_button(toolbar, "重做", self.redo).pack(side="left", padx=(4, 0))
        self._toolbar_button(toolbar, "反向", self.reverse_route).pack(side="left", padx=(4, 0))
        self._toolbar_button(toolbar, "清空", self.clear_route).pack(side="left", padx=(4, 0))
        self._toolbar_button(toolbar, "适配", self.fit_view).pack(side="left", padx=(4, 0))
        self._toolbar_button(toolbar, "聚焦点位", self.fit_checkpoints).pack(
            side="left", padx=(4, 0)
        )
        self._toolbar_button(toolbar, "插入打卡点", self.insert_missing_checkpoints).pack(
            side="left", padx=(4, 0)
        )
        self._toolbar_button(toolbar, "导出 GeoJSON", self.export_geojson).pack(
            side="left", padx=(4, 0)
        )

        tk.Checkbutton(
            toolbar,
            text="吸附到道路",
            variable=self.snap_road,
            bg=BG,
            fg=TEXT,
            activebackground=BG,
            selectcolor=CARD,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(12, 0))

        tk.Checkbutton(
            toolbar,
            text="卫星地图",
            variable=self.satellite_var,
            command=self._toggle_map_style,
            bg=BG,
            fg=TEXT,
            activebackground=BG,
            selectcolor=CARD,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(8, 0))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        self.canvas = tk.Canvas(
            body,
            bg=MAP_BG,
            highlightthickness=1,
            highlightbackground=BORDER,
            cursor="crosshair",
        )
        self.canvas.bind("<Button-1>", self.on_left_down)
        self.canvas.bind("<B1-Motion>", self.on_left_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_up)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Button-2>", self.on_pan_start)
        self.canvas.bind("<B2-Motion>", self.on_pan_motion)
        self.canvas.bind("<ButtonRelease-2>", self.on_pan_end)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<Control-z>", lambda _event: self.undo())
        self.bind("<Control-y>", lambda _event: self.redo())
        self.bind("<Control-Z>", lambda _event: self.undo())
        self.bind("<Control-Y>", lambda _event: self.redo())

        sidebar = tk.Frame(body, bg=BG, width=300)
        sidebar.pack(side="right", fill="y", padx=(10, 0))
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)

        # 先 pack 侧栏，再让画布占剩余空间；否则 expand=True 的画布会把侧栏挤没。
        self.canvas.pack(side="left", fill="both", expand=True)

        self.status_var = tk.StringVar(
            value="左键加点 · 编辑模式拖点 · 右键删点 · 滚轮缩放 · 中键/Shift 平移"
        )
        tk.Label(
            self,
            textvariable=self.status_var,
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        ).pack(fill="x", padx=14, pady=(0, 8))

    def _toolbar_button(self, parent, text, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=CARD,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            padx=8,
            pady=2,
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        )

    def _build_sidebar(self, parent) -> None:
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=12, pady=10)

        tk.Label(inner, text="路线参数", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(
            anchor="w"
        )

        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="目标距离 (m)", bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(
            side="left"
        )
        self.target_var = tk.StringVar(value=str(self.target_distance_m))
        target_entry = tk.Entry(
            row,
            textvariable=self.target_var,
            width=10,
            font=("Consolas", 10),
            relief="solid",
            bd=1,
            state="readonly",
            readonlybackground=CARD,
            justify="center",
        )
        target_entry.pack(side="right")

        if self.min_target_distance_m or self.max_target_distance_m:
            lower = self.min_target_distance_m or "?"
            upper = self.max_target_distance_m or "不限"
            tk.Label(
                inner,
                text="学校范围：%s–%s m" % (lower, upper),
                bg=CARD,
                fg=MUTED,
                anchor="w",
                font=("Microsoft YaHei UI", 8),
            ).pack(fill="x", pady=(2, 0))

        self.length_var = tk.StringVar(value="路线长度: -")
        self.laps_var = tk.StringVar(value="预计趟数: -")
        self.remain_var = tk.StringVar(value="距离状态: -")
        for variable, color in (
            (self.length_var, TEXT),
            (self.laps_var, TEXT),
            (self.remain_var, MUTED),
        ):
            tk.Label(
                inner,
                textvariable=variable,
                bg=CARD,
                fg=color,
                anchor="w",
                justify="left",
                wraplength=250,
                font=("Microsoft YaHei UI", 10),
            ).pack(fill="x", pady=(6, 0))

        self.map_status_var = tk.StringVar(
            value=(
                "底图：高德地图（加载中）"
                if self.tile_layer is not None and self.tile_layer.enabled
                else "底图：本地矢量（Pillow 未安装）"
            )
        )
        tk.Label(
            inner,
            textvariable=self.map_status_var,
            bg=CARD,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=250,
            font=("Microsoft YaHei UI", 9),
        ).pack(fill="x", pady=(4, 0))

        tk.Label(
            inner,
            text="打卡点",
            bg=CARD,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", pady=(12, 2))
        self.checkpoint_labels: List[tk.Label] = []
        for index, checkpoint in enumerate(self.checkpoints):
            name = checkpoint.get("name") or checkpoint.get("pointName") or ""
            label = tk.Label(
                inner,
                text="P%d %s" % (index + 1, name),
                bg=CARD,
                fg=MUTED,
                anchor="w",
                justify="left",
                wraplength=260,
                font=("Microsoft YaHei UI", 9),
            )
            label.pack(fill="x", pady=1)
            self.checkpoint_labels.append(label)

        notes_row = tk.Frame(inner, bg=CARD)
        notes_row.pack(fill="x", pady=(12, 0))
        tk.Label(notes_row, text="备注", bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(
            side="left"
        )
        self.notes_var = tk.StringVar(value=self.initial_notes)
        tk.Entry(notes_row, textvariable=self.notes_var, font=("Microsoft YaHei UI", 9)).pack(
            side="left", fill="x", expand=True, padx=(6, 0)
        )

        buttons = tk.Frame(inner, bg=CARD)
        buttons.pack(fill="x", pady=(12, 0))
        tk.Button(
            buttons,
            text="校验 / 修复",
            command=lambda: self.run_validation(show_dialog=True),
            bg=BLUE,
            fg="white",
            activebackground="#1d4ed8",
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=10,
            pady=4,
            font=("Microsoft YaHei UI", 10),
            cursor="hand2",
        ).pack(side="left")
        tk.Button(
            buttons,
            text="保存路线",
            command=self.save_route,
            bg=GREEN,
            fg="white",
            activebackground=GREEN_DARK,
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=10,
            pady=4,
            font=("Microsoft YaHei UI", 10, "bold"),
            cursor="hand2",
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            buttons,
            text="取消",
            command=self.cancel,
            bg=CARD,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
            padx=10,
            pady=4,
            font=("Microsoft YaHei UI", 10),
            cursor="hand2",
        ).pack(side="right")

        validation_card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        validation_card.pack(fill="both", expand=True, pady=(10, 0))
        tk.Label(
            validation_card,
            text="校验结果",
            bg=CARD,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", padx=12, pady=(8, 2))
        self.validation_text = tk.Text(
            validation_card,
            height=10,
            wrap="word",
            bg="#0f172a",
            fg="#e2e8f0",
            relief="flat",
            bd=0,
            padx=8,
            pady=6,
            font=("Consolas", 9),
            state="disabled",
        )
        self.validation_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.validation_text.tag_configure("error", foreground="#fca5a5")
        self.validation_text.tag_configure("warning", foreground="#fcd34d")
        self.validation_text.tag_configure("ok", foreground="#86efac")
        self.validation_text.configure(state="normal")
        self.validation_text.insert(
            "end", "点击「校验 / 修复」或「保存路线」：会先自动修复再校验。\n", ("ok",)
        )
        self.validation_text.configure(state="disabled")

    def _initial_fit(self) -> None:
        self.canvas.update_idletasks()
        # 从路线库编辑已有路线时先聚焦这条路线；新画路线仍看整个校园。
        if self.route:
            self.fit_checkpoints()
            return
        if self.campus.boundary:
            self.view.fit(self.campus.boundary)
        self.refresh()

    def _poll_tiles(self) -> None:
        if self._closed:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if self.tile_layer is not None and self.tile_layer.poll():
            self._update_map_status()
            self.redraw()
        self.after(200, self._poll_tiles)

    def _update_map_status(self) -> None:
        if self.tile_layer is None or not self.tile_layer.enabled:
            self.map_status_var.set("底图：本地矢量（Pillow 未安装）")
        elif self.tile_layer.has_any_tiles():
            label = "卫星" if self.tile_layer.style == "satellite" else "标准"
            self.map_status_var.set("底图：高德地图（%s）" % label)
        elif self.tile_layer.last_error:
            self.map_status_var.set("底图：高德地图加载失败，使用本地矢量")
        else:
            self.map_status_var.set("底图：高德地图（加载中）")

    def _toggle_map_style(self) -> None:
        if self.tile_layer is not None:
            self.tile_layer.set_style(
                "satellite" if self.satellite_var.get() else "road"
            )
        self._update_map_status()
        self.redraw()

    def show_modal(self) -> Optional[Dict[str, Any]]:
        self.wait_window(self)
        return self.result

    # ------------------------------------------------------------------ 路线操作

    def _push_undo(self) -> None:
        self.undo_stack.append(list(self.route))
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self) -> None:
        if not self.undo_stack:
            self._set_status("没有可撤销的操作。")
            return
        self.redo_stack.append(list(self.route))
        self.route = self.undo_stack.pop()
        self.refresh()

    def redo(self) -> None:
        if not self.redo_stack:
            self._set_status("没有可重做的操作。")
            return
        self.undo_stack.append(list(self.route))
        self.route = self.redo_stack.pop()
        self.refresh()

    def reverse_route(self) -> None:
        if len(self.route) < 2:
            return
        self._push_undo()
        self.route.reverse()
        self.refresh()

    def clear_route(self) -> None:
        if not self.route:
            return
        if not messagebox.askyesno("清空路线", "确定清空当前路线吗？", parent=self):
            return
        self._push_undo()
        self.route = []
        self.refresh()

    def fit_view(self) -> None:
        if self.campus.boundary:
            self.view.fit(self.campus.boundary)
        self.refresh()

    def fit_checkpoints(self) -> None:
        """把视图聚焦到打卡点和当前路线；画线时比全校园视图更好用。"""
        points = []
        for checkpoint in self.checkpoints:
            try:
                points.append(rl.normalize_point(checkpoint, require_gcj=True))
            except ValueError:
                continue
        points.extend(self.route)
        if not points:
            self.fit_view()
            return
        self.view.fit_points(points, padding_m=80.0)
        self.refresh()

    def add_vertex(self, lat: float, lon: float) -> None:
        bounds_error = self._target_bounds_error(self._target_distance())
        if bounds_error:
            self._set_status(bounds_error + "；请修改 config.json 后重开编辑器。")
            return
        checkpoint = self._nearest_checkpoint(lat, lon)
        if checkpoint is not None:
            lat, lon = rl.normalize_point(checkpoint, require_gcj=True)
        elif self.snap_road.get():
            snapped = self.campus.nearest_road_point(lat, lon)
            if snapped is not None:
                lat, lon = snapped

        if self.route:
            if rl.distance_m(self.route[-1], (lat, lon)) < MIN_VERTEX_DISTANCE_M:
                self._set_status("这个点离上一个顶点太近，已忽略。")
                return
        candidate = list(self.route) + [(float(lat), float(lon))]
        if self._candidate_too_long(candidate):
            self._set_status(
                "再画会超过 %.0f m 上限，已忽略这个点。"
                % self._draw_length_limit()
            )
            return
        self._push_undo()
        self.route = candidate
        self.refresh()

    def _nearest_checkpoint(self, lat: float, lon: float) -> Optional[Dict[str, Any]]:
        best = None
        best_pixels = float("inf")
        screen = self.view.latlon_to_screen(lat, lon)
        for checkpoint in self.checkpoints:
            try:
                cp_lat, cp_lon = rl.normalize_point(checkpoint, require_gcj=True)
            except ValueError:
                continue
            cp_screen = self.view.latlon_to_screen(cp_lat, cp_lon)
            pixels = self.view.screen_distance(screen, cp_screen)
            if pixels < best_pixels:
                best_pixels = pixels
                best = checkpoint
        if best is not None and best_pixels <= SNAP_CHECKPOINT_PX:
            return best
        return None

    def delete_vertex(self, index: int) -> None:
        if index < 0 or index >= len(self.route):
            return
        if index in self.locked_vertices:
            self._set_status("这是官方打卡点顶点，不能删除；需要调整请清空后重画。")
            return
        self._push_undo()
        self.route.pop(index)
        self.refresh()

    def _insert_missing_checkpoints(self) -> int:
        """把离路线 30 m 内的缺失打卡点插成精确顶点；返回插入数量。"""
        if not self.checkpoints or not self.route:
            return 0
        distances = self._checkpoint_distances()
        missing = [
            index
            for index in range(len(self.checkpoints))
            if index >= len(distances) or distances[index] > 0.1
        ]
        inserted = 0
        for checkpoint_index in missing:
            try:
                cp_lat, cp_lon = rl.normalize_point(
                    self.checkpoints[checkpoint_index], require_gcj=True
                )
            except ValueError:
                continue
            best = self._nearest_segment(cp_lat, cp_lon)
            if best is None:
                continue
            segment_index, distance = best
            if distance <= INSERT_CHECKPOINT_MAX_M:
                candidate = list(self.route)
                candidate.insert(segment_index + 1, (cp_lat, cp_lon))
                if self._candidate_too_long(candidate):
                    continue
                self.route = candidate
                inserted += 1
            elif self._detour_to_checkpoint(cp_lat, cp_lon):
                inserted += 1
        return inserted

    def insert_missing_checkpoints(self) -> None:
        if not self.checkpoints:
            return
        if not self.route:
            self._set_status("先画一段路线，再插入打卡点。")
            return
        distances = self._checkpoint_distances()
        if all(distance <= 0.1 for distance in distances):
            self._set_status("5 个打卡点已经都在路线上了。")
            return
        self._push_undo()
        inserted = self._insert_missing_checkpoints()
        if inserted:
            self.refresh()
            self._set_status("插入了 %d 个打卡点；请检查新连接线是否穿建筑。" % inserted)
        else:
            self._set_status(
                "没有可插入的打卡点：它们离路线都超过 %.0f m。"
                % INSERT_CHECKPOINT_MAX_M
            )

    def _nearest_segment(self, lat: float, lon: float) -> Optional[Tuple[int, float]]:
        if len(self.route) < 2:
            return None
        best_index = -1
        best_distance = float("inf")
        cos_lat = self.campus.projection.cos_lat
        for index, (a, b) in enumerate(zip(self.route, self.route[1:])):
            ax = (a[1] - lon) * METERS_PER_DEG_LAT * cos_lat
            ay = (a[0] - lat) * METERS_PER_DEG_LAT
            bx = (b[1] - lon) * METERS_PER_DEG_LAT * cos_lat
            by = (b[0] - lat) * METERS_PER_DEG_LAT
            dx = bx - ax
            dy = by - ay
            length_sq = dx * dx + dy * dy
            if length_sq <= 1e-9:
                px, py = ax, ay
            else:
                t = max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
                px = ax + t * dx
                py = ay + t * dy
            value = math.hypot(px, py)
            if value < best_distance:
                best_distance = value
                best_index = index
        if best_index < 0:
            return None
        return best_index, best_distance

    # ------------------------------------------------------------------ 自动修复

    @staticmethod
    def _dedupe_route(points, tolerance_m: float = 0.5):
        result = []
        for point in points:
            value = (float(point[0]), float(point[1]))
            if not result or rl.distance_m(result[-1], value) > tolerance_m:
                result.append(value)
        return result

    def _is_checkpoint_point(self, point, tolerance_m: float = 0.1) -> bool:
        for checkpoint in self.checkpoints:
            try:
                lat, lon = rl.normalize_point(checkpoint, require_gcj=True)
            except ValueError:
                continue
            if rl.distance_m(point, (lat, lon)) <= tolerance_m:
                return True
        return False

    def _checkpoint_building_ids(self) -> set:
        ids = set()
        if self.planner is None:
            return ids
        for checkpoint in self.checkpoints:
            try:
                lat, lon = rl.normalize_point(checkpoint, require_gcj=True)
            except ValueError:
                continue
            building = self.planner.campus.building_at(lat, lon)
            if building is not None:
                ids.add(int(building.osm_id))
        return ids

    def _sample_segment(self, a, b):
        length = rl.distance_m(a, b)
        steps = max(1, int(math.ceil(length)))
        for step in range(steps + 1):
            fraction = step / steps
            yield (
                a[0] + (b[0] - a[0]) * fraction,
                a[1] + (b[1] - a[1]) * fraction,
            )

    def _first_collision(self, allowed_buildings):
        """返回 (段下标, 建筑 id 或 None)；无冲突返回 None。"""
        if self.planner is None or len(self.route) < 2:
            return None
        for index in range(len(self.route) - 1):
            a, b = self.route[index], self.route[index + 1]
            for lat, lon in self._sample_segment(a, b):
                building = self.planner.campus.building_at(lat, lon)
                if building is not None and int(building.osm_id) in allowed_buildings:
                    continue
                col, row = self.planner.grid.latlon_to_cell(lat, lon)
                if (
                    not self.planner.grid.in_bounds(col, row)
                    or not self.planner.grid.walkable(col, row)
                ):
                    building_id = (
                        int(building.osm_id) if building is not None else None
                    )
                    return index, building_id
        return None

    def _astar_between(self, a, b):
        """复用协议 OSM 规划器的 A*，在两点之间生成避建筑路径。"""
        if self.planner is None:
            return None
        start = self.planner.grid.snap(a[0], a[1], radius_m=60.0)
        goal = self.planner.grid.snap(b[0], b[1], radius_m=60.0)
        if start is None or goal is None:
            return None
        path = self.planner._astar((start[0], start[1]), (goal[0], goal[1]))
        if not path:
            return None
        try:
            points = self.planner._leg_points(path, smooth=False)
        except Exception:
            return None
        result = [(float(lat), float(lon)) for lat, lon in points]
        start_lat, start_lon = self.planner.grid.cell_latlon(start[0], start[1])
        goal_lat, goal_lon = self.planner.grid.cell_latlon(goal[0], goal[1])
        result = (
            [(float(start_lat), float(start_lon))]
            + result
            + [(float(goal_lat), float(goal_lon))]
        )
        return self._dedupe_route(result)

    def _detour_to_checkpoint(self, lat: float, lon: float) -> bool:
        """打卡点离路线较远时，用 A* 绕到打卡点再回到原段。"""
        if self.planner is None or len(self.route) < 2:
            return False
        best = self._nearest_segment(lat, lon)
        if best is None:
            return False
        index, _ = best
        a, b = self.route[index], self.route[index + 1]
        checkpoint = (float(lat), float(lon))
        first = self._astar_between(a, checkpoint)
        second = self._astar_between(checkpoint, b)
        if not first or not second:
            return False
        segment = self._dedupe_route(
            [a] + first + [checkpoint] + second + [b]
        )
        candidate = self.route[:index] + segment + self.route[index + 2 :]
        if self._candidate_too_long(candidate):
            return False
        self.route = candidate
        return True

    def _trim_to_target(self) -> bool:
        target = self._target_distance()
        if target <= 0 or len(self.route) < 2:
            return False
        length = rl.polyline_length(self.route)
        if length <= target + 0.001:
            return False
        result = [self.route[0]]
        accumulated = 0.0
        for a, b in zip(self.route, self.route[1:]):
            segment = rl.distance_m(a, b)
            if accumulated + segment >= target:
                result.append(
                    rl.interpolate_point(
                        a, b, (target - accumulated) / segment
                    )
                )
                break
            result.append(b)
            accumulated += segment
        self.route = self._dedupe_route(result)
        return True

    def _append_validation_fixes(self, fixes) -> None:
        self.validation_text.configure(state="normal")
        self.validation_text.insert("end", "\n已自动修复：\n", ("ok",))
        for fix in fixes:
            self.validation_text.insert("end", "· %s\n" % fix, ("ok",))
        self.validation_text.configure(state="disabled")

    def auto_repair(self):
        """自动修复目标范围/重复点/超长/缺打卡点；穿建筑/越界直接报错。

        返回 (fixes, fatal_error)；fatal_error 非空表示无法自动修复。
        """
        fixes = []
        target = self._target_distance()
        bounds_error = self._target_bounds_error(target)
        if bounds_error:
            return fixes, bounds_error
        if len(self.route) < 2:
            return fixes, "路线至少需要 2 个点，无法自动修复"

        original = list(self.route)
        cleaned = self._dedupe_route(self.route)
        if cleaned != self.route:
            self.route = cleaned
            fixes.append("移除过近的重复顶点")
        dense = rl.simplify_dense_vertices(self.route, self.checkpoints)
        if dense != self.route:
            fixes.append(
                "移除 %d 个过密顶点（<%.0f m）"
                % (len(self.route) - len(dense),
                   rl.DEFAULT_DENSE_VERTEX_SPACING_M)
            )
            self.route = dense
        if self._trim_to_target():
            fixes.append("路线超过目标，已截断到 %d m" % target)
        inserted = self._insert_missing_checkpoints()
        if inserted:
            fixes.append("自动插入 %d 个打卡点" % inserted)
        if self._trim_to_target():
            fixes.append("最终截断到目标 %d m" % target)

        fatal = ""
        collision = self._first_collision(self._checkpoint_building_ids())
        if collision is not None:
            index, _building_id = collision
            fatal = "第 %d 段穿建筑/边界，请重画" % (index + 1)

        if self.route != original:
            self.undo_stack.append(original)
            if len(self.undo_stack) > 100:
                self.undo_stack.pop(0)
            self.redo_stack.clear()
            self.refresh()
        return fixes, fatal

    # ------------------------------------------------------------------ 事件

    def on_left_down(self, event) -> None:
        if event.state & 0x0001:  # Shift
            self.pan_last = (event.x, event.y)
            return
        lat, lon = self.view.screen_to_latlon(event.x, event.y)
        if self.mode.get() == "add":
            self.add_vertex(lat, lon)
            return
        index = self._nearest_route_vertex(event.x, event.y)
        if index is None:
            self._set_status("编辑模式：请拖动已有的顶点；切到「加点」可继续画。")
            return
        if index in self.locked_vertices:
            self._set_status("这是官方打卡点顶点，不能拖动。")
            return
        self._push_undo()
        self.drag_index = index

    def on_left_motion(self, event) -> None:
        if self.pan_last is not None:
            dx = event.x - self.pan_last[0]
            dy = event.y - self.pan_last[1]
            self.pan_last = (event.x, event.y)
            self.view.pan(dx, dy)
            self.redraw()
            return
        if self.drag_index is None:
            return
        lat, lon = self.view.screen_to_latlon(event.x, event.y)
        if self.snap_road.get():
            snapped = self.campus.nearest_road_point(lat, lon)
            if snapped is not None:
                lat, lon = snapped
        candidate = list(self.route)
        candidate[self.drag_index] = (lat, lon)
        if self._candidate_too_long(candidate):
            self._set_status(
                "拖动后会超过 %.0f m 上限。" % self._draw_length_limit()
            )
            return
        self.route = candidate
        self.refresh()

    def on_left_up(self, _event) -> None:
        self.drag_index = None
        self.pan_last = None

    def on_right_click(self, event) -> None:
        index = self._nearest_route_vertex(event.x, event.y)
        if index is None:
            return
        self.delete_vertex(index)

    def on_mouse_wheel(self, event) -> None:
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        self.view.zoom(factor, event.x, event.y)
        self.redraw()

    def on_pan_start(self, event) -> None:
        self.pan_last = (event.x, event.y)

    def on_pan_motion(self, event) -> None:
        if self.pan_last is None:
            return
        dx = event.x - self.pan_last[0]
        dy = event.y - self.pan_last[1]
        self.pan_last = (event.x, event.y)
        self.view.pan(dx, dy)
        self.redraw()

    def on_pan_end(self, _event) -> None:
        self.pan_last = None

    def _nearest_route_vertex(self, sx: float, sy: float) -> Optional[int]:
        best_index = None
        best_distance = 12.0
        for index, point in enumerate(self.route):
            screen = self.view.latlon_to_screen(point[0], point[1])
            value = math.hypot(screen[0] - sx, screen[1] - sy)
            if value < best_distance:
                best_distance = value
                best_index = index
        return best_index

    # ------------------------------------------------------------------ 绘制

    def _polygon_points(self, polygon: Sequence[Tuple[float, float]]) -> List[float]:
        points = []
        for lat, lon in polygon:
            sx, sy = self.view.latlon_to_screen(lat, lon)
            points.extend([sx, sy])
        return points

    def redraw(self) -> None:
        if self.view is None:
            return
        canvas = self.canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())

        # 官方底图：优先 AMap 瓦片；瓦片未到/离线时用本地矢量兜底。
        show_vector = (
            self.tile_layer is None
            or not self.tile_layer.enabled
            or not self.tile_layer.has_any_tiles()
        )
        if show_vector:
            # 建筑
            for building in self.campus.buildings:
                points = self._polygon_points(building["polygon"])
                if len(points) >= 6:
                    canvas.create_polygon(
                        points,
                        fill=BUILDING_FILL,
                        outline=BUILDING_LINE,
                        width=1,
                        tags=("building",),
                    )

            # 道路
            for road in self.campus.roads:
                points = self._polygon_points(road["polyline"])
                if len(points) >= 4:
                    canvas.create_line(
                        points,
                        fill=ROAD_LINE,
                        width=max(1, min(4, self.view.scale * 3)),
                        tags=("road",),
                    )

        if self.tile_layer is not None and self.tile_layer.enabled:
            self.tile_layer.draw(canvas, self.view, width, height)

        # 允许边界
        if self.campus.boundary:
            points = self._polygon_points(self.campus.boundary)
            points.extend(points[:2])
            canvas.create_line(
                points,
                fill=BOUNDARY_LINE,
                width=2,
                dash=(6, 4),
                tags=("boundary",),
            )

        # 路线
        if self.route:
            points = self._polygon_points(self.route)
            if len(points) >= 4:
                canvas.create_line(
                    points,
                    fill=BLUE,
                    width=3,
                    tags=("route",),
                )
            for index, (lat, lon) in enumerate(self.route):
                sx, sy = self.view.latlon_to_screen(lat, lon)
                radius = 4 if index not in self.locked_vertices else 6
                canvas.create_oval(
                    sx - radius,
                    sy - radius,
                    sx + radius,
                    sy + radius,
                    fill="white",
                    outline=BLUE,
                    width=2,
                    tags=("vertex",),
                )

        # 折返段：展开结果里超出模板的部分，橙色虚线画在蓝线之上。
        # 2 px 屏幕偏移只用于绘制，不写入 self.route，也不进入提交几何。
        target = self._target_distance()
        length = rl.polyline_length(self.route) if len(self.route) >= 2 else 0.0
        if (
            len(self.route) >= 2
            and target > 0
            and not self._target_bounds_error(target)
            and length < target
        ):
            expanded = rl.expand_polyline_to_distance(self.route, target)
            if len(expanded) > len(self.route):
                foldback = [self.route[-1]] + expanded[len(self.route):]
                points = []
                for lat, lon in foldback:
                    sx, sy = self.view.latlon_to_screen(lat, lon)
                    points.extend([sx, sy - 2])
                if len(points) >= 4:
                    canvas.create_line(
                        points,
                        fill=ORANGE,
                        width=2,
                        dash=(5, 4),
                        tags=("return",),
                    )

        # 打卡点
        checkpoint_map = self._checkpoint_vertex_map()
        for index, checkpoint in enumerate(self.checkpoints):
            try:
                lat, lon = rl.normalize_point(checkpoint, require_gcj=True)
            except ValueError:
                continue
            sx, sy = self.view.latlon_to_screen(lat, lon)
            on_route = index in checkpoint_map
            canvas.create_oval(
                sx - 9,
                sy - 9,
                sx + 9,
                sy + 9,
                fill="#dcfce7" if on_route else "#fee2e2",
                outline=GREEN if on_route else RED,
                width=2,
                tags=("checkpoint",),
            )
            canvas.create_text(
                sx,
                sy,
                text=str(index + 1),
                fill=GREEN_DARK if on_route else RED,
                font=("Microsoft YaHei UI", 9, "bold"),
                tags=("checkpoint",),
            )
            canvas.create_text(
                sx + 12,
                sy - 12,
                text="P%d" % (index + 1),
                anchor="w",
                fill=TEXT,
                font=("Microsoft YaHei UI", 8, "bold"),
                tags=("checkpoint",),
            )

        # 比例尺与底图署名
        self._draw_scale_bar(width, height)
        if self.tile_layer is not None and self.tile_layer.enabled:
            label = "© 高德地图"
            if self.tile_layer.style == "satellite":
                label += "（卫星）"
        else:
            label = "本地矢量底图"
        canvas.create_text(
            width - 12,
            height - 10,
            text=label,
            anchor="se",
            fill=MUTED,
            font=("Microsoft YaHei UI", 8),
        )

    def _draw_scale_bar(self, width: int, height: int) -> None:
        meters = 100.0
        pixels = meters * self.view.scale
        if pixels < 40:
            meters = 500.0
            pixels = meters * self.view.scale
        if pixels < 40:
            meters = 1000.0
            pixels = meters * self.view.scale
        x0 = 20
        y0 = height - 24
        x1 = x0 + pixels
        self.canvas.create_line(x0, y0, x1, y0, fill=TEXT, width=2)
        self.canvas.create_line(x0, y0 - 4, x0, y0 + 4, fill=TEXT, width=2)
        self.canvas.create_line(x1, y0 - 4, x1, y0 + 4, fill=TEXT, width=2)
        self.canvas.create_text(
            (x0 + x1) / 2,
            y0 - 10,
            text="%.0f m" % meters,
            fill=TEXT,
            font=("Microsoft YaHei UI", 8),
        )
        self.canvas.create_text(
            width - 20,
            20,
            text="N",
            fill=MUTED,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.canvas.create_line(width - 20, 30, width - 20, 46, fill=MUTED, width=2, arrow="last")

    # ------------------------------------------------------------------ 状态/校验

    def _target_distance(self) -> int:
        try:
            return int(float(self.target_var.get().strip()))
        except (TypeError, ValueError):
            return 0

    def _draw_length_limit(self) -> float:
        """画线时的硬上限：目标距离与学校单日目标中较小者。"""
        limit = (
            float(self.max_target_distance_m)
            if self.max_target_distance_m > 0
            else float("inf")
        )
        target = self._target_distance()
        if target > 0:
            limit = min(limit, float(target))
        return limit

    def _candidate_too_long(self, route: Sequence[Tuple[float, float]]) -> bool:
        limit = self._draw_length_limit()
        if not math.isfinite(limit):
            return False
        return rl.polyline_length(route) > limit + 0.001

    def _target_bounds_error(self, target: int) -> str:
        if target <= 0:
            return "目标距离必须是正整数"
        if self.min_target_distance_m > 0 and target < self.min_target_distance_m:
            return "目标距离 %d m 低于学校最低 %d m" % (
                target,
                self.min_target_distance_m,
            )
        if self.max_target_distance_m > 0 and target > self.max_target_distance_m:
            return "目标距离 %d m 超过学校单日目标 %d m" % (
                target,
                self.max_target_distance_m,
            )
        return ""

    def _show_validation_error(self, message: str) -> None:
        self.validation_text.configure(state="normal")
        self.validation_text.delete("1.0", "end")
        self.validation_text.insert("end", message + "\n", ("error",))
        self.validation_text.configure(state="disabled")

    def _checkpoint_distances(self) -> List[float]:
        """每个打卡点到最近路线顶点的距离；不抛异常，便于实时提示。"""
        if not self.route:
            return [float("inf")] * len(self.checkpoints)
        result: List[float] = []
        for checkpoint in self.checkpoints:
            try:
                lat, lon = rl.normalize_point(checkpoint, require_gcj=True)
            except ValueError:
                result.append(float("inf"))
                continue
            result.append(
                min(rl.distance_m((lat, lon), vertex) for vertex in self.route)
            )
        return result

    def _checkpoint_vertex_map(self) -> set:
        """返回“已经精确经过”的打卡点下标集合。"""
        return {
            index
            for index, distance in enumerate(self._checkpoint_distances())
            if distance <= 0.1
        }

    def _locked_vertex_indices(self) -> set:
        """官方打卡点所占的顶点不允许拖动/删除。"""
        locked = set()
        if len(self.route) < 2:
            return locked
        for checkpoint in self.checkpoints:
            try:
                lat, lon = rl.normalize_point(checkpoint, require_gcj=True)
            except ValueError:
                continue
            best_index = -1
            best_distance = float("inf")
            for index, vertex in enumerate(self.route):
                value = rl.distance_m((lat, lon), vertex)
                if value < best_distance:
                    best_distance = value
                    best_index = index
            if best_index >= 0 and best_distance <= 0.1:
                locked.add(best_index)
        return locked

    def refresh(self) -> None:
        self.locked_vertices = self._locked_vertex_indices()
        self.redraw()
        self._update_status_panel()

    def _update_status_panel(self) -> None:
        target = self._target_distance()
        length = rl.polyline_length(self.route) if len(self.route) >= 2 else 0.0
        self.length_var.set("路线长度: %.1f m" % length)
        bounds_error = self._target_bounds_error(target)
        if bounds_error:
            self.laps_var.set("预计趟数: -")
            self.remain_var.set("距离状态: %s，无法保存" % bounds_error)
        elif length <= 0:
            self.laps_var.set("预计趟数: -")
            self.remain_var.set("距离状态: 继续画")
        else:
            laps = target / length
            forward = min(laps, 1.0)
            foldback = max(0.0, laps - 1.0)
            self.laps_var.set(
                "预计趟数: %.2f 趟（正向 %.2f + 折返 %.2f）"
                % (laps, forward, foldback)
            )
            if length > target + 0.001:
                self.remain_var.set("距离状态: 超出 %.1f m，无法保存" % (length - target))
            elif laps > rl.DEFAULT_MAX_LAPS:
                self.remain_var.set(
                    "距离状态: 还需 %.1f m；趟数偏多" % (target - length)
                )
            else:
                self.remain_var.set(
                    "距离状态: 还需 %.1f m；将沿原路折返" % (target - length)
                )

        distances = self._checkpoint_distances()
        for index, label in enumerate(self.checkpoint_labels):
            name = self.checkpoints[index].get("name") or ""
            if index < len(distances) and distances[index] <= 0.1:
                label.configure(
                    text="P%d %s  ✓" % (index + 1, name),
                    fg=GREEN_DARK,
                )
            elif self.route and index < len(distances) and math.isfinite(distances[index]):
                label.configure(
                    text="P%d %s  ✗ 最近 %.1f m" % (index + 1, name, distances[index]),
                    fg=RED,
                )
            else:
                label.configure(
                    text="P%d %s  ✗ 未经过" % (index + 1, name), fg=RED
                )

    def run_validation(self, show_dialog: bool = False) -> Optional[rl.RouteValidation]:
        target = self._target_distance()
        bounds_error = self._target_bounds_error(target)
        if bounds_error:
            if show_dialog:
                messagebox.showwarning("校验", bounds_error, parent=self)
            self._set_status(bounds_error + "；无法保存/提交。")
            self._show_validation_error(bounds_error)
            self._update_status_panel()
            return None
        fixes, fatal = self.auto_repair()
        if fatal:
            if show_dialog:
                messagebox.showwarning("校验", fatal, parent=self)
            self._set_status(fatal)
            self._show_validation_error(fatal)
            return None
        report = rl.validate_route(
            self.route,
            self.checkpoints,
            target,
            planner=self.planner,
        )
        self._show_validation(report)
        if fixes:
            self._append_validation_fixes(fixes)
            self._set_status(
                "已自动修复 %d 处：%s" % (len(fixes), "；".join(fixes))
            )
        if show_dialog:
            if report.ok:
                message = (
                    "路线 %.1f m，%.2f 趟（不足部分原路折返）；5 点与建筑/边界校验通过。"
                    % (report.length_m, report.laps)
                )
                if fixes:
                    message += "\n\n已自动修复：\n" + "\n".join(
                        "· " + item for item in fixes
                    )
                messagebox.showinfo("校验通过", message, parent=self)
            else:
                messagebox.showwarning(
                    "校验未通过",
                    "\n".join(report.errors[:8]),
                    parent=self,
                )
        return report

    def _show_validation(self, report: rl.RouteValidation) -> None:
        self.validation_text.configure(state="normal")
        self.validation_text.delete("1.0", "end")
        if report.ok:
            self.validation_text.insert(
                "end",
                "校验通过\n路线 %.1f m，目标 %.0f m，%.2f 趟（不足部分原路折返）。\n"
                % (report.length_m, self._target_distance(), report.laps),
                ("ok",),
            )
        else:
            self.validation_text.insert("end", "校验未通过\n", ("error",))
            for item in report.errors:
                self.validation_text.insert("end", "· %s\n" % item, ("error",))
        if report.order:
            self.validation_text.insert(
                "end",
                "经过顺序: %s\n"
                % " → ".join("P%d" % (index + 1) for index in report.order),
                ("ok",),
            )
        for item in report.warnings:
            self.validation_text.insert("end", "· %s\n" % item, ("warning",))
        self.validation_text.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    # ------------------------------------------------------------------ 保存/导出

    def save_route(self) -> None:
        target = self._target_distance()
        bounds_error = self._target_bounds_error(target)
        if bounds_error:
            messagebox.showwarning("不能保存", bounds_error, parent=self)
            self._show_validation_error(bounds_error)
            self._update_status_panel()
            return
        if self.planner is None:
            if not messagebox.askyesno(
                "未加载校园数据",
                "未加载校园数据，无法校验建筑/边界。仍要保存吗？",
                parent=self,
            ):
                return
        report = self.run_validation(show_dialog=False)
        if report is None:
            return
        if not report.ok:
            messagebox.showwarning(
                "不能保存",
                "路线校验未通过：\n\n" + "\n".join(report.errors[:8]),
                parent=self,
            )
            return
        if self.library is None:
            self.library = rl.RouteLibrary()
        notes = self.notes_var.get().strip()
        if not notes:
            notes = simpledialog.askstring(
                "保存路线", "备注（可选）：", parent=self
            ) or ""
        try:
            if self.editing_route_id:
                entry = self.library.update(
                    self.editing_route_id,
                    self.route,
                    self.checkpoints,
                    notes=notes,
                    validation=report,
                    anchor=self.anchor,
                )
            else:
                entry = self.library.add(
                    self.checkpoints,
                    self.route,
                    self.unid,
                    self.area_id,
                    self.campus_id,
                    notes=notes,
                    validation=report,
                    anchor=self.anchor,
                )
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return

        ordered = [self.checkpoints[index] for index in entry.order]
        expanded = rl.expand_polyline_to_distance(self.route, target)
        self.result = {
            "polyline": [[float(lat), float(lon)] for lat, lon in self.route],
            "expanded_polyline": [
                [float(lat), float(lon)] for lat, lon in expanded
            ],
            "ordered_checkpoints": ordered,
            "entry": entry,
            "validation": report,
        }
        gap = max(0.0, target - report.length_m)
        if self.editing_route_id:
            messagebox.showinfo(
                "已更新",
                "路线 %.1f m，%.2f 趟；不足 %.1f m 沿原路折返到 %d m。\n"
                "route_id=%s\n已覆盖路线库中的这条路线。"
                % (report.length_m, report.laps, gap, target, entry.route_id[:8]),
                parent=self,
            )
        else:
            messagebox.showinfo(
                "已保存",
                "路线 %.1f m，%.2f 趟；不足 %.1f m 沿原路折返到 %d m。\n"
                "route_id=%s\n可用于手动路线库提交。"
                % (report.length_m, report.laps, gap, target, entry.route_id[:8]),
                parent=self,
            )
        self._finish()

    def export_geojson(self) -> None:
        if len(self.route) < 2:
            messagebox.showwarning("导出", "路线至少需要 2 个点。", parent=self)
            return
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出 GeoJSON",
            defaultextension=".geojson",
            filetypes=[("GeoJSON", "*.geojson"), ("JSON", "*.json")],
        )
        if not path:
            return
        feature = {
            "type": "Feature",
            "properties": {
                "campus": self.campus_id,
                "length_m": round(rl.polyline_length(self.route), 2),
                "target_distance_m": self._target_distance(),
                "frame": "GCJ-02",
                "points": [
                    {
                        "name": checkpoint.get("name"),
                        "glat": rl.normalize_point(checkpoint, require_gcj=True)[0],
                        "glon": rl.normalize_point(checkpoint, require_gcj=True)[1],
                    }
                    for checkpoint in self.checkpoints
                ],
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[lon, lat] for lat, lon in self.route],
            },
        }
        Path(path).write_text(
            json.dumps(
                {"type": "FeatureCollection", "features": [feature]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._set_status("已导出: %s" % path)

    # ------------------------------------------------------------------ 关闭

    def cancel(self) -> None:
        self.result = None
        self._finish()

    def _finish(self) -> None:
        self._closed = True
        if self.tile_layer is not None:
            self.tile_layer.shutdown()
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                pass


# --------------------------------------------------------------------------- 独立入口


def _load_points_from_file(path: Path) -> Tuple[List[Dict[str, Any]], Optional[int], int]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("records"), list) and raw["records"]:
        record = raw["records"][0]
        return (
            list(record.get("points") or []),
            raw.get("unid"),
            int(record.get("area_id", -1) or -1),
        )
    if isinstance(raw, dict) and isinstance(raw.get("points"), list):
        return list(raw["points"]), raw.get("unid"), int(raw.get("area_id", -1) or -1)
    if isinstance(raw, list):
        return list(raw), None, -1
    raise ValueError("点位文件格式无法识别: %s" % path)


def _load_report_rule_limits(path) -> Tuple[int, int]:
    """从取点报告读取学校 min_distance / day_goal；读不到时返回 (0, 0)。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return 0, 0
    rule = raw.get("rule") if isinstance(raw, dict) else None
    if not isinstance(rule, dict):
        return 0, 0
    try:
        return (
            int(rule.get("min_distance", 0) or 0),
            int(rule.get("day_goal", 0) or 0),
        )
    except (TypeError, ValueError):
        return 0, 0


def _latest_sampler_report() -> Optional[Path]:
    reports = sorted((APP_DIR / "logs").glob("points-*.json"))
    return reports[-1] if reports else None


def _parse_points_text(text: str) -> List[Dict[str, Any]]:
    result = []
    for index, item in enumerate(text.split(";")):
        item = item.strip()
        if not item:
            continue
        parts = [part.strip() for part in item.replace("，", ",").split(",")]
        if len(parts) != 2:
            raise ValueError("第 %d 个点位不是 lat,lon 格式" % (index + 1))
        result.append({"glat": float(parts[0]), "glon": float(parts[1]), "name": "P%d" % (index + 1)})
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="校园跑 · 手动画路线编辑器")
    parser.add_argument("--points-file", default="", help="点位 JSON（sampler 报告或点位数组）")
    parser.add_argument("--points", default="", help="直接给 GCJ 点: lat,lon;lat,lon;...")
    parser.add_argument(
        "--latest-sampler",
        action="store_true",
        help="使用 logs/ 下最新的 points-*.json",
    )
    parser.add_argument("--target", type=int, default=3000, help="目标距离（米，可超过 3000）")
    parser.add_argument(
        "--min-distance", type=int, default=0, help="学校最低距离；默认读取取点报告"
    )
    parser.add_argument(
        "--max-distance", type=int, default=0, help="可选上限；0 或省略表示不限"
    )
    parser.add_argument("--campus", default="xuchang_280173698", help="校园数据 id")
    parser.add_argument("--unid", type=int, default=0, help="学校 unid")
    parser.add_argument("--area-id", type=int, default=-1, help="跑步区域 id")
    parser.add_argument("--library", default="", help="路线库 JSON 路径")
    parser.add_argument("--smoke-test", action="store_true", help="打开后 0.8 秒自动关闭，用于检查 UI 能否启动")
    args = parser.parse_args(argv)

    checkpoints: List[Dict[str, Any]] = []
    unid = args.unid
    area_id = args.area_id
    report_path: Optional[Path] = None
    if args.points:
        checkpoints = _parse_points_text(args.points)
    elif args.points_file:
        report_path = Path(args.points_file)
        checkpoints, file_unid, file_area = _load_points_from_file(report_path)
        if file_unid is not None:
            unid = int(file_unid)
        if file_area >= -1:
            area_id = file_area
    else:
        report = _latest_sampler_report()
        if report is not None:
            report_path = report
            checkpoints, file_unid, file_area = _load_points_from_file(report)
            if file_unid is not None:
                unid = int(file_unid)
            if file_area >= -1:
                area_id = file_area
            print("使用采样报告:", report)

    if not checkpoints:
        print("没有点位。先运行 route_sampler.py，或用 --points-file / --points 指定。")
        return 2

    min_distance = max(0, int(args.min_distance or 0))
    # 默认不设上限：目标距离可以超过学校 day_goal / 3000 m。
    max_distance = max(0, int(args.max_distance or 0))
    if report_path is not None:
        report_min, _report_max = _load_report_rule_limits(report_path)
        min_distance = min_distance or report_min
    if not min_distance:
        min_distance = 1000

    planner = rl.load_planner(args.campus)
    library = rl.RouteLibrary(args.library or None)

    root = tk.Tk()
    root.withdraw()
    try:
        root.iconbitmap(default=str(APP_DIR / "assets" / "app.ico"))
    except Exception:
        pass

    editor = RouteEditor(
        root,
        checkpoints=checkpoints,
        target_distance_m=args.target,
        min_target_distance_m=min_distance,
        max_target_distance_m=max_distance,
        campus_id=args.campus,
        unid=unid,
        area_id=area_id,
        library=library,
        planner=planner,
        modal=False,
        on_close=root.destroy,
    )

    if args.smoke_test:
        root.after(800, editor.cancel)
        root.mainloop()
        print("SMOKE_OK")
        return 0

    root.mainloop()
    if editor.result:
        entry = editor.result.get("entry")
        expanded = editor.result.get(
            "expanded_polyline"
        ) or editor.result["polyline"]
        print(
            "已保存路线: 模板 %.1f m，展开 %.1f m，route_id=%s"
            % (
                rl.polyline_length(editor.result["polyline"]),
                rl.polyline_length(expanded),
                getattr(entry, "route_id", "")[:8],
            )
        )
        return 0
    print("未保存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
