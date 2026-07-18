#!/usr/bin/env python3
"""标题生成器抽象层测试:生成器选择、各 CLI 的 argv 契约、titles.py 接线。"""

from __future__ import annotations

import os
import unittest
from unittest import mock

import titlegen
import titles


class _FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def _which(available: set[str]):
    """构造只认 available 里那些命令的 shutil.which 替身。"""
    return lambda exe: f"/usr/bin/{exe}" if exe in available else None


_NO_ENV = {titlegen.ENV_GENERATOR: "", titlegen.ENV_MODEL: ""}


class ResolveGeneratorTests(unittest.TestCase):
    def test_env_configured_generator_wins(self) -> None:
        with mock.patch.object(titlegen.shutil, "which", side_effect=_which({"claude", "codex"})), \
                mock.patch.dict(os.environ, {**_NO_ENV, titlegen.ENV_GENERATOR: "codex"}):
            generator = titlegen.resolve_generator()
        self.assertIsNotNone(generator)
        self.assertEqual(generator.id, "codex")

    def test_falls_back_to_availability_order_when_env_target_missing(self) -> None:
        # 指定的生成器本机没装时,退回可用性顺序,标题照常生成
        with mock.patch.object(titlegen.shutil, "which", side_effect=_which({"claude"})), \
                mock.patch.dict(os.environ, {**_NO_ENV, titlegen.ENV_GENERATOR: "nonexistent"}):
            generator = titlegen.resolve_generator()
        self.assertEqual(generator.id, "claude")

    def test_defaults_to_claude_first(self) -> None:
        with mock.patch.object(titlegen.shutil, "which", side_effect=_which({"claude", "codex"})), \
                mock.patch.dict(os.environ, _NO_ENV):
            generator = titlegen.resolve_generator()
        self.assertEqual(generator.id, "claude")

    def test_returns_none_when_nothing_available(self) -> None:
        with mock.patch.object(titlegen.shutil, "which", side_effect=_which(set())), \
                mock.patch.dict(os.environ, _NO_ENV):
            self.assertIsNone(titlegen.resolve_generator())


class ClaudeGeneratorTests(unittest.TestCase):
    def test_argv_stdin_and_stdout(self) -> None:
        calls: dict = {}

        def fake_run(argv, **kwargs):
            calls["argv"] = list(argv)
            calls["input"] = kwargs.get("input")
            return _FakeProc(stdout='{"claude:s1": "标题"}')

        with mock.patch.object(titlegen.subprocess, "run", side_effect=fake_run), \
                mock.patch.dict(os.environ, _NO_ENV):
            out = titlegen.ClaudeTitleGenerator().generate("prompt 内容", timeout=5)

        self.assertEqual(calls["argv"], ["claude", "-p", "--model", "haiku"])
        self.assertEqual(calls["input"], "prompt 内容")
        self.assertEqual(out, '{"claude:s1": "标题"}')

    def test_model_env_override(self) -> None:
        calls: dict = {}

        def fake_run(argv, **kwargs):
            calls["argv"] = list(argv)
            return _FakeProc(stdout="ok")

        with mock.patch.object(titlegen.subprocess, "run", side_effect=fake_run), \
                mock.patch.dict(os.environ, {**_NO_ENV, titlegen.ENV_MODEL: "sonnet"}):
            titlegen.ClaudeTitleGenerator().generate("p", timeout=5)

        self.assertEqual(calls["argv"], ["claude", "-p", "--model", "sonnet"])

    def test_nonzero_exit_returns_none(self) -> None:
        with mock.patch.object(titlegen.subprocess, "run", return_value=_FakeProc(returncode=1)), \
                mock.patch.dict(os.environ, _NO_ENV):
            self.assertIsNone(titlegen.ClaudeTitleGenerator().generate("p", timeout=5))

    def test_oserror_returns_none(self) -> None:
        with mock.patch.object(titlegen.subprocess, "run", side_effect=OSError()), \
                mock.patch.dict(os.environ, _NO_ENV):
            self.assertIsNone(titlegen.ClaudeTitleGenerator().generate("p", timeout=5))


class CodexGeneratorTests(unittest.TestCase):
    def _run_with_fake(self, fake_run):
        with mock.patch.object(titlegen.subprocess, "run", side_effect=fake_run), \
                mock.patch.dict(os.environ, _NO_ENV):
            return titlegen.CodexTitleGenerator().generate("prompt 内容", timeout=5)

    def test_reads_answer_from_output_last_message_file(self) -> None:
        seen: dict = {}

        def fake_run(argv, **kwargs):
            argv = list(argv)
            seen["argv"] = argv
            seen["input"] = kwargs.get("input")
            out_path = argv[argv.index("-o") + 1]
            seen["out_path"] = out_path
            with open(out_path, "w", encoding="utf-8") as f:
                f.write('{"codex:s1": "标题"}')
            return _FakeProc(stdout="事件日志噪音,不能当结果用")

        out = self._run_with_fake(fake_run)

        self.assertEqual(out, '{"codex:s1": "标题"}')
        self.assertEqual(seen["input"], "prompt 内容")
        self.assertEqual(seen["argv"][:2], ["codex", "exec"])
        for flag in ("--ephemeral", "--skip-git-repo-check"):
            self.assertIn(flag, seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("-s") + 1], "read-only")
        self.assertEqual(seen["argv"][-1], "-")  # prompt 从 stdin 读
        self.assertNotIn("-m", seen["argv"])  # 未配置模型时不带 -m,用 codex 自己的默认模型
        self.assertFalse(os.path.exists(seen["out_path"]))  # 临时文件用完即删

    def test_model_env_adds_m_flag(self) -> None:
        seen: dict = {}

        def fake_run(argv, **kwargs):
            argv = list(argv)
            seen["argv"] = argv
            with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as f:
                f.write("ok")
            return _FakeProc()

        with mock.patch.object(titlegen.subprocess, "run", side_effect=fake_run), \
                mock.patch.dict(os.environ, {**_NO_ENV, titlegen.ENV_MODEL: "gpt-5.1-codex-mini"}):
            titlegen.CodexTitleGenerator().generate("p", timeout=5)

        self.assertEqual(seen["argv"][seen["argv"].index("-m") + 1], "gpt-5.1-codex-mini")

    def test_failure_returns_none_and_cleans_temp_file(self) -> None:
        seen: dict = {}

        def fake_run(argv, **kwargs):
            argv = list(argv)
            seen["out_path"] = argv[argv.index("-o") + 1]
            return _FakeProc(returncode=1)

        self.assertIsNone(self._run_with_fake(fake_run))
        self.assertFalse(os.path.exists(seen["out_path"]))


class _StaticGenerator(titlegen.TitleGenerator):
    """测试用生成器:固定返回构造时给定的文本。"""

    id = "static"
    executable = "true"

    def __init__(self, text: str | None):
        self._text = text

    def generate(self, prompt: str, timeout: int) -> str | None:
        self.last_prompt = prompt
        return self._text


def _session(sid: str) -> dict:
    return {
        "source": "claude",
        "id": sid,
        "short_id": sid[:12],
        "size_bytes": 1024,
        "size_kb": 1.0,
        "fallback_title": "修复登录报错",
        "first_user_msg": "帮我修复登录报错",
        "last_user_msg": "继续",
        "last_agent_msg": "已修复",
    }


class GenerateTitlesBatchTests(unittest.TestCase):
    def test_parses_fenced_json_from_generator(self) -> None:
        generator = _StaticGenerator('```json\n{"claude:s1": "修复登录报错"}\n```')
        result = titles.generate_titles_batch([_session("s1")], generator)
        self.assertEqual(result, {"claude:s1": "修复登录报错"})
        self.assertTrue(generator.last_prompt.startswith(titles.PROMPT_MARKER))

    def test_none_generator_returns_empty(self) -> None:
        self.assertEqual(titles.generate_titles_batch([_session("s1")], None), {})

    def test_generator_failure_returns_empty(self) -> None:
        self.assertEqual(titles.generate_titles_batch([_session("s1")], _StaticGenerator(None)), {})


class RefreshTitlesGeneratorTests(unittest.TestCase):
    def test_uses_injected_generator_and_writes_cache(self) -> None:
        cache: dict = {}
        generator = _StaticGenerator('{"claude:s1": "修复登录报错"}')
        with mock.patch.object(titles, "save_cache"):
            result = titles.refresh_titles([_session("s1")], cache, generator=generator)
        self.assertEqual(result, {"claude:s1": "修复登录报错"})
        self.assertEqual(cache["claude:s1"]["title"], "修复登录报错")

    def test_returns_empty_when_no_generator_resolvable(self) -> None:
        with mock.patch.object(titlegen, "resolve_generator", return_value=None):
            self.assertEqual(titles.refresh_titles([_session("s1")], {}), {})


class SaveCacheAtomicWriteTests(unittest.TestCase):
    """save_cache 原子写：后台生成进程逐批写、TUI 轮询读同一文件，不能被撕裂读。"""

    def setUp(self) -> None:
        self._tmpdir = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._patch_dir = mock.patch.object(titles, "CACHE_DIR", self._tmpdir.name)
        self._patch_file = mock.patch.object(
            titles, "CACHE_FILE", os.path.join(self._tmpdir.name, "titles.json")
        )
        self._patch_dir.start()
        self._patch_file.start()
        self.addCleanup(self._patch_dir.stop)
        self.addCleanup(self._patch_file.stop)

    def test_round_trip_and_no_leftover_tmp_file(self) -> None:
        titles.save_cache({"claude:s1": {"fp": "v3:100", "title": "修复登录报错"}})

        self.assertEqual(
            titles.load_cache(), {"claude:s1": {"fp": "v3:100", "title": "修复登录报错"}}
        )
        leftovers = [
            name for name in os.listdir(self._tmpdir.name) if name != "titles.json"
        ]
        self.assertEqual(leftovers, [])

    def test_readers_never_see_partial_content_across_repeated_writes(self) -> None:
        # os.replace 是同文件系统内的原子操作：反复覆写后，读到的内容必须
        # 始终是某一次完整写入的结果，不能是半截 JSON（load_cache 解析失败
        # 会静默退回 {}，这里断言每次都非空且可解析）。
        for i in range(20):
            titles.save_cache({f"claude:s{i}": {"fp": f"v3:{i}", "title": f"标题{i}"}})
            cache = titles.load_cache()
            self.assertEqual(len(cache), 1)


if __name__ == "__main__":
    unittest.main()
