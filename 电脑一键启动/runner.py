# -*- coding: utf-8 -*-
"""
校园跑 · 电脑一键启动核心流程

职责：
  - 读取 config.json（写死账号密码）
  - 预检 Node / Playwright / Chromium / 协议目录
  - 调用现有 SportClient792：自动极验登录 + 跑步提交
  - 写日志

不修改 协议/ 下任何文件。
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
PROTOCOL_HOME = REPO_ROOT / "协议" / "7.3.92"
LOGIN_HOME = PROTOCOL_HOME / "login"
CONFIG_PATH = APP_DIR / "config.json"
EXAMPLE_PATH = APP_DIR / "config.example.json"
LOG_DIR = APP_DIR / "logs"
DEVICE_ID_PATH = APP_DIR / "device_id"

# 上游（腾讯 EdgeOne）在连续请求后会把响应压到 20s 以上，甚至直接断连。
# 两次运行间隔太短只会让限流更严重，所以上次运行结束后要求冷却一段时间。
MIN_RUN_INTERVAL_SEC = 60

# 代跑白名单（可选）：留空 = 任何账号都能真实提交。
# 想在发布版里只允许本人账号时，在 config.json 里加 "allowed_phones": ["13800000000"]；
# 名单外的账号会在登录前中止，不消耗登录次数、不写成绩。
DEFAULT_ALLOWED_PHONES: set = set()

DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "distance": 3000,
    "time_sec": 1060,
    "mode": 1,
    "route_mode": "osm",
    "dry_run_default": False,
    "confirm_before_submit": True,
    "campus": "xuchang_280173698",
    "node_path": "",
    "chromium_path": "",
    "allowed_phones": [],
    "session_file": "session.json",
}


def _normalise_phones(values) -> set:
    """把配置里的白名单（数组或逗号分隔字符串）统一成纯数字集合。"""
    if isinstance(values, str):
        values = re.split(r"[,，;；\s]+", values)
    if not isinstance(values, (list, tuple, set)):
        return set()
    phones = set()
    for item in values:
        text = re.sub(r"\D", "", str(item or ""))
        if text:
            phones.add(text)
    return phones


def allowed_phones(config: Optional[dict] = None) -> set:
    """当前生效的白名单；空集表示不限制账号。"""
    phones = _normalise_phones((config or {}).get("allowed_phones"))
    return phones or set(DEFAULT_ALLOWED_PHONES)


def gate_allows(phone: str, config: Optional[dict] = None) -> bool:
    """账号是否允许真实提交。"""
    phones = allowed_phones(config)
    return not phones or str(phone or "").strip() in phones


# 兼容旧引用（例如脚本里直接读 runner.RUN_ALLOWED_PHONES）；实际判定用 gate_allows()。
RUN_ALLOWED_PHONES = set(DEFAULT_ALLOWED_PHONES)


class ConfigError(Exception):
    """配置缺失或无效。"""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class RunResult:
    success: bool
    dry_run: bool
    message: str
    rrid: Optional[object] = None
    distance: int = 0
    time_sec: int = 0
    is_valid: Optional[bool] = None
    error_code: Optional[object] = None
    log_path: str = ""
    elapsed_sec: float = 0.0


# --------------------------------------------------------------------------- 配置


def ensure_config() -> bool:
    """config.json 不存在时从模板复制一份，返回是否新建。"""
    if CONFIG_PATH.exists():
        return False
    if not EXAMPLE_PATH.exists():
        raise ConfigError(f"缺少配置模板: {EXAMPLE_PATH}")
    shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
    return True


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


def load_config(path: Optional[Path] = None) -> dict:
    path = Path(path or CONFIG_PATH)
    if not path.exists():
        raise ConfigError(f"缺少配置文件: {path}")

    try:
        # 记事本/PowerShell 另存会带 UTF-8 BOM，utf-8-sig 两种都能读
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"配置文件读取失败: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config.json 顶层必须是对象")

    cfg = dict(DEFAULT_CONFIG)
    cfg.update(raw)

    username = str(cfg.get("username", "")).strip()
    password = str(cfg.get("password", ""))
    if not username or not password:
        raise ConfigError("请在 config.json 填写 username 和 password")
    if username == "13800000000" or password == "your-password":
        raise ConfigError("config.json 还是示例值，请填写真实账号密码")

    try:
        distance = int(cfg.get("distance", 3000))
        time_sec = int(cfg.get("time_sec", 1060))
        mode = int(cfg.get("mode", 1))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"distance / time_sec / mode 必须是整数: {exc}") from exc

    if distance <= 0:
        raise ConfigError("distance 必须大于 0")
    if time_sec <= 0:
        raise ConfigError("time_sec 必须大于 0")
    if mode not in (1, 6):
        raise ConfigError("mode 只能是 1（计分跑）或 6（自由跑）")

    route_mode = str(cfg.get("route_mode", "osm")).strip().lower()
    if route_mode not in ("osm", "manual"):
        raise ConfigError("route_mode 只能是 osm（OSM 自动）或 manual（手动路线库）")

    cfg.update(
        {
            "username": username,
            "password": password,
            "distance": distance,
            "time_sec": time_sec,
            "mode": mode,
            "route_mode": route_mode,
            "dry_run_default": _as_bool(cfg.get("dry_run_default"), False),
            "confirm_before_submit": _as_bool(cfg.get("confirm_before_submit"), True),
            "campus": str(cfg.get("campus", "") or "xuchang_280173698").strip(),
            "node_path": str(cfg.get("node_path", "") or "").strip(),
            "chromium_path": str(cfg.get("chromium_path", "") or "").strip(),
            "allowed_phones": sorted(_normalise_phones(cfg.get("allowed_phones"))),
            "session_file": Path(
                str(cfg.get("session_file") or "session.json").strip()
            ).name,
        }
    )
    return cfg


def session_path(config: Optional[dict] = None) -> Path:
    """会话文件路径：默认 session.json，可用 config 的 session_file 换名。"""
    name = Path(
        str((config or {}).get("session_file") or "session.json").strip()
    ).name
    return APP_DIR / (name or "session.json")


def open_path(path) -> None:
    path = Path(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def stable_device_id() -> str:
    """固定本机的 DeviceId，避免每次运行都被识别成一台新设备。"""
    try:
        value = DEVICE_ID_PATH.read_text(encoding="utf-8").strip().lower()
        if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
            return value
    except OSError:
        pass

    value = uuid.uuid4().hex
    try:
        DEVICE_ID_PATH.write_text(value + "\n", encoding="utf-8")
    except OSError:
        pass
    return value


def resolve_device_id(config: Optional[dict] = None) -> str:
    """优先用 config 里写死的 device_id（多账号各用各的历史设备），否则用本机固定值。"""
    value = str((config or {}).get("device_id") or "").strip().lower()
    if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
        return value
    return stable_device_id()


# --------------------------------------------------------------------------- 预检


def _shorten(text, limit: int = 500) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _patch_subprocess_no_window() -> None:
    """pythonw 启动时，避免 Node/Chromium 子进程弹出黑色控制台窗口。"""
    if not sys.platform.startswith("win"):
        return
    if getattr(subprocess, "_campusrun_no_window", False):
        return

    original = subprocess.Popen

    class NoWindowPopen(original):
        def __init__(self, *args, **kwargs):
            if "creationflags" not in kwargs:
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = NoWindowPopen
    subprocess._campusrun_no_window = True


def node_executable(config: Optional[dict] = None) -> Optional[str]:
    if config:
        raw = str(config.get("node_path", "") or "").strip()
        if raw:
            p = Path(raw).expanduser()
            if p.is_file():
                return str(p)

    found = shutil.which("node")
    if found:
        return found

    for candidate in (
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def _node_env(config: Optional[dict]) -> dict:
    env = os.environ.copy()
    node_modules = LOGIN_HOME / "node_modules"
    existing = env.get("NODE_PATH", "")
    env["NODE_PATH"] = str(node_modules) + (os.pathsep + existing if existing else "")
    if config and config.get("chromium_path"):
        env["CHROMIUM_PATH"] = str(config["chromium_path"])
    return env


def deep_solve_check(config: Optional[dict] = None) -> Check:
    """真实解一次极验（不登录、不提交），验证自动验证码链路。"""
    if str(PROTOCOL_HOME) not in sys.path:
        sys.path.insert(0, str(PROTOCOL_HOME))
    if config:
        if config.get("node_path"):
            os.environ["NODE_EXECUTABLE"] = str(config["node_path"])
        if config.get("chromium_path"):
            os.environ["CHROMIUM_PATH"] = str(config["chromium_path"])

    try:
        from login.captcha import GeetestV4Solver

        started = time.time()
        result = GeetestV4Solver().solve()
        return Check(
            "自动极验",
            True,
            "识别成功，用时 %.1fs，lot=%s…" % (
                time.time() - started,
                (result.lot_number or "")[:8],
            ),
        )
    except Exception as exc:
        return Check("自动极验", False, _shorten(f"{type(exc).__name__}: {exc}", 800))


def preflight(config: Optional[dict] = None, deep: bool = False) -> List[Check]:
    _patch_subprocess_no_window()
    checks: List[Check] = []

    checks.append(
        Check(
            "Python",
            sys.version_info >= (3, 12),
            "Python %s" % sys.version.split()[0],
        )
    )

    checks.append(
        Check(
            "协议目录",
            (PROTOCOL_HOME / "sport_client.py").is_file(),
            str(PROTOCOL_HOME),
        )
    )

    if config is None:
        try:
            config = load_config()
            checks.append(Check("配置文件", True, str(CONFIG_PATH)))
        except ConfigError as exc:
            checks.append(Check("配置文件", False, str(exc)))
    else:
        checks.append(Check("配置文件", True, str(CONFIG_PATH)))

    node = node_executable(config)
    if not node:
        checks.append(
            Check(
                "Node.js",
                False,
                "未找到 node；请安装 Node，或在 config.json 的 node_path 填写完整路径",
            )
        )
    else:
        try:
            proc = subprocess.run(
                [node, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            detail = ((proc.stdout or "") + (proc.stderr or "")).strip() or node
            checks.append(
                Check(
                    "Node.js",
                    proc.returncode == 0,
                    "%s（%s）" % (detail, node),
                )
            )
        except Exception as exc:
            checks.append(Check("Node.js", False, "%s: %s" % (node, exc)))

    playwright_pkg = LOGIN_HOME / "node_modules" / "playwright" / "package.json"
    if not playwright_pkg.is_file():
        checks.append(Check("Playwright", False, "缺少 %s" % playwright_pkg))
    else:
        try:
            version = json.loads(playwright_pkg.read_text(encoding="utf-8")).get(
                "version", "?"
            )
            checks.append(Check("Playwright", True, "v%s" % version))
        except Exception:
            checks.append(Check("Playwright", True, str(playwright_pkg)))

    if node:
        script = (
            "const {chromium}=require('playwright');"
            "(async()=>{const b=await chromium.launch({headless:true});"
            "await b.close();console.log('CHROMIUM_OK');})()"
            ".catch(e=>{console.error(e.message);process.exit(1)});"
        )
        try:
            proc = subprocess.run(
                [node, "-e", script],
                cwd=str(LOGIN_HOME),
                env=_node_env(config),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
            )
            ok = proc.returncode == 0 and "CHROMIUM_OK" in (proc.stdout or "")
            detail = (
                "Chromium 启动正常"
                if ok
                else _shorten((proc.stderr or proc.stdout or "启动失败").strip(), 500)
            )
            checks.append(Check("Chromium", ok, detail))
        except Exception as exc:
            checks.append(Check("Chromium", False, _shorten(str(exc), 500)))
    else:
        checks.append(Check("Chromium", False, "先解决 Node.js"))

    try:
        import tkinter

        checks.append(Check("Tkinter", True, "Tk %s" % tkinter.TkVersion))
    except Exception as exc:
        checks.append(Check("Tkinter", False, _shorten(str(exc), 300)))

    if deep:
        checks.append(deep_solve_check(config))

    return checks


# --------------------------------------------------------------------------- 运行


def _remaining_cooldown_sec() -> float:
    """距上次运行结束还需等待多少秒（以最新日志的 mtime 为准）。"""
    try:
        newest = max(
            (p.stat().st_mtime for p in LOG_DIR.glob("*.log")),
            default=0.0,
        )
    except OSError:
        return 0.0
    return max(0.0, MIN_RUN_INTERVAL_SEC - (time.time() - newest))


def _friendly_exception(exc: BaseException) -> str:
    """把协议层抛出的网络异常转成用户能看懂的中文提示。"""
    hint = "上游加速节点在高频请求后会压响应，请等待 5–10 分钟后再试。"
    if isinstance(exc, TimeoutError):
        return "网络超时：服务端未在限定时间内响应。" + hint
    if isinstance(exc, urllib.error.URLError):
        return "网络不可用：%s。请检查网络（或代理/VPN）后重试。" % (exc.reason,)
    if isinstance(exc, ConnectionError):
        return "连接被中断：%s。%s" % (exc, hint)
    if isinstance(exc, OSError):
        return "网络错误：%s。请检查网络后重试。" % (exc,)
    return _shorten("%s: %s" % (type(exc).__name__, exc), 1200)


def _friendly_login_error(code, message: str) -> str:
    text = "登录失败"
    if code is not None:
        text += "（code=%s）" % code
    text += "：%s" % message
    if str(code) == "10121":
        text += "｜提示：账号单设备保护，请先在其它设备退出登录，或稍后再试。"
    return text


def _distance_bounds_error(rule, distance: int) -> str:
    """目标距离只受学校最低里程约束；允许超过 day_goal / 3000 m。"""
    if rule is None:
        return ""
    try:
        minimum = int(getattr(rule, "min_distance", 0) or 0)
        value = int(distance)
    except (TypeError, ValueError):
        return ""
    if minimum > 0 and value < minimum:
        return "目标距离 %d m 低于学校最低 %d m；请修改 config.json 的距离。" % (
            value,
            minimum,
        )
    return ""


def _friendly_run_error(code, message: str) -> str:
    text = "跑步提交失败"
    if code is not None:
        text += "（code=%s）" % code
    text += "：%s" % message
    if str(code) == "10121":
        text += "｜提示：账号单设备保护，请先在其它设备退出登录，或稍后再试。"
    return text


def run(
    config: dict,
    dry_run: bool = False,
    progress_callback: Optional[Callable[[str], None]] = None,
    route_provider: Optional[Callable] = None,
) -> RunResult:
    """执行一次登录 + 跑步。异常会转换成 RunResult，不抛给 UI 线程。"""
    _patch_subprocess_no_window()

    phone = str(config.get("username") or "").strip()
    if not gate_allows(phone, config):
        return RunResult(
            success=False,
            dry_run=dry_run,
            message=(
                "账号 %s 不在本机白名单里。"
                "要允许该账号，把手机号加进 config.json 的 \"allowed_phones\"，"
                "或留空该字段表示不限制。" % (phone or "（空）")
            ),
            distance=int(config.get("distance", 0)),
            time_sec=int(config.get("time_sec", 0)),
        )

    if not os.environ.get("CAMPUSRUN_SKIP_COOLDOWN"):
        wait = _remaining_cooldown_sec()
        if wait > 0:
            return RunResult(
                success=False,
                dry_run=dry_run,
                message=(
                    "请求过于频繁：距上次运行结束仅 %.0f 秒，请再等 %.0f 秒。"
                    "上游在高频请求后会把响应压到 20 秒以上，连续点击只会更慢。"
                    % (MIN_RUN_INTERVAL_SEC - wait, wait)
                ),
                distance=int(config.get("distance", 0)),
                time_sec=int(config.get("time_sec", 0)),
            )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / time.strftime("%Y%m%d-%H%M%S.log")
    started = time.time()

    try:
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError as exc:
        return RunResult(
            success=False,
            dry_run=dry_run,
            message="无法写入日志: %s" % exc,
            distance=config.get("distance", 0),
            time_sec=config.get("time_sec", 0),
        )

    def log(message: str) -> None:
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), message)
        try:
            log_file.write(line + "\n")
        except Exception:
            pass

        if progress_callback is not None:
            try:
                progress_callback(line)
            except Exception:
                pass
        elif sys.stdout is not None:
            try:
                print(line, flush=True)
            except Exception:
                pass

    def finish(success: bool, message: str, **kwargs) -> RunResult:
        elapsed = time.time() - started
        log("结束（%s，%.1fs）: %s" % ("成功" if success else "失败", elapsed, message))
        return RunResult(
            success=success,
            dry_run=dry_run,
            message=message,
            distance=int(config.get("distance", 0)),
            time_sec=int(config.get("time_sec", 0)),
            log_path=str(log_path),
            elapsed_sec=elapsed,
            **kwargs,
        )

    try:
        if str(PROTOCOL_HOME) not in sys.path:
            sys.path.insert(0, str(PROTOCOL_HOME))
        if config.get("node_path"):
            os.environ["NODE_EXECUTABLE"] = str(config["node_path"])
        if config.get("chromium_path"):
            os.environ["CHROMIUM_PATH"] = str(config["chromium_path"])

        from sport_client import SportClient792

        log("创建客户端...")
        device_config = {"DeviceId": resolve_device_id(config)}
        client = SportClient792(device_config=device_config)
        log("设备 ID: %s" % device_config["DeviceId"])

        anchor_supported = "anchor_gcj" in inspect.signature(
            client.execute_run).parameters
        if config.get("anchor_gcj") and not anchor_supported:
            log("[警告] 当前协议包不支持锚点取点，回退围栏顶点")

        # 手动路线库要求协议侧支持 route_provider；旧协议包在登录前就失败，
        # 避免白白消耗一次登录（账号有单设备保护）。
        if config.get("route_mode") == "manual" and not dry_run:
            if int(config["mode"]) != 1:
                return finish(False, "手动路线库只支持计分跑（mode=1）")
            if route_provider is None:
                return finish(False, "手动路线库模式缺少 route_provider 回调")
            if "route_provider" not in inspect.signature(client.execute_run).parameters:
                return finish(
                    False,
                    "当前协议包不支持 route_provider；"
                    "请确认 协议/7.3.92 已更新到带手动路线钩子的版本",
                )

        log("登录中（自动极验，无需手动操作）...")
        login_res = client.login(
            config["username"],
            config["password"],
            progress_callback=log,
        )
        if not login_res.success:
            code = getattr(login_res, "error_code", None)
            message = getattr(login_res, "message", "登录失败")
            return finish(False, _friendly_login_error(code, message), error_code=code)

        log("登录成功: uid=%s unid=%s" % (client.uid, client.unid))

        if dry_run:
            log("演练模式：只查询档案和规则，不提交成绩...")
            profile = client.get_user_profile()
            if profile:
                log(
                    "个人档案: %s · %s · %s"
                    % (profile.name, profile.campus_name, profile.student_id)
                )
            else:
                log("[警告] 个人档案查询为空")

            rule = client.get_run_policy(run_mode=config["mode"])
            if rule:
                log(
                    "跑步规则: 单日上限=%sm 单次最低=%sm 配速=%s~%s 步频=%s~%s"
                    % (
                        rule.day_goal,
                        rule.min_distance,
                        rule.speed_top,
                        rule.speed_bottom,
                        rule.step_top,
                        rule.step_bottom,
                    )
                )
            else:
                log("[警告] 跑步规则查询为空")

            return finish(True, "演练完成：登录成功，未提交成绩", is_valid=None)

        # 目标距离必须在学校规则范围内；避免 >dayGoal 的里程提交。
        try:
            rule = client.get_run_policy(run_mode=config["mode"])
        except Exception as exc:
            rule = None
            log("[警告] 规则校验失败: %s" % exc)
        if rule is not None:
            bounds_error = _distance_bounds_error(rule, int(config["distance"]))
            if bounds_error:
                return finish(False, bounds_error)
        else:
            detail = getattr(getattr(client, "policy", None), "last_policy_error", None)
            log("[警告] 规则预检未取得响应%s；提交前会重试"
                % ("：%s" % detail if detail else ""))

        if config.get("route_mode") == "manual":
            if int(config["mode"]) != 1:
                return finish(False, "手动路线库只支持计分跑（mode=1）")
            if route_provider is None:
                return finish(False, "手动路线库模式缺少 route_provider 回调")
            if "route_provider" not in inspect.signature(client.execute_run).parameters:
                return finish(
                    False,
                    "当前协议包不支持 route_provider；"
                    "请确认 协议/7.3.92 已更新到带手动路线钩子的版本",
                )
            route_label = "手动路线库"
            run_kwargs = {"route_provider": route_provider}
        else:
            route_label = "OSM 避建筑" if int(config["mode"]) == 1 else "内部合成（自由跑）"
            run_kwargs = {}

        anchor_gcj = config.get("anchor_gcj")
        if anchor_gcj and anchor_supported:
            try:
                anchor_lat, anchor_lon = float(anchor_gcj[0]), float(anchor_gcj[1])
            except (TypeError, ValueError, IndexError):
                log("[警告] 锚点坐标无效，回退围栏顶点")
            else:
                run_kwargs.update(
                    anchor_gcj=(anchor_lat, anchor_lon),
                    anchor_id=config.get("anchor_id"),
                    anchor_name=config.get("anchor_name"),
                )
                log("取点锚点: %s (%.6f, %.6f) 来源=锚点池"
                    % (config.get("anchor_name") or config.get("anchor_id") or "自定义",
                       anchor_lat, anchor_lon))

        log("路线来源: %s" % route_label)
        log("跑步提交中（轨迹 / OBS / 结算 / 回查）...")
        run_res = client.execute_run(
            target_distance_m=config["distance"],
            target_time_sec=config["time_sec"],
            sport_type=config["mode"],
            progress_callback=log,
            campus=config.get("campus") or "xuchang_280173698",
            **run_kwargs,
        )
        if not run_res.success:
            code = getattr(run_res, "error_code", None)
            message = getattr(run_res, "message", "跑步提交失败")
            return finish(False, _friendly_run_error(code, message), error_code=code)

        valid = getattr(run_res, "is_valid", None)
        total_distance = getattr(run_res, "total_distance", config["distance"])
        total_time = getattr(run_res, "total_time", config["time_sec"])
        summary = "rrid=%s · %.0fm / %ss · %s" % (
            getattr(run_res, "rrid", None),
            float(total_distance),
            total_time,
            "达标" if valid else "未达标",
        )
        return finish(
            True,
            summary,
            rrid=getattr(run_res, "rrid", None),
            is_valid=valid,
        )
    except Exception as exc:
        try:
            log("异常堆栈:\n" + traceback.format_exc())
        except Exception:
            pass
        return finish(False, _friendly_exception(exc))
    finally:
        try:
            log_file.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- CLI


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="校园跑 · 电脑一键启动核心")
    parser.add_argument("--dry-run", action="store_true", help="只登录，不提交成绩")
    parser.add_argument("--check", action="store_true", help="只做环境自检")
    parser.add_argument("--deep", action="store_true", help="自检时真实解一次极验")
    args = parser.parse_args(argv)

    try:
        created = ensure_config()
        if created:
            print("已生成 config.json，请先填写账号密码。")
    except ConfigError as exc:
        print("配置模板:", exc)
        return 2

    try:
        config = load_config()
    except ConfigError as exc:
        print("配置错误:", exc)
        return 2

    if args.check:
        checks = preflight(config, deep=args.deep)
        for check in checks:
            print(("[OK]  " if check.ok else "[FAIL]") + " %s: %s" % (check.name, check.detail))
        return 0 if all(check.ok for check in checks) else 1

    result = run(config, dry_run=args.dry_run)
    print(result.message)
    print("日志:", result.log_path)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
