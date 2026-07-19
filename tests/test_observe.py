"""pickup.observe.py：结构化事件日志（~/.cache/pickup/events.log）。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock


class ObserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cache_dir = self._tmpdir.name
        from pickup import observe

        self.observe = observe
        self._events_log = os.path.join(self.cache_dir, "events.log")
        self._embed_error_log = os.path.join(self.cache_dir, "embed-error.log")
        self._patchers = [
            mock.patch.object(observe, "CACHE_DIR", self.cache_dir),
            mock.patch.object(observe, "EVENTS_LOG", self._events_log),
            mock.patch.object(observe, "EMBED_ERROR_LOG", self._embed_error_log),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        observe.reset_for_tests()

    def _lines(self) -> list[dict]:
        if not os.path.isfile(self._events_log):
            return []
        out = []
        with open(self._events_log, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def test_event_writes_json_line_with_ts_and_name(self) -> None:
        self.observe.init(debug=False)
        self.observe.event("scan_done", duration_ms=12, runtime_count=2)
        rows = self._lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "scan_done")
        self.assertEqual(rows[0]["duration_ms"], 12)
        self.assertEqual(rows[0]["runtime_count"], 2)
        self.assertIn("ts", rows[0])

    def test_debug_silent_unless_debug_enabled(self) -> None:
        self.observe.init(debug=False)
        self.observe.debug("osc_probe", ok=True)
        self.assertEqual(self._lines(), [])
        self.observe.init(debug=True)
        self.observe.debug("osc_probe", ok=True)
        rows = self._lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "osc_probe")
        self.assertEqual(rows[0]["level"], "debug")

    def test_redacts_sensitive_fields(self) -> None:
        self.observe.init(debug=False)
        self.observe.event(
            "x",
            text="secret dialogue",
            prompt="do not log",
            messages=[{"role": "user"}],
            runtime="claude",
        )
        row = self._lines()[0]
        self.assertEqual(row["text"], "<redacted>")
        self.assertEqual(row["prompt"], "<redacted>")
        self.assertEqual(row["messages"], "<redacted>")
        self.assertEqual(row["runtime"], "claude")

    def test_truncates_when_over_size_cap(self) -> None:
        self.observe.init(debug=False)
        # 写成超过上限，再追加一条仍应成功且文件不大爆炸。
        with open(self._events_log, "w", encoding="utf-8") as fh:
            fh.write("x" * (256 * 1024 + 10))
        self.observe.event("after_truncate", ok=True)
        self.assertLessEqual(os.path.getsize(self._events_log), 256 * 1024 + 512)
        rows = self._lines()
        self.assertTrue(any(r.get("name") == "after_truncate" for r in rows))

    def test_timed_records_duration_ms(self) -> None:
        self.observe.init(debug=False)
        with self.observe.timed("list_rebuild", mode="full"):
            time.sleep(0.01)
        row = self._lines()[0]
        self.assertEqual(row["name"], "list_rebuild")
        self.assertEqual(row["mode"], "full")
        self.assertGreaterEqual(row["duration_ms"], 5)

    def test_log_exception_writes_events_and_embed_error(self) -> None:
        self.observe.init(debug=False)
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            self.observe.log_exception("抓帧", exc)
        rows = self._lines()
        self.assertEqual(rows[0]["name"], "error")
        self.assertEqual(rows[0]["where"], "抓帧")
        self.assertEqual(rows[0]["exc_type"], "RuntimeError")
        self.assertNotIn("traceback", rows[0])
        with open(self._embed_error_log, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("抓帧", body)
        self.assertIn("RuntimeError", body)
        self.assertIn("boom", body)
        self.assertIn("Traceback", body)


class PickupEmbedErrorBridgeTests(unittest.TestCase):
    """pickup._log_embed_error 必须转调 observe（双写 events + embed-error）。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        cache = self._tmpdir.name
        from pickup import observe
        import pickup

        self.observe = observe
        self.pickup = pickup
        self.events = os.path.join(cache, "events.log")
        self.embed_err = os.path.join(cache, "embed-error.log")
        for p in (
            mock.patch.object(observe, "CACHE_DIR", cache),
            mock.patch.object(observe, "EVENTS_LOG", self.events),
            mock.patch.object(observe, "EMBED_ERROR_LOG", self.embed_err),
            mock.patch.object(pickup.titles, "CACHE_FILE", os.path.join(cache, "pickup.titles.json")),
        ):
            p.start()
            self.addCleanup(p.stop)
        observe.reset_for_tests()
        observe.init(debug=False)

    def test_log_embed_error_writes_events_and_traceback_file(self) -> None:
        try:
            raise RuntimeError("x")
        except RuntimeError as exc:
            self.pickup._log_embed_error("抓帧", exc)
        with open(self.events, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(rows[0]["name"], "error")
        self.assertEqual(rows[0]["where"], "抓帧")
        with open(self.embed_err, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("抓帧", body)
        self.assertIn("Traceback", body)


class InstrumentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        cache = self._tmpdir.name
        from pickup import observe

        self.observe = observe
        self.events = os.path.join(cache, "events.log")
        for p in (
            mock.patch.object(observe, "CACHE_DIR", cache),
            mock.patch.object(observe, "EVENTS_LOG", self.events),
            mock.patch.object(observe, "EMBED_ERROR_LOG", os.path.join(cache, "embed-error.log")),
        ):
            p.start()
            self.addCleanup(p.stop)
        observe.reset_for_tests()
        observe.init(debug=False)

    def test_scan_all_event_from_store_refresh(self) -> None:
        import pickup

        runtime = mock.Mock(id="claude", display_name="Claude")
        runtime.scan_signature.return_value = None
        runtime.scan_sessions.return_value = []
        registry = pickup.RuntimeRegistry((runtime,))
        with mock.patch.object(pickup.titles, "load_cache", return_value={}), mock.patch.object(
            pickup.keepalive, "annotate"
        ):
            store = pickup.SessionStore(limit=5, registry=registry)
            store.load()
            store.refresh()
        with open(self.events, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        scan_rows = [r for r in rows if r.get("name") == "scan_all"]
        self.assertGreaterEqual(len(scan_rows), 2)
        self.assertIn("duration_ms", scan_rows[0])
        self.assertEqual(scan_rows[0]["session_count"], 0)


if __name__ == "__main__":
    unittest.main()
