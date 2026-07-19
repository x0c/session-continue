"""界面导航状态：当前项目筛选与默认运行时，跨 MainScreen/模态弹窗共享。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NavState:
    source: str
    project_key: str | None = None
    pinned_key: str | None = None  # 固定监看的会话键；None = 右栏跟随左栏选择
