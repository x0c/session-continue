"""本机项目发现：会话 cwd ∪ 文件系统 git 根，供快捷启动与 TUI 新建共用。

扫描策略参考常见「家目录下找 git 根」做法，但不耦合其它产品的环境变量。
硬排除 `.stversions` 等目录，避免 Syncthing 版本快照里的 `.git` 冒充项目。
"""

from __future__ import annotations

import fnmatch
import os
import sys
from dataclasses import dataclass, field
from typing import Iterable, TextIO

from pickup.display import _disambiguate_labels, _fuzzy_match, _normalize_cwd
from pickup.scan.common import is_ephemeral_agent_cwd

DEFAULT_SCAN_DEPTH = 4

# walk 时按目录名 SkipDir。`.stversions` / `.stfolder` 是 Syncthing 快照坑，必须硬编码。
HARD_SKIP_DIR_NAMES = frozenset({
    ".stversions",
    ".stfolder",
    ".git",
    ".cache",
    ".Trash",
    ".npm",
    ".nvm",
    ".local",
    ".cargo",
    ".rustup",
    ".pyenv",
    ".docker",
    ".venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "node_modules",
    "Library",
    "venv",
    "__pycache__",
    "dist",
    "build",
})

_SOURCE_SESSION = "session"
_SOURCE_FILESYSTEM = "filesystem"

# 进程内缓存：同一配置下重复 discover 不重扫磁盘。
_fs_cache_key: tuple | None = None
_fs_cache_paths: list[str] | None = None


@dataclass(frozen=True)
class Project:
    """一条已发现项目。"""

    path: str
    name: str
    label: str
    sources: frozenset[str] = field(default_factory=frozenset)


class ProjectResolveError(Exception):
    """项目名匹配失败（0 命中、多命中无法交互、或多余参数）。"""


def configured_roots() -> list[str]:
    """默认 `$HOME`；`PICKUP_PROJECT_ROOTS` 覆盖（逗号分隔）。

    环境变量已设置但解析结果为空（例如 `PICKUP_PROJECT_ROOTS=`）时返回空列表，
    表示跳过文件系统扫描，只保留会话 cwd——便于测试与只要会话源的场景。
    """
    if "PICKUP_PROJECT_ROOTS" not in os.environ:
        return [os.path.expanduser("~")]
    raw = os.environ.get("PICKUP_PROJECT_ROOTS") or ""
    return [os.path.expanduser(p.strip()) for p in raw.split(",") if p.strip()]


def configured_depth() -> int:
    raw = (os.environ.get("PICKUP_PROJECT_DEPTH") or "").strip()
    if not raw:
        return DEFAULT_SCAN_DEPTH
    try:
        depth = int(raw)
    except ValueError:
        return DEFAULT_SCAN_DEPTH
    return depth if depth > 0 else DEFAULT_SCAN_DEPTH


def configured_extra_excludes() -> list[str]:
    raw = (os.environ.get("PICKUP_PROJECT_EXCLUDE") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def clear_filesystem_cache() -> None:
    """测试用：清掉 git 扫描进程内缓存。"""
    global _fs_cache_key, _fs_cache_paths
    _fs_cache_key = None
    _fs_cache_paths = None


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.normpath(path)


def _path_depth(root: str, path: str) -> int:
    rel = os.path.relpath(path, root)
    if rel in (".", ""):
        return 0
    return rel.count(os.sep) + 1


def _should_skip_dir(name: str, abs_path: str, extra_excludes: list[str]) -> bool:
    if name in HARD_SKIP_DIR_NAMES:
        return True
    # 其它以 `.` 开头的目录一律不进（.stversions 已在硬排除里；这里兜住未见过的点目录）。
    if name.startswith("."):
        return True
    if not extra_excludes:
        return False
    slash = abs_path.replace("\\", "/")
    for pattern in extra_excludes:
        if pattern in {name, abs_path, slash}:
            return True
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(slash, pattern):
            return True
        # 路径任一段命中（如 exclude=vendor）
        if any(fnmatch.fnmatch(part, pattern) for part in slash.split("/") if part):
            return True
    return False


def _has_git_marker(path: str) -> bool:
    return os.path.lexists(os.path.join(path, ".git"))


def scan_git_roots(
    roots: Iterable[str] | None = None,
    *,
    depth: int | None = None,
    extra_excludes: Iterable[str] | None = None,
    allow_nested: bool = False,
    use_cache: bool = True,
) -> list[str]:
    """在配置根下递归找含 `.git` 的目录；命中后默认不进嵌套。返回排序后的绝对路径。"""
    global _fs_cache_key, _fs_cache_paths

    root_list = [os.path.expanduser(r) for r in (roots if roots is not None else configured_roots())]
    max_depth = configured_depth() if depth is None else depth
    excludes = list(extra_excludes) if extra_excludes is not None else configured_extra_excludes()
    cache_key = (tuple(root_list), max_depth, tuple(excludes), allow_nested)
    if use_cache and _fs_cache_key == cache_key and _fs_cache_paths is not None:
        return list(_fs_cache_paths)

    seen: dict[str, None] = {}
    for root in root_list:
        if not root:
            continue
        root_clean = _realpath(root)
        if not os.path.isdir(root_clean):
            continue
        _scan_one_root(root_clean, max_depth, excludes, allow_nested, seen)

    paths = sorted(seen.keys())
    if use_cache:
        _fs_cache_key = cache_key
        _fs_cache_paths = list(paths)
    return paths


def _scan_one_root(
    root: str,
    max_depth: int,
    extra_excludes: list[str],
    allow_nested: bool,
    seen: dict[str, None],
) -> None:
    # 根自身若是 git 项目也收录（深度 0）。
    if _has_git_marker(root):
        seen[_normalize_cwd(root) or root] = None
        if not allow_nested:
            return

    for dirpath, dirnames, _filenames in os.walk(root, topdown=True, followlinks=False):
        # 先按名剪枝，再决定是否把当前目录当项目。
        keep: list[str] = []
        for name in dirnames:
            child = os.path.join(dirpath, name)
            if _should_skip_dir(name, child, extra_excludes):
                continue
            if _path_depth(root, child) > max_depth:
                continue
            keep.append(name)
        dirnames[:] = keep

        if dirpath == root and _has_git_marker(root) and not allow_nested:
            dirnames[:] = []
            continue

        if _path_depth(root, dirpath) > max_depth:
            dirnames[:] = []
            continue

        if dirpath != root and _has_git_marker(dirpath):
            key = _normalize_cwd(dirpath) or dirpath
            seen[key] = None
            if not allow_nested:
                dirnames[:] = []


def discover(
    session_cwds: Iterable[str] | None = None,
    *,
    scan_filesystem: bool = True,
    roots: Iterable[str] | None = None,
    depth: int | None = None,
    extra_excludes: Iterable[str] | None = None,
    use_cache: bool = True,
) -> list[Project]:
    """合并会话 cwd 与 git 扫描结果，按 path 去重。"""
    by_path: dict[str, set[str]] = {}

    for raw in session_cwds or ():
        key = _normalize_cwd(raw)
        if not key or is_ephemeral_agent_cwd(key):
            continue
        # 会话 cwd 即使目录已删也保留（与旧 _project_groups 一致）；启动时再校验。
        by_path.setdefault(key, set()).add(_SOURCE_SESSION)

    if scan_filesystem:
        for path in scan_git_roots(
            roots,
            depth=depth,
            extra_excludes=extra_excludes,
            use_cache=use_cache,
        ):
            key = _normalize_cwd(path) or path
            by_path.setdefault(key, set()).add(_SOURCE_FILESYSTEM)

    named = [p for p in by_path if p]
    labels = _disambiguate_labels(named)
    projects = [
        Project(
            path=path,
            name=os.path.basename(path) or path,
            label=labels.get(path, os.path.basename(path) or path),
            sources=frozenset(sources),
        )
        for path, sources in by_path.items()
    ]
    projects.sort(key=lambda p: (p.label.casefold(), p.path))
    return projects


def _match_rank(query: str, project: Project) -> int | None:
    """越小越优先；None 表示不匹配。"""
    needle = (query or "").casefold().strip()
    if not needle:
        return None
    name = project.name.casefold()
    label = project.label.casefold()
    path = project.path.casefold()
    if name == needle or label == needle:
        return 0
    if needle in name:
        return 1
    if needle in label:
        return 2
    if _fuzzy_match(query, project.name):
        return 3
    if _fuzzy_match(query, project.label):
        return 4
    if needle in path or _fuzzy_match(query, project.path):
        return 5
    return None


def match_projects(query: str, projects: Iterable[Project]) -> list[Project]:
    """大小写无关模糊匹配；按相关度排序。"""
    ranked: list[tuple[int, Project]] = []
    for project in projects:
        rank = _match_rank(query, project)
        if rank is not None:
            ranked.append((rank, project))
    ranked.sort(key=lambda item: (item[0], item[1].label.casefold(), item[1].path))
    return [project for _, project in ranked]


def resolve_query(
    query: str,
    projects: Iterable[Project],
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    interactive: bool | None = None,
) -> str:
    """解析项目查询为唯一 cwd。0 命中 / 非交互多命中抛 ProjectResolveError。"""
    matches = match_projects(query, projects)
    if not matches:
        raise ProjectResolveError(f"未找到匹配项目：{query}")
    if len(matches) == 1:
        return matches[0].path

    out = stdout or sys.stderr
    in_stream = stdin or sys.stdin
    if interactive is None:
        interactive = bool(getattr(in_stream, "isatty", lambda: False)())

    listing = "\n".join(
        f"  {index}. {project.label}  ({project.path})"
        for index, project in enumerate(matches, start=1)
    )
    if not interactive:
        raise ProjectResolveError(
            f"多个项目匹配「{query}」（共 {len(matches)} 个）；"
            f"请在交互终端中选择，或换更精确的名字：\n{listing}"
        )

    out.write(f"多个项目匹配「{query}」，请选择：\n{listing}\n")
    out.flush()

    while True:
        out.write("请输入序号：")
        out.flush()
        line = in_stream.readline()
        if not line:
            raise ProjectResolveError("未选择项目")
        text = line.strip()
        if not text.isdigit():
            out.write("请输入有效数字序号。\n")
            continue
        choice = int(text)
        if 1 <= choice <= len(matches):
            return matches[choice - 1].path
        out.write(f"请输入 1–{len(matches)} 之间的序号。\n")


def session_cwds_from_sessions(sessions_by_source: dict[str, list[dict]]) -> list[str]:
    """从扫描结果提取有效 cwd。"""
    out: list[str] = []
    seen: set[str] = set()
    for bucket in sessions_by_source.values():
        for session in bucket:
            key = _normalize_cwd(session.get("cwd"))
            if not key or key in seen or is_ephemeral_agent_cwd(key):
                continue
            seen.add(key)
            out.append(key)
    return out


def project_entries(
    sessions_by_source: dict[str, list[dict]],
    *,
    scan_filesystem: bool = True,
    roots: Iterable[str] | None = None,
    depth: int | None = None,
    extra_excludes: Iterable[str] | None = None,
    use_cache: bool = True,
) -> list[dict]:
    """供 SessionStore / pick_project 使用的项目列表（兼容旧字段形状）。

    每项：cwd_key / label / count / latest_mtime；纯 git 项目 count=0。
    """
    stats: dict[str, dict] = {}
    for bucket in sessions_by_source.values():
        for session in bucket:
            key = _normalize_cwd(session.get("cwd"))
            if not key or is_ephemeral_agent_cwd(key):
                # 未知目录仍留给旧逻辑：空 cwd 聚合在侧边栏意义不大，跳过。
                continue
            entry = stats.setdefault(key, {"count": 0, "latest_mtime": 0.0})
            entry["count"] += 1
            mtime = session.get("mtime") or 0
            if mtime > entry["latest_mtime"]:
                entry["latest_mtime"] = mtime

    # 无路径会话：保留「未知目录」桶（与旧 _project_groups 行为一致）。
    unknown_count = 0
    unknown_mtime = 0.0
    for bucket in sessions_by_source.values():
        for session in bucket:
            key = _normalize_cwd(session.get("cwd"))
            if key:
                continue
            unknown_count += 1
            mtime = session.get("mtime") or 0
            if mtime > unknown_mtime:
                unknown_mtime = mtime

    discovered = discover(
        list(stats.keys()),
        scan_filesystem=scan_filesystem,
        roots=roots,
        depth=depth,
        extra_excludes=extra_excludes,
        use_cache=use_cache,
    )
    entries: list[dict] = []
    for project in discovered:
        st = stats.get(project.path, {"count": 0, "latest_mtime": 0.0})
        entries.append({
            "cwd_key": project.path,
            "label": project.label,
            "count": st["count"],
            "latest_mtime": st["latest_mtime"],
        })

    if unknown_count:
        from pickup.display import UNKNOWN_PROJECT_LABEL

        entries.append({
            "cwd_key": "",
            "label": UNKNOWN_PROJECT_LABEL,
            "count": unknown_count,
            "latest_mtime": unknown_mtime,
        })

    return sorted(entries, key=lambda p: (-p["count"], -p["latest_mtime"], str(p["label"])))
