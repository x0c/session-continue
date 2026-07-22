"""会话仓库：扫描结果、标题缓存轮询、对话预览缓存与托管标注。"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

from pickup import embed, keepalive, titles
from pickup.cache import get_cache
from pickup.display import (
    _filter_sessions_by_query,
    _normalize_cwd,
    _project_groups,
)
from pickup.models import ConversationMessage, session_key
from pickup.runtime import RuntimeRegistry, default_registry

# 新扫到的会话：mtime 在此窗口内才插到列表最前；更旧的（常为临时 cwd 复活）
# 追加到末尾，避免几天前的会话整批顶到侧边栏。
_FRESH_PREPEND_MAX_AGE = 2 * 86400


class SessionStore:
    """持有所有已注册运行时的会话列表与标题缓存。

    标题生成已移交独立后台进程（pickup --generate-titles），本类只负责读取缓存，
    并通过轮询缓存文件把后台进程逐批写入的新标题反映到界面，自身不写缓存、
    不调用 claude，避免与后台进程重复花额度或竞争缓存文件。
    """

    def __init__(self, limit: int, registry: RuntimeRegistry | None = None):
        self.limit = limit
        self.registry = registry or default_registry()
        self.lock = threading.Lock()
        self.sessions: dict[str, list[dict]] = {runtime_id: [] for runtime_id in self.registry.ids}
        self.display_titles: dict[str, str] = {}  # 跨运行时会话键 -> 当前展示标题
        self.dirty = threading.Event()
        self.cache = titles.load_cache()
        self.generating: set[str] = set()  # 仍是临时兜底、等待后台进程产出的会话键（转圈圈）
        # 本进程内嵌托管的 会话键 -> tmux 会话名。_embed_open 在启动成功的瞬间就写入，
        # 比 annotate() 的 pid 祖先链匹配更快、更确定：运行时还没来得及注册 pid 文件
        # （或像某些 fake CLI 一样根本不注册）时，后台重扫替换会话字典后仍能立刻恢复
        # keepalive_name，避免 x 拒绝关闭、回车误开竞争进程。
        self.hosted: dict[str, str] = {}
        # 跨运行时接力 / 空白新建：目标助手尚未落盘历史时，扫描器看不到条目。
        # 这里暂存本进程插入的「运行中(托管)」占位卡，后台重扫时若磁盘仍无对应
        # 会话且 tmux 还活着，就重新灌回列表；真实会话一经 annotate 挂上同一
        # keepalive 名，占位卡即退役（见 _merge_scanned）。
        self._provisional: dict[str, dict] = {}
        # 用户刚用 q 结束的会话键：杀掉到进程真正退出之间，扫描仍可能报 live=True。
        # 在确认已死之前强制按已结束展示，避免「托管 → 运行中 → 已结束」闪烁。
        self._force_ended: set[str] = set()
        # 值是 (读取时的历史文件 mtime, 消息列表)；文件 mtime 变化就重读，
        # 修掉"同一次 pickup 内 / 关闭预览重开还是旧内容"的问题。
        self.conversations: dict[str, tuple[float | None, list[ConversationMessage]]] = {}
        self._cache_mtime: float = self._cache_file_mtime()
        self._projects: list[dict] | None = None  # 项目聚合缓存，仅在 load() 时失效
        # 稳定的展示顺序（跨运行时会话键）：列表展示出来后已有会话位置固定，
        # 后台重扫只把「新出现」的会话插到最前，不再按 mtime 整体重排——
        # 否则运行中的会话一有消息更新就跳到列表顶上，用户刚要看的位置全乱（用户实报）。
        self._order: list[str] = []
        # load() 是否已经跑完至少一次：main() 现在把 load() 挪到后台线程异步跑，
        # UI 侧（MainScreen）据此决定是直接渲染已有数据，还是先展示空骨架列表、
        # 挂一个 worker 等它完成。_load_event 供 UI 线程阻塞等待，避免和 main()
        # 里预先起的加载线程重复扫描一次。
        self.loaded = False
        self._load_event = threading.Event()
        self.load_error: str | None = None

    @staticmethod
    def _cache_file_mtime() -> float:
        try:
            return os.path.getmtime(titles.CACHE_FILE)
        except OSError:
            return 0.0

    def load(self) -> None:
        from pickup import observe

        try:
            t0 = time.perf_counter()
            scanned = self.registry.scan_all(self.limit)
            duration_ms = int((time.perf_counter() - t0) * 1000)
            session_count = sum(len(items) for items in scanned.values())
            observe.event("scan_all", duration_ms=duration_ms, session_count=session_count, reason="load")
            self._merge_scanned(scanned)
            with self.lock:
                self.load_error = None
        except Exception as exc:
            # main() 在裸后台线程里调用 load()；异常不能让线程直接退出、让 UI
            # 永远等不到完成事件。保留中文错误给页头展示，后台 refresh 仍会继续
            # 尝试并在成功后自动清除。
            with self.lock:
                from pickup.i18n import t

                self.load_error = t("store.load_failed", error=exc)
        finally:
            with self.lock:
                self.loaded = True
            self._load_event.set()

    def wait_loaded(self, timeout: float | None = None) -> bool:
        """阻塞等待 load() 完成一次；已完成时立即返回。

        供 UI 侧的加载 worker 使用：main() 可能已经在后台线程里抢先跑了 load()
        （与探测终端 OSC 颜色并行），worker 不需要再重复扫一遍磁盘，只要等那次
        跑完即可。返回值语义与 threading.Event.wait 一致（超时未完成返回 False）。
        """
        return self._load_event.wait(timeout)

    def get_load_error(self) -> str | None:
        """线程安全读取最近一次加载/刷新错误，供界面页头展示。"""
        with self.lock:
            return self.load_error

    def refresh(self) -> bool:
        """后台周期性重扫磁盘，把新增/结束的会话并入当前列表。

        与 load() 共用合并逻辑，唯一区别是返回「会话集合是否真的变了」，
        供调用方只在有变化时才 dirty.set()，避免主循环无谓重定位光标。
        """
        from pickup import observe

        try:
            t0 = time.perf_counter()
            scanned = self.registry.scan_all(self.limit)
            duration_ms = int((time.perf_counter() - t0) * 1000)
            session_count = sum(len(items) for items in scanned.values())
            observe.event("scan_all", duration_ms=duration_ms, session_count=session_count, reason="refresh")
            before = self._sessions_signature()
            self._merge_scanned(scanned)
            changed = self._sessions_signature() != before
        except Exception as exc:
            with self.lock:
                from pickup.i18n import t

                self.load_error = t("store.refresh_failed", error=exc)
            raise
        with self.lock:
            self.load_error = None
        return changed

    def _sessions_signature(self) -> tuple:
        """判定「会话集合是否真的变了」的签名，只应纳入值变化后必须触发列表
        重建的字段。

        `live`/`keepalive_name` 必须在内——否则「运行中→已结束」状态翻转和
        托管标注出现/消失时，会话键集合本身没变，`refresh()` 判定"没变化"、
        `dirty` 不会 set，`SessionCard` 手上还是上一次合并时的旧 dict 引用，
        状态列和运行中标注会一直冻结在首次展示时的取值，直到某个真正的新增/
        结束会话顺带带动一次 rebuild（真实 bug：长时间开着 pickup 盯一个正在
        跑的会话，看到的"运行中"字样可能已经过期很久）。

        列表已经支持按会话键原地更新卡片，因此 mtime、标题来源和详情摘要也要
        纳入签名；否则扫描拿到了新内容，refresh() 却会误判“没有变化”，卡片的
        相对时间和右栏最近问答会一直停在旧值。
        """
        with self.lock:
            return tuple(
                (
                    runtime_id,
                    tuple(
                        (
                            session_key(session),
                            bool(session.get("live")),
                            session.get("keepalive_name"),
                            session.get("mtime"),
                            session.get("cwd"),
                            session.get("cwd_display"),
                            session.get("native_title"),
                            session.get("fallback_title"),
                            session.get("first_user_msg"),
                            session.get("last_user_msg"),
                            session.get("last_agent_msg"),
                        )
                        for session in bucket
                    ),
                )
                for runtime_id, bucket in sorted(self.sessions.items())
            )

    def _merge_scanned(self, scanned: dict[str, list[dict]]) -> None:
        # 每个适配器负责按时间倒序返回，无需在界面层二次排序
        keepalive.annotate([session for bucket in scanned.values() for session in bucket])

        with self.lock:
            self.sessions.update(scanned)
            claimed_keepalive = {
                session.get("keepalive_name")
                for bucket in self.sessions.values()
                for session in bucket
                if session.get("keepalive_name")
            }
            for key, provisional in list(self._provisional.items()):
                name = self.hosted.get(key) or provisional.get("keepalive_name")
                if name and name in claimed_keepalive:
                    # 真实会话已挂上同一托管名：占位卡退役，避免双卡。
                    self._provisional.pop(key, None)
                    self.hosted.pop(key, None)
                    continue
                if not name or not embed.is_alive(str(name)):
                    self._provisional.pop(key, None)
                    self.hosted.pop(key, None)
                    continue
                runtime_id = str(provisional.get("source") or "")
                bucket = self.sessions.setdefault(runtime_id, [])
                if any(session_key(session) == key for session in bucket):
                    continue
                provisional["keepalive_name"] = name
                provisional["live"] = True
                bucket.insert(0, provisional)
            by_key: dict[str, dict] = {}
            for bucket in self.sessions.values():
                for session in bucket:
                    by_key[session_key(session)] = session
            # 稳定顺序：已展示的会话保持原位（只更新内容，不移动）。
            # 新出现的会话：最近活跃的插到最前；「目录复活」等重新扫到的旧会话
            # 追加到末尾——避免 /tmp 临时 cwd 重建时几天前的会话整批顶到侧边栏。
            known = set(self._order)
            fresh = [session for key, session in by_key.items() if key not in known]
            now = time.time()
            fresh_hot = [
                session for session in fresh
                if now - float(session.get("mtime") or 0) <= _FRESH_PREPEND_MAX_AGE
            ]
            fresh_cold = [
                session for session in fresh
                if now - float(session.get("mtime") or 0) > _FRESH_PREPEND_MAX_AGE
            ]
            fresh_hot.sort(key=lambda session: float(session.get("mtime") or 0), reverse=True)
            fresh_cold.sort(key=lambda session: float(session.get("mtime") or 0), reverse=True)
            self._order = (
                [session_key(session) for session in fresh_hot]
                + [key for key in self._order if key in by_key]
                + [session_key(session) for session in fresh_cold]
            )
            # 已从扫描结果消失的会话不能继续占着生成状态，否则 has_generating()
            # 会永久为真，列表仍会空转刷新不存在的卡片。
            self.generating.intersection_update(by_key)
            for session in by_key.values():
                key = session_key(session)
                # 用户刚结束的会话：进程可能还没退出，扫描仍报 live；强制已结束展示，
                # 直到某次扫描确认 live=False 再解除（见 mark_hosted 清除分支）。
                if key in self._force_ended:
                    if session.get("live"):
                        session["live"] = False
                        session["pid"] = None
                        session.pop("keepalive_name", None)
                    else:
                        self._force_ended.discard(key)
                # annotate 没匹配上时，用本进程的内嵌托管记录兜底（见 __init__ 注释）；
                # 托管会话已死则清掉记录，让状态回到真实的「已结束」
                if "keepalive_name" not in session:
                    hosted_name = self.hosted.get(key)
                    if hosted_name:
                        if embed.is_alive(hosted_name):
                            session["keepalive_name"] = hosted_name
                        else:
                            self.hosted.pop(key, None)
                title, needs = titles.resolve_initial_title(session, self.cache)
                self.display_titles[key] = title
                # 生成状态必须以标题状态机返回的 needs 为唯一依据。低价值会话、
                # 已尝试失败的会话都可能没有模型标题，但它们不应继续转圈。
                if needs:
                    self.generating.add(key)
                else:
                    self.generating.discard(key)
            self._projects = None

    def projects(self) -> list[dict]:
        """跨所有来源聚合的项目文件夹列表（侧边栏用），惰性计算并缓存。"""
        with self.lock:
            if self._projects is None:
                self._projects = _project_groups(self.sessions)
            return self._projects

    def all_sessions(self) -> list[dict]:
        """返回稳定展示顺序的会话快照：已有位置固定；近 2 天的新会话在前，更旧的复活会话在后。"""
        with self.lock:
            by_key = {
                session_key(session): session
                for bucket in self.sessions.values()
                for session in bucket
            }
            ordered = [by_key[key] for key in self._order if key in by_key]
            if len(ordered) != len(by_key):
                # 兜底：_order 尚未覆盖的 key（如测试直接塞 sessions 未经合并），
                # 按 mtime 倒序排在最前，与「新会话置顶」语义一致。
                missing = [s for key, s in by_key.items() if key not in set(self._order)]
                missing.sort(key=lambda session: float(session.get("mtime") or 0), reverse=True)
                ordered = missing + ordered
            return ordered

    def find_session(self, key: str) -> dict | None:
        """按跨运行时会话键返回当前扫描快照中的会话对象。"""
        with self.lock:
            for bucket in self.sessions.values():
                for session in bucket:
                    if session_key(session) == key:
                        return session
        return None

    def remove_session(self, key: str) -> None:
        """从当前内存状态里彻底摘除一条会话，供删除动作调用后立即消失。

        磁盘历史已被 `runtime.delete_session` 抹掉；这里只是让 UI 不必等下一轮
        `refresh()` 才发现它没了。清理范围覆盖 `_merge_scanned`/`mark_hosted`/
        `register_hosted_session` 会写入的每一处按 key 索引的结构，任何一处漏清都
        会让卡片残留或状态机不一致（如 `generating` 漏清会让转圈圈永远转下去）。
        """
        with self.lock:
            for bucket in self.sessions.values():
                bucket[:] = [session for session in bucket if session_key(session) != key]
            self._order = [k for k in self._order if k != key]
            self.display_titles.pop(key, None)
            self.generating.discard(key)
            self.conversations.pop(key, None)
            self.hosted.pop(key, None)
            self._provisional.pop(key, None)
            self._force_ended.discard(key)
            self._projects = None

    def register_hosted_session(
        self,
        *,
        runtime_id: str,
        keepalive_name: str,
        title: str,
        cwd: str | None,
        ident: str | None = None,
    ) -> dict:
        """跨运行时接力 / 空白新建：在扫描出真实历史前插入「运行中(托管)」占位卡。

        返回写入列表的会话 dict；调用方应用其会话键选中左栏并挂右栏画面。
        """
        from pickup.scan.common import shorten_cwd
        from pickup.models import format_message_time

        session_id = ident or keepalive_name.rsplit("-", 1)[-1]
        now = time.time()
        cwd_text = str(cwd or "").strip()
        session = {
            "source": runtime_id,
            "id": session_id,
            "short_id": session_id.replace("-", "")[:12],
            "cwd": cwd_text,
            "cwd_display": shorten_cwd(cwd_text) if cwd_text else "",
            "mtime": now,
            "display_time": format_message_time(now),
            "time_source": "provisional",
            "event_time": now,
            "file_mtime": now,
            "size_bytes": 0,
            "size_kb": 0,
            "native_title": None,
            "fallback_title": title or f"新{runtime_id}会话",
            "status_tag": titles.STATUS_PENDING,
            "live": True,
            "pid": None,
            "first_user_msg": "",
            "last_user_msg": "",
            "last_agent_msg": "",
            "path": "",
            "keepalive_name": keepalive_name,
            "provisional": True,
        }
        key = session_key(session)
        with self.lock:
            self.hosted[key] = keepalive_name
            self._force_ended.discard(key)
            self._provisional[key] = session
            bucket = self.sessions.setdefault(runtime_id, [])
            bucket[:] = [item for item in bucket if session_key(item) != key]
            bucket.insert(0, session)
            self._order = [key] + [item for item in self._order if item != key]
            self.display_titles[key] = session["fallback_title"]
            self.generating.discard(key)
        return session

    def mark_hosted(self, key: str, name: str | None) -> dict | None:
        """原子登记/清除托管会话，并同步更新当前扫描快照中的展示字段。

        清除时（name=None）一并把 `live`/`pid` 置为已结束，并记入 `_force_ended`：
        用户按 q 杀掉托管后，若只清 `keepalive_name` 而留下上次扫描的 `live=True`，
        列表会先从「运行中(托管)」闪成「运行中」，再等后台重扫才变成「已结束」；
        进程尚未退出时下一轮扫描仍可能报 live，靠 `_force_ended` 压住直到确认已死。
        """
        with self.lock:
            if name:
                self.hosted[key] = name
                self._force_ended.discard(key)
            else:
                self.hosted.pop(key, None)
                self._provisional.pop(key, None)
                self._force_ended.add(key)
            for bucket in self.sessions.values():
                for session in bucket:
                    if session_key(session) != key:
                        continue
                    if name:
                        session["keepalive_name"] = name
                    else:
                        session.pop("keepalive_name", None)
                        session["live"] = False
                        session["pid"] = None
                    return session
        return None

    def poll_cache_updates(self) -> None:
        """缓存文件被后台生成进程更新时重读，把新标题刷到界面并停掉对应转圈圈。"""
        mtime = self._cache_file_mtime()
        if mtime == self._cache_mtime:
            return
        self._cache_mtime = mtime
        cache = titles.load_cache()
        changed = False
        with self.lock:
            self.cache = cache
            for bucket in self.sessions.values():
                for session in bucket:
                    key = session_key(session)
                    title, needs = titles.resolve_initial_title(session, cache)
                    old_title = self.display_titles.get(key)
                    was_generating = key in self.generating
                    self.display_titles[key] = title
                    if needs:
                        self.generating.add(key)
                    else:
                        self.generating.discard(key)
                    if old_title != title or was_generating != needs:
                        changed = True
        if changed:
            self.dirty.set()

    def snapshot(self) -> tuple[dict[str, str], set[str]]:
        """一次性取「当前展示标题」和「正在生成的 ID 集合」快照，保证两者一致。"""
        with self.lock:
            return dict(self.display_titles), set(self.generating)

    def has_generating(self) -> bool:
        """轻量判断有没有会话在生成标题，只读一个 bool、不拷贝任何 dict/set；
        供高频轮询（如列表转圈圈 spinner）在没有生成任务时直接跳过 snapshot()
        的拷贝开销。"""
        with self.lock:
            return bool(self.generating)

    def get_title(self, session: dict) -> str:
        with self.lock:
            return self.display_titles.get(session_key(session), session["fallback_title"])

    def get_conversation(self, session: dict) -> list[ConversationMessage]:
        """按需读取并缓存选中会话的真实聊天记录；历史文件 mtime 变化（有新写入）时自动
        重读，供预览页关闭重开和停留期间的轮询刷新使用。"""
        key = session_key(session)
        path = str(session.get("path") or "")
        try:
            mtime = os.stat(path).st_mtime if path else None
        except OSError:
            mtime = None
        with self.lock:
            cached = self.conversations.get(key)
            if cached is not None and cached[0] == mtime:
                return list(cached[1])
        runtime_id = str(session.get("source") or "")
        persistent = get_cache().get_conversation(runtime_id, key, path) if path else None
        if persistent is not None:
            with self.lock:
                self.conversations[key] = (mtime, list(persistent))
            return list(persistent)
        runtime = self.registry.get(runtime_id)
        messages = runtime.load_conversation(session)
        with self.lock:
            self.conversations[key] = (mtime, list(messages))
        if path:
            get_cache().put_conversation(runtime_id, key, path, list(messages))
        return messages

    def peek_conversation(self, session: dict) -> list[ConversationMessage] | None:
        """若缓存仍有效则返回对话副本，否则返回 None（不触发磁盘读取）。"""
        key = session_key(session)
        path = str(session.get("path") or "")
        try:
            mtime = os.stat(path).st_mtime if path else None
        except OSError:
            mtime = None
        with self.lock:
            cached = self.conversations.get(key)
            if cached is not None and cached[0] == mtime:
                return list(cached[1])
        return None


def _new_session_cwd(store: SessionStore, nav, session: dict | None) -> str | None:
    """新建会话工作目录：搜索结果若恰好只剩一个项目则沿用，否则用所选会话目录。

    `nav` 需要有 `project_query` 属性（界面层的 `ui.nav.NavState`），这里不直接
    依赖 ui 包的具体类型，避免循环 import。
    """
    query = str(getattr(nav, "project_query", "") or "").strip()
    if query:
        titles_map = getattr(store, "display_titles", None) or {}
        visible = _filter_sessions_by_query(store.all_sessions(), query, titles=titles_map)
        keys = {_normalize_cwd(s.get("cwd")) for s in visible}
        keys.discard("")
        if len(keys) == 1:
            return next(iter(keys))
    if session is not None:
        cwd_key = _normalize_cwd(session.get("cwd"))
        return cwd_key or None
    return None
