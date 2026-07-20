"""活跃会话分屏组合记忆：同一项目下最多三格，切走再回来自动恢复同伴。

持久化到 ~/.cache/pickup/split-layout.json，原子写（模式同 titles.save_cache）。
仅记录活跃/托管会话；已结束会话不参与组合恢复。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from pickup.titles import CACHE_DIR

LAYOUT_FILE = os.path.join(CACHE_DIR, "split-layout.json")
LAYOUT_VERSION = 1
MAX_PANES = 3


@dataclass
class SplitGroup:
    """同一项目下的一组活跃会话分屏。"""

    group_id: str
    project_cwd: str
    session_keys: list[str]
    focus_key: str | None = None


@dataclass
class SplitLayoutStore:
    """内存中的分屏布局；读写磁盘时整体序列化。"""

    version: int = LAYOUT_VERSION
    last_project: str = ""
    last_focus_key: str = ""
    groups: dict[str, SplitGroup] = field(default_factory=dict)
    session_to_group: dict[str, str] = field(default_factory=dict)

    def get_group(self, session_key: str) -> SplitGroup | None:
        gid = self.session_to_group.get(session_key)
        if not gid:
            return None
        return self.groups.get(gid)

    def group_session_keys(self, session_key: str) -> list[str]:
        group = self.get_group(session_key)
        if group is None:
            return [session_key]
        return list(group.session_keys)

    def set_group(
        self,
        project_cwd: str,
        session_keys: list[str],
        *,
        focus_key: str | None = None,
    ) -> None:
        """写入或更新组合；session_keys 去重保序，最多 MAX_PANES 个。"""
        keys: list[str] = []
        seen: set[str] = set()
        for key in session_keys:
            if key and key not in seen:
                keys.append(key)
                seen.add(key)
        if not keys:
            return
        if len(keys) > MAX_PANES:
            keys = keys[:MAX_PANES]
        focus = focus_key if focus_key in keys else keys[0]
        self._drop_sessions_from_other_groups(keys)
        # 若已有成员属于同一组合且成员集合一致，复用 group_id
        existing_gid = self.session_to_group.get(keys[0])
        if existing_gid:
            existing = self.groups.get(existing_gid)
            if existing and set(existing.session_keys) == set(keys):
                existing.session_keys = keys
                existing.focus_key = focus
                existing.project_cwd = project_cwd
                self.last_project = project_cwd
                self.last_focus_key = focus or ""
                self._reindex_group(existing_gid)
                return
        gid = existing_gid or str(uuid.uuid4())
        self.groups[gid] = SplitGroup(
            group_id=gid,
            project_cwd=project_cwd,
            session_keys=keys,
            focus_key=focus,
        )
        self.last_project = project_cwd
        self.last_focus_key = focus or ""
        self._reindex_group(gid)

    def remove_session(self, session_key: str) -> None:
        """从组合中移除单个会话；组合空则删组。"""
        gid = self.session_to_group.pop(session_key, None)
        if not gid:
            return
        group = self.groups.get(gid)
        if group is None:
            return
        group.session_keys = [k for k in group.session_keys if k != session_key]
        if not group.session_keys:
            del self.groups[gid]
            return
        if group.focus_key == session_key:
            group.focus_key = group.session_keys[0]
        self._reindex_group(gid)

    def prune_inactive(self, is_active: Callable[[str], bool]) -> None:
        """剔除不再活跃/托管的会话键。"""
        dead: list[str] = []
        for key in list(self.session_to_group):
            if not is_active(key):
                dead.append(key)
        for key in dead:
            self.remove_session(key)

    def _drop_sessions_from_other_groups(self, keys: list[str]) -> None:
        """新组合写入前，把这些键从旧组摘掉（避免一键多组）。"""
        for key in keys:
            old_gid = self.session_to_group.get(key)
            if not old_gid:
                continue
            group = self.groups.get(old_gid)
            if group is None:
                continue
            if set(group.session_keys) == set(keys):
                continue
            group.session_keys = [k for k in group.session_keys if k != key]
            if not group.session_keys:
                del self.groups[old_gid]
            else:
                if group.focus_key == key:
                    group.focus_key = group.session_keys[0]
                self._reindex_group(old_gid)

    def _reindex_group(self, gid: str) -> None:
        group = self.groups.get(gid)
        if group is None:
            return
        for key in list(self.session_to_group):
            if self.session_to_group.get(key) == gid and key not in group.session_keys:
                del self.session_to_group[key]
        for key in group.session_keys:
            self.session_to_group[key] = gid


def load_layout() -> SplitLayoutStore:
    try:
        with open(LAYOUT_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return SplitLayoutStore()
    if not isinstance(raw, dict):
        return SplitLayoutStore()
    store = SplitLayoutStore(version=int(raw.get("version") or LAYOUT_VERSION))
    store.last_project = str(raw.get("last_project") or "")
    store.last_focus_key = str(raw.get("last_focus_key") or "")
    groups_raw = raw.get("groups") or {}
    if isinstance(groups_raw, dict):
        for gid, g in groups_raw.items():
            if not isinstance(g, dict):
                continue
            keys = g.get("session_keys") or []
            if not isinstance(keys, list):
                continue
            session_keys = [str(k) for k in keys if k][:MAX_PANES]
            if not session_keys:
                continue
            store.groups[str(gid)] = SplitGroup(
                group_id=str(gid),
                project_cwd=str(g.get("project_cwd") or ""),
                session_keys=session_keys,
                focus_key=str(g["focus_key"]) if g.get("focus_key") else None,
            )
    index = raw.get("session_to_group") or {}
    if isinstance(index, dict):
        for sk, gid in index.items():
            if sk in store.groups or any(sk in g.session_keys for g in store.groups.values()):
                store.session_to_group[str(sk)] = str(gid)
    # 索引与 groups 不一致时以 groups 为准重建
    store.session_to_group.clear()
    for gid, group in store.groups.items():
        store._reindex_group(gid)
    return store


def save_layout(store: SplitLayoutStore) -> None:
    """原子写分屏布局文件。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    payload = {
        "version": store.version,
        "last_project": store.last_project,
        "last_focus_key": store.last_focus_key,
        "groups": {
            gid: {
                "project_cwd": g.project_cwd,
                "session_keys": g.session_keys,
                "focus_key": g.focus_key,
            }
            for gid, g in store.groups.items()
        },
        "session_to_group": dict(store.session_to_group),
    }
    tmp_path = LAYOUT_FILE + f".tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, LAYOUT_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def resolve_active_group(
    store: SplitLayoutStore,
    session_key: str,
    *,
    is_active: Callable[[str], bool],
    find_session: Callable[[str], dict | None],
) -> tuple[str, list[str]]:
    """解析选中会话应恢复的分屏组合。

    返回 (project_cwd, ordered_session_keys)；同伴已不活跃则降级为单格。
    """
    group = store.get_group(session_key)
    if group is None:
        session = find_session(session_key)
        project = ""
        if session:
            from pickup.display import _normalize_cwd

            project = _normalize_cwd(session.get("cwd"))
        return project, [session_key]
    alive = [k for k in group.session_keys if is_active(k)]
    if session_key not in alive:
        alive = [session_key] if is_active(session_key) else []
    if not alive:
        return group.project_cwd, [session_key]
    if session_key in alive and len(alive) == 1:
        return group.project_cwd, alive
    # 保持原顺序，只留活跃成员
    ordered = [k for k in group.session_keys if k in alive]
    if session_key in ordered:
        # 聚焦项不变，顺序保持
        pass
    elif session_key in alive:
        ordered = [session_key] + [k for k in ordered if k != session_key]
    return group.project_cwd, ordered or [session_key]
