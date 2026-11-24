# -*- coding: utf-8 -*-
"""学校规则接口的重试与失败原因记录。"""
import unittest
from unittest import mock

from policy.client import PolicyClient
from policy.models import RunRule

RULE_RESPONSE = {
    "error": 10000,
    "data": {
        "runRuleModel": {
            "minDistance": 1000,
            "dayGoal": 3000,
            "speedTop": "10'",
            "speedBottom": "3'",
            "stepTop": 0,
            "stepBottom": 0,
            "validTime": [],
        }
    },
}

TIMEOUT_RESPONSE = {"error": -1, "message": "timed out"}


def make_client():
    return PolicyClient(
        uid=1, token="DUMMY", unid=17601,
        device_config={"DeviceId": "1" * 32},
    )


class PolicyRetryTests(unittest.TestCase):
    def test_transient_timeout_is_retried_and_recovers(self):
        client = make_client()
        responses = [TIMEOUT_RESPONSE, TIMEOUT_RESPONSE, RULE_RESPONSE]
        with mock.patch.object(
                client, "execute_request", side_effect=responses) as request, \
                mock.patch("policy.client.time.sleep"):
            rule = client.get_run_policy(run_mode=1)
        self.assertIsInstance(rule, RunRule)
        self.assertEqual(rule.min_distance, 1000)
        self.assertEqual(request.call_count, 3)
        self.assertIsNone(client.last_policy_error)

    def test_all_attempts_fail_reports_last_reason(self):
        client = make_client()
        with mock.patch.object(
                client, "execute_request", return_value=TIMEOUT_RESPONSE) as request, \
                mock.patch("policy.client.time.sleep"):
            rule = client.get_run_policy(run_mode=1)
        self.assertIsNone(rule)
        self.assertEqual(request.call_count, PolicyClient.POLICY_ATTEMPTS)
        self.assertIn("timed out", client.last_policy_error)
        self.assertIn("3/3", client.last_policy_error)

    def test_unauthorized_is_not_retried(self):
        client = make_client()
        response = {"status": 401, "error": "Unauthorized", "message": "未登录用户"}
        with mock.patch.object(
                client, "execute_request", return_value=response) as request, \
                mock.patch("policy.client.time.sleep"):
            rule = client.get_run_policy(run_mode=1)
        self.assertIsNone(rule)
        self.assertEqual(request.call_count, 1)
        self.assertIn("HTTP 401", client.last_policy_error)

    def test_empty_data_is_retried(self):
        client = make_client()
        responses = [{"error": 10000, "data": ""}, RULE_RESPONSE]
        with mock.patch.object(
                client, "execute_request", side_effect=responses) as request, \
                mock.patch("policy.client.time.sleep"):
            rule = client.get_run_policy(run_mode=1)
        self.assertIsInstance(rule, RunRule)
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
