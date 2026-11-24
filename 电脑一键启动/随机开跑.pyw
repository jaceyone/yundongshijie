# -*- coding: utf-8 -*-
"""校园跑 · 随机开跑（双击即跑今天的一次）

打开窗口就自动开跑，不需要填参数：
距离 3000–4000 m、配速、取点锚点、路线（含正/反向）都由 random_runner 随机决定。

按钮：
- 「再跑一次」  重新随机一遍并提交
- 「只演练」    随机一遍，但只登录 + 生成轨迹，不写服务器
- 「打开日志」  打开本次日志所在目录
- 「关闭」

用 pythonw 运行时没有黑色命令行窗口。
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

import random_runner  # noqa: E402
import runner  # noqa: E402


class RandomRunApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.queue = queue.Queue()
        self.worker = None
        self.plan_text = tk.StringVar(value="等待开跑…")
        self.status_text = tk.StringVar(value="")
        self.log_dir = runner.LOG_DIR

        root.title("校园跑 · 随机开跑")
        root.geometry("760x520")
        root.minsize(620, 420)
        self._build_ui()
        root.after(120, self.drain_queue)
        root.after(400, lambda: self.start_run(dry_run=False, auto=True))

    # ---- UI --------------------------------------------------------------
    def _build_ui(self) -> None:
        mono = tkfont.nametofont("TkFixedFont")
        header = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        header.pack(fill="x")
        title = ttk.Label(header, text="随机开跑 · 距离 / 配速 / 锚点 / 路线全随机",
                          font=("Microsoft YaHei UI", 12, "bold"))
        title.pack(anchor="w")
        ttk.Label(header, textvariable=self.plan_text,
                  font=("Microsoft YaHei UI", 9), foreground="#1f4e79",
                  wraplength=720, justify="left").pack(anchor="w", pady=(4, 0))

        body = ttk.Frame(self.root, padding=(12, 4, 12, 4))
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, wrap="word", height=18, font=mono,
                            background="#101418", foreground="#d8e2ea",
                            insertbackground="#d8e2ea", relief="flat")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.text.configure(state="disabled")

        footer = ttk.Frame(self.root, padding=(12, 4, 12, 10))
        footer.pack(fill="x")
        self.btn_run = ttk.Button(footer, text="再跑一次",
                                  command=lambda: self.start_run(dry_run=False))
        self.btn_dry = ttk.Button(footer, text="只演练",
                                  command=lambda: self.start_run(dry_run=True))
        self.btn_log = ttk.Button(footer, text="打开日志", command=self.open_logs)
        self.btn_quit = ttk.Button(footer, text="关闭", command=self.root.destroy)
        for widget in (self.btn_run, self.btn_dry, self.btn_log, self.btn_quit):
            widget.pack(side="left", padx=(0, 8))
        ttk.Label(footer, textvariable=self.status_text, foreground="#666666").pack(
            side="right")

    def append(self, line: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def open_logs(self) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            runner.open_path(self.log_dir)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("打开日志", "打开失败: %s" % exc, parent=self.root)

    # ---- 运行 ------------------------------------------------------------
    def start_run(self, dry_run: bool, auto: bool = False) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.append("[提示] 上一次还在跑，请等它结束。")
            return
        try:
            config = runner.load_config()
        except runner.ConfigError as exc:
            messagebox.showerror("配置错误", str(exc), parent=self.root)
            return

        if not auto and not dry_run and config.get("confirm_before_submit", True):
            if not messagebox.askyesno(
                    "确认提交",
                    "本次会真实提交一次成绩（随机距离/配速/路线）。继续吗？",
                    parent=self.root):
                return

        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.plan_text.set("随机抽取中…")
        self.status_text.set("运行中")
        self._set_buttons(False)
        self.append("模式: %s" % ("演练（不提交）" if dry_run else "正式提交"))
        self.append("")

        def worker() -> None:
            try:
                result = random_runner.run_once(
                    config, dry_run=dry_run,
                    progress_callback=lambda line: self.queue.put(("log", line)))
                self.queue.put(("done", result))
            except Exception:  # noqa: BLE001
                self.queue.put(("crash", traceback.format_exc()))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _set_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (self.btn_run, self.btn_dry):
            widget.configure(state=state)

    def drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.append(str(payload))
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "crash":
                    self.append("")
                    self.append("内部异常：")
                    self.append(str(payload))
                    self.status_text.set("异常")
                    self._set_buttons(True)
        except queue.Empty:
            pass
        self.root.after(120, self.drain_queue)

    def _on_done(self, result) -> None:
        self._set_buttons(True)
        self.append("")
        self.append(result.message)
        if result.log_path:
            self.append("日志: %s" % result.log_path)
        if result.rrid:
            self.append("rrid: %s" % result.rrid)
        ok = bool(result.success)
        self.status_text.set("完成" if ok else "失败")
        self.plan_text.set("本次：%s" % (
            "距离 %d m · 时长 %d:%02d" % (result.distance, *divmod(int(result.time_sec), 60))
            if result.distance else "未产生计划"))
        if not ok:
            messagebox.showwarning("本次未完成", result.message, parent=self.root)


def main() -> int:
    root = tk.Tk()
    try:
        root.iconname("随机开跑")
    except Exception:
        pass
    RandomRunApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
