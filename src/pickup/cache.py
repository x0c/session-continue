"""pickup 本地派生缓存：会话元数据与对话正文。

缓存只保存可从原始历史重建的数据。任何错误都应退化为缓存未命中，不能阻断扫描。
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pickup.models import ConversationMessage

SCHEMA_VERSION = 1
DEFAULT_MAX_MB = 256
_PARSER_VERSION = "2026-07-22.1"


def enabled() -> bool:
    return os.environ.get("PICKUP_CACHE", "1").strip().lower() not in {"0", "false", "no", "off"}


def cache_dir() -> Path:
    override = os.environ.get("PICKUP_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    root = os.environ.get("XDG_CACHE_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".cache") / "pickup"


def cache_path() -> Path:
    return cache_dir() / "performance-cache.sqlite3"


def max_bytes() -> int:
    try:
        value = int(os.environ.get("PICKUP_CACHE_MAX_MB", str(DEFAULT_MAX_MB)))
    except ValueError:
        value = DEFAULT_MAX_MB
    return max(16, value) * 1024 * 1024


def file_signature(path: str) -> tuple[int, int, int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class PerformanceCache:
    """多进程安全的 SQLite 派生缓存；失败时始终按未命中处理。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or cache_path()
        self._local = threading.local()
        self._pending_lock = threading.Lock()
        self._pending_sessions: list[tuple] = []

    @contextmanager
    def _connect(self, *, create: bool = True) -> Iterator[sqlite3.Connection | None]:
        if not enabled():
            yield None
            return
        conn = getattr(self._local, "connection", None)
        try:
            if create:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(self.path.parent, 0o700)
            elif not self.path.exists():
                yield None
                return
            if conn is None:
                conn = sqlite3.connect(self.path, timeout=0.08)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=80")
                self._local.connection = conn
                if create:
                    self._init_schema(conn)
                    try:
                        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
                    except OSError:
                        pass
        except (OSError, sqlite3.Error):
            yield None
            return
        try:
            yield conn
        except sqlite3.Error:
            # 缓存损坏、竞争或只读文件系统都只能造成未命中，不能影响原始会话读取。
            try:
                conn.rollback()
            except sqlite3.Error:
                pass

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_meta (
                runtime TEXT NOT NULL,
                path TEXT NOT NULL,
                dev INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                parser_version TEXT NOT NULL,
                payload TEXT NOT NULL,
                accessed_at REAL NOT NULL,
                PRIMARY KEY(runtime, path)
            );
            CREATE TABLE IF NOT EXISTS conversation (
                runtime TEXT NOT NULL,
                session_key TEXT NOT NULL,
                path TEXT NOT NULL,
                dev INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                parser_version TEXT NOT NULL,
                payload TEXT NOT NULL,
                accessed_at REAL NOT NULL,
                PRIMARY KEY(runtime, session_key)
            );
            CREATE INDEX IF NOT EXISTS conversation_lru ON conversation(accessed_at);
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

    def get_session(self, runtime: str, path: str, extra_version: str = "") -> dict | None:
        signature = file_signature(path)
        if signature is None:
            return None
        version = _PARSER_VERSION + extra_version
        row = None
        with self._connect() as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT dev, ino, size, mtime_ns, parser_version, payload "
                "FROM session_meta WHERE runtime=? AND path=?",
                (runtime, path),
            ).fetchone()
            if row is None or tuple(row[:4]) != signature or row[4] != version:
                return None
            try:
                payload = json.loads(row[5])
            except (TypeError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

    def put_session(self, runtime: str, path: str, payload: dict, extra_version: str = "") -> None:
        signature = file_signature(path)
        if signature is None:
            return
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return
        with self._pending_lock:
            self._pending_sessions.append(
                (runtime, path, *signature, _PARSER_VERSION + extra_version, encoded, time.time())
            )

    def flush_pending(self) -> None:
        """把扫描线程积累的元数据一次事务落盘，避免每条会话各做一次同步提交。"""
        with self._pending_lock:
            pending, self._pending_sessions = self._pending_sessions, []
        if not pending:
            return
        with self._connect() as conn:
            if conn is None:
                return
            conn.executemany(
                "INSERT OR REPLACE INTO session_meta "
                "(runtime,path,dev,ino,size,mtime_ns,parser_version,payload,accessed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                pending,
            )
            conn.commit()
        self.prune()

    def get_conversation(
        self, runtime: str, session_key: str, path: str,
    ) -> list[ConversationMessage] | None:
        signature = file_signature(path)
        if signature is None:
            return None
        row = None
        with self._connect() as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT dev, ino, size, mtime_ns, parser_version, payload "
                "FROM conversation WHERE runtime=? AND session_key=?",
                (runtime, session_key),
            ).fetchone()
            if row is None or tuple(row[:4]) != signature or row[4] != _PARSER_VERSION:
                return None
            try:
                raw = json.loads(row[5])
                messages = [
                    ConversationMessage(str(item[0]), str(item[1]), item[2])
                    for item in raw
                ]
            except (TypeError, ValueError, json.JSONDecodeError, IndexError):
                return None
            return messages

    def put_conversation(
        self, runtime: str, session_key: str, path: str, messages: list[ConversationMessage],
    ) -> None:
        signature = file_signature(path)
        if signature is None:
            return
        raw = [[item.role, item.text, item.timestamp] for item in messages]
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            if conn is None:
                return
            conn.execute(
                "INSERT OR REPLACE INTO conversation "
                "(runtime,session_key,path,dev,ino,size,mtime_ns,parser_version,payload,accessed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    runtime, session_key, path, *signature, _PARSER_VERSION,
                    encoded, time.time(),
                ),
            )
            conn.commit()
        self.prune()

    def prune(self) -> None:
        def total_size() -> int:
            return sum(
                candidate.stat().st_size
                for candidate in (
                    self.path,
                    Path(str(self.path) + "-wal"),
                    Path(str(self.path) + "-shm"),
                )
                if candidate.exists()
            )

        try:
            if not self.path.exists() or total_size() <= max_bytes():
                return
        except OSError:
            return
        with self._connect() as conn:
            if conn is None:
                return
            while self.path.exists() and total_size() > max_bytes():
                deleted = conn.execute(
                    "DELETE FROM conversation WHERE rowid IN "
                    "(SELECT rowid FROM conversation ORDER BY accessed_at LIMIT 64)"
                ).rowcount
                if not deleted:
                    break
                conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def status(self) -> dict:
        from pickup.native import available as native_available

        result = {
            "enabled": enabled(),
            "native_accelerator": native_available(),
            "path": str(self.path),
            "size_bytes": 0,
            "max_bytes": max_bytes(),
            "session_count": 0,
            "conversation_count": 0,
            "search_index_count": 0,
            "schema_version": SCHEMA_VERSION,
        }
        try:
            result["size_bytes"] = sum(
                path.stat().st_size
                for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"))
                if path.exists()
            )
        except OSError:
            pass
        with self._connect(create=False) as conn:
            if conn is not None:
                result["session_count"] = conn.execute("SELECT count(*) FROM session_meta").fetchone()[0]
                result["conversation_count"] = conn.execute("SELECT count(*) FROM conversation").fetchone()[0]
        return result

    def clear(self, *, dry_run: bool = False) -> dict:
        status = self.status()
        existed = bool(status["session_count"] or status["conversation_count"])
        if dry_run or not existed:
            return {"status": "would_clear" if dry_run and existed else "unchanged", **status}
        with self._connect(create=False) as conn:
            if conn is not None:
                conn.execute("DELETE FROM conversation")
                conn.execute("DELETE FROM session_meta")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"status": "cleared", **self.status()}


_CACHE = PerformanceCache()


def get_cache() -> PerformanceCache:
    return _CACHE
