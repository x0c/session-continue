"""split_layout 分屏组合记忆单测。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from pickup import split_layout


class SplitLayoutStoreTests(unittest.TestCase):
    def test_set_group_and_lookup(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/tmp/proj", ["claude:a", "codex:b"], focus_key="codex:b")
        group = store.get_group("claude:a")
        self.assertIsNotNone(group)
        assert group is not None
        self.assertEqual(group.session_keys, ["claude:a", "codex:b"])
        self.assertEqual(store.get_group("codex:b"), group)

    def test_max_three_panes(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group(
            "/p",
            ["claude:1", "codex:2", "kimi:3", "cursor:4"],
        )
        group = store.get_group("claude:1")
        assert group is not None
        self.assertEqual(len(group.session_keys), split_layout.MAX_PANES)

    def test_remove_session_shrinks_group(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/p", ["claude:a", "codex:b"])
        store.remove_session("codex:b")
        self.assertIsNone(store.get_group("codex:b"))
        group = store.get_group("claude:a")
        assert group is not None
        self.assertEqual(group.session_keys, ["claude:a"])

    def test_prune_inactive(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/p", ["claude:a", "codex:b"])
        store.prune_inactive(lambda k: k == "claude:a")
        group = store.get_group("claude:a")
        assert group is not None
        self.assertEqual(group.session_keys, ["claude:a"])

    def test_resolve_active_group_degrades_dead_mates(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/p", ["claude:a", "codex:b"])
        sessions = {
            "claude:a": {"cwd": "/p", "keepalive_name": "n1"},
            "codex:b": {"cwd": "/p"},
        }

        def is_active(k: str) -> bool:
            return k == "claude:a"

        def find_session(k: str) -> dict | None:
            return sessions.get(k)

        project, keys = split_layout.resolve_active_group(
            store, "claude:a", is_active=is_active, find_session=find_session,
        )
        self.assertEqual(project, "/p")
        self.assertEqual(keys, ["claude:a"])

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "split-layout.json")
            with mock.patch.object(split_layout, "LAYOUT_FILE", path):
                with mock.patch.object(split_layout, "CACHE_DIR", td):
                    store = split_layout.SplitLayoutStore()
                    store.set_group("/proj", ["claude:x", "codex:y"], focus_key="claude:x")
                    split_layout.save_layout(store)
                    loaded = split_layout.load_layout()
                    group = loaded.get_group("codex:y")
                    assert group is not None
                    self.assertEqual(group.session_keys, ["claude:x", "codex:y"])
                    self.assertEqual(loaded.last_project, "/proj")


if __name__ == "__main__":
    unittest.main()
