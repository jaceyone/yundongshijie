# -*- coding: utf-8 -*-
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Dict, Any, Optional, Callable

# 兼容包引用与单文件直跑
cur_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(cur_dir)
for p in [cur_dir, parent_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from .config import (
        URL_CHECK_GEE_USE,
        URL_GEE_VALIDATE,
        URL_LOGIN,
        LOGIN_CAPTCHA_ID,
        DEFAULT_DEVICE_CONFIG,
    )
    from .models import LoginResult, UserProfile, GeetestResult
    from .captcha import GeetestV4Solver
except (ImportError, ValueError):
    from config import (
        URL_CHECK_GEE_USE,
        URL_GEE_VALIDATE,
        URL_LOGIN,
        LOGIN_CAPTCHA_ID,
        DEFAULT_DEVICE_CONFIG,
    )
    from models import LoginResult, UserProfile, GeetestResult
    from captcha import GeetestV4Solver

try:
    from .crypto import NetSecClient, compute_token_sign, aes128_cbc_decrypt
except (ImportError, ValueError):
    from crypto import NetSecClient, compute_token_sign, aes128_cbc_decrypt

from core.net import build_https_opener

# 登录链路网络参数
#
# 实测（2026-11-17）：run.gxapp.iydsj.com 走腾讯 EdgeOne 加速，连续请求后
# 边缘节点会把响应压到 20 s 以上，甚至直接断连（RemoteDisconnected）。
# 旧的单次 20 s 超时正好卡在限流区间内，一次抖动就会废掉整次登录。
# 这里把单次超时放宽到 35 s；checkGeeUse / geevalidate 是只读请求，允许
# 有限重试；/login 超时结果未知（服务端可能已登录成功），不自动重试。
REQUEST_TIMEOUT = 35.0
RETRY_DELAYS = (1.5, 3.0)
PREAUTH_RETRIES = 2
GEEVALIDATE_RETRIES = 1

_NETWORK_HINT = ("运动世界校园加速节点在高频请求后会把响应压到 20 秒以上，"
                 "请等待 5–10 分钟后再试，期间不要连续点击。")


class LoginNetworkError(RuntimeError):
    """登录链路的网络层失败（重试耗尽后抛出）。"""

    def __init__(self, message: str, last_error: Optional[BaseException] = None):
        super().__init__(message)
        self.last_error = last_error


def _is_retryable_network_error(exc: BaseException) -> bool:
    """网络抖动/被限流可重试；服务端已给出 HTTP 状态码的错误不重试。"""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return (isinstance(exc.reason, BaseException)
                and _is_retryable_network_error(exc.reason))
    return isinstance(exc, OSError)  # TimeoutError / ConnectionError / ssl / gaierror


def _network_failure(step: str, exc: BaseException) -> "LoginResult":
    """把预检阶段的网络失败包装成登录结果，避免英文堆栈直接弹给界面。"""
    return LoginResult(
        success=False,
        error_code=-1,
        message="网络超时（%s）：%s 详情：%s" % (step, _NETWORK_HINT, exc),
        raw={"network_error": str(exc)},
    )


class LoginClient:
    """运动世界校园 7.3.92 独立登录客户端"""

    def __init__(self,
                 device_config: Optional[Dict[str, Any]] = None,
                 solver: Optional[GeetestV4Solver] = None):
        self.device_config = dict(DEFAULT_DEVICE_CONFIG)
        if device_config:
            self.device_config.update(device_config)

        self.solver = solver or GeetestV4Solver()
        self.crypto = NetSecClient()
        self.ssl_ctx = ssl.create_default_context()
        self.opener = build_https_opener(self.ssl_ctx)

    def execute_request(self,
                        url: str,
                        body: Any,
                        custom_headers: Optional[Dict[str, str]] = None,
                        timeout: float = REQUEST_TIMEOUT,
                        retries: int = 0,
                        log: Optional[Callable[[str], None]] = None) -> Any:
        """底层标准加解密请求管道。

        ``retries`` 只给可安全重复的只读请求；每次重试都会重新生成
        nonce / timeStamp / headerSign。``log`` 用于把重试过程写进运行日志。
        """
        attempts = max(1, 1 + int(retries))
        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            if attempts > 1 and log:
                log("    -> 请求 %s（第 %d/%d 次，超时 %.0fs）..."
                    % (url.rsplit("/", 1)[-1], attempt, attempts, timeout))
            try:
                return self._execute_once(url, body, custom_headers, timeout)
            except Exception as exc:
                if not _is_retryable_network_error(exc):
                    raise
                last_error = exc
                if attempt >= attempts:
                    break
                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                if log:
                    log("    [!] 网络异常：%s；%.1fs 后重试..." % (exc, delay))
                time.sleep(delay)
        raise LoginNetworkError(
            "%s 请求失败（单次 %.0fs，共 %d 次）：%s"
            % (url, timeout, attempts, last_error),
            last_error,
        ) from last_error

    def _execute_once(self,
                      url: str,
                      body: Any,
                      custom_headers: Optional[Dict[str, str]] = None,
                      timeout: float = REQUEST_TIMEOUT) -> Any:
        """单次发送：加密 → 发送 → 解密响应。"""
        now_ms = int(time.time() * 1000)
        nonce = str(uuid.uuid4())

        headers = dict(self.device_config)
        headers["timeStamp"] = str(now_ms)
        headers["nonce"] = nonce

        if custom_headers:
            headers.update(custom_headers)

        # 1. 对全部 Header 生成 headerSign
        h_wire, h_resp_key = self.crypto.encrypt_request(
            json.dumps(headers, separators=(",", ":"), ensure_ascii=False)
        )
        headers["headerSign"] = json.dumps(h_wire, separators=(",", ":"))

        # 2. 对 Body 执行 NetSec 加密
        b_wire, _ = self.crypto.encrypt_request(body)
        b_bytes = json.dumps(b_wire).encode("utf-8")

        # 3. 发送请求
        req = urllib.request.Request(url, data=b_bytes, headers=headers, method="POST")
        with self.opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            resp_obj = json.loads(raw)
            if "r" in resp_obj:
                r_bytes = base64.b64decode(resp_obj["r"])
                dec_h = aes128_cbc_decrypt(r_bytes, h_resp_key)
                inner = json.loads(dec_h.decode("utf-8"))
                if "data" in inner:
                    biz_b64 = inner["data"]
                    biz_bytes = base64.b64decode(biz_b64)
                    try:
                        return json.loads(biz_bytes.decode("utf-8"))
                    except Exception:
                        return biz_bytes.decode("utf-8")
                return inner
            return resp_obj

    def check_gee_use(self,
                      username: str,
                      session_uuid: str,
                      log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """发起安全初审（只读预检，可安全重试）"""
        body = {"username": username, "uuid": session_uuid, "unid": 0, "type": 2}
        return self.execute_request(URL_CHECK_GEE_USE, body,
                                    retries=PREAUTH_RETRIES, log=log)

    def gee_validate(self,
                     gt_res: GeetestResult,
                     session_uuid: str,
                     username: str,
                     log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """向服务端上报极验凭据并进行二次绑定校验"""
        payload = gt_res.to_validate_payload(session_uuid=session_uuid, username=username)
        return self.execute_request(URL_GEE_VALIDATE, payload,
                                    retries=GEEVALIDATE_RETRIES, log=log)

    def submit_login(self,
                     username: str,
                     password: str,
                     session_uuid: str,
                     log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """提交基础认证凭据与设备信息（超时结果未知，不自动重试）"""
        auth_raw = f"{username}:{password}".encode("utf-8")
        custom_headers = {
            "Authorization": "Basic " + base64.b64encode(auth_raw).decode("ascii")
        }
        login_body = {
            "loginType": 1,
            "device_model": self.device_config.get("deviceName", "Android Device"),
            "os_version": self.device_config.get("osVersion", "13"),
            "username": username,
            "uuid": session_uuid
        }
        return self.execute_request(URL_LOGIN, login_body,
                                    custom_headers=custom_headers, log=log)

    def login(self,
              username: str,
              password: str,
              progress_callback: Optional[Callable[[str], None]] = None) -> LoginResult:
        """端到端登录流程 (checkGeeUse -> solve_captcha -> geevalidate -> login)"""
        def log(msg: str):
            if progress_callback:
                progress_callback(msg)

        session_uuid = str(uuid.uuid4())

        # 1. checkGeeUse
        log("[1/3] 发起账号安全初审 (checkGeeUse)...")
        try:
            chk_res = self.check_gee_use(username, session_uuid, log=log)
        except LoginNetworkError as exc:
            log("    -> 网络失败：%s" % exc)
            return _network_failure("账号安全初审", exc)

        need_captcha = True
        if isinstance(chk_res, dict) and chk_res.get("data") is True:
            need_captcha = False

        # 2. 极验滑块与二次验证 (带自动重试)
        if need_captcha:
            max_val_retries = 3
            val_passed = False
            for val_attempt in range(1, max_val_retries + 1):
                log(f"[2/3] 极验人机校验 (第 {val_attempt}/{max_val_retries} 次尝试)...")
                gt_res = self.solver.solve(captcha_id=LOGIN_CAPTCHA_ID)
                log(f"    -> 滑块识别验证完成 (lot_number: {gt_res.lot_number[:16]}...)")

                log("[2.1/3] 上报极验凭据至服务端进行会话绑定 (geevalidate)...")
                try:
                    val_res = self.gee_validate(gt_res, session_uuid, username, log=log)
                except LoginNetworkError as exc:
                    log("    -> 网络失败：%s" % exc)
                    return _network_failure("极验会话绑定", exc)
                log(f"    -> geevalidate 响应: {val_res}")
                if isinstance(val_res, dict) and val_res.get("error") == 10000:
                    val_passed = True
                    log("    -> 二次校验通过 PASS!")
                    break
                else:
                    log(f"    [!] 第 {val_attempt} 次凭据核验未通过，准备重试...")
                    time.sleep(1.0)

            if not val_passed:
                err_msg = val_res.get("message", "geevalidate 校验失败") if isinstance(val_res, dict) else str(val_res)
                return LoginResult(
                    success=False,
                    error_code=val_res.get("error", -1) if isinstance(val_res, dict) else -1,
                    message=f"极验二次校验失败: {err_msg}",
                    raw=val_res
                )
        else:
            log("[2/3] 当前设备/会话处于白名单，免验证码直接跳过！")

        # 3. 正式登录
        log("[3/3] 提交正式鉴权凭据 (login)...")
        try:
            login_res = self.submit_login(username, password, session_uuid, log=log)
        except LoginNetworkError as exc:
            # 超时说明请求已发出但没等到响应，服务端可能已经登录成功；
            # 自动重试只会增加密码错误次数，所以这里直接返回「结果未知」。
            log("    -> 网络失败：%s" % exc)
            return LoginResult(
                success=False,
                error_code=-1,
                message=("登录请求超时：服务端未响应，本次结果未知。"
                         "请先到官方 App 确认是否已登录成功，不要立即重试"
                         "（避免触发密码错误锁定）。详情：%s" % exc),
                raw={"network_error": str(exc)},
            )

        if isinstance(login_res, dict) and login_res.get("error") == 10000 and "data" in login_res:
            user_prof = UserProfile.from_api_data(login_res["data"])
            log(f"登录成功: 姓名={user_prof.name} uid={user_prof.uid} unid={user_prof.unid} 学校={user_prof.campus_name}")
            return LoginResult(
                success=True,
                error_code=10000,
                message="登录成功",
                user=user_prof,
                raw=login_res
            )
        else:
            code = login_res.get("error", -1) if isinstance(login_res, dict) else -1
            msg = login_res.get("message", "未知错误") if isinstance(login_res, dict) else str(login_res)
            return LoginResult(
                success=False,
                error_code=code,
                message=msg,
                raw=login_res
            )


