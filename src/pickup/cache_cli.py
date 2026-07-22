"""独立缓存维护命令；不污染 Agent 只读查询接口。"""

from __future__ import annotations

import argparse
import json
import sys

from pickup.cache import get_cache


def _envelope(data=None, error=None, *, dry_run: bool = False) -> dict:
    return {
        "ok": error is None,
        "data": data if error is None else None,
        "error": error,
        "meta": {"version": 1, "dry_run": dry_run},
    }


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, json_requested: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.json_requested = json_requested

    def error(self, message: str) -> None:
        if self.json_requested:
            print(json.dumps(_envelope(error={
                "code": "usage_error",
                "message": f"参数错误：{message}",
                "hint": "运行 pickup cache --help 查看用法",
                "next_commands": ["pickup cache --help"],
            }), ensure_ascii=False))
            raise SystemExit(2)
        super().error(message)


def main(argv: list[str]) -> int:
    parser = _Parser(
        prog="pickup cache",
        description="查看或清理 pickup 本地派生缓存。",
        json_requested="--json" in argv,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser("status", help="查看缓存状态")
    status_parser.add_argument("--json", action="store_true", dest="json_mode")
    clear_parser = sub.add_parser("clear", help="清理可安全重建的缓存")
    clear_parser.add_argument("--dry-run", action="store_true")
    clear_parser.add_argument("--json", action="store_true", dest="json_mode")
    try:
        args = parser.parse_args(argv)
        if args.command == "status":
            data = get_cache().status()
        else:
            data = get_cache().clear(dry_run=args.dry_run)
    except SystemExit as exc:
        return int(exc.code)
    except Exception as exc:  # noqa: BLE001：维护命令必须给稳定错误契约
        if "--json" in argv:
            print(json.dumps(_envelope(error={"code": "cache_error", "message": f"缓存操作失败：{exc}"}), ensure_ascii=False))
        else:
            print(f"缓存操作失败：{exc}", file=sys.stderr)
        return 1

    if args.json_mode:
        print(json.dumps(_envelope(data, dry_run=bool(getattr(args, "dry_run", False))), ensure_ascii=False))
    elif args.command == "status":
        state = "已启用" if data["enabled"] else "已禁用"
        print(f"缓存：{state}")
        print(f"位置：{data['path']}")
        print(f"占用：{data['size_bytes']} / {data['max_bytes']} 字节")
        print(f"会话元数据：{data['session_count']}；对话正文：{data['conversation_count']}")
    else:
        labels = {"cleared": "缓存已清理", "unchanged": "缓存原本为空", "would_clear": "将清理缓存"}
        print(labels[data["status"]])
    return 0
