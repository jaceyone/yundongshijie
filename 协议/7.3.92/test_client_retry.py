# -*- coding: utf-8 -*-
"""通用请求重试与 OBS 预签名申请重试的单测（不联网）。"""
import socket
import unittest
import urllib.error
import urllib.request
from unittest import mock

from core.client import BaseNetSecClient, _is_retryable_network_error
from running.client import RunningClient

URL = "https://run.example.com/api/obs/temporary/url"
OK = {"error": 10000, "data": {"signedUrl": "https://obs.example.com/x"}}


def reset_error():
    return ConnectionResetError(10054, "远端强制关闭")


class RetryClassificationTests(unittest.TestCase):
    def test_network_errors_are_retryable(self):
        for exc in (TimeoutError("timed out"),
                    ConnectionResetError(10054, "reset"),
                    socket.gaierror("dns"),
                    urllib.error.URLError(ConnectionResetError(10054, "reset"))):
            self.assertTrue(_is_retryable_network_error(exc), exc)

    def test_http_error_and_value_error_are_not_retryable(self):
        http_error = urllib.error.HTTPError(URL, 500, "err", {}, None)
        self.assertFalse(_is_retryable_network_error(http_error))
        self.assertFalse(_is_retryable_network_error(ValueError("bad json")))


class ExecuteRequestRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = BaseNetSecClient()

    def test_retries_reset_then_succeeds(self):
        with mock.patch.object(
                self.client, "_execute_once",
                side_effect=[reset_error(), OK]) as once, \
                mock.patch("core.client.time.sleep") as sleep:
            result = self.client.execute_request("POST", URL, body_plain={}, retries=2)
        self.assertEqual(result, OK)
        self.assertEqual(once.call_count, 2)
        sleep.assert_called_once_with(1.5)

    def test_exhausts_attempts_and_returns_last_error(self):
        with mock.patch.object(
                self.client, "_execute_once",
                side_effect=reset_error()) as once, \
                mock.patch("core.client.time.sleep"):
            result = self.client.execute_request("POST", URL, body_plain={}, retries=2)
        self.assertEqual(once.call_count, 3)
        self.assertEqual(result["error"], -1)
        self.assertIn("10054", result["message"])

    def test_business_exception_is_not_retried(self):
        with mock.patch.object(
                self.client, "_execute_once",
                side_effect=ValueError("bad payload")) as once:
            result = self.client.execute_request("POST", URL, body_plain={}, retries=2)
        self.assertEqual(once.call_count, 1)
        self.assertEqual(result["error"], -1)
        self.assertIn("bad payload", result["message"])

    def test_default_timeout_is_raised_to_35(self):
        with mock.patch.object(self.client, "_execute_once", return_value=OK) as once:
            self.client.execute_request("POST", URL, body_plain={})
        self.assertEqual(once.call_args.args[-1], 35.0)


class ObsUrlRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = RunningClient(
            uid=1, token="DUMMY", unid=17601,
            device_config={"DeviceId": "1" * 32})

    def test_upload_url_uses_three_attempts_and_parses_data(self):
        response = {"error": 10000, "data": '{"signedUrl": "https://obs.example.com/x"}'}
        with mock.patch.object(
                self.client, "execute_request", return_value=response) as request:
            result = self.client.request_obs_upload_url("run_data/x.json")
        self.assertEqual(result["data"]["signedUrl"], "https://obs.example.com/x")
        self.assertEqual(request.call_args.kwargs["retries"], 2)

    def test_download_url_is_retried_too(self):
        response = {"error": 10000, "data": {"signedUrl": "https://obs.example.com/y"}}
        with mock.patch.object(
                self.client, "execute_request", return_value=response) as request:
            result = self.client.request_obs_download_url("run_data/x.json")
        self.assertEqual(result["data"]["signedUrl"], "https://obs.example.com/y")
        self.assertEqual(request.call_args.kwargs["retries"], 2)

    def test_failure_message_is_passed_through(self):
        failed = {"error": -1, "message": "<urlopen error [WinError 10054] ...>"}
        with mock.patch.object(self.client, "execute_request", return_value=failed):
            result = self.client.request_obs_upload_url("run_data/x.json")
        self.assertEqual(result["error"], -1)
        self.assertIn("10054", result["message"])


if __name__ == "__main__":
    unittest.main()
