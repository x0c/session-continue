# session-continue 维护指南

## 标题与排序

- 最近会话排序优先使用历史文件更新时间。用户对“最近”的直觉是最近被续接或写入，而不是文件内部最后一条可解析消息。
- 文件时间不是绝对可信。Syncthing、复制或批量元数据刷新可能让一批历史 Claude 会话拥有同一个时间；扫描侧要识别批量刷新簇，并在明显污染时回退到会话内部最后事件时间。
- Claude Code 自带 `aiTitle` 不稳定，只能作为临时兜底的最后来源，不能绕过生成缓存直接展示。
- 无缓存时必须先生成本地短标题，再交给后台模型优化。首屏不能依赖 `claude -p` 是否及时返回。
- 后台标题生成可能留下新的 Claude 会话记录；扫描侧必须过滤自产标题 prompt 和只有低价值消息的记录，避免历史污染反过来进入列表。
- 标题生成只做批量调用。批量失败时保留临时标题，不逐条慢重试。

## 标题生成进程

- 标题生成必须由脱离当前终端的独立进程承载，不能放回 TUI 进程内线程。
- `execute_launch` 会用 `os.execvp` 替换当前进程；按 `q` 退出也会结束 TUI 进程。TUI 内线程会在这些路径上丢失未完成标题。
- 当前模型是：`_spawn_title_daemon` 拉起 `sc --generate-titles` 后台进程，后台进程用缓存目录下的文件锁保证全机单实例；TUI 侧只读缓存并轮询缓存文件变化。

## Claude 扫描

- Claude 的 `aiTitle` 不一定在第一条用户消息之前出现，扫描头部不能拿到工作目录和首条用户消息后立即停止。
- Claude 的 `/plan` 等本地命令会把真实需求放在 `<command-args>...</command-args>` 中，这类内容必须提取为用户意图。
- Claude 的兜底标题必须走候选评分。短续接词、催促、系统提示、错误消息和自产标题 prompt 都是低价值候选；用户侧全是低价值消息时，才允许用最后助手摘要兜底。
- Claude 侧通过 tail 消息里的 `[Request interrupted by user]` 精确字符串识别中断，不要用宽松关键词匹配。
- `titles.py` 的 `_compact_title` 里正则使用原始字符串。写 `\s`、`\S`、`\w`、`\d`、`\n` 时不要多打一层反斜杠；改这个函数前先用真实会话文本验证输出是完整可读片段。

## 扫描性能

- **硬性指标：`sc` 首屏（进程启动到 TUI 首次渲染）延迟必须 ≤1s。** `main()` 里 `store.load()`（→ `registry.scan_all()`）是同步阻塞首屏的调用，扫描没跑完屏幕就是空的；这个指标和验证方式记在 `AGENTS.md`「验证要求」，`test_session_scanning.py` 的 `StartupLatencyTests` 是配套的回归闸门。
- `scan_claude.py`/`scan_codex.py` 的 `scan_sessions()` 接受 `limit`，但早期实现会先对全部历史会话文件做完整的头尾 JSONL 解析，再 `results[:limit]` 截断——不管 `limit` 多小都要扫完全部历史（本机曾实测 796+1074 个文件耗时 ~5s，是 `sc` 启动慢的根因）。现在改为先用 `os.stat` 按真实文件 mtime 把候选文件排好序，只解析凑够 `limit` 条有效结果所需的那部分；新增或改写这两个扫描函数时不要退回“先建全量列表再截断”的写法。
- Claude 侧的提前停止是按“文件 mtime 分钟桶”粒度的，不是攒够 `limit` 条立刻停：一旦达到 `limit`，要把当前跨越的那个分钟桶处理完再停，否则会让「标题与排序」一节说的批量 touch 污染检测看到不完整的簇，判断失真。
- Codex 侧提前停止前必须先按真实文件 mtime（`os.path.getmtime`）重新排序，不能直接用 `_find_all_session_files` 现成的按文件名（创建时间）排序去做提前停止——同一会话被续接时 mtime 会变但文件名不变，按创建时间提前停会漏掉“很久以前创建、但刚被续接”的会话。
- `runtime/registry.py` 的 `scan_all()` 用 `ThreadPoolExecutor` 并发跑各运行时的扫描：各运行时读的是完全独立的目录、无共享状态，线程池只是为了重叠磁盘 I/O 等待。新增运行时时这个并发逻辑不用改，注册进去即可自动享受。
- **cwd 判活必须按 cwd 记忆化，不能逐会话裸调 `os.path.isdir`。** 排查过一次首屏 >1.3s 的问题：`scan_sessions` 循环里对每条候选会话的 `cwd` 都单独调用 `os.path.isdir` 判断目录是否还在（用于过滤已删除工作目录、无法 resume 的会话），但实测本机一次扫描里几百条候选会话经常只对应十几个不同的 cwd（同一项目下反复续接）；这些 cwd 常年落在 Syncthing/网络同步目录上，单次 `isdir` 实测 ~5-10ms，去重前光这一项就吃掉 profile 里 0.6s+ 的裸开销。修法是在 `scan_sessions` 内建一个按 cwd 缓存结果的 `isdir` 闭包（单次扫描内 cwd 存在性稳定，用完即弃），两个扫描函数都要保留这个闭包，不要退回裸调用。
- **Claude 侧完整解析前必须先用廉价预探（`_peek_head_meta`）拦掉自产噪音会话和死 cwd 会话，不能等整文件解析完才丢弃。** 后台标题生成会调用 `claude`/`codex`，在 `~/.claude/projects/` 留下以 `titles.PROMPT_MARKER` 开头的噪音会话；这类会话和 cwd 已删的会话本来就会在解析后被过滤，但过滤发生在读完 300 行头部 + 64KB 尾部之后，白白解析。实测本机为凑够 30 条有效结果，`_build_session_info` 曾被调 347 次，其中一大半是最终会被丢弃的噪音/死 cwd 会话。`_peek_head_meta` 只读头部 ≤40 行拿 cwd 和首条用户消息，探到确定是噪音或死 cwd 才提前 `continue`；探不到（如头部很长的真实会话）时不跳过，照常走完整解析兜底——改动前后结果必须字节级一致（id 顺序、`fallback_title`、`native_title` 全部相同），新增类似优化时也要用这个标准核验。Codex 侧噪音少，只做了 cwd 记忆化，没加预探，保持简单。
- 上述两项优化落地后，本机实测 `limit=30` 时 Claude 扫描从 1320ms 降到 225ms、Codex 从 585ms 降到 243ms，`scan_all` 并发后首屏约 0.25s；`limit=50`（默认值）约 0.6s，仍在 1s 硬指标内。
- 评估过给单文件解析结果加磁盘缓存（按 `路径+mtime+size` 做 key）的方案，判断当前收益不足以覆盖风险，暂缓：cwd 记忆化 + 预探已经把首屏压到 1s 硬指标以内，缓存要处理和后台标题生成进程的并发写、文件被删除/截断重写后的失效判断，复杂度换来的收益不划算。若历史继续增长导致启动明显变慢（>1s）或用户明确要求，再按 `titles.py` 已有的原子写 + 内容指纹失效模式加缓存。

## 界面

- TUI 表格列宽必须按终端显示宽度计算，不要用 Python 字符数直接 `ljust` 或切片；中文、箭头和状态图标会占 2 个显示列。
- 表格列之间用 `sc.py` 里的 `COL_GAP` 固定间隔拼接。
- 大小列统一用 MB、保留两位小数、右对齐。
- 主界面「状态」列显示的是**会话进程活性**（`live: bool`，两档：`进行中`/`已结束`，无图标），不是末轮对话怎么结束的；`titles.py` 里的 `STATUS_ABORTED`/`STATUS_PENDING`/`STATUS_DONE`/`STATUS_NONE`（人看的中文+emoji `status_tag`）和对应的英文枚举只在 `agent_api.py`（`sc list --status` 等脚本接口）里使用，两套语义已解耦，改其中一个不代表另一个也要跟着改。
- 判活刻意只做「进程在/不在」两档，不做 Claude 能力范围内的「思考中/空闲」细分——因为 Codex 没有对应信号，两个运行时的状态列必须口径一致。
- Claude 判活：遍历 `~/.claude/sessions/{pid}.json`（`scan_claude._live_session_ids`），`os.kill(pid, 0)` 确认进程仍存活后取其 `sessionId`；与 `active-claude-sessions` skill 同一判活思路。
- Codex 没有 pid 注册表，判活改用 `lsof`：活着的 `codex` 进程会以写模式持有自己的 rollout JSONL（实测 `lsof -p <pid>` 能看到形如 `codex … 45w … rollout-…-<uuid>.jsonl` 的记录），`scan_codex._live_session_ids` 用 `pgrep -x codex` 拿 pid 后逐个 `lsof -p` 解析文件名里的 UUID。`pgrep`/`lsof` 缺失或调用失败一律静默返回空集，退化为全部显示「已结束」，不抛异常、不阻塞扫描。
- 聊天记录预览必须按需读取，不加入启动扫描；只展示真实用户消息和每轮最终答复，过滤工具调用、工具结果、思考过程、进度播报和内部提醒。
- **弱化文字（分隔线/次要列/帮助文字）禁止用 `curses.COLOR_WHITE` 强制写死前景色，也不能只靠 `curses.A_DIM` 保证可读性。** 排查过一次用户在白色背景终端下反馈「弱化文字几乎看不清」的问题：老实现把 `PAIR_DIM`/`PAIR_TAB_INACTIVE` 的前景色写死成 `COLOR_WHITE` 再叠加 `A_DIM`，在深色终端上没问题，但在浅色/白色背景终端上等于「白底写白字再调暗」，对比度几乎为零。修法是在 `sc.py` 的 `_init_colors` 里判断 `curses.COLORS >= 256`：够 256 色时改用真正的中灰（256 色调色板第 244 号），不再叠加任何暗淡属性（灰色本身对比度已经够、再叠加会过淡）；退化到 8/16 色终端时才回退到「终端默认前景色（`use_default_colors()` 成功时的 `-1`）+ `A_DIM`」，靠终端自身配色跟随浅色/深色主题，而不是硬编码某个具体颜色。模块级 `DIM_EXTRA_ATTR` 由 `_init_colors` 按上述判断结果覆盖，所有需要弱化的文字必须用 `curses.color_pair(PAIR_DIM) | DIM_EXTRA_ATTR` 这个组合，不要在别处再手写字面量 `curses.A_DIM`。新增弱化用途的颜色对时按同样思路处理，不要复用裸 `COLOR_WHITE`。

## 运行时边界

- 界面和接力编排禁止新增 `if source == "claude"` 这类运行时分支。
- 运行时私有扫描格式、恢复参数和新会话参数必须留在对应适配器中。
- 公共流程只依赖注册表和统一接力模型。

## 机器接口维护（agent_api.py）

- `list`/`search`/`show`/`context`/`describe` 的 JSON envelope 结构（`{ok, data, error, meta}`）、
  退出码分配（0/1/2/3/5）和已发布字段名是对外契约，一旦发布过版本就按“只加不改不删”演进；
  确需破坏性变更时同步提升 `agent_api.AGENT_API_VERSION` 并在 `docs/SKILL.md` 标注。
- 新增子命令或参数只在 `agent_api.py` 的 `COMMANDS` 列表里加一份定义——`sc describe` 的输出、
  `argparse` 的参数解析共用同一份数据，不要为 `describe` 另写一套文案，否则会和真实行为漂移。
- `title` 字段只读 `titles.load_cache()`，不得在 `agent_api.py` 里触发 `refresh_titles`；机器接口
  不消耗 Claude 额度是硬约束，触发生成的入口只能是 `_spawn_title_daemon` 拉起的后台进程。
- `status` 是给程序判断用的英文枚举（`STATUS_LABELS`），`status_tag` 是给人看的中文 + emoji；
  新增状态时两边要同步更新，不能只加一边。
- `list`/`search` 的 `--limit` 是每个运行时的扫描深度，`--top` 才是最终结果数量上限；不要为了省事
  把 `--limit` 改回“扫描多少就返回多少”的混合语义。Agent 调用通常同时传 `--limit` 和 `--top`：
  前者控制找多深，后者控制 token。
- `list`/`search` 的返回行必须保留 `resumable`/`resume_command`；`search` 还必须保留 `score`、
  `matched_via` 和 `matched_fields`，排序按相关性分数优先、更新时间次之。`matched_via` 已发布过
  `quick`/`deep` 语义，不能改成数组；字段级命中信息放在 `matched_fields`。计算这些字段不能读取完整
  会话文件，避免破坏首屏 <1s 和 Agent 查询的低 token/低延迟目标。
- `--compact` 同时表示无缩进 JSON 和默认精简字段集；`--fields` 只能进一步裁剪/覆盖字段，不要让 compact
  模式输出比普通模式更大。
- `show --full` 的大结果优先配合 `--out` 落盘，stdout 只返回路径、字节数和消息数量摘要；完整 JSON
  envelope 写到目标文件。这个写文件行为只允许发生在用户显式传 `--out` 时，不能变成默认副作用。
- `sc.py` 的非 TTY 自动降级（`sys.stdin.isatty() and sys.stdout.isatty()`）和 `list/search/show/
  context/describe` 子命令分发写在 `main()` 顶部，早于旧版 `--json`/`--limit` 的 legacy parser；
  改 `main()` 时不要把两条路径的参数解析合并到同一个 `argparse.ArgumentParser`，legacy 路径的报错
  仍是给人看的文本，机器接口路径的报错必须是 JSON envelope，混用会破坏其中一边的调用方假设。

## 开源发布

- GitHub 公开仓库是 `https://github.com/x0c/session-continue`，本地远端名为 `github`；原 `origin` 仍指向内部 Forgejo，用于同步备份。
- 项目历史版本线已经到 `v0.2.x`，新增公开发布版本必须沿现有标签递增，不能从 `0.1.0` 重新开始。
- 打包元数据同时维护 `pyproject.toml` 和 `setup.cfg`。当前环境的旧版 setuptools 会把只写在 `pyproject.toml` 的包名解析成 `UNKNOWN-0.0.0`，所以改版本号、入口、描述或项目链接时要同步两处。
- 发布前至少构建一次 wheel，确认产物名形如 `session_continue-<version>-py3-none-any.whl`，并用临时目录安装后检查 `sc = sc:main` 入口元数据。
- 开源前隐私扫描要覆盖准备提交的文件和完整 Git 历史补丁内容；本机 `.git/config` 里的内部远端不进入仓库内容，但真实文件、历史提交、Release 说明和 README 不能包含密钥、个人路径、内网地址或占位符。
- GitHub Release 发布后检查 Actions、Release、topics 和仓库可见性；当前仓库 topics 为 `claude-code`、`codex-cli`、`terminal`、`tui`、`session-manager`、`ai-coding-agent`。

### 一键安装渠道

- Homebrew 配方在独立仓库 `x0c/homebrew-tap` 的 `Formula/session-continue.rb`，本项目不维护该文件的本地副本。配方用 `Language::Python::Virtualenv` 直接从 GitHub 源码 tag 归档安装（项目零运行时依赖，不需要 PyPI）。
- 新打 `v*` 标签并推送后，`.github/workflows/release.yml` 自动下载新 tag 归档、算 sha256、直接提交到 `x0c/homebrew-tap` 的 `main` 分支——不用手改配方文件里的版本号和哈希。也可以在 GitHub Actions 页面手动 `workflow_dispatch` 并填 tag 名重跑（比如某次自动触发失败后需要补跑）。
- 该步骤需要仓库 secret `HOMEBREW_TAP_TOKEN`：一个对 `x0c/homebrew-tap` 有 contents write 权限的 fine-grained PAT（不要复用本机 `gh auth` 的个人会话 token，那个权限范围过宽且和 CI 生命周期不一致）。token 过期或权限变更会导致这一步失败，发新版本后应看一眼 Actions 页面确认 bump 任务成功。
- 实现是纯 `curl + git`（见 `release.yml`），不依赖第三方 Action：`mislav/bump-homebrew-formula-action` 对 `HEAD /repos/{owner}/{repo}/tarball/{ref}` 的重定向只认严格等于 302，但 GitHub 自 2026-05-16 起对该端点的 HEAD 请求改答 303，导致该 Action 必现 `unexpected HTTP 303 response`（上游 issue mislav/bump-homebrew-formula-action#340，修复 PR #342 长期未合并）。改回用该 Action 前，先确认上游是否已发布修复版本。
- 不用 Homebrew 的用户走 `install.sh`（托管在本仓库 `main` 分支，通过 `curl -fsSL .../install.sh | bash` 执行）：校验 Python 版本、查询最新 Release 的 tag、`pip install --user` 安装、按需提示把安装目录加入 `PATH`。改这个脚本后必须实际执行一遍（可用 `PYTHONUSERBASE` 重定向到临时目录，避免污染真实用户环境），不能只过静态检查。
- 在 suzhou 上验证 `install.sh` 时，`pip install git+https://github.com/x0c/session-continue.git@<tag>` 可能卡在 GitHub clone 并超时（2026-07-06 实测约 130 秒后 `Failed to connect to github.com port 443`）。这属于该节点直连 GitHub 出口不稳定，不等于安装脚本或 tag 有问题；先原样重试，仍失败时换到 GitHub 出口稳定的环境验证，并在发布记录里明确写出阻塞输出。
- 三条安装路径（Homebrew、一键脚本、源码安装）在 `README.md` 里必须保持同步；新增或调整任一路径都要回头检查其余两条描述是否还准确。

## 真实路径验证

改标题、排序或列宽后，除编译和单测外，还要做真实路径验证：

```bash
python3 -m py_compile sc.py scan_claude.py scan_codex.py titles.py models.py agent_api.py runtime/*.py test_*.py
python3 -m unittest -v
```

然后用真实会话列表检查前 120 条没有 raw slug、纯命令、省略号或自产标题 prompt；再用真实终端启动一次 TUI 并退出，确认本机 `sc` 入口指向当前代码。
