#!/usr/bin/env bash
# 一键安装 pickup（不使用 Homebrew 的场景，例如 Linux 或未装 Homebrew 的 macOS）。
# 用法：curl -fsSL https://raw.githubusercontent.com/x0c/pickup/main/install.sh | bash
set -euo pipefail

REPO="${PICKUP_REPO:-x0c/pickup}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：未找到 python3，请先安装 Python 3.10 及以上版本" >&2
  exit 1
fi

PYMAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
PYMINOR=$(python3 -c 'import sys; print(sys.version_info[1])')
if [ "$PYMAJOR" -lt 3 ] || { [ "$PYMAJOR" -eq 3 ] && [ "$PYMINOR" -lt 10 ]; }; then
  echo "错误：需要 Python 3.10 及以上版本，当前是 ${PYMAJOR}.${PYMINOR}" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "错误：未找到 curl，无法查询最新版本号" >&2
  exit 1
fi

# tmux 是硬依赖（会话托管、内嵌面板、断线保活全部建立在 tmux 之上），装之前先拦住
if ! command -v tmux >/dev/null 2>&1; then
  echo "错误：pickup 需要 tmux 才能运行，请先安装" >&2
  echo "  macOS:          brew install tmux" >&2
  echo "  Debian/Ubuntu:  sudo apt install tmux" >&2
  echo "  Fedora:         sudo dnf install tmux" >&2
  exit 1
fi

VERSION="${PICKUP_VERSION:-}"
if [ -z "$VERSION" ]; then
  VERSION=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')
fi

echo "正在安装 pickup ${VERSION} ..."
VERSION_NUMBER="${VERSION#v}"
MACHINE=$(uname -m)
case "$MACHINE" in
  x86_64|amd64) ARCH="x86_64" ;;
  arm64|aarch64) ARCH="aarch64" ;;
  *) echo "错误：暂不支持处理器架构 ${MACHINE}" >&2; exit 1 ;;
esac

SYSTEM=$(uname -s)
case "$SYSTEM" in
  Darwin)
    WHEEL_PATTERN="^pickup-${VERSION_NUMBER}-cp310-abi3-macosx_.*_universal2\\.whl$"
    ;;
  Linux)
    if ldd --version 2>&1 | grep -qi musl; then
      PLATFORM="musllinux_1_2"
    else
      PLATFORM="manylinux_2_17"
    fi
    if [ "$PLATFORM" = "manylinux_2_17" ]; then
      # auditwheel 同时附加新旧兼容标签，例如
      # manylinux_2_17_x86_64.manylinux2014_x86_64。
      WHEEL_PATTERN="^pickup-${VERSION_NUMBER}-cp310-abi3-${PLATFORM}_${ARCH}(\\.manylinux2014_${ARCH})?\\.whl$"
    else
      WHEEL_PATTERN="^pickup-${VERSION_NUMBER}-cp310-abi3-${PLATFORM}_${ARCH}\\.whl$"
    fi
    ;;
  *)
    echo "错误：pickup 当前只支持 macOS 与 Linux" >&2
    exit 1
    ;;
esac

WHEEL_URL="${PICKUP_WHEEL_URL:-}"
if [ -z "$WHEEL_URL" ]; then
  WHEEL_URL=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/tags/${VERSION}" \
    | WHEEL_PATTERN="$WHEEL_PATTERN" python3 -c '
import json, os, re, sys
pattern = re.compile(os.environ["WHEEL_PATTERN"])
assets = json.load(sys.stdin).get("assets", [])
print(next((item["browser_download_url"] for item in assets
            if pattern.match(item.get("name", ""))), ""))
')
fi

if [ -n "$WHEEL_URL" ]; then
  python3 -m pip install --user --upgrade "$WHEEL_URL"
else
  echo "未找到匹配的预编译包，正在从源码构建（需要本机 Rust 工具链）..."
  python3 -m pip install --user --upgrade \
    "https://github.com/${REPO}/archive/refs/tags/${VERSION}.tar.gz"
fi

SCRIPTS_DIR="$(python3 -m site --user-base)/bin"
case ":${PATH}:" in
  *":${SCRIPTS_DIR}:"*)
    echo "安装完成，运行 pickup 开始使用。"
    ;;
  *)
    echo ""
    echo "安装完成，但 ${SCRIPTS_DIR} 不在 PATH 中。"
    echo "请将以下内容加入你的 shell 配置文件（如 ~/.bashrc 或 ~/.zshrc）后重新打开终端："
    echo "  export PATH=\"${SCRIPTS_DIR}:\$PATH\""
    ;;
esac
