"""性能缓存与维护命令测试；全部使用临时目录，不碰真实用户缓存。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pickup.cache import PerformanceCache
from pickup.models import ConversationMessage


class PerformanceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "cache.sqlite3"
        self.cache = PerformanceCache(self.path)
        self.env = mock.patch.dict(os.environ, {"PICKUP_CACHE": "1"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_session_cache_invalidates_on_append(self):
        history = Path(self.temp.name) / "session.jsonl"
        history.write_text("{}\n", encoding="utf-8")
        payload = {"source": "claude", "id": "abc", "live": False}
        self.cache.put_session("claude", str(history), payload)
        self.cache.flush_pending()
        self.assertEqual(self.cache.get_session("claude", str(history)), payload)
        with history.open("a", encoding="utf-8") as file:
            file.write("{}\n")
        self.assertIsNone(self.cache.get_session("claude", str(history)))

    def test_conversation_round_trip_and_clear_is_idempotent(self):
        history = Path(self.temp.name) / "session.jsonl"
        history.write_text("{}\n", encoding="utf-8")
        messages = [ConversationMessage("user", "你好", 123.0)]
        self.cache.put_conversation("claude", "claude:abc", str(history), messages)
        self.assertEqual(self.cache.get_conversation("claude", "claude:abc", str(history)), messages)
        self.assertEqual(self.cache.clear()["status"], "cleared")
        self.assertEqual(self.cache.clear()["status"], "unchanged")

    def test_dry_run_does_not_change_database(self):
        history = Path(self.temp.name) / "session.jsonl"
        history.write_text("{}\n", encoding="utf-8")
        self.cache.put_session("claude", str(history), {"id": "abc"})
        self.cache.flush_pending()
        before = self.cache.status()
        result = self.cache.clear(dry_run=True)
        after = self.cache.status()
        self.assertEqual(result["status"], "would_clear")
        self.assertEqual(before["session_count"], after["session_count"])

    def test_corrupt_database_degrades_to_cache_miss(self):
        self.path.write_bytes("这不是 SQLite 数据库".encode("utf-8"))
        history = Path(self.temp.name) / "session.jsonl"
        history.write_text("{}\n", encoding="utf-8")
        broken = PerformanceCache(self.path)
        self.assertIsNone(broken.get_session("claude", str(history)))
        self.assertEqual(broken.status()["session_count"], 0)


class CacheCliTests(unittest.TestCase):
    def _run(self, *args: str):
        env = dict(os.environ)
        env["PICKUP_CACHE_DIR"] = self.temp.name
        return subprocess.run(
            [sys.executable, "-m", "pickup", "cache", *args],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_status_json_uses_agent_envelope(self):
        result = self._run("status", "--json")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {"ok", "data", "error", "meta"})
        self.assertTrue(payload["ok"])

    def test_usage_error_returns_two_without_hanging(self):
        result = self._run("unknown", "--json")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(result.stderr, "")
