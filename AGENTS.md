<!-- managed:inherited-agents:start -->
<!-- source: /Users/geraltgraham/Codes/pickup/AGENTS.md -->
# pickup

终端会话接力 CLI，支持跨 Claude Code / Codex / OpenCode / Kimi Code / Cursor 会话恢复与接力。

通用工程规范：[Python/通用](../_standards/)

## 文档导航

- [cli/AGENTS.md](cli/AGENTS.md)：改、评审或发布 pickup CLI 工具前必读（含界面改动后的截图验收）。Remote：`ssh://git@10.10.10.2:2222/Max/pickup.git`

## 组件一览

| 目录 | 技术栈 | 状态 |
|---|---|---|
| `cli/` | Python | 活跃 |

<!-- managed:inherited-agents:end -->

# pickup 项目规范

## 文档导航

- `README.md`：使用、修改、评审或扩展会话扫描、终端界面、标题生成、运行时适配和跨运行时接力前必读。
- `docs/MAINTAINER_GUIDE.md`：修改、评审、排查或优化标题生成、扫描排序、扫描性能/启动耗时（含首屏异步 `store.load`）、界面列宽/侧边栏配色与标题省略、runtime 名配色（`pickup.runtime_label_style`）、TUI 可观测性（`observe.py` / `events.log` / F12 截图 / `pickup diagnose`）、TUI 刷新（列表原地更新、spinner 省刷）、状态列/会话存活判断、会话预览（右栏完整对话、消息级时间戳、缓存按 mtime 失效）、运行时边界（含跨运行时接手提示词的对话摘录）、会话保活（`keepalive.py`，含 tmux 隔离、pid 祖先链匹配、回收策略）、内嵌面板（`embed.py` + `ui/embed_pane.py` 的 `EmbedPane`，含控制模式通道与抓帧 `request()`、真彩色直通、Line API 局部重绘、输入延迟、跨终端/触控板滚动兼容、回滚状态、鼠标拖拽选词的取舍、IME 光标锚定、深/浅主题背景色注入、「连接中…」卡死）、直启子命令（`pickup claude`/`pickup codex`，含 `auto_approve_args` 设计取舍）、`agent_api.py` 机器接口（含新增字段/参数、只读边界取舍、`AgentApiTests` 测试写法）、打包发布和 GitHub 开源维护前必读。
- `docs/SKILL.md`：修改、评审 `agent_api.py` 面向 Agent 的子命令、字段或退出码语义前必读（含 `diagnose` 与界面异常排查）；这是 Agent 侧唯一的使用文档，改命令行为必须同步这里。
- `PRIVACY.md`：修改、评审或排查历史文件读取、缓存写入、标题生成、跨运行时接力和开源隐私边界前必读。
- `CONTRIBUTING.md`：修改开源贡献流程、验证命令、设计边界或 PR 要求前必读。

## 架构约束

- `pickup.py` 只负责界面、会话展示和用户选择，不得直接拼接某个运行时的启动参数。
- 运行时私有行为必须收敛在 `runtime/` 对应适配器中；新增运行时只实现扫描、对话预览、原生恢复、历史格式提示、接力新会话（读取其他运行时历史）和空白新会话（不关联任何历史，仅指定工作目录）两种启动能力，并在默认注册表注册一次。
- 跨运行时接力统一走“源适配器导出 `Handoff` → 目标适配器生成 `LaunchPlan`”，禁止增加 Claude→Gemini、Codex→Gemini 等两两转换分支。
- 同运行时使用原生恢复；跨运行时必须新建目标会话、让目标 Agent 按需读取原始 JSONL，不能改写或伪造原会话。
- 标题生成是独立服务，不属于任何运行时适配器。生成后端统一走 `titlegen.py` 的 `TitleGenerator` 抽象，`titles.py` 不得直接拼接任何 CLI 命令；`titlegen.py` 与 `runtime/` 互不 import——运行时适配器管「怎么恢复/接力会话」，标题生成器管「怎么无头问一次模型」，两者后端恰好重名但职责不同，不要合并。标题和界面状态使用“运行时 + 会话 ID”作为唯一键，新增运行时不得退回纯会话 ID。新增标题生成后端时，若该 CLI 会把生成调用落盘成会话历史，对应扫描器必须加 `titles.PROMPT_MARKER` 前缀过滤。
- 会话预览：选中非进行中会话时，右栏直接展示完整对话；进行中/已托管会话右栏展示内嵌实时终端。唯一界面是左右分栏，禁止再加回全屏预览或纯列表第二套入口。
- **侧边栏末行间隔（硬约定）**：凡往左栏加控件（搜索框、新建项、会话卡、未来任何块），**最后一行必须是间隔空行**，画在该控件自身高度内并算进命中区与选中高亮；禁止用 `margin`、兄弟空隙或 `ListItem` padding 做分隔（点在空隙上不会落到本项）。当前基准：搜索框高 2、新建项高 2、会话卡高 3。细则见 `docs/MAINTAINER_GUIDE.md`「界面」节。
- `agent_api.py`（`pickup list`/`search`/`show`/`context`/`describe`）是只读数据接口，禁止新增任何执行/拉起副作用命令——pickup 只负责把会话数据交出来，怎么用是调用方的事。暴露更多可见性字段（如运行中会话的 `live`/`pid`）不违反这条约束，只要新字段本身来自扫描/只读探测、不触发任何拉起或写操作；真正"接管/下发指令给运行中会话"的能力不属于 pickup，留给调用方基于这些数据自行实现。命令参数与 `pickup describe` 的输出必须共用同一份 `COMMANDS` 定义，不能各写一份导致漂移。新增或修改子命令时同步 `docs/SKILL.md`。
- Agent 接口里 `list`/`search` 的 `--limit` 固定表示每个运行时的扫描深度，`--top` 才表示最终返回条数；`--compact` 必须同时做到紧凑 JSON 和精简默认字段。改这三个参数或 `show --out` 大结果落盘行为时，同步 `pickup describe`、`docs/SKILL.md` 和 `docs/MAINTAINER_GUIDE.md`。
- 会话保活（`keepalive.py`）是运行时无关的启动包装层，只在 `registry` 生成 `LaunchPlan` 之后、`execute_launch` 之前介入，禁止塞进 `runtime/` 某个具体适配器，也禁止让适配器感知 tmux 的存在。改保活匹配/回收逻辑前先读 `docs/MAINTAINER_GUIDE.md`「会话保活」节。`pickup claude`/`pickup codex` 直启子命令默认带 `_DirectLaunch` 进 TUI、经 `embed.host_session` 托管（与界面内「新建会话」同一路径），仅非真实终端 / `--no-keepalive` / 内嵌不可用时退回 `keepalive.enabled`/`wrap_plan` + `execute_launch` 旧路径（保活的第三个调用点，与 TUI 的 `_launch()` 复用同一套开关语义）。
- 内嵌面板（`embed.py`）是与 `keepalive.py` 平级的运行时无关层：不 attach，用 `capture-pane` 拿画面、经常驻 `tmux -C attach` 控制通道（`ControlChannel`）送按键与修改类命令（通道死亡自动回退外部 fork），把托管在保活 socket（`pickup-*`/`sc-*` 命名空间）里的会话渲染进 TUI 右半屏。适配器不感知本模块；`ui.main_screen.MainScreen`/`ui.embed_pane.EmbedPane` 是主要调用方（界面层已从 curses 换成 Textual，`embed.py` 本身与 UI 框架无关，未随之改动）。tmux 是软件级硬依赖（TUI 与直启启动时检查，缺失即报错退出；agent_api 只读子命令不受影响）。环境变量新名为 `PICKUP_*`（`PICKUP_KEEPALIVE`、`PICKUP_KEEPALIVE_IDLE_HOURS`、`PICKUP_TITLE_GENERATOR`、`PICKUP_TITLE_MODEL`、`PICKUP_RUNTIME`、`PICKUP_SESSION_ID`），旧名 `SC_*` 一律保留兜底读取/注入，不得删除兼容路径。
- 运行时跳过权限审批的危险启动参数（如 Claude 的 `--dangerously-skip-permissions`、Codex 的 `--dangerously-bypass-approvals-and-sandbox`）必须声明为对应适配器的 `auto_approve_args` 类属性，不得在 `build_resume_plan`/`build_new_plan`/直启透传等多处各写一份字面量字符串；`pickup.py` 和 `registry.build_passthrough_plan` 只负责按需拼接这个属性，不感知具体参数内容。

## 验证要求

**首屏（进程启动到 TUI 首次渲染完成）延迟目标 ≤1s；这条红线已随界面层改用 Textual 放宽为非阻断项（用户已同意），但改动扫描/标题/界面代码后仍必须实测并如实汇报耗时，不能不测。** 改动扫描（`scan_claude.py`/`scan_codex.py`/`runtime/`）、标题或界面相关代码后，除下面的编译/单测外，必须额外跑一次真实计时并汇报数值：

```bash
python3 -c "
import time
from runtime import default_registry
r = default_registry()
t = time.perf_counter()
r.scan_all(50)
print(f'{(time.perf_counter()-t)*1000:.0f}ms')
"
```

`test_session_scanning.py` 的 `StartupLatencyTests` 会在有真实会话数据时对同一调用做 <1s 断言（`python3 -m unittest -v` 已包含），但真实计时仍要单独跑一次确认，不能只信任测试里的一次采样。**不达标不再是提交阻断条件（硬性红线已放宽），但必须如实汇报实测耗时**；根因排查思路和已修过的坑见 `docs/MAINTAINER_GUIDE.md`「扫描性能」节。

改动代码、界面或运行时适配器后至少执行：

```bash
python3 -m py_compile pickup.py scan_claude.py scan_codex.py scan_opencode.py scan_kimi.py scan_cursor.py scan_common.py titles.py titlegen.py models.py agent_api.py keepalive.py embed.py observe.py runtime/*.py ui/*.py test_*.py
python3 -m unittest -v
```

涉及界面时还要运行一次真实终端冒烟。标题后台生成会调用本机 agent CLI、消耗对应账号额度；只验证界面时，在临时目录把 `claude`、`codex` 指向本机 `true`，放到 `PATH` 最前面，再启动 `python3 pickup.py --limit 5`，确认：

- 底部 Textual `Footer` 显示 `a Advanced`（中文环境下为 `a 高级操作`；`ui/main_screen.py` 的 `MainScreen.BINDINGS`，不再是 curses 手绘的底部帮助行）。
- 高级操作弹窗（`ui/modals.py` 的 `choose_target_runtime`）动态列出注册表中的运行时。
- 默认选中第一个已安装的其他运行时。
- `Esc` 先关闭弹窗，再退出主界面。
- 选中已结束会话时右栏是完整对话预览（含 `● 你` / 运行时名角色行），不再出现「最近提问 / 最近回复」摘要块。

**界面改动后的截图验收（必要步骤，不能只靠单测文字断言）：** Agent / 维护者必须自己进 TUI 出图并肉眼看图，确认布局与文案没有明显回归。标准做法（Textual Pilot → SVG → PNG，与当初 README 截图同一路径）：

```bash
cd cli
pip install cairosvg   # 首次；ImageMagick convert 渲 Rich SVG 常出空白图，不要当主路径
python3 docs/screenshots/capture.py   # → docs/screenshots/list.png
```

然后用读图工具打开 `docs/screenshots/list.png`（以及必要时其它新截图）检查：左栏搜索框与卡片、右栏完整对话、Footer、有无截断错乱、错误文案（如残留「最近提问」、空白右栏、运行时名缺失、标题整行转圈）。**runtime 配色不要只靠这张 SVG→PNG 判断**（导出常把真彩色压成灰阶）；颜色用真机 TUI 或 `SessionCard.render_line` 的 segment style 验收，见 `docs/MAINTAINER_GUIDE.md` 侧边栏配色踩坑。中文若成豆腐块，多半是截图环境缺 CJK 字体——本机（`root@10.10.10.2` / suzhou）需有 `fonts-noto-cjk`（`Noto Sans Mono CJK SC`）；`capture.py` 已按该字体族改写 SVG。属出图环境问题，不要当成产品回归。README 若仍引用旧「全屏预览」图，界面语义变了必须同步换图与说明。截图使用虚构演示数据，禁止把真实用户会话内容写进仓库。

**改动 `keepalive.py`、`pickup.py` 里保活相关的接线、`embed.py`/`ui/embed_pane.py` 内嵌面板、或 `pickup claude`/`pickup codex` 直启子命令时，除单测外必须额外跑一次真实 tmux 冒烟**：内嵌面板与界面交互（控制通道、滚轮转发、copy-mode、光标、主题注入、「连接中…」回归；界面层已从 curses 换成 Textual，鼠标拖拽选词这版暂未实现，见 `docs/MAINTAINER_GUIDE.md`「内嵌面板」节）的统一入口是仓库根的端到端脚本——直接跑 `bash selftest.sh`（约 90 秒，独立 tmux socket + 隔离 fake HOME，只创建/清理自己名下的 `pickup-claude-aaaa1111/bbbb2222` 会话，不碰其他保活会话），全部断言全绿才算过。用 `python3 -c "import keepalive; from models import LaunchPlan; print(keepalive.wrap_plan(LaunchPlan(('sleep','300'),None),'claude','smoketest'))"` 拿到真实 argv 后执行（加 `-d` 变成后台创建，不实际 attach），确认 `tmux -L pickup-keepalive list-sessions` 能看到会话、`keepalive.annotate()` 能靠 pid 匹配上、`keepalive.reap_idle(now=<未来时间戳>)` 能正确回收、正常退出（跑一个立即结束的命令如 `true`）后会话不留残留；测试用的 socket 用完后确认没有残留 `tmux -L pickup-keepalive` 进程（`ps aux | grep "[t]mux -L pickup-keepalive"` 应为空）。改完配置内容（`keepalive.py` 里的 `_TMUX_CONFIG` 常量）后，额外跑一次 `pip install --target <临时目录> .` 确认真实安装产物里没有缺文件（这个包用扁平 `py_modules` 分发，不会自动带上任何非 `.py` 文件）。直启子命令额外验证：把 `claude`/`codex` 指向本机 `true`（或一个会 sleep 的 fake 脚本）放到 `PATH` 最前面，跑 `pickup --no-keepalive claude <参数>` 确认参数原样透传且垫上了危险参数、用户已带危险参数时不重复；默认路径（真实终端内跑 `pickup claude`）确认进入 TUI 侧边栏模式、新会话包进 `tmux -L pickup-keepalive` 并显示在右栏；非真实终端（管道）则确认退回 `tmux -L pickup-keepalive` 包装后的 execvp 全屏接管。**本机若已有其他真实保活会话在跑（`tmux -L pickup-keepalive list-sessions` 能看到非本次测试创建的 `pickup-*`/`sc-*` 会话），冒烟测试一律只操作自己新建的会话名，不得 `kill-session` 或以其他方式影响已存在的会话**——那些通常是该机器上真实在跑的 Agent 会话。

**涉及会话扫描、标题或会话预览（`load_conversation`）时，改完必须至少随机抽查 5 条真实会话验证，不能只靠手写的单测小样例过关。** 优先用真实终端打开预览页肉眼检查内容，或写一次性脚本批量跑 `load_conversation`/`scan_sessions` 扫描本机全部真实会话文件、断言没有异常（如空文本、字面量 `"None"`、角色标错、时间戳缺失或非单调）。本机 Claude/Codex 历史里曾各自藏着单测样例覆盖不到的真实格式坑（`stop_reason` 与文本内容无关、`origin.kind` 区分真人和系统事件、`payload` 字段值可能是 JSON `null` 而不是缺失），这类坑只有跑真实数据才会暴露，见「Claude 扫描」节的具体记录。

**标题生成改动的自测硬要求：完成安装后必须直接运行真实 `pickup --generate-titles`，同时记录缓存条目数和待补会话数。** 若命令因已有后台补全进程持锁而立即返回，必须检查该进程及其 5 路生成子进程、持续观察缓存增长，不能把立即返回误判为未执行或完成；补全结束后再扫描确认只剩没有可提炼任务信息的会话，且这类会话不会继续排队。不得只验证 `pickup list`、源码函数或单测。

## 本机入口

源码主模块已改名 `sc.py` → `pickup.py`（sessionContinue 时代残留清理）；`pickup` 命令可能是 pip 安装到 site-packages 的独立副本（`pip show pickup` 可见版本），**不随源码目录更新**。验证界面/行为改动时要确认跑的是当前源码：`cd cli && python3 pickup.py`，或先重装：

```bash
cd cli
python3 -m pip install --user --force-reinstall --no-deps .
# 本机曾出现 pip install -e . 因 setuptools/easy_install 权限失败；force-reinstall 非 editable 更稳
```

仍不生效时核对 `command -v pickup`、`pip show pickup` 和：

```bash
python3 -c "import pickup; print(pickup.__file__); print(pickup.runtime_label_style('claude'))"
# 期望：路径指向刚装的 site-packages 或当前 cli/pickup.py，且打印 bold #D97757
```

确认没有运行另一台机器或旧目录中的副本；改完配色/布局后必须**重启**已打开的 TUI，旧进程不会热加载。
