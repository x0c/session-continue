#!/usr/bin/env bash
# 把本仓库以 editable 方式装到「pickup 命令实际用的解释器」上。
# 解决：改了 cli/src，敲 pickup 却仍跑 pipx/site-packages 旧副本。
#
# 用法（在任意目录）：
#   bash /path/to/pickup/cli/scripts/dev-install.sh
# 或：
#   cd cli && bash scripts/dev-install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f "$ROOT/pyproject.toml" || ! -f "$ROOT/src/pickup/__init__.py" ]]; then
  echo "错误：未找到 pickup 源码树（需要 pyproject.toml 与 src/pickup/）: $ROOT" >&2
  exit 1
fi

entry_python() {
  local bin shebang
  bin="$(command -v pickup 2>/dev/null || true)"
  if [[ -n "$bin" && -f "$bin" ]]; then
    shebang="$(sed -n '1s/^#!//p' "$bin" 2>/dev/null || true)"
    if [[ -n "$shebang" && -x "$shebang" ]]; then
      printf '%s\n' "$shebang"
      return 0
    fi
  fi
  return 1
}

install_editable() {
  local py="$1"
  echo "→ $py -m pip install --force-reinstall --no-deps -e $ROOT"
  "$py" -m pip install --force-reinstall --no-deps -e "$ROOT"
}

PY=""
if PY="$(entry_python)"; then
  echo "检测到 pickup 入口解释器：$PY"
  install_editable "$PY"
elif command -v pipx >/dev/null 2>&1; then
  echo "未找到可用的 pickup shebang，改用 pipx install -e …"
  pipx install -e "$ROOT" --force
  PY="$(entry_python || true)"
else
  echo "未找到 pickup / pipx，改用 python3 --user editable 安装"
  PY="$(command -v python3)"
  install_editable "$PY"
fi

echo ""
echo "校验加载路径："
CHECK_PY="${PY:-$(command -v python3)}"
if command -v pickup >/dev/null 2>&1; then
  ENTRY_PY="$(entry_python || true)"
  if [[ -n "${ENTRY_PY:-}" ]]; then
    CHECK_PY="$ENTRY_PY"
  fi
fi
"$CHECK_PY" -c "
import pickup, os, sys
path = os.path.abspath(pickup.__file__)
print('  version:', pickup.__version__)
print('  file:   ', path)
print('  python: ', sys.executable)
ok = os.path.samefile(os.path.dirname(path), r'$ROOT/src/pickup') or path.startswith(r'$ROOT/src/pickup' + os.sep)
print('  editable指向本仓库:', '是' if ok else '否（请检查上方 pip 输出）')
raise SystemExit(0 if ok else 1)
"

echo ""
echo "完成。请重启已打开的 pickup TUI（旧进程不会热加载）。"
echo "日常开发：改 src/ 后直接再跑 pickup 即可，无需反复 force-reinstall。"
