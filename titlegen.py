#!/usr/bin/env python3
"""标题生成器抽象:把「用哪个 CLI 无头生成标题」与标题业务逻辑解耦。

标题生成是独立服务,不属于任何运行时适配器(见 AGENTS.md 架构约束),
本模块只依赖标准库,不 import runtime/。titles.py 负责批量 prompt 构建、
结果解析和缓存;本模块的每个生成器只负责一次无头 CLI 调用并交回原始文本。

选择策略:
- 环境变量 SC_TITLE_GENERATOR(claude/codex)显式指定;
- 未指定或指定的不可用时,按注册顺序取第一个本机已安装的;
- 环境变量 SC_TITLE_MODEL 覆盖生成器默认模型。
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


_GENERATORS: tuple[TitleGenerator, ...] = (ClaudeTitleGenerator(), CodexTitleGenerator())


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
