# -*- coding: utf-8 -*-
"""
校园跑 · 取点锚点编辑器（Tkinter Canvas）

用途
  - 在校园地图上画 1–5 个锚点（推荐 5 个），保存到 ``anchor_points.json``；
  - 每次正式运行随机选 1 个锚点，作为官方 5 点接口的取点位置；
  - 底图复用 ``route_editor`` 的高德瓦片/本地矢量与投影。

独立运行
  python anchor_editor.py
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, simpledialog
from typing import Any, Dict, List, Optional, Tuple

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import anchor_points as ap  # noqa: E402
import amap_tiles  # noqa: E402
import route_editor  # noqa: E402

BG = route_editor.BG
CARD = route_editor.CARD
BORDER = route_editor.BORDER
TEXT = route_editor.TEXT
MUTED = route_editor.MUTED
BLUE = route_editor.BLUE
GREEN = route_editor.GREEN
GREEN_DARK = route_editor.GREEN_DARK
ORANGE = route_editor.ORANGE
MAP_BG = route_editor.MAP_BG
BUILDING_FILL = route_editor.BUILDING_FILL
BUILDING_LINE = route_editor.BUILDING_LINE
ROAD_LINE = route_editor.ROAD_LINE
BOUNDARY_LINE = route_editor.BOUNDARY_LINE

MARKER_RADIUS = 10
DRAG_RADIUS_PX = 14


class AnchorEditor(tk.Toplevel):
    """地图点击添加锚点；拖动移动；右键删除；保存前做边界/建筑校验。"""

    def __init__(
        self,
        master,
        campus_id: str = "xuchang_280173698",
        unid: Optional[int] = None,
        path=None,
        modal: bool = False,
    ):
        super().__init__(master)
        self.campus_id = str(campus_id or "xuchang_280173698")
        self.unid = unid
        self.path = Path(path) if path else ap.DEFAULT_PATH
        self.campus = route_editor.CampusMap(self.campus_id)
        boundary, buildings = ap.load_campus_geometry(self.campus_id)
        self.boundary = boundary or list(self.campus.boundary)
        self.buildings = buildings or [b["polygon"] for b in self.campus.buildings]

        self.points: List[Dict[str, Any]] = []
        try:
            pool = ap.load_pool(self.path)
        except ap.AnchorPoolError as exc:
            messagebox.showwarning("锚点文件", "读取失败，按空池处理：\n%s" % exc)
            pool = ap.default_payload()
        for index, raw in enumerate(pool.get("points") or []):
            try:
                self.points.append(ap.normalise_point(raw, index))
            except ap.AnchorPoolError:
                continue

        self.view: Optional[route_editor.ViewTransform] = None
        self.tile_layer = amap_tiles.AmapTileLayer(APP_DIR / "cache" / "amap")
        self.satellite_var = tk.BooleanVar(value=False)
        self.snap_road = tk.BooleanVar(value=False)
        self.drag_index: Optional[int] = None
        self.pan_last: Optional[Tuple[float, float]] = None
        self._closed = False
        self.result: Optional[bool] = None
        self._rows: List[tuple] = []

        self.title("校园跑 · 取点锚点")
        self.configure(bg=BG)
        self.minsize(900, 620)
        self._center_window(1020, 730)
        self._build_ui()
        self.view = route_editor.ViewTransform(self.canvas, self.campus.projection)
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
        toolbar.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(
            toolbar,
            text="取点锚点（1–5 个，推荐 5 个）",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(side="left")
        self._button(toolbar, "适配", self.fit_view).pack(side="left", padx=(14, 0))
        self._button(toolbar, "清空", self.clear_points).pack(side="left", padx=(4, 0))
        tk.Checkbutton(
            toolbar,
            text="吸附到道路",
            variable=self.snap_road,
            bg=BG,
            fg=TEXT,
            activebackground=BG,
            activeforeground=TEXT,
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
            activeforeground=TEXT,
            selectcolor=CARD,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(8, 0))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        self.canvas = tk.Canvas(
            body, bg=MAP_BG, highlightthickness=1,
            highlightbackground=BORDER, cursor="crosshair")
        self.canvas.bind("<Button-1>", self.on_left_down)
        self.canvas.bind("<B1-Motion>", self.on_left_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_up)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Button-2>", self.on_pan_start)
        self.canvas.bind("<B2-Motion>", self.on_pan_motion)
        self.canvas.bind("<ButtonRelease-2>", self.on_pan_end)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

        sidebar = tk.Frame(body, bg=BG, width=310)
        sidebar.pack(side="right", fill="y", padx=(10, 0))
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.status_var = tk.StringVar(
            value="左键添加 · 拖动移动 · 右键删除 · 滚轮缩放 · 中键平移")
        tk.Label(
            self, textvariable=self.status_var, bg=BG, fg=MUTED,
            anchor="w", font=("Microsoft YaHei UI", 9),
        ).pack(fill="x", padx=16, pady=(0, 8))

    def _button(self, parent, text, command) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, bg=CARD, fg=TEXT,
            activebackground=BORDER, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BORDER,
            padx=10, pady=3, font=("Microsoft YaHei UI", 9), cursor="hand2")

    def _build_sidebar(self, parent) -> None:
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=12, pady=10)

        tk.Label(inner, text="锚点列表", bg=CARD, fg=TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        self.summary_var = tk.StringVar(value="")
        tk.Label(inner, textvariable=self.summary_var, bg=CARD, fg=MUTED,
                 anchor="w", justify="left", wraplength=270,
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(4, 0))

        self.list_frame = tk.Frame(inner, bg=CARD)
        self.list_frame.pack(fill="both", expand=True, pady=(8, 0))

        self.map_status_var = tk.StringVar(
            value=("底图：高德地图（加载中）"
                   if self.tile_layer is not None and self.tile_layer.enabled
                   else "底图：本地矢量（Pillow 未安装）"))
        tk.Label(inner, textvariable=self.map_status_var, bg=CARD, fg=MUTED,
                 anchor="w", justify="left", wraplength=270,
                 font=("Microsoft YaHei UI", 8)).pack(fill="x", pady=(6, 0))
        tk.Label(
            inner,
            text="锚点只决定「从哪取点」，官方返回的 5 点仍由服务端决定。\n"
                 "坐标是 GCJ-02，与高德底图一致。",
            bg=CARD, fg=MUTED, anchor="w", justify="left", wraplength=270,
            font=("Microsoft YaHei UI", 8),
        ).pack(fill="x", pady=(4, 0))

        buttons = tk.Frame(inner, bg=CARD)
        buttons.pack(fill="x", pady=(10, 0))
        tk.Button(
            buttons, text="保存锚点", command=self.save, bg=GREEN, fg="white",
            activebackground=GREEN_DARK, activeforeground="white",
            relief="flat", bd=0, padx=12, pady=5,
            font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2",
        ).pack(side="left")
        tk.Button(
            buttons, text="取消", command=self.cancel, bg=CARD, fg=TEXT,
            activebackground=BORDER, relief="flat", bd=0, padx=12, pady=5,
            font=("Microsoft YaHei UI", 10), cursor="hand2",
        ).pack(side="right")

    # ------------------------------------------------------------------ 视图

    def _initial_fit(self) -> None:
        self.canvas.update_idletasks()
        if self.points:
            self.view.fit_points(
                [(point["glat"], point["glon"]) for point in self.points],
                pixels_padding=80)
        elif self.boundary:
            self.view.fit(self.boundary)
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
                "satellite" if self.satellite_var.get() else "road")
        self._update_map_status()
        self.redraw()

    def _polygon_points(self, polygon) -> List[float]:
        result: List[float] = []
        for lat, lon in polygon:
            sx, sy = self.view.latlon_to_screen(lat, lon)
            result.extend([sx, sy])
        return result

    def redraw(self) -> None:
        if self.view is None:
            return
        canvas = self.canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())

        show_vector = (
            self.tile_layer is None
            or not self.tile_layer.enabled
            or not self.tile_layer.has_any_tiles())
        if show_vector:
            for polygon in self.buildings:
                points = self._polygon_points(polygon)
                if len(points) >= 6:
                    canvas.create_polygon(
                        points, fill=BUILDING_FILL, outline=BUILDING_LINE,
                        width=1, tags=("building",))
            for road in self.campus.roads:
                points = self._polygon_points(road["polyline"])
                if len(points) >= 4:
                    canvas.create_line(
                        points, fill=ROAD_LINE,
                        width=max(1, min(4, self.view.scale * 3)),
                        tags=("road",))
        if self.tile_layer is not None and self.tile_layer.enabled:
            self.tile_layer.draw(canvas, self.view, width, height)

        if self.boundary:
            points = self._polygon_points(self.boundary)
            points.extend(points[:2])
            canvas.create_line(
                points, fill=BOUNDARY_LINE, width=2, dash=(6, 4),
                tags=("boundary",))

        for index, point in enumerate(self.points):
            sx, sy = self.view.latlon_to_screen(point["glat"], point["glon"])
            radius = MARKER_RADIUS + (2 if index == self.drag_index else 0)
            canvas.create_oval(
                sx - radius, sy - radius, sx + radius, sy + radius,
                fill="#fef3c7", outline=ORANGE, width=2, tags=("anchor",))
            canvas.create_text(
                sx, sy, text=("A%d" % (index + 1)), fill=TEXT,
                font=("Microsoft YaHei UI", 9, "bold"), tags=("anchor",))
            canvas.create_text(
                sx + radius + 4, sy - radius,
                text=point["name"], anchor="w", fill=TEXT,
                font=("Microsoft YaHei UI", 8), tags=("anchor",))

        self._draw_scale_bar(width, height)
        if self.tile_layer is not None and self.tile_layer.enabled:
            label = "© 高德地图"
            if self.tile_layer.style == "satellite":
                label += "（卫星）"
        else:
            label = "本地矢量底图"
        canvas.create_text(width - 12, height - 10, text=label,
                           anchor="se", fill=MUTED,
                           font=("Microsoft YaHei UI", 8))

    def _draw_scale_bar(self, width: int, height: int) -> None:
        meters = 100.0
        pixels = meters * self.view.scale
        if pixels < 40:
            meters = 500.0
            pixels = meters * self.view.scale
        if pixels < 40:
            meters = 1000.0
            pixels = meters * self.view.scale
        x0, y0 = 20, height - 24
        x1 = x0 + pixels
        self.canvas.create_line(x0, y0, x1, y0, fill=TEXT, width=2)
        self.canvas.create_line(x0, y0 - 4, x0, y0 + 4, fill=TEXT, width=2)
        self.canvas.create_line(x1, y0 - 4, x1, y0 + 4, fill=TEXT, width=2)
        self.canvas.create_text((x0 + x1) / 2, y0 - 10, text="%.0f m" % meters,
                                fill=TEXT, font=("Microsoft YaHei UI", 8))

    # ------------------------------------------------------------------ 操作

    def _next_id(self) -> str:
        used = {point["id"] for point in self.points}
        for index in range(1, ap.MAX_ANCHORS + 1):
            candidate = "a%d" % index
            if candidate not in used:
                return candidate
        return "a%d" % (len(self.points) + 1)

    def _snap(self, lat: float, lon: float) -> Tuple[float, float]:
        if not self.snap_road.get():
            return lat, lon
        snapped = self.campus.nearest_road_point(lat, lon)
        return snapped if snapped is not None else (lat, lon)

    def add_point(self, lat: float, lon: float) -> None:
        if len(self.points) >= ap.MAX_ANCHORS:
            messagebox.showinfo(
                "锚点已满", "最多 %d 个锚点；请先删除或移动已有锚点。"
                % ap.MAX_ANCHORS, parent=self)
            return
        lat, lon = self._snap(float(lat), float(lon))
        point = {
            "id": self._next_id(),
            "name": "锚点 %d" % (len(self.points) + 1),
            "glat": lat,
            "glon": lon,
        }
        warning = ""
        for polygon in self.buildings:
            if ap.point_in_polygon(lat, lon, polygon):
                warning = "在建筑内，建议移到道路上"
                break
        self.points.append(point)
        self.refresh()
        self.status_var.set(
            "%s 已添加 (%.6f, %.6f)%s"
            % (point["id"].upper(), lat, lon,
               (" · " + warning) if warning else ""))

    def delete_point(self, index: int) -> None:
        if not (0 <= index < len(self.points)):
            return
        removed = self.points.pop(index)
        self.refresh()
        self.status_var.set("%s 已删除" % removed["id"].upper())

    def rename_point(self, index: int) -> None:
        if not (0 <= index < len(self.points)):
            return
        point = self.points[index]
        value = simpledialog.askstring(
            "锚点名称", "名称（只用于界面与日志）：",
            initialvalue=point["name"], parent=self)
        if value is None:
            return
        value = value.strip()
        if not value:
            messagebox.showwarning("锚点名称", "名称不能为空。", parent=self)
            return
        point["name"] = value
        self.refresh()

    def locate_point(self, index: int) -> None:
        if not (0 <= index < len(self.points)):
            return
        point = self.points[index]
        world_x, world_y = self.campus.projection.latlon_to_world(
            point["glat"], point["glon"])
        self.view.view_x = world_x
        self.view.view_y = world_y
        self.view.scale = max(self.view.scale, 1.0)
        self.redraw()

    def clear_points(self) -> None:
        if not self.points:
            return
        if not messagebox.askyesno(
                "清空锚点", "删除全部 %d 个锚点？" % len(self.points),
                parent=self):
            return
        self.points.clear()
        self.refresh()

    def fit_view(self) -> None:
        if self.points:
            self.view.fit_points(
                [(point["glat"], point["glon"]) for point in self.points],
                pixels_padding=80)
        elif self.boundary:
            self.view.fit(self.boundary)
        self.redraw()

    def refresh(self) -> None:
        self.redraw()
        for child in self.list_frame.winfo_children():
            child.destroy()
        for index, point in enumerate(self.points):
            row = tk.Frame(self.list_frame, bg=CARD, highlightbackground=BORDER,
                           highlightthickness=1)
            row.pack(fill="x", pady=(0, 4))
            tk.Label(
                row, text="A%d  %s" % (index + 1, point["name"]),
                bg=CARD, fg=TEXT, anchor="w",
                font=("Microsoft YaHei UI", 9, "bold"),
            ).pack(fill="x", padx=8, pady=(4, 0))
            tk.Label(
                row, text="%.6f, %.6f" % (point["glat"], point["glon"]),
                bg=CARD, fg=MUTED, anchor="w", font=("Consolas", 8),
            ).pack(fill="x", padx=8)
            actions = tk.Frame(row, bg=CARD)
            actions.pack(fill="x", padx=6, pady=(2, 5))
            for text, command in (
                ("定位", lambda i=index: self.locate_point(i)),
                ("改名", lambda i=index: self.rename_point(i)),
                ("删除", lambda i=index: self.delete_point(i)),
            ):
                tk.Button(
                    actions, text=text, command=command, bg=CARD, fg=BLUE,
                    activebackground=BORDER, relief="flat", bd=0,
                    font=("Microsoft YaHei UI", 8), cursor="hand2",
                ).pack(side="left", padx=(0, 8))
        if not self.points:
            tk.Label(
                self.list_frame,
                text="还没有锚点。\n在地图上点击即可添加（1–5 个）。",
                bg=CARD, fg=MUTED, justify="left",
                font=("Microsoft YaHei UI", 9),
            ).pack(anchor="w", pady=(6, 0))
        self.summary_var.set(
            "已画 %d / %d 个%s"
            % (len(self.points), ap.MAX_ANCHORS,
               "；推荐画满 5 个" if len(self.points) < 5 else ""))

    # ------------------------------------------------------------------ 鼠标

    def _marker_at(self, sx: float, sy: float) -> Optional[int]:
        best = None
        for index, point in enumerate(self.points):
            px, py = self.view.latlon_to_screen(point["glat"], point["glon"])
            gap = ((px - sx) ** 2 + (py - sy) ** 2) ** 0.5
            if gap <= DRAG_RADIUS_PX and (best is None or gap < best[0]):
                best = (gap, index)
        return None if best is None else best[1]

    def on_left_down(self, event) -> None:
        index = self._marker_at(event.x, event.y)
        if index is not None:
            self.drag_index = index
            self.redraw()
            return
        lat, lon = self.view.screen_to_latlon(event.x, event.y)
        self.add_point(lat, lon)

    def on_left_motion(self, event) -> None:
        if self.drag_index is None:
            return
        lat, lon = self.view.screen_to_latlon(event.x, event.y)
        lat, lon = self._snap(lat, lon)
        point = self.points[self.drag_index]
        point["glat"], point["glon"] = lat, lon
        self.refresh()

    def on_left_up(self, _event) -> None:
        if self.drag_index is not None:
            self.drag_index = None
            self.refresh()

    def on_right_click(self, event) -> None:
        index = self._marker_at(event.x, event.y)
        if index is not None:
            self.delete_point(index)

    def on_mouse_wheel(self, event) -> None:
        factor = 1.2 if event.delta > 0 else 1 / 1.2
        self.view.zoom(factor, event.x, event.y)
        self.redraw()

    def on_pan_start(self, event) -> None:
        self.pan_last = (event.x, event.y)

    def on_pan_motion(self, event) -> None:
        if self.pan_last is None:
            return
        dx = event.x - self.pan_last[0]
        dy = event.y - self.pan_last[1]
        self.view.pan(dx, dy)
        self.pan_last = (event.x, event.y)
        self.redraw()

    def on_pan_end(self, _event) -> None:
        self.pan_last = None

    # ------------------------------------------------------------------ 保存

    def save(self) -> None:
        if not self.points:
            messagebox.showwarning(
                "不能保存", "至少画 1 个锚点；0 个视为未配置。", parent=self)
            return
        try:
            warnings = ap.save_pool(
                self.points,
                unid=self.unid,
                campus=self.campus_id,
                path=self.path,
                boundary=self.boundary,
                buildings=self.buildings,
            )
        except ap.AnchorPoolError as exc:
            messagebox.showerror("不能保存", str(exc), parent=self)
            return
        if warnings:
            messagebox.showwarning(
                "已保存（有提示）",
                "锚点已保存，下次运行生效。\n\n" + "\n".join(warnings[:8]),
                parent=self)
        else:
            messagebox.showinfo(
                "已保存",
                "锚点已保存，下次运行生效。\n\n共 %d 个锚点。"
                % len(self.points),
                parent=self)
        self.result = True
        self._finish()

    def cancel(self) -> None:
        self.result = None
        self._finish()

    def _finish(self) -> None:
        self._closed = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def show_modal(self) -> Optional[bool]:
        self.wait_window(self)
        return self.result


def main() -> int:
    root = tk.Tk()
    root.withdraw()
    editor = AnchorEditor(
        root,
        campus_id="xuchang_280173698",
        path=ap.DEFAULT_PATH,
        modal=False,
    )
    editor.wait_window(editor)
    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
