"""Cursor Agent CLI 运行时适配器。"""

from __future__ import annotations

import os

from pickup.scan import cursor as scan_cursor
from pickup.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from pickup.runtime.base import BaseRuntime, usable_cwd


class CursorRuntime(BaseRuntime):
    id = "cursor"
    display_name = "Cursor"
    executable = "agent"
    history_reading_hint = (
        "Cursor Agent CLI 会话目录（~/.cursor/chats/<workspace>/<chatId>/）："
        "meta.json 是标题与工作目录；prompt_history.json 是用户输入（最新在前）；"
        "完整对话在 store.db 的 blobs 表里（JSON 消息含 role/content，用户正文常在 "
        "<user_query> 标签中）。请只读打开，不要改写原会话。"
    )
    auto_approve_args = ("--force",)

    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        return scan_cursor.scan_sessions(limit=limit)

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        return scan_cursor.load_conversation(str(session.get("path") or ""))

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--resume",
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--resume",
                str(session["id"]),
                "--print",
                instruction,
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        history_path = handoff.history_path
        history_dir = (
            history_path if os.path.isdir(history_path) else os.path.dirname(history_path)
        )
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--add-dir",
                history_dir,
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(self.executable, *self.auto_approve_args),
            cwd=usable_cwd(cwd),
        )
