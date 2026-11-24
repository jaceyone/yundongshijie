# -*- coding: utf-8 -*-
"""
AMap 瓦片图层（官方 App 使用的地图）

- 瓦片来源：高德地图（标准 wprd/style=7，卫星 webst/style=6），与官方 App 的地图一致。
- 坐标系：GCJ-02；官方 5 点、路线、围栏都使用 GCJ，直接对齐。
- 不需要 key / SDK；瓦片按需下载并缓存到 `电脑一键启动/cache/amap/`。
- 只负责背景底图；建筑/边界校验仍用打包校园数据。
- Pillow 缺失时禁用本图层，编辑器回退到本地矢量底图。

层级事实（2026-11-18 实测）：
- 标准底图旧源 `webrd/style=8` 只到 z18，z19 返回 179 字节纯色占位 PNG；
  现改用 `wprd/style=7`，z19 仍有真实瓦片，z20 才返回空白。
- 卫星 `webst/style=6` 实际只到 z18；z19 返回「此区域无卫星图」灰色占位图。
  超过 z18 时用 z18 瓦片放大绘制（overzoom），不再请求不存在的层级。
"""

from __future__ import annotations

import math
import queue
import threading
import time
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from PIL import Image, ImageTk
except Exception:  # pragma: no cover - 仅在未安装 Pillow 时命中
    Image = None
    ImageTk = None

TILE_SIZE = 256
MIN_ZOOM = 3
MAX_ZOOM = 19

# 各底图实际存在的最大层级；超出时 draw() 用该层级瓦片放大，避免整屏空白。
STYLE_MAX_ZOOM = {"road": 19, "satellite": 18}

# 下载失败的瓦片在多少秒后允许重新请求；过期前不重复打服务器。
FAILED_RETRY_SECONDS = 30.0

# 旧 webrd 缓存与 wprd 的标注略有差异，
# 标准底图换一个缓存目录，避免两种样式的瓦片混在一张图上。
CACHE_NAMES = {"road": "road_wprd", "satellite": "satellite"}

ROAD_URL = (
    "https://wprd0{server}.is.autonavi.com/appmaptile"
    "?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scl=1&style=7"
)
SATELLITE_URL = (
    "https://webst0{server}.is.autonavi.com/appmaptile"
    "?style=6&x={x}&y={y}&z={z}"
)


def latlon_to_tile(lat: float, lon: float, zoom: int) -> Tuple[float, float]:
    """GCJ-02 经纬度 → Web Mercator 瓦片浮点坐标。"""
    n = 2 ** int(zoom)
    x = (float(lon) + 180.0) / 360.0 * n
    lat_rad = math.radians(float(lat))
    y = (
        1.0
        - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi
    ) / 2.0 * n
    return x, y


def tile_bounds(zoom: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """返回瓦片的 (north, west, south, east)，均为 GCJ-02 经纬度。"""
    n = 2 ** int(zoom)
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return north, west, south, east


def choose_zoom(scale_px_per_m: float, lat: float) -> int:
    """按当前画布比例选择瓦片层级；256 px 约等于一屏瓦片的自然尺寸。"""
    value = 156543.03392 * math.cos(math.radians(float(lat))) * float(scale_px_per_m)
    if value <= 0:
        return 16
    return max(MIN_ZOOM, min(MAX_ZOOM, int(round(math.log2(value)))))


class AmapTileLayer:
    """异步下载/缓存 AMap 瓦片，并在 Canvas 上绘制当前视野。"""

    def __init__(
        self,
        cache_dir: Path,
        style: str = "road",
        workers: int = 4,
    ):
        self.cache_dir = Path(cache_dir)
        self.style = style if style in ("road", "satellite") else "road"
        self.enabled = Image is not None and ImageTk is not None
        self.last_error = "" if self.enabled else "Pillow 未安装，使用本地矢量底图"
        self.images: Dict[Tuple[str, int, int, int], Any] = {}
        self.photos: Dict[Tuple[str, int, int, int, int, int], Any] = {}
        self.pending = set()
        # key -> 失败时刻（time.monotonic）；超过 FAILED_RETRY_SECONDS 允许重试。
        self.failed: Dict[Tuple[str, int, int, int], float] = {}
        self._requests: "queue.Queue[Optional[Tuple[str, int, int, int]]]" = queue.Queue()
        self._results: "queue.Queue[Tuple[Tuple[str, int, int, int], Any, str]]" = queue.Queue()
        self._stop = False
        self._lock = threading.Lock()
        self._workers = []
        if self.enabled:
            for _ in range(max(1, int(workers))):
                thread = threading.Thread(target=self._worker, daemon=True)
                thread.start()
                self._workers.append(thread)

    # ------------------------------------------------------------------ 对外

    def has_any_tiles(self) -> bool:
        return bool(self.images)

    def set_style(self, style: str) -> None:
        if style not in ("road", "satellite") or style == self.style:
            return
        self.style = style
        self.images.clear()
        self.photos.clear()
        with self._lock:
            self.pending.clear()
            self.failed.clear()
        while True:
            try:
                self._requests.get_nowait()
            except queue.Empty:
                break

    def request(self, zoom: int, x: int, y: int) -> None:
        if not self.enabled:
            return
        key = (self.style, int(zoom), int(x), int(y))
        now = time.monotonic()
        with self._lock:
            if key in self.images or key in self.pending:
                return
            failed_at = self.failed.get(key)
            if failed_at is not None and now - failed_at < FAILED_RETRY_SECONDS:
                return
            self.failed.pop(key, None)
            self.pending.add(key)
        self._requests.put(key)

    def poll(self) -> bool:
        """主线程调用；返回是否有新瓦片到达。"""
        changed = False
        while True:
            try:
                key, image, error = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self.pending.discard(key)
                if image is None:
                    self.failed[key] = time.monotonic()
                    if error:
                        self.last_error = error
                else:
                    self.failed.pop(key, None)
                    self.images[key] = image
                    changed = True
        # 有失败瓦片到达重试时间时返回 True，让编辑器触发一次 redraw 去重新请求。
        with self._lock:
            now = time.monotonic()
            for failed_at in self.failed.values():
                if now - failed_at >= FAILED_RETRY_SECONDS:
                    return True
        return changed

    def draw(self, canvas, view, width: int, height: int) -> bool:
        """按当前视野绘制瓦片；未下载的瓦片会排队请求。"""
        if not self.enabled:
            return False
        # 调用方每次 redraw 都先 canvas.delete("all")，旧的 PhotoImage 已无人引用；
        # 在这里清空只保留本帧的图片，避免 Tk 图片被 GC 后瓦片变空白。
        self.photos.clear()
        center_lat, _ = view.screen_to_latlon(width / 2.0, height / 2.0)
        # 自然层级可能高于该底图的真实层级：用最大可用层级放大绘制（overzoom）。
        zoom = min(choose_zoom(view.scale, center_lat), STYLE_MAX_ZOOM.get(self.style, MAX_ZOOM))
        lat_top, lon_left = view.screen_to_latlon(0, 0)
        lat_bottom, lon_right = view.screen_to_latlon(width, height)
        x_min_f, y_min_f = latlon_to_tile(lat_top, lon_left, zoom)
        x_max_f, y_max_f = latlon_to_tile(lat_bottom, lon_right, zoom)
        x_min, x_max = int(math.floor(x_min_f)), int(math.floor(x_max_f))
        y_min, y_max = int(math.floor(y_min_f)), int(math.floor(y_max_f))
        n = 2 ** zoom
        drawn = False

        for x in range(x_min, x_max + 1):
            if x < 0 or x >= n:
                continue
            for y in range(y_min, y_max + 1):
                if y < 0 or y >= n:
                    continue
                key = (self.style, zoom, x, y)
                if key not in self.images:
                    self.request(zoom, x, y)
                    continue
                north, west, south, east = tile_bounds(zoom, x, y)
                sx0, sy0 = view.latlon_to_screen(north, west)
                sx1, sy1 = view.latlon_to_screen(south, east)
                tile_width = int(math.ceil(sx1 - sx0)) + 1
                tile_height = int(math.ceil(sy1 - sy0)) + 1
                if (
                    tile_width <= 0
                    or tile_height <= 0
                    or tile_width > 2048
                    or tile_height > 2048
                ):
                    continue
                photo = self._photo(self.style, zoom, x, y, tile_width, tile_height)
                if photo is None:
                    continue
                canvas.create_image(
                    sx0, sy0, anchor="nw", image=photo, tags=("amap",)
                )
                drawn = True
        return drawn

    def shutdown(self) -> None:
        self._stop = True
        for _ in self._workers:
            try:
                self._requests.put_nowait(None)
            except Exception:
                pass

    # ------------------------------------------------------------------ 内部

    def _worker(self) -> None:
        while not self._stop:
            try:
                key = self._requests.get(timeout=0.5)
            except queue.Empty:
                continue
            if key is None:
                break
            style, zoom, x, y = key
            image = None
            error = ""
            try:
                image = self._load(style, zoom, x, y)
            except Exception as exc:
                error = "%s: %s" % (type(exc).__name__, exc)
            self._results.put((key, image, error))

    def _load(self, style: str, zoom: int, x: int, y: int):
        folder = CACHE_NAMES.get(style, style)
        path = self.cache_dir / folder / str(zoom) / str(x) / ("%d.png" % y)
        if path.is_file():
            try:
                return self._decode(path.read_bytes())
            except Exception:
                # 缓存损坏/被截断：删掉重下，避免这块永远失败。
                try:
                    path.unlink()
                except OSError:
                    pass
        data = self._download(style, zoom, x, y)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_bytes(data)
        temp.replace(path)  # 原子替换，避免进程中断留下半截文件
        return self._decode(data)

    def _download(self, style: str, zoom: int, x: int, y: int) -> bytes:
        server = 1 + ((x + y) % 4)
        template = ROAD_URL if style == "road" else SATELLITE_URL
        url = template.format(server=server, x=x, y=y, z=zoom)
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()

    @staticmethod
    def _decode(data: bytes):
        # 不再按字节数判断“空瓦片”：合法但稀疏的道路瓦片可能只有 ~900 字节，
        # 被误杀后会在视野里留下一个方块空洞。只校验能否解码成正常尺寸图片。
        image = Image.open(BytesIO(data))
        image.load()
        if image.width < 64 or image.height < 64:
            raise ValueError(
                "invalid tile: %dx%d px" % (image.width, image.height)
            )
        if image.mode == "P" and "transparency" in image.info:
            image = image.convert("RGBA")
        return image.convert("RGB")

    def _photo(
        self, style: str, zoom: int, x: int, y: int, width: int, height: int
    ):
        key = (style, zoom, x, y, width, height)
        photo = self.photos.get(key)
        if photo is not None:
            return photo
        image = self.images.get((style, zoom, x, y))
        if image is None:
            return None
        try:
            resized = image.resize(
                (max(1, width), max(1, height)), Image.BILINEAR
            )
        except Exception:
            return None
        photo = ImageTk.PhotoImage(resized)
        # 图片只在本帧有效；draw() 每帧开头会清空，避免引用被 GC 后瓦片变空白。
        self.photos[key] = photo
        return photo
