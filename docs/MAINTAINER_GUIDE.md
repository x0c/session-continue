# pickup 维护指南

## 标题与排序

- 最近会话排序优先使用历史文件更新时间。用户对“最近”的直觉是最近被续接或写入，而不是文件内部最后一条可解析消息。
- 文件时间不是绝对可信，且污染粒度可以细到单个文件，不一定成批出现：Claude Code 在会话驻留/被重新打开时会追加没有时间戳的元数据条目（`last-prompt`、`ai-title`、`mode`、`permission-mode`），把文件 mtime 顶到“现在”而不产生任何新对话内容；Syncthing、复制、批量元数据刷新是同一类问题的批量版本。修正逻辑统一收在 `models.py` 的 `effective_session_time(file_mtime, event_time)`：当 mtime 比会话内部最后一条真实事件新出 1 小时以上的 gap，就判定 mtime 不可信，逐会话回退到 event_time；两个扫描器的 `_build_session_info` 都在返回结果前调用它写回 `mtime`/`display_time`/`time_source`。曾经按“同一分钟桶 ≥5 个会话”识别批量污染簇的启发式已废弃——它只覆盖批量场景，漏过了本节描述的单文件被驻留进程 touch 的情形（真实故障：两个会话被 touch 到不同分钟，各自没能凑够聚簇阈值，在列表里显示成"20分钟前"，实际是 9-11 天前的会话）。
- Claude Code 自带 `aiTitle` 不稳定，只能作为临时兜底的最后来源，不能绕过生成缓存直接展示。
- 无缓存时必须先生成本地短标题，再交给后台模型优化。首屏不能依赖后台生成器（`claude`/`codex` 无头调用）是否及时返回。
- 后台标题生成可能留下新的 Claude 会话记录；扫描侧必须过滤自产标题 prompt 和只有低价值消息的记录，避免历史污染反过来进入列表。
- 标题生成只做批量调用。批量失败时保留临时标题，不逐条慢重试。
- 标题生成后端已抽象为 `titlegen.py` 的 `TitleGenerator`（当前有 claude、codex 两个实现）。`titles.py` 只负责批量 prompt、JSON 解析和缓存，不感知具体 CLI；新增生成器只在 `titlegen.py` 加实现并注册进 `_GENERATORS`，禁止在 `titles.py` 里写 `subprocess` 调用。选择顺序：`SC_TITLE_GENERATOR` 环境变量 → 按注册顺序取第一个已安装的；`SC_TITLE_MODEL` 覆盖模型。缓存与生成器无关，换生成器不重算已有标题。
- 自产噪音会话的过滤，每个可能被生成器落盘的运行时扫描器都要有：Claude 侧靠 `PROMPT_MARKER` 预探过滤（见「扫描性能」）；Codex 侧生成用 `codex exec --ephemeral` 不落盘，扫描过滤仅是兜底。接入没有 ephemeral 类开关的 CLI 后端（如 opencode run，每次调用必然真实落一条会话）时，对应扫描器的 `PROMPT_MARKER` 过滤是必需项，漏掉会让标题生成会话刷屏列表。

## 标题生成进程

- 标题生成必须由脱离当前终端的独立进程承载，不能放回 TUI 进程内线程。
- `execute_launch` 会用 `os.execvp` 替换当前进程；按 `q` 退出也会结束 TUI 进程。TUI 内线程会在这些路径上丢失未完成标题。
- 当前模型是：`_spawn_title_daemon` 拉起 `pickup --generate-titles` 后台进程，后台进程用缓存目录下的文件锁保证全机单实例；TUI 侧只读缓存并轮询缓存文件变化。
- 后台进程内的生成器选择发生在 `refresh_titles`（`titlegen.resolve_generator`），本机一个 agent CLI 都没有时静默跳过，列表保持临时兜底标题；不要在 TUI 首屏路径做可用性探测。

## Claude 扫描

- Claude 的 `aiTitle` 不一定在第一条用户消息之前出现，扫描头部不能拿到工作目录和首条用户消息后立即停止。
- Claude 的 `/plan` 等本地命令会把真实需求放在 `<command-args>...</command-args>` 中，这类内容必须提取为用户意图。
- Claude 的兜底标题必须走候选评分。短续接词、催促、系统提示、错误消息和自产标题 prompt 都是低价值候选；用户侧全是低价值消息时，才允许用最后助手摘要兜底。
- Claude 侧通过 tail 消息里的 `[Request interrupted by user]` 精确字符串识别中断，不要用宽松关键词匹配。
- `titles.py` 的 `_compact_title` 里正则使用原始字符串。写 `\s`、`\S`、`\w`、`\d`、`\n` 时不要多打一层反斜杠；改这个函数前先用真实会话文本验证输出是完整可读片段。
- **`load_conversation`（会话预览的正文来源）不能按 `stop_reason` 过滤要不要展示某条 assistant 文本。** 真实 JSONL 里一次 assistant 轮次的 `thinking`/`text`/`tool_use` 各是独立的顶层行，且共享同一个 `stop_reason`——哪怕这一行本身就是纯文本、只是后面紧跟着一次工具调用，它的 `stop_reason` 也是 `tool_use` 而不是 `end_turn`。之前的实现按 `stop_reason in (None, "end_turn")` 过滤 assistant 行，实测在一个 97 条真实消息的会话里把 88 条有真实文本内容的 assistant 消息整段丢了（包括工具调用前的说明、`AskUserQuestion` 之前的分析文字等），预览页看起来像是"Claude 跳过了回答"，其实是解析漏了。现在只要 `content` 里有非空 `text` block 就展示，不再看 `stop_reason`；`stop_reason is None` 分支保留给可能存在的历史遗留流式格式（增量快照，只在 flush 前取最后一份），和当前主流格式互不冲突。改这块逻辑前先用真实会话文件跑一遍 `load_conversation` 数消息条数，不要只信单测里手写的小样例。
- **Monitor/task-notification 等系统注入事件在原始 JSONL 里也挂在 `type: "user"` 轮次下，`load_conversation` 必须整条丢弃，不能当成真人输入。** 区分信号是顶层 `origin.kind` 字段：真人手动输入是 `"human"`（或字段缺失，如老格式/`/plan` 等本地命令包装出的用户轮次），系统事件（Monitor 到点触发、task 通知）是 `"task-notification"`。消息历史只保留 Agent 和真人的对话，系统事件价值很低——最初的实现把它标成 `ConversationMessage("system", ...)` 单独渲染成"◇ 系统事件"展示出来，被用户否决（"什么系统消息都不要显示出来"），改成命中 `origin.kind not in (None, "human")` 就直接 `continue` 跳过，不进入返回结果。`ConversationMessage.role` 因此保持只有 `user`/`assistant` 两种，不要再引入第三种 role。新增其它 `origin.kind` 取值（目前本机全量历史只出现过 `human`/`task-notification` 两种）时按同一分类原则处理，不要默认归到 `user`。
- **Codex 的 `event_msg.payload` 里字段值可能是 JSON `null`（key 存在但值为 null），`payload.get(key, "")` 的默认值只在 key 缺失时生效，取不到 null 场景，会拿到 `None` 再被 `str()` 变成字面量 `"None"` 混进正文。** 实测 `task_complete.last_agent_message` 为 null 很常见（任务结束但没有最终文本输出，比如被打断/答案已在更早轮次说完），预览页因此显示过多轮" ◆ Codex\nNone"。三处取值（`user_message.message`、`agent_message.message`、`task_complete.last_agent_message`）统一改成 `payload.get(key) or ""`，`or` 会把 `None` 也兜成空字符串再被后续的 `if text:` 过滤掉。改 `scan_codex.py` 任何从 payload 取文本的地方都要用这个写法，不要用 `.get(key, "")`。

## Codex 扫描

- **Codex 自身的多智能体（swarm/subagent）任务会把每个子代理线程各自写成一份独立的 `rollout-*.jsonl` 文件，扫描时必须过滤掉，不能当作用户发起的顶层会话列出。** 真实故障：一个真实会话（`session_meta.payload.thread_source` 缺失或为 `"user"`）执行多智能体任务时，会派生出好几个子代理线程（`thread_source: "subagent"`，`forked_from_id`/`parent_thread_id` 指向父会话，`agent_nickname` 是 Codex 随机取的代号），这些子代理线程 fork 时继承了父会话开头的历史，因此它们文件里"第一条用户消息"（也就是列表兜底标题的来源）和父会话完全相同。`_find_all_session_files` 对 `~/.codex/sessions/` 下所有 `.jsonl` 一视同仁地扫描，没读 `thread_source` 字段时，这些子代理文件会和父会话一起出现在列表里，表现为同一段任务描述反复出现好几条、目录和时间都很接近，用户会误以为是"同一个会话被重复列出"的 bug，实际是把 Codex 内部子任务线程误当成了独立会话。修法是在 `_build_session_info` 读 `session_meta` 时顺带取 `payload.get("thread_source")`，`scan_sessions` 里 `thread_source == "subagent"` 直接 `continue` 跳过，和过滤空会话、死 cwd 会话放在同一批前置检查里。

## OpenCode 扫描

- **历史存储是 SQLite（`~/.local/share/opencode/opencode.db`），不是 JSONL 文件**：`session`/`message`/`part` 三张表，正文只在 `part.data` 的 JSON `text` 字段（`message.data` 只有角色/时间/finish 等元数据）。OpenCode v1.2.0 起才是这个格式，更早版本的纯 JSON 文件存储**不做兼容**——官方升级会自动迁移到 SQLite，遗留在老格式的用户极少；本机没有 `opencode.db` 时这个运行时的会话列表就是空的，不报错、不尝试读旧格式。
- **只读连接，WAL 库可能拒绝只读打开**：`_connect_ro` 用 `sqlite3.connect("file:<path>?mode=ro", uri=True, timeout=0.5)`。opencode 正在写库（WAL 模式）时，极端情况下（需要 checkpoint 恢复且无活跃写者）只读打开可能失败；所有查询包一层 `except sqlite3.Error`，失败就把该 db 当 0 条会话处理，不抛异常、不阻塞其它运行时的扫描。`timeout=0.5` 把 busy 等待封顶在 0.5s，避免拖垮首屏 1s 硬指标。
- **一条 SQL 拿 top-N，含四个预览子查询**：过滤 `parent_id IS NULL`（子代理会话，本机真实数据 31 条会话里 22 条是子代理拆分出的）和 `time_archived IS NULL`（已归档），按 `time_updated DESC` 排序；四个关联子查询分别取最后一条消息（推状态用）、首/末条用户文本、末条助手文本、`SUM(LENGTH(part.data))` 作 `size_bytes`（标题缓存失效 key，必须随内容增长，不能用固定值）。本机 31 会话/219 消息/1135 part 实测这条 SQL 含全部子查询仅 7ms，远在预算内，无需再加 Codex 那种"凑够 limit 提前停止"的优化。
- **状态推导没有显式中断信号**：OpenCode 不像 Codex 有 `turn_aborted` 这种明确事件，末轮 `finish` 只有 `stop`/`tool-calls`/缺失/未知几种取值；只有消息带非空 `error` 字段才判"已中断"，`finish=="tool-calls"` 或其它值一律归"无状态"（宁缺毋滥），不要臆测把 `tool-calls` 当成中断或完成。
- **判活没有 Codex 那样"进程独占持有会话文件"的信号**：历史在共享 SQLite 里，无法用 `lsof` 定位某个 pid 对应哪个会话。改用 `pgrep -x opencode` 拿存活进程后读其工作目录（Linux 用 `/proc/<pid>/cwd`，macOS 用 `lsof -a -p <pid> -d cwd -Fn` 只查 cwd 一个 fd，比 Codex 判活时的全量 `lsof -p` 还便宜），与会话的 `directory` 字段（`os.path.realpath` 归一化）匹配；命中即认为该 cwd 下"最新一条"会话存活，同目录下更老的历史会话不标记（宁缺毋滥）。已知局限：`opencode serve`/`opencode run` 等同名进程会被一并计入；判活失败（`pgrep` 缺失/调用失败）静默降级为空集，不抛异常。
- **`--dangerously-skip-permissions` 实测只在 `opencode run` 子命令下生效，官方文档提到的 `--auto` 在本机 v1.15.11 完全不可用**：起初按照 claude/codex 的既有模式把这个 flag 塞进 `auto_approve_args` 类属性、四处复用，结果真机冒烟发现 `opencode --dangerously-skip-permissions -s <id>`（裸 TUI 命令）直接报错退出（exit=1，打印用法说明）——yargs 对默认/主命令的参数校验是严格模式，这个 flag 只在 `opencode run [message..]` 的 `--help` 里出现，主命令完全不认。同时按官方文档站（`opencode.ai/docs/permissions`）的说法，应该有个语义更安全的 `--auto` 参数（保留 deny 规则，只放行会弹 ask 的请求），但本机实测 `opencode --auto` 和 `opencode run --auto` 两种写法都同样报错退出（exit=1）——这台机器装的 1.15.11 版本还不支持这个参数，文档领先于当前发行版（或反过来是文档过时），**以实测行为为准，不要以文档为准**；未来升级 opencode 版本后应重新验证 `--auto` 是否可用，若可用应优先切换过去（deny 规则仍生效，比 `--dangerously-skip-permissions` 更安全）。因此 `OpenCodeRuntime.auto_approve_args` 显式设为空元组（不像 claude/codex 那样声明危险参数），`--dangerously-skip-permissions` 只硬编码在 `build_continue_plan`（唯一走 `run` 子命令、确认可用的路径）里；这意味着 `pickup opencode`（裸直启，透传给主命令）不会像 `pickup claude`/`pickup codex` 那样自动垫上跳过审批参数，也意味着 `build_new_plan`（跨运行时接力读取其它运行时历史时的新会话）里目标 OpenCode 若触发权限询问，需要用户在 TUI 里手动确认——这是相对 claude/codex 的已知能力差距，不是遗漏。

## Kimi 扫描

- **历史按「工作区 / 会话」两级目录存放，不是单文件**：`~/.kimi-code/sessions/<workspace_id>/<session_id>/`，元数据在 `state.json`（`title`、`isCustomTitle`、`workDir`、`lastPrompt`、`createdAt`/`updatedAt`），对话流水在 `agents/main/wire.jsonl`。子 agent 的 `agents/<other>/wire.jsonl` 是旁路对话，扫描和预览一律只读 `main`，忽略其它 agent。元数据优先取 `state.json`（小而权威，`updatedAt` 直接作会话时间）；正文只能解析 `wire.jsonl`。本机没有 `~/.kimi-code/sessions/` 时该运行时会话列表为空，不报错。
- **wire.jsonl 是协议事件流，混着体量很大的噪音行**：开头的 `config.update`（系统提示，约 20KB）、`llm.tools_snapshot`（工具定义，可上百 KB）、`llm.request`/`usage.record` 等都与对话正文无关。逐行 `json.loads` 会很慢，`scan_kimi._iter_message_entries` 先按带引号的类型值子串（`"context.append_message"` / `"context.append_loop_event"`）廉价过滤，只对真正承载对话的两类事件行做完整解析。**用带引号的类型值而不是 `"type":"…"` 前缀做匹配，是为了兼容紧凑与带空格两种 JSON 写法**（真实 wire 是紧凑的，但测试 fixture 用默认 `json.dumps` 带空格）。
- **用户 / 助手正文分别来自两类事件**：用户消息是 `type=="context.append_message"` 且 `message.role=="user"`，正文在 `message.content` 里 `type=="text"` 的分片；`message.origin.kind` 非 `"user"`（如 `task-notification` 等系统注入事件）一律丢弃，和 Claude 的 `origin.kind` 过滤同思路。助手正文是 `type=="context.append_loop_event"` 且 `event.type=="content.part"` 且 `event.part.type=="text"`；`part.type=="think"` 是思考过程，跳过。同一轮里连续的文本分片合并成一条助手消息，遇到下一条用户消息断开成新一轮。（`turn.prompt` 事件与 `context.append_message` 冗余，不解析，避免用户消息重复。）
- **状态推导按末轮角色**：解析到的最后一条消息是用户 → `⏳待回复`，是助手 → `✅已完成`，都没有 → 无状态。Kimi 的 `step.end.finishReason` 里虽然可能有中断信号，但格式尚未在真实数据里充分观察，暂不细分「已中断」，宁缺毋滥（和 OpenCode 一致）。
- **判活同 OpenCode 思路**：Kimi 没有 pid 注册表，历史也不独占单文件。`_live_pids_by_cwd` 用 `pgrep -x kimi-code`（进程 comm 是 `kimi-code`，不是 `kimi`）拿存活进程，读其 cwd 与会话 `workDir` 归一化匹配，同 cwd 只标最新一条存活。已知局限：`kimi server`/`kimi web` 等同名进程会被计入；判活失败静默降级为空集。
- **接力到 Kimi 只能走非交互模式（相对 claude/codex 的已知能力差距）**：Kimi 的 `-p/--prompt` 是「跑一个 prompt 并打印，跑完退出」的 headless 模式；根命令不接受位置参数形式的初始 prompt（带位置参数会报 `unknown command`），交互式 TUI 也没有从命令行预置首条消息的入口（实测 dist 里根 action 是 `opts.prompt !== void 0 ? "run prompt" : "start shell"`，二选一）。因此 `KimiRuntime.build_new_plan`（跨运行时接力读别家历史新建 Kimi 会话）只能用 `kimi --add-dir <源历史目录> -y -p <接力提示词>`：Kimi 读原始历史、把最后一个未完成任务跑完并打印结果后退出，用户随后可用 `kimi -c`（continue previous session for working dir）在同一会话上继续交互。同运行时原生恢复（`kimi -y -S <sessionId>`）和空白新会话（`kimi -y`）不受此限。Kimi 作为接力**源**（被别家读取）完全正常：`export_handoff` 指向 `wire.jsonl`，`history_reading_hint` 说明上面的格式。未来 Kimi 若新增交互式预置 prompt 的入口，应把 `build_new_plan` 切成交互式，与 claude/codex 对齐。
- **`-y/--yolo` 在根命令即生效**：不像 OpenCode 的危险参数只在子命令下可用，Kimi 的 `-y` 主命令直接接受，所以正常放进 `KimiRuntime.auto_approve_args`，`pickup kimi` 裸直启会自动垫上，与 claude/codex 一致。

## 扫描性能

- **硬性指标：`pickup` 首屏（进程启动到 TUI 首次渲染）延迟必须 ≤1s。** `main()` 里 `store.load()`（→ `registry.scan_all()`）是同步阻塞首屏的调用，扫描没跑完屏幕就是空的；这个指标和验证方式记在 `AGENTS.md`「验证要求」，`test_session_scanning.py` 的 `StartupLatencyTests` 是配套的回归闸门。
- `scan_claude.py`/`scan_codex.py` 的 `scan_sessions()` 接受 `limit`，但早期实现会先对全部历史会话文件做完整的头尾 JSONL 解析，再 `results[:limit]` 截断——不管 `limit` 多小都要扫完全部历史（本机曾实测 796+1074 个文件耗时 ~5s，是 `pickup` 启动慢的根因）。现在改为先用 `os.stat` 按真实文件 mtime 把候选文件排好序，凑够 `limit` 条有效结果就停止；新增或改写这两个扫描函数时不要退回“先建全量列表再截断”的写法。因为时间修正已经收敛成单会话 `effective_session_time` 判断（见「标题与排序」），提前停止不再需要按分钟桶粒度对齐。
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
- 主界面「状态」列显示的是**会话进程活性**（`live: bool`，两档：`进行中`/`已结束`，无图标），不是末轮对话怎么结束的；`titles.py` 里的 `STATUS_ABORTED`/`STATUS_PENDING`/`STATUS_DONE`/`STATUS_NONE`（人看的中文+emoji `status_tag`）和对应的英文枚举只在 `agent_api.py`（`pickup list --status` 等脚本接口）里使用，两套语义已解耦，改其中一个不代表另一个也要跟着改。
- 判活刻意只做「进程在/不在」两档，不做 Claude 能力范围内的「思考中/空闲」细分——因为 Codex/OpenCode 没有对应信号，三个运行时的状态列必须口径一致。
- Claude 判活：遍历 `~/.claude/sessions/{pid}.json`（`scan_claude._live_session_ids`），`os.kill(pid, 0)` 确认进程仍存活后取其 `sessionId`；与 `active-claude-sessions` skill 同一判活思路。
- Codex 没有 pid 注册表，判活改用 `lsof`：活着的 `codex` 进程会以写模式持有自己的 rollout JSONL（实测 `lsof -p <pid>` 能看到形如 `codex … 45w … rollout-…-<uuid>.jsonl` 的记录），`scan_codex._live_session_ids` 用 `pgrep -x codex` 拿 pid 后逐个 `lsof -p` 解析文件名里的 UUID。`pgrep`/`lsof` 缺失或调用失败一律静默返回空集，退化为全部显示「已结束」，不抛异常、不阻塞扫描。
- OpenCode 既没有 pid 注册表，也没有可独占定位的会话文件（历史在共享 SQLite 里），判活退化为「进程 cwd 匹配会话 directory」：`scan_opencode._live_pids_by_cwd` 用 `pgrep -x opencode` 拿 pid，读其工作目录后与会话 `directory` 归一化匹配，同一 cwd 下只标最新一条会话存活。细节和已知局限见「OpenCode 扫描」节。
- 聊天记录预览必须按需读取，不加入启动扫描；只展示真实用户消息和每轮最终答复，过滤工具调用、工具结果、思考过程、进度播报和内部提醒。
- 预览页头部右侧展示的会话 ID 用完整 `session["id"]`（36 位 UUID），**不用 `short_id`**：这里是给用户直接复制去跑 `claude --resume <id>`/`codex resume <id>` 等原生命令的，这两个命令都要求完整 ID，8 位前缀只在 `pickup show <前缀>` 这类 pickup 自己的 Agent 接口里可用。
- **消息级时间戳**：`models.ConversationMessage` 多了个可选 `timestamp: float | None` 字段，两个扫描器的 `load_conversation` 在构造每条消息时顺手带上该条 JSONL 记录已经解析出的时间（`_entry_time(entry)`，扫描器本来就有，之前只用于会话级 `display_time`）。Claude 侧的历史遗留格式（`stop_reason is None` 的 `pending_legacy_answer`）要注意：这类答复要等下一条用户消息或文件末尾才会被 `flush_legacy_answer()` 真正 append 进 `messages`，时间戳必须在"设置 pending"那一刻就记下（`pending_legacy_ts`），不能用 flush 发生时的时间——否则同一条历史遗留答复会显示成很久之后才发的。`_preview_lines` 只在 `message.timestamp` 存在时才追加时间后缀（`format_message_time`，`%m-%d %H:%M`，和列表页时间格式共享同一个 helper），老格式解析不出时间的消息保持不标注，不强行伪造一个。
- **预览页实时刷新 + 会话缓存改按 mtime 失效**：`SessionStore.get_conversation` 原来按会话键**永久缓存**，历史文件被追加写入后同一次 `pickup` 进程内也不会重读（这也是"关闭预览重开还是旧内容"的根因），现在缓存值带上读取时的文件 `mtime`，命中时先比对当前 `mtime`，变了才重读。`_show_preview` 复用 `_run()` 已经设好的 `stdscr.timeout(200)`（`_show_preview` 是从 `_run` 的循环内调用的，同一个 `stdscr`，不需要重新设置），每约 5 次超时（~1s，和主列表页标题轮询同一节奏）检查一次 `get_conversation`；只有当前 `scroll` 已经等于上一次绘制算出的 `max_scroll`（即用户停在最底部）才把新内容自动滚到底部，否则保持原位不打扰阅读——`_draw_preview` 因此从只返回 `scroll` 改成返回 `(scroll, max_scroll)`，调用方靠 `max_scroll` 判断是否"贴底"。
- 预览页支持鼠标滚轮滚动（`sc._show_preview` 里 `curses.mousemask(...)`），**必须只在预览页这个作用域内开启，进入时开、退出（含所有提前 return 路径）时关**，用 `try/finally` 包裹整个按键循环保证一定会关闭。全局常开会导致主列表页/侧边栏也进入鼠标上报模式，终端原生的「鼠标拖拽选中文字」会失效，用户没法用鼠标复制会话路径/标题。滚轮事件走 `curses.KEY_MOUSE` + `curses.getmouse()` 的 `bstate` 位（`BUTTON4_PRESSED`=上滚、`BUTTON5_PRESSED`=下滚，用 `getattr` 兜底防止旧 curses 编译版本没有该常量），一格滚 `PREVIEW_MOUSE_SCROLL_LINES`（3）行；`mousemask`/`getmouse` 调用都要包 `try/except curses.error`，终端不支持鼠标上报时静默降级为纯键盘操作，不抛异常。实测发现不同终端协商的鼠标协议不一致（这台机器上 tmux 内协商的是 X10 经典格式，不是 SGR 扩展格式），两种格式 ncurses 都能正确解析成统一的 `bstate`，代码不需要关心具体是哪种协议。
- **鼠标上报开启期间，终端会把预览页内所有鼠标事件（含拖拽选中）都发给本程序，这是 xterm 鼠标协议的固有限制**——`curses.mousemask()` 只能控制程序自己订阅哪些事件类型，控制不了终端本身还要不要保留原生框选；一旦对某个窗格开了鼠标上报，那个窗格里鼠标拖拽选中文字就会失效，没有办法只订阅滚轮、放过点击拖拽。因此预览页额外提供 `m` 键在运行时切换 `mouse_enabled`（`_apply_preview_mousemask`），关闭后用户可以正常用鼠标框选复制头部的会话 ID 或正文，footer 提示文案随状态切换（`m 关闭鼠标滚轮` / `m 开启鼠标滚轮（当前可框选复制）`）；新增依赖鼠标状态的渲染逻辑要走 `_draw_preview` 的 `mouse_enabled` 参数，不要另开一条状态通道。
- **已修复（预览页 footer 在窄终端曾会崩溃）**：`_draw_preview` 底部快捷键提示行（`footer_y + 1` 那一行，屏幕最后一行）曾在 80/90/100 列终端下抛 `curses.error: addnwstr() returned ERR` 崩掉整个 TUI——`hint` 字符串真实显示宽度（`_text_width` 约 100 列）超过多数终端宽度，而 `addnstr` 的 `n` 参数按字符数而非显示列数截断，含中文的字符串写到最后一行边界时触发 ncurses 的"右下角"保护性异常（字符数达到 n 时，实际已写入的显示列数可能因宽字符超出 n，越界到真正的最后一列）。修法：改用 `_fit_cell(hint, width - 2)` 按**显示宽度**截断（留 2 列安全边界）再 `addnstr`，实际写入列数因此可控，永远不会碰到最后一列；等价于任何在屏幕最后一行写变长中英文混排文本的地方都应该用这个模式，不要直接把显示宽度当字符数传给 `addnstr` 的 `n`。用真实 pty（80/90/100/120 列各测一遍）复现过修复前的崩溃、验证过修复后不再崩溃，窄终端下超出的尾部提示（如 `n 新建`/`Space/q 关闭`）会被自然截掉，不影响前面更常用的按键提示。
- **弱化文字（分隔线/次要列/帮助文字）禁止用 `curses.COLOR_WHITE` 强制写死前景色，也不能只靠 `curses.A_DIM` 保证可读性。** 排查过一次用户在白色背景终端下反馈「弱化文字几乎看不清」的问题：老实现把 `PAIR_DIM`/`PAIR_TAB_INACTIVE` 的前景色写死成 `COLOR_WHITE` 再叠加 `A_DIM`，在深色终端上没问题，但在浅色/白色背景终端上等于「白底写白字再调暗」，对比度几乎为零。修法是在 `sc.py` 的 `_init_colors` 里判断 `curses.COLORS >= 256`：够 256 色时改用真正的中灰（256 色调色板第 244 号），不再叠加任何暗淡属性（灰色本身对比度已经够、再叠加会过淡）；退化到 8/16 色终端时才回退到「终端默认前景色（`use_default_colors()` 成功时的 `-1`）+ `A_DIM`」，靠终端自身配色跟随浅色/深色主题，而不是硬编码某个具体颜色。模块级 `DIM_EXTRA_ATTR` 由 `_init_colors` 按上述判断结果覆盖，所有需要弱化的文字必须用 `curses.color_pair(PAIR_DIM) | DIM_EXTRA_ATTR` 这个组合，不要在别处再手写字面量 `curses.A_DIM`。新增弱化用途的颜色对时按同样思路处理，不要复用裸 `COLOR_WHITE`。

### 项目侧边栏

- 侧边栏展示的「项目」= 所有来源（Claude + Codex + OpenCode）会话 `cwd` 归一化后的完整路径分组统计，`sc._project_groups` 按会话数倒序、同数按最近会话时间倒序排序；显示名只取末级目录名，同名末级目录冲突时由 `sc._disambiguate_labels` 逐级向上补父级路径直到唯一（VS Code 标签页风格）。**过滤永远按完整归一化 cwd 精确匹配（`sc._filter_sessions`），绝不按末级目录名字符串比较**——否则去歧义拆开的两个同名目录会互相污染彼此的会话列表。
- 宽度阈值：终端总宽 `< SIDEBAR_HIDE_THRESHOLD`（96 列）时 `sc._sidebar_width` 返回 0，整个侧边栏不绘制，界面完全退化为改动前的单栏布局；否则按最长项目名自适应，夹在 `SIDEBAR_MIN_WIDTH`(14) ~ `SIDEBAR_MAX_WIDTH`(26) 之间。宽度计算必须走 `_text_width`（中文全角占 2 列），不能用字符数。
- 焦点通过 `UIState.focus`（`"sidebar"` | `"list"`）驱动，横向序列固定是「侧边栏 → 来源1 → 来源2 → …」：`←` 在第一个来源上再按才会进侧边栏，`→` 从侧边栏出来固定回到第一个来源；侧边栏可见时左右端点**停住不回绕**（`Tab` 仍可循环切来源，不受影响）。焦点视觉规则：反白光标条只出现在真正持有焦点的窗格，另一侧的当前项降级为 `PAIR_TAB_ACTIVE|A_BOLD`（青字不反白），不要两边同时反白。
- 侧边栏内 `↑/↓` 是「移动即筛选」——不需要回车确认；`SessionStore.projects()` 是惰性缓存，唯一失效点在 `load()`，标题后台轮询（`poll_cache_updates`）不改变会话集，不会也不应该触发重算，否则违反首屏/每帧零 IO 的性能红线。
- 会话列表侧的所有取数入口统一收敛到 `sc._visible_sessions(store, ui, sidebar_visible)`，新增依赖当前列表内容的逻辑（预览、恢复、高级操作）应该消费它的返回值，不要绕过它直接读 `store.sessions[source]`，否则会漏过滤。
- 终端 resize 导致侧边栏被隐藏时，`ui.focus` 强制回 `"list"` 但 `ui.proj_idx` 保留、只是过滤旁路（`sidebar_visible=False` 时 `_visible_sessions` 直接返回未过滤列表）；拉宽后自动恢复原过滤状态，不需要用户重新选择项目。

### 会话级快捷键与预览页一致性（`_session_action`）

- 列表页和预览页（Space 打开）共用同一个分发点 `sc._session_action(ch, stdscr, store, ui, session, sidebar_visible)`，返回三态之一：一个启动请求（冒泡给调用方退出 TUI 执行）、`_ACTION_STAY`（按键已处理——弹窗被取消或 beep 拒绝，调用方留在当前视图重绘）、`_ACTION_PASS`（不是会话级动作键，调用方自行处理导航/滚动）。**新增会话级快捷键只需要在这一个函数里加分支**，列表页和预览页会自动同时生效，禁止在两处分别写一份重复的按键判断。
- `n`（新建空白会话）：工作目录由 `sc._new_session_cwd` 解析——侧边栏选中具体项目时用该项目路径，否则退回光标所在会话的 `cwd`；两者都拿不到，或解析出的目录在本机不存在（`usable_cwd` 校验），一律 `curses.beep()` 后停留，不弹窗、不报错。运行时选择：焦点在 Claude/Codex 标签页时直接用当前标签对应的运行时，不弹窗；焦点在侧边栏（只选中了项目、没有具体运行时上下文）时弹 `_pick_runtime_for_new_session` 菜单，默认高亮当前标签对应的运行时。预览页里 `ui.focus` 恒为 `"list"`（只能从列表进入预览），所以预览页按 `n` 永远直接用当前标签、不弹窗。
- `a`（跨运行时接力）在侧边栏焦点或预览页里没有具体会话上下文时（`session is None`）同样只是 beep，不弹菜单。
- 运行时选择弹窗底层复用同一个 `sc._pick_runtime(stdscr, store, title, action_for, default_index)`：`_choose_target_runtime`（接力）和 `_pick_runtime_for_new_session`（新建）只是传不同标题、不同每项说明文案和不同默认选中项的薄封装，不要再各写一份绘制+按键循环。

## 运行时边界

- 界面和接力编排禁止新增 `if source == "claude"` 这类运行时分支。
- 运行时私有扫描格式、恢复参数和新会话参数必须留在对应适配器中。
- 公共流程只依赖注册表和统一接力模型。

### 接手提示词的对话摘录（`Handoff.conversation_digest`）

- 摘录在 `BaseRuntime.export_handoff` 统一构建（调用 `self.load_conversation`，运行时无关），
  不在各适配器里各写一份；`render_prompt` 只负责渲染。为什么要有摘录：标题最长十几个字，
  作为任务说明极度有损；原始 JSONL 尾部常是工具结果/系统注入事件等噪音，冷启动的目标 agent
  首次解析容易定位错重点。`load_conversation` 已踩平真实格式坑（过滤系统事件、None 兜底），
  用它提取的摘录给目标 agent 一个可靠锚点。
- **原始历史文件仍是权威来源**：摘录在提示词里明确标注"截断版、以历史文件为准"，阅读指令
  改为"以摘录为线索核对补全"，不能让目标 agent 只信摘录不读文件。
- **摘录构建失败必须静默降级**：`load_conversation` 异常/为空时回退扫描层的
  `first_user_msg`/`last_user_msg`/`last_agent_msg`，再空则 digest 留空串、提示词退回无摘录
  形态——任何情况下不允许因摘录失败阻断接力。
- 摘录里的角色标签是"用户"/"助手"，**不能用"你"**——摘录是给接手的大模型看的，"你"会被
  它误解为指自己（用户明确纠正过）。消息压平成单行再截断（`_clip`），多行原文会破坏逐行结构。
- `pickup context` 的 `suggested_prompt` 与 TUI `a` 接力共用同一个 `render_prompt`，改摘录格式时
  两边同时生效，同步检查 `docs/SKILL.md` 的描述。

## 会话保活（keepalive.py）

- **定位**：运行时无关的启动包装层，地位类似 `titles.py`——不属于任何 `runtime/` 适配器，`registry.py` 只管生成 `LaunchPlan`，`keepalive.py` 负责在执行前后包一层 tmux。新增运行时不需要碰这个模块。
- **专用 socket 隔离**：全部操作走 `tmux -L pickup-keepalive`（独立 server），配套专属配置（`-f` 显式指定），完全不读、不写用户自己的 `~/.tmux.conf` 或默认 socket。目的是让保活会话“无感”（隐藏状态栏、真彩色、`window-size latest`），同时绝不影响用户手动开的 tmux 会话。
- **tmux 配置内容内联在 `keepalive.py` 的 `_TMUX_CONFIG` 字符串常量里，不是仓库里一个独立的 `.conf` 文件**——这是踩过坑之后改的：项目用 setuptools `py_modules`（扁平模块列表，不是 package）分发，只声明 `.py` 文件会被装进去，同目录放一个 `keepalive.tmux.conf` 完全不会随 `pip install`/Homebrew 安装进最终环境（实测 `pip install --target <dir> .` 之后目标目录里只有 `keepalive.py`，配置文件确实缺失）。改成 `_ensure_config_file()` 在每次 `wrap_plan` 时把内联字符串落盘到 `~/.cache/pickup/keepalive.tmux.conf`（内容变了才重写，同 `titles.py` 缓存目录），从根源避免"源码目录能跑、真实安装后启动就找不到 `-f` 文件"这类只有装包验证才能发现的问题。改配置内容只改 `_TMUX_CONFIG` 常量，不要再新建独立文件；改完最好实际走一次 `pip install --target <临时目录> .` 确认没有引入新的非 `.py` 依赖。
- **匹配保活会话不能只靠 tmux 会话名，必须走 pid 祖先链**：`wrap_plan` 生成的 tmux 会话名只在创建时用某个 `runtime_id + ident` 拼一次，之后原生恢复（如 `claude --resume`）可能在内部 fork/重新注册进程，导致 pane 里的顶层 pid（`#{pane_pid}`）不一定等于运行时自己事后记录的“活跃 pid”（如 `~/.claude/sessions/{pid}.json` 里的 pid）。`annotate()` 因此不比较 pid 是否相等，而是一次 `ps -eo pid,ppid` 建出整机父子关系表，对每个候选活跃 pid 向上追祖先链，只要能追到某个 tmux pane 顶层 pid 就算命中——对是否发生过 fork 免疫。`ps` 而非 `/proc`：项目要求同时支持 macOS/Linux（见 `README.md` Requirements），`/proc` 在 macOS 上不存在，`ps -eo pid,ppid` 两边通用。
- **`annotate()` 的调用点分散在三处，故意不做成单一收敛点**：`sc.py` 的 `SessionStore.load()`（TUI 列表）、`agent_api.py` 的 `cmd_list`/`cmd_search`（直接 `runtime.scan_sessions` 拼列表）、`resolve_ref`（`show`/`context`/`plan continue` 共用的会话定位）。三处各自扫描各自的会话集合，注册表层的 `scan_all()` 只被 TUI 用到，`agent_api.py` 走的是另一条按 runtime 单独扫描的路径，没有单一choke point；`annotate()` 本身只读（一次 `tmux list-sessions` + 一次 `ps`），开销可忽略（有活跃 pid 候选才会真的发子进程，见下一条），所以选择在每个"即将构建 session payload/渲染列表"的地方各调一次，而不是硬凑一个共享入口增加耦合。
- **`annotate()` 内部先判断有没有带 pid 的候选会话，再决定要不要真的发 `tmux`/`ps` 子进程**：完全空闲、没有任何 `live` 会话时（`pickup --json` 场景之外的多数命令行调用），这一步直接短路返回，不产生子进程开销；只有存在候选活跃 pid 时才值得为此打两次子进程。这个判断顺序（先查候选、后发子进程，不是反过来）是刻意的性能取舍，不要为了"代码更直觉"而颠倒。
- **`_launch` 里先无条件尝试 `attach_plan`，`keepalive_on` 开关只管要不要包装新启动的进程，不管是否要接回已有的**：如果某个历史会话已经被标注了 `keepalive_name`（意味着它当前正跑在某个 tmux pane 里），即使这次调用带了 `--no-keepalive`，也必须走 `attach-session` 接回去，不能假装没看见、重新拉起一个 `claude --resume` 去抢同一份会话文件——那会导致两个进程同时写同一个 JSONL，状态错乱。`--no-keepalive`/`SC_KEEPALIVE=0` 只影响"这次新启动的进程要不要被包进保活层"，对"识别到的已有保活会话该不该接回"没有否决权。改这段逻辑前想清楚这个区分，不要把两件事合并成一个开关。
- **回收（`reap_idle`）**：按 tmux 自己维护的 `#{session_activity}`（该会话最后一次有任何活动的时间戳）判断空闲时长，超过 `SC_KEEPALIVE_IDLE_HOURS`（默认 24，`0` 禁用）就 `kill-session`。不常驻额外的守护进程/定时器——`main()` 在进 TUI 前顺带跑一次，随 `pickup` 的启动节奏自然触发，足够覆盖"长期没人用 pickup 就不会占着内存"的诉求；会话历史本身在磁盘上，回收只是关掉后台进程，不丢数据。
- **无前缀脱离键 `Ctrl-\`**：`keepalive.tmux.conf` 里 `bind-key -n C-\\ detach-client`（`-n` 表示不需要 prefix 就能触发）。选它是因为 tmux 接管终端后处于 raw 模式，`Ctrl-\` 不会像普通终端那样触发本地 `SIGQUIT`；标准 `Ctrl-b d` 始终保留作为备用。新增/改绑定前确认没有和目标运行时 CLI 自身的快捷键冲突。
- **已知边缘案例**：`attach-session` 发起瞬间目标会话恰好自然退出（tmux 报错退出），`pickup` 不做特殊重试，用户重新打开一次 `pickup` 即可（这时该会话已经不再显示"后台运行中"，回车会走正常原生恢复路径）。
- **`pickup claude`/`pickup codex` 直启子命令是保活的第三个调用点**：`sc.py` 的 `_dispatch_direct_launch` 同样调 `keepalive.enabled()`/`keepalive.wrap_plan()`，和 TUI 的 `_launch()` 复用同一套开关语义（`--no-keepalive`、`SC_KEEPALIVE=0`）。直启没有"已有保活会话"这个概念（每次都是全新会话，`ident` 用 `keepalive.new_session_ident()` 现生成），所以不需要像 `_launch()` 那样先尝试 `attach_plan`。

## 直启子命令（`pickup claude` / `pickup codex` / `pickup opencode` / `pickup kimi`）

- **定位**：`main()` 里在 agent_api 分发分支之后、TUI 的 argparse 之前再加一个前置分支——`sys.argv[1]`（跳过可选的前置 `--no-keepalive`）命中 `registry.ids` 就整体转发给 `_dispatch_direct_launch`，不进入下面的 TUI/`--json` 参数体系。这个顺序刻意和 agent_api 的分发方式对称：两者都是"整个命令行属于另一套子系统，不该被 TUI 的 argparse 解析"。
- **为什么是纯透传 + 只垫危险参数，不像 TUI 里的 `build_resume_plan` 之类还塞了 codex 的 `-c model_reasoning_effort="high"`**：直启的诉求是"我知道我要传什么参数给底层 CLI，只是不想每次手打一长串跳过审批的危险参数"，属于用户对透传语义有明确预期的场景；额外静默塞入其它默认配置（哪怕是好意）会让用户没法确定"这次命令实际执行了什么"，所以 `registry.build_passthrough_plan` 只处理 `auto_approve_args`，不碰运行时的其它默认参数。
- **危险参数改成运行时类属性 `auto_approve_args`**（`runtime/base.py` 声明、`runtime/claude.py`/`runtime/codex.py` 各赋值一次），原本在每个适配器的 `build_resume_plan`/`build_continue_plan`/`build_new_plan`/`build_new_session_plan` 四处各写一遍字面量字符串，现在四处和直启共用同一份声明。新增运行时想接入直启子命令，只需要declare 这个类属性（不声明则默认空元组，直启不会额外加任何参数）。
- **`OpenCodeRuntime` 是这个模式下的一个刻意例外**：它的危险参数（`--dangerously-skip-permissions`）只在 `opencode run` 子命令下真实生效，裸命令（`pickup opencode` 直启透传的默认形态）带上会直接报错退出（实测确认，非猜测）。这个 flag 因此没有放进 `auto_approve_args`（该属性对 OpenCode 显式设为空元组），而是只硬编码在 `build_continue_plan` 内部——`pickup opencode`（裸直启）不会被自动垫上这个参数，这是有意为之，不是遗漏。详见「OpenCode 扫描」节最后一条。新增运行时如果也存在"危险参数只在特定子命令下有效"的情况，应参照这个处理方式，不要为了凑统一模式硬塞进 `auto_approve_args` 导致裸命令被打坏。
- **用户在透传参数里已经带了该运行时的危险参数时不重复添加**（`build_passthrough_plan` 用 `arg not in user_args` 过滤），这样 `pickup claude --dangerously-skip-permissions --resume xxx` 这类用户自己拼好完整参数的调用不会看到参数被加两遍。
- **`cwd` 恒为 `None`（不改变当前目录）**：直启是"就地拉起"语义，和 TUI 里恢复某个历史会话需要 `cd` 回原 `cwd`不是一回事，不要混用 `usable_cwd`。
- `_dispatch_direct_launch` 捕获 `execute_launch` 抛出的 `LaunchError`（如运行时未安装）打印错误信息并 `sys.exit(1)`，不让用户看到裸 Python 堆栈。

## 机器接口维护（agent_api.py）

- `list`/`search`/`show`/`context`/`plan continue`/`describe` 的 JSON envelope 结构（`{ok, data, error, meta}`）、
  退出码分配（0/1/2/3/5）和已发布字段名是对外契约，一旦发布过版本就按“只加不改不删”演进；
  确需破坏性变更时同步提升 `agent_api.AGENT_API_VERSION` 并在 `docs/SKILL.md` 标注。
- 新增子命令或参数只在 `agent_api.py` 的 `COMMANDS` 列表里加一份定义——`pickup describe` 的输出、
  `argparse` 的参数解析共用同一份数据，不要为 `describe` 另写一套文案，否则会和真实行为漂移。
- **`--compact` 精简字段集是「给人看」的默认值，不是「给机器控制逻辑」的默认值**：`list`/`show`
  单独传 `--compact` 时只返回 `DEFAULT_LIST_FIELDS`/`DEFAULT_SHOW_FIELDS`，两者都不含 `cwd`/`pid`。
  这曾在 OpenConductor 接入时造成真实故障：`internal/agentcontrol.SCClient` 只传了 `--compact`，
  拿到的每条会话 `cwd`/`pid` 恒为空——不是报错，是静默拿到零值，导致停止动作因缺 pid 直接判定
  「进程无效」、项目归属判断因缺 cwd 退化为「无归属，仅机器主人可见」，两者都不会在日志里报错，
  只会表现为功能悄悄不工作。`show` 因此在这次修复中补上了 `--fields`（此前只有 `list`/`search`
  支持）：任何需要 `cwd`/`pid` 等非默认字段的调用方，必须显式 `--fields id,runtime,cwd,pid,...`
  指名，`--compact` 只负责 JSON 排版（不缩进），不能假设它顺带给出全部字段。
- `title` 字段只读 `titles.load_cache()`，不得在 `agent_api.py` 里触发 `refresh_titles`；机器接口
  不消耗 Claude 额度是硬约束，触发生成的入口只能是 `_spawn_title_daemon` 拉起的后台进程。
- **续接计划仍是只读数据**：`pickup plan continue <runtime:id> --instruction <文本>` 只验证目标会话、
  读取 runtime 能力并返回统一 envelope 中的会话事实、能力列表与执行计划；它不得启动进程、发送信号、
  写入历史或改变终端。真正执行计划的是调用方（例如 OpenConductor），不是 pickup。
- **执行计划禁止 Shell 拼接**：计划必须以 `argv` 数组与 `cwd` 表达，调用方使用无 Shell 的进程启动
  API 逐项传入参数；不要返回或消费可交给 `sh -c`、`eval` 等解释的命令字符串。这样含空格、引号或
  用户需求文本的参数不会被二次解释，也不会把只读计划接口变成命令注入入口。
- **第三方 runtime 的续接扩展点**：新增 runtime 时，在 adapter 实现 `build_continue_plan`，由它把已
  扫描到的原生会话转换为统一的 `argv`/`cwd` 计划；不在 `agent_api.py` 添加按 runtime 分支。不能原生
  续接的 runtime 应明确返回不可续接能力，而不是伪造计划；实时下发指令同样不属于此扩展点。
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
- `sc.py`（`pickup` 入口）的非 TTY 自动降级（`sys.stdin.isatty() and sys.stdout.isatty()`）和 `list/search/show/
  context/describe` 子命令分发写在 `main()` 顶部，早于旧版 `--json`/`--limit` 的 legacy parser；
  改 `main()` 时不要把两条路径的参数解析合并到同一个 `argparse.ArgumentParser`，legacy 路径的报错
  仍是给人看的文本，机器接口路径的报错必须是 JSON envelope，混用会破坏其中一边的调用方假设。
- **`AgentApiTests` 里给 `store_true` 参数写测试的坑**：`_registry`/各测试方法构造 `args` 大多是裸
  `mock.Mock(...)`，只显式传了用到的关键字；访问没传的属性时 Mock 会自动生成一个新的、truthy 的
  Mock 实例，不会抛 `AttributeError`，也不会落回 `getattr(args, name, default)` 的 `default`。
  所以 `cmd_list`/`cmd_search` 里判断 `--live` 用的是 `getattr(args, "live", None) is True`，不是
  常见的 truthy 写法——用 `is True` 才能让"老测试没传 `live` 参数"正确落到"未开启"，而不是被
  自动生成的 Mock 误判成"已开启，过滤到只剩 live 会话"。新增任何 `store_true` 参数、且要在
  `cmd_*` 里按它做条件分支时，同样用 `is True` 这个模式；纯读值转发（如 `compact`/`out`）目前
  所有测试都显式传了值，暂时安全，但新写测试时也建议养成显式传 `live=False` 等布尔关键字的习惯，
  别依赖 Mock 的默认行为。
- **`live`/`pid` 从扫描层到接口的传递**：`scan_claude._live_session_ids()`/`scan_codex._live_session_ids()`
  返回 `{会话ID: pid}` 字典（不是纯 `set`），`scan_sessions` 里 `info["live"] = info["id"] in live_ids`
  之后紧跟 `info["pid"] = live_ids.get(info["id"])`；两个运行时判活时手上本来就有 pid（Claude 是
  `~/.claude/sessions/{pid}.json` 的文件名，Codex 是 `pgrep -x codex` 循环里的 pid），顺手带出，
  不需要额外系统调用。`agent_api.session_payload` 直接透传这两个字段，不做二次判活。
- **面向管家 Agent 的可见性 vs 只读边界**：为了让管家 Agent 能回答"现在哪个 CodingAgent 在跑"，
  `list`/`search`/`context` 暴露了 `live`（进程真实存活）、`pid`（配合 `live` 使用）。这只是
  **暴露可见性**，不是新增执行能力——`agent_api.py` 仍然是纯只读接口，不提供"向运行中进程发送
  指令/接管会话"的命令；管家拿到 `pid` 之后想做什么是调用方自己的事，pickup 不代劳，也不应该代劳
  （只读边界详见文件头注释和 `AGENTS.md`）。
- **`list`/`search` 默认带摘要**：`DEFAULT_LIST_FIELDS`/`DEFAULT_SEARCH_FIELDS` 默认含 `last_user`/
  `last_agent`（`session_payload` 里用 `_trim()` 硬截断到 `_SUMMARY_TRIM_LEN`，约 120 字），让管家
  一眼看懂"这条会话在聊什么"，不必为每条候选都多一次 `pickup show` 往返。这两个字段本来就是扫描阶段
  已经提取好的 `last_user_msg`/`last_agent_msg`（`search` 的 haystack 早就在用），只是之前没有
  暴露给 Agent 接口；`pid` 因为多数场景是 `null`、只在 `live=true` 时有值，没有进 `--compact` 的
  精简默认集，避免精简模式反而字段膨胀。

## 会话管理与检索的 Agent 可用性设计取舍

给管家 Agent（OpenConductor）设计 `pickup` 的可用性时，明确讨论过并否决了两个方向，记录下来避免以后
重复纠结：

- **不做"下发指令给运行中会话"的执行命令**：管家想"指挥某个正在运行的 CodingAgent"，最直接的实现
  是 pickup 直接往目标进程注入输入或调用其 API。这会打破"`agent_api.py` 只读、无副作用"的硬架构约束，
  让 pickup 从数据接口变成执行器，责任边界和风险都会显著上升。最终选择只增可见性（`live`/`pid`），
  接管逻辑留给管家自己基于这些数据去实现，pickup 不跨这条线。
- **检索不引入语义/向量搜索**：现有关键词子串匹配（标题权重最高，其次首尾消息、目录）已经够用，
  上语义搜索要引入嵌入依赖、离线索引维护和额外算力/额度成本，与 pickup"轻量、零依赖、离线可用"的
  定位冲突，暂不做。

## 开源发布

- GitHub 公开仓库是 `https://github.com/x0c/pickup`，本地远端名为 `github`；原 `origin` 仍指向内部 Forgejo，用于同步备份。
- 项目历史版本线已经到 `v0.2.x`，新增公开发布版本必须沿现有标签递增，不能从 `0.1.0` 重新开始。
- 打包元数据同时维护 `pyproject.toml` 和 `setup.cfg`。当前环境的旧版 setuptools 会把只写在 `pyproject.toml` 的包名解析成 `UNKNOWN-0.0.0`，所以改版本号、入口、描述或项目链接时要同步两处。
- 发布前至少构建一次 wheel，确认产物名形如 `pickup-<version>-py3-none-any.whl`，并用临时目录安装后检查 `pickup = sc:main` 入口元数据。
- 开源前隐私扫描要覆盖准备提交的文件和完整 Git 历史补丁内容；本机 `.git/config` 里的内部远端不进入仓库内容，但真实文件、历史提交、Release 说明和 README 不能包含密钥、个人路径、内网地址或占位符。
- GitHub Release 发布后检查 Actions、Release、topics 和仓库可见性；当前仓库 topics 为 `claude-code`、`codex-cli`、`terminal`、`tui`、`session-manager`、`ai-coding-agent`。

### 一键安装渠道

- Homebrew 配方在独立仓库 `x0c/homebrew-tap` 的 `Formula/pickup.rb`，本项目不维护该文件的本地副本；`Aliases/session-continue` 软链到 `pickup`，兼容改名前的 `brew install/upgrade x0c/tap/session-continue`。配方用 `Language::Python::Virtualenv` 直接从 GitHub 源码 tag 归档安装（项目零运行时依赖，不需要 PyPI）。
- 新打 `v*` 标签并推送后，`.github/workflows/release.yml` 自动下载新 tag 归档、算 sha256、直接提交到 `x0c/homebrew-tap` 的 `main` 分支——不用手改配方文件里的版本号和哈希。也可以在 GitHub Actions 页面手动 `workflow_dispatch` 并填 tag 名重跑（比如某次自动触发失败后需要补跑）。
- 该步骤需要仓库 secret `HOMEBREW_TAP_TOKEN`：一个对 `x0c/homebrew-tap` 有 contents write 权限的 fine-grained PAT（不要复用本机 `gh auth` 的个人会话 token，那个权限范围过宽且和 CI 生命周期不一致）。token 过期或权限变更会导致这一步失败，发新版本后应看一眼 Actions 页面确认 bump 任务成功。
- 实现是纯 `curl + git`（见 `release.yml`），不依赖第三方 Action：`mislav/bump-homebrew-formula-action` 对 `HEAD /repos/{owner}/{repo}/tarball/{ref}` 的重定向只认严格等于 302，但 GitHub 自 2026-05-16 起对该端点的 HEAD 请求改答 303，导致该 Action 必现 `unexpected HTTP 303 response`（上游 issue mislav/bump-homebrew-formula-action#340，修复 PR #342 长期未合并）。改回用该 Action 前，先确认上游是否已发布修复版本。
- 不用 Homebrew 的用户走 `install.sh`（托管在本仓库 `main` 分支，通过 `curl -fsSL .../install.sh | bash` 执行）：校验 Python 版本、查询最新 Release 的 tag、`pip install --user` 安装、按需提示把安装目录加入 `PATH`。改这个脚本后必须实际执行一遍（可用 `PYTHONUSERBASE` 重定向到临时目录，避免污染真实用户环境），不能只过静态检查。
- **`install.sh` 依赖 GitHub Release 对象（`GET /repos/{owner}/{repo}/releases/latest`），不是纯靠 tag。** `release.yml` 从来只负责 Homebrew 配方同步，从未有过创建 Release 的步骤——早期版本（`v0.2.x` ~ `v0.11.1`）的 Release 是每次发布时顺手手动 `gh release create` 出来的，v0.13.0 发布前有一段时间这一步被漏掉，导致 `releases/latest` 停留在 `v0.11.1` 不再更新：`install.sh` 会静默装出落后好几个版本的旧代码（不报错，只是版本不对），比 Homebrew 配方的哈希校验更容易被漏查。发布新 tag 时必须同时跑 `gh release create <tag> --title <tag> --notes <说明>`（或等价的 Release 创建动作），发布收尾检查清单里要加一条「`curl -fsSL https://api.github.com/repos/x0c/pickup/releases/latest` 返回的 `tag_name` 等于刚发的版本」，不能只看 Homebrew Actions 是否绿。
- 在 suzhou 上验证 `install.sh` 时，`pip install git+https://github.com/x0c/pickup.git@<tag>` 可能卡在 GitHub clone 并超时（2026-07-06 实测约 130 秒后 `Failed to connect to github.com port 443`）。这属于该节点直连 GitHub 出口不稳定，不等于安装脚本或 tag 有问题；先原样重试，仍失败时换到 GitHub 出口稳定的环境验证，并在发布记录里明确写出阻塞输出。
- 三条安装路径（Homebrew、一键脚本、源码安装）在 `README.md` 里必须保持同步；新增或调整任一路径都要回头检查其余两条描述是否还准确。

## 真实路径验证

改标题、排序或列宽后，除编译和单测外，还要做真实路径验证：

```bash
python3 -m py_compile sc.py scan_claude.py scan_codex.py scan_opencode.py scan_kimi.py titles.py titlegen.py models.py agent_api.py keepalive.py runtime/*.py test_*.py
python3 -m unittest -v
```

然后用真实会话列表检查前 120 条没有 raw slug、纯命令、省略号或自产标题 prompt；再用真实终端启动一次 TUI 并退出，确认本机 `pickup` 入口指向当前代码。改动会话扫描/预览逻辑时（不限于 Claude/Codex，OpenCode、Kimi 同样适用），至少随机抽查 5 条真实会话跑一遍 `scan_sessions`/`load_conversation`，断言没有空文本、字面量 `"None"`、角色标错或时间戳非单调，不能只信手写的单测小样例。
