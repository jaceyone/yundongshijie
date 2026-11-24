"""Offline tests for the run submission outbox."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from running.outbox import (
    Outbox,
    OutboxError,
    STATUS_FAILED,
    STATUS_OBS_UPLOADED,
    STATUS_PENDING,
    STATUS_SAVED,
)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outbox = Outbox(self.tmp.name)

    def write(self, uuid_str="11111111-2222-3333-4444-555555555555"):
        return self.outbox.write_pending(
            uuid_str,
            save_record={"uuid": uuid_str, "totalDis": 3000},
            obs_payload={"run_data": "payload"},
            object_key=f"run_data/2026010112/{uuid_str}.json",
            created_at_ms=1700000000000,
        )

    def test_write_load_update_and_find(self):
        path = self.write()
        self.assertTrue(path.is_file())
        entry = self.outbox.load("11111111-2222-3333-4444-555555555555")
        self.assertEqual(entry["status"], STATUS_PENDING)
        self.assertEqual(entry["created_at_ms"], 1700000000000)
        self.assertEqual(entry["save_record"]["totalDis"], 3000)
        self.assertEqual(list(Path(self.tmp.name).glob("*.tmp")), [])

        self.outbox.mark(entry["uuid"], STATUS_OBS_UPLOADED)
        self.outbox.mark(entry["uuid"], STATUS_SAVED, rrid=1358423253)
        found = self.outbox.find_by_rrid(1358423253)
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], STATUS_SAVED)
        self.assertEqual(found["rrid"], 1358423253)

    def test_failed_state_records_error(self):
        self.write()
        self.outbox.mark("11111111-2222-3333-4444-555555555555",
                         STATUS_FAILED, error="OBS HTTP 403")
        entry = self.outbox.load("11111111-2222-3333-4444-555555555555")
        self.assertEqual(entry["status"], STATUS_FAILED)
        self.assertEqual(entry["last_error"], "OBS HTTP 403")

    def test_rejects_path_traversal_and_missing_entry(self):
        for bad in ("../../etc/passwd", "a/b", "", "short"):
            with self.subTest(bad=bad), self.assertRaises(OutboxError):
                self.outbox.path_for(bad)
        with self.assertRaises(OutboxError):
            self.outbox.load("66666666-7777-8888-9999-000000000000")

    def test_iter_entries_skips_corrupt_files(self):
        self.write()
        (Path(self.tmp.name) / "broken.json").write_text("{", encoding="utf-8")
        entries = list(self.outbox.iter_entries())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["uuid"], "11111111-2222-3333-4444-555555555555")

    def test_unknown_status_rejected(self):
        self.write()
        with self.assertRaises(OutboxError):
            self.outbox.update("11111111-2222-3333-4444-555555555555",
                               status="not-a-status")


if __name__ == "__main__":
    unittest.main()
