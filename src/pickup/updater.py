"""客户端自动更新：版本检查、安装渠道判定、就地升级与重启哨兵。

运行时无关、不依赖 ui 包，方便独立测试；网络与子进程调用一律吞掉异常返回
None/False，绝不能让检查或升级本身的故障拖死 TUI 或后台线程。只用标准库
（urllib/json/site/subprocess），不引入新依赖。
"""

from __future__ import annotations

import json
import os
import re
import site
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Literal

REPO = "x0c/pickup"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
_FETCH_TIMEOUT = 3.0  # 秒；慢网络/无网不能拖住后台 worker

CACHE_DIR = os.path.expanduser("~/.cache/pickup")
STATE_FILE = os.path.join(CACHE_DIR, "update.json")

Channel = Literal["brew", "pip", "dev"]


@dataclass(frozen=True)
class RestartRequest:
    """升级成功后用户确认重启：run_app() 用它代替 LaunchRequest/NewSessionRequest/None
    作为返回值，交给 cli.main() 用新代码 re-exec 一个全新的 pickup 进程。"""


def _version_tuple(text: str) -> tuple[int, ...]:
    """把 "0.20.0" / "v0.20.0" 这类字符串解析成可比较的整数元组。

    解析不出的片段按 0 处理，不因为一次奇怪的 tag 名（如带 -rc1 后缀）而抛异常。
    """
    text = text.strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    parts = re.split(r"[.\-+]", text)
    out: list[int] = []
    for part in parts:
        match = re.match(r"\d+", part)
        if match:
            out.append(int(match.group()))
        else:
            break
    return tuple(out) or (0,)


def current_version() -> tuple[int, ...]:
    import pickup

    return _version_tuple(pickup.__version__)


def is_newer(latest: str, current: tuple[int, ...] | None = None) -> bool:
    """比较 latest（如 "0.20.1" 或 "v0.20.1"）是否严格新于当前版本。"""
    current = current if current is not None else current_version()
    return _version_tuple(latest) > current


def fetch_latest(timeout: float = _FETCH_TIMEOUT) -> str | None:
    """查询 GitHub 最新 release 的 tag_name，去掉前导 v；失败一律返回 None。"""
    try:
        req = urllib.request.Request(
            LATEST_RELEASE_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "pickup-updater"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        tag = str(payload.get("tag_name") or "").strip()
        if not tag:
            return None
        return tag[1:] if tag[:1] in ("v", "V") else tag
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, json.JSONDecodeError):
        return None


_BREW_MARKERS = ("/Cellar/", "/homebrew/", "linuxbrew")


def detect_channel() -> Channel:
    """按 pickup 包自身安装路径判定发布渠道：brew（Homebrew tap）/ pip（用户或系统
    site-packages）/ dev（源码检出或 editable 安装，无法一键升级）。"""
    import pickup

    pkg_dir = os.path.dirname(os.path.abspath(pickup.__file__))
    if any(marker in pkg_dir for marker in _BREW_MARKERS):
        return "brew"
    site_dirs = [os.path.abspath(p) for p in _site_packages_dirs()]
    if any(pkg_dir.startswith(d) for d in site_dirs) or "site-packages" in pkg_dir:
        return "pip"
    return "dev"


def package_file() -> str:
    import pickup

    return os.path.abspath(pickup.__file__)


def find_checkout_root(start: str | None = None) -> str | None:
    """自 start（默认 cwd）向上找含 pyproject.toml + src/pickup 的源码根。"""
    cur = os.path.abspath(start or os.getcwd())
    for _ in range(10):
        pyproject = os.path.join(cur, "pyproject.toml")
        init = os.path.join(cur, "src", "pickup", "__init__.py")
        if os.path.isfile(pyproject) and os.path.isfile(init):
            try:
                with open(pyproject, "r", encoding="utf-8") as fh:
                    text = fh.read(400)
                if 'name = "pickup"' in text or "name = 'pickup'" in text:
                    return cur
            except OSError:
                pass
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def is_loaded_from_checkout(checkout_root: str | None = None) -> bool:
    """当前 import 的 pickup 是否来自给定（或 cwd 探测到的）源码树。"""
    root = checkout_root if checkout_root is not None else find_checkout_root()
    if not root:
        return False
    pkg = package_file()
    src = os.path.join(os.path.abspath(root), "src", "pickup")
    try:
        return os.path.commonpath([pkg, src]) == src
    except ValueError:
        return False


def stale_source_warning(cwd: str | None = None) -> str | None:
    """在 pickup 源码树内开发，但实际加载的是别处的安装副本时返回告警；否则 None。

    普通 pipx/brew 用户不在仓库里跑，不会触发。
    """
    root = find_checkout_root(cwd)
    if root is None:
        return None
    if is_loaded_from_checkout(root):
        return None
    script = os.path.join(root, "scripts", "dev-install.sh")
    return (
        f"正在仓库 {root} 中开发，但当前进程加载的是 {package_file()}，"
        f"改源码不会生效。请运行: bash {script} ，然后重启 TUI。"
    )


def install_report(cwd: str | None = None) -> dict:
    """供 diagnose / --version：版本、路径、渠道、是否与 cwd 源码树一致。"""
    import pickup

    root = find_checkout_root(cwd)
    pkg = package_file()
    channel = detect_channel()
    from_checkout = is_loaded_from_checkout(root) if root else False
    editable_like = channel == "dev" or from_checkout
    warning = stale_source_warning(cwd)
    hints: list[str] = []
    if warning:
        hints.append(warning)
    elif root and not from_checkout:
        hints.append(
            f"开发安装请运行: bash {os.path.join(root, 'scripts', 'dev-install.sh')}"
        )
    elif not editable_like and channel == "pip":
        hints.append(
            "若在改仓库源码，请用 scripts/dev-install.sh 做 editable 安装，"
            "不要只 force-reinstall 非 -e 副本"
        )
    return {
        "version": pickup.__version__,
        "package_file": pkg,
        "python": sys.executable,
        "channel": channel,
        "checkout_root": root,
        "loaded_from_checkout": from_checkout,
        "editable_like": editable_like,
        "stale_source_warning": warning,
        "hints": hints,
    }


def _site_packages_dirs() -> list[str]:
    dirs: list[str] = []
    try:
        dirs.append(site.getusersitepackages())
    except Exception:
        pass
    try:
        dirs.extend(site.getsitepackages())
    except Exception:
        pass
    return [d for d in dirs if d]


def _is_user_site(pkg_dir: str) -> bool:
    try:
        user_site = os.path.abspath(site.getusersitepackages())
    except Exception:
        return False
    return pkg_dir.startswith(user_site)


def is_updatable(channel: Channel | None = None) -> bool:
    channel = channel or detect_channel()
    return channel in ("brew", "pip")


def update_command(latest_tag: str, channel: Channel | None = None) -> list[str] | None:
    """返回执行就地升级的命令；dev 渠道无法自动升级，返回 None。"""
    channel = channel or detect_channel()
    if channel == "brew":
        return ["brew", "upgrade", "pickup"]
    if channel == "pip":
        import pickup

        pkg_dir = os.path.dirname(os.path.abspath(pickup.__file__))
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
        if _is_user_site(pkg_dir):
            cmd.append("--user")
        cmd.append(f"git+https://github.com/{REPO}.git@v{latest_tag}")
        return cmd
    return None


def _brew_installed_version() -> tuple[int, ...] | None:
    """查询 Homebrew 中 pickup 实际已安装的最高版本；查不到返回 None（此时不阻断，
    避免把「查询失败」误判成「升级失败」）。"""
    try:
        result = subprocess.run(
            ["brew", "list", "--versions", "pickup"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # 输出形如 "pickup 0.20.0 0.21.0"：首列是包名，其余是保留在本机的各版本
    versions = [_version_tuple(tok) for tok in (result.stdout or "").split()[1:]]
    versions = [v for v in versions if v != (0,)]
    return max(versions) if versions else None


def run_update(latest_tag: str, channel: Channel | None = None) -> tuple[bool, str]:
    """跑升级命令；返回 (是否成功, 合并输出摘要)。dev 渠道直接失败。

    brew 渠道的两个坑都在这里兜住：
    1. 用户 shell 里常设 `HOMEBREW_NO_AUTO_UPDATE=1` 提速；子进程继承后
       `brew upgrade` 不刷新 tap，永远看不到刚发布的新配方 → 空跑退出 0（假成功）。
       这里为 brew 子进程强制去掉该变量，放开自动刷新。
    2. 即便如此，`brew upgrade` 什么都没升级也退出 0；必须核实实际安装版本确已
       到位，否则会误报「更新完成」，用户重启后仍是旧版本。
    """
    channel = channel or detect_channel()
    cmd = update_command(latest_tag, channel)
    if cmd is None:
        return False, "当前安装方式（源码/开发安装）不支持一键更新"
    env = os.environ.copy()
    if channel == "brew":
        env.pop("HOMEBREW_NO_AUTO_UPDATE", None)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, env=env,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()[-4000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, output
    if channel == "brew":
        installed = _brew_installed_version()
        if installed is not None and installed < _version_tuple(latest_tag):
            hint = "brew 未升级到最新版本，本地配方可能过期。请手动执行：brew update && brew upgrade pickup"
            return False, (output + "\n" + hint).strip()
    return True, output


# ---- 更新检查状态（当天忽略过就不再弹，次日恢复） ----

def _load_state() -> dict:
    if not os.path.isfile(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    """原子写：与 titles.py 的 save_cache 同一惯用法，避免并发读到半截 JSON。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp_path = STATE_FILE + f".tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _today() -> str:
    return date.today().isoformat()


def should_prompt(latest: str) -> bool:
    """latest 严格新于当前版本、且不满足"今天已经忽略过这个版本"时才应该弹窗。"""
    if not is_newer(latest):
        return False
    state = _load_state()
    return not (state.get("dismissed_version") == latest and state.get("dismissed_date") == _today())


def mark_dismissed(version: str) -> None:
    _save_state({"dismissed_version": version, "dismissed_date": _today()})


# ---- `pickup update` 终端子命令 ----

def cli_update() -> int:
    """人读输出的终端手动升级入口，返回进程退出码。"""
    from pickup.i18n import t

    channel = detect_channel()
    if not is_updatable(channel):
        print(t("update.cli_dev_hint", repo=REPO))
        return 1

    latest = fetch_latest()
    if latest is None:
        print(t("update.cli_check_failed"))
        return 1

    current = current_version()
    if not is_newer(latest, current):
        print(t("update.cli_latest", version=".".join(map(str, current))))
        return 0

    print(t("update.cli_updating", version=latest))
    ok, output = run_update(latest, channel)
    if output:
        print(output)
    if not ok:
        print(t("update.cli_failed"))
        return 1
    print(t("update.cli_updated", version=latest))
    return 0
