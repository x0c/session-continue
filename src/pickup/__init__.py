"""pickup：终端会话接力工具。

包顶层保持历史兼容导出，但全部按需加载，避免 ``pickup --version`` 和机器命令
为未使用的 Textual、网络、tmux 与扫描模块支付导入成本。
"""

from __future__ import annotations

import importlib
import sys as sys

__version__ = "0.24.1"

_MODULE_EXPORTS = {"embed", "keepalive", "titles", "updater", "split_layout", "observe", "theme"}
_STANDARD_MODULE_EXPORTS = {"shutil"}
_STANDARD_SYMBOL_EXPORTS = {"datetime": ("datetime", "datetime")}
_SYMBOL_EXPORTS = {
    "_DirectLaunch": ("pickup.cli", "_DirectLaunch"),
    "_dispatch_direct_launch": ("pickup.cli", "_dispatch_direct_launch"),
    "_launch": ("pickup.cli", "_launch"),
    "_require_tmux": ("pickup.cli", "_require_tmux"),
    "_restart_process": ("pickup.cli", "_restart_process"),
    "_spawn_title_daemon": ("pickup.cli", "_spawn_title_daemon"),
    "main": ("pickup.bootstrap", "main"),
    "SPINNER_FRAMES": ("pickup.display", "SPINNER_FRAMES"),
    "UNKNOWN_PROJECT_LABEL": ("pickup.display", "UNKNOWN_PROJECT_LABEL"),
    "_disambiguate_labels": ("pickup.display", "_disambiguate_labels"),
    "_filter_sessions": ("pickup.display", "_filter_sessions"),
    "_filter_sessions_by_query": ("pickup.display", "_filter_sessions_by_query"),
    "_fit_cell": ("pickup.display", "_fit_cell"),
    "_fit_cell_right": ("pickup.display", "_fit_cell_right"),
    "_format_relative_time": ("pickup.display", "_format_relative_time"),
    "_fuzzy_match": ("pickup.display", "_fuzzy_match"),
    "_normalize_cwd": ("pickup.display", "_normalize_cwd"),
    "_preview_lines": ("pickup.display", "_preview_lines"),
    "_project_groups": ("pickup.display", "_project_groups"),
    "_text_width": ("pickup.display", "_text_width"),
    "_wrap_preview_text": ("pickup.display", "_wrap_preview_text"),
    "ConversationMessage": ("pickup.models", "ConversationMessage"),
    "LaunchPlan": ("pickup.models", "LaunchPlan"),
    "LaunchRequest": ("pickup.models", "LaunchRequest"),
    "NewSessionRequest": ("pickup.models", "NewSessionRequest"),
    "format_message_time": ("pickup.models", "format_message_time"),
    "session_key": ("pickup.models", "session_key"),
    "_log_embed_error": ("pickup.observe", "log_embed_error"),
    "LaunchError": ("pickup.runtime", "LaunchError"),
    "RuntimeRegistry": ("pickup.runtime", "RuntimeRegistry"),
    "default_registry": ("pickup.runtime", "default_registry"),
    "execute_launch": ("pickup.runtime", "execute_launch"),
    "usable_cwd": ("pickup.runtime", "usable_cwd"),
    "SessionStore": ("pickup.store", "SessionStore"),
    "_new_session_cwd": ("pickup.store", "_new_session_cwd"),
    "RUNTIME_LABEL_STYLES": ("pickup.theme", "RUNTIME_LABEL_STYLES"),
    "_background_channels": ("pickup.theme", "_background_channels"),
    "_background_is_light": ("pickup.theme", "_background_is_light"),
    "_background_rgb": ("pickup.theme", "_background_rgb"),
    "_probe_osc_colours": ("pickup.theme", "_probe_osc_colours"),
    "runtime_label_style": ("pickup.theme", "runtime_label_style"),
}


def __getattr__(name: str):
    if name in _STANDARD_SYMBOL_EXPORTS:
        module_name, symbol_name = _STANDARD_SYMBOL_EXPORTS[name]
        value = getattr(importlib.import_module(module_name), symbol_name)
    elif name in _STANDARD_MODULE_EXPORTS:
        value = importlib.import_module(name)
    elif name in _MODULE_EXPORTS:
        value = importlib.import_module(f"pickup.{name}")
    else:
        target = _SYMBOL_EXPORTS.get(name)
        if target is None:
            raise AttributeError(f"module 'pickup' has no attribute {name!r}")
        value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(
        set(globals()) | _MODULE_EXPORTS | _STANDARD_MODULE_EXPORTS
        | set(_STANDARD_SYMBOL_EXPORTS) | set(_SYMBOL_EXPORTS)
    )
