# -*- coding: utf-8 -*-
"""网关多边缘节点选择与地址级回退。

背景：``run.gxapp.iydsj.com`` 走腾讯 EdgeOne，DNS 会轮换多个边缘 IP。
实测其中偶有节点「TCP 能连上、TLS 握手卡到约 19 秒后断开」。Python 标准库
的 ``socket.create_connection`` 只在 TCP 阶段失败时换下一个地址，TLS 阶段
失败不会回退，于是请求直接失败。

这里把「TCP + TLS」放进同一个逐地址循环：单地址只给 4 秒，失败就换下一个；
最近一次成功的 IP 会在进程内缓存 5 分钟，后续请求优先复用它。

不改变证书校验与 SNI：连接目标可以是 IP，但 ``server_hostname`` 始终是域名。
代理隧道（HTTP CONNECT）场景直接回退到原生实现。
"""
import http.client
import os
import socket
import ssl
import threading
import time
import urllib.request

# 单地址 TCP + TLS 预算。健康节点实测 0.01–0.06s，坏节点会卡 19s。
PER_ADDRESS_TIMEOUT = 4.0
NODE_TTL_SEC = 300.0
ENABLE_ENV = "CAMPUSRUN_NODE_PIN"


def _enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "1").lower() not in ("0", "false", "no")


class NodeSelector:
    """按主机记录最近一次握手成功的边缘 IP（进程内缓存）。"""

    def __init__(self, ttl: float = NODE_TTL_SEC):
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._good = {}

    def cached_ip(self, host: str):
        with self._lock:
            entry = self._good.get(host)
            if entry is None:
                return None
            ip, expires_at = entry
            if expires_at <= time.monotonic():
                self._good.pop(host, None)
                return None
            return ip

    def remember(self, host: str, ip: str) -> None:
        with self._lock:
            self._good[host] = (str(ip), time.monotonic() + self.ttl)

    def invalidate(self, host: str, ip=None) -> None:
        with self._lock:
            entry = self._good.get(host)
            if entry is None:
                return
            if ip is None or entry[0] == str(ip):
                self._good.pop(host, None)

    def candidates(self, host: str, port: int):
        """返回去重后的 getaddrinfo 结果，缓存的可用 IP 排在最前。"""
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        seen = set()
        ordered = []
        for info in infos:
            key = (info[0], info[4])
            if key in seen:
                continue
            seen.add(key)
            ordered.append(info)
        good = self.cached_ip(host)
        if good:
            ordered.sort(key=lambda info: 0 if info[4][0] == good else 1)
        return ordered


_SELECTOR = NodeSelector()


def get_selector() -> NodeSelector:
    return _SELECTOR


class EdgeHTTPSConnection(http.client.HTTPSConnection):
    """逐地址完成 TCP + TLS；TLS 卡死或断连时换下一个边缘节点。"""

    per_address_timeout = PER_ADDRESS_TIMEOUT

    def connect(self):
        # 走代理时由原生 CONNECT 隧道处理，不在这里直连 IP。
        if self._tunnel_host or not _enabled():
            return super().connect()

        last_error = None
        tried = []
        for af, socktype, proto, _canon, sockaddr in _SELECTOR.candidates(self.host, self.port):
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                if self.source_address:
                    sock.bind(self.source_address)
                limit = self.per_address_timeout
                if self.timeout is not None:
                    limit = min(limit, float(self.timeout))
                sock.settimeout(limit)
                sock.connect(sockaddr)
                sock.settimeout(self.timeout)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                _SELECTOR.remember(self.host, sockaddr[0])
                return
            except OSError as exc:  # 包含 SSLError / TimeoutError / ConnectionResetError
                last_error = exc
                tried.append(sockaddr[0])
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        _SELECTOR.invalidate(self.host)
        if last_error is None:
            last_error = OSError(
                "no usable edge node for %s (tried %s)"
                % (self.host, ", ".join(tried) or "none"))
        raise last_error


class EdgeHTTPSHandler(urllib.request.HTTPSHandler):
    """让 urllib 使用逐地址回退的 HTTPSConnection。"""

    def https_open(self, req):
        return self.do_open(EdgeHTTPSConnection, req, context=self._context)


def build_https_opener(context=None):
    """构造带地址回退的 urllib opener（保留代理、重定向、错误处理）。"""
    ctx = context or ssl.create_default_context()
    return urllib.request.build_opener(EdgeHTTPSHandler(context=ctx))
