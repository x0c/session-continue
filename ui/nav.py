"""界面导航状态：项目搜索查询与默认运行时，跨 MainScreen/模态弹窗共享。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NavState:
    source: str
    # 侧边栏顶部搜索框内容；空字符串 = 不过滤（全部项目/会话）
    project_query: str = ""
    pinned_key: str | None = None  # 固定监看的会话键；None = 右栏跟随左栏选择
