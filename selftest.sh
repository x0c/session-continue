#!/usr/bin/env bash
# pickup 端到端自测（Textual 界面层）：隔离 HOME 与 tmux socket，不碰真实会话。
#
# 界面层已从 curses 换成 Textual；本脚本随之重写，覆盖：内嵌面板真实托管/
# 接回/关闭、Ctrl+\ 焦点切回列表（Textual 能原生区分 Ctrl+\ 和连续两次按 \，
# 不再需要旧版靠 300ms 时间窗口消歧义的双反斜杠 hack）、键盘输入真实转发进
# 托管会话、Esc 退出、直启子命令（pickup claude ...）托管路径、IME 光标锚定
# 的真实终端坐标验证、划词选中 + Ctrl+C 复制的真实 OSC 52 写入验证。
#
# 会话列表卡片本身的鼠标点击这版暂未覆盖（Textual 的会话列表布局与旧版 curses
# 手绘坐标不同，点击路径本身走 Textual ListView 内置的鼠标处理，非本项目自写
# 代码，风险低于键盘路径；如需要可后续用
# `tmux send-keys -l "$(printf '\033[<0;COL;ROWM')"` 针对新布局重新量出坐标补上）。
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
# 右栏键盘交互入口：列表聚焦时 Tab 沿焦点链进入内嵌面板（搜索→列表→右栏）。
# 回车/直启只挂接画面、不抢焦点——要测键盘转发或 IME 光标，必须先进入右栏。
# 自动化里用 Tab 比注入 SGR 单击更稳（tmux send-keys 的假鼠标偶发不触发 Textual 命中）。
focus_right_pane() {
  local target="${1:-tui}"
  tmux -L "$OUTER" send-keys -t "$target" Tab
  sleep 0.35
}
cleanup() {
  tmux -L "$OUTER" kill-server 2>/dev/null || true
  tmux -L "$KEEPALIVE" kill-session -t pickup-claude-aaaa1111 2>/dev/null || true
  tmux -L "$KEEPALIVE" kill-session -t pickup-claude-bbbb2222 2>/dev/null || true
  # direct_session/cursor_session 是直启子命令生成的随机 uuid 会话名，运行到
  # 对应步骤才会被赋值；trap 在函数体内引用是延迟求值，EXIT 时能拿到当时的值。
  [[ -n "${direct_session:-}" ]] && tmux -L "$KEEPALIVE" kill-session -t "$direct_session" 2>/dev/null || true
  [[ -n "${cursor_session:-}" ]] && tmux -L "$KEEPALIVE" kill-session -t "$cursor_session" 2>/dev/null || true
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
if [[ "$sid" == "cursortest" ]]; then
  # 光标探测专用：不带换行地打印提示符，光标停在提示符末尾（模拟真实 shell/
  # agent 等待输入时的光标位置），用于验证外层终端的真实硬件光标是否精确
  # 落在这里——这是 IME 候选框定位依赖的同一套机制。
  printf 'PROMPT> '
  while true; do sleep 1; done
fi
echo "FAKE-CLAUDE --resume $sid"
while IFS= read -r line; do echo "ECHO: $line"; done
EOF
chmod +x "$TMP/fakebin/claude"

TMUX_DIR="$(dirname "$(command -v tmux)")"
# textual/rich 是 pip --user 装的：HOME 一旦被隔离成 $TMP/home，--user 站点包
# 路径会跟着 HOME 走而失效（真机排查过的坑，不是猜测）。这里把当前解释器实际
# 能看到的 sys.path 原样透传，绕开这个问题；真正 pip install 到系统/venv 的
# 用户不受影响。
PYWORKAROUND_PATH="$(python3 -c 'import sys; print(":".join(p for p in sys.path if p))')"
ENVV="HOME=$TMP/home PYTHONPATH=$PYWORKAROUND_PATH PATH=$TMP/fakebin:$TMUX_DIR:/usr/local/bin:/usr/bin:/bin TERM=xterm-256color PICKUP_TITLE_GENERATOR=none PICKUP_LANG=zh"
tmux -L "$OUTER" new-session -d -s tui -x 180 -y 42
tmux -L "$OUTER" set-option -t tui mouse on
tmux -L "$OUTER" send-keys -t tui "cd $REPO && env $ENVV python3 -m pickup --limit 5" Enter

wait_for "workA: 修复切换体验" 60
wait_for "workB: 第二个会话" 60
ok "首屏是跨运行时统一时间线"

# 下移到第一张会话卡，右栏应展示完整对话预览（选中即预览，不再依赖 Space）。
tmux -L "$OUTER" send-keys -t tui Down
wait_for "● 你" 60
wait_for "修复切换体验" 60
ok "选中未托管会话时右栏展示完整对话预览"

tmux -L "$OUTER" send-keys -t tui Enter
wait_for "FAKE-CLAUDE --resume aaaa1111" 60
sessions | grep -qx "pickup-claude-aaaa1111"
ok "回车把会话托管进后台 tmux 并在右栏展示实时画面"

# 回车只挂右栏画面，焦点仍在侧边栏。先点右栏再打字，验证键盘真实转发进
# 托管会话（同时证明「点右栏才交互」这条焦点边界生效）。
focus_right_pane tui
tmux -L "$OUTER" send-keys -t tui -l "smoke-input"
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "ECHO: smoke-input" 40
sleep 0.5
ok "点右栏后键盘输入真实转发进托管会话"

# Ctrl+\ 回列表：Textual 原生区分 Ctrl+\ 与连续两次按 \，不再需要旧版的双反
# 斜杠时间窗口消歧义。回列表后按 / 聚焦搜索框并输入项目名应能过滤列表（如果焦点
# 还停在 pane 上，这些按键会被当成字面文本发进托管会话，搜索框不会出现独立的
# workA 查询串、workB 卡片也不会消失）。
tmux -L "$OUTER" send-keys -t tui C-\\
sleep 0.8
tmux -L "$OUTER" send-keys -t tui /
sleep 0.3
tmux -L "$OUTER" send-keys -t tui -l "workA"
# 注意：不能 wait_for "workA"——列表里本来就有「workA: …」标题，会立刻假阳性。
# 以 workB 卡片消失为准，证明搜索过滤已生效（也就证明焦点已回到列表）。
filtered=0
for _ in {1..40}; do
  if ! cap | grep -q "workB:"; then
    filtered=1
    break
  fi
  sleep 0.15
done
if [[ "$filtered" != "1" ]]; then
  echo "搜索 workA 后列表仍出现 workB 会话卡，焦点可能没回到列表或过滤未生效" >&2
  cap >&2
  exit 1
fi
ok "Ctrl+\\ 把键盘焦点交回列表（/ 搜索过滤生效证明焦点确实回来了）"
# Esc 清空搜索，恢复全部项目可见；再 Down 把焦点交回列表，避免后续快捷键被搜索框吞掉
tmux -L "$OUTER" send-keys -t tui Escape
sleep 0.4
wait_for "workB:" 20
tmux -L "$OUTER" send-keys -t tui Down
sleep 0.2

# 关闭分栏：托管会话必须在后台 tmux 继续存活，不能被一并杀掉。
tmux -L "$OUTER" send-keys -t tui c
sleep 0.4
sessions | grep -qx "pickup-claude-aaaa1111"
ok "c 关闭分栏后，托管会话仍在后台存活"

# 再次回车接回同一个托管会话，不能新建重复会话。
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "FAKE-CLAUDE --resume aaaa1111" 40
[[ "$(sessions | grep -c '^pickup-claude-aaaa1111$')" == "1" ]]
ok "重新回车接回已托管会话，不产生重复会话"

# Esc 退出：先回列表，再 Esc。
tmux -L "$OUTER" send-keys -t tui C-\\
sleep 0.8
tmux -L "$OUTER" send-keys -t tui Escape
for _ in {1..20}; do
  [[ "$(tmux -L "$OUTER" display-message -p -t tui '#{pane_current_command}')" != "python3" ]] && break
  sleep 0.1
done
if [[ "$(tmux -L "$OUTER" display-message -p -t tui '#{pane_current_command}')" == "python3" ]]; then
  echo "Esc 后 pickup 仍在运行" >&2
  cap >&2
  exit 1
fi
ok "列表 Esc 退出，托管会话继续在后台存活"
sessions | grep -qx "pickup-claude-aaaa1111"
ok "退出 pickup 后，后台托管会话不受影响"

# ---- 直启子命令：pickup claude --resume <id> 直接带进 TUI 侧边栏并托管 ----
# 直启的 ident 是 keepalive.new_session_ident() 生成的随机 uuid 片段，与
# --resume 后面的参数无关，因此新会话名靠"托管前后 diff 出唯一新增项"识别，
# 不能假设成 pickup-claude-<--resume 的参数>。
before_direct="$(sessions | grep '^pickup-claude-' || true)"
tmux -L "$OUTER" new-window -t tui -n direct
tmux -L "$OUTER" send-keys -t direct "cd $REPO && env $ENVV python3 -m pickup claude --resume directcccc" Enter
wait_for_direct() {
  local text="$1" tries="${2:-40}"
  local i
  for ((i = 0; i < tries; i++)); do
    tmux -L "$OUTER" capture-pane -p -t direct 2>/dev/null | grep -qF -- "$text" && return 0
    sleep 0.25
  done
  echo "未等到（direct 窗口）：$text" >&2
  tmux -L "$OUTER" capture-pane -p -t direct >&2 || true
  return 1
}
wait_for_direct "FAKE-CLAUDE --resume directcccc" 60
direct_session="$(comm -13 <(echo "$before_direct" | sort) <(sessions | grep '^pickup-claude-' | sort))"
[[ -n "$direct_session" ]]
ok "直启子命令自动托管新会话并在侧边栏展示"

# 直启会自动聚焦右栏（与侧边栏点选不抢焦点的语义不同）。
tmux -L "$OUTER" send-keys -t direct -l "direct-smoke-input"
tmux -L "$OUTER" send-keys -t direct Enter
wait_for_direct "ECHO: direct-smoke-input" 40
ok "直启场景键盘输入真实转发进托管会话"

# ---- IME 光标锚定：真实终端硬件光标必须精确落在托管 pane 内的真实光标位置 ----
# （这是 IME 候选框/emoji 弹出框定位依赖的同一套机制，真机没有输入法没法直接
# 看候选框位置，但可以验证底层依据——外层终端光标坐标——算得准不准）。
# 快照必须在新建 cursor 窗口之前拍，diff 才有意义（曾经错误地在 wait_for_cursor
# 成功之后才拍快照，那时新会话早已存在，diff 出来永远是空，是脚本自身的 bug，
# 不是产品的 bug）。
before_cursor="$(sessions | grep '^pickup-claude-' || true)"
tmux -L "$OUTER" new-window -t tui -n cursor
tmux -L "$OUTER" send-keys -t cursor "cd $REPO && env $ENVV python3 -m pickup claude --resume cursortest" Enter
wait_for_cursor() {
  local text="$1" tries="${2:-40}"
  local i
  for ((i = 0; i < tries; i++)); do
    tmux -L "$OUTER" capture-pane -p -t cursor 2>/dev/null | grep -qF -- "$text" && return 0
    sleep 0.25
  done
  echo "未等到（cursor 窗口）：$text" >&2
  tmux -L "$OUTER" capture-pane -p -t cursor >&2 || true
  return 1
}
wait_for_cursor "PROMPT>" 60
cursor_session="$(comm -13 <(echo "$before_cursor" | sort -u) <(sessions | grep '^pickup-claude-' | sort -u) | head -1)"
if [[ -z "$cursor_session" ]]; then
  echo "未能识别出 cursor 测试新建的托管会话名" >&2
  sessions >&2
  exit 1
fi
# 直启已自动聚焦右栏；等外层光标跟上内嵌 pane 光标即可。
expected_x=""
outer_cursor=""
inner_cursor=""
for _ in {1..40}; do
  inner_cursor="$(tmux -L "$KEEPALIVE" display-message -p -t "$cursor_session" '#{cursor_x},#{cursor_y}')"
  outer_cursor="$(tmux -L "$OUTER" display-message -p -t cursor '#{cursor_x},#{cursor_y}')"
  outer_x="${outer_cursor%,*}"; outer_y="${outer_cursor#*,}"
  inner_x="${inner_cursor%,*}"; inner_y="${inner_cursor#*,}"
  # 左栏固定宽度 39，右栏前保留 1 列空隙，因此面板起点在第 40 列；
  # 真实外层光标列 = 40 + pane 内真实光标列；顶栏与格标题各占 1 行，因此外层行
  # = 2 + pane 内真实光标行。算错了说明光标坐标换算或时机有问题。
  expected_x=$((40 + inner_x))
  expected_y=$((2 + inner_y))
  if [[ "$outer_x" == "$expected_x" && "$outer_y" == "$expected_y" ]]; then
    break
  fi
  sleep 0.15
done
outer_x="${outer_cursor%,*}"; outer_y="${outer_cursor#*,}"
inner_x="${inner_cursor%,*}"; inner_y="${inner_cursor#*,}"
expected_x=$((40 + inner_x))
expected_y=$((2 + inner_y))
if [[ "$outer_x" == "$expected_x" && "$outer_y" == "$expected_y" ]]; then
  ok "外层终端真实光标精确落在托管 pane 内的真实光标位置（内 ${inner_cursor} -> 外 ${outer_cursor}，IME 候选框定位依据的机制验证通过）"
else
  echo "光标锚定坐标不匹配：inner=${inner_cursor} outer=${outer_cursor} expected=${expected_x},${expected_y}" >&2
  exit 1
fi

# ---- IME 关键回归：外层真实光标必须「可见」（DECTCEM 打开），不能只是位置对 ----
# Textual 全屏运行期默认 `\e[?25l` 藏掉真实光标，只移动一个看不见的光标——位置
# 再准，IME 也没有可见锚点，用户在内嵌 Agent 里根本打不出中文（真机反馈）。
# `#{cursor_flag}` 反映托管 pickup 当前有没有把真实光标显示出来：聚焦内嵌 pane
# 且有可见光标时必须为 1。EmbedPane._set_real_cursor 就是补这一步的。
outer_cursor_flag="$(tmux -L "$OUTER" display-message -p -t cursor '#{cursor_flag}')"
if [[ "$outer_cursor_flag" == "1" ]]; then
  ok "外层终端真实光标处于「可见」状态（cursor_flag=1）——IME 有可见锚点，中文合成才能激活"
else
  echo "外层真实光标被隐藏（cursor_flag=${outer_cursor_flag}）：位置对但不可见，IME 仍无法工作" >&2
  exit 1
fi

# ---- 划词选中 + Ctrl+C 复制：验证真实 OSC 52 写入到达外层终端（tmux 充当） ----
tmux -L "$OUTER" send-keys -t cursor -l "$(printf '\033[<0;40;3M')"
sleep 0.05
tmux -L "$OUTER" send-keys -t cursor -l "$(printf '\033[<32;70;3M')"
sleep 0.05
tmux -L "$OUTER" send-keys -t cursor -l "$(printf '\033[<0;70;3m')"
sleep 0.3
tmux -L "$OUTER" set-option -t cursor set-clipboard on
tmux -L "$OUTER" send-keys -t cursor C-c
sleep 0.5
copied="$(tmux -L "$OUTER" show-buffer 2>/dev/null || true)"
if [[ "$copied" == *"PROMPT>"* ]]; then
  ok "划词选中托管 pane 画面文字后 Ctrl+C 复制，真实 OSC 52 写入到达外层终端"
else
  echo "划词复制未生效，tmux 缓冲区内容：${copied@Q}" >&2
  exit 1
fi

echo "==== $PASS passed, 0 failed ===="
