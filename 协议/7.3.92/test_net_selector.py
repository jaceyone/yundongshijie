# -*- coding: utf-8 -*-
"""边缘节点选择与地址级回退的单测（不联网）。"""
import http.client
import socket
import unittest
from unittest import mock

from core import net


BAD_IP = "10.0.0.1"
GOOD_IP = "10.0.0.2"
INFOS = [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (BAD_IP, 443)),
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (GOOD_IP, 443)),
]


class FakeSocket:
    def __init__(self, af, socktype, proto):
        self.connected = None
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, address):
        self.connected = address

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, bad_ips):
        self.bad_ips = set(bad_ips)
        self.sni = None

    def wrap_socket(self, sock, server_hostname=None):
        if sock.connected[0] in self.bad_ips:
            raise ConnectionResetError(10054, "远端强制关闭")
        self.sni = server_hostname
        return sock


class NodeSelectorTests(unittest.TestCase):
    def test_cached_ip_moves_to_front(self):
        selector = net.NodeSelector(ttl=60)
        with mock.patch("core.net.socket.getaddrinfo", return_value=INFOS):
            self.assertEqual(selector.candidates("h", 443)[0][4][0], BAD_IP)
            selector.remember("h", GOOD_IP)
            self.assertEqual(selector.candidates("h", 443)[0][4][0], GOOD_IP)

    def test_cached_ip_expires(self):
        selector = net.NodeSelector(ttl=0)
        selector.remember("h", GOOD_IP)
        self.assertIsNone(selector.cached_ip("h"))

    def test_invalidate_only_matching_ip(self):
        selector = net.NodeSelector(ttl=60)
        selector.remember("h", GOOD_IP)
        selector.invalidate("h", BAD_IP)
        self.assertEqual(selector.cached_ip("h"), GOOD_IP)
        selector.invalidate("h")
        self.assertIsNone(selector.cached_ip("h"))


class EdgeConnectionTests(unittest.TestCase):
    def _connect(self, bad_ips=(BAD_IP,), candidates=INFOS, timeout=20):
        selector = net.NodeSelector(ttl=60)
        with mock.patch("core.net._SELECTOR", selector), \
                mock.patch.object(selector, "candidates", return_value=candidates), \
                mock.patch("core.net.socket.socket", side_effect=FakeSocket):
            conn = net.EdgeHTTPSConnection("run.example.com", timeout=timeout)
            conn._context = FakeContext(bad_ips)
            conn.connect()
        return conn, selector

    def test_skips_tls_failing_node_and_remembers_good_one(self):
        conn, selector = self._connect()
        self.assertEqual(conn.sock.connected, (GOOD_IP, 443))
        self.assertEqual(selector.cached_ip("run.example.com"), GOOD_IP)

    def test_tries_cached_good_node_first(self):
        selector = net.NodeSelector(ttl=60)
        selector.remember("run.example.com", GOOD_IP)
        with mock.patch("core.net._SELECTOR", selector), \
                mock.patch.object(selector, "candidates", return_value=INFOS), \
                mock.patch("core.net.socket.socket", side_effect=FakeSocket):
            conn = net.EdgeHTTPSConnection("run.example.com", timeout=20)
            conn._context = FakeContext((BAD_IP,))
            conn.connect()
        self.assertEqual(conn.sock.connected, (GOOD_IP, 443))

    def test_all_nodes_fail_raises_and_clears_cache(self):
        selector = net.NodeSelector(ttl=60)
        selector.remember("run.example.com", GOOD_IP)
        with mock.patch("core.net._SELECTOR", selector), \
                mock.patch.object(selector, "candidates", return_value=INFOS), \
                mock.patch("core.net.socket.socket", side_effect=FakeSocket):
            conn = net.EdgeHTTPSConnection("run.example.com", timeout=20)
            conn._context = FakeContext((BAD_IP, GOOD_IP))
            with self.assertRaises(OSError):
                conn.connect()
        self.assertIsNone(selector.cached_ip("run.example.com"))

    def test_proxy_tunnel_falls_back_to_raw_connect(self):
        conn = net.EdgeHTTPSConnection("run.example.com", timeout=20)
        conn._tunnel_host = "proxy.local"
        with mock.patch.object(http.client.HTTPSConnection, "connect") as raw:
            conn.connect()
        raw.assert_called_once()


if __name__ == "__main__":
    unittest.main()
