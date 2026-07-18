# 标题生成器抽象化改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「用哪个 CLI 生成会话标题」从 `titles.py` 里硬编码的 `claude -p --model haiku` 抽象成可插拔的标题生成器,并接入 Codex(`codex exec`)和 OpenCode(`opencode run`)两个新实现,让没装 Claude Code 的用户也能生成标题。

**Architecture:** 新增独立模块 `titlegen.py`,定义 `TitleGenerator` 抽象基类和三个实现(Claude/Codex/OpenCode)。`titles.py` 保留全部业务逻辑(批量 prompt 构建、JSON 解析、缓存、拆批),只把「执行一次无头 CLI 调用拿回原始文本」委托给生成器。选择策略:环境变量 `SC_TITLE_GENERATOR` 显式指定,未指定或指定的不可用时按 claude → codex → opencode 顺序取第一个本机已安装的。标题生成依旧是独立服务,**不进 `runtime/` 适配器**(AGENTS.md 架构约束:标题生成不属于任何运行时适配器)。

**Tech Stack:** Python 3.10+ 标准库(subprocess/shutil/tempfile/abc),unittest + mock,无第三方依赖。

## 全局约束(每个任务都隐含遵守)

- 首屏(进程启动到 TUI 首次渲染)延迟必须 ≤1s;本改造只动后台生成路径,不得在 `sc.py` 首屏路径新增任何子进程调用或重 import。
- 代码注释、日志、错误信息一律中文。
- `titles.PROMPT_MARKER`(`"你将看到一批编程助手会话的摘录"`)必须保持为批量 prompt 的第一行前缀,三个运行时的扫描器都靠它过滤自产噪音会话。
- 标题生成器不进 `runtime/`;`titlegen.py` 不得 import `runtime` 包,`runtime/` 也不得 import `titlegen`。
- 每个任务完成后必跑:
  ```bash
  cd <仓库根>/cli
  python3 -m py_compile sc.py scan_claude.py scan_codex.py scan_opencode.py titles.py titlegen.py models.py agent_api.py keepalive.py runtime/*.py test_*.py
  python3 -m unittest -v
  ```
  预期:0 错误、全部测试 PASS。
- 每个任务一个独立 commit,不 amend。
- 下文相对路径均以 CLI 仓库根为基准。

## 背景:现状事实(实现前无需再考古)

- `titles.py:240` `generate_titles_batch(sessions, model="haiku", timeout=90)` 是全项目唯一的 LLM 调用点:`subprocess.run(["claude", "-p", "--model", model], input=prompt, ...)`,stdout 剥 ```json 围栏后 `json.loads` 成 `{id: 标题}`。
- `titles.py:282` `refresh_titles(sessions, cache, model="haiku")` 按 `_BATCH_SIZE=5` 拆批串行调用上者,每批成功后立即 `save_cache`。
- 调用方只有两处,都不传 model:`sc.py:1410`(`_run_title_daemon`,即 `sc --generate-titles` 后台进程)和 `titles.py` 的 `__main__` 自测块。
- 自产噪音过滤:后台生成用 `claude -p` 会在 `~/.claude/projects/` 留下新会话,`scan_claude.py:484,495` 用 `titles.PROMPT_MARKER` 前缀过滤;`scan_codex.py` 和 `scan_opencode.py` 目前**没有**这个过滤(它们从未被生成器污染过)。两个扫描器都已 `import titles`。
- 本机验证过的无头 CLI 能力(实现时如版本不同需重新 `--help` 核对):
  - `codex exec`:`-m <model>`、`--ephemeral`(不落盘会话文件)、`--skip-git-repo-check`、`-s read-only`、`--color never`、`-o <file>`(最终答复写入文件,stdout 是事件日志不可直接用)、prompt 用 `-` 从 stdin 读。
  - `opencode run`:message 作 positional 参数、`-m provider/model`。**没有** ephemeral 类开关,会真实落一条会话进 opencode.db。
- 缓存键是 `models.session_key()` = `"{source}:{id}"`(如 `claude:xxx`),与生成器无关;换生成器不应导致已有标题失效,缓存结构不动。
- `pyproject.toml:44` 的 `py-modules` 缺 `scan_opencode`(**已存在的打包缺陷**,安装产物里没有该文件),本次顺手修复。

---

### Task 1: `titlegen.py` 抽象层 + Claude 实现 + 选择逻辑,`titles.py` 接线

**Files:**
- Create: `titlegen.py`
- Create: `test_titlegen.py`
- Modify: `titles.py:240-308`(`generate_titles_batch` / `refresh_titles` 签名与实现)、`titles.py:13`(删除不再使用的 `import subprocess`)
- Modify: `test_session_scanning.py:557-585`(两个 `refresh_titles` 测试注入 generator)
- Modify: `pyproject.toml:44`(`py-modules` 增加 `titlegen` 和缺失的 `scan_opencode`)
- Modify: `sc.py:1387-1391`(`_run_title_daemon` docstring 里「重复消耗 Claude 额度」改为「重复消耗模型额度」)

**Interfaces:**
- Consumes: 无(首任务)。
- Produces(后续任务与调用方依赖的契约):
  - `titlegen.TitleGenerator`:抽象基类,类属性 `id: str`、`executable: str`、`default_model: str | None`;方法 `is_available() -> bool`、`generate(prompt: str, timeout: int) -> str | None`(返回模型原始文本,失败返回 None)。
  - `titlegen.resolve_generator() -> TitleGenerator | None`。
  - `titlegen.ENV_GENERATOR = "SC_TITLE_GENERATOR"`、`titlegen.ENV_MODEL = "SC_TITLE_MODEL"`。
  - `titles.generate_titles_batch(sessions, generator, timeout=90)`(generator 为 None 时返回 `{}`)。
  - `titles.refresh_titles(sessions, cache, generator=None)`(None 时内部 `resolve_generator()`,解析不到直接返回 `{}`)。

- [ ] **Step 1: 写失败测试**

创建 `test_titlegen.py`:

```python
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
        with mock.patch.object(titlegen.shutil, "which", side_effect=_which({"claude", "codex", "opencode"})), \
                mock.patch.dict(os.environ, {**_NO_ENV, titlegen.ENV_GENERATOR: "claude"}):
            generator = titlegen.resolve_generator()
        self.assertIsNotNone(generator)
        self.assertEqual(generator.id, "claude")

    def test_falls_back_to_availability_order_when_env_target_missing(self) -> None:
        # 指定的生成器本机没装时,退回可用性顺序,标题照常生成
        with mock.patch.object(titlegen.shutil, "which", side_effect=_which({"claude"})), \
                mock.patch.dict(os.environ, {**_NO_ENV, titlegen.ENV_GENERATOR: "nonexistent"}):
            generator = titlegen.resolve_generator()
        self.assertEqual(generator.id, "claude")

    def test_defaults_to_claude_first(self) -> None:
        with mock.patch.object(titlegen.shutil, "which", side_effect=_which({"claude", "codex", "opencode"})), \
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m unittest test_titlegen -v
```

预期:`ModuleNotFoundError: No module named 'titlegen'`。

- [ ] **Step 3: 实现 `titlegen.py`**

```python
#!/usr/bin/env python3
"""标题生成器抽象:把「用哪个 CLI 无头生成标题」与标题业务逻辑解耦。

标题生成是独立服务,不属于任何运行时适配器(见 AGENTS.md 架构约束),
本模块只依赖标准库,不 import runtime/。titles.py 负责批量 prompt 构建、
结果解析和缓存;本模块的每个生成器只负责一次无头 CLI 调用并交回原始文本。

选择策略:
- 环境变量 SC_TITLE_GENERATOR(claude/codex/opencode)显式指定;
- 未指定或指定的不可用时,按注册顺序取第一个本机已安装的;
- 环境变量 SC_TITLE_MODEL 覆盖生成器默认模型(opencode 需 provider/model 格式)。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod

ENV_GENERATOR = "SC_TITLE_GENERATOR"
ENV_MODEL = "SC_TITLE_MODEL"


def _run(argv: list[str], input_text: str | None, timeout: int) -> str | None:
    """执行一次 CLI 调用并返回 stdout;非零退出、超时或无法启动一律返回 None。"""
    try:
        proc = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


class TitleGenerator(ABC):
    """每个标题生成后端需要实现的最小能力。"""

    id: str
    executable: str
    default_model: str | None = None

    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def _model(self) -> str | None:
        return os.environ.get(ENV_MODEL, "").strip() or self.default_model

    @abstractmethod
    def generate(self, prompt: str, timeout: int) -> str | None:
        """无头调用一次 CLI,返回模型原始文本输出;失败返回 None。"""


class ClaudeTitleGenerator(TitleGenerator):
    id = "claude"
    executable = "claude"
    default_model = "haiku"  # 标题生成用最便宜的模型即可

    def generate(self, prompt: str, timeout: int) -> str | None:
        return _run(["claude", "-p", "--model", self._model()], prompt, timeout)


_GENERATORS: tuple[TitleGenerator, ...] = (ClaudeTitleGenerator(),)


def resolve_generator() -> TitleGenerator | None:
    """按环境变量与本机可用性选定生成器;一个都不可用时返回 None。"""
    configured = os.environ.get(ENV_GENERATOR, "").strip().lower()
    for generator in _GENERATORS:
        if generator.id == configured and generator.is_available():
            return generator
    for generator in _GENERATORS:
        if generator.is_available():
            return generator
    return None
```

- [ ] **Step 4: 接线 `titles.py`**

删除 `titles.py:13` 的 `import subprocess`,在 import 区加 `import titlegen`。把 `generate_titles_batch`(原 240-276 行)替换为:

```python
def generate_titles_batch(sessions: list[dict], generator: "titlegen.TitleGenerator | None", timeout: int = 90) -> dict[str, str]:
    """通过标题生成器批量生成标题,返回 {id: title}。失败时返回空字典。"""
    if not sessions or generator is None:
        return {}

    text = generator.generate(_build_batch_prompt(sessions), timeout=timeout)
    if not text:
        return {}

    text = text.strip()
    # 模型可能用 ```json 包裹,剥掉代码块标记
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v).strip() for k, v in data.items() if v}
    except json.JSONDecodeError:
        pass
    return {}
```

把 `refresh_titles`(原 282-308 行)签名和开头替换为(循环体内 `generate_titles_batch(chunk, model=model)` 改为 `generate_titles_batch(chunk, generator)`,其余不动):

```python
def refresh_titles(sessions: list[dict], cache: dict, generator: "titlegen.TitleGenerator | None" = None) -> dict[str, str]:
    """对一批待生成的会话批量生成标题,写回缓存,返回 {会话键: title} 增量。

    内部按 _BATCH_SIZE 拆批串行,避免单次调用超时。
    generator 为 None 时按环境自动选择;本机没有任何可用 CLI 时返回空增量。
    """
    if not sessions:
        return {}
    if generator is None:
        generator = titlegen.resolve_generator()
    if generator is None:
        return {}
```

- [ ] **Step 5: 更新既有测试与杂项**

`test_session_scanning.py:557-585` 的 `test_refresh_titles_does_not_retry_each_session_when_batch_fails` 和 `test_refresh_titles_saves_cache_per_batch` 里的 `titles.refresh_titles(sessions, {})` 调用改为 `titles.refresh_titles(sessions, {}, generator=mock.Mock())`(它们 mock 掉了 `generate_titles_batch`,注入哨兵对象即可绕开对本机 CLI 可用性的依赖,CI 上没装 claude 也能过)。

`pyproject.toml:44` 改为:

```toml
py-modules = ["models", "sc", "agent_api", "scan_claude", "scan_codex", "scan_opencode", "titles", "titlegen", "keepalive"]
```

`sc.py:1391` docstring 中「重复消耗 Claude 额度」改为「重复消耗模型额度」。

`titles.py` 底部 `__main__` 自测块里 `refresh_titles(pending, cache)` 保持原样(新签名兼容)。

- [ ] **Step 6: 跑测试确认通过**

```bash
python3 -m py_compile sc.py scan_claude.py scan_codex.py scan_opencode.py titles.py titlegen.py models.py agent_api.py keepalive.py runtime/*.py test_*.py
python3 -m unittest -v
```

预期:全部 PASS(含新增 test_titlegen)。

- [ ] **Step 7: Commit**

```bash
git add titlegen.py test_titlegen.py titles.py test_session_scanning.py pyproject.toml sc.py
git commit -m "refactor: 抽象标题生成器,claude 实现与业务逻辑解耦

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Codex 标题生成器 + Codex 扫描侧噪音防御

**Files:**
- Modify: `titlegen.py`(新增 `CodexTitleGenerator`,注册进 `_GENERATORS`)
- Modify: `scan_codex.py:352-355` 附近(`scan_sessions` 过滤链)
- Test: `test_titlegen.py`、`test_session_scanning.py`(CodexScanTests)

**Interfaces:**
- Consumes: Task 1 的 `TitleGenerator` 基类、`_run()`、`_GENERATORS`。
- Produces: `titlegen.CodexTitleGenerator`,`id="codex"`,`executable="codex"`,`default_model=None`(不带 `-m`,用用户 `~/.codex/config.toml` 里的默认模型;`SC_TITLE_MODEL` 可覆盖)。

**设计要点:** `codex exec` 的 stdout 混着事件日志,最终答复必须用 `-o <临时文件>` 取;`--ephemeral` 让本次调用不落盘会话文件,从源头避免自产噪音污染 `~/.codex/sessions/` 扫描;`-s read-only` 沙箱 + `--skip-git-repo-check` 保证在任意 cwd 下无副作用可跑。扫描侧仍加 PROMPT_MARKER 过滤兜底(旧版 codex 无 `--ephemeral` 时调用会直接失败返回 None、保留临时标题,但用户也可能手动用 codex 跑过类似 prompt)。

- [ ] **Step 1: 实现前先核对本机 codex 版本的旗标仍然存在**

```bash
codex exec --help | grep -E "ephemeral|output-last-message|skip-git-repo-check"
```

预期:三个旗标都在。若缺失,停下来把差异记录进本计划再调整 argv。

- [ ] **Step 2: 写失败测试**

在 `test_titlegen.py` 追加:

```python
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
```

在 `test_session_scanning.py` 的 `CodexScanTests` 里追加(fixture 写法与同类的 `test_scan_sessions_filters_out_subagent_thread_spawns` 保持一致):

```python
    def test_scan_filters_self_generated_title_sessions(self) -> None:
        # 后台标题生成兜底路径若真在 ~/.codex/sessions/ 留下会话
        # (旧版 codex 无 --ephemeral,或用户手动跑过同款 prompt),
        # 必须像 Claude 侧一样按 PROMPT_MARKER 前缀过滤,不进列表。
        old_sessions_dir = scan_codex.SESSIONS_DIR
        old_session_index = scan_codex.SESSION_INDEX
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_codex.SESSIONS_DIR = td
                scan_codex.SESSION_INDEX = os.path.join(td, "session_index.jsonl")
                real_cwd = Path(td) / "real_cwd"
                real_cwd.mkdir()

                specs = [
                    ("019efe42-6d51-7fb3-ad48-112a8eefaa01", "真实的用户问题"),
                    ("019efe42-6d51-7fb3-ad48-112a8eefaa02", f"{titles.PROMPT_MARKER}(JSON 数组…)"),
                ]
                for i, (uuid, first_msg) in enumerate(specs):
                    path = Path(td) / f"rollout-2026-07-16T10-0{i}-00-{uuid}.jsonl"
                    _write_jsonl(
                        path,
                        [
                            {
                                "timestamp": "2026-07-16T02:00:00.000Z",
                                "type": "session_meta",
                                "payload": {"id": uuid, "cwd": str(real_cwd)},
                            },
                            {
                                "timestamp": "2026-07-16T02:00:10.000Z",
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": first_msg},
                            },
                        ],
                    )
                    mtime = 1_800_000_000 + i * 60
                    os.utime(path, (mtime, mtime))

                sessions = scan_codex.scan_sessions(limit=10)
        finally:
            scan_codex.SESSIONS_DIR = old_sessions_dir
            scan_codex.SESSION_INDEX = old_session_index

        self.assertEqual([s["first_user_msg"] for s in sessions], ["真实的用户问题"])
```

- [ ] **Step 3: 跑测试确认失败**

```bash
python3 -m unittest test_titlegen.CodexGeneratorTests test_session_scanning.CodexScanTests.test_scan_filters_self_generated_title_sessions -v
```

预期:`AttributeError: module 'titlegen' has no attribute 'CodexTitleGenerator'`;扫描测试断言失败(噪音会话混进结果)。

- [ ] **Step 4: 实现**

`titlegen.py` 追加(并把 `_GENERATORS` 改为 `(ClaudeTitleGenerator(), CodexTitleGenerator())`):

```python
class CodexTitleGenerator(TitleGenerator):
    id = "codex"
    executable = "codex"
    default_model = None  # 不带 -m,用用户 codex 配置里的默认模型

    def generate(self, prompt: str, timeout: int) -> str | None:
        # stdout 混着事件日志,最终答复用 -o 落到临时文件读取;
        # --ephemeral 不落盘会话文件,避免自产噪音污染 Codex 历史扫描。
        fd, out_path = tempfile.mkstemp(prefix="sc-title-", suffix=".txt")
        os.close(fd)
        try:
            argv = [
                "codex", "exec",
                "--skip-git-repo-check", "--ephemeral",
                "-s", "read-only", "--color", "never",
                "-o", out_path,
            ]
            model = self._model()
            if model:
                argv += ["-m", model]
            argv.append("-")  # prompt 从 stdin 读
            if _run(argv, prompt, timeout) is None:
                return None
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return None
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
```

`scan_codex.py` 的 `scan_sessions` 过滤链(352-353 行的空会话检查之后)追加:

```python
        if info["first_user_msg"].startswith(titles.PROMPT_MARKER):
            continue  # 后台标题生成自产的噪音会话,和 Claude 侧同一套 PROMPT_MARKER 过滤
```

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

```bash
python3 -m unittest -v
```

预期:全部 PASS。

- [ ] **Step 6: 真实冒烟(消耗少量 codex 额度)**

```bash
SC_TITLE_GENERATOR=codex python3 - <<'EOF'
import titlegen
g = titlegen.resolve_generator()
print("选中生成器:", g.id)
out = g.generate('只输出一个 JSON 对象:{"k": "v"},不要输出任何其他文字。', timeout=120)
print("原始输出:", repr(out))
EOF
```

预期:`选中生成器: codex`,输出含 `{"k": "v"}`。随后 `ls -t ~/.codex/sessions/**/*.jsonl | head -3` 确认没有新增本次调用的 rollout 文件(`--ephemeral` 生效)。

- [ ] **Step 7: Commit**

```bash
git add titlegen.py scan_codex.py test_titlegen.py test_session_scanning.py
git commit -m "feat: 标题生成接入 codex exec,扫描侧防御自产噪音会话

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: OpenCode 标题生成器 + OpenCode 扫描侧噪音过滤

**Files:**
- Modify: `titlegen.py`(新增 `OpenCodeTitleGenerator`,注册进 `_GENERATORS`)
- Modify: `scan_opencode.py:170-175` 附近(`_build_session_info`)
- Test: `test_titlegen.py`、`test_session_scanning.py`(OpenCodeScanTests)

**Interfaces:**
- Consumes: Task 1 的 `TitleGenerator`、`_run()`。
- Produces: `titlegen.OpenCodeTitleGenerator`,`id="opencode"`,`executable="opencode"`,`default_model=None`(`SC_TITLE_MODEL` 需用 `provider/model` 格式,如 `anthropic/claude-haiku-4-5`)。

**设计要点:** `opencode run` 没有 ephemeral 开关,每次调用会真实落一条会话进 opencode.db,所以 `scan_opencode.py` 的 PROMPT_MARKER 过滤不是防御而是**必需**。prompt 作 positional 参数传递(批量 prompt ~6KB,远小于 ARG_MAX,且比依赖 stdin 管道语义更确定)。

- [ ] **Step 1: 核对本机 opencode 旗标**

```bash
opencode run --help | grep -E "^\s+-m|--model"
```

预期:存在 `-m, --model`(provider/model 格式)。

- [ ] **Step 2: 写失败测试**

`test_titlegen.py` 追加:

```python
class OpenCodeGeneratorTests(unittest.TestCase):
    def test_prompt_as_positional_arg(self) -> None:
        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["input"] = kwargs.get("input")
            return _FakeProc(stdout='{"opencode:s1": "标题"}')

        with mock.patch.object(titlegen.subprocess, "run", side_effect=fake_run), \
                mock.patch.dict(os.environ, _NO_ENV):
            out = titlegen.OpenCodeTitleGenerator().generate("prompt 内容", timeout=5)

        self.assertEqual(seen["argv"], ["opencode", "run", "prompt 内容"])
        self.assertIsNone(seen["input"])
        self.assertEqual(out, '{"opencode:s1": "标题"}')

    def test_model_env_adds_m_flag(self) -> None:
        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return _FakeProc(stdout="ok")

        with mock.patch.object(titlegen.subprocess, "run", side_effect=fake_run), \
                mock.patch.dict(os.environ, {**_NO_ENV, titlegen.ENV_MODEL: "anthropic/claude-haiku-4-5"}):
            titlegen.OpenCodeTitleGenerator().generate("p", timeout=5)

        self.assertEqual(seen["argv"], ["opencode", "run", "-m", "anthropic/claude-haiku-4-5", "p"])
```

`test_session_scanning.py` 的 `OpenCodeScanTests` 追加(fixture 与同类测试一致,`_text_part` 是类内既有 helper):

```python
    def test_scan_filters_self_generated_title_sessions(self) -> None:
        # opencode run 没有 ephemeral 开关,标题生成每次都会真实落一条会话
        # 进 opencode.db;必须按 PROMPT_MARKER 前缀过滤,不进列表。
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_real", "directory": "/tmp/demo", "title": "真实会话",
                     "time_created": 0, "time_updated": 100_000},
                    {"id": "ses_noise", "directory": "/tmp/demo", "title": "自产噪音",
                     "time_created": 0, "time_updated": 200_000},
                ],
                messages=[
                    {"id": "msg_r1", "session_id": "ses_real", "time_created": 1_000,
                     "data": {"role": "user", "time": {"created": 1_000}}},
                    {"id": "msg_n1", "session_id": "ses_noise", "time_created": 2_000,
                     "data": {"role": "user", "time": {"created": 2_000}}},
                ],
                parts=[
                    self._text_part("p_r1", "msg_r1", "ses_real", "帮我修复登录报错", 1_000),
                    self._text_part("p_n1", "msg_n1", "ses_noise", f"{titles.PROMPT_MARKER}(JSON 数组…)", 2_000),
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "_live_pids_by_cwd", return_value={}):
                sessions = scan_opencode.scan_sessions(limit=10)

        self.assertEqual([s["id"] for s in sessions], ["ses_real"])
```

- [ ] **Step 3: 跑测试确认失败**

```bash
python3 -m unittest test_titlegen.OpenCodeGeneratorTests test_session_scanning.OpenCodeScanTests.test_scan_filters_self_generated_title_sessions -v
```

预期:`AttributeError`(无 OpenCodeTitleGenerator);扫描测试断言失败。

- [ ] **Step 4: 实现**

`titlegen.py` 追加(并把 `_GENERATORS` 改为三元组 `(ClaudeTitleGenerator(), CodexTitleGenerator(), OpenCodeTitleGenerator())`):

```python
class OpenCodeTitleGenerator(TitleGenerator):
    id = "opencode"
    executable = "opencode"
    default_model = None  # 不带 -m,用用户 opencode 配置里的默认模型

    def generate(self, prompt: str, timeout: int) -> str | None:
        argv = ["opencode", "run"]
        model = self._model()
        if model:
            argv += ["-m", model]  # 格式 provider/model
        argv.append(prompt)
        return _run(argv, None, timeout)
```

`scan_opencode.py` 的 `_build_session_info`,在 `first_user = str(row["first_user_text"] or "")`(172 行)之后追加:

```python
    if first_user.startswith(titles.PROMPT_MARKER):
        return None  # 后台标题生成自产的噪音会话(opencode run 无 ephemeral 开关,必然落盘),不进列表
```

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

```bash
python3 -m unittest -v
```

预期:全部 PASS。

- [ ] **Step 6: 真实冒烟(消耗少量 opencode 配置的模型额度)**

```bash
SC_TITLE_GENERATOR=opencode python3 - <<'EOF'
import titlegen
g = titlegen.resolve_generator()
print("选中生成器:", g.id)
out = g.generate('只输出一个 JSON 对象:{"k": "v"},不要输出任何其他文字。', timeout=120)
print("原始输出:", repr(out))
EOF
```

预期:`选中生成器: opencode`,输出含 `{"k": "v"}`。随后跑 `python3 -c "import scan_opencode; print([s['first_user_msg'][:20] for s in scan_opencode.scan_sessions(limit=10)])"` 确认列表里没有以 PROMPT_MARKER 开头的会话(真实数据验证过滤生效)。

- [ ] **Step 7: Commit**

```bash
git add titlegen.py scan_opencode.py test_titlegen.py test_session_scanning.py
git commit -m "feat: 标题生成接入 opencode run,扫描侧过滤自产噪音会话

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 文档同步

**Files:**
- Modify: `README.md:200`
- Modify: `PRIVACY.md`(约 24 行「Optional title generation launches your local `claude` command」段)
- Modify: `docs/MAINTAINER_GUIDE.md`(「标题与排序」「标题生成进程」两节)
- Modify: `AGENTS.md`(「架构约束」标题生成条目)

**Interfaces:** Consumes: Task 1-3 的最终行为。Produces: 无代码接口。

- [ ] **Step 1: README.md**

第 200 行改为(保持英文,README 是唯一英文文档):

```markdown
The TUI first shows a local fallback title so the first screen is immediate. A detached background process then generates better Chinese titles in small batches through whichever agent CLI is available locally — `claude -p --model haiku`, `codex exec`, or `opencode run` (auto-detected in that order; override with `SC_TITLE_GENERATOR=claude|codex|opencode`, and optionally `SC_TITLE_MODEL` to pick the model).
```

- [ ] **Step 2: PRIVACY.md**

「Optional title generation launches your local `claude` command」一句改为:

```markdown
Optional title generation launches one of your locally installed agent CLIs (`claude`, `codex`, or `opencode`; auto-detected, or pinned via `SC_TITLE_GENERATOR`). That command sends short session excerpts to the corresponding model provider under your own account and credentials.
```

- [ ] **Step 3: MAINTAINER_GUIDE.md**

「标题与排序」节追加一条:

```markdown
- 标题生成后端已抽象为 `titlegen.py` 的 `TitleGenerator`(claude/codex/opencode 三个实现)。`titles.py` 只负责批量 prompt、JSON 解析和缓存,不感知具体 CLI;新增生成器只在 `titlegen.py` 加实现并注册进 `_GENERATORS`,禁止在 `titles.py` 里写 `subprocess` 调用。选择顺序:`SC_TITLE_GENERATOR` 环境变量 → 按注册顺序取第一个已安装的;`SC_TITLE_MODEL` 覆盖模型(opencode 需 `provider/model` 格式)。缓存与生成器无关,换生成器不重算已有标题。
- 自产噪音会话的过滤三个扫描器都要有:Claude 侧 `PROMPT_MARKER` 预探过滤(见「扫描性能」);Codex 侧生成用 `codex exec --ephemeral` 不落盘,扫描过滤仅是兜底;OpenCode 侧 `opencode run` 无 ephemeral 开关必然落盘,`_build_session_info` 的 `PROMPT_MARKER` 过滤是必需项,删掉它列表会被标题生成会话刷屏。
```

「标题生成进程」节末尾追加:

```markdown
- 后台进程内的生成器选择发生在 `refresh_titles`(`titlegen.resolve_generator`),本机一个 agent CLI 都没有时静默跳过,列表保持临时兜底标题;不要在 TUI 首屏路径做可用性探测。
```

- [ ] **Step 4: AGENTS.md**

「架构约束」中「标题生成是独立服务,不属于任何运行时适配器」一条扩写为:

```markdown
- 标题生成是独立服务,不属于任何运行时适配器。生成后端统一走 `titlegen.py` 的 `TitleGenerator` 抽象(claude/codex/opencode),`titles.py` 不得直接拼接任何 CLI 命令;`titlegen.py` 与 `runtime/` 互不 import——运行时适配器管「怎么恢复/接力会话」,标题生成器管「怎么无头问一次模型」,两者后端恰好重名但职责不同,不要合并。标题和界面状态使用"运行时 + 会话 ID"作为唯一键,新增运行时不得退回纯会话 ID。新增标题生成后端时,若该 CLI 会把生成调用落盘成会话历史,对应扫描器必须加 `titles.PROMPT_MARKER` 前缀过滤。
```

「验证要求」里的 `py_compile` 命令行同步加上 `scan_opencode.py titlegen.py`(现状漏了 scan_opencode)。

- [ ] **Step 5: 校验与 Commit**

通读四处改动,确认没有残留「标题只能用 claude 生成」的旧表述:

```bash
grep -rn "claude -p" README.md PRIVACY.md docs/MAINTAINER_GUIDE.md AGENTS.md
```

预期:仅剩 MAINTAINER_GUIDE 中作为 claude 实现细节的合理提及。

```bash
git add README.md PRIVACY.md docs/MAINTAINER_GUIDE.md AGENTS.md
git commit -m "docs: 标题生成器抽象与多后端接入的文档同步

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 全量验证 + 真实路径冒烟 + 发布 v0.13.0

**Files:**
- Modify: `pyproject.toml:7`(version)

**Interfaces:** Consumes: Task 1-4 全部产物。

- [ ] **Step 1: 全量回归**

```bash
python3 -m py_compile sc.py scan_claude.py scan_codex.py scan_opencode.py titles.py titlegen.py models.py agent_api.py keepalive.py runtime/*.py test_*.py
python3 -m unittest -v
```

预期:全部 PASS。

- [ ] **Step 2: 首屏延迟实测(硬性红线 ≤1s)**

```bash
python3 -c "
import time
from runtime import default_registry
r = default_registry()
t = time.perf_counter()
r.scan_all(50)
print(f'{(time.perf_counter()-t)*1000:.0f}ms')
"
```

预期:≤1000ms(本改造不动扫描热路径,应与基线持平)。

- [ ] **Step 3: 端到端真实路径验证(消耗少量额度)**

依次用三个生成器跑一次真实后台生成(每次生成前挑 1-2 条无缓存会话即可;若全部有缓存,先在 `~/.cache/session-continue/titles.json` 里临时删掉两条):

```bash
for g in claude codex opencode; do
  echo "=== $g ==="
  SC_TITLE_GENERATOR=$g python3 sc.py --generate-titles --limit 10
  python3 - <<'EOF'
import json, titles
cache = titles.load_cache()
print("缓存条数:", len(cache))
print("最新样例:", json.dumps(dict(list(cache.items())[-2:]), ensure_ascii=False))
EOF
done
```

预期:三轮之后缓存里新标题为可读中文短句,无 raw slug、无省略号、无 PROMPT_MARKER 片段。

再验证扫描无污染(真实数据抽查,按 AGENTS.md 要求):

```bash
python3 - <<'EOF'
from runtime import default_registry
import titles
for rid, bucket in default_registry().scan_all(120).items():
    bad = [s for s in bucket if s["first_user_msg"].startswith(titles.PROMPT_MARKER)]
    print(rid, "总数", len(bucket), "噪音", len(bad))
    assert not bad
EOF
```

预期:三个运行时噪音均为 0。最后真实终端启动一次 `python3 sc.py --limit 20`,肉眼确认标题列正常、退出干净。

- [ ] **Step 4: 安装产物验证(打包缺陷修复的回归)**

```bash
pip install --target /tmp/sc-pkg-check . >/dev/null && ls /tmp/sc-pkg-check | grep -E "titlegen|scan_opencode" && rm -rf /tmp/sc-pkg-check
```

预期:`titlegen.py` 和 `scan_opencode.py` 都在安装产物里。

- [ ] **Step 5: 发版**

`pyproject.toml` 版本 `0.12.0` → `0.13.0`(新功能,SemVer minor),然后按项目发布流程:

```bash
git add pyproject.toml
git commit -m "release: v0.13.0 标题生成器抽象化,支持 codex/opencode 后端

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git tag v0.13.0
git push && git push --tags
```

推送 tag 后确认 `.github/workflows/release.yml` 触发的 GitHub Release 与 `x0c/homebrew-tap` 配方自动更新成功(Actions 页面绿色、tap 仓库配方版本号为 0.13.0);本机 `install.sh` 渠道不需要改动(装的是仓库当前源码)。最后覆盖安装到本机并核对入口:

```bash
command -v sc && readlink "$(command -v sc)"
sc --help >/dev/null && echo OK
```

---

## 已知取舍与风险

- **配置的生成器不可用时回退而非报错**:后台 daemon 是静默进程,报错无人可见;回退到第一个可用 CLI 保证标题照常出。代价是用户拼错 `SC_TITLE_GENERATOR` 时不易察觉——文档里写明回退语义。
- **codex/opencode 默认不指定模型**:用用户各自 CLI 里配置的默认模型,可能比 haiku 贵。不硬编码第三方模型名(命名变动频繁),用 `SC_TITLE_MODEL` 留出口。
- **旧版 codex 无 `--ephemeral`**:调用直接失败返回 None,列表保留临时兜底标题,行为可接受;扫描侧过滤兜底已加。
- **opencode 生成必然落盘会话**:靠 PROMPT_MARKER 过滤,与 Claude 侧同一套机制;`_is_low_value_title` 也已含 PROMPT_MARKER 前缀检查,双保险。
- **prompt 的 JSON-only 输出约定对 agent 型 CLI 的服从性**:codex/opencode 是完整 agent,可能比 `claude -p` 更啰嗦。解析端已剥代码围栏;若真实冒烟(Task 2/3 Step 6)发现输出带额外说明文字,再在解析里加「提取第一个平衡花括号 JSON 对象」的兜底,不提前实现(YAGNI)。
