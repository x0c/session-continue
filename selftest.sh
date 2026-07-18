#!/usr/bin/env bash
# pickup 新版统一会话时间线端到端自测：隔离 HOME 与 tmux socket，不碰真实会话。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTER="pickup-timeline-test-$$"
KEEPALIVE="pickup-keepalive"
TMP="$(mktemp -d /tmp/pickup-selftest.XXXXXX)"
PASS=0

ok() { PASS=$((PASS + 1)); echo "PASS  $1"; }
cap() { tmux -L "$OUTER" capture-pane -p -t tui 2>/dev/null; }
sessions() { tmux -L "$KEEPALIVE" list-sessions -F '#{session_name}' 2>/dev/null; }
wait_for() {
  local text="$1" tries="${2:-30}"
  local i
  for ((i = 0; i < tries; i++)); do
    cap | grep -qF -- "$text" && return 0
    sleep 0.25
  done
  echo "未等到：$text" >&2
  cap >&2 || true
  return 1
}
cleanup() {
  tmux -L "$OUTER" kill-server 2>/dev/null || true
  tmux -L "$KEEPALIVE" kill-session -t pickup-claude-aaaa1111 2>/dev/null || true
  tmux -L "$KEEPALIVE" kill-session -t pickup-claude-bbbb2222 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

mkdir -p "$TMP/home/.claude/projects/demo" "$TMP/workA" "$TMP/workB" "$TMP/fakebin" "$TMP/home/.cache/pickup"
cat > "$TMP/home/.cache/pickup/titles.json" <<'EOF'
{"claude:aaaa1111":{"title":"修复切换体验","fp":"seed"},"claude:bbbb2222":{"title":"第二个会话","fp":"seed"}}
EOF
cat > "$TMP/home/.claude/projects/demo/aaaa1111.jsonl" <<EOF
{"type":"user","message":{"content":"修复切换体验"},"timestamp":"2026-07-18T10:01:00.000Z","cwd":"$TMP/workA","sessionId":"aaaa1111"}
{"type":"assistant","message":{"content":[{"type":"text","text":"会话 A 回复"}]},"timestamp":"2026-07-18T10:02:00.000Z","sessionId":"aaaa1111"}
EOF
cat > "$TMP/home/.claude/projects/demo/bbbb2222.jsonl" <<EOF
{"type":"user","message":{"content":"第二个会话"},"timestamp":"2026-07-18T09:01:00.000Z","cwd":"$TMP/workB","sessionId":"bbbb2222"}
{"type":"assistant","message":{"content":[{"type":"text","text":"会话 B 回复"}]},"timestamp":"2026-07-18T09:02:00.000Z","sessionId":"bbbb2222"}
EOF
cat > "$TMP/fakebin/claude" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then echo "fake"; exit 0; fi
sid=""
previous=""
for arg in "$@"; do [[ "$previous" == "--resume" ]] && sid="$arg"; previous="$arg"; done
echo "FAKE-CLAUDE --resume $sid"
while IFS= read -r line; do echo "ECHO: $line"; done
EOF
chmod +x "$TMP/fakebin/claude"

TMUX_DIR="$(dirname "$(command -v tmux)")"
ENVV="HOME=$TMP/home PATH=$TMP/fakebin:$TMUX_DIR:/usr/local/bin:/usr/bin:/bin TERM=xterm-256color PICKUP_TITLE_GENERATOR=none"
tmux -L "$OUTER" new-session -d -s tui -x 180 -y 42
tmux -L "$OUTER" set-option -t tui mouse on
tmux -L "$OUTER" send-keys -t tui "cd $REPO && env $ENVV python3 pickup.py --limit 5" Enter

wait_for "workA: 修复切换体验" 60
wait_for "workB: 第二个会话" 60
wait_for "最近提问" 60
ok "首屏是跨运行时统一时间线，右栏直接显示当前会话摘要"

# 每张卡片有标题、状态和一行留白；第二张卡首行在 y=5（tmux SGR 坐标从 1 开始，因此 y=6）。点击等于 Enter。
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;12;6M')"
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;12;6m')"
wait_for "FAKE-CLAUDE --resume bbbb2222" 60
sessions | grep -qx "pickup-claude-bbbb2222"
ok "鼠标点击第二张会话卡直接恢复该会话（等价 Enter）"

# 双反斜杠从右栏回到列表；单个反斜杠不会作为快捷键处理。
tmux -L "$OUTER" send-keys -t tui -l '\\'
sleep 0.08
tmux -L "$OUTER" send-keys -t tui -l '\\'
wait_for "Enter 恢复/操作" 40
ok "双反斜杠可从会话操作区返回列表"

# 点击左栏空白处只回列表，不额外启动会话。
before="$(sessions | grep -c '^pickup-claude-' || true)"
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;12;30M')"
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;12;30m')"
sleep 0.5
after="$(sessions | grep -c '^pickup-claude-' || true)"
[[ "$before" == "$after" ]]
wait_for "Enter 恢复/操作" 20
ok "点击列表空白处只回到列表，不启动会话"

# 点击第一张卡同样直接进入；同时验证 Enter 的旧路径保留。
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;12;3M')"
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;12;3m')"
wait_for "FAKE-CLAUDE --resume aaaa1111" 60
sessions | grep -qx "pickup-claude-aaaa1111"
ok "点击第一张会话卡也直接恢复对应会话"

tmux -L "$OUTER" send-keys -t tui -l '\\'
sleep 0.08
tmux -L "$OUTER" send-keys -t tui -l '\\'
wait_for "Enter 恢复/操作" 40
tmux -L "$OUTER" send-keys -t tui j Enter
wait_for "FAKE-CLAUDE --resume bbbb2222" 40
ok "键盘 Enter 恢复路径仍可用"

# 回到列表后，Esc 必须退出 pickup；此前 q 才是退出键，且直接绑 Esc 会吞掉鼠标/方向键序列。
tmux -L "$OUTER" send-keys -t tui -l '\\'
sleep 0.08
tmux -L "$OUTER" send-keys -t tui -l '\\'
wait_for "Enter 恢复/操作" 40
tmux -L "$OUTER" send-keys -t tui C-[
for _ in {1..20}; do
  [[ "$(tmux -L "$OUTER" display-message -p -t tui '#{pane_current_command}')" != "python3" ]] && break
  sleep 0.1
done
if [[ "$(tmux -L "$OUTER" display-message -p -t tui '#{pane_current_command}')" == "python3" ]]; then
  echo "Esc 后 pickup 仍在运行" >&2
  cap >&2
  exit 1
fi
ok "列表 Esc 退出，同时不影响前述鼠标点击和 Enter 路径"

echo "==== $PASS passed, 0 failed ===="
