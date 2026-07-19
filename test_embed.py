"""embed.py 的单元测试：tmux 命令拼装、SGR 画面解析、Cell→Style 映射、按键翻译。

tmux 子进程一律 mock，不需要真实 tmux，可在无终端环境跑。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import unittest
import unittest.mock as mock

import embed
from models import LaunchPlan


def _run_completed_ok(*_args, **_kwargs):
    return subprocess.CompletedProcess(args=[], returncode=0)


class AvailableTests(unittest.TestCase):
    def test_available_with_tmux(self):
        with mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(embed.available())

    def test_unavailable_without_tmux(self):
        with mock.patch.object(embed.shutil, "which", return_value=None), \
                mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(embed.available())

    def test_unavailable_when_explicitly_disabled(self):
        with mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"):
            self.assertFalse(embed.available(disabled_flag=True))
            with mock.patch.dict("os.environ", {"PICKUP_KEEPALIVE": "0"}, clear=True):
                self.assertFalse(embed.available())
            with mock.patch.dict("os.environ", {"SC_KEEPALIVE": "0"}, clear=True):
                self.assertFalse(embed.available())

    def test_ignores_tmux_env_nesting(self):
        # 用户在自己的 tmux 里跑 pickup 时 keepalive.enabled() 会关闭，但内嵌不 attach，
        # TMUX/STY 不影响可用性。
        with mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.dict("os.environ", {"TMUX": "/tmp/tmux-1000/default,1,0"}, clear=True):
            self.assertTrue(embed.available())


class HostSessionTests(unittest.TestCase):
    def test_argv_detached_with_size_and_env(self):
        plan = LaunchPlan(argv=("claude", "--resume", "abc"), cwd="/tmp/work")
        with mock.patch.object(embed.subprocess, "run", side_effect=_run_completed_ok) as run, \
                mock.patch.object(embed.keepalive, "_ensure_config_file", return_value="/tmp/k.conf"), \
                mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"):
            name = embed.host_session(plan, "claude", "0123456789abcdef", 120, 40)
        self.assertEqual(name, "pickup-claude-01234567")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["tmux", "-L", "pickup-keepalive"])
        self.assertIn("-f", argv)
        joined = " ".join(argv)
        self.assertIn("new-session -d -P -F #{pane_id} -s pickup-claude-01234567 -x 120 -y 40", joined)
        self.assertIn("-c /tmp/work", joined)
        for env_pair in ("PICKUP_RUNTIME=claude", "PICKUP_SESSION_ID=0123456789abcdef",
                         "SC_RUNTIME=claude", "SC_SESSION_ID=0123456789abcdef"):
            self.assertIn(f"-e {env_pair}", joined)
        self.assertEqual(argv[-2:], ["--resume", "abc"])

    def test_duplicate_session_falls_back_to_reuse(self):
        plan = LaunchPlan(argv=("claude",), cwd=None)

        def run_side_effect(argv, **_kwargs):
            if "new-session" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(args=argv, returncode=0)  # has-session 成功

        with mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect), \
                mock.patch.object(embed.keepalive, "_ensure_config_file", return_value="/tmp/k.conf"), \
                mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"):
            self.assertEqual(embed.host_session(plan, "claude", "0123456789abcdef", 80, 24),
                             "pickup-claude-01234567")

    def test_create_failure_raises_embed_error(self):
        plan = LaunchPlan(argv=("claude",), cwd=None)

        def run_side_effect(argv, **_kwargs):
            raise subprocess.CalledProcessError(1, argv)  # new-session 与 has-session 都失败

        with mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect), \
                mock.patch.object(embed.keepalive, "_ensure_config_file", return_value="/tmp/k.conf"), \
                mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"):
            with self.assertRaises(embed.EmbedError):
                embed.host_session(plan, "claude", "0123456789abcdef", 80, 24)


class SessionIoTests(unittest.TestCase):
    def test_capture_returns_decoded_text(self):
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"hi \x1b[31mred\x1b[0m"):
            self.assertEqual(embed.capture("sc-claude-1"), "hi \x1b[31mred\x1b[0m")

    def test_capture_none_on_failure(self):
        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=subprocess.CalledProcessError(1, [])):
            self.assertIsNone(embed.capture("sc-claude-1"))

    def test_is_alive(self):
        with mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(embed.subprocess, "run", side_effect=_run_completed_ok):
            self.assertTrue(embed.is_alive("sc-claude-1"))
        with mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(embed.subprocess, "run",
                                  side_effect=subprocess.CalledProcessError(1, [])):
            self.assertFalse(embed.is_alive("sc-claude-1"))

    def test_send_literal_and_key(self):
        calls = []

        def run_side_effect(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect):
            embed.send_literal("sc-claude-1", "你好 world")
            embed.send_key("sc-claude-1", "Enter")
            embed.send_key("sc-claude-1", "C-c")
        self.assertEqual(calls[0][3:],
                         ["send-keys", "-l", "-t", "sc-claude-1", "--", "你好 world"])
        self.assertEqual(calls[1][-2:], ["--", "Enter"])
        self.assertEqual(calls[2][-2:], ["--", "C-c"])

    def test_paste_uses_buffer_with_bracketed_flag(self):
        calls = []

        def run_side_effect(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect):
            embed.paste("sc-claude-1", "line1\nline2")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][3:6], ["set-buffer", "-b", "pickup-embed"])
        self.assertEqual(calls[0][-1], "line1\nline2")
        self.assertEqual(calls[1][3:6], ["paste-buffer", "-p", "-d"])
        self.assertEqual(calls[1][-1], "sc-claude-1")

    def test_pane_state_parses_formats(self):
        # (光标 x, 光标 y, 光标可见, 程序申请鼠标, SGR 鼠标模式, 回滚行数)
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"12|7|1|1|1|234\n"):
            self.assertEqual(embed.pane_state("s"), (12, 7, True, True, True, 234))
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"0|0|0|0|0|0\n"):
            self.assertEqual(embed.pane_state("s"), (0, 0, False, False, False, 0))
        # 旧版 5 段输出（解析失败兜底 None，不崩）
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"12|7|1|1|1\n"):
            self.assertIsNone(embed.pane_state("s"))

    def test_pane_state_none_on_failure(self):
        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=subprocess.CalledProcessError(1, [])):
            self.assertIsNone(embed.pane_state("s"))
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"garbage"):
            self.assertIsNone(embed.pane_state("s"))


class ControlModeTests(unittest.TestCase):
    """控制模式转义、SGR 鼠标序列、tmux 版本门控、copy-mode 原语（无通道时走 fork）。"""

    def setUp(self):
        embed._tmux_version.cache_clear()  # lru_cache 跨用例污染
        self.addCleanup(embed._tmux_version.cache_clear)

    def test_ctl_quote_clean_ascii_passthrough(self):
        self.assertEqual(embed._ctl_quote("send-keys"), "send-keys")
        self.assertEqual(embed._ctl_quote("%1"), "%1")
        self.assertEqual(embed._ctl_quote("-t"), "-t")

    def test_ctl_quote_escapes_specials(self):
        self.assertEqual(embed._ctl_quote('a "b"'), '"a \\"b\\""')
        self.assertEqual(embed._ctl_quote("$HOME"), '"\\$HOME"')
        self.assertEqual(embed._ctl_quote("a`b"), '"a\\`b"')
        self.assertEqual(embed._ctl_quote("a\\b"), '"a\\\\b"')
        self.assertEqual(embed._ctl_quote(" "), '" "')  # 空格不在安全集，需包裹

    def test_sgr_mouse_sequence(self):
        self.assertEqual(embed.sgr_mouse_sequence(64, 5, 3), "\x1b[<64;5;3M")
        self.assertEqual(embed.sgr_mouse_sequence(65, 1, 1), "\x1b[<65;1;1M")

    def test_send_mouse_sequence_nonblocking_and_forked(self):
        """滚轮序列必须排队到后台线程发送：调用方（UI 主线程）零 fork、立即返回。"""
        delivered = threading.Event()
        calls = []

        def fake_send(name, seq, *, force_fork=False):
            calls.append((name, seq, force_fork))
            delivered.set()

        with mock.patch.object(embed, "send_literal", side_effect=fake_send):
            started = time.monotonic()
            embed.send_mouse_sequence("sc-claude-1", "\x1b[<65;10;6M")
            elapsed = time.monotonic() - started
            self.assertTrue(delivered.wait(2.0), "后台线程应投递滚轮序列")
        self.assertLess(elapsed, 0.05, "send_mouse_sequence 自身不得阻塞调用方")
        self.assertEqual(calls, [("sc-claude-1", "\x1b[<65;10;6M", True)])

    def test_send_mouse_sequence_queue_cap_drops_oldest(self):
        """积压超过 _WHEEL_QUEUE_MAX 时丢弃最旧事件：内层重绘不过来就跳中间帧，
        不能停手后还在追滚动。"""
        gate = threading.Event()
        drained = threading.Event()
        delivered = []

        def fake_send(name, seq, *, force_fork=False):
            if not delivered:
                gate.wait(2.0)  # 第一条卡住发送线程，模拟内层程序重绘慢
            delivered.append(seq)
            if seq == "seq29":
                drained.set()

        with mock.patch.object(embed, "send_literal", side_effect=fake_send), \
                mock.patch.object(embed, "_WHEEL_SEND_INTERVAL", 0):
            embed.send_mouse_sequence("sc-claude-1", "first")
            # 等发送线程取走 first（队列清空即说明已进入发送、卡在 gate 上）
            for _ in range(200):
                with embed._wheel_lock:
                    if not embed._wheel_queues.get("sc-claude-1"):
                        break
                time.sleep(0.01)
            else:
                self.fail("发送线程未取走第一条")
            time.sleep(0.05)  # 留出「取走 → 进入 fake_send 卡 gate」的窗口
            for i in range(30):
                embed.send_mouse_sequence("sc-claude-1", f"seq{i}")
            gate.set()
            self.assertTrue(drained.wait(5.0), "队列应在放行后全部投递")
        # first + 队列上限 12 条（seq18..seq29，最旧的 seq0..seq17 被丢弃）
        self.assertEqual(delivered, ["first"] + [f"seq{i}" for i in range(18, 30)])

    def test_supports_theme_report_version_gate(self):
        for ver, expected in ((b"tmux 3.5a\n", True), (b"tmux 3.4\n", False),
                              (b"tmux next-3.7\n", True), (b"tmux 2.9\n", False)):
            embed._tmux_version.cache_clear()
            with mock.patch.object(embed.subprocess, "check_output", return_value=ver):
                self.assertEqual(embed.supports_theme_report(), expected, ver)

    def test_supports_theme_report_false_when_tmux_missing(self):
        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=FileNotFoundError()):
            self.assertFalse(embed.supports_theme_report())

    def test_capture_scroll_offset_inserts_range_flags(self):
        captured = {}

        def check_output_side_effect(argv, **kwargs):
            captured["argv"] = argv
            return b"screen text"

        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=check_output_side_effect):
            # 应用层滚动：offset>0 时抓「live 窗口上移 offset 行」的历史窗口，
            # 公式 -S -offset -E (h-1-offset)（真 tmux 实测钉死）；
            # copy-mode 滚动对 capture 不可见（实测），不能用
            self.assertEqual(embed.capture("s", scroll_offset=10, pane_height=40),
                             "screen text")
        argv = captured["argv"]
        self.assertEqual(argv[4:8], ["-S", "-10", "-E", "29"])

    def test_capture_live_without_range_flags(self):
        captured = {}

        def check_output_side_effect(argv, **kwargs):
            captured["argv"] = argv
            return b"x"

        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=check_output_side_effect):
            embed.capture("s")
        self.assertNotIn("-S", captured["argv"])


class TranslateTextualKeyTests(unittest.TestCase):
    def test_enter_tab_backspace_escape(self):
        self.assertEqual(embed.translate_textual_key("enter"), ("keys", "Enter"))
        self.assertEqual(embed.translate_textual_key("return"), ("keys", "Enter"))
        self.assertEqual(embed.translate_textual_key("tab"), ("keys", "Tab"))
        self.assertEqual(embed.translate_textual_key("backspace"), ("keys", "BSpace"))
        self.assertEqual(embed.translate_textual_key("escape"), ("keys", "Escape"))

    def test_control_letters(self):
        self.assertEqual(embed.translate_textual_key("ctrl+c"), ("keys", "C-c"))
        self.assertEqual(embed.translate_textual_key("ctrl+z"), ("keys", "C-z"))
        self.assertEqual(embed.translate_textual_key("ctrl+a"), ("keys", "C-a"))

    def test_special_keys(self):
        self.assertEqual(embed.translate_textual_key("up"), ("keys", "Up"))
        self.assertEqual(embed.translate_textual_key("pageup"), ("keys", "PPage"))
        self.assertEqual(embed.translate_textual_key("f5"), ("keys", "F5"))
        self.assertEqual(embed.translate_textual_key("delete"), ("keys", "DC"))
        self.assertEqual(embed.translate_textual_key("insert"), ("keys", "IC"))

    def test_untranslatable(self):
        self.assertIsNone(embed.translate_textual_key("x"))
        self.assertIsNone(embed.translate_textual_key("shift+up"))


class ParseScreenTests(unittest.TestCase):
    def test_plain_text_and_padding(self):
        grid = embed.parse_screen("abc", 5, 2)
        self.assertEqual("".join(c.ch for c in grid[0]), "abc  ")
        self.assertEqual("".join(c.ch for c in grid[1]), "     ")
        self.assertEqual(len(grid), 2)

    def test_basic_sgr_color_and_reset(self):
        grid = embed.parse_screen("a\x1b[1;31mb\x1b[0mc", 3, 1)
        row = grid[0]
        self.assertEqual((row[0].ch, row[0].fg, row[0].bold), ("a", -1, False))
        self.assertEqual((row[1].ch, row[1].fg, row[1].bold), ("b", 1, True))
        self.assertEqual((row[2].ch, row[2].fg, row[2].bold), ("c", -1, False))

    def test_256_and_truecolor(self):
        grid = embed.parse_screen("\x1b[38;5;200mx\x1b[48;2;255;0;0my", 2, 1)
        self.assertEqual(grid[0][0].fg, 200)
        self.assertEqual(grid[0][1].bg, 196)  # 纯红量化到 196

    def test_bright_colors_and_reverse(self):
        grid = embed.parse_screen("\x1b[92;7mz", 1, 1)
        self.assertEqual(grid[0][0].fg, 10)
        self.assertTrue(grid[0][0].reverse)

    def test_wide_char_occupies_two_cells(self):
        grid = embed.parse_screen("a好b", 4, 1)
        row = grid[0]
        self.assertEqual(row[1].ch, "好")
        self.assertFalse(row[1].wide_cont)
        self.assertTrue(row[2].wide_cont)
        self.assertEqual(row[3].ch, "b")

    def test_wide_char_cut_at_right_edge_becomes_blank(self):
        grid = embed.parse_screen("ab好", 3, 1)
        row = grid[0]
        self.assertEqual((row[0].ch, row[1].ch, row[2].ch), ("a", "b", " "))

    def test_combining_char_merges_into_previous_cell(self):
        grid = embed.parse_screen("éx", 2, 1)
        self.assertEqual(grid[0][0].ch, "é")
        self.assertEqual(grid[0][1].ch, "x")

    def test_non_sgr_sequences_are_skipped(self):
        grid = embed.parse_screen("a\x1b[2Kb\x1b(Bc", 3, 1)
        self.assertEqual("".join(c.ch for c in grid[0]), "abc")

    def test_height_truncation(self):
        grid = embed.parse_screen("l1\nl2\nl3", 2, 2)
        self.assertEqual(len(grid), 2)
        self.assertEqual(grid[1][0].ch, "l")


class RgbQuantizeTests(unittest.TestCase):
    def test_pure_colors(self):
        self.assertEqual(embed._rgb_to_256(255, 0, 0), 196)
        self.assertEqual(embed._rgb_to_256(0, 255, 0), 46)

    def test_grays(self):
        self.assertEqual(embed._rgb_to_256(0, 0, 0), 16)
        self.assertEqual(embed._rgb_to_256(255, 255, 255), 231)
        mid = embed._rgb_to_256(128, 128, 128)
        self.assertTrue(232 <= mid <= 255)


class CellStyleTests(unittest.TestCase):
    def test_default_colors_are_none(self):
        style = embed.cell_style(embed.Cell("x"))
        self.assertIsNone(style.color)
        self.assertIsNone(style.bgcolor)

    def test_colors_map_to_ansi_256(self):
        style = embed.cell_style(embed.Cell("x", fg=200, bg=17))
        self.assertEqual(style.color.number, 200)
        self.assertEqual(style.bgcolor.number, 17)

    def test_attr_flags(self):
        style = embed.cell_style(embed.Cell("x", bold=True, dim=True, underline=True, reverse=True))
        self.assertTrue(style.bold)
        self.assertTrue(style.dim)
        self.assertTrue(style.underline)
        self.assertTrue(style.reverse)

    def test_same_combo_returns_cached_equal_style(self):
        a = embed.cell_style(embed.Cell("x", fg=1, bg=2))
        b = embed.cell_style(embed.Cell("y", fg=1, bg=2))  # ch 不同不影响样式缓存键
        self.assertEqual(a, b)


class GridToTextTests(unittest.TestCase):
    def test_merges_adjacent_cells_with_same_style_into_one_span(self):
        grid = embed.parse_screen("a\x1b[1;31mb\x1b[0mc", 3, 1)
        rows = embed.grid_to_text(grid)
        self.assertEqual(len(rows), 1)
        text, spans = rows[0]
        self.assertEqual(text, "abc")
        self.assertEqual([(s, e) for s, e, _ in spans], [(0, 1), (1, 2), (2, 3)])
        self.assertNotEqual(spans[0][2], spans[1][2])

    def test_wide_char_continuation_cell_excluded_from_text(self):
        grid = embed.parse_screen("a好b", 4, 1)
        text, _ = embed.grid_to_text(grid)[0]
        self.assertEqual(text, "a好b")


@unittest.skipUnless(shutil.which("tmux"), "需要真实 tmux")
class ControlChannelIntegrationTests(unittest.TestCase):
    """真 tmux 上的控制通道端到端：命令下发、%output 事件、copy-mode、主题注入、死亡检测。

    用独立 socket（pickup-test-ctl），与 pickup-keepalive 上的真实会话完全隔离；
    通过 patch keepalive._BASE_ARGV 让 embed 的全部 tmux 调用指向测试 socket。
    """

    SOCKET = "pickup-test-ctl"
    SESSION = "ctl-it"

    @classmethod
    def setUpClass(cls):
        subprocess.run(["tmux", "-L", cls.SOCKET, "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def setUp(self):
        subprocess.run(["tmux", "-L", self.SOCKET, "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "-L", self.SOCKET, "new-session", "-d",
                        "-s", self.SESSION, "-x", "100", "-y", "30"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        patcher = mock.patch.object(embed.keepalive, "_BASE_ARGV",
                                    ("tmux", "-L", self.SOCKET))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(embed.close_channel)
        time.sleep(0.3)  # 等 shell 就绪，避免首批按键被 init 吃掉

    def tearDown(self):
        subprocess.run(["tmux", "-L", self.SOCKET, "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _capture(self) -> str:
        return embed.capture(self.SESSION) or ""

    def _wait_text(self, needle: str, timeout: float = 4.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self._capture()
            if needle in text:
                return text
            time.sleep(0.1)
        self.fail(f"{timeout}s 内画面未出现 {needle!r}：\n{self._capture()}")

    def test_channel_send_reaches_pane_without_fork(self):
        fired = threading.Event()
        ch = embed.open_channel(self.SESSION, on_output=fired.set)
        self.assertIsNotNone(ch)
        embed.send_literal(self.SESSION, "echo chan-$((40+2))")
        embed.send_key(self.SESSION, "Enter")
        self._wait_text("chan-42")
        self.assertFalse(ch.dead)

    def test_output_event_fires_on_pane_output(self):
        fired = threading.Event()
        embed.open_channel(self.SESSION, on_output=fired.set)
        embed.send_literal(self.SESSION, "echo hello-event")
        embed.send_key(self.SESSION, "Enter")
        self.assertTrue(fired.wait(4.0), "%output 事件应在 pane 产生输出后触发")

    def test_capture_scroll_offset_reads_history(self):
        """应用层滚动的真 tmux 验证：静态会话里 offset 抓到的历史窗口内容上移。

        copy-mode 滚动对 capture 不可见（scroll_position 变但 pane buffer 不变），
        内嵌滚动必须走 capture-pane -S/-E 历史窗口——本测试钉死这条路径。"""
        subprocess.run(["tmux", "-L", self.SOCKET, "kill-session", "-t", self.SESSION],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "-L", self.SOCKET, "new-session", "-d", "-s", self.SESSION,
                        "-x", "60", "-y", "20", "seq 1 100; sleep 60"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        live = embed.capture(self.SESSION)
        self.assertIsNotNone(live)
        self.assertIn("100", live)
        back = embed.capture(self.SESSION, scroll_offset=30, pane_height=20)
        self.assertIsNotNone(back)
        # 新公式窗口 = live(82..100) 上移 30 = 52..71
        self.assertIn("60", back, f"上滚 30 行应看到 52..71 区间内容：{back!r}")
        self.assertNotIn("100", back)
        self.assertNotIn("95", back, f"窗口上界不得超过 71：{back!r}")

    def test_resize_via_channel(self):
        embed.open_channel(self.SESSION)
        embed.resize(self.SESSION, 90, 25)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            out = subprocess.check_output(
                ["tmux", "-L", self.SOCKET, "display-message", "-p", "-t", self.SESSION,
                 "#{window_width}x#{window_height}"], timeout=2).decode().strip()
            if out == "90x25":
                return
            time.sleep(0.1)
        self.fail("resize-window 经控制通道未生效")

    @unittest.skipUnless(embed.supports_theme_report(), "refresh-client -r 需要 tmux 3.5a+")
    def test_report_theme_answers_pane_osc11_query(self):
        """注入 OSC 11 应答后，pane 内程序的背景色查询应拿到注入值而非超时。"""
        ch = embed.open_channel(self.SESSION)
        self.assertIsNotNone(ch.pane_id)
        report = b"\x1b]11;rgb:abcd/1234/5678\x07"
        self.assertTrue(embed.report_theme(ch, report))
        probe = ("python3 -c 'import os,sys,termios,tty,select;"
                 "fd=sys.stdin.fileno();old=termios.tcgetattr(fd);tty.setraw(fd);"
                 "os.write(1,b\"\\x1b]11;?\\x07\");"
                 "r,_,_=select.select([fd],[],[],2.5);"
                 "d=os.read(fd,64) if r else b\"TIMEOUT\";"
                 "termios.tcsetattr(fd,termios.TCSADRAIN,old);"
                 "print(\"RESP\",repr(d))'")
        embed.send_literal(self.SESSION, probe)
        embed.send_key(self.SESSION, "Enter")
        # 等 "RESP b'"（python 输出行的特征前缀）：直接等 "RESP" 会匹配到命令行
        # 回显里 print("RESP",...) 的字样，在命令尚未执行完时就提前返回
        text = self._wait_text("RESP b'", timeout=8.0)
        # tmux 把注入的 16-bit RGB 归一化成高 8 位重复格式（abcd→abab/1212/5656），
        # 断言归一化后的值；TIMEOUT 出现则说明 pane 内查询无人应答（机制失效）
        self.assertIn("abab/1212/5656", text, f"pane 内 OSC 11 应答应为注入值：{text!r}")

    def test_channel_death_falls_back_to_fork(self):
        ch = embed.open_channel(self.SESSION)
        ch.close()
        embed.close_channel()
        # 通道死亡后 send_literal 应自动回退外部 fork 路径，文本依然到达
        embed.send_literal(self.SESSION, "echo fork-$((1+1))")
        embed.send_key(self.SESSION, "Enter")
        self._wait_text("fork-2")

    @unittest.skipUnless(embed.supports_theme_report(), "refresh-client -r 需要 tmux 3.5a+")
    def test_host_session_with_osc_report_answers_program_own_first_query(self):
        """回归测试：真机排查过的竞态——host_session(osc_report=...) 必须让**托管
        程序自己在启动瞬间发起的第一次 OSC 11 查询**就拿到正确颜色，而不是像
        test_report_theme_answers_pane_osc11_query 那样"先注入、再手动在已有
        shell 里补发一次查询"（那条测试测的是机制本身能不能用，不测时序）。

        真机发现过两种更差的坏法，这里都要防止回归：
        1. 完全不早注入：托管程序自己启动时查，大概率拿到 tmux 的默认猜测值
           （通常是纯黑），不是真实终端色。
        2. 注入后立刻关闭控制通道：比什么都不做还差——`refresh-client -r` 依赖
           "当前有控制模式客户端连接着"这个前提，通道一关，注入的颜色跟着失效，
           托管程序此后一次都查不到（而不是查到默认猜测值）。
        """
        subprocess.run(["tmux", "-L", self.SOCKET, "kill-session", "-t", self.SESSION],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with mock.patch.object(embed.keepalive, "_BASE_ARGV", ("tmux", "-L", self.SOCKET)):
            probe_script = (
                "import os,sys,termios,tty,select,time;"
                "fd=sys.stdin.fileno();old=termios.tcgetattr(fd);tty.setraw(fd);"
                "os.write(1,b'\\x1b]11;?\\x07');"
                "r,_,_=select.select([fd],[],[],1.5);"
                "d=os.read(fd,64) if r else b'TIMEOUT';"
                "termios.tcsetattr(fd,termios.TCSADRAIN,old);"
                "print('RESP', repr(d));"
                # 探测完立刻退出会让这个 pane（乃至整个测试用 tmux server，因为
                # 它是唯一会话）跟着关掉，capture 就再也读不到内容——留一个死
                # 循环撑住进程，直到测试自己 kill-session 收尾。
                "time.sleep(60)"
            )
            plan = LaunchPlan(("python3", "-c", probe_script), None)
            report = b"\x1b]11;rgb:abcd/1234/5678\x07"
            name = embed.host_session(plan, "themetest", "themetest-race", 80, 24,
                                      osc_report=report)
            self.addCleanup(lambda: subprocess.run(
                ["tmux", "-L", self.SOCKET, "kill-session", "-t", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            deadline = time.monotonic() + 6.0
            text = ""
            while time.monotonic() < deadline:
                text = embed.capture(name) or ""
                if "RESP" in text:
                    break
                time.sleep(0.1)
            self.assertIn("RESP", text, f"托管程序自己的首次查询应已完成：{text!r}")
            self.assertNotIn("TIMEOUT", text, f"首次查询不应超时无应答：{text!r}")
            self.assertIn("abab/1212/5656", text,
                          f"首次查询应拿到注入的真实颜色，而不是 tmux 的默认猜测值：{text!r}")


if __name__ == "__main__":
    unittest.main()
