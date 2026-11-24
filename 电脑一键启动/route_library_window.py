# -*- coding: utf-8 -*-
"""
校园跑 · 路线库管理页（Tkinter）

从主窗口「管理路线库」打开：
  - 按 group_id 分组列出已保存的手动路线；
  - 选中一条变体后可进入地图编辑器修改、改备注、重新校验、删除；
  - 双击变体直接进入编辑。

编辑保存时覆盖原条目（RouteEditor 的 editing_route_id），不新增变体。
"""

from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Dict, List, Optional

import route_library as rl

BG = "#f3f4f6"
CARD = "#ffffff"
BORDER = "#e5e7eb"
TEXT = "#111827"
MUTED = "#6b7280"
GREEN = "#16a34a"
GREEN_DARK = "#15803d"
RED = "#dc2626"
ORANGE = "#d97706"

STATUS_LABELS = {
    "valid": "可用",
    "needs_review": "待复核",
    "invalid": "不可用",
}


def _time_key(value: str) -> str:
    return value or "0000-00-00T00:00:00"


def _short_time(value: str) -> str:
    return (value or "-").replace("T", " ")[:16]


def _point_names(entry: rl.RouteEntry) -> str:
    names = []
    for index, point in enumerate(entry.points or []):
        if isinstance(point, dict):
            name = point.get("name") or point.get("pointName") or ""
        else:
            name = ""
        names.append("P%d %s" % (index + 1, name) if name else "P%d" % (index + 1))
    return " / ".join(names)


class RouteLibraryWindow(tk.Toplevel):
    def __init__(
        self,
        master,
        library: Optional[rl.RouteLibrary] = None,
        target_distance_m: int = 3000,
        min_target_distance_m: int = 0,
        campus_id: str = "xuchang_280173698",
    ):
        super().__init__(master)
        self.target_distance_m = max(1, int(target_distance_m or 3000))
        self.min_target_distance_m = max(0, int(min_target_distance_m or 0))
        self.campus_id = campus_id
        self._planners: Dict[str, Any] = {}
        self._entries_by_item: Dict[str, rl.RouteEntry] = {}
        self._load_error = ""

        if library is not None:
            self.library: Optional[rl.RouteLibrary] = library
        else:
            try:
                self.library = rl.RouteLibrary()
            except ValueError as exc:
                self.library = None
                self._load_error = str(exc)

        self.title("校园跑 · 路线库管理")
        self.configure(bg=BG)
        self.minsize(940, 560)
        self._center_window(1040, 680)
        self._build_ui()
        self.refresh()
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.close)

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

        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(
            header,
            text="路线库管理",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 18, "bold"),
        ).pack(anchor="w")
        self.summary_var = tk.StringVar(value="")
        tk.Label(
            header,
            textvariable=self.summary_var,
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        ).pack(fill="x", pady=(2, 0))
        self.path_var = tk.StringVar(value="")
        tk.Label(
            header,
            textvariable=self.path_var,
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Consolas", 8),
        ).pack(fill="x")

        table = tk.Frame(self, bg=BG)
        table.pack(fill="both", expand=True, padx=16, pady=(8, 6))

        style = ttk.Style(self)
        try:
            style.configure(
                "Library.Treeview",
                rowheight=26,
                font=("Microsoft YaHei UI", 10),
            )
            style.configure(
                "Library.Treeview.Heading",
                font=("Microsoft YaHei UI", 9, "bold"),
            )
        except tk.TclError:
            pass

        columns = ("length", "laps", "status", "order", "notes", "created", "used")
        self.tree = ttk.Treeview(
            table,
            columns=columns,
            show="tree headings",
            selectmode="browse",
            style="Library.Treeview",
        )
        self.tree.heading("#0", text="点位组 / 路线")
        self.tree.column("#0", width=260, minwidth=180, stretch=True, anchor="w")
        headings = (
            ("length", "长度", 90, "e"),
            ("laps", "在目标下", 90, "e"),
            ("status", "状态", 70, "center"),
            ("order", "经过顺序", 150, "w"),
            ("notes", "备注", 150, "w"),
            ("created", "创建时间", 130, "w"),
            ("used", "最近使用", 130, "w"),
        )
        for key, label, width, anchor in headings:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=60, stretch=False, anchor=anchor)

        self.tree.tag_configure(
            "group",
            background="#eef2ff",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.tree.tag_configure("valid", foreground=GREEN_DARK)
        self.tree.tag_configure("needs_review", foreground=ORANGE)
        self.tree.tag_configure("invalid", foreground=RED)

        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_actions())
        self.tree.bind("<Double-1>", self._on_double_click)

        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=16, pady=(0, 14))

        actions = tk.Frame(footer, bg=BG)
        actions.pack(fill="x")
        self.edit_button = self._action_button(
            actions, "编辑路线", self._edit_selected, bg=GREEN, fg="white"
        )
        self.edit_button.pack(side="left")
        self.notes_button = self._action_button(
            actions, "备注", self._edit_notes
        )
        self.notes_button.pack(side="left", padx=(8, 0))
        self.revalidate_button = self._action_button(
            actions, "重新校验", self._revalidate_selected
        )
        self.revalidate_button.pack(side="left", padx=(8, 0))
        self.delete_button = self._action_button(
            actions, "删除", self._delete_selected, fg=RED
        )
        self.delete_button.pack(side="left", padx=(8, 0))
        self._action_button(actions, "刷新", self.refresh).pack(
            side="left", padx=(8, 0)
        )
        self._action_button(actions, "关闭", self.close).pack(side="right")

        self.status_var = tk.StringVar(
            value="双击一条路线可直接编辑；目标距离取 config.json，编辑页内只读。"
        )
        tk.Label(
            footer,
            textvariable=self.status_var,
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        ).pack(fill="x", pady=(6, 0))

    def _action_button(
        self,
        parent,
        text: str,
        command,
        bg: str = CARD,
        fg: str = TEXT,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=GREEN_DARK if bg == GREEN else BORDER,
            activeforeground="white" if bg == GREEN else TEXT,
            disabledforeground=MUTED,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            padx=12,
            pady=4,
            font=("Microsoft YaHei UI", 10, "bold" if bg == GREEN else "normal"),
            cursor="hand2",
        )

    # ------------------------------------------------------------------ 列表

    def refresh(self) -> None:
        if self.library is None:
            self.summary_var.set(
                "路线库读取失败：%s（文件：%s）"
                % (self._load_error or "未知错误", rl.DEFAULT_LIBRARY_PATH)
            )
            self.path_var.set("")
            self.status_var.set("请修复或删除路线库 JSON 后点「刷新」。")
            self._update_actions()
            return
        try:
            self.library.load()
        except ValueError as exc:
            self.summary_var.set("路线库读取失败：%s" % exc)
            self.path_var.set(str(self.library.path))
            self._update_actions()
            return

        self.tree.delete(*self.tree.get_children())
        self._entries_by_item.clear()

        groups: Dict[str, List[rl.RouteEntry]] = {}
        for entry in self.library.entries:
            key = entry.group_id or entry.points_key or entry.route_id
            groups.setdefault(key, []).append(entry)

        ordered_groups = sorted(
            groups.values(),
            key=lambda items: _time_key(max(item.created_at for item in items)),
            reverse=True,
        )
        for items in ordered_groups:
            items.sort(key=lambda item: item.length_m)
            first = items[0]
            names = _point_names(first)
            if len(names) > 44:
                names = names[:44] + "…"
            parent = self.tree.insert(
                "",
                "end",
                text="点位组 · %s（%d 条）" % (names, len(items)),
                values=("", "", "", "", "", "", ""),
                open=True,
                tags=("group",),
            )
            for index, entry in enumerate(items):
                item = self.tree.insert(
                    parent,
                    "end",
                    text="变体 %d · %s" % (index + 1, entry.route_id[:8]),
                    values=(
                        "%.1f m" % entry.length_m,
                        self._laps_text(entry),
                        STATUS_LABELS.get(entry.status, entry.status),
                        " → ".join("P%d" % (position + 1) for position in entry.order),
                        entry.notes or "",
                        _short_time(entry.created_at),
                        _short_time(entry.last_used_at),
                    ),
                    tags=(entry.status,),
                )
                self._entries_by_item[item] = entry

        total = len(self.library.entries)
        self.summary_var.set(
            "共 %d 组 / %d 条路线 · 目标 %d m（来自 config.json）%s"
            % (
                len(ordered_groups),
                total,
                self.target_distance_m,
                ""
                if total
                else " · 路线库为空：手动模式开始跑步并保存后才会出现路线",
            )
        )
        self.path_var.set(str(self.library.path))
        self._update_actions()

    def _laps_text(self, entry: rl.RouteEntry) -> str:
        if entry.length_m <= 0:
            return "-"
        if entry.length_m > self.target_distance_m + 0.001:
            return "超出 %.0f m" % (entry.length_m - self.target_distance_m)
        return "%.2f 趟" % (self.target_distance_m / entry.length_m)

    def _selected_entry(self) -> Optional[rl.RouteEntry]:
        selection = self.tree.selection()
        if not selection:
            return None
        return self._entries_by_item.get(selection[0])

    def _update_actions(self) -> None:
        entry = self._selected_entry()
        state = "normal" if entry is not None else "disabled"
        for button in (
            self.edit_button,
            self.notes_button,
            self.revalidate_button,
            self.delete_button,
        ):
            button.configure(state=state)
        if entry is None:
            self.status_var.set(
                "选中一条路线后可编辑；双击也可直接进入编辑。"
            )
            return
        self.status_var.set(
            "已选：%.1f m · %s · 经过 %s · route_id=%s"
            % (
                entry.length_m,
                self._laps_text(entry),
                " → ".join("P%d" % (index + 1) for index in entry.order),
                entry.route_id[:8],
            )
        )

    def _on_double_click(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if item in self._entries_by_item:
            self.tree.selection_set(item)
            self._edit_selected()

    # ------------------------------------------------------------------ 动作

    def _planner(self, campus: str):
        campus = str(campus or self.campus_id)
        if campus not in self._planners:
            self._planners[campus] = rl.load_planner(campus)
        return self._planners[campus]

    def _entry_target(self, entry: rl.RouteEntry) -> int:
        """重新校验时使用条目保存时的目标距离（由 laps × length 反推）。"""
        laps = 0.0
        validation = entry.validation if isinstance(entry.validation, dict) else {}
        try:
            laps = float(validation.get("laps") or 0)
        except (TypeError, ValueError):
            laps = 0.0
        target = int(round(entry.length_m * laps)) if laps > 0 else 0
        return max(target, int(math.ceil(entry.length_m)), 1)

    def _edit_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        import route_editor

        try:
            editor = route_editor.RouteEditor(
                self,
                checkpoints=entry.points,
                target_distance_m=self.target_distance_m,
                min_target_distance_m=self.min_target_distance_m,
                max_target_distance_m=0,
                campus_id=entry.campus or self.campus_id,
                unid=entry.unid or None,
                area_id=entry.area_id,
                library=self.library,
                planner=self._planner(entry.campus),
                modal=True,
                initial_route=entry.polyline,
                initial_notes=entry.notes,
                editing_route_id=entry.route_id,
            )
            editor.show_modal()
        except Exception as exc:
            messagebox.showerror(
                "编辑路线", "无法打开编辑器：%s" % exc, parent=self
            )
        try:
            if self.winfo_exists():
                self.grab_set()
        except tk.TclError:
            pass
        self.refresh()

    def _edit_notes(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        notes = simpledialog.askstring(
            "路线备注",
            "备注（留空可清除）：",
            initialvalue=entry.notes or "",
            parent=self,
        )
        if notes is None:
            return
        try:
            self.library.set_notes(entry.route_id, notes.strip())
        except Exception as exc:
            messagebox.showerror("备注", str(exc), parent=self)
            return
        self.refresh()

    def _revalidate_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        planner = self._planner(entry.campus)
        target = self._entry_target(entry)
        try:
            report = rl.validate_route(
                entry.polyline,
                entry.points,
                target,
                planner=planner,
            )
            self.library.update(
                entry.route_id,
                entry.polyline,
                entry.points,
                validation=report,
            )
        except Exception as exc:
            messagebox.showerror("重新校验", str(exc), parent=self)
            return

        if report.ok:
            lines = [
                "路线 %.1f m，目标 %d m，%.2f 趟（不足部分原路折返）。"
                % (report.length_m, target, report.laps)
            ]
            lines.extend("· %s" % item for item in report.warnings[:6])
            messagebox.showinfo(
                "校验通过", "\n".join(lines), parent=self
            )
        else:
            messagebox.showwarning(
                "校验未通过",
                "\n".join(report.errors[:8]),
                parent=self,
            )
        self.refresh()

    def _delete_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        if not messagebox.askyesno(
            "删除路线",
            "确定删除这条路线吗？\n\n%.1f m · 备注：%s\n\n删除后不可恢复。"
            % (entry.length_m, entry.notes or "（无）"),
            parent=self,
        ):
            return
        try:
            self.library.delete(entry.route_id)
        except Exception as exc:
            messagebox.showerror("删除路线", str(exc), parent=self)
            return
        self.refresh()

    # ------------------------------------------------------------------ 关闭

    def show_modal(self) -> None:
        self.wait_window(self)

    def close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
