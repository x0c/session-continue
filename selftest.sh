#!/usr/bin/env bash
# pickup 内嵌面板端到端自测：在独立 tmux socket 里跑真实 TUI，抓屏断言 + 模拟按键。
# 只创建/清理自己名下的 pickup-claude-aaaa1111/bbbb2222 会话，不碰本机其他保活会话。
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTER=pickuptest
TMP=$(mktemp -d /tmp/pickup-selftest.XXXXXX)
PASS=0; FAIL=0
mkdir -p "$TMP/shots"

ok()   { PASS=$((PASS+1)); echo "PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; echo "--- screen at failure:"; cap | tail -12; echo "--- keepalive:"; ka_sessions; fi; }

cap()  { tmux -L "$OUTER" capture-pane -p -t tui 2>/dev/null; }
capl() { tmux -L pickup-keepalive capture-pane -p -t "$1" 2>/dev/null; }
ka_sessions() { tmux -L pickup-keepalive list-sessions -F '#{session_name}' 2>/dev/null; }

wait_for() { # pattern [timeout] [capture-fn]
  local pat="$1" t="${2:-12}" fn="${3:-cap}" i=0
  while (( i++ < t*2 )); do
    if $fn | grep -qF -- "$pat"; then return 0; fi
    sleep 0.5
  done
  echo "--- TIMEOUT waiting for [$pat], current screen:" >&2
  $fn >&2
  return 1
}

# ---------- 环境：隔离 HOME + 假 claude（注册 pid 文件、免疫心跳、JSON 标题） ----------
mkdir -p "$TMP/home/.claude/projects/projA" "$TMP/workA" "$TMP/fakebin" "$TMP/home/.cache/pickup"
cat > "$TMP/home/.cache/pickup/titles.json" <<EOF
{"claude:aaaa1111":{"title":"自测标题A","fp":"seed"},"claude:bbbb2222":{"title":"自测标题B","fp":"seed"}}
EOF
cat > "$TMP/home/.claude/projects/projA/aaaa1111.jsonl" <<EOF
{"type":"user","message":{"content":"自测标题A的问题"},"timestamp":"2026-07-17T10:00:00.000Z","cwd":"$TMP/workA","sessionId":"aaaa1111"}
{"type":"assistant","message":{"content":[{"type":"text","text":"会话A的答复"}]},"timestamp":"2026-07-17T10:01:00.000Z","sessionId":"aaaa1111"}
EOF
cat > "$TMP/home/.claude/projects/projA/bbbb2222.jsonl" <<EOF
{"type":"user","message":{"content":"自测标题B的问题"},"timestamp":"2026-07-17T09:00:00.000Z","cwd":"$TMP/workA","sessionId":"bbbb2222"}
{"type":"assistant","message":{"content":[{"type":"text","text":"会话B的答复"}]},"timestamp":"2026-07-17T09:01:00.000Z","sessionId":"bbbb2222"}
EOF

cat > "$TMP/fakebin/claude" <<'EOF'
#!/usr/bin/env bash
# 假 claude：-p 走标题生成（输出 titles.py 期望的 JSON）；其余进入交互 REPL
# （回显输入、每秒心跳、C-c 可捕获、按真实 claude 的惯例注册 pid 会话文件）
if [[ "${1:-}" == "-p" ]]; then
  cat >/dev/null
  echo '{"claude:aaaa1111":"自测标题A","claude:bbbb2222":"自测标题B"}'
  exit 0
fi
if [[ "${1:-}" == "--version" ]]; then echo "fake-claude 0.0.1"; exit 0; fi
SID=""
prev=""
for a in "$@"; do [[ "$prev" == "--resume" ]] && SID="$a"; prev="$a"; done
FAKEHOME=$(cd "$(dirname "$0")/.." && pwd)/home   # tmux 服务器的 HOME 不是隔离环境，自己算
mkdir -p "$FAKEHOME/.claude/sessions"
printf '{"sessionId":"%s"}' "$SID" > "$FAKEHOME/.claude/sessions/$$.json"
trap 'echo "SIGINT-RECEIVED"' INT
trap 'rm -f "$FAKEHOME/.claude/sessions/$$.json"' EXIT
# 模拟 agent 启动时的深/浅主题检测：向终端发 OSC 11 背景色查询并打印应答。
# 没经 refresh-client -r 注入时，内嵌场景（有控制 client）应答应为默认黑色
# rgb:0000/0000/0000；注入后应为注入值——测试 16 据此验证背景色注入链路。
# 刻意放在 FAKE-CLAUDE-INTERACTIVE 就绪标志之前：wait_for 看到就绪标志时探测已结束，
# 否则探测窗口内到达的按键会被 python 的 os.read 吃掉。
# sleep 模拟真实 agent 的启动耗时（Claude Code 等加载要 1-2s）：pickup 的注入
# 在会话创建后 ~100ms 内完成，真实 agent 查询时注入早已就位
sleep 0.5
python3 -c 'import os,sys,termios,tty,select;fd=sys.stdin.fileno();old=termios.tcgetattr(fd);tty.setraw(fd);os.write(1,b"\x1b]11;?\x07");r,_,_=select.select([fd],[],[],2.0);d=os.read(fd,64) if r else b"TIMEOUT";termios.tcsetattr(fd,termios.TCSADRAIN,old);print("BGRESP",repr(d))' 2>/dev/null || echo "BGRESP unavailable"
echo "FAKE-CLAUDE-INTERACTIVE args=$*"
# bbbb2222 模拟申请了鼠标的 TUI 程序（Claude Code 等真实 agent 的行为）：
# 开 SGR 1006 鼠标上报，收到的输入原样记入 mouse.log 供滚轮直达断言
if [[ "$SID" == "bbbb2222" ]]; then
  printf '\e[?1000h\e[?1006h'
  exec 9>>"$FAKEHOME/mouse.log"
fi
( trap '' INT; i=0; while :; do i=$((i+1)); echo "TICK-$i"; sleep 1; done ) &
TICKPID=$!
while :; do
  if IFS= read -r -t 1 line; then
    if [[ "$line" == "STOP-TICK" ]]; then
      kill "$TICKPID" 2>/dev/null
      echo "TICK-STOPPED"   # 此后画面静止，用于复现「静止会话切回卡连接中」场景
    else
      echo "ECHO: $line"
    fi
    [[ "$SID" == "bbbb2222" ]] && printf '%q\n' "$line" >&9
  fi
done
EOF
chmod +x "$TMP/fakebin/claude"

TUIENV="HOME=$TMP/home PATH=$TMP/fakebin:/usr/local/bin:/usr/bin:/bin TERM=xterm-256color"

kill_mine() {
  for s in $(ka_sessions | grep '^pickup-claude-'); do
    tmux -L pickup-keepalive kill-session -t "$s" 2>/dev/null
  done
}
cleanup() {
  tmux -L "$OUTER" kill-server 2>/dev/null
  kill_mine
}
trap cleanup EXIT
tmux -L "$OUTER" kill-server 2>/dev/null  # 清掉上轮可能残留的外层 socket
kill_mine  # 清掉上轮测试残留（含 e 全屏产生的 pickup-claude-<uuid>）

# ---------- 0. 无 tmux 启动报错（直启路径最先检查） ----------
OUT=$(env -i PATH="$TMP/fakebin" HOME="$TMP/home" /usr/bin/python3 "$REPO/pickup.py" claude 2>&1)
check "无 tmux 时直启报错提示安装" '[[ "$OUT" == *"需要 tmux"* ]]'

# ---------- 1. 首屏 ----------
tmux -L "$OUTER" new-session -d -s tui -x 200 -y 50
tmux -L "$OUTER" set-option -g set-clipboard on   # pickup 的 OSC 52 落进外层 buffer 供选词断言
tmux -L "$OUTER" send-keys -t tui "cd $REPO && env $TUIENV python3 pickup.py --limit 5" Enter
wait_for "自测标题A" 20 || exit 1
check "首屏渲染出两个会话" 'cap | grep -qF "自测标题B"'
check "底部提示含 Enter 打开" 'cap | grep -qF "打开"'
cap > "$TMP/shots/1-first-screen.txt"

# ---------- 2. 回车 → 内嵌分栏 ----------
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "FAKE-CLAUDE-INTERACTIVE" 15 || exit 1
check "右半屏出现假 claude 画面" 'true'
check "内嵌会话托管进保活 socket" 'ka_sessions | grep -qx "pickup-claude-aaaa1111"'
check "假 claude 收到 --resume aaaa1111" 'capl pickup-claude-aaaa1111 | grep -qF -- "--resume aaaa1111"'
check "分栏状态：左栏列表仍在（自测会话标题可见）" 'cap | grep -qF "自测标题"'
check "面板提示行可见（C-\\ 回列表）" 'cap | grep -qF "回列表"'
cap > "$TMP/shots/2-embedded.txt"

# ---------- 3. 按键透传：打字 + Enter ----------
tmux -L "$OUTER" send-keys -t tui "hello embed world" Enter
wait_for "ECHO: hello embed world" 8 || exit 1
check "键入内容被会话回显（send-keys 通路）" 'true'

# ---------- 4. C-c 透传给会话且不杀 pickup ----------
tmux -L "$OUTER" send-keys -t tui C-c
wait_for "SIGINT-RECEIVED" 8 || exit 1
check "C-c 到达会话（SIGINT trap）" 'true'
check "pickup 自己还活着（列表仍渲染）" 'cap | grep -qF "自测标题"'
check "会话在 C-c 后仍存活" 'ka_sessions | grep -qx "pickup-claude-aaaa1111"'

# ---------- 5. C-\ 回列表，后台心跳继续 ----------
tmux -L "$OUTER" send-keys -t tui 'C-\'
wait_for "聚焦会话" 8 || exit 1
check "C-\\ 后焦点回列表（面板提示变为聚焦入口）" 'true'
BEFORE=$(capl pickup-claude-aaaa1111 | grep -oE "TICK-[0-9]+" | tail -1)
sleep 3
AFTER=$(capl pickup-claude-aaaa1111 | grep -oE "TICK-[0-9]+" | tail -1)
check "离开面板后后台会话心跳继续（$BEFORE → $AFTER）" '[[ -n "$BEFORE" && "$BEFORE" != "$AFTER" ]]'
cap > "$TMP/shots/5-focus-back.txt"

# ---------- 6. 切到第二个会话 ----------
tmux -L "$OUTER" send-keys -t tui j Enter
wait_for "--resume bbbb2222" 15 || exit 1   # bbbb 独有特征；aaaa 画面的 FAKE 行会误匹配
# tmux 服务器建会话与 pickup 处理 Enter 是异步的，pane_start_command 查询要轮询收敛
STARTCMD=""
for i in $(seq 1 10); do
  STARTCMD=$(tmux -L pickup-keepalive display-message -p -t pickup-claude-bbbb2222 "#{pane_start_command}" 2>/dev/null)
  [[ "$STARTCMD" == *"--resume bbbb2222"* ]] && break
  sleep 0.5
done
check "面板切换到第二个会话" '[[ "$STARTCMD" == *"--resume bbbb2222"* ]]'
check "第一个会话仍在后台" 'ka_sessions | grep -qx "pickup-claude-aaaa1111"'
check "重扫后第一个会话仍标注后台运行中" 'cap | grep -qF "后台运行中"'
cap > "$TMP/shots/6-second-session.txt"

# ---------- 6b. 滚轮直达：程序申请了鼠标时，滚轮编码为 SGR 序列发给它 ----------
sleep 2  # 等 pane_state 把 bbbb 的 mouse_any 刷进 emb
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<64;60;5M')"   # pane 内滚轮上一格
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<65;60;6M')"   # pane 内滚轮下一格
sleep 1
# fake claude 按行 read：无换行的 SGR 序列要和后续输入凑满一行才落 mouse.log，
# 补一行普通输入触发 read 返回（同时验证序列之后的按键通路正常）
tmux -L "$OUTER" send-keys -t tui "wheelmark" Enter
wait_for "ECHO: wheelmark" 8 || exit 1
sleep 1
# 外层 200x50：pane 内容区 0 列起算 45（0-based），ncurses 报 (59,4)/(59,5) → pane 内 (15,5)/(15,6)
check "滚轮上事件以 SGR 序列直达会话（64;15;5M）" 'grep -qF "64;15;5M" "$TMP/home/mouse.log"'
check "滚轮下事件以 SGR 序列直达会话（65;15;6M）" 'grep -qF "65;15;6M" "$TMP/home/mouse.log"'
check "SGR 路径不进入 copy-mode" '[[ "$(tmux -L pickup-keepalive display-message -p -t pickup-claude-bbbb2222 "#{pane_in_mode}" 2>/dev/null)" == "0" ]]'
cap > "$TMP/shots/6b-wheel-sgr.txt"

# ---------- 6d. m 键切换鼠标上报：关闭后滚轮不再直达，重开恢复 ----------
tmux -L "$OUTER" send-keys -t tui 'C-\'
wait_for "聚焦会话" 8 || exit 1
tmux -L "$OUTER" send-keys -t tui m   # 关闭鼠标上报
sleep 1
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "TICK-" 10 || exit 1
W0=$(wc -l < "$TMP/home/mouse.log")
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<64;60;5M')"   # 上报关闭中滚轮
sleep 1
tmux -L "$OUTER" send-keys -t tui "offmark" Enter
wait_for "ECHO: offmark" 8 || exit 1
sleep 1
W1=$(wc -l < "$TMP/home/mouse.log")
check "m 关闭后滚轮不再直达会话（行数 $W0 → $W1，只差 offmark 一行）" '[[ "$W1" -le "$((W0 + 1))" ]]'
tmux -L "$OUTER" send-keys -t tui 'C-\'
wait_for "聚焦会话" 8 || exit 1
tmux -L "$OUTER" send-keys -t tui m   # 重新打开
sleep 1
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "TICK-" 10 || exit 1
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<64;60;5M')"
sleep 1
tmux -L "$OUTER" send-keys -t tui "onmark" Enter
wait_for "ECHO: onmark" 8 || exit 1
sleep 1
check "m 重开后滚轮恢复直达（64;15;5M 再次出现）" '[[ "$(grep -cF "64;15;5M" "$TMP/home/mouse.log")" -ge 2 ]]'
cap > "$TMP/shots/6d-mouse-toggle.txt"

# ---------- 6f. 内置拖拽选词：按下→拖动→抬起，OSC 52 复制 + 反显高亮 ----------
# 当前 pane 聚焦 bbbb。先在 pane 内拖拽（选中 agent 画面文本）
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;60;6M')"    # press (59,5)
sleep 0.3
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<32;66;6M')"   # motion → (65,5)
sleep 0.2
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<32;72;7M')"   # motion → (71,6)
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;72;7m')"    # release (71,6)
sleep 1
BUF_PANE=$(tmux -L "$OUTER" show-buffer 2>/dev/null)
check "pane 内拖拽选中 agent 画面并复制（buffer 非空）" '[[ -n "$BUF_PANE" ]]'
check "选区反显高亮可见" 'tmux -L "$OUTER" capture-pane -p -e -t tui | grep -qF "$(printf "\033[7m")"'
cap > "$TMP/shots/6f-select-pane.txt"
# C-\ 回列表，拖拽底部帮助行的固定文案「↑↓ 选择」
tmux -L "$OUTER" send-keys -t tui 'C-\'
wait_for "聚焦会话" 8 || exit 1
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;2;50M')"    # press (1,49)
sleep 0.3
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<32;9;50M')"   # motion → (8,49)
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;9;50m')"    # release
sleep 1
BUF_LIST=$(tmux -L "$OUTER" show-buffer 2>/dev/null)
check "列表区拖拽选中固定文案（buffer=[$BUF_LIST]）" '[[ "$BUF_LIST" == *"选择"* ]]'
tmux -L "$OUTER" send-keys -t tui k   # 键盘输入 → 高亮清除
sleep 1
check "按键后选区高亮清除" '! tmux -L "$OUTER" capture-pane -p -e -t tui | grep -qF "$(printf "\033[7m")"'
# m 关闭上报后拖拽不再触发内置复制（终端原生框选接管，脚本侧表现为 buffer 不变）
tmux -L "$OUTER" send-keys -t tui m
sleep 1
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;2;50M')"
sleep 0.3
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<32;9;50M')"
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;9;50m')"
sleep 1
BUF_OFF=$(tmux -L "$OUTER" show-buffer 2>/dev/null)
check "m 关闭后拖拽不再内置复制（buffer 未变）" '[[ "$BUF_OFF" == "$BUF_LIST" ]]'
tmux -L "$OUTER" send-keys -t tui m   # 恢复上报
sleep 1
cap > "$TMP/shots/6f-select-list.txt"

# ---------- 6g. 选词区域钳制：从 pane 拖过分栏线，左栏文本不得进选区 ----------
tmux -L "$OUTER" send-keys -t tui Enter   # 聚焦 bbbb（pane 内选择场景）
wait_for "TICK-" 10 || exit 1
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;90;6M')"    # press pane 内 (89,5)
sleep 0.3
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<32;10;9M')"   # 大幅拖向左越过分栏线 → (9,8)
sleep 0.2
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<0;10;9m')"    # release
sleep 1
BUF_ZONE=$(tmux -L "$OUTER" show-buffer 2>/dev/null)
check "跨线拖拽后选区被钳制在 pane 内（buffer 不含左栏标题）" '[[ "$BUF_ZONE" != *"自测标题"* ]]'
cap > "$TMP/shots/6g-select-clamp.txt"
tmux -L "$OUTER" send-keys -t tui 'C-\'
wait_for "聚焦会话" 8 || exit 1

# ---------- 7. 预览页照常 ----------
tmux -L "$OUTER" send-keys -t tui 'C-\'
sleep 1
tmux -L "$OUTER" send-keys -t tui Space
wait_for "对话预览" 8 || exit 1
check "预览页全屏打开" 'true'
tmux -L "$OUTER" send-keys -t tui q
wait_for "聚焦会话" 8 || exit 1
check "预览关闭后回到分栏" 'true'

# ---------- 8. x 关闭后台会话（带确认；托管表兜底） ----------
tmux -L "$OUTER" send-keys -t tui k   # 光标回第一个会话
sleep 1

# ---------- 8a. 回车聚焦 aaaa（未开鼠标）：滚轮 → copy-mode 回滚 ----------
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "TICK-" 15 || exit 1
tmux -L "$OUTER" send-keys -t tui "backmark" Enter   # 新标记确认接回 aaaa 且按键通路正常
wait_for "ECHO: backmark" 8 || exit 1
sleep 2  # 等 pane_state 刷新 mouse_any=0
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<64;60;5M')"   # pane 内滚轮上
sleep 2
INMODE=$(tmux -L pickup-keepalive display-message -p -t pickup-claude-aaaa1111 '#{pane_in_mode}' 2>/dev/null)
check "滚轮上进入 copy-mode 回滚浏览（pane_in_mode=$INMODE）" '[[ "$INMODE" == "1" ]]'
# 渲染验证：pickup 画面必须真的显示回滚位置，不是只进了模式而视图没动
T_LIVE=$(capl pickup-claude-aaaa1111 | grep -oE "TICK-[0-9]+" | grep -oE "[0-9]+" | sort -n | tail -1)
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<64;60;5M')"   # 再滚一格（累计 6 行）
sleep 1.5
T_VIEW=$(cap | grep -oE "TICK-[0-9]+" | grep -oE "[0-9]+" | sort -n | tail -1)
check "滚轮后 pickup 渲染出回滚视图（画面 TICK-$T_VIEW 早于会话最新 TICK-$T_LIVE）" '[[ -n "$T_VIEW" && -n "$T_LIVE" && "$T_VIEW" -lt "$T_LIVE" ]]'
tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<65;60;5M')"   # pane 内滚轮下
sleep 1
tmux -L "$OUTER" send-keys -t tui "wmark" Enter                      # 普通键：先退 copy-mode 再透传
wait_for "ECHO: wmark" 8 || exit 1
INMODE2=$(tmux -L pickup-keepalive display-message -p -t pickup-claude-aaaa1111 '#{pane_in_mode}' 2>/dev/null)
check "按键自动退出 copy-mode 且到达会话（pane_in_mode=$INMODE2）" '[[ "$INMODE2" == "0" ]]'
cap > "$TMP/shots/8a-wheel-copymode.txt"

# ---------- 8b. 光标锚定：pane 聚焦时外层光标落在面板内（IME 预览跟随处） ----------
CUR=$(tmux -L "$OUTER" display-message -p -t tui '#{cursor_x} #{cursor_y}' 2>/dev/null)
CX=${CUR% *}
check "外层光标横坐标锚定在面板内（cursor_x=$CX，期望 ≥45）" '[[ "$CX" -ge 45 ]]'

# ---------- 8c. 输入延迟实测：发送 → 内层回显（L1）/ → 外层 TUI 回显（L2） ----------
ms() { date +%s%3N; }
MARK="latmark$$"
t0=$(ms)
tmux -L "$OUTER" send-keys -t tui "$MARK" Enter
L1=""; L2=""
for i in $(seq 1 150); do
  if capl pickup-claude-aaaa1111 | grep -qF "ECHO: $MARK"; then L1=$(( $(ms) - t0 )); break; fi
  sleep 0.02
done
for i in $(seq 1 150); do
  if cap | grep -qF "ECHO: $MARK"; then L2=$(( $(ms) - t0 )); break; fi
  sleep 0.02
done
echo "LATENCY 按键→内层回显 L1=${L1}ms  按键→外层 TUI 回显 L2=${L2}ms"
check "转发链路 L1 < 600ms（控制通道消 fork）" '[[ -n "$L1" && "$L1" -lt 600 ]]'
check "端到端回显 L2 < 1500ms（%output 事件驱动）" '[[ -n "$L2" && "$L2" -lt 1500 ]]'

# ---------- 8d. 拖拽事件风暴：aaaa 未申请鼠标，motion 快速丢弃不卡死 ----------
t0=$(ms)
for i in $(seq 1 100); do
  tmux -L "$OUTER" send-keys -t tui -l "$(printf '\033[<32;%d;5M' $((60 + i % 10)))"
done
tmux -L "$OUTER" send-keys -t tui "stormok" Enter
wait_for "ECHO: stormok" 12 || exit 1
ST=$(( $(ms) - t0 ))
echo "STORM 100 个拖拽 motion + 回显共 ${ST}ms"
check "风暴后 TUI 仍即时响应（${ST}ms < 15000）" '[[ "$ST" -lt 15000 ]]'
check "风暴未把 aaaa 弄进 copy-mode" '[[ "$(tmux -L pickup-keepalive display-message -p -t pickup-claude-aaaa1111 "#{pane_in_mode}" 2>/dev/null)" == "0" ]]'

# ---------- 8e. 静止会话切换：心跳停后切走再切回，不得卡在「连接中…」 ----------
# 回归场景：_embed_focus 重置 emb.size/grid 后，capture 线程在尺寸就绪前抓到静止
# 画面会错误标记 last_text，之后每轮 capture 文本相同便永远跳过解析（用户实报）
tmux -L "$OUTER" send-keys -t tui "STOP-TICK" Enter
wait_for "TICK-STOPPED" 8 || exit 1
tmux -L "$OUTER" send-keys -t tui 'C-\'
wait_for "聚焦会话" 8 || exit 1
tmux -L "$OUTER" send-keys -t tui j Enter   # 切 bbbb
sleep 3
tmux -L "$OUTER" send-keys -t tui 'C-\'
wait_for "聚焦会话" 8 || exit 1
tmux -L "$OUTER" send-keys -t tui k Enter   # 切回静止的 aaaa
sleep 3
check "切回静止会话正常显示画面（不卡连接中）" 'cap | grep -qF "TICK-" && ! cap | grep -qF "连接中"'
tmux -L "$OUTER" send-keys -t tui 'C-\'
wait_for "聚焦会话" 8 || exit 1
tmux -L "$OUTER" send-keys -t tui j Enter
sleep 2
tmux -L "$OUTER" send-keys -t tui 'C-\'
wait_for "聚焦会话" 8 || exit 1
tmux -L "$OUTER" send-keys -t tui k Enter
sleep 2
check "第二轮切回静止会话仍正常显示" 'cap | grep -qF "TICK-" && ! cap | grep -qF "连接中"'
cap > "$TMP/shots/8e-still-switch.txt"

tmux -L "$OUTER" send-keys -t tui 'C-\'
sleep 1
echo "=== pre-x state ==="
tmux -L pickup-keepalive list-sessions -F '#{session_name}|pane_pid=#{pane_pid}|dead=#{pane_dead}|#{pane_start_command}' 2>/dev/null
ls "$TMP/home/.claude/sessions/" 2>/dev/null
for f in "$TMP/home/.claude/sessions/"*.json; do echo "$f: $(cat "$f")"; done 2>/dev/null
cap | grep -a "自测标题"
tmux -L "$OUTER" send-keys -t tui x
wait_for "关闭后台进程" 8 || exit 1
tmux -L "$OUTER" send-keys -t tui y
sleep 2
check "x+y 后第一个会话被关闭" '! ka_sessions | grep -qx "pickup-claude-aaaa1111"'
check "第二个会话未受影响" 'ka_sessions | grep -qx "pickup-claude-bbbb2222"'
cap > "$TMP/shots/8-after-kill.txt"

# ---------- 9. c 关闭面板回全宽 ----------
tmux -L "$OUTER" send-keys -t tui c
sleep 1
check "c 后回到全宽布局（项目侧栏出现）" 'cap | grep -qF "项目"'
cap > "$TMP/shots/9-fullwidth.txt"

# ---------- 10. q 退出，后台会话保留 ----------
tmux -L "$OUTER" send-keys -t tui q
sleep 2
check "q 退出后第二个会话仍在保活" 'ka_sessions | grep -qx "pickup-claude-bbbb2222"'

# ---------- 11. 重开 pickup，回车接回存活会话（pid 祖先链 annotate 路径） ----------
tmux -L "$OUTER" send-keys -t tui "env $TUIENV python3 pickup.py --limit 5" Enter
wait_for "自测标题B" 20 || exit 1
wait_for "后台运行中" 15 || exit 1
check "重开后状态列显示后台运行中" 'true'
T_BEFORE=$(capl pickup-claude-bbbb2222 | grep -oE "TICK-[0-9]+" | tail -1)
tmux -L "$OUTER" send-keys -t tui j
sleep 1
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "TICK-" 15 || exit 1
check "回车直接接回画面（不新建进程）" '[[ "$(ka_sessions | grep -c "pickup-claude-bbbb2222")" == "1" ]]'
sleep 3
T_AFTER=$(capl pickup-claude-bbbb2222 | grep -oE "TICK-[0-9]+" | tail -1)
check "心跳延续同一进程（$T_BEFORE → $T_AFTER）" '[[ -n "$T_BEFORE" && "$T_BEFORE" != "$T_AFTER" ]]'
cap > "$TMP/shots/11-reattach.txt"

# ---------- 12. 粘贴（bracketed paste 序列整段注入） ----------
tmux -L "$OUTER" send-keys -t tui -H -- 1b 5b 32 30 30 7e 70 61 73 74 65 64 0a 6c 69 6e 65 32 0a 1b 5b 32 30 31 7e
wait_for "ECHO: pasted" 8 || exit 1
check "粘贴第一行被注入并回显" 'true'
check "粘贴第二行被注入并回显" 'capl pickup-claude-bbbb2222 | grep -qF "ECHO: line2"'
cap > "$TMP/shots/12-paste.txt"

# ---------- 13. e 全屏接管 + C-\ 脱离 ----------
tmux -L "$OUTER" send-keys -t tui 'C-\'
wait_for "聚焦会话" 8 || exit 1   # 确认焦点已回列表再发 e，否则 e 会被透传进 pane
tmux -L "$OUTER" send-keys -t tui e
wait_for "TICK-" 10 || exit 1
# 全屏接管后 pickup 的列表与底部提示消失（tmux attach 的画面里窗口右缘自带边框
# 竖线 │，不能用「竖线消失」当判据——那是 tmux 对小于终端的窗口画的边界）
check "e 后全屏接管（pickup 列表与提示消失）" '! cap | grep -qF "聚焦会话" && ! cap | grep -qF "自测标题"'
cap > "$TMP/shots/13-fullscreen.txt"
tmux -L "$OUTER" send-keys -t tui 'C-\'
sleep 2
check "C-\\ 脱离全屏回 shell（execvp 路径不变）" '! cap | grep -qF "FAKE-CLAUDE-INTERACTIVE"'

# ---------- 14. resize 同步 ----------
tmux -L "$OUTER" send-keys -t tui "env $TUIENV python3 pickup.py --limit 5" Enter
wait_for "自测标题B" 20 || exit 1
tmux -L "$OUTER" send-keys -t tui j
sleep 1
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "TICK-" 15 || exit 1
tmux -L "$OUTER" resize-window -t tui -x 150 -y 40
sleep 3
W=$(tmux -L pickup-keepalive display-message -p -t pickup-claude-bbbb2222 '#{window_width}' 2>/dev/null)
check "外层缩到 150 列后内嵌 tmux 窗口同步（=$W，期望≈104）" '[[ "$W" -ge 100 && "$W" -le 108 ]]'
cap > "$TMP/shots/14-resize.txt"

# ---------- 15. --no-keepalive：回车退化为 execvp ----------
tmux -L "$OUTER" send-keys -t tui 'C-\'
sleep 1
tmux -L "$OUTER" send-keys -t tui q
sleep 2
tmux -L "$OUTER" send-keys -t tui "env $TUIENV python3 pickup.py --limit 5 --no-keepalive" Enter
wait_for "自测标题B" 20 || exit 1
tmux -L "$OUTER" send-keys -t tui j
sleep 1
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "TICK-" 10 || exit 1
check "--no-keepalive 时回车为全屏 execvp（无分栏）" '! cap | grep -qF "项目"'
check "--no-keepalive 时不新增托管会话" '[[ "$(ka_sessions | grep -c "pickup-claude-bbbb2222")" == "1" ]]'
cap > "$TMP/shots/15-no-keepalive.txt"
tmux -L "$OUTER" send-keys -t tui C-d   # EOF 让 fake claude 的 read 退出
sleep 1

# ---------- 16. 背景色注入：PICKUP_OSC_REPORT 钩子 → refresh -r → pane 内 OSC 11 应答 ----------
# --no-keepalive 的 execvp 全屏里 fake claude 的 REPL 对 EOF 不退出（busy loop），
# 整个 tui session 销毁重建，确保 16 在干净的 shell 里启动 TUI
tmux -L "$OUTER" kill-session -t tui 2>/dev/null
sleep 1
tmux -L "$OUTER" new-session -d -s tui -x 200 -y 50
sleep 1
# 钩子值是 OSC 11 应答原文（ESC ] 11 ; rgb:abcd/1234/5678 BEL）的 hex 编码，
# 模拟真实终端对 pickup 启动探测的应答；tmux 注入后归一化为 abab/1212/5656。
OSCREPORT_HEX=$(python3 -c "print(b'\x1b]11;rgb:abcd/1234/5678\x07'.hex())")
tmux -L "$OUTER" send-keys -t tui "cd $REPO && env $TUIENV PICKUP_OSC_REPORT=$OSCREPORT_HEX python3 pickup.py --limit 5" Enter
wait_for "自测标题B" 20 || exit 1
tmux -L "$OUTER" send-keys -t tui n   # 在 bbbb 的目录新建空白会话：fake claude 全新启动才重新探测
wait_for "FAKE-CLAUDE-INTERACTIVE" 20 || exit 1
check "新内嵌会话启动并完成背景色探测" 'cap | grep -qF "BGRESP"'
check "pane 内 OSC 11 应答为注入值（abab/1212/5656，归一化后）" 'cap | grep -qF "abab/1212/5656"'
check "对照：bbbb 启动时无注入，tmux 按控制 client 默认值应答黑色" 'tmux -L pickup-keepalive capture-pane -p -S - -t pickup-claude-bbbb2222 2>/dev/null | grep -qF "rgb:0000/0000/0000"'
cap > "$TMP/shots/16-theme-injection.txt"
tmux -L "$OUTER" send-keys -t tui q
sleep 1

echo
echo "==== $PASS passed, $FAIL failed ===="
echo "screenshots: $TMP/shots/"
exit $((FAIL > 0))
