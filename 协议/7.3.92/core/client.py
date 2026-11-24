# -*- coding: utf-8 -*-
import base64
import json
import ssl
import time
import urllib.error
import urllib.request
import uuid
from typing import Dict, Any, Optional, Union, Callable

from .config import DEFAULT_DEVICE_CONFIG, load_customer_device
from .crypto import NetSecClient, compute_token_sign, aes128_cbc_decrypt
from .net import build_https_opener

# 上游 EdgeOne 会把偶发请求压到 20 秒以上；20 秒单次超时正好卡在限流区间。
REQUEST_TIMEOUT = 35.0
RETRY_DELAYS = (1.5, 3.0)


def _is_retryable_network_error(exc: BaseException) -> bool:
    """只有网络层异常可重试；服务端已给出 HTTP 状态码的错误不重试。"""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, OSError)
    # TimeoutError / ConnectionError / ssl.SSLError / RemoteDisconnected 都是 OSError
    return isinstance(exc, OSError)


class BaseNetSecClient:
    """运动世界校园 7.3.92 通用 NetSecKit 4.2.4 协议客户端基类"""

    def __init__(self,
                 uid: Optional[Union[int, str]] = None,
                 token: Optional[str] = None,
                 unid: Optional[Union[int, str]] = None,
                 device_config: Optional[Dict[str, Any]] = None):
        self.uid = str(uid) if uid is not None and str(uid) != "-1" else None
        self.token = str(token) if token else None
        self.unid = str(unid) if unid is not None else None
        
        # 初始化设备指纹
        self.device_config = load_customer_device()
        if device_config:
            self.device_config.update(device_config)

        self.crypto = NetSecClient()
        self.ssl_ctx = ssl.create_default_context()
        self.opener = build_https_opener(self.ssl_ctx)

    def set_credentials(self, uid: Optional[Union[int, str]], token: Optional[str], unid: Optional[Union[int, str]] = None):
        """动态设置或更新鉴权凭据"""
        if uid is not None and str(uid) != "-1":
            self.uid = str(uid)
        if token:
            self.token = str(token)
        if unid is not None:
            self.unid = str(unid)

    def build_headers(self, custom_headers: Optional[Dict[str, str]] = None, sign_params: Optional[Dict[str, str]] = None) -> tuple:
        """组装标准请求头并计算 headerSign 与 tokenSign"""
        now_ms = int(time.time() * 1000)
        nonce = str(uuid.uuid4())

        headers = dict(self.device_config)
        headers["timeStamp"] = str(now_ms)
        headers["nonce"] = nonce

        if self.uid:
            headers["uid"] = str(self.uid)
        if self.token:
            headers["token"] = str(self.token)

        if custom_headers:
            headers.update(custom_headers)

        # 1. 计算 tokenSign (仅在已有 token 或显式要求时签名)
        sign_dict = {
            "timeStamp": str(now_ms),
            "token": str(self.token or ""),
            "uid": str(self.uid or "")
        }
        if sign_params:
            sign_dict.update(sign_params)
        headers["tokenSign"] = compute_token_sign(sign_dict)

        # 2. 生成 headerSign (包含本会话密钥种子 kd1..kd4 并派生 h_resp_key)
        h_wire, h_resp_key = self.crypto.encrypt_request(
            json.dumps(headers, separators=(",", ":"), ensure_ascii=False)
        )
        headers["headerSign"] = json.dumps(h_wire, separators=(",", ":"))

        return headers, h_resp_key

    def execute_request(self,
                        method: str,
                        url: str,
                        body_plain: Optional[Union[dict, list]] = None,
                        custom_headers: Optional[Dict[str, str]] = None,
                        sign_params: Optional[Dict[str, str]] = None,
                        timeout: float = REQUEST_TIMEOUT,
                        retries: int = 0,
                        log: Optional[Callable[[str], None]] = None) -> Any:
        """发送加密网络请求并自动解密返回响应。

        ``retries`` 只给网络层可安全重复的请求（OBS 预签名、只读查询）；
        每次重试都会重新生成 nonce / timeStamp / headerSign。``/save/record``
        这类可能已写入服务端的请求不要自动重试。
        """
        attempts = max(1, 1 + int(retries))
        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                return self._execute_once(
                    method, url, body_plain, custom_headers, sign_params, timeout)
            except Exception as exc:
                last_error = exc
                if not _is_retryable_network_error(exc) or attempt >= attempts:
                    break
                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                if log:
                    log("    -> 网络异常（第 %d/%d 次）：%s；%.1fs 后重试..."
                        % (attempt, attempts, exc, delay))
                time.sleep(delay)
        return {"error": -1, "message": str(last_error)}

    def _execute_once(self,
                      method: str,
                      url: str,
                      body_plain: Optional[Union[dict, list]] = None,
                      custom_headers: Optional[Dict[str, str]] = None,
                      sign_params: Optional[Dict[str, str]] = None,
                      timeout: float = REQUEST_TIMEOUT) -> Any:
        """单次发送；网络层异常向上抛，由 ``execute_request`` 决定是否重试。"""
        headers, h_resp_key = self.build_headers(custom_headers, sign_params)

        method = method.upper()
        req_data = None
        if method == "POST" and body_plain is not None:
            b_wire, _ = self.crypto.encrypt_request(body_plain)
            req_data = json.dumps(b_wire, separators=(",", ":")).encode("utf-8")

        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                resp_obj = json.loads(raw)
                if "r" in resp_obj:
                    r_bytes = base64.b64decode(resp_obj["r"])
                    dec_h = aes128_cbc_decrypt(r_bytes, h_resp_key)
                    inner = json.loads(dec_h.decode("utf-8", errors="replace"))
                    if "data" in inner:
                        biz_b64 = inner["data"]
                        biz_bytes = base64.b64decode(biz_b64)
                        try:
                            return json.loads(biz_bytes.decode("utf-8", errors="replace"))
                        except Exception:
                            return biz_bytes.decode("utf-8", errors="replace")
                    return inner
                return resp_obj
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            try:
                return json.loads(err_body)
            except Exception:
                return {"error": e.code, "message": f"HTTP Error {e.code}: {e.reason}", "raw": err_body}
