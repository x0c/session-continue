"""Kimi Code CLI 运行时适配器。"""

from __future__ import annotations

import os

from pickup.scan import kimi as scan_kimi
from pickup.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from pickup.runtime.base import BaseRuntime, usable_cwd


class KimiRuntime(BaseRuntime):
    id = "kimi"
    display_name = "Kimi"
    executable = "kimi"
    history_reading_hint = (
        "Kimi Code CLI 的 wire.jsonl（协议事件逐行 JSON）：用户消息是 "
        'type=="context.append_message" 且 message.role=="user"，正文在 message.content 里 '
        'type=="text" 的分片；助手正文是 type=="context.append_loop_event" 且 '
        'event.type=="content.part" 且 event.part.type=="text"（part.type=="think" 是思考过程，'
        "忽略）。开头体量很大的 config.update（系统提示）、llm.tools_snapshot、llm.request、"
        "usage.record 等都是协议噪音，可直接跳过。"
    )
    # `-y/--yolo` 在根命令即被接受（自动放行全部操作），供原生恢复、空白新会话和直启子命令复用。
    auto_approve_args = ("-y",)

    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        return scan_kimi.scan_sessions(limit=limit)

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        return scan_kimi.load_conversation(str(session.get("path") or ""))

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "-S",
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        # Kimi 的 `-p/--prompt` 是非交互「跑一个 prompt 并打印」模式，跑完即退出；根命令
        # 不接受位置参数形式的初始 prompt，交互式 TUI 也没有从命令行预置首条消息的入口
        # （见 MAINTAINER_GUIDE「Kimi 接力目标限制」）。因此接力到 Kimi 只能走非交互模式：
        # 用 --add-dir 把源历史目录纳入工作区，让 Kimi 读取原始历史并把最后一个未完成任务
        # 跑完打印结果，随后用户可用 `kimi -c` 在同一会话上继续交互。
        history_dir = os.path.dirname(handoff.history_path)
        return LaunchPlan(
            argv=(
                self.executable,
                "--add-dir",
                history_dir,
                *self.auto_approve_args,
                "-p",
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
            ),
            cwd=usable_cwd(cwd),
        )
