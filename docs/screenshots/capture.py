#!/usr/bin/env python3
"""用 Textual Pilot 渲染真实 TUI 并导出截图（SVG → PNG），供 README / 改动验收。

不读真实用户历史：夹具会话内容是虚构的。依赖：textual；SVG→PNG 优先 cairosvg
（`pip install cairosvg`），否则回退 ImageMagick `convert`（对 Rich 的 clipPath
文字支持很差，通常会出空白图，不推荐）。

用法（在 cli/ 目录）：

    python3 docs/screenshots/capture.py

产物写入本目录：list.png（左栏列表 + 右栏完整对话预览）。

真机运行中的 TUI 请用 **F12**（`MainScreen.action_save_screenshot`）导出到
`~/.cache/pickup/screenshots/`；勿把含真实对话的截图提交进仓库。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import pickup
from pickup.models import ConversationMessage
from pickup import session_key
from pickup.ui.app import PickupApp


OUT_DIR = Path(__file__).resolve().parent

# Rich/Textual SVG 默认 Fira Code，本机常无 CJK；换成 mono+CJK 本地字体，避免豆腐块。
_FONT_CSS = '"Noto Sans Mono CJK SC", "Noto Sans CJK SC", "Droid Sans Fallback", monospace'


def _demo_store():
    sessions = [
        {
            "source": "claude",
            "id": "demo-claude-1",
            "short_id": "demo1",
            "mtime": 1_700_000_100.0,
            "size_bytes": 4096,
            "size_kb": 4.0,
            "native_title": "Fix login flake",
            "fallback_title": "Fix login flake",
            "cwd": "/Users/demo/Codes/webapp",
            "cwd_display": "~/Codes/webapp",
            "live": False,
            "path": "/tmp/demo-claude-1.jsonl",
            "first_user_msg": "登录偶发失败，帮我定位",
            "last_user_msg": "再补一组回归测试",
            "last_agent_msg": "已加上 flaky 重试与断言",
        },
        {
            "source": "cursor",
            "id": "demo-cursor-1",
            "short_id": "democ1",
            "mtime": 1_700_000_050.0,
            "size_bytes": 2048,
            "size_kb": 2.0,
            "native_title": "Add Cursor runtime",
            "fallback_title": "Add Cursor runtime",
            "cwd": "/Users/demo/Codes/pickup",
            "cwd_display": "~/Codes/pickup",
            "live": False,
            "path": "/tmp/demo-cursor-1",
            "first_user_msg": "帮我加上 cursor-cli 支持",
            "last_user_msg": "右栏统一完整预览",
            "last_agent_msg": "已改选中即预览",
        },
        {
            "source": "codex",
            "id": "demo-codex-1",
            "short_id": "demox1",
            "mtime": 1_700_000_000.0,
            "size_bytes": 1024,
            "size_kb": 1.0,
            "native_title": "Tighten handoff prompt",
            "fallback_title": "Tighten handoff prompt",
            "cwd": "/Users/demo/Codes/pickup",
            "cwd_display": "~/Codes/pickup",
            "live": False,
            "path": "/tmp/demo-codex-1.jsonl",
            "first_user_msg": "接力提示词太散",
            "last_user_msg": "再压一版摘录",
            "last_agent_msg": "已收敛 digest 字段",
        },
    ]
    conversations = {
        "claude:demo-claude-1": [
            ConversationMessage("user", "登录偶发失败，帮我定位"),
            ConversationMessage(
                "assistant",
                "根因是并发下 session cookie 被覆盖。已加锁并补 flaky 回归。",
            ),
            ConversationMessage("user", "再补一组回归测试"),
            ConversationMessage("assistant", "已加上重试与断言，本地全绿。"),
        ],
        "cursor:demo-cursor-1": [
            ConversationMessage("user", "帮我加上 cursor-cli 支持"),
            ConversationMessage("assistant", "已按完整适配器接入扫描/恢复/接力/直启。"),
            ConversationMessage("user", "右栏统一完整预览"),
            ConversationMessage("assistant", "选中即完整对话；进行中仍走内嵌实时窗口。"),
        ],
        "codex:demo-codex-1": [
            ConversationMessage("user", "接力提示词太散"),
            ConversationMessage("assistant", "已把摘录收敛到原始需求 + 最近对话。"),
        ],
    }
    # 截图用稳定「已生成」标题，避免转圈兜底文案进 README
    demo_titles = {
        "claude:demo-claude-1": "Fix login flake",
        "cursor:demo-cursor-1": "Add Cursor runtime",
        "codex:demo-codex-1": "Tighten handoff prompt",
    }

    from unittest import mock
    from pickup.runtime import RuntimeRegistry

    runtimes = []
    for rid, name in (("claude", "Claude"), ("cursor", "Cursor"), ("codex", "Codex")):
        rt = mock.Mock()
        rt.id = rid
        rt.display_name = name
        rt.is_available.return_value = True
        rt.scan_signature.return_value = None
        own = [s for s in sessions if s["source"] == rid]
        rt.scan_sessions.return_value = own
        rt.load_conversation.side_effect = (
            lambda session, _rid=rid: list(conversations.get(f"{session['source']}:{session['id']}", []))
        )
        runtimes.append(rt)

    registry = RuntimeRegistry(tuple(runtimes))
    with mock.patch.object(pickup.titles, "load_cache", return_value={}):
        store = pickup.SessionStore(limit=20, registry=registry)
        store.load()
    # 注意：all_sessions() 自己会拿 store.lock；不可在持锁时再调，否则死锁。
    sessions_now = store.all_sessions()
    with store.lock:
        store.generating.clear()
        for session in sessions_now:
            key = session_key(session)
            store.display_titles[key] = demo_titles[key]
    for session in sessions_now:
        store.get_conversation(session)
    return store


def _prepare_svg(svg_text: str) -> str:
    """去掉远程 @font-face，换成带 CJK 的本地字体，并去掉 textLength/逐行 clip。

    Rich 按 Fira Code 字宽写了 textLength；换成 Droid 后字宽对不上，cairosvg
    会把字形压成豆腐块。去掉强制字宽与行裁剪后，截图可读（间距略松一点可接受）。
    """
    svg_text = re.sub(r"@font-face\s*\{.*?\}", "", svg_text, flags=re.S)
    svg_text = re.sub(
        r"font-family:\s*Fira Code,\s*monospace;",
        f"font-family: {_FONT_CSS};",
        svg_text,
    )
    svg_text = re.sub(
        r'font-family:\s*"Fira Code"',
        f"font-family: {_FONT_CSS}",
        svg_text,
    )
    svg_text = re.sub(r'\s+textLength="[^"]*"', "", svg_text)
    svg_text = re.sub(r'\s+clip-path="url\([^"]+\)"', "", svg_text)
    # Droid Sans Fallback 无真正的 bold；合成粗体时 cairosvg 常把字形渲成空框。
    svg_text = re.sub(r"font-weight:\s*bold;?", "", svg_text)
    return svg_text


def _svg_to_png(svg_path: Path, png_path: Path) -> None:
    prepared = _prepare_svg(svg_path.read_text(encoding="utf-8"))
    prepared_path = svg_path.with_suffix(".prepared.svg")
    prepared_path.write_text(prepared, encoding="utf-8")

    # cairosvg 可能装在另一份 Python（如 python3.11）；本解释器没有就 subprocess 调。
    converters: list[list[str]] = []
    try:
        from cairosvg import svg2png  # noqa: F401
        converters.append([sys.executable, "-c", _CAIRO_SNIPPET, str(prepared_path), str(png_path)])
    except ImportError:
        pass
    for candidate in ("python3.11", "python3.12", "python3"):
        converters.append([candidate, "-c", _CAIRO_SNIPPET, str(prepared_path), str(png_path)])

    last_err: Exception | None = None
    for cmd in converters:
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return
        except (OSError, subprocess.CalledProcessError) as exc:
            last_err = exc
            continue

    # 最后才回退 ImageMagick：对 Rich 的 per-glyph clipPath 支持很差，常出空白图。
    try:
        subprocess.check_call(
            ["convert", "-background", "#121212", str(prepared_path), str(png_path)],
        )
        print(
            "warning: cairosvg 不可用，已回退 ImageMagick convert；"
            "Rich SVG 常被渲成空白，请 pip install cairosvg",
            file=sys.stderr,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "无法将 SVG 转为 PNG：请安装 cairosvg（pip install cairosvg）"
        ) from (last_err or exc)


_CAIRO_SNIPPET = (
    "import sys; from cairosvg import svg2png; "
    "svg2png(url=sys.argv[1], write_to=sys.argv[2])"
)

async def _capture() -> None:
    store = _demo_store()
    app = PickupApp(store, embed_ok=True, osc_report=None)
    async with app.run_test(size=(140, 36)) as pilot:
        await pilot.pause(delay=0.4)
        # 跳过「新建会话」钉，选中第一条真实会话 → 右栏完整预览
        await pilot.press("down")
        await pilot.pause(delay=0.5)
        with tempfile.TemporaryDirectory() as td:
            svg = app.save_screenshot("list.svg", path=td)
            _svg_to_png(Path(td) / Path(svg).name, OUT_DIR / "list.png")
        print(f"wrote {OUT_DIR / 'list.png'}")


def main() -> None:
    asyncio.run(_capture())


if __name__ == "__main__":
    main()
