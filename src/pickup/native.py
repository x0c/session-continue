"""可选原生加速层；不可用或被禁用时自动回退纯 Python。"""

from __future__ import annotations

import json
import os

_disabled = os.environ.get("PICKUP_NATIVE", "1").strip().lower() in {"0", "false", "no", "off"}
try:
    if _disabled:
        raise ImportError("已通过 PICKUP_NATIVE 禁用")
    from pickup import _native as _extension
except ImportError:
    _extension = None


def available() -> bool:
    return _extension is not None


def json_loads(data):
    if _extension is None:
        return json.loads(data)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _extension.loads(data)


def parse_ansi_rows(text: str, width: int, height: int):
    if _extension is None:
        return None
    return _extension.parse_ansi_rows(text, width, height)
