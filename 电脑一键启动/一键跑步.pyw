# -*- coding: utf-8 -*-
"""
校园跑 · 电脑一键启动 GUI

双击本文件（或桌面快捷方式）打开。
使用 pythonw 运行时没有黑色命令行窗口。
"""

import os
import queue
import sys
import threading
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import anchor_points  # noqa: E402
import runner  # noqa: E402

# Windows 高 DPI，避免界面发虚
try:
    from ctypes import windll

    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# --------------------------------------------------------------------------- 外观

BG = "#f3f4f6"
CARD = "#ffffff"
BORDER = "#e5e7eb"
TEXT = "#111827"
MUTED = "#6b7280"
GREEN = "#16a34a"
GREEN_DARK = "#15803d"
BLUE = "#2563eb"
RED = "#dc2626"

# --------------------------------------------------------------------------- 线程 -> UI 队列

ui_queue = queue.Queue()

_ORIG_STDOUT = sys.stdout
_ORIG_STDERR = sys.stderr


class QueueWriter:
    """把协议里零散的 print 收进日志框；同时保留控制台输出方便排错。"""

    def __init__(self, original=None):
        self.original = original

    def write(self, text):
        if text:
            ui_queue.put(("log", str(text).rstrip("\n")))
        if self.original is not None:
            try:
                self.original.write(text)
            except Exception:
                pass

    def flush(self):
        if self.original is not None:
            try:
                self.original.flush()
            except Exception:
                pass


sys.stdout = QueueWriter(_ORIG_STDOUT)
sys.stderr = QueueWriter(_ORIG_STDERR)

# --------------------------------------------------------------------------- 窗口

root = tk.Tk()
root.title("校园跑 · 一键启动")
root.configure(bg=BG)
root.minsize(780, 700)

width, height = 880, 840
screen_w = root.winfo_screenwidth()
screen_h = root.winfo_screenheight()
root.geometry(
    "%dx%d+%d+%d" % (width, height, (screen_w - width) // 2, (screen_h - height) // 2)
)

try:
    default_font = tkfont.nametofont("TkDefaultFont")
    default_font.configure(family="Microsoft YaHei UI", size=10)
except Exception:
    pass

title_font = ("Microsoft YaHei UI", 18, "bold")
button_font = ("Microsoft YaHei UI", 16, "bold")
body_font = ("Microsoft YaHei UI", 10)
small_font = ("Microsoft YaHei UI", 9)
mono_font = ("Consolas", 9)
result_font = ("Microsoft YaHei UI", 11, "bold")

icon_path = os.path.join(APP_DIR, "assets", "app.ico")
if os.path.isfile(icon_path):
    try:
        root.iconbitmap(default=icon_path)
    except Exception:
        pass

# --------------------------------------------------------------------------- 状态

running = False
config = None
checks_ok = False

route_mode_var = tk.StringVar(value="osm")
shutdown_event = threading.Event()

# --------------------------------------------------------------------------- 控件


def card(parent):
    frame = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    return frame


header = tk.Frame(root, bg=BG)
header.pack(fill="x", padx=22, pady=(18, 6))
tk.Label(header, text="校园跑 · 一键启动", bg=BG, fg=TEXT, font=title_font).pack(
    anchor="w"
)
tk.Label(
    header,
    text="自动登录 · 自动极验 · 一键提交",
    bg=BG,
    fg=MUTED,
    font=body_font,
).pack(anchor="w", pady=(2, 0))

status_card = card(root)
status_card.pack(fill="x", padx=22, pady=8)
status_inner = tk.Frame(status_card, bg=CARD)
status_inner.pack(fill="x", padx=14, pady=11)

status_dot = tk.Label(status_inner, text="●", bg=CARD, fg=MUTED, font=("Segoe UI", 15))
status_dot.pack(side="left")

status_var = tk.StringVar(value="就绪")
tk.Label(
    status_inner,
    textvariable=status_var,
    bg=CARD,
    fg=TEXT,
    font=("Microsoft YaHei UI", 12, "bold"),
).pack(side="left", padx=(6, 14))

env_var = tk.StringVar(value="环境检查中…")
tk.Label(status_inner, textvariable=env_var, bg=CARD, fg=MUTED, font=small_font).pack(
    side="left"
)

start_row = tk.Frame(root, bg=BG)
start_row.pack(fill="x", padx=22, pady=(6, 4))

start_button = tk.Button(
    start_row,
    text="开 始 跑 步",
    command=lambda: on_start(),
    bg=GREEN,
    fg="white",
    activebackground=GREEN_DARK,
    activeforeground="white",
    disabledforeground="#d1fae5",
    relief="flat",
    bd=0,
    highlightthickness=0,
    font=button_font,
    cursor="hand2",
    pady=10,
)
start_button.pack(side="left", fill="x", expand=True)

route_row = tk.Frame(root, bg=BG)
route_row.pack(fill="x", padx=22, pady=(2, 0))
tk.Label(route_row, text="路线来源", bg=BG, fg=MUTED, font=small_font).pack(side="left")
tk.Radiobutton(
    route_row,
    text="OSM 自动",
    variable=route_mode_var,
    value="osm",
    command=lambda: update_target(config),
    bg=BG,
    fg=TEXT,
    activebackground=BG,
    activeforeground=TEXT,
    selectcolor=CARD,
    font=body_font,
    cursor="hand2",
).pack(side="left", padx=(8, 0))
tk.Radiobutton(
    route_row,
    text="手动路线库",
    variable=route_mode_var,
    value="manual",
    command=lambda: update_target(config),
    bg=BG,
    fg=TEXT,
    activebackground=BG,
    activeforeground=TEXT,
    selectcolor=CARD,
    font=body_font,
    cursor="hand2",
).pack(side="left", padx=(4, 0))
tk.Label(
    route_row,
    text="（手动模式：开始跑步后取当天点位再画）",
    bg=BG,
    fg=MUTED,
    font=("Microsoft YaHei UI", 8),
).pack(side="left", padx=(8, 0))
ttk.Button(route_row, text="管理路线库", command=lambda: on_manage_library()).pack(
    side="right"
)

anchor_row = tk.Frame(root, bg=BG)
anchor_row.pack(fill="x", padx=22, pady=(2, 0))
anchor_var = tk.StringVar(value="取点位置：未配置（使用围栏顶点）")
tk.Label(anchor_row, textvariable=anchor_var, bg=BG, fg=TEXT, font=small_font).pack(
    side="left", padx=(8, 0)
)
anchor_button = ttk.Button(
    anchor_row, text="设置取点锚点", command=lambda: on_edit_anchors()
)
anchor_button.pack(side="right")

options = tk.Frame(root, bg=BG)
options.pack(fill="x", padx=22)

dry_var = tk.BooleanVar(value=False)
tk.Checkbutton(
    options,
    text="演练模式（只登录，不提交成绩）",
    variable=dry_var,
    bg=BG,
    fg=TEXT,
    activebackground=BG,
    activeforeground=TEXT,
    selectcolor=CARD,
    font=body_font,
    cursor="hand2",
).pack(side="left")

target_var = tk.StringVar(value="")
tk.Label(options, textvariable=target_var, bg=BG, fg=MUTED, font=small_font).pack(
    side="right"
)

progress = ttk.Progressbar(root, mode="indeterminate")
progress.pack(fill="x", padx=22, pady=(8, 4))

log_header = tk.Frame(root, bg=BG)
log_header.pack(fill="x", padx=22, pady=(8, 4))
tk.Label(log_header, text="运行日志", bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(
    side="left"
)
log_path_var = tk.StringVar(value="")
tk.Label(log_header, textvariable=log_path_var, bg=BG, fg=MUTED, font=("Consolas", 8)).pack(
    side="right"
)

log_frame = card(root)
log_frame.pack(fill="both", expand=True, padx=22, pady=(0, 8))

log_text = tk.Text(
    log_frame,
    bg="#0f172a",
    fg="#e2e8f0",
    insertbackground="#e2e8f0",
    font=mono_font,
    relief="flat",
    bd=0,
    wrap="word",
    height=8,
    state="disabled",
    padx=10,
    pady=8,
)
log_scroll = ttk.Scrollbar(log_frame, command=log_text.yview)
log_text.configure(yscrollcommand=log_scroll.set)
log_scroll.pack(side="right", fill="y")
log_text.pack(side="left", fill="both", expand=True)

result_card = card(root)
result_card.pack(fill="x", padx=22, pady=(0, 8))
result_inner = tk.Frame(result_card, bg=CARD)
result_inner.pack(fill="x", padx=14, pady=10)

result_title_var = tk.StringVar(value="尚未运行")
result_detail_var = tk.StringVar(
    value="勾选演练模式可以先测试登录与自动极验，不会提交成绩。"
)
tk.Label(
    result_inner,
    textvariable=result_title_var,
    bg=CARD,
    fg=TEXT,
    font=result_font,
).pack(anchor="w")
tk.Label(
    result_inner,
    textvariable=result_detail_var,
    bg=CARD,
    fg=MUTED,
    font=small_font,
    justify="left",
    wraplength=740,
).pack(anchor="w", pady=(2, 0))

footer = tk.Frame(root, bg=BG)
footer.pack(fill="x", padx=22, pady=(0, 14))
ttk.Button(footer, text="环境自检", command=lambda: on_check()).pack(side="left")
ttk.Button(footer, text="重新加载配置", command=lambda: on_reload()).pack(
    side="left", padx=(8, 0)
)
ttk.Button(footer, text="打开配置", command=lambda: on_open_config()).pack(
    side="left", padx=(8, 0)
)
ttk.Button(footer, text="打开日志目录", command=lambda: on_open_logs()).pack(
    side="left", padx=(8, 0)
)
tk.Label(footer, text="v1.3 · 锚点池", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(
    side="right"
)

# --------------------------------------------------------------------------- 辅助


def set_status(text, color=MUTED):
    status_var.set(text)
    status_dot.configure(fg=color)


def append_log(text):
    text = str(text).rstrip()
    if not text:
        return
    log_text.configure(state="normal")
    log_text.insert("end", text + "\n")
    log_text.see("end")
    log_text.configure(state="disabled")


def update_target(cfg):
    if not cfg:
        target_var.set("")
        return
    minutes, seconds = divmod(int(cfg["time_sec"]), 60)
    mode = "计分跑" if int(cfg["mode"]) == 1 else "自由跑"
    if route_mode_var.get() == "manual":
        route = "手动路线库"
    elif int(cfg["mode"]) == 1:
        route = "OSM 避建筑"
    else:
        route = "内部合成"
    target_var.set(
        "目标：%s m / %d:%02d · %s · %s"
        % (cfg["distance"], minutes, seconds, mode, route)
    )


def beep(ok):
    try:
        import winsound

        winsound.MessageBeep(
            winsound.MB_ICONASTERISK if ok else winsound.MB_ICONHAND
        )
    except Exception:
        pass


def show_checks(checks):
    lines = []
    for check in checks:
        mark = "OK  " if check.ok else "FAIL"
        lines.append("[%s] %s: %s" % (mark, check.name, check.detail))
    text = "\n".join(lines)
    if all(check.ok for check in checks):
        messagebox.showinfo("环境自检", "全部通过\n\n" + text)
    else:
        messagebox.showwarning("环境自检", "有项目未通过\n\n" + text)


# --------------------------------------------------------------------------- 路线 provider


def _context_value(context, name, default=None):
    if isinstance(context, dict):
        return context.get(name, default)
    return getattr(context, name, default)


def _protocol_home_on_path():
    protocol = str(runner.PROTOCOL_HOME)
    if protocol not in sys.path:
        sys.path.insert(0, protocol)


def _route_decision(mode):
    """构造协议侧的 RouteDecision；只在 provider 线程等待时使用。"""
    _protocol_home_on_path()
    from running.models import RouteDecision

    if mode == "osm":
        return RouteDecision.osm()
    if mode == "cancel":
        return RouteDecision.cancel()
    raise ValueError("未知路线决策: %s" % mode)


def _anchor_label(context) -> str:
    ident = _context_value(context, "anchor_id", None)
    name = _context_value(context, "anchor_name", None)
    coords = _context_value(context, "anchor_gcj", None)
    if not coords:
        return "围栏顶点（未配置锚点）"
    label = ("%s %s" % (ident or "", name or "")).strip() or "自定义锚点"
    try:
        return "%s（%.6f, %.6f）" % (label, float(coords[0]), float(coords[1]))
    except (TypeError, ValueError, IndexError):
        return label


def anchor_meta_from_context(context):
    coords = _context_value(context, "anchor_gcj", None)
    if not coords:
        return None
    try:
        lat, lon = float(coords[0]), float(coords[1])
    except (TypeError, ValueError, IndexError):
        return None
    return {
        "id": _context_value(context, "anchor_id", None) or "",
        "name": _context_value(context, "anchor_name", None) or "",
        "glat": lat,
        "glon": lon,
    }


def _point_names(points):
    names = []
    for index, point in enumerate(points):
        if isinstance(point, dict):
            name = point.get("name") or point.get("pointName") or ""
        else:
            name = ""
        names.append(name or "P%d" % (index + 1))
    return "、".join(names)


def ask_route_choice(title, message):
    """路线库未命中时的三选一：手动画线 / OSM 自动规划 / 取消。

    Tk messagebox 不支持三个自定义按钮，这里用一个小 Toplevel；必须在主线程调用。
    """
    dialog = tk.Toplevel(root)
    dialog.title(title)
    dialog.configure(bg=CARD)
    dialog.transient(root)
    dialog.resizable(False, False)
    dialog.grab_set()
    choice = {"value": "cancel"}

    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    dialog.geometry("+%d+%d" % ((screen_w - 520) // 2, (screen_h - 300) // 2))

    tk.Label(
        dialog,
        text=message,
        bg=CARD,
        fg=TEXT,
        justify="left",
        anchor="w",
        wraplength=470,
        font=("Microsoft YaHei UI", 10),
    ).pack(fill="x", padx=18, pady=(16, 12))

    row = tk.Frame(dialog, bg=CARD)
    row.pack(fill="x", padx=16, pady=(0, 14))

    def pick(value):
        choice["value"] = value
        dialog.destroy()

    tk.Button(
        row, text="手动画线", command=lambda: pick("manual"),
        bg=GREEN, fg="white", activebackground=GREEN_DARK,
        activeforeground="white", relief="flat", bd=0, padx=14, pady=6,
        font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2",
    ).pack(side="left")
    tk.Button(
        row, text="OSM 自动规划", command=lambda: pick("osm"),
        bg=BLUE, fg="white", activebackground="#1d4ed8",
        activeforeground="white", relief="flat", bd=0, padx=14, pady=6,
        font=("Microsoft YaHei UI", 10), cursor="hand2",
    ).pack(side="left", padx=(8, 0))
    tk.Button(
        row, text="取消", command=lambda: pick("cancel"),
        bg=CARD, fg=TEXT, activebackground=BORDER, relief="flat", bd=0,
        padx=14, pady=6, font=("Microsoft YaHei UI", 10), cursor="hand2",
    ).pack(side="right")

    dialog.protocol("WM_DELETE_WINDOW", lambda: pick("cancel"))
    root.wait_window(dialog)
    return choice["value"]


def refresh_anchor_row():
    try:
        summary = anchor_points.describe_pool()
    except Exception as exc:
        anchor_var.set("取点位置：读取失败：%s" % exc)
        anchor_button.configure(text="设置取点锚点")
        return {"configured": False}
    anchor_var.set("取点位置：%s" % summary["text"])
    anchor_button.configure(
        text="编辑锚点" if summary["configured"] else "设置取点锚点")
    return summary


def make_route_provider():
    """把 worker 线程的路线请求转给主线程，等待结果。"""

    def provider(context):
        event = threading.Event()
        box = {}
        ui_queue.put(("route_request", (context, box, event)))
        while not event.is_set():
            if shutdown_event.is_set():
                return None
            event.wait(0.2)
        return box.get("decision")

    return provider


def handle_route_request(context, box, event):
    try:
        import route_editor
        import route_library as route_lib

        points = (
            _context_value(context, "raw_checkpoints")
            or _context_value(context, "checkpoints")
            or _context_value(context, "points")
            or []
        )
        target = int(
            _context_value(
                context,
                "target_distance_m",
                (config or {}).get("distance", 3000),
            )
        )
        unid = _context_value(context, "unid", None)
        area_id = int(_context_value(context, "area_id", -1) or -1)
        campus = _context_value(context, "campus", None) or (
            config or {}
        ).get("campus", "xuchang_280173698")

        rule = _context_value(context, "rule", None)

        def _rule_int(names, default):
            for name in names:
                value = None
                if isinstance(rule, dict):
                    value = rule.get(name)
                elif rule is not None:
                    value = getattr(rule, name, None)
                if value not in (None, ""):
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        continue
            return default

        min_distance = _rule_int(("min_distance", "minDistance"), 1000)
        # 目标距离允许超过 day_goal / 3000 m；编辑器只保留下限和本次目标。
        max_distance = 0

        if min_distance and target < min_distance:
            append_log(
                "路线选择: 目标距离 %d m 低于学校最低 %d m；已拒绝"
                % (target, min_distance)
            )
            return

        if not points:
            append_log("路线选择: provider 没有拿到当次官方点位，跳过")
            return

        anchor_meta = anchor_meta_from_context(context)
        try:
            key = route_lib.points_key(points, unid, area_id)
        except ValueError:
            key = ""
        append_log(
            "官方点位: 点数=%d key=%s 点=%s"
            % (len(points), key[:10] or "-", _point_names(points))
        )
        append_log(
            "路线选择: 已取得当天官方点位 %d 个，目标 %d m"
            % (len(points), target)
        )

        library = route_lib.RouteLibrary()
        planner = route_lib.load_planner(campus)
        # 宽松命中：只看当次 5 点与库中 5 点的位置，不再按状态/长度/趟数/复核过滤。
        matches = library.find_by_points(points, unid, area_id, target)
        if matches:
            best = matches[0]
            template_length = float(best.entry.length_m)
            expanded_template = route_lib.expand_polyline_to_distance(
                best.adjusted_polyline, target
            )
            truncated = template_length > target + 0.001
            usable = True
            if truncated:
                # 长于目标的路线会被截断；截断后必须仍覆盖全部打卡点。
                try:
                    route_lib.checkpoint_order(expanded_template, points)
                except ValueError:
                    usable = False

            if not usable:
                append_log(
                    "路线库: 命中 %s，但路线 %.1f m 长于目标 %d m，"
                    "截断后缺少打卡点，改为打开编辑器"
                    % (best.entry.route_id[:8], template_length, target)
                )
                messagebox.showwarning(
                    "路线库命中（无法直接使用）",
                    "已按 5 点位置命中路线库：\n\n"
                    "路线长度：%.1f m\n"
                    "目标距离：%d m\n\n"
                    "这条路线比目标长，截断后会缺少部分打卡点，无法直接提交。\n"
                    "将打开编辑器，请重画一条不超过目标的路线。"
                    % (template_length, target),
                    parent=root,
                )
            else:
                status_text = {
                    "valid": "可用",
                    "needs_review": "待复核",
                    "invalid": "不可用",
                }.get(best.entry.status, best.entry.status)
                detail_lines = [
                    "命中路线库（只按 5 点位置匹配）",
                    "",
                    "路线长度：%.1f m" % template_length,
                ]
                if truncated:
                    detail_lines.append(
                        "目标距离：%d m（路线比目标长 %.1f m，将截断）"
                        % (target, template_length - target)
                    )
                else:
                    detail_lines.append(
                        "目标距离：%d m（%.2f 趟，不足 %.1f m 原路折返）"
                        % (target, best.laps, target - template_length)
                    )
                detail_lines.extend(
                    [
                        "点位最大偏差：%.1f m" % best.max_point_distance_m,
                        "状态：%s" % status_text,
                        "上次使用：%s"
                        % (best.entry.last_used_at or "未使用过"),
                    ]
                )
                if best.entry.notes:
                    detail_lines.append("备注：%s" % best.entry.notes)
                if best.entry.status != "valid":
                    detail_lines.append(
                        "注意：这条路线未通过最近一次校验；协议提交前仍会再次校验。"
                    )
                if not truncated and best.laps > route_lib.DEFAULT_MAX_LAPS:
                    detail_lines.append(
                        "注意：约 %.1f 趟，趟数偏多。" % best.laps
                    )
                detail_lines.extend(
                    ["", "使用这条路线吗？选「否」将打开编辑器重画。"]
                )
                if messagebox.askyesno(
                    "路线库命中", "\n".join(detail_lines), parent=root
                ):
                    library.mark_used(best.entry.route_id, target,
                                      anchor=anchor_meta)
                    full_polyline = [
                        [float(lat), float(lon)] for lat, lon in expanded_template
                    ]
                    box["decision"] = (full_polyline, best.ordered_checkpoints)
                    append_log(
                        "路线库: 命中 %s 模板=%.1f m 趟数=%.2f，已沿路线%s到 %d m"
                        % (
                            best.entry.route_id[:8],
                            template_length,
                            best.laps,
                            "截断" if truncated else "展开",
                            target,
                        )
                    )
                    return
        else:
            append_log("路线库: 未命中 key=%s，等待用户选择路线"
                       % (key[:10] or "-"))
            library.record_miss(
                points, unid, area_id, target,
                reason="no_match", anchor=anchor_meta)
            choice = ask_route_choice(
                "官方返回了新的点位",
                "官方返回了一组新的点位\n\n"
                "本次锚点：%s\n"
                "点位：%s\n"
                "points_key：%s…\n\n"
                "这不是错误：官方返回了一组路线库里没有的点位。\n"
                "路线库没有对应路线，请选择："
                % (_anchor_label(context), _point_names(points),
                   key[:10] or "-"),
            )
            if choice == "osm":
                append_log("路线决策: 路线库未命中 → 用户选择 OSM 自动规划")
                box["decision"] = _route_decision("osm")
                return
            if choice == "cancel":
                append_log("路线决策: 路线库未命中 → 用户取消本次提交")
                box["decision"] = _route_decision("cancel")
                return
            append_log("路线决策: 路线库未命中 → 用户选择手动画线")

        editor = route_editor.RouteEditor(
            root,
            checkpoints=points,
            target_distance_m=target,
            min_target_distance_m=min_distance,
            max_target_distance_m=max_distance,
            campus_id=campus,
            unid=unid,
            area_id=area_id,
            library=library,
            planner=planner,
            modal=True,
            anchor=anchor_meta,
        )
        result = editor.show_modal()
        if result:
            expanded = result.get("expanded_polyline")
            if not expanded:
                expanded = [
                    [float(lat), float(lon)]
                    for lat, lon in route_lib.expand_polyline_to_distance(
                        result["polyline"], target
                    )
                ]
            box["decision"] = (expanded, result["ordered_checkpoints"])
            append_log(
                "手动路线: 模板 %.1f m，已沿路线展开到 %d m"
                % (route_lib.polyline_length(result["polyline"]), target)
            )
        else:
            append_log("手动路线: 已取消")
    except Exception as exc:
        append_log("路线选择异常: %s: %s" % (type(exc).__name__, exc))
        try:
            append_log(traceback.format_exc())
        except Exception:
            pass
    finally:
        event.set()


# --------------------------------------------------------------------------- 队列处理


def poll_queue():
    try:
        while True:
            kind, payload = ui_queue.get_nowait()
            if kind == "log":
                append_log(payload)
            elif kind == "checks":
                handle_checks(payload)
            elif kind == "check_dialog":
                show_checks(payload)
            elif kind == "done":
                handle_done(payload)
            elif kind == "fatal":
                handle_fatal(payload)
            elif kind == "route_request":
                handle_route_request(*payload)
    except queue.Empty:
        pass
    root.after(80, poll_queue)


def handle_checks(checks):
    global checks_ok

    critical_names = {
        "Python",
        "协议目录",
        "配置文件",
        "Node.js",
        "Playwright",
        "Chromium",
        "Tkinter",
    }
    critical = [c for c in checks if c.name in critical_names]
    checks_ok = all(c.ok for c in critical)

    if checks_ok:
        env_var.set("环境就绪 ✓")
        if config is not None and not running:
            start_button.configure(state="normal")
    else:
        failed = [c.name for c in critical if not c.ok]
        env_var.set("环境异常：" + "、".join(failed))
        if not running:
            start_button.configure(state="disabled")
        set_status("环境异常", RED)


def handle_done(result):
    global running

    running = False
    progress.stop()
    start_button.configure(state="normal", text="开 始 跑 步")
    log_path_var.set(result.log_path)
    refresh_anchor_row()

    if result.success:
        set_status("完成", GREEN)
        if result.dry_run:
            result_title_var.set("演练完成 · 未提交成绩")
            result_detail_var.set(
                "登录与自动极验正常。取消勾选演练模式后，再点一次即可正式提交。"
            )
        else:
            valid = "达标" if result.is_valid else "未达标"
            result_title_var.set("提交完成 · " + valid)
            result_detail_var.set(
                "rrid=%s · %s m / %s s · 用时 %.1fs"
                % (
                    result.rrid,
                    result.distance,
                    result.time_sec,
                    result.elapsed_sec,
                )
            )
        beep(True)
    else:
        set_status("失败", RED)
        result_title_var.set("运行失败")
        result_detail_var.set(result.message)
        beep(False)


def handle_fatal(payload):
    global running

    if isinstance(payload, tuple) and len(payload) == 2:
        exc, tb_text = payload
    else:
        exc, tb_text = payload, ""

    running = False
    progress.stop()
    start_button.configure(state="normal", text="开 始 跑 步")
    set_status("异常", RED)
    result_title_var.set("程序异常")
    result_detail_var.set(str(exc))
    append_log(tb_text or traceback.format_exc())
    beep(False)
    messagebox.showerror("程序异常", str(exc))


# --------------------------------------------------------------------------- 动作


def on_start():
    global running, config

    if running:
        return

    try:
        config = runner.load_config()
    except runner.ConfigError as exc:
        messagebox.showerror(
            "配置错误",
            "%s\n\n配置文件：\n%s" % (exc, runner.CONFIG_PATH),
        )
        runner.open_path(runner.CONFIG_PATH)
        return

    config["route_mode"] = route_mode_var.get()
    if config["route_mode"] == "manual" and int(config["mode"]) != 1:
        messagebox.showerror(
            "路线来源", "手动路线库只支持计分跑（mode=1）。", parent=root
        )
        return

    # 先用最近一次取点报告做快速下限检查；允许超过 day_goal / 3000 m。
    try:
        import route_editor

        report = route_editor._latest_sampler_report()
        if report is not None:
            min_distance, _max_distance = route_editor._load_report_rule_limits(
                report
            )
            distance = int(config["distance"])
            if min_distance and distance < min_distance:
                messagebox.showerror(
                    "距离超限",
                    "目标距离 %d m 低于学校最低 %d m；请修改 config.json。"
                    % (distance, min_distance),
                    parent=root,
                )
                return
    except Exception:
        pass

    update_target(config)
    dry_run = bool(dry_var.get())

    # 取点锚点：登录前选好，确认框里可见；点「否」不登录、不取点。
    selected_anchor = None
    if int(config["mode"]) == 1 and not dry_run:
        try:
            selected_anchor = anchor_points.select_anchor()
        except Exception as exc:
            messagebox.showerror(
                "取点锚点", "读取锚点失败：%s" % exc, parent=root)
            return
        if selected_anchor is None:
            append_log("[警告] 未配置有效锚点，回退围栏顶点")
        else:
            config["anchor_gcj"] = (selected_anchor.glat, selected_anchor.glon)
            config["anchor_id"] = selected_anchor.id
            config["anchor_name"] = selected_anchor.name
            append_log(
                "本次取点锚点: %s (%.6f, %.6f) 来源=锚点池 seed=%s"
                % (selected_anchor.label, selected_anchor.glat,
                   selected_anchor.glon, selected_anchor.seed))
            anchor_var.set("取点位置：随机（A1–An）· 本次 %s"
                           % selected_anchor.label)

    if not dry_run and config.get("confirm_before_submit", True):
        minutes, seconds = divmod(int(config["time_sec"]), 60)
        mode = "计分跑" if int(config["mode"]) == 1 else "自由跑"
        if config["route_mode"] == "manual":
            route = "手动路线库"
        elif int(config["mode"]) == 1:
            route = "OSM 避建筑"
        else:
            route = "内部合成"
        anchor_line = ""
        if int(config["mode"]) == 1:
            if selected_anchor is not None:
                anchor_line = "\n本次取点位置：%s（%.6f, %.6f）" % (
                    selected_anchor.label,
                    selected_anchor.glat,
                    selected_anchor.glon,
                )
            else:
                anchor_line = "\n本次取点位置：围栏顶点（未配置锚点）"
        extra = ""
        if config["route_mode"] == "manual":
            extra = (
                "\n手动模式：点「是」后先登录并取当天官方点位，"
                "再打开画线器；保存后自动继续提交。"
            )
        if not messagebox.askyesno(
            "确认提交",
            "将提交真实成绩：\n\n%s m / %d:%02d · %s · %s%s%s\n\n继续吗？"
            % (config["distance"], minutes, seconds, mode, route,
               anchor_line, extra),
            parent=root,
        ):
            return

    running = True
    start_button.configure(state="disabled", text="运 行 中…")
    set_status("运行中…", BLUE)
    progress.start(12)
    log_path_var.set("")
    log_text.configure(state="normal")
    log_text.delete("1.0", "end")
    log_text.configure(state="disabled")
    result_title_var.set("正在运行")
    if config.get("route_mode") == "manual" and not dry_run:
        result_detail_var.set(
            "登录 → 自动极验 → 取当天点位 → 选路线 → 提交，请稍候…"
        )
    else:
        result_detail_var.set("登录 → 自动极验 → 提交，请稍候…")
    if dry_run and config.get("route_mode") == "manual":
        append_log("演练模式只登录，不取点、不画线；手动路线只在正式提交时使用当天点位。")

    def work():
        try:
            provider = (
                make_route_provider()
                if config.get("route_mode") == "manual"
                else None
            )
            result = runner.run(
                config,
                dry_run=dry_run,
                progress_callback=lambda message: ui_queue.put(("log", message)),
                route_provider=provider,
            )
            ui_queue.put(("done", result))
        except Exception as exc:
            ui_queue.put(("fatal", (exc, traceback.format_exc())))

    threading.Thread(target=work, daemon=True).start()


def run_preflight_async(cfg, show_dialog=False):
    def work():
        try:
            checks = runner.preflight(cfg, deep=False)
            ui_queue.put(("checks", checks))
            if show_dialog:
                ui_queue.put(("check_dialog", checks))
        except Exception as exc:
            ui_queue.put(("fatal", (exc, traceback.format_exc())))

    threading.Thread(target=work, daemon=True).start()


def on_check():
    if running:
        return
    set_status("环境检查中…", BLUE)
    env_var.set("环境检查中…")
    start_button.configure(state="disabled")
    run_preflight_async(config, show_dialog=True)


def on_reload():
    global config

    try:
        config = runner.load_config()
    except runner.ConfigError as exc:
        config = None
        if not running:
            start_button.configure(state="disabled")
        set_status("未配置", RED)
        env_var.set("请先填写 config.json")
        result_title_var.set("需要配置")
        result_detail_var.set(str(exc))
        messagebox.showwarning("配置", str(exc))
        return

    route_mode_var.set(config.get("route_mode", "osm"))
    update_target(config)
    dry_var.set(bool(config.get("dry_run_default", False)))
    refresh_anchor_row()
    set_status("就绪", GREEN)
    env_var.set("环境检查中…")
    if not running:
        start_button.configure(state="normal")

    run_preflight_async(config)


def on_open_config():
    try:
        runner.ensure_config()
    except Exception as exc:
        messagebox.showerror("配置", str(exc))
        return
    runner.open_path(runner.CONFIG_PATH)


def on_open_logs():
    try:
        runner.LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    runner.open_path(runner.LOG_DIR)


def on_manage_library():
    if running:
        messagebox.showinfo("路线库", "正在运行，结束后再管理路线库。", parent=root)
        return

    import route_editor
    import route_library_window

    target = 3000
    campus = "xuchang_280173698"
    if config:
        try:
            target = int(config.get("distance", 3000))
        except (TypeError, ValueError):
            target = 3000
        campus = str(config.get("campus") or campus)

    min_distance = 0
    try:
        report = route_editor._latest_sampler_report()
        if report is not None:
            min_distance, _day_goal = route_editor._load_report_rule_limits(report)
    except Exception:
        min_distance = 0

    try:
        window = route_library_window.RouteLibraryWindow(
            root,
            target_distance_m=target,
            min_target_distance_m=min_distance,
            campus_id=campus,
        )
        window.show_modal()
    except Exception as exc:
        messagebox.showerror("路线库", "无法打开路线库：%s" % exc, parent=root)


def on_edit_anchors():
    if running:
        messagebox.showinfo("取点锚点", "正在运行，结束后再编辑锚点。", parent=root)
        return
    try:
        import anchor_editor

        campus = "xuchang_280173698"
        if config:
            campus = str(config.get("campus") or campus)
        editor = anchor_editor.AnchorEditor(
            root,
            campus_id=campus,
            unid=None,
            modal=True,
        )
        editor.show_modal()
    except Exception as exc:
        messagebox.showerror("取点锚点", "无法打开锚点编辑器：%s" % exc, parent=root)
    finally:
        refresh_anchor_row()


def on_close():
    if running:
        if not messagebox.askyesno("正在运行", "正在执行，确定退出吗？"):
            return
        shutdown_event.set()
    root.destroy()


# --------------------------------------------------------------------------- 启动


def startup():
    global config

    try:
        created = runner.ensure_config()
    except Exception as exc:
        messagebox.showerror("启动失败", str(exc))
        return

    if created:
        if messagebox.askyesno(
            "首次使用",
            "已生成 config.json。\n\n请填写账号密码、距离和时长后保存。\n现在打开配置文件吗？",
        ):
            runner.open_path(runner.CONFIG_PATH)

    try:
        config = runner.load_config()
    except runner.ConfigError as exc:
        config = None
        set_status("未配置", RED)
        env_var.set("请先填写 config.json")
        start_button.configure(state="disabled")
        result_title_var.set("需要配置")
        result_detail_var.set(str(exc))
        append_log(str(exc))
        run_preflight_async(None)
        return

    route_mode_var.set(config.get("route_mode", "osm"))
    update_target(config)
    dry_var.set(bool(config.get("dry_run_default", False)))
    refresh_anchor_row()
    set_status("就绪", GREEN)
    env_var.set("环境检查中…")
    start_button.configure(state="disabled")

    run_preflight_async(config)


root.protocol("WM_DELETE_WINDOW", on_close)
root.after(120, startup)
root.after(80, poll_queue)
if "--smoke-test" in sys.argv:
    root.title("校园跑 · 一键启动 [smoke]")
    root.after(3000, root.destroy)
root.mainloop()
