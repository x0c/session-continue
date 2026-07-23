"""pickup 命令行入口：参数解析、直启分发、TUI 启动与外层接管。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass

# 关掉 Textual 默认开启的 Kitty 键盘协议。必须在任何 `import textual` 之前设置。
os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")

from pickup import agent_api, embed, keepalive, observe, titles, updater
from pickup.models import LaunchPlan, LaunchRequest, NewSessionRequest
from pickup.observe import log_embed_error as _log_embed_error
from pickup.runtime import LaunchError, RuntimeRegistry, default_registry, execute_launch, usable_cwd
from pickup.store import SessionStore, _new_session_cwd
from pickup import theme as theme_mod
from pickup.theme import _probe_osc_colours


def _restart_process() -> None:
    """客户端自动更新：升级成功、用户点了「重启」后，用新装好的磁盘代码
    re-exec 一个全新 pickup 进程，原样透传本次启动的命令行参数。

    tmux 保活会话与本进程无关，重启不影响已托管会话；execv 成功后本进程
    直接被替换，不会返回。"""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", "pickup", *sys.argv[1:]])

@dataclass(frozen=True)
class _DirectLaunch:
    """直启子命令（`pickup claude …`）带进 TUI 的待托管启动计划：

    进入主循环前就把新会话托管进保活 socket 并聚焦右栏，让直启与界面内
    「新建会话」走完全相同的内嵌路径。
    """

    plan: LaunchPlan
    runtime_id: str
    ident: str


def _launch(request: LaunchRequest | NewSessionRequest, registry: RuntimeRegistry, keepalive_on: bool) -> None:
    """生成启动计划并让目标运行时接管当前终端。

    会话已经在后台保活时直接接回现场，不重新拉起一个和它竞争同一份会话文件的
    新进程；否则按 keepalive_on 开关决定新启动的进程要不要包进保活层。
    """
    if isinstance(request, LaunchRequest):
        attach = keepalive.attach_plan(request.session)
        if attach is not None:
            execute_launch(attach)
            return
        plan = registry.build_launch_plan(request)
        same_runtime = request.session.get("source") == request.target_runtime_id
        ident = request.session["id"] if same_runtime else keepalive.new_session_ident()
    else:
        plan = registry.build_new_session_plan(request)
        ident = keepalive.new_session_ident()

    if keepalive_on:
        plan = keepalive.wrap_plan(plan, request.target_runtime_id, ident)
    execute_launch(plan)


def _format_resume_command(argv: tuple[str, ...]) -> str:
    """把启动计划的 argv 拼成可直接在 shell 中运行的命令字符串。"""
    parts = []
    for arg in argv:
        if " " in arg or "\n" in arg or '"' in arg:
            escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(arg)
    return " ".join(parts)


def _output_json(registry, limit: int) -> None:
    """以 JSON 格式输出所有运行时的会话列表，每条附上恢复命令，然后退出。

    供大模型或自动化脚本调用：不启动 TUI，不触发后台标题生成，
    不消耗 Claude 额度。标题使用本地临时兜底标题（fallback_title）。
    """
    scanned = registry.scan_all(limit)
    result = []
    for runtime in registry:
        for session in scanned.get(runtime.id, []):
            try:
                plan = runtime.build_resume_plan(session)
                resume_cmd = _format_resume_command(plan.argv)
            except Exception:
                resume_cmd = None
            result.append({
                "runtime": session.get("source"),
                "id": session.get("id"),
                "title": session.get("fallback_title") or session.get("native_title") or "",
                "cwd": session.get("cwd") or "",
                "time": session.get("display_time") or "",
                "mtime": session.get("mtime"),
                "size_kb": round(session.get("size_kb") or 0, 1),
                "status": session.get("status_tag") or "",
                "resume_command": resume_cmd,
                "history_path": session.get("path") or "",
            })
    print(json.dumps(result, ensure_ascii=False, indent=2))


_TITLE_LOCK_FILE = os.path.join(titles.CACHE_DIR, "titles.lock")


def _run_title_daemon(registry: RuntimeRegistry, limit: int) -> None:
    """脱离 TUI 的独立标题生成进程入口（pickup --generate-titles）。

    用文件锁保证全机单实例：拿不到锁说明已有后台进程在跑，直接退出，
    避免用户反复进 pickup 堆积多个生成进程、重复消耗模型额度。
    """
    os.makedirs(titles.CACHE_DIR, exist_ok=True)
    lock_fp = open(_TITLE_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # 已有进程持锁，本次无需重复生成

    try:
        scanned = registry.scan_all(limit)
        cache = titles.load_cache()
        pending = []
        for bucket in scanned.values():
            for session in bucket:
                _, needs = titles.resolve_initial_title(session, cache)
                if needs:
                    pending.append(session)
        if pending:
            titles.refresh_titles(pending, cache)
    finally:
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()


def _spawn_title_daemon(limit: int) -> None:
    """以脱离当前终端的方式拉起后台标题生成进程。

    start_new_session 让子进程独立成新会话/进程组：TUI 之后无论被 execvp
    替换（原生恢复）还是退出，该进程都继续把标题生成完并写入缓存。
    """
    try:
        subprocess.Popen(
            [sys.executable, "-m", "pickup", "--generate-titles", "--limit", str(limit)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass  # 拉起失败仅退化为「只显示临时兜底标题」，不影响主流程


def _require_tmux() -> None:
    """pickup 的会话托管/内嵌面板/断线保活全部建立在 tmux 之上，属于硬依赖；
    缺失或版本过旧时明确报错并给出安装/升级提示，不静默降级出残废功能。

    版本下限检查复用 embed._tmux_version()：`new-session -e` 环境变量注入
    （托管会话的 PICKUP_RUNTIME/PICKUP_SESSION_ID 等元数据唯一注入点）和
    pause-after 流控通知都要求 tmux 3.2+，更旧版本上创建托管会话会直接失败，
    报出的却是一个和版本无关的笼统 EmbedError，用户很难联想到升级 tmux。
    版本解析不出（如 `tmux -V` 输出被魔改）时不阻断——宁可信任已通过的
    `shutil.which` 探测，让真实失败在后续调用里自然暴露，不做过度拦截。"""
    import pickup as pkg

    if pkg.shutil.which("tmux") is None:
        print(
            "pickup 需要 tmux 才能运行，请先安装"
            "（macOS: brew install tmux；Debian/Ubuntu: sudo apt install tmux）。",
            file=pkg.sys.stderr,
        )
        pkg.sys.exit(1)
    version = pkg.embed._tmux_version()
    if version is not None and version < pkg.embed.MIN_TMUX_VERSION:
        need_major, need_minor = pkg.embed.MIN_TMUX_VERSION
        print(
            f"pickup 需要 tmux {need_major}.{need_minor} 及以上版本"
            f"（当前检测到 {version[0]}.{version[1]}）：会话托管依赖 "
            "new-session -e 环境变量注入等 3.2+ 才有的特性，低于此版本会在创建"
            "托管会话时失败。请升级 tmux 后重试。",
            file=pkg.sys.stderr,
        )
        pkg.sys.exit(1)


# 直启子命令进 TUI 侧边栏模式时的每运行时扫描深度，与主 TUI 的 --limit 默认值一致
_DIRECT_LAUNCH_LIMIT = 50


def _dispatch_direct_launch(argv: list[str], registry: RuntimeRegistry) -> None:
    """处理 `pickup [--no-keepalive] <runtime> [参数…]` 直启子命令。

    分流：
    - `pickup claude subswap`：第二个位置参数不以 `-` 开头 → 项目名模糊匹配，
      在匹配目录下 `build_new_session_plan`（新建空白会话）。
    - `pickup claude --resume …` / 无额外参数：透传（`build_passthrough_plan` 只垫
      默认全自动放行参数，用户已显式带了就不重复）。

    真实终端且内嵌可用时默认进入 TUI 侧边栏模式；非真实终端、`--no-keepalive`
    或内嵌不可用时保持直接启动。经 `import pickup as pkg` 取符号，便于测试 patch。
    """
    import pickup as pkg
    from pickup import projects as projects_mod

    pkg._require_tmux()
    no_keepalive = argv and argv[0] == "--no-keepalive"
    rest = argv[1:] if no_keepalive else argv
    runtime_id, user_args = rest[0], rest[1:]

    project_query: str | None = None
    if user_args and not user_args[0].startswith("-"):
        project_query = user_args[0]
        if user_args[1:]:
            print(
                f"项目快捷启动不接受额外参数：{user_args[1:]!r}。"
                "透传请让参数以 - 开头（如 pickup claude --resume id）。",
                file=pkg.sys.stderr,
            )
            pkg.sys.exit(1)

    tty = bool(pkg.sys.stdin.isatty() and pkg.sys.stdout.isatty())
    use_tui = tty and pkg.embed.available(no_keepalive)
    store = None

    if use_tui:
        pkg.keepalive.reap_idle()
        store = pkg.SessionStore(limit=_DIRECT_LAUNCH_LIMIT, registry=registry)
        store.load()
        pkg._spawn_title_daemon(_DIRECT_LAUNCH_LIMIT)

    if project_query is not None:
        try:
            if store is not None:
                discovered = projects_mod.discover(
                    projects_mod.session_cwds_from_sessions(store.sessions),
                )
            else:
                scanned = registry.scan_all(_DIRECT_LAUNCH_LIMIT)
                discovered = projects_mod.discover(
                    projects_mod.session_cwds_from_sessions(scanned),
                )
            cwd = projects_mod.resolve_query(project_query, discovered)
            plan = registry.get(runtime_id).build_new_session_plan(cwd)
        except projects_mod.ProjectResolveError as exc:
            print(str(exc), file=pkg.sys.stderr)
            pkg.sys.exit(1)
        except pkg.LaunchError as exc:
            print(f"启动失败：{exc}", file=pkg.sys.stderr)
            pkg.sys.exit(1)
    else:
        plan = registry.build_passthrough_plan(runtime_id, user_args)

    ident = pkg.keepalive.new_session_ident()

    if not use_tui:
        if pkg.keepalive.enabled(no_keepalive):
            plan = pkg.keepalive.wrap_plan(plan, runtime_id, ident)
        try:
            pkg.execute_launch(plan)
        except pkg.LaunchError as exc:
            print(f"启动失败：{exc}", file=pkg.sys.stderr)
            pkg.sys.exit(1)
        return

    # 与 main() 的 TUI 入口相同：趁 Textual 接管终端前探测外层终端前景/背景色。
    theme_mod._OSC_REPORT = pkg._probe_osc_colours(timeout=0.25)

    from pickup.ui.app import run_app
    chosen = run_app(store, True, _DirectLaunch(plan, runtime_id, ident), theme_mod._OSC_REPORT)
    pkg.embed.close_channel()
    if isinstance(chosen, pkg.updater.RestartRequest):
        pkg._restart_process()
        return
    if chosen is None:
        return
    try:
        pkg._launch(chosen, store.registry, pkg.keepalive.enabled(no_keepalive))
    except pkg.LaunchError as exc:
        print(f"启动失败：{exc}", file=pkg.sys.stderr)
        pkg.sys.exit(1)


def main() -> None:
    # 尽早挂崩溃钩子：TUI 闪退后 stderr 常被清掉，必须先落盘才能事后 diagnose。
    observe.install_crash_hooks()

    # list/search/show/context/plan/describe 是面向 Agent 的机器可读子命令，整体转发给
    # agent_api，不与下面的 TUI/--json 旧参数共用同一个 parser。
    if len(sys.argv) > 1 and sys.argv[1] in agent_api.COMMAND_ROOT_NAMES:
        sys.exit(agent_api.dispatch(sys.argv[1:]))

    # `pickup update`：手动触发客户端自动更新，不放进只读的 agent_api。
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        sys.exit(updater.cli_update())

    # `pickup claude …` / `pickup codex …`（可选前置 --no-keepalive）是直启透传子命令，同样整体
    # 绕开下面的 TUI/--json 旧参数 parser；分发探测此处只需运行时 ID 集合。
    _direct_launch_argv = sys.argv[1:]
    _direct_launch_probe = (
        _direct_launch_argv[1:] if _direct_launch_argv[:1] == ["--no-keepalive"] else _direct_launch_argv
    )
    if _direct_launch_probe and _direct_launch_probe[0] in default_registry().ids:
        _dispatch_direct_launch(_direct_launch_argv, default_registry())
        return

    parser = argparse.ArgumentParser(
        description=(
            "pickup：终端会话接力工具。\n"
            "列出 Claude Code / Codex / OpenCode / Kimi Code / Cursor 最近的会话，选择后原生恢复或跨运行时接力。\n"
            "默认启动交互式 TUI（Textual），需要真实终端；非真实终端自动退化为 JSON。\n"
            "大模型 Agent 结构化查询请用 list/search/show/context/describe 子命令。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  pickup                 # 启动 TUI，交互式选择并接管终端\n"
            "  pickup --json          # 输出 JSON 会话列表后退出，不启动 TUI（旧格式）\n"
            "  pickup --json --limit 5  # JSON 模式，每个运行时最多 5 条\n"
            "  pickup describe        # 查看 list/search/show/context 等子命令的用法\n"
            "\n"
            "JSON 输出字段说明：\n"
            "  runtime        运行时标识（claude / codex / opencode / kimi / cursor）\n"
            "  id             会话 ID\n"
            "  title          会话标题（本地临时兜底，不调用 AI）\n"
            "  cwd            原会话工作目录\n"
            "  time           最后更新时间（人类可读）\n"
            "  mtime          最后更新时间（Unix 时间戳）\n"
            "  size_kb        历史文件大小（KB）\n"
            "  status         会话状态（已完成 / 待回复 / 已中断）\n"
            "  resume_command 恢复该会话的完整 shell 命令（可直接执行）\n"
            "  history_path   历史文件路径（Claude/Codex/Kimi 为 JSONL；OpenCode 为 SQLite 数据库）\n"
        ),
    )
    parser.add_argument("--limit", type=int, default=50, help="每个来源最多列出多少条")
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="以 JSON 格式输出会话列表后退出，不启动 TUI")
    parser.add_argument("--no-input", action="store_true", dest="no_input",
                        help="禁用交互并输出 JSON 会话列表，适合脚本和 Agent 调用")
    parser.add_argument("--no-keepalive", action="store_true", dest="no_keepalive",
                        help="本次启动不把会话包进后台保活（tmux），SSH 断开会话会跟着中断")
    parser.add_argument("--no-color", action="store_true", dest="no_color",
                        help="关闭彩色输出，也可设置 NO_COLOR 环境变量")
    parser.add_argument("-d", "--debug", "--verbose", action="store_true", dest="debug",
                        help="启用详细诊断日志，也可设置 PICKUP_DEBUG=1")
    parser.add_argument("-q", "--quiet", action="store_true", dest="quiet",
                        help="隐藏非必要的启动提示和诊断输出")
    parser.add_argument("--version", "-V", "-v", action="store_true", dest="show_version",
                        help="显示版本、安装路径与渠道后退出")
    parser.add_argument("--generate-titles", action="store_true", dest="generate_titles",
                        help=argparse.SUPPRESS)  # 内部用途：TUI 拉起的后台标题生成进程
    args = parser.parse_args()

    # 通用 CLI 开关先于任何输出和 TUI 导入生效；quiet 与 debug 同时出现时以 quiet 为准。
    if args.no_color:
        os.environ["NO_COLOR"] = "1"
    if args.quiet:
        os.environ.pop("PICKUP_DEBUG", None)
    elif args.debug:
        os.environ["PICKUP_DEBUG"] = "1"

    registry = default_registry()

    if args.show_version:
        report = updater.install_report()
        print(f"pickup {report['version']}")
        print(f"  package_file: {report['package_file']}")
        print(f"  python:       {report['python']}")
        print(f"  channel:      {report['channel']}")
        if report["checkout_root"]:
            print(f"  checkout:     {report['checkout_root']}")
            print(f"  from_checkout:{report['loaded_from_checkout']}")
        if report["stale_source_warning"]:
            print(f"  WARNING:      {report['stale_source_warning']}", file=sys.stderr)
        return

    if args.generate_titles:
        _run_title_daemon(registry, args.limit)
        return

    if args.json_mode or args.no_input:
        _output_json(registry, args.limit)
        return

    # 没有真实终端（管道、脚本、被 Agent 直接调用）时，Textual 无法接管终端；自动
    # 退化为 JSON 列表而不是崩溃。stdin/stdout 分开检测：任一端不是真实终端都不能进 TUI。
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _output_json(registry, args.limit)
        return

    _require_tmux()

    keepalive_on = keepalive.enabled(args.no_keepalive)
    if keepalive_on:
        keepalive.reap_idle()  # 顺带回收空闲太久没人管的后台保活会话，不常驻额外进程

    store = SessionStore(limit=args.limit, registry=registry)
    # store.load()（磁盘扫描 + JSON 解析）和下面的 _probe_osc_colours()（终端 OSC
    # 10/11 探测，最长阻塞 1.2s）互不依赖，串行执行会把两者耗时直接相加、白白
    # 拖长首屏。这里提前在后台线程里开始扫描，让它跟随后的 OSC 探测重叠执行；
    # UI 启动后 MainScreen 通过 store.wait_loaded() 等它跑完（大多数情况下，扫描
    # 会在 OSC 探测的等待期间就已经跑完，UI 挂载时可以直接渲染，不需要额外等待）。
    # 找不到任何会话不再在这里直接 sys.exit(1)：扫描本身现在是异步的，主进程无法
    # 同步判断"扫完了但真的一条都没有"，这个空状态提示改由 MainScreen 在
    # wait_loaded() 完成后展示（见 ui/main_screen.py 的 _update_header）。
    threading.Thread(target=store.load, daemon=True).start()

    # 拉起脱离终端的后台进程生成标题：用户秒退或原生恢复（execvp 替换进程）后仍继续，
    # TUI 通过轮询缓存文件拾取它逐批写入的标题。
    _spawn_title_daemon(args.limit)

    # 趁 Textual 接管终端前探测外层终端的前景/背景色（OSC 10/11）：内嵌面板聚焦时经
    # refresh-client -r 注入托管 pane，让 pane 内 agent 的深/浅主题检测拿到真实值。
    # 这一步仍然必须在 UI 启动前同步完成（EmbedPane/MainScreen 的深浅色主题判断
    # 依赖它），但现在跟上面的后台扫描线程是并行的，不再是"扫描 + 探测"首尾相加。
    # 读到应答即返回（快终端只需数毫秒，不受上限影响）；上限给足 0.25s 以覆盖
    # tmux/SSH 的往返，确保应答在 Textual 接管前收完——上限太短会让晚到的应答漏进
    # TUI 被当键盘输入注入搜索框（搜索框乱码 + 会话列表被过滤空）。不应答的终端最
    # 多白等 0.25s，且与上面的后台扫描线程并行，通常被扫描耗时掩盖。
    theme_mod._OSC_REPORT = _probe_osc_colours(timeout=0.25)
    observe.init(debug=bool(os.environ.get("PICKUP_DEBUG")))
    # 在源码树里开发却加载了 pipx/site-packages 副本时，启动前打一次 stderr 告警
    # （普通发行版用户 cwd 不在仓库内，不会触发）。
    stale = updater.stale_source_warning()
    if stale and sys.stderr.isatty() and not args.quiet:
        print(f"[pickup] {stale}", file=sys.stderr)
    if os.environ.get("PICKUP_DEBUG"):
        observe.debug(
            "osc_probe",
            report_present=theme_mod._OSC_REPORT is not None,
            in_tmux=bool(os.environ.get("TMUX")),
            theme_report=embed.supports_theme_report(),
        )
        print(f"[pickup debug] 外层终端 OSC 10/11 探测: {theme_mod._OSC_REPORT!r} "
              f"(tmux={'是' if os.environ.get('TMUX') else '否'}, "
              f"refresh -r 支持={'是' if embed.supports_theme_report() else '否'})",
              file=sys.stderr)

    from pickup.ui.app import run_app
    chosen = run_app(store, embed.available(args.no_keepalive), osc_report=theme_mod._OSC_REPORT)
    # 兜底关闭内嵌控制通道：pane 聚焦时打开的 `tmux -C attach` 控制 client 只有
    # c 键关分栏才会关，Esc 退出/回车全屏接管等退出路径不经那条分支——不在这里统一
    # 兜底就会把孤儿控制 client 留在保活服务端上。close_channel 无通道时是空操作。
    embed.close_channel()
    if isinstance(chosen, updater.RestartRequest):
        _restart_process()
        return
    if chosen is None:
        return

    try:
        _launch(chosen, store.registry, keepalive_on)
    except LaunchError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
