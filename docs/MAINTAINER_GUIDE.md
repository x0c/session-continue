# pickup 维护指南

## 标题与排序

- 最近会话排序优先使用历史文件更新时间。用户对“最近”的直觉是最近被续接或写入，而不是文件内部最后一条可解析消息。
- 文件时间不是绝对可信，且污染粒度可以细到单个文件，不一定成批出现：Claude Code 在会话驻留/被重新打开时会追加没有时间戳的元数据条目（`last-prompt`、`ai-title`、`mode`、`permission-mode`），把文件 mtime 顶到“现在”而不产生任何新对话内容；Syncthing、复制、批量元数据刷新是同一类问题的批量版本。修正逻辑统一收在 `models.py` 的 `effective_session_time(file_mtime, event_time)`：当 mtime 比会话内部最后一条真实事件新出 1 小时以上的 gap，就判定 mtime 不可信，逐会话回退到 event_time；两个扫描器的 `_build_session_info` 都在返回结果前调用它写回 `mtime`/`display_time`/`time_source`。曾经按“同一分钟桶 ≥5 个会话”识别批量污染簇的启发式已废弃——它只覆盖批量场景，漏过了本节描述的单文件被驻留进程 touch 的情形（真实故障：两个会话被 touch 到不同分钟，各自没能凑够聚簇阈值，在列表里显示成"20分钟前"，实际是 9-11 天前的会话）。
- Claude Code 自带 `aiTitle` 不稳定，只能作为临时兜底的最后来源，不能绕过生成缓存直接展示。
- 无缓存时必须先生成本地短标题，再交给后台模型优化。首屏不能依赖后台生成器（`claude`/`codex` 无头调用）是否及时返回。
- 后台标题生成可能留下新的 Claude 会话记录；扫描侧必须过滤自产标题 prompt 和只有低价值消息的记录，避免历史污染反过来进入列表。
- 标题生成以 5 条会话为一批。第一批先**串行**探测候选生成器：首选失败、超时或返回无有效标题时才试备用，避免坏首选在并发 worker 启动后重复消耗 5 次；选出健康生成器后，其余批次最多 5 路并发。每次后端调用仍以 90 秒为上限，当前首选 + 备用两级在第一批全部失败时最坏约 180 秒，但只发生一次串行健康探测；后续不会再探测已失败候选。每批完成立即原子写缓存，界面可陆续显示结果。生成出的有效标题一旦写入缓存即为该会话的固定标题，后续对话内容增长不能让它再次排队。会话只有“在吗”等无任务信息时保留本地标题且不调用模型。
- **标题生成的失败也是必须落盘的终态，不是“下次启动再试”**：调用失败、超时、不可解析/低价值/机器 slug、批量结果部分缺项，以及本机没有任何可用生成器时，都给受影响会话写入当前 `TITLE_CACHE_VERSION` 的 `generation_state=failed`，保留本地兜底并立即让 TUI 停止 spinner。同一缓存版本后续启动不再自动提交模型，避免永久转圈和反复花额度；提升缓存版本后失败标记自然失效，才允许按新规则重新尝试。成功、失败和部分缺项都要逐批 `save_cache`，不能只保存成功项，否则缺项会永远重新排队。
- 标题生成后端已抽象为 `titlegen.py` 的 `TitleGenerator`（当前有 claude、codex 两个实现）。`titles.py` 只负责批量 prompt、JSON 解析和缓存，不感知具体 CLI；新增生成器只在 `titlegen.py` 加实现并注册进 `_GENERATORS`，禁止在 `titles.py` 里写 `subprocess` 调用。选择顺序：`PICKUP_TITLE_GENERATOR` 环境变量（旧名 `SC_TITLE_GENERATOR` 仍生效）→ 按注册顺序取已安装候选；环境变量只决定首选，第一批探测失败才降级到下一个候选。`PICKUP_TITLE_MODEL`（旧名 `SC_TITLE_MODEL`）覆盖模型。缓存与生成器无关，换生成器不重算已有成功标题，也不绕过当前缓存版本的失败终态。
- 自产噪音会话的过滤，每个可能被生成器落盘的运行时扫描器都要有：Claude 侧靠 `PROMPT_MARKER` 预探过滤（见「扫描性能」）；Codex 侧生成用 `codex exec --ephemeral` 不落盘，扫描过滤仅是兜底。接入没有 ephemeral 类开关的 CLI 后端（如 opencode run，每次调用必然真实落一条会话）时，对应扫描器的 `PROMPT_MARKER` 过滤是必需项，漏掉会让标题生成会话刷屏列表。
- **`titles.save_cache` 是原子写（临时文件 + `os.replace`），不是直接覆写**：后台标题生成进程逐批写、TUI 每约 1 秒轮询读同一份 `titles.json`；直接 `open(..., "w")` 覆写会被并发读到半截 JSON（`load_cache` 解析失败静默退回 `{}`，界面标题短暂回退临时兜底、转圈圈重置）。改这个函数前确认没有退回裸覆写。

## 标题生成进程

- 标题生成必须由脱离当前终端的独立进程承载，不能放回 TUI 进程内线程。
- `execute_launch` 会用 `os.execvp` 替换当前进程；按 `Esc` 退出也会结束 TUI 进程。TUI 内线程会在这些路径上丢失未完成标题。
- 当前模型是：`_spawn_title_daemon` 拉起 `pickup --generate-titles` 后台进程，后台进程用缓存目录下的文件锁保证全机单实例；TUI 侧只读缓存并轮询缓存文件变化。
- 后台进程内的候选生成器选择发生在 `refresh_titles`；本机一个 agent CLI 都没有时保留临时兜底标题，并把本批会话写成当前缓存版本的失败终态，不能静默返回后让 TUI 永久转圈、下次启动再次排队。不要在 TUI 首屏路径做可用性探测。

## 跨扫描器共享 helper（scan_common.py）

五个扫描器（scan_claude.py/scan_codex.py/scan_opencode.py/scan_kimi.py/scan_cursor.py）互不
依赖运行时私有格式，但都需要几个完全相同的小工具：`shorten_cwd`（路径展示时
把用户主目录替换成 `~`）、`parse_timestamp`（ISO8601 字符串转 epoch 秒）、
`live_processes` / `live_pids_by_process_name`（存活同名进程及其 cwd）、
`process_command_line` / `process_environ` / `open_file_paths`（读命令行、环境变量、
打开的文件路径，供 Cursor 等按正向证据精确绑会话）。这些集中在 `scan_common.py`，
避免多份重复实现各自演进出细微差异；新增运行时扫描器时优先检查这里有没有能复用的
helper，不要先照抄再改。这个模块只放无状态纯函数，运行时私有的解析格式
（JSONL 字段、SQLite 表结构等）仍留在各自的 scan_*.py 里。

## Claude 扫描

- Claude 的 `aiTitle` 不一定在第一条用户消息之前出现，扫描头部不能拿到工作目录和首条用户消息后立即停止。
- Claude 的 `/plan` 等本地命令会把真实需求放在 `<command-args>...</command-args>` 中，这类内容必须提取为用户意图。
- Claude 的兜底标题必须走候选评分。短续接词、催促、系统提示、错误消息和自产标题 prompt 都是低价值候选；用户侧全是低价值消息时，才允许用最后助手摘要兜底。
- Claude 侧通过 tail 消息里的 `[Request interrupted by user]` 精确字符串识别中断，不要用宽松关键词匹配。
- `titles.py` 的 `_compact_title` 里正则使用原始字符串。写 `\s`、`\S`、`\w`、`\d`、`\n` 时不要多打一层反斜杠；改这个函数前先用真实会话文本验证输出是完整可读片段。
- **`load_conversation`（会话预览的正文来源）不能按 `stop_reason` 过滤要不要展示某条 assistant 文本。** 真实 JSONL 里一次 assistant 轮次的 `thinking`/`text`/`tool_use` 各是独立的顶层行，且共享同一个 `stop_reason`——哪怕这一行本身就是纯文本、只是后面紧跟着一次工具调用，它的 `stop_reason` 也是 `tool_use` 而不是 `end_turn`。之前的实现按 `stop_reason in (None, "end_turn")` 过滤 assistant 行，实测在一个 97 条真实消息的会话里把 88 条有真实文本内容的 assistant 消息整段丢了（包括工具调用前的说明、`AskUserQuestion` 之前的分析文字等），预览页看起来像是"Claude 跳过了回答"，其实是解析漏了。现在只要 `content` 里有非空 `text` block 就展示，不再看 `stop_reason`；`stop_reason is None` 分支保留给可能存在的历史遗留流式格式（增量快照，只在 flush 前取最后一份），和当前主流格式互不冲突。改这块逻辑前先用真实会话文件跑一遍 `load_conversation` 数消息条数，不要只信单测里手写的小样例。
- **Monitor/task-notification 等系统注入事件在原始 JSONL 里也挂在 `type: "user"` 轮次下，`load_conversation` 必须整条丢弃，不能当成真人输入。** 区分信号是顶层 `origin.kind` 字段：真人手动输入是 `"human"`（或字段缺失，如老格式/`/plan` 等本地命令包装出的用户轮次），系统事件（Monitor 到点触发、task 通知）是 `"task-notification"`。消息历史只保留 Agent 和真人的对话，系统事件价值很低——最初的实现把它标成 `ConversationMessage("system", ...)` 单独渲染成"◇ 系统事件"展示出来，被用户否决（"什么系统消息都不要显示出来"），改成命中 `origin.kind not in (None, "human")` 就直接 `continue` 跳过，不进入返回结果。`ConversationMessage.role` 因此保持只有 `user`/`assistant` 两种，不要再引入第三种 role。新增其它 `origin.kind` 取值（目前本机全量历史只出现过 `human`/`task-notification` 两种）时按同一分类原则处理，不要默认归到 `user`。
- **content part 的 `text` 字段、`snapshot`/`payload` 这类嵌套对象字段，同样可能是 JSON `null`（key 存在但值为 null），不是只有 Codex 才有这个坑。** `_extract_text`、`_entry_time` 的 `snapshot` 取值、`_build_session_info` 尾部循环取 assistant 文本、`load_conversation` 的 `text_parts` 列表推导式，都曾用裸 `part.get("text", "")`/`entry.get("snapshot", {})` 取值——这类写法的默认值只在 key **缺失**时生效，key 存在但值为 `null` 时会拿到 `None`，后续 `.strip()`/`.get(...)` 直接 `AttributeError` 崩掉整个扫描或预览（真实场景可复现，不是假设）。统一改成 `(x.get(key) or 默认值)` 的写法；新增任何从 Claude JSONL 嵌套字段取值的代码都要假设该字段可能是显式 `null`，不能只防"key 缺失"这一种情况。
- **Codex 的 `event_msg.payload` 里字段值可能是 JSON `null`（key 存在但值为 null），`payload.get(key, "")` 的默认值只在 key 缺失时生效，取不到 null 场景，会拿到 `None` 再被 `str()` 变成字面量 `"None"` 混进正文。** 实测 `task_complete.last_agent_message` 为 null 很常见（任务结束但没有最终文本输出，比如被打断/答案已在更早轮次说完），预览页因此显示过多轮" ◆ Codex\nNone"。三处取值（`user_message.message`、`agent_message.message`、`task_complete.last_agent_message`）统一改成 `payload.get(key) or ""`，`or` 会把 `None` 也兜成空字符串再被后续的 `if text:` 过滤掉。改 `scan_codex.py` 任何从 payload 取文本的地方都要用这个写法，不要用 `.get(key, "")`。

## Codex 扫描

- **Codex 自身的多智能体（swarm/subagent）任务会把每个子代理线程各自写成一份独立的 `rollout-*.jsonl` 文件，扫描时必须过滤掉，不能当作用户发起的顶层会话列出。** 真实故障：一个真实会话（`session_meta.payload.thread_source` 缺失或为 `"user"`）执行多智能体任务时，会派生出好几个子代理线程（`thread_source: "subagent"`，`forked_from_id`/`parent_thread_id` 指向父会话，`agent_nickname` 是 Codex 随机取的代号），这些子代理线程 fork 时继承了父会话开头的历史，因此它们文件里"第一条用户消息"（也就是列表兜底标题的来源）和父会话完全相同。`_find_all_session_files` 对 `~/.codex/sessions/` 下所有 `.jsonl` 一视同仁地扫描，没读 `thread_source` 字段时，这些子代理文件会和父会话一起出现在列表里，表现为同一段任务描述反复出现好几条、目录和时间都很接近，用户会误以为是"同一个会话被重复列出"的 bug，实际是把 Codex 内部子任务线程误当成了独立会话。修法是在 `_build_session_info` 读 `session_meta` 时顺带取 `payload.get("thread_source")`，`scan_sessions` 里 `thread_source == "subagent"` 直接 `continue` 跳过，和过滤空会话、死 cwd 会话放在同一批前置检查里。
- **判活（`_live_session_ids`）曾对每个存活 codex 进程各发一次 `lsof -p`，是首屏超过 1s 硬指标的真实根因**：本机实测单次 `lsof -p <pid>` 耗时约 500ms，2 个 codex 进程就吃掉近 1 秒，进程越多越慢。改为按平台分流：Linux 直接遍历 `/proc/<pid>/fd` 逐个 `os.readlink` 找 `rollout-` 文件（近乎零成本，不 fork 子进程）；macOS 等无 `/proc` 的平台改为一次合并调用 `lsof -n -P -Fpn -p <pid1>,<pid2>,...` 覆盖全部候选 pid（`-n -P` 跳过 DNS/端口名解析——这是原实现单次 `lsof -p` 慢的另一诱因；`-Fpn` 只输出 `p<pid>`/`n<name>` 两类字段行，逐行按 `p` 切换当前 pid、遇到含 `rollout-` 的 `n` 行才抽 UUID）。两条路径都不判断 fd 是读还是写模式，与改动前 lsof 实现的实际行为一致（旧实现同样没有过滤 `w` 模式，只要 `rollout-` 出现在 lsof 输出里就算命中）。

## OpenCode 扫描

- **历史存储是 SQLite（`~/.local/share/opencode/opencode.db`），不是 JSONL 文件**：`session`/`message`/`part` 三张表，正文只在 `part.data` 的 JSON `text` 字段（`message.data` 只有角色/时间/finish 等元数据）。OpenCode v1.2.0 起才是这个格式，更早版本的纯 JSON 文件存储**不做兼容**——官方升级会自动迁移到 SQLite，遗留在老格式的用户极少；本机没有 `opencode.db` 时这个运行时的会话列表就是空的，不报错、不尝试读旧格式。
- **只读连接，WAL 库可能拒绝只读打开**：`_connect_ro` 用 `sqlite3.connect("file:<path>?mode=ro", uri=True, timeout=0.5)`。opencode 正在写库（WAL 模式）时，极端情况下（需要 checkpoint 恢复且无活跃写者）只读打开可能失败；单个数据库连接/查询失败时继续扫描其它数据目录，至少一个数据库成功（即使确实查到 0 条）就正常返回。若已发现数据库路径但全部连接/查询失败，必须抛中文 `RuntimeError` 给 registry 保留上一次成功结果，不能把瞬时故障伪装成“所有会话被删除”；本机确实没有数据库路径时仍正常返回空列表。`timeout=0.5` 把单库 busy 等待封顶在 0.5s。
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

- **首屏延迟目标 ≤1s（已放宽为非阻断，见 `AGENTS.md`「验证要求」）。** 当前路径：`main()` 把 `store.load()`（→ `registry.scan_all()`）丢进后台 daemon 线程，同时跑 `_probe_osc_colours()`，再 `run_app()` 先画出骨架（空列表 +「＋ 新建会话」）；`MainScreen` 用 `@work` 等 `store.wait_loaded()` 后再 `rebuild`。扫描没跑完时页头不得误报「未找到任何会话」——必须等 `store.loaded`。直启/`_dispatch_direct_launch` 等仍可同步预加载后再进 UI。`StartupLatencyTests` 测的是 `scan_all(50)` 本身耗时，不是「进程启动到首帧」墙钟。
- `scan_claude.py`/`scan_codex.py` 的 `scan_sessions()` 接受 `limit`，但早期实现会先对全部历史会话文件做完整的头尾 JSONL 解析，再 `results[:limit]` 截断——不管 `limit` 多小都要扫完全部历史（本机曾实测 796+1074 个文件耗时 ~5s，是 `pickup` 启动慢的根因）。现在改为先用 `os.stat` 按真实文件 mtime 把候选文件排好序，凑够 `limit` 条有效结果就停止；新增或改写这两个扫描函数时不要退回“先建全量列表再截断”的写法。因为时间修正已经收敛成单会话 `effective_session_time` 判断（见「标题与排序」），提前停止不再需要按分钟桶粒度对齐。
- Codex 侧提前停止前必须先按真实文件 mtime（`os.path.getmtime`）重新排序，不能直接用 `_find_all_session_files` 现成的按文件名（创建时间）排序去做提前停止——同一会话被续接时 mtime 会变但文件名不变，按创建时间提前停会漏掉“很久以前创建、但刚被续接”的会话。
- `runtime/registry.py` 的 `scan_all()` 用 `ThreadPoolExecutor` 并发跑各运行时的扫描：各运行时读的是完全独立的目录、无共享状态，线程池只是为了重叠磁盘 I/O 等待。实现了 `scan_signature()` 的运行时可复用上一次扫描；目前只有 OpenCode 接入，签名必须同时包含数据库/`-wal` mtime 和排序后的存活进程 cwd→pid 快照——只看文件时间会在进程退出但数据库不再写入时把“运行中”永久冻住，探活恢复也无法触发重扫。Claude/Codex 的多层历史目录不满足可靠 mtime 冒泡条件，继续返回 `None`，不要为追求命中率强接缓存。
- **registry 缓存与失败回退的可变对象边界**：缓存保存、命中返回和旧结果回退都必须逐条 `dict(session)`，因为 `SessionStore`/`keepalive.annotate()` 会就地注入 `keepalive_name` 等展示字段；直接返回缓存对象会让调用方反向污染下一轮扫描。带新签名的 `scan_sessions` 失败时不得用空列表或新签名覆盖最后一次成功缓存：已有缓存返回其副本，首次失败才降级为空，并在同一签名恢复后重新扫描。未实现签名的运行时仍保持单次异常隔离为空。`agent_api.py` 的 `_scan_runtimes` 是独立扫描路径，不复用 TUI registry 缓存；改通用异常隔离语义时仍要检查两处是否需要同步。
- **cwd 判活必须按 cwd 记忆化，不能逐会话裸调 `os.path.isdir`。** 排查过一次首屏 >1.3s 的问题：`scan_sessions` 循环里对每条候选会话的 `cwd` 都单独调用 `os.path.isdir` 判断目录是否还在（用于过滤已删除工作目录、无法 resume 的会话），但实测本机一次扫描里几百条候选会话经常只对应十几个不同的 cwd（同一项目下反复续接）；这些 cwd 常年落在 Syncthing/网络同步目录上，单次 `isdir` 实测 ~5-10ms，去重前光这一项就吃掉 profile 里 0.6s+ 的裸开销。修法是在 `scan_sessions` 内建一个按 cwd 缓存结果的 `isdir` 闭包（单次扫描内 cwd 存在性稳定，用完即弃），两个扫描函数都要保留这个闭包，不要退回裸调用。
- **Claude 侧完整解析前必须先用廉价预探（`_peek_head_meta`）拦掉自产噪音会话和死 cwd 会话，不能等整文件解析完才丢弃。** 后台标题生成会调用 `claude`/`codex`，在 `~/.claude/projects/` 留下以 `titles.PROMPT_MARKER` 开头的噪音会话；这类会话和 cwd 已删的会话本来就会在解析后被过滤，但过滤发生在读完 300 行头部 + 64KB 尾部之后，白白解析。实测本机为凑够 30 条有效结果，`_build_session_info` 曾被调 347 次，其中一大半是最终会被丢弃的噪音/死 cwd 会话。`_peek_head_meta` 只读头部 ≤40 行拿 cwd 和首条用户消息，探到确定是噪音或死 cwd 才提前 `continue`；探不到（如头部很长的真实会话）时不跳过，照常走完整解析兜底——改动前后结果必须字节级一致（id 顺序、`fallback_title`、`native_title` 全部相同），新增类似优化时也要用这个标准核验。Codex 侧噪音少，只做了 cwd 记忆化，没加预探，保持简单。
- 上述两项优化落地后，本机实测 `limit=30` 时 Claude 扫描从 1320ms 降到 225ms、Codex 从 585ms 降到 243ms，`scan_all` 并发后首屏约 0.25s；`limit=50`（默认值）约 0.6s，仍在 1s 硬指标内。
- 评估过给单文件解析结果加磁盘缓存（按 `路径+mtime+size` 做 key）的方案，判断当前收益不足以覆盖风险，暂缓：cwd 记忆化 + 预探已经把首屏压到 1s 硬指标以内，缓存要处理和后台标题生成进程的并发写、文件被删除/截断重写后的失效判断，复杂度换来的收益不划算。若历史继续增长导致启动明显变慢（>1s）或用户明确要求，再按 `titles.py` 已有的原子写 + 内容指纹失效模式加缓存。
- **本机历史数据量增长后，`scan_claude.py` 的头部解析（`_read_head`，最多 300 行）重新成为首屏耗时大头**：实测本机真实会话文件里，前 300 行经常混入几百 KB 甚至上 MB 的 `assistant`/工具调用大段嵌入内容（大文件读取、工具结果），逐行 `json.loads()` 解析这些巨大行很贵；同时后台标题生成积累的自产噪音会话（`PROMPT_MARKER` 前缀）在候选文件中占比可能相当高（本机实测一次凑够 50 条有效结果需要探测 130+ 候选，其中过半是噪音），叠加多个真实 codex/claude 进程并发争抢磁盘和 CPU 时，`scan_all` 有概率短暂超过 1s。**曾尝试给 `_read_head` 加字节预算提前停止（仿 `_read_tail` 的 `max_bytes`），用本机全部 1263 条真实会话验证后发现 196 条（约 15.6%）`fallback_title` 结果改变，且抽查显示新结果通常比原结果质量更差（挑到的是对话里更靠后、更简短的跟帖消息，而不是最初的完整需求描述）——已回退，不要重新引入这个方向。** 若要继续优化头部解析开销，应该在不改变"必须完整扫描到能覆盖 ai-title/last-prompt 出现位置"这个约束的前提下想办法（如按类型子串先廉价过滤要不要整行 `json.loads`，同时确保时间戳提取仍走完整解析，不能用无法区分嵌套字段的正文子串匹配去猜时间戳）。
- `_choose_claude_fallback_title` 同分候选（如两条 `last_prompt`）平手时用 `max()` 取先出现的那条：这是**有意**保留的行为，不是 bug。头部/尾部按时间顺序把候选依次加入 `title_candidates`，同源同分时"先出现"往往对应对话里更早、更完整的原始诉求，而"最后出现"经常只是简短的追问或跟帖（已用本机真实数据核验：反转成"取最后一条"会让约 15.6% 的会话标题变得更模糊、更不能代表这次会话在做什么）。不要假设"更晚出现的候选更能代表用户意图"去改这里。

## 界面

唯一界面是 Textual 左右分栏（`ui/main_screen.py`）：左栏会话列表 + 右栏对话预览/内嵌终端。旧 curses 全宽列表、Space 全屏预览页均已删除，禁止再加回第二套界面。改界面行为以 `ui/` 源码与 `test_ui.py` 为准。

- **TUI 多语言（`i18n.py`）**：界面默认英文；系统 locale 主语言为 `zh*`（`LANG`/`LC_ALL`/`LC_MESSAGES`/`LANGUAGE`）时自动中文。`PICKUP_LANG=en|zh` 可强制覆盖。机器接口（`pickup list` 等 JSON）不走翻译。新增用户可见文案必须进 `_MESSAGES` 并同时写 en/zh；测试固定 `i18n.set_lang("en")` 再断言，中文覆盖用 `test_i18n.py`。Textual 的 `BINDINGS` 在类创建时会冻结：`MainScreen` 在 `__init__` 里用 `dataclasses.replace` **只改 description**，禁止整表替换 `_bindings`（会丢掉 ListView 继承的 up/down/enter，表现为方向键失灵）。

- **点击崩溃（2026-07 真机实报，已修复）**：Textual 默认给所有 Widget 开启内置的鼠标拖拽文本选择（`ALLOW_SELECT = True`）。`SessionCard`/`NewSessionCard` 这类会被后台重扫线程动态增删的列表项 Widget 被点击时触发该逻辑，选择过程中控件被移除会导致 `container` 解析为 `None`，访问 `.region` 直接 `AttributeError` 崩溃整个应用。修法：`PickupApp`/`SessionCard`/`NewSessionCard`/弹窗菜单项 `_ChoiceItem` 设 `ALLOW_SELECT = False`；`EmbedPane` 保留默认值用于划词选中+复制（见「内嵌面板」节）。回归：`test_ui.py` 的 `test_clicking_session_card_selects_and_launches_without_crashing`。
- **分屏标题挂载竞态（2026-07-22，用户实报闪退）**：分屏外框进入父节点后，标题栏自身的 `compose()` 仍可能未完成；这时后台重扫或侧边栏高亮变化会走同身份原地更新，若再用 `query_one(".title")` 查尚未挂载的后代，会抛 `NoMatches` 并退出整个 TUI。修法：需要在挂载前更新的子控件应在父控件构造时创建并保存直接引用，`compose()` 只产出该实例，更新方法直接改引用；不要把“父控件已能从父级 children 找到”误当作“它的后代已完成挂载”。回归：`test_pane_header_title_update_before_compose`。
- **窗口缩放花屏（2026-07-20，iTerm2 真机实报）**：拖动终端窗口时备用屏幕会被终端自行 reflow，Textual 默认只差分重绘布局变化区域，两边对不上就整屏残影/错位。修法分两层且**必须防抖**：① `PickupApp._check_resize` 不立刻全量刷，只重置约 120–200ms 计时器，尺寸停稳后 `_force_full_repaint` 把整屏标脏走 compositor 全量路径一次；② 右栏 `EmbedPane` 的 `tmux resize-window` + 唤醒抓帧同样防抖，拖动期 `render_line` 只按当前宽度裁补旧缓存行，不在主线程狂刷 tmux。禁止改成「每次 Resize 都整屏全量重绘」——拖动会卡顿闪烁。回归：`test_resize_full_repaint_is_debounced`、`EmbedPaneResizeTests`。
- **窗口缩放后 Cursor「疯狂滚动」数秒（2026-07-22，iTerm2 真机实报）**：外层停稳后一次 `resize-window` 会让 Cursor/Claude 等按新尺寸整屏重排，重排过程会把对话从早到晚刷过一遍；pickup 若按 `%output` 全速镜像，观感就是右栏狂滚再停在最新。修法：已有 live `_grid` 时，resize 后开启抓帧冻结（`_begin_resize_capture_hold`），最短约 0.35s、最长约 3s，连续两帧画面相同才放行并一次跳到最新；中间态不刷 UI。回归：`test_resize_with_live_grid_starts_capture_hold`、`test_resize_hold_deadline_forces_release`。
- **窗口缩放崩溃 IndexError（2026-07-20，suzhou SSH 真机实报）**：高度骤变时 Textual `ChopsUpdate` 偶发 `chops[y]` 越界（spans 仍引用旧高度）。默认 `_handle_exception` 会直接退出整个 TUI。`PickupApp` 在 `_check_resize` 里给恢复额度，命中 `IndexError` 时先整屏重绘自愈，额度耗尽才走致命路径。回归：`test_compositor_index_error_recovers_instead_of_exiting`。
- **往上滚出现「只剩几列」的历史（2026-07-20，suzhou 真机实报）**：右栏短暂缩到极窄（布局未稳、终端被拖得很小）时若仍 `resize-window`，Cursor/Claude 会按当前列数**硬换行**写入 scrollback；之后右栏恢复正常宽度，直播区看起来正常，但往上滚仍是窄条。下限：`embed.MIN_HOST_WIDTH/HEIGHT`（40×10）；创建用 `normalize_host_size` 抬到下限，后续缩放用 `should_resize_host` 过滤，过窄直接跳过、保留上一次可用尺寸。`embed.resize` 自身再兜底一次。已烧进历史的窄折行无法自动还原，只能靠新输出覆盖。回归：`test_normalize_and_guard_host_size`、`test_tmux_resize_skips_when_pane_too_narrow`。
- **侧边栏被几天前的 Codex 管家会话刷屏（2026-07-22）**：OpenConductor 自动任务 cwd 在 `/tmp/oc-manager-codex/...`；目录删掉后会话被「cwd 不存在」滤掉，目录再建时整批复活，`_merge_scanned` 把它们全当 fresh 按 mtime prepend。修法：扫描丢弃 `oc-manager-*` 路径段；fresh 仅近 2 天 prepend、更旧追加末尾。回归：`test_scan_filters_ephemeral_oc_manager_cwd`、`test_all_sessions_append_resurrected_old_sessions_on_refresh`。
- **内嵌面板滚动卡顿（2026-07 真机实报，已修复）**：滚轮/resize 不得在 Textual 主线程同步 `embed.capture()`/`embed.pane_state()`。抓帧走后台线程（`EmbedPane._capture_loop`），`mouse_any`/`mouse_sgr`/`history_size` 等为后台写、主线程只读缓存；事件处理只更新 `history_offset` 并用 `_poke` 唤醒补抓。
- **内嵌面板滚动卡顿第二波（2026-07-19，v0.17.4）**：Claude Code 自 v2.1.88 起托管 pane 常开 SGR 鼠标捕获，滚轮主路径变为转发 press-only 序列；转发必须经 `embed.send_mouse_sequence` 后台队列（限速、积压丢旧），主线程零 fork；`pane_state` 查询降频到 5Hz。回归：`test_embed.py` / `test_ui.py` 的 `EmbedPaneWheelTests`。
- **已结束会话预览滚轮方向（2026-07-19，v0.18.2）**：`detail_offset`（0=顶部）与 `history_offset`（0=直播底）符号相反；详情态 `_wheel` 必须对 `local_delta` 取反再 `scroll_detail`。
- **静态预览默认钉底（2026-07-21）**：选中已结束/未托管会话时 `_detail_stick_bottom=True`，窗口与 `detail_offset` 贴最新消息；异步暖加载正文变长后仍钉底。用户上滚或 Home 后取消钉底，之后 `invalidate_detail`（列表刷新）只 clamp 当前位置、不强制跳回底部；End 或下滚到尾重新钉底。回归：`test_right_pane_detail_scrolls_with_page_and_end`、`test_detail_async_load_pins_to_bottom`。
- **托管首帧回退顶裁闪回会话开头（2026-07-22，用户实报）**：Cursor 等持续输出时，分屏 remount / 重扫会让 `focus_session(fallback)` 清空 `_grid`；旧逻辑用 `to_strips(..., height=pane_h)` 从对话**顶部**裁一屏，观感是「突然跳回最早消息再滚回最新」。修法：① 有 fallback 时钉底，托管等待首帧走 `_uses_detail_window()` 整篇+窗口；② `show_hosted_group` 在 `(session_key, keepalive)` 有序身份不变时就地更新、禁止整排 remount；③ `store.hosted` 仍登记时活跃判定不依赖单次 `is_alive`。回归：`test_hosted_fallback_pins_long_conversation_to_bottom`、`test_same_hosted_identity_skips_remount_keeps_live_grid`、`test_hosted_registration_keeps_session_active_without_is_alive`。
- **新建 Codex 右栏突然变空（2026-07-22，用户实报）**：空白新建先登记 8 位临时会话键，Codex 写出真实历史后扫描器会用正式 UUID 替换占位卡。旧逻辑只按同一托管名迁移分屏记忆和右栏格，没有迁移侧边栏当前选中键；列表重建找不到旧键便回到顶部「＋ 新建会话」，随后选择跟随把仍在运行的右栏覆盖成新建提示。修法：分屏键对齐时返回旧键→新键映射，列表重建在读取旧 DOM 选中键后同步映射并强制选中正式卡。回归：`test_reconcile_split_keys_after_provisional_becomes_real`。
- **会话列表刷新开销优化（2026-07-19）**：会话键序列不变时 `rebuild()` 走原地更新；变了才 `clear()`+`extend()`。`SessionCard` 的标题/生成态由外部注入，禁止每卡 `snapshot()`。`_tick_spinner` 无生成中会话时直接 return。完整对话只由右栏 `EmbedPane.show_detail` 承担，禁止再加全屏预览页。回归：`test_ui.py` 相关 rebuild/spinner 用例。
- **Textual 后台 worker 必须可取消**：用 `get_current_worker().is_cancelled` / `cancelled_event.wait(interval)`；worker 内不得直接读写 Widget/DOM，结果经 `call_from_thread` 回写。托管启动同样走单飞 worker。
- **侧边栏末行间隔（硬约定）**：搜索框、新建项的最后一行是间隔空行，画在控件自身高度内并算进命中区；禁止用 margin/兄弟空隙/`ListItem` padding。会话卡是三行正文（标题 / 状态+运行时 / 时间），不再另加末行空行。基准：搜索框高 2、新建项高 2、会话卡高 3。
- **筛选状态只认 `nav` 一份**：顶部搜索框写 `nav.project_query`；测试必须断言渲染结果。
- 卡片列宽按终端显示宽度计算（`pickup._text_width` / `_fit_cell`），不要用字符数 `ljust`。
- 主界面「状态」是进程活性（进行中/已结束）；`titles.status_tag` 与 `agent_api` 的英文枚举是另一套语义，不要混用。
- 判活只做「进程在/不在」两档；Claude / Codex / OpenCode 各自判活细节见对应扫描节。
- 聊天预览按需读取，只展示真实用户消息与最终答复；消息可选带 `timestamp`，有则由 `_preview_lines` 追加时间后缀。
- **会话缓存按 mtime 失效**：`get_conversation` 命中时比对文件 mtime，变了才重读。

### 统一会话时间线与右栏跟随

- `SessionStore.all_sessions()` 合并全部运行时后按 `_order` 稳定顺序：首次按 mtime 倒序；之后已有项位置固定。新出现且 mtime 在约 2 天内的插最前；更旧的「复活」会话追加末尾（避免 `/tmp/oc-manager-*` 目录重建时几天前的会话整批顶栏）。扫描侧丢弃路径含 `oc-manager-*` 段的临时 cwd。
- 列表虚拟索引 0 是顶部固定「＋ 新建会话」；默认选中最近会话。该项回车走 `new_session_flow`；底栏不再提供 `n` 快捷新建（改走侧边栏项或右栏顶栏加格）。
- **踩坑：空白新建闪退**——托管成功回调必须区分 `LaunchRequest` / `NewSessionRequest`，空白新建禁止读 `.session`。回归：`test_new_session_request_hosts_without_reading_session`。
- 会话卡三行正文（标题 / 状态+运行时 / 时间）；运行中绿色、已结束弱化；生成中只加 spinner。
- **项目搜索**：`#project-search` + `nav.project_query`；`/` 聚焦搜索；Down/Enter 回列表；Esc 先清空再回列表。
- 右栏随选择变化：托管显示现场；未托管/已结束显示完整对话预览（选中即加载，默认钉在最新）。面板聚焦时列表→右栏跟随暂停；**右栏→列表**仍要同步高亮（`_on_pane_focused`）。长对话用 Home/End/PgUp/PgDn 或滚轮（`detail_offset` / `_detail_stick_bottom`）。
- 点击会话卡等价 Enter；右栏聚焦时 `Ctrl-\` 交回列表。

### 会话级快捷键

- `a`/`q`/`x` 等会话动作集中在 `MainScreen` 与 `ui/modals.py`；不要再拆第二套「预览页专用」按键分发。侧边栏选中/托管不抢右栏焦点；滚轮按命中区处理。
- 新建：侧边栏「＋」走 `new_session_flow`（项目→运行时）；顶栏点助手走 `_on_runtime_pick`（当前项目加格）。cwd 仍由 `_new_session_cwd` / `area.current_project` 解析。
- `a`：无具体会话时只 beep。
- 运行时/项目选择走 `RuntimePickerModal` / `PickMenuModal`。

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
- **接力提示词刻意不注入源会话的列表状态标签（`status_tag`，如 `✅已完成`/`待回复`/`已中断`）**：
  接力的目的就是接着往下干，一旦开头告诉接手 agent「会话状态：✅已完成」，它极可能直接回
  「当前没有待办、等新指令」，把接力废掉。是否还有未完成任务由接手 agent 自己读历史 + 看工作区
  实际状态判断，`render_prompt` 末尾已有对应指令。因此 `Handoff` 不再带 `status_note` 字段，
  `export_handoff` 也不再从 `status_tag` 取值——不要为了「让接手方知道原状态」把这行加回来。

## 会话保活（keepalive.py）

- **定位**：运行时无关的启动包装层，地位类似 `titles.py`——不属于任何 `runtime/` 适配器，`registry.py` 只管生成 `LaunchPlan`，`keepalive.py` 负责在执行前后包一层 tmux。新增运行时不需要碰这个模块。
- **专用 socket 隔离**：全部操作走 `tmux -L pickup-keepalive`（独立 server），配套专属配置（`-f` 显式指定），完全不读、不写用户自己的 `~/.tmux.conf` 或默认 socket。目的是让保活会话“无感”（隐藏状态栏、真彩色、`window-size latest`），同时绝不影响用户手动开的 tmux 会话。
- **tmux 配置内容内联在 `keepalive.py` 的 `_TMUX_CONFIG` 字符串常量里，不是仓库里一个独立的 `.conf` 文件**——这是踩过坑之后改的：项目用 setuptools `py_modules`（扁平模块列表，不是 package）分发，只声明 `.py` 文件会被装进去，同目录放一个 `keepalive.tmux.conf` 完全不会随 `pip install`/Homebrew 安装进最终环境（实测 `pip install --target <dir> .` 之后目标目录里只有 `keepalive.py`，配置文件确实缺失）。改成 `_ensure_config_file()` 在每次 `wrap_plan` 时把内联字符串落盘到 `~/.cache/pickup/keepalive.tmux.conf`（内容变了才重写，同 `titles.py` 缓存目录），从根源避免"源码目录能跑、真实安装后启动就找不到 `-f` 文件"这类只有装包验证才能发现的问题。改配置内容只改 `_TMUX_CONFIG` 常量，不要再新建独立文件；改完最好实际走一次 `pip install --target <临时目录> .` 确认没有引入新的非 `.py` 依赖。
- **匹配保活会话不能只靠 tmux 会话名，必须走 pid 祖先链**：`wrap_plan` 生成的 tmux 会话名只在创建时用某个 `runtime_id + ident` 拼一次，之后原生恢复（如 `claude --resume`）可能在内部 fork/重新注册进程，导致 pane 里的顶层 pid（`#{pane_pid}`）不一定等于运行时自己事后记录的“活跃 pid”（如 `~/.claude/sessions/{pid}.json` 里的 pid）。`annotate()` 因此不比较 pid 是否相等，而是一次 `ps -eo pid,ppid` 建出整机父子关系表，对每个候选活跃 pid 向上追祖先链，只要能追到某个 tmux pane 顶层 pid 就算命中——对是否发生过 fork 免疫。`ps` 而非 `/proc`：项目要求同时支持 macOS/Linux（见 `README.md` Requirements），`/proc` 在 macOS 上不存在，`ps -eo pid,ppid` 两边通用。
- **`annotate()` 的调用点分散在三处，故意不做成单一收敛点**：`store.SessionStore.load()`（TUI 列表）、`agent_api` 的 `cmd_list`/`cmd_search`（直接 `runtime.scan_sessions` 拼列表）、`resolve_ref`（`show`/`context`/`plan continue` 共用的会话定位）。三处各自扫描各自的会话集合，注册表层的 `scan_all()` 只被 TUI 用到，`agent_api.py` 走的是另一条按 runtime 单独扫描的路径，没有单一choke point；`annotate()` 本身只读（一次 `tmux list-sessions` + 一次 `ps`），开销可忽略（有活跃 pid 候选才会真的发子进程，见下一条），所以选择在每个"即将构建 session payload/渲染列表"的地方各调一次，而不是硬凑一个共享入口增加耦合。
- **`annotate()` 内部先判断有没有带 pid 的候选会话，再决定要不要真的发 `tmux`/`ps` 子进程**：完全空闲、没有任何 `live` 会话时（`pickup --json` 场景之外的多数命令行调用），这一步直接短路返回，不产生子进程开销；只有存在候选活跃 pid 时才值得为此打两次子进程。这个判断顺序（先查候选、后发子进程，不是反过来）是刻意的性能取舍，不要为了"代码更直觉"而颠倒。
- **`_launch` 里先无条件尝试 `attach_plan`，`keepalive_on` 开关只管要不要包装新启动的进程，不管是否要接回已有的**：如果某个历史会话已经被标注了 `keepalive_name`（意味着它当前正跑在某个 tmux pane 里），即使这次调用带了 `--no-keepalive`，也必须走 `attach-session` 接回去，不能假装没看见、重新拉起一个 `claude --resume` 去抢同一份会话文件——那会导致两个进程同时写同一个 JSONL，状态错乱。`--no-keepalive`/`PICKUP_KEEPALIVE=0`（旧名 `SC_KEEPALIVE=0` 仍生效）只影响"这次新启动的进程要不要被包进保活层"，对"识别到的已有保活会话该不该接回"没有否决权。改这段逻辑前想清楚这个区分，不要把两件事合并成一个开关。
- **回收（`reap_idle`）**：按 tmux 自己维护的 `#{session_activity}`（该会话最后一次有任何活动的时间戳）判断空闲时长，超过 `PICKUP_KEEPALIVE_IDLE_HOURS`（旧名 `SC_KEEPALIVE_IDLE_HOURS` 仍生效；默认 24，`0` 禁用）就 `kill-session`。不常驻额外的守护进程/定时器——`main()` 在进 TUI 前顺带跑一次，随 `pickup` 的启动节奏自然触发，足够覆盖"长期没人用 pickup 就不会占着内存"的诉求；会话历史本身在磁盘上，回收只是关掉后台进程，不丢数据。
- **会话名前缀 `pickup-`，旧前缀 `sc-` 保留匹配**：项目改名 sessionContinue → pickup 之前创建的 `sc-*` 保活会话可能仍在用户机器上跑，`_list_tmux_sessions` 同时匹配两种前缀，annotate/回收对存量会话继续生效；新建会话一律用 `pickup-` 前缀。注入托管会话的环境变量同理：`PICKUP_RUNTIME`/`PICKUP_SESSION_ID` 为新名，`SC_RUNTIME`/`SC_SESSION_ID` 继续注入兜底。
- **无前缀脱离键 `Ctrl-\`**：`keepalive.tmux.conf` 里 `bind-key -n C-\\ detach-client`（`-n` 表示不需要 prefix 就能触发）。选它是因为 tmux 接管终端后处于 raw 模式，`Ctrl-\` 不会像普通终端那样触发本地 `SIGQUIT`；标准 `Ctrl-b d` 始终保留作为备用。新增/改绑定前确认没有和目标运行时 CLI 自身的快捷键冲突。
- **已知边缘案例**：`attach-session` 发起瞬间目标会话恰好自然退出（tmux 报错退出），`pickup` 不做特殊重试，用户重新打开一次 `pickup` 即可（这时该会话已经不再显示"后台运行中"，回车会走正常原生恢复路径）。
- **直启子命令（`pickup claude` 等）默认不再直接 execvp，而是带着 `_DirectLaunch`（plan + runtime_id + ident）进 TUI**：真实终端且内嵌可用时，`_run` 在主循环前经 `embed.host_session` 把新会话托管进保活 socket 并聚焦右栏——保活包裹因此走 embed 路径（与界面内「新建会话」同一个调用点）。托管成功回调必须用 `register_hosted_session(..., ident=direct.ident)` 立即登记侧边栏占位卡、按当前工作目录归入项目并重建列表；不能只给右栏临时 dict、等扫描器发现真实历史。Codex 会在首条用户消息发出时才真正创建历史文件，旧做法会让侧边栏从直启到首条消息再到下一轮重扫才出现，真机曾延迟约 40 秒。真实历史经 `keepalive.annotate()` 挂上同一托管名后，`_merge_scanned()` 会自动退役占位卡，不能另写替换逻辑。只有非真实终端、`--no-keepalive` 或内嵌不可用（无 tmux/被环境变量禁用）时才退回旧路径 `keepalive.enabled()`/`keepalive.wrap_plan()` + `execute_launch`（`wrap_plan` 的第三个调用点，与 TUI 的 `_launch()` 复用同一套开关语义）。直启没有"已有保活会话"这个概念（每次都是全新会话，`ident` 用 `keepalive.new_session_ident()` 现生成），所以不需要像 `_launch()` 那样先尝试 `attach_plan`。回归：`DirectLaunchHostingTests.test_direct_launch_hosts_and_focuses_pane_without_stealing_focus_back`。

## 内嵌面板（embed.py）

> **界面层迁移说明（2026-07）**：界面已从手写 curses 整体换成 [Textual](https://github.com/Textualize/textual)，`embed.py` 的 tmux 抓帧/输入转发/控制通道仍保持 UI 框架无关的架构边界，但其性能与生命周期实现会继续演进。旧版入口模块的 `_run` 主循环 + `_draw_embed_pane` 被 `ui/main_screen.py` 的 `MainScreen` + `ui/embed_pane.py` 的 `EmbedPane` widget 取代；`PairPool`（curses 颜色对池）被框架中立的 `embed.cell_style(cell) -> rich.style.Style` 取代；`translate_key(curses键码)` 被 `translate_textual_key(key字符串)` 取代。下文凡是描述 curses/ncurses 内部行为的部分仅作历史存档；tmux/协议层结论仍适用，但以同节较新的 ControlChannel、Line API 和滚动约束为准。**鼠标拖拽跨行选词 + 复制**直接复用 Textual 文本选择：拖动高亮，`Ctrl+C` 经 OSC 52 复制；`Ctrl+C` 判断必须留在 `EmbedPane._on_key`，因为 widget `event.stop()` 后 Screen `BINDINGS` 不会再执行。无选区时 `Ctrl+C` 继续转发给托管会话中断命令。

- **定位**：与 `keepalive.py` 平级的运行时无关层。keepalive 管「把启动计划包进 tmux 保活」，embed 管「不 attach——用 `capture-pane` 拿画面、`send-keys` 送按键」，让 TUI 回车后退化成左侧会话列表（固定 ~39 列，`ui/main_screen.py` 的 `LIST_PANE_WIDTH`）+ 右侧会话现场（`EmbedPane`）。与保活共用 `tmux -L pickup-keepalive` socket 和 `pickup-*`/`sc-*` 命名空间：`keepalive.annotate()` 状态标注、`reap_idle()` 空闲回收、`q` 结束进行中会话，对内嵌会话全部照旧生效。适配器不感知本模块。键盘焦点默认在侧边栏；点右栏才与内嵌会话交互，滚轮与焦点无关。
- **窄栏是卡片式多行布局**：搜索框/新建项遵守「末行间隔」硬约定（见 AGENTS.md / 上文）。`SessionListView` 里每个会话是高度 3 的 `SessionCard`（三行正文：标题 / 状态+运行时靠右 / 时间靠右）；`NewSessionCard` 高 2；`#project-search` 高 2。搜索框与新建项的间隔画在控件自身内并算进命中区，不要用 `ListItem` 的 margin/padding。运行中状态为绿色，已结束弱化，生成中只加 spinner、不加粗。左栏 `LIST_PANE_WIDTH=39`；`SessionListView` 把滚动条占位收成 0；长标题按显示宽度截断并加 `...`。
- **runtime 名配色（单一来源）**：色表与样式串在 `theme.py` 的 `RUNTIME_LABEL_STYLES` / `runtime_label_style(runtime_id)`（按 runtime **id**，不是 display_name）。左栏 `SessionCard`、右栏详情头、对话预览里的 `◆ Runtime` 行必须共用这一处，禁止再在 `ui/` 里另写一份 hex。配色优先一眼可辨、不强制品牌色复刻——Cursor 品牌橙与 Claude 撞色，故 Cursor 用紫：`claude=#D97757`、`codex=#60A5FA`、`cursor=#A78BFA`、`kimi=#F472B6`、`opencode=#34D399`，未知回退 `dim`；展示用 `bold <color>`。新增 runtime 只加色表一行。
- **踩坑（2026-07-19 / 2026-07-21 / 2026-07-22）：改了源码但 `pickup` 仍是旧行为**——`~/.local/bin/pickup` 常见 shebang 指向 **pipx venv**，加载的是该 venv 的 site-packages **副本**，不随 `cli/` 源码自动更新。Cursor / 系统 `python3 -c "import pickup"` 往往能 import 到仓库 `cli/src/pickup`，单测「看起来已改好」，用户敲 `pickup` 却仍是旧包。**根治（开发机）**：`bash scripts/dev-install.sh`（对入口解释器 / pipx 做 `-e` editable）；之后改 `src/` 立刻生效。核对：`pickup --version` / `pickup diagnose` 看 `package_file`、`loaded_from_checkout`、`stale_source_warning`；在仓库目录内启动 TUI 若加载了别处副本，stderr 会告警。仍须**重启**已打开的 TUI。`pip show pickup` 的 Version 跨 Python / pipx 还可能误导。Textual SVG 导出常把真彩色压成灰阶，**不能**靠 SVG fill 判断 runtime 配色——用真机 TUI 或 Pilot 下 `SessionCard.render_line(0)` 的 segment style 核对。命令清单见 `AGENTS.md`「本机入口」。
- **踩坑（2026-07-21）：SSH 上 pickup 颜色「变丑」不是为了省带宽**——Rich/Textual 按环境协商 `color_system`：`COLORTERM=truecolor|24bit` → 真彩；否则 `TERM` 以 `-256color` 结尾 → 256 色（hex 主题被量化，看起来发脏）；再否则 → 约 16 色。OpenSSH 默认 `AcceptEnv` 常只有 `LANG LC_*`，**不转发 `COLORTERM`**，远端因此掉到 256。这与网络负载无关，pickup 主题/内嵌画面本身发的是真彩 hex，也禁止再做 `_rgb_to_256`。排查：远端 `echo $TERM $COLORTERM`；本机兜底可 `export COLORTERM=truecolor`、`/etc/profile.d/truecolor.sh`，或 sshd `AcceptEnv ... COLORTERM` + 客户端 `SendEnv`。强制真彩在现代终端（iTerm2/Ghostty/kitty）经 SSH 通常安全，带宽开销可忽略。
- **输入延迟的五道闸**：① **控制模式通道**（`embed.ControlChannel`）：聚焦 pane 时开常驻 `tmux -C attach` 子进程；修改类命令必须走通道，`capture-pane`/`display-message` 只读查询优先走同步 `request()`、通道失败才回退外部 fork；SGR 鼠标序列和多行 paste 仍走专用外部路径。pane 输出的 `%output` 唤醒抓帧，`%pause` 自动回 continue。② 没事发生不画；③ 画面文本没变不重复 `parse_screen`；④ Line API 只编译和刷新变化行；⑤ `%output` 事件驱动 + 慢速兜底轮询，抓帧在输出风暴下限速。死亡判定要求连续 3 次 capture 失败且 `has-session` 确认，防 tmux 瞬时超时偷走焦点。控制通道不会把窗口缩成自身尺寸，keepalive 的窗口策略无需为它调整。
- **`ControlChannel` 的 ready/FIFO/close 是一组不可拆的协议约束**：构造后必须先完整消费 `tmux -C attach` 自身的启动响应并确认 ready，业务命令才能进入队列，否则首个请求会错拿 attach 的 `%end`、后续响应整体错位。同步 `request()` 和火后不理的 `command()` 共用一个按写入顺序登记的 FIFO；waiter 登记与 stdin 写入/flush 必须在同一把锁内，reader 按完整 `%begin…%end/%error` 块依次出队，不能用时间戳猜跨线程响应归属。请求超时后通道必须关闭（响应是否迟到已不可知，继续复用只会错位）。`close()` 必须幂等，唤醒 pending 与 reader 已取出的 active waiter，并依次关闭 stdin、terminate/必要时 kill + wait 子进程、避开 reader 自己 join 自己、最后关闭 stdout；分栏关闭和应用退出都要走 `close_channel()`，不能留下孤儿控制 client 或僵尸进程。真机集成测 `test_embed.ControlChannelIntegrationTests.test_channel_send_reaches_pane_without_fork` 在共享机器高负载下偶发「数秒内画面未出现注入标记」超时——优先隔离重跑该用例，不要据此回退通道抓帧路径。
- **光标锚定（IME 预览位置修复，2026-07 已在 Textual 版本补齐）**：curses 版本靠每帧把外层硬件光标 move 到 pane 内 agent 光标处；Textual 的等价接口是 `App.cursor_position`（官方文档明确写的用途就是"controlling the positioning of OS IME and emoji popup menus"）。`EmbedPane._update_app_cursor()` 把 `pane_state()` 拿到的 `cursor_x/y/flag`（widget 内局部坐标）加上 `self.region.offset`（widget 在屏幕上的绝对位置）换算成屏幕绝对坐标写入 `self.app.cursor_position`；聚焦（`_on_focus`）、抓到新一帧（`_apply_capture`/`_apply_cursor_and_flags`）、resize 时都会重新计算。**真机验证方法**（本机没有输入法，验证不了候选框肉眼位置，但可以验证它依据的底层坐标算得准不准）：`selftest.sh` 里用一个不换行打印提示符的 fake 命令占住 pane，同时读外层 tmux 会话（pickup 自己跑的那个）和内层托管会话（tmux 视角）各自的 `#{cursor_x}/#{cursor_y}`，断言 `外层x == 左栏固定宽度(39) + 右栏前空隙(1) + 内层x` 且 `外层y == 助手顶栏(1) + 格标题(1) + 内层y`——即验证 Textual 真实接管的终端硬件光标寄存器精确落在托管画面里 agent 光标应该在的屏幕位置。光标不可见（`cursor_flag=False`）时 `_cursor_local_offset()` 返回 `None`，不更新 `app.cursor_position`（沿用上一次位置，不锚到左上角或底部帮助行）。
  - **只锚定位置还不够，必须让外层真实光标「可见」——否则中文根本打不进去（2026-07 真机反馈"内嵌 agent 打不了中文"后补的关键修复）**：`App.cursor_position` 只负责把光标**移到哪**，但 Textual 全屏运行期默认在驱动启动时写一次 `\e[?25l` 把真实硬件光标**藏掉**、整个运行期都不再显示（只有退出时才 `\e[?25h`），全靠 Input/TextArea 自己画一个假光标块。位置算得再准，被藏起来的真实光标对 IME 也没意义——macOS 等系统的输入法靠**可见的真实光标**决定候选词窗口位置、甚至据此决定要不要激活中文合成，真实光标不可见时用户在内嵌 Agent 里连中文都打不出来（不是候选框错位，是压根不合成）。**这正是"位置验证通过但 IME 仍不工作"的盲区**：老的 `selftest.sh` 只断言了 `#{cursor_x}/#{cursor_y}`（位置），没断言 `#{cursor_flag}`（可见性），位置对了就误判通过。修法：`EmbedPane._set_real_cursor(visible)` 在聚焦 pane 且有可见光标时显式写 `\e[?25h` 打开真实光标（Textual 每帧只移动、不会重新隐藏，写一次即保持），失焦/会话结束/托管程序自己藏光标时写 `\e[?25l` 收回；`_update_app_cursor` 在设置 `app.cursor_position` 的同时调用它，`_on_blur`/`_apply_dead`/`on_unmount` 负责收起。效果等同 tmux/screen attach 时把外层光标停在活动 pane 的光标处——那正是嵌套终端里 IME 能正常工作的原因。**真机验证**：`selftest.sh` 已补 `#{cursor_flag}==1` 断言（聚焦内嵌 pane 时真实光标必须可见）；Pilot 侧 `test_focus_shows_real_cursor_blur_hides_it` 断言 `_real_cursor_shown` 随焦点翻转。**教训**：验证"某个依赖底层终端状态 A 的上层效果 B"时，只断言 A 的一个维度（位置）不能证明 B 成立——A 的另一个维度（可见性）同样是 B 的必要条件，漏断言哪个都会放过真实 bug。
- **托管状态双通道**：`keepalive.annotate()` 靠 pid 祖先链匹配（跨进程有效），`SessionStore.hosted` 记本进程内刚内嵌的会话（比 pid 注册快、对不注册 pid 的程序也有效）；`_merge_scanned` 先 annotate，没匹配上的用 hosted 兜底并校验存活。`q` 结束会话时两处都要清，并立刻把 `live`/`pid` 置为已结束、记入 `_force_ended`——否则列表会先从「运行中(托管)」闪成「运行中」（只清了托管名、上次扫描的 live 还在），进程尚未退出时下一轮扫描仍报 live 也会再闪一次；确认扫描到 `live=False` 后才解除强制。**已知残留风险（2026-07 真实实例）**：高负载/竞争下 `keepalive_name` 标注可能瞬时丢失（annotate 匹配失败 + hosted 的 `is_alive` 超时误报同时发生），此时回车会走 `_embed_open` 新建出第二个同会话进程——保活 socket 上出现过 `sc-kimi-session_`（旧命名）与 `pickup-kimi-session_`（新命名）并存、两个进程抢同一份会话文件的真实案例；同一竞争还会导致 `q` 的确认弹窗不出现（端到端自测偶发过一次，加状态抓取后未复现）。根治方向（未做）：回车新建前若同名/同会话已有存活托管，强制复用而不是新建。
- **tmux 是软件级硬依赖，且有版本下限 3.2**：TUI 与直启子命令在启动时 `_require_tmux()` 检查，缺失即报错退出并提示安装；`agent_api` 只读子命令（`list`/`search`/`show`/`context`/`describe`）不检查——它们不拉起任何进程，`annotate()` 在无 tmux 时本就静默跳过。改这里之前想清楚：不要因为"优雅降级"把无 tmux 的半残启动路径加回来。**版本下限（2026-07 补）**：`host_session()`/`keepalive.wrap_plan()` 用的 `new-session -e` 环境变量注入（托管会话的 `PICKUP_RUNTIME`/`PICKUP_SESSION_ID` 等元数据唯一注入点）要求 tmux 3.2+（2021-04 发布），`ControlChannel` 依赖的 pause-after 流控（`%pause`）同样是 3.2+ 引入。旧发行版（如 Ubuntu 20.04 的 tmux 3.0a、Debian 11 的 3.1c）用户装完 pickup，若不检查版本会在首次创建托管会话时拿到一个和版本无关的笼统 `EmbedError`，很难联想到升级 tmux。`_require_tmux()`（`cli.py`）在 `shutil.which` 通过后追加 `embed._tmux_version() >= embed.MIN_TMUX_VERSION` 判断，报错明确点名当前版本和最低要求；版本解析不出时不阻断（宁可信任已通过的 `which` 探测，让真实失败在后续调用里自然暴露）。`embed.MIN_TMUX_VERSION = (3, 2)` 与 `supports_theme_report()` 用的 `(3, 5)` 背景色注入下限是两件独立的事——前者是硬性拦截，后者是软性降级（探测不到就退回文档兜底，不阻断启动），改任一个都不要混到另一个的判断里。
- **渲染为什么不自己写终端模拟器**：`capture-pane -p -e` 输出的就是 tmux（它本身就是终端模拟器）渲染好的当前画面加 SGR 颜色序列，`embed.parse_screen` 只需一个 SGR 状态机落格。`Cell.fg/bg` 为 `int | tuple[int,int,int]`：SGR 38/48;2 原样保留 RGB，`cell_style` 用 `rich.color.Color.from_rgb` 直通真彩色——**禁止再加回 curses 时代的 `_rgb_to_256` 量化**（会把托管 agent 的渐变/主题色打成 256 色块）。字符宽度统一走 `rich.cells.cell_len`（`embed._char_width` 与 `pickup._char_width`/`_text_width` 同一张表），自写 wcwidth 表会导致列表截断与内嵌画面 CJK/emoji 对齐不一致。curses 端曾用 `PairPool` 分配 `init_pair`；**该问题在 Textual 版本里已不存在**——颜色直接变成 `rich.style.Style`。实时画面使用 Textual Line API：按行缓存 `Strip`、比较新旧 `Cell` 行，只重编译并刷新变化行；`render()` 仅作框架内部/既有测试的兼容入口，必须复用 `render_line()` 结果，不能再维护第二套渲染逻辑。
- **Line API 的字符下标、选区坐标和静态详情失效必须一起维护**：`embed.row_text_and_spans()` 的样式 span 是 Python 字符串下标，不是终端 cell 坐标；跳过宽字符 continuation cell 后，索引仍要按 `len(cell.ch)` 增加，因为组合字符会和基础字符合在同一个 `Cell.ch`（例如 `e` + 组合重音长度为 2），固定 `+1` 会切掉后续文本。自定义 `render_line()` 返回的 `Strip` 必须 `apply_offsets(0, y)` 提供行坐标，否则 Textual 拖选会把整个 Widget 误判成全选；选区高亮在渲染时动态叠加，不能写进基础行缓存。**Textual 的选区坐标系是"字符索引"（字符串下标），不是 cell 列，`_apply_selection` 裁切前必须把字符索引换算成 cell 列**：`selection.get_span(y)` 返回的 (start, end) 是字符下标（`compositor.get_widget_and_offset_at` 逐字符 `get_character_cell_size` 推进最终得到的是字符 offset，`apply_offsets` 给段的基址也是累计字符数），而 `Strip.crop` 按 cell 列裁切。CJK/emoji 一个字占 2 列，直接把字符索引当 cell 列交给 `crop` 会有两个后果（2026-07-20 headless 复现）：① 高亮宽度按字符数缩水，中文选区只框住一半；② 裁切边界落在宽字符中间时该宽字符被 `crop` 整个丢弃、渲染成空格（真机反馈"从头拖到尾只高亮两个半、还有个字消失了"）。修复：`_apply_selection` 里用 `cell_len(text[:start])`/`cell_len(text[:end])` 把字符索引换算成 cell 列再裁切。**踩坑记录**：曾错误地把 offset 元数据改成 cell 基址（`_apply_cell_aware_offsets`）想在源头修，但那对单段行是 no-op（段基址为 0，两套坐标相等）、根本没生效，对多段行反而破坏了 Textual"段基址+段内字符"的一致性，已回退成 Textual 自带的 `apply_offsets`——正确的修法只在裁切那一步做字符→cell 换算，不要动 offset 元数据的坐标系。
  - **选区高亮不能整段套 `get_component_rich_style("screen--selection")`，否则会把选中的文字整个盖住看不见**（2026-07-20 真机 bug，headless 启动真实 app 打印样式值确认根因）：Textual 默认主题的 `screen-selection-foreground` 是 `transparent`（alpha 0），语义是"保留原文字前景色、只给背景着色"；但 `get_component_rich_style` 会把这个 transparent 前景**预解析成一个具体颜色**，实测这个值恰好等于选区背景色（都解析成 `#094472`），于是整段 `apply_style` 后前景==背景、文字隐形。Textual 自己的渲染路径（`Content.render_segments` → `line.stylize`）用的是 `textual.style.Style`（保留 alpha 语义，transparent 前景=不改前景），这里在自定义 Line API 路径上用 `_selection_style()` 手动复刻：读 `textual.style.Style.from_styles(...)` 判断前景 alpha，transparent 时只取 `bgcolor`、保留每个 Segment 原前景；确有前景色的主题才连前景一起套。样式按主题名缓存，不在 `render_line` 热路径每行每帧重解析。改动内嵌面板的选区渲染时，**务必 headless 启动真实 app 打印解析后的 `color`/`bgcolor` 实测，不要靠肉眼看颜色猜**。静态详情/占位页按状态和尺寸缓存后，标题缓存、会话状态、摘要、列表重扫或 resize 变化都必须调用 `invalidate_detail()`；详情 renderer 延迟执行时还要按稳定会话键重新解析 store 最新对象，不能继续展示闭包捕获的旧 dict。
- **输入路径的三个关键设计**：① curses 版本 TUI 必须从 cbreak 改 `curses.raw()`——否则 pane 聚焦时用户按 C-c 想打断 agent，SIGINT 会杀掉 pickup 自己；raw 模式下 C-\(0x1C) 才能作为「焦点回列表」的普通按键读入（和保活 tmux 配置里 C-\ detach 是同一肌肉记忆），列表/侧栏里 C-c（字节 3）显式映射为退出、C-z（字节 26）为挂起 pickup。**Textual 版本没有 cbreak/raw 的选择问题**：Textual 自己管理终端模式（应用启动即接管为适合自身事件循环的模式），`Ctrl-C`/`Ctrl-\` 都作为普通按键事件（`event.key == "ctrl+c"`/`"ctrl+backslash"`）送到当前聚焦的 widget；`EmbedPane._on_key` 专门拦截 `ctrl+backslash` 转发焦点请求（见「输入转发」小节），其余按键（含 `ctrl+c`）转发给托管会话，`event.stop()` 阻止再冒泡到 `MainScreen` 的 `ctrl+c` 退出绑定——效果与旧版一致（pane 聚焦时 C-c 打断 agent 不会误杀 pickup 自己），机制不同。② 可打印字节（含 UTF-8 高位字节）先按字节攒批、解码成字符串再一次 `send-keys -l`，避免每键一个 tmux 子进程；IME 提交的中文因此不会散成乱码——这条在 Textual 版本里对应 `event.is_printable and event.character` 分支直接调用 `embed.send_literal`，Textual 已经把按键解码成完整字符再交给事件处理，不需要 pickup 自己攒字节。③ 粘贴走终端 bracketed paste：curses 版本 TUI 启动时开 `\e[?2004h`（`main()` 在 wrapper 返回后关），pane 聚焦时识别 `\e[200~`/`\e[201~` 包裹的正文；Textual 版本里这条由框架原生处理并派发为 `events.Paste` 事件，`EmbedPane._on_paste` 直接拿到解析好的 `event.text` 经 `set-buffer` + `paste-buffer -p` 整段注入，目标程序按 bracketed paste 接收，不需要 pickup 自己解析 `\e[200~`/`\e[201~` 包裹序列。
- **必须关掉 Textual 默认的 Kitty 键盘协议，否则 iTerm2/Ghostty/kitty 里内嵌 Agent 打不了中文（2026-07 真机反馈"SSH 到远端跑 pickup、iTerm2 里内嵌 Agent 无法输入中文"后定位的真正根因）**：`cli.py` 在任何 `import textual` 之前 `os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")`。**根因**：Textual 的 `linux_driver` 启动时默认向终端发 `\e[>25u`（`25 = DISAMBIGUATE(1) | REPORT_ALL_KEYS(8) | REPORT_ASSOCIATED_TEXT(16)`）开启 [Kitty 键盘协议](https://sw.kovidgoyal.net/kitty/keyboard-protocol/)。支持该协议的终端（iTerm2 / Ghostty / kitty）收到后进入"把按键当转义码原样上报"模式，**绕过操作系统输入法（IME）**——用户在内嵌 Agent 里打 `nihao` 时，终端把 n/i/h/a/o 直接作为 CSI-u 事件发给应用，IME 根本没机会介入弹候选词，中文压根打不出来。**关键排查路径（记下来免得又走弯路）**：① 先怀疑并逐一排除了 pickup 侧——用真实 pickup 进程 + `tmux send-keys -l` 灌中文（含逐字节拆开模拟 SSH 网络分片切断多字节 UTF-8），验证 `_on_key → send_literal → tmux` 转发链**完全正常**，中文能落到 agent；② 又验证 Textual 的 `XTermParser` 对中文（无论普通 UTF-8 还是 Kitty CSI-u 关联文本形态）都能正确解出带 `character` 的 Key 事件——**解析和转发两半都没问题**；③ 决定性线索是用户反馈"同一个 SSH、同一个 iTerm2，`nano` 能打中文、pickup 不能"——两者唯一差别就是 pickup（Textual）开了 Kitty 协议而 nano 没开。**这类"输入根本进不来"的问题要优先排查终端协议协商（Kitty keyboard / DA / DECRQM 这类应用启动时主动发给终端、改变终端行为的私有序列），而不是死磕应用内部的解析/转发代码**——后者用 `send-keys` 灌字节很容易自证清白，真正的坑在"终端被应用切成了另一种模式"。**为什么可以整体关掉**：pickup 本质是把外层终端输入转发给托管 tmux 会话的终端复用器（类似 tmux/screen，它们默认也不对外层开 Kitty 协议），普通字节 + 标准转义序列已够用；Kitty 协议的按键消歧义好处（区分 Ctrl+I/Tab、上报按键释放等）对 pickup 边际很小，却实打实破坏 IME。**真机之外能做到的最硬验证**：用 `pty.fork()` 跑真实 pickup 抓它写给终端的原始字节流，断言默认不再出现 `\e[>25u`（强制 `TEXTUAL_DISABLE_KITTY_KEY=0` 作对照时该序列出现）——`test_ui.KittyKeyboardProtocolTests` 锁住开关状态，selftest.sh 全部按键路径在关闭协议下仍 14/14 通过（关掉不影响 Ctrl+\ 回列表、Ctrl+C、方向键、可打印字符转发）。⚠️ 必须在 textual 导入前设置：`textual.constants.DISABLE_KITTY_KEY` 是导入期求值的 `Final` 常量；用 `setdefault` 让想恢复协议的用户能显式设非 "1" 值覆盖。
- **「连接中…」卡死的状态机约束（2026-07 两次用户实报后补齐；当前实现位于 `ui/embed_pane.py`）**：产品层不允许出现任何“连接中”中间页——连接/抓帧是后台实现细节，已有会话首帧到达前立即显示扫描层已有详情，新启动且无详情的会话立即显示空白终端画布，随后无缝替换成实时画面。老 curses 版曾因 `grid=None` 时提前记录 `last_text`，静止画面之后永远被当作“未变化”而不再解析；迁移到 Textual 后又出现同类但更隐蔽的竞态——后台重扫/重复点击会再次 `focus_session` 同一个会话，前台无条件清空 `_grid`，抓帧线程的局部 `last_text` 却仍认为画面没变，于是实时画面永久不出现，调整窗口大小只是碰巧改变尺寸/文本后打破缓存。当前不变量必须同时保持：① `render()` 永远不渲染连接占位文案，`focus_session` 接收可选的即时详情渲染器；② `focus_session` 对“同名且已有有效画面”幂等，不清帧；确需失效（切换会话、详情页快速往返、尚无有效画面）时提升 `_capture_generation`，抓帧线程按版本强制重解析，即使它没来得及观察中间的 `session_name=None` 也能识别；③ 帧缓存键包含“版本 + 回滚偏移 + 宽高 + 文本”，窗口变化即使文本相同也要重排；缓存键只能在解析和主线程回写都成功后提交，任一步异常都要重试同一帧；④ `_apply_capture`/光标/死亡等所有跨线程回调携带“版本 + 会话名”并在回写前校验，旧会话已排队的回调不得覆盖新视图；⑤ 抓帧循环全包 `try/except`，异常写 `~/.cache/pickup/embed-error.log`（含 traceback，256KB 截断）后继续，不能让后台线程静默死亡。回归测试至少覆盖：首帧前即时内容、重复选中静止会话、详情页快速切回同名会话、旧回调延迟到达、单帧解析异常后自动恢复；真实 tmux 冒烟继续覆盖侧边栏显示。
- **终端背景色注入（深/浅主题检测修复）**：tmux 对 pane 内的 OSC 11 背景色查询的应答取决于有无 client——无 client 时石沉大海（查询超时），有 client（内嵌场景恒有控制 client）时按 client 默认值**应答黑色**，agent 因此在浅色终端上被误判成深色主题（这不是内嵌引入的，全屏 attach 一样）。`main()` 趁 Textual 接管终端前（`ui.app.run_app()` 之前，`_probe_osc_colours()` 本身不依赖 curses，未随迁移改动）`_probe_osc_colours()` 向外层终端查询 OSC 10/11 拿应答原文（非 TTY/不应答则 None；测试钩子 `PICKUP_OSC_REPORT`=hex）。
  - **`pickup` 自身界面的深浅色**是另一件独立的事：`PickupApp.on_mount` 用 `pickup._background_is_light(osc_report)`（解析 OSC 11 的 `rgb:RRRR/GGGG/BBBB`，ITU-R BT.709 亮度公式，阈值 0.5）决定 `self.theme = "pickup-light"` 还是 `"pickup-dark"`（冷静工作台主题，不是 Textual 默认的 `textual-*`）——这条 2026-07 才补上，之前完全没接，Textual 默认主题在浅色终端下配色不对（真机反馈）。筛选框与列表选中的层级约定见 `docs/TERMINAL_UI_KNOWLEDGE_BASE.md`「壳层配色层级」。
  - **内嵌 pane 底色必须垫成外层终端真实背景色（2026-07 修，真机反馈"内嵌 agent tui 背景变中性灰"）**：托管 agent 画面里绝大多数格子是"默认背景"（tmux `capture-pane` 报 `bg=-1` → `cell_style` 映射成 `bgcolor=None` → Rich 透明），Textual 会把它们透到 widget 底色。`EmbedPane` 若不显式垫底，透出的就是 Textual 主题的 `$background`（textual-dark 下是一种中性灰蓝），整块内嵌画面因此看着发灰——老 curses 版是天然透到终端真实底色的（`use_default_colors()` 的 `-1`），这是迁移引入的回归。修法：`EmbedPane.on_mount` 用 `pickup._background_rgb(osc_report)`（复用 `_background_channels` 解析 OSC 11，输出 `#rrggbb`）拿到外层终端真实 RGB，`self.styles.background = bg` 垫到面板上，默认背景的格子就落在真实终端底色上、和外层无缝衔接。探不到 OSC 11（`osc_report=None`）时不设显式底色、退回 Textual 主题灰（降级可接受）。回归测试 `test_ui.AppThemeTests.test_embed_pane_background_matches_real_terminal_bg` 用 Pilot 断言 `pane.styles.background.rgb` 等于注入的 OSC 11 RGB。注意这条只解决**视觉底色**，和上一条 `_background_is_light` 决定 pickup 自身主题、下一条 `report_theme` 注入让 agent 自己检测深浅，是三件独立的事，别混。
  - **托管 agent 自己的深浅色检测**注入链路：探测结果经 `run_app(osc_report=...)` 传进 `MainScreen`/`EmbedPane`。**关键设计（2026-07 真机排查修正）**：注入必须在 `embed.host_session()` 创建会话的**同一次调用里**完成，不能留到调用方后续聚焦面板时（`EmbedPane.focus_session`）才做——`refresh-client -r` 只影响"pane 尚未被回答过"的**后续**查询，一旦某次查询已经被 tmux 用默认猜测值（纯黑）答复过，那次查询的结果就**定死了**，之后再注入不能让已经用掉错误答案的进程回头重查一遍；真实 agent 常在启动的头几百毫秒内自己查一次 OSC 11，如果注入只在"用户聚焦面板"这一步才发生（中间还隔着 `open_channel`/`resize` 等多个 Python 级往返），大概率已经错过窗口。当前实现：`host_session(..., osc_report=...)` 创建会话成功后立即调用 `open_channel(name)` + `report_theme(channel, osc_report)`，且**通道必须保持打开、不能注入完就关**——`refresh-client -r` 依赖"当前有控制模式客户端连接着"这个前提，真机验证过一次性通道（注入后立刻 `channel.close()`）比完全不注入还差（之后一次都查不到，不是查到默认值，是彻底拿不到应答）；也验证过更激进的"先起占位命令、注入、再 `respawn-pane -k` 换成真实命令"方案（试图把窗口压缩到真实程序完全没机会先查），结果是 `respawn-pane` 会让 pane 拿到全新的 pty，把刚注入的颜色状态一并清空，比现在的方案更差，已放弃。`open_channel()` 复用同一个全局单例通道时，若已有相同名字的通道存在会跳过重建但更新 `on_output` 回调（不这样做的话，`host_session` 用 `on_output=None` 建的通道会一直卡在 None，`EmbedPane` 后续聚焦时的事件驱动重绘失效、退化成慢速轮询）。`host_session` 创建时经 `new-session -P -F '#{pane_id}'` 顺带取回 pane_id（记入 `_pane_ids`），供上述注入寻址。**实测边界（2026-07 在 tmux next-3.7 + Claude Code v2.1.207 上逐字节验证；观测手段：`tmux pipe-pane -t <会话> 'cat > <文件>'` 抓 pane 程序原始输出流，agent 启动时发出的查询序列原样可见）**：① Claude Code 启动时双通道查询——`\e[?2031h` 订阅主题通知 + DCS passthrough 包装的 OSC 11（tmux allow-passthrough 默认 off 被丢）+ 裸 OSC 11（tmux 应答，注入值走这条路）；② 注入只影响**之后启动**的 agent——已运行的 agent 不重查，refresh -r 后 tmux 会向订阅 pane 推 `\e[?997;1n`(dark)/`\e[?997;2n`(light) 通知，但 Claude Code 实测**不响应**（注入浅色后 user pill 依旧无色），旧会话只能重启或在 agent 里手动固定主题；③ Claude Code 的 user 消息背景 pill 在 tmux 里**天然不画**（启动前注入白色也一样），与 Codex issue #19741 同款，是 agent 侧行为不是 pickup 渲染错误；④ tmux 把注入的 16-bit RGB **归一化**成高 8 位重复格式（`abcd/1234/5678`→`abab/1212/5656`），断言/调试时别按原值比对；⑤ Kimi Code 查的也是裸 OSC 11（passthrough 包装只用于 DA 查询）——注入米白（`fae0`）后启动的 kimi 界面实测全部变为深色文字（浅色主题），注入对 kimi 端到端有效；未注入时它是近白字（深色主题误判），用户实报的「白底上白字」即此。
> **以下四条鼠标兼容记录（能力边界、列表页 SGR 降级、macOS 旧 curses 根治、向下回直播规则）全部描述 curses/ncurses 时代的手写鼠标协议协商**（`mousemask`/`KEY_MOUSE`/`BUTTON5_PRESSED`/SGR 1006 主动协商/`_apply_mousemask`/`_read_sgr_mouse` 等符号已随迁移整体删除）。Textual 用自己的终端输入驱动统一处理跨平台鼠标协议协商与解析（`events.MouseScrollUp`/`MouseScrollDown` 等是已经归一化好的事件，不需要 pickup 自己判断 BUTTON5 是否可用、要不要主动请求 SGR 1006），这一整类「同一代码 Mac 和 Linux 表现不一致」「加粗/协议协商炸掉点击滚轮」的坑随迁移被结构性消除。以下四条保留仅供追溯旧实现踩过的坑；`ui/embed_pane.py` 的当前实现见小节末尾新增的「Textual 版本鼠标处理现状」。

- **（历史）鼠标在面板内的能力边界**：mousemask 订阅滚轮 + 左键按下/抬起/拖动（不订阅会被 ncurses 在队列层滤掉，连丢弃的机会都没有）。`KEY_MOUSE` 必须先按坐标统一路由，**不能只在右侧 pane 聚焦时处理**：光标在左侧时，滚轮独立滚动会话列表的视口，**不改变当前选中会话**；键盘导航或点击某张卡片时才恢复“选中项始终可见”。落在右侧 pane 时，滚轮才进入回滚逻辑。右侧滚轮默认由 pickup 自己处理应用层历史偏移；仅当停在直播画面（偏移为 0）**且** pane 内程序确实申请了 SGR 鼠标上报（`mouse_any_flag`+`mouse_sgr_flag`）时，才把滚轮转成 SGR 序列直达内层程序（2026-07-18 用户要求：程序自己要滚动信号就给它）。~~实测 Claude/Codex 在托管 pane 里都不申请鼠标（两机 `mouse_any=False`），这条转发路径日常不生效~~ **（此实测结论已过时：Claude Code 自 v2.1.88 起默认全屏渲染并申请鼠标捕获，2026-07-19 实测 2.1.214/2.1.215 托管会话全部 `mouse_any=True`，转发路径已成为主路径——见上方「滚动卡顿第二波」条目）**；已翻进回滚历史后无条件由 pickup 收敛，保证「下滚回直播」永远可达。需要完整鼠标点选内层程序时，当前版本不提供 `e` 全屏逃生路径（已删除）；只能依赖终端修饰键拖选或后续产品再定方案。**点击/拖拽刻意不转发**——ncurses 会把快速连续的 press+release 合并成 CLICK（未订阅即整个丢弃）、press+drag 合并成 motion，行为碎片化到无法承诺语义（本机实测两种合并都复现）；另两个实测点：Python curses **没有 `BUTTON1_POSITION_CHANGED` 常量**（getattr 兜底返回 0，motion 位根本进不了 mask），未订阅的鼠标序列会被 ncurses 整个吞掉、不会漏进键盘通道变成垃圾按键；macOS 自带 ncurses 5.x 还不导出 `BUTTON5_PRESSED`，向下滚轮会伪装成 `BUTTON2_PRESSED`（有的终端还会附带 `REPORT_MOUSE_POSITION`），因此必须订阅并把中键按下视作向下滚动，否则事件在队列层被滤掉，回滚只能向上不能向下。

- **（历史）列表页原始 SGR 滚轮降级（2026-07 用户实测）**：不能只靠 `KEY_MOUSE`。部分 macOS 终端会把 SGR 1006 的 `ESC[<64/65;…M` 原样交给列表页的 Escape 解码；此前该路径只消费左键点击，滚轮因此被静默丢弃。列表页也必须识别 bit 64 的滚轮事件、按坐标确认落在左栏后滚动 `ui.top`，并保留当前选中会话不变。回归必须同时覆盖 ncurses `KEY_MOUSE` 与原始 SGR 两条路径。

- **（历史）macOS 旧 curses 的根治：主动请求 SGR 1006**：macOS SDK 的 `NCURSES_MOUSE_VERSION=1` 采用 32 位旧鼠标布局，官方头文件明确没有 Button5 的可用位；Linux 新 ncurses 有 Button5（实测 suzhou ncurses 6.3 协议 v2 下 `BUTTON5_PRESSED=0x200000`），故同一代码会出现「suzhou 可滚、Mac 不可滚」。`_apply_mousemask()` **仅在 BUTTON5 不可用的旧平台**（按值判断，见下文坑①）于 curses 配置后再发 `CSI ?1000h` + `CSI ?1006h`，关闭时对称撤销；之后原始 SGR 序列经 `_read_sgr_mouse` + `_sgr_synth_bstate` 合成 bstate，走与 KEY_MOUSE **完全相同**的 `_handle_mouse` 坐标路由（列表点击/滚轮、pane 滚轮/选词、预览页滚轮缺一不可），鼠标序列绝不当 Escape 文本透传给内层程序。**严禁全平台强开 1006**：新版 ncurses 自己协商并解码 KEY_MOUSE，强切编码会让终端上报格式变成 ncurses 不认的样子，点击/滚轮全部失灵——v0.16.8 就是这样在 Mac 和 suzhou 同时炸掉的（2026-07-19 用户双机实报，v0.16.9 修复）；同一教训的另一半是：改鼠标/终端模式类代码，selftest 的模拟按键不构成验收，必须做一次真实终端冒烟（至少用 `tmux pipe-pane` 核对进程实际发出的模式序列）。补充三个实测坑（2026-07-18 跨机器排查确认）：① Homebrew Python 按系统 ncurses 头文件编译时 `BUTTON5_PRESSED` 是「存在但等于 0x0」——`getattr` 的 default 分支救不了它，掩码/判定必须按值判断，否则掩码里下滚位是 0，事件在 ncurses 队列层被整个过滤（曾导致 Mac 只能上滚的直接根因）；② 协议 v1 的下滚除了伪装成 `BUTTON2_PRESSED`，还可能只报一个裸 `REPORT_MOUSE_POSITION` 位，`_is_mouse_wheel_down` 在 BUTTON5 不可用的平台上要认这两种形态；③ 跨平台鼠标事件差异只能拿真机事件流定位——设 `PICKUP_MOUSE_DEBUG=<文件路径>` 环境变量后 `_pane_mouse` 会把每个 KEY_MOUSE 的 bstate/分类结果追加写入该文件（默认零开销），排查时不用再临时改代码发版。

- **（历史）向下回直播的跨终端兼容规则（2026-07 用户真机确认）**：不能把 `BUTTON2_PRESSED` 当作唯一根因或唯一修复；它只是旧版 macOS ncurses 的一个已知伪装形式，触控板和不同终端还会产生无法可靠枚举的 `KEY_MOUSE` 状态组合。标准 Button5、已知 Button2 都可以优先识别；但只要已经处于回滚（历史偏移大于 0），任何**非左键**鼠标事件都按“向下回直播”处理，左键事件保留给选词。这一状态优先的降级规则才保证了先上滚、再用鼠标或触控板下滚能够回到直播；不要退回到“把所有鼠标事件转交内层 TUI”或继续猜测某一个位掩码。验收必须在真实终端完成“向上进入历史 → 鼠标/触控板向下 → 回到直播”的完整路径；只确认 SGR 序列能到达内层程序，不构成验收。

- **Textual 版本鼠标处理现状**：`EmbedPane` 自己只处理 `events.MouseScrollUp`/`MouseScrollDown` 两种滚轮事件（`ui/embed_pane.py` 的 `_on_mouse_scroll_up`/`_on_mouse_scroll_down` → `_wheel`）。托管会话自然方向必须固定为 Up 增加 `history_offset`（进入更早历史）、Down 减少（回到直播）；这个应用层整数**不得命名为 `scroll_offset`**，后者是 Textual `Widget` 的内置二维 `Offset`，覆盖后会让框架选区坐标执行 `Offset + int` 崩溃。**已结束/未托管会话的静态对话预览**用另一套 `detail_offset`（0=顶部、增大=更靠后），文档式自然方向与 `history_offset` **符号相反**——`_wheel` 在详情态必须对传入的 `local_delta` 取反再调 `scroll_detail`，否则会出现「下滚反而往上看」；2026-07-19 用户实报即此漏取反。直播位置且 pane 内程序申请鼠标上报时，仍转成 SGR **press-only**：64=上滚、65=下滚，经 `embed.send_mouse_sequence` 排队发送；已经进入历史或程序未申请鼠标时才更新 `history_offset`。点击、拖拽由 Textual 自己路由；改动或验收前先确认是在 Textual 事件层还是 `embed.py` 的 tmux 协议层。

- **滚轮翻历史必须走应用层滚动，不能用 tmux copy-mode**（2026-07 三层根因叠加的教训，用户实报「无法滚动」）：① copy-mode 的滚动偏移（`scroll_position`）只作用于 **client 渲染层**，`capture-pane` 抓的 pane buffer 永远停在 live 窗口——实测 `scroll_position` 从 50 涨到 56，capture 内容一个字节都不变，内嵌显示自然纹丝不动；② `send -X` 的 `-N` repeat 只对普通键有效（对 copy-mode 命令被静默忽略）、`scroll-up` 不收行数参数、`-X` 后多参数按多命令逐个解释（`-X -N 3 scroll-up` = 三个命令里前两个无效、最后滚 1 行）——每格滚轮实际只滚 1 行，和持续输出会话的新行速度完全抵消；③ `copy-mode -e` 在视图被新输出追平到底时自动退出，滚上去几秒就被顶回 live。正确做法（现实现）：`EmbedPane.history_offset` 记应用层偏移，Up 增加、Down 减少；变化后经 `capture-pane -S -offset -E (pane_h-1-offset)` 抓历史窗口渲染——窗口公式经真 tmux 钉死（seq 1 100 会话 `-S -6 -E 13` 得 76..95，相对 live 82..101 精确上移 6 行）；`pane_state` 顺带取 `#{history_size}` 作上限；键盘输入归零回直播（C-\ 保留位置）。**fake 夹具必须 `seq 1 100` 预置历史**（放在就绪标志输出之前，否则标志行被顶出屏幕），否则 history_size=0 会让滚轮测试假通过。pane 外（左栏）滚轮滚会话列表，其余忽略。
- **（历史）curses 版本手写的内置拖拽选词**：左键按下记 `emb.sel_anchor`、拖动（`REPORT_MOUSE_POSITION`）实时更新 `sel_start/sel_end`、抬起按流式区域（跨行连续）复制，文本读 `stdscr.instr`、复制走 OSC 52、高亮用 `stdscr.chgat(A_REVERSE)`。**这套手写实现在 Textual 重写里被整个删除**，不是照搬移植，而是换成 Textual 内置的鼠标拖拽文本选择（`EmbedPane` 不设 `ALLOW_SELECT = False`）——效果等价（拖拽高亮 + `Ctrl+C` 走 OSC 52 复制），实现方式不同：不需要 pickup 自己算 `sel_anchor`/合并跨行区域/管理高亮重绘，Textual 的 `Screen.get_selected_text()`/`get_selection()` 直接从 widget 当前渲染的内容里取选中范围。**已知取舍**：`m` 键关闭鼠标上报后走终端原生框选这条降级路径还在（未受影响）；但选区裁剪逻辑（旧版 `sel_zone` 限制选择不跨过左栏/pane 分界线）没有对应实现——Textual 的选择是按 widget 边界自然裁剪的（`EmbedPane` 和 `SessionListView` 是两个独立 widget，选择不会跨过去），不需要手写裁剪。
- **会话生命周期**：列表焦点下 `Esc` 退出 pickup 不碰任何托管会话（后台 tmux 里继续跑）；面板聚焦时 `EmbedPane._on_key` 把 `Esc` 以外的按键转发给 Agent（`ctrl+backslash` 专门拦截用于焦点回列表，见「输入转发」小节）。curses 版本需要手动等待 300ms 消化完整转义序列以区分方向键/鼠标序列和裸 `Esc`；Textual 的输入驱动自己完成这层转义序列解析，`escape` 是稳定的按键事件名，不需要移植这个等待窗口。面板里 agent 进程退出后 tmux 会话消失，capture 线程经 `has-session` 确认死亡（capture 失败本身不算数，可能只是超时），面板显示占位文案并把焦点弹回列表；`c` 只关闭分栏布局，不杀会话。
- **冒烟必须只操作自己新建的会话名**：本机其他 `pickup-*`/`sc-*` 会话通常是真实在跑的 Agent 会话，测试时一律不得 `kill-session` 或 attach 干扰（同保活节的红线）。
- **端到端自测脚本（`selftest.sh`，仓库根，58 项断言；以下记录写于 curses 时代，内置拖拽选词相关断言随该功能移除已不成立，改动或重新核对该脚本时先确认哪些断言仍对应 Textual 版本的真实行为）**：在独立外层 tmux socket 里跑真实 TUI（隔离 fake HOME + fake `claude` 夹具——注册 pid 文件、免疫心跳、按行回显、OSC 11 主题探测模拟、按 SID 决定是否申请鼠标），send-keys 驱动按键/粘贴/鼠标序列（`\e[<64;x;yM` 滚轮、`\e[<0;x;yM/m` 按下/抬起、`\e[<32;x;yM` 拖动），capture-pane 抓屏断言；外层 tmux 开 `set-clipboard on` 后可用 `show-buffer` 断言内置选词的复制结果。滚轮回归必须断言外层回滚状态：先滚入历史，再发送向下或未知非左键 `KEY_MOUSE` 事件，确认偏移下降并恢复直播；**不得**把“内层 fake 收到 SGR 滚轮序列”当作通过条件，因为内嵌模式的滚轮本就不再转发。写断言的坑（全部实踩过）：① `wait_for` 的 grep 必须加 `--`（模式以 `-` 开头会被当选项）；② 等输出特征别等「命令名」（如等 `RESP b'` 而非 `RESP`——命令行回显里就含 `RESP` 字样会提前命中）；③ fake 按行 `read`，无换行的控制序列要和后续输入凑满一行才落日志，断言控制序列前先补一行普通输入触发；④ tmux attach 到**小于终端的窗口**时会自画右缘边框竖线 `│` 和点阵填充——「竖线消失」不能当全屏判据，要用 pickup 自己的列表/提示文案消失；⑤ `--no-keepalive` 全屏 execvp 的 fake REPL 对 EOF 不退出（busy loop），后续步骤前必须整个外层 session 销毁重建；⑥ fake 的 OSC 11 探测要放在「就绪标志」输出**之前**，否则探测窗口内到达的按键会被探测进程的 os.read 吃掉；⑦ 屏幕文本读取用 `stdscr.instr`（不是 inchnstr/innstr——Python curses 只有 `instr`/`inch`），其 n **按字节截断**，宽字符区域要按格数 ×4 过读再 `_fit_cell` 按格截回。



## Cursor 扫描（scan_cursor.py / runtime/cursor.py）

- 历史只扫 CLI：`~/.cursor/chats/<workspace>/<chatId>/`（不扫 IDE `agent-transcripts`）。
- 列表轻扫：`meta.json` + `prompt_history.json`（最新在前）；`path` 优先指向 `store.db`。
- 完整对话：`load_conversation` 只读 `store.db` JSON blobs，提取 `<user_query>` 与 assistant 文本。
- 运行时 id=`cursor`，可执行文件=`agent`，`auto_approve_args=("--force",)`；恢复 `agent --force --resume <id>`。
- **同 cwd 多 agent 判活（2026-07 真机实报后修，同日再修串台）**：跨助手接力 / 空白新建会在项目目录起无 `--resume` 的 `agent`，同时旧会话可能仍以 `--resume <chatId>` 跑着。第一版用 `live_pids_by_process_name("agent")` 按 cwd 只留一个 pid，新接续标题会挂上旧保活画面。第二版改走 `live_processes` 保留全部进程，无 resume 时按「cwd → mtime 最新未标记会话」兜底——仍会把空壳欢迎页进程错绑到同目录更早的真实历史（真机：侧边栏「我想加个顶栏」、右栏却是空白 Cursor 欢迎页；`lsof` 显示该进程实际打开的是另一条 chat 的 `store.db`）。现绑定只认正向证据，优先级：① 命令行 `--resume <chatId>`；② 进程已打开的 `~/.cursor/chats/.../<chatId>/store.db`（含 wal/shm）；③ 环境变量 `PICKUP_SESSION_ID`/`SC_SESSION_ID`（仅完整会话 id，8 位空白新建临时标识不参与猜测）。**禁止**再按 cwd/mtime 猜测。回归：`CursorScanTests.test_live_flags_bind_resume_and_open_store_separately_in_same_cwd`、`test_live_flags_do_not_bind_blank_agent_to_older_cwd_history`、`test_live_flags_bind_via_pickup_session_env`。OpenCode/Kimi 仍用 cwd→单 pid 的保守策略（它们没有稳定的 resume 参数可解析）。

## 直启子命令（`pickup claude` / `pickup codex` / `pickup opencode` / `pickup kimi`）

- **定位**：`main()` 里在 agent_api 分发分支之后、TUI 的 argparse 之前再加一个前置分支——`sys.argv[1]`（跳过可选的前置 `--no-keepalive`）命中 `registry.ids` 就整体转发给 `_dispatch_direct_launch`，不进入下面的 TUI/`--json` 参数体系。这个顺序刻意和 agent_api 的分发方式对称：两者都是"整个命令行属于另一套子系统，不该被 TUI 的 argparse 解析"。分发后默认进 TUI 侧边栏模式托管新会话（见「会话保活」节对应条目），非真实终端 / `--no-keepalive` / 内嵌不可用时才 execvp 全屏接管。
- **为什么是纯透传 + 只垫危险参数，不像 TUI 里的 `build_resume_plan` 之类还塞了 codex 的 `-c model_reasoning_effort="high"`**：直启的诉求是"我知道我要传什么参数给底层 CLI，只是不想每次手打一长串跳过审批的危险参数"，属于用户对透传语义有明确预期的场景；额外静默塞入其它默认配置（哪怕是好意）会让用户没法确定"这次命令实际执行了什么"，所以 `registry.build_passthrough_plan` 只处理 `auto_approve_args`，不碰运行时的其它默认参数。
- **危险参数改成运行时类属性 `auto_approve_args`**（`runtime/base.py` 声明、`runtime/claude.py`/`runtime/codex.py` 各赋值一次），原本在每个适配器的 `build_resume_plan`/`build_continue_plan`/`build_new_plan`/`build_new_session_plan` 四处各写一遍字面量字符串，现在四处和直启共用同一份声明。新增运行时想接入直启子命令，只需要declare 这个类属性（不声明则默认空元组，直启不会额外加任何参数）。
- **`OpenCodeRuntime` 是这个模式下的一个刻意例外**：它的危险参数（`--dangerously-skip-permissions`）只在 `opencode run` 子命令下真实生效，裸命令（`pickup opencode` 直启透传的默认形态）带上会直接报错退出（实测确认，非猜测）。这个 flag 因此没有放进 `auto_approve_args`（该属性对 OpenCode 显式设为空元组），而是只硬编码在 `build_continue_plan` 内部——`pickup opencode`（裸直启）不会被自动垫上这个参数，这是有意为之，不是遗漏。详见「OpenCode 扫描」节最后一条。新增运行时如果也存在"危险参数只在特定子命令下有效"的情况，应参照这个处理方式，不要为了凑统一模式硬塞进 `auto_approve_args` 导致裸命令被打坏。
- **用户在透传参数里已经带了该运行时的危险参数时不重复添加**（`build_passthrough_plan` 用 `arg not in user_args` 过滤），这样 `pickup claude --dangerously-skip-permissions --resume xxx` 这类用户自己拼好完整参数的调用不会看到参数被加两遍。
- **`cwd` 恒为 `None`（不改变当前目录）**：直启是"就地拉起"语义，和 TUI 里恢复某个历史会话需要 `cd` 回原 `cwd`不是一回事，不要混用 `usable_cwd`。
- `_dispatch_direct_launch` 捕获 `execute_launch` 抛出的 `LaunchError`（如运行时未安装）打印错误信息并 `sys.exit(1)`，不让用户看到裸 Python 堆栈。

## 机器接口维护（agent_api.py）

- `list`/`search`/`resolve_ref`（`show`/`context`/`plan continue` 共用）在扫描多个运行时时统一走 `_scan_runtimes` 辅助函数：`ThreadPoolExecutor` 并发扫描 + 单运行时异常隔离，与 `runtime/registry.py` 的 `scan_all()` 同语义但独立实现（机器接口按需只扫描 `--runtime` 指定的子集，不复用 TUI 那份）。之前是逐个运行时串行 `scan_sessions()`，运行时数量越多、`pickup list`/`search` 不带 `--runtime` 时延迟越接近各运行时耗时之和；改并发后接近最慢那个运行时的耗时。新增调用点需要扫描多个运行时时复用这个函数，不要退回字典推导式的串行写法。
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
- `pickup.py`（`pickup` 入口）的非 TTY 自动降级（`sys.stdin.isatty() and sys.stdout.isatty()`）和 `list/search/show/
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
  上语义搜索要引入嵌入依赖、离线索引维护和额外算力/额度成本，与 pickup"轻量、依赖极简、离线可用"的
  定位冲突，暂不做（2026-07 界面层引入 `textual` 作为唯一第三方依赖后，这条结论权衡的份量不变——嵌入
  模型的依赖体积和离线维护成本比 `textual` 高一个数量级，不构成"反正已经不是零依赖了就无所谓"的理由）。

## 可观测性（怎么用）

pickup 是本地 TUI，不是常驻服务：**禁止** Prometheus / 远程遥测。诊断靠本地文件与只读子命令。

| 用途 | 入口 |
|---|---|
| 结构化事件（scan/rebuild/host/慢抓帧/截图/error） | `~/.cache/pickup/events.log`（一行一条 JSON） |
| 后台异常 traceback | `~/.cache/pickup/embed-error.log` |
| 真机当前屏截图 | TUI 内 **F12** → `~/.cache/pickup/screenshots/tui-*.svg` |
| README/改动夹具截图 | `python3 docs/screenshots/capture.py` |
| 端到端冒烟 | `bash selftest.sh` |
| 一键只读诊断 | `pickup diagnose`（JSON：路径、tmux、配色自检） |
| 细日志 | `PICKUP_DEBUG=1` 或 `PICKUP_LOG=debug`（额外 debug 事件） |

实现模块：`observe.py`（`event` / `debug` / `timed` / `log_exception` / `save_tui_screenshot`）。`pickup._log_embed_error` 转调 `observe.log_exception`（events 一条 + embed-error 栈）。

事件名约定：`scan_all`、`list_rebuild`、`host_session`、`capture_slow`（≥100ms）、`screenshot`、`error`。默认不写对话正文；敏感字段名会被改写为 `<redacted>`。

不要用 Textual SVG 导出判断 runtime 真彩色是否生效（常被压成灰）；配色验收见上文「踩坑：改了配色但 TUI 仍无色」。计划原文：`docs/superpowers/plans/2026-07-19-tui-observability.md`。

## 客户端自动更新（updater.py / ui/update_toast.py）

- 业务逻辑集中在 `src/pickup/updater.py`，与 UI/CLI 解耦、可独立测试：版本比较（`current_version`/`is_newer`）、安装渠道判定（`detect_channel`：按 `pickup.__file__` 路径含 `/Cellar/`、`/homebrew/`、`linuxbrew` 判定 `brew`；在 `site.getusersitepackages()`/`site.getsitepackages()` 或路径含 `site-packages` 判定 `pip`；其余判定 `dev`，源码检出/editable 安装一律不可自动升级）、最新版查询（`fetch_latest`：打 `https://api.github.com/repos/x0c/pickup/releases/latest`，取 `tag_name` 去掉前导 `v`，3 秒超时，任何异常一律返回 `None`，不得抛出拖垮调用方）、就地升级命令（`update_command`：brew 走 `brew upgrade pickup`；pip 走 `pip install --upgrade git+https://github.com/x0c/pickup.git@v<tag>`，是否带 `--user` 取决于当前安装路径是否在用户 site-packages 下）。
- 忽略状态持久化在 `~/.cache/pickup/update.json`（`{"dismissed_version", "dismissed_date"}`），写法复用 `titles.py` 的原子写惯用法（`.tmp.{pid}` + `os.replace`，避免并发读到半截 JSON）。`should_prompt(latest)` 语义：`latest` 严格新于当前版本，且不满足"今天已经忽略过这个版本"——同一天忽略后不再弹，换一天或出了更新的版本会恢复提示。
- TUI 侧：`ui/main_screen.py` 的 `MainScreen.on_mount` 起一个 `@work(thread=True)` 的 `_check_for_update`，只在 `is_updatable(channel)` 为真时才查网络，dev 渠道直接跳过、完全不发请求（源码检出/开发安装因此永远不会被打扰）。有满足条件的新版本时 `call_from_thread` 把 `ui/update_toast.py` 的 `UpdateToast` 切到 `available` 状态。点击后走 `_run_update_worker`（同样是 `@work(thread=True)`）跑 `updater.run_update`，成功进 `done`（可点击触发重启）、失败进 `failed`（可点击重试）。
- 重启机制：`UpdateToast` 的 restart 回调调用 `self.app.exit(result=updater.RestartRequest())`——`RestartRequest` 是 `updater.py` 里的哨兵 `@dataclass(frozen=True)`，与既有的 `LaunchRequest`/`NewSessionRequest`/`None` 并列作为 `run_app()` 的第四种返回值语义。`cli.py` 的 `main()` 和 `_dispatch_direct_launch()` 在 `run_app()` 返回后都要判断 `isinstance(chosen, updater.RestartRequest)`，命中则调用 `cli._restart_process()`——用 `os.execv(sys.executable, [sys.executable, "-m", "pickup", *sys.argv[1:]])` 原地替换当前进程，透传本次启动的全部命令行参数。tmux 保活会话与本进程生命周期无关，重启不影响已托管会话。
- `pickup update` 终端子命令（`cli.py` `main()` 顶层拦截 `sys.argv[1:2] == ["update"]`，转发给 `updater.cli_update()`）用于不开 TUI 时手动触发升级；**故意不放进 `agent_api.py`**——那里的架构约束是只读、无副作用命令，`update` 有真实写盘/装包副作用，不符合这条边界。
- **真机调试踩坑（务必记住）**：`UpdateToast`（`Container` 子类）最初把状态刷新方法命名为 `_render`，与 Textual `Widget` 基类自身用来计算可绘制内容的内部方法 `_render()` 同名，被静默覆盖后返回 `None`；框架在需要自绘时（如 `dock: bottom` + `align: right bottom` 的浮层容器里，子节点未占满的空白区域）调用 `self._render()` 拿到 `None` 而不是真正的 `Visual`，导致 `Visual.to_strips` 内部对 `None.render_strips` 崩溃（`AttributeError: 'NoneType' object has no attribute 'render_strips'`）。排查耗时很长是因为崩溃现象（`align`+`dock`+首次由 `display:none` 变为可见）看起来像是这些 CSS 属性组合的问题，实际和它们毫无关系——真正诱因是方法名冲突。**教训：自定义 Widget 子类的私有辅助方法一律避免使用 `_render`/`render` 或任何与 Textual 基类同名的下划线前缀方法**；已在 `update_toast.py` 顶部注释和方法命名（`_sync_display`）里固化这条教训，之后再新增浮层/自定义 Widget 时先检查方法名是否与 `textual.widget.Widget` 的现有方法（`render`/`_render`/`get_content_width` 等）冲突。
- 浮层定位手法：外层 `UpdateToast(Container)` 用 `layer: overlay; dock: bottom; align: right bottom;` 把自己锚到屏幕右下角（镜像 Textual 内置 `Toast`/`ToastRack` 的定位手法：单个 leaf widget 自身 docked 时无法把自己右对齐——`align` 只作用于容器的子节点，所以外层必须是容器）；不作为 `#list-pane` 子节点挂载，不受"侧边栏末行间隔"硬约定牵连；也不必显式声明 CSS `layers:` 列表，未声明的 `layer` 名称 Textual 会自动登记。"忽略"命中区（`_ToastClose`）的可见性完全靠 CSS 类选择器驱动（`UpdateToast.-available #toast-close { display: block; }`），不在 Python 侧用 `.display = bool` 直接改子节点样式，避免任何"父容器首次可见的同一拍内又改子节点显示状态"的时序脆弱点。
- 测试覆盖：`tests/test_updater.py`（版本比较、渠道判定、`fetch_latest` mock、忽略状态、`cli_update` 三条主路径）、`tests/test_update_toast.py`（Pilot 驱动的状态机与点击行为，纯 widget 级别）、`tests/test_main_screen_update.py`（Pilot 驱动的 `MainScreen` 接线：mock `updater` 让开发树也能覆盖"有新版本"路径、点击更新/重启/忽略的完整链路）、`tests/test_cli_restart.py`（`_restart_process()` mock `os.execv` 验证 re-exec 参数）。开发树里 `detect_channel()` 恒为 `"dev"`，因此 MainScreen/CLI 层的"有新版本"测试必须 mock `updater` 对外函数，不能依赖真实网络或真实渠道判定。

## 开源发布

- GitHub 公开仓库是 `https://github.com/x0c/pickup`，本地远端名为 `github`；原 `origin` 仍指向内部 Forgejo，用于同步备份。
- 项目历史版本线已经到 `v0.2.x`，新增公开发布版本必须沿现有标签递增，不能从 `0.1.0` 重新开始。
- 打包元数据只维护 `pyproject.toml`（`setuptools.packages.find`，`where = ["src"]`）。**不要**再引入 `setup.cfg` 双源。控制台入口：`pickup = "pickup.cli:main"`。
- `.github/workflows/test.yml` 必须先执行 `python -m pip install .` 再编译和跑测试，不能假设 GitHub Runner 预装运行依赖。项目从零依赖迁移到 Textual 后曾因 CI 只 checkout 源码就直接跑 `unittest`，导致 `rich`/`textual` 导入失败、Python 3.10–3.13 全矩阵同时报红；新增或调整依赖时要把“全新环境能按项目元数据安装”作为 CI 的第一道验证。
- 发布前至少构建一次 wheel，确认产物名形如 `pickup-<version>-py3-none-any.whl`，并用临时目录安装后检查 `pickup = pickup.cli:main` 入口元数据。
- 开源前隐私扫描要覆盖准备提交的文件和完整 Git 历史补丁内容；本机 `.git/config` 里的内部远端不进入仓库内容，但真实文件、历史提交、Release 说明和 README 不能包含密钥、个人路径、内网地址或占位符。
- GitHub Release 发布后检查 Actions、Release、topics 和仓库可见性；当前仓库 topics 为 `claude-code`、`codex-cli`、`terminal`、`tui`、`session-manager`、`ai-coding-agent`。

### 一键安装渠道

- Homebrew 配方在独立仓库 `x0c/homebrew-tap` 的 `Formula/pickup.rb`，本项目不维护该文件的本地副本；`Aliases/session-continue` 软链到 `pickup`，兼容改名前的 `brew install/upgrade x0c/tap/session-continue`。配方用 `Language::Python::Virtualenv` 直接从 GitHub 源码 tag 归档安装。**2026-07 界面层引入 `textual`（连带 `rich`/`markdown-it-py`/`mdurl`/`pygments` 等传递依赖）作为项目首个第三方运行时依赖，打破了此前"零运行时依赖，不需要 PyPI"的前提**——`Language::Python::Virtualenv` 需要为这些包各声明一个 `resource` 块（含下载 URL 和 sha256）才能离线可靠构建；下次发版同步配方时必须确认这些 `resource` 块已补上，不能只当作零依赖项目处理（本项目不维护配方文件，只能在发版流程里人工核对，或考虑用 `brew update-python-resources`/`homebrew-pypi-poet` 类工具生成）。tmux 成为硬依赖后配方需要声明 `depends_on "tmux"`——下次发版同步配方时确认这一条已加上。
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
python3 -m compileall -q src/pickup tests
python3 -m unittest discover -s tests -v
```

然后用真实会话列表检查前 120 条没有 raw slug、纯命令、省略号或自产标题 prompt；再用真实终端启动一次 TUI 并退出，确认本机 `pickup` 入口指向当前代码。改动会话扫描/预览逻辑时（不限于 Claude/Codex，OpenCode、Kimi 同样适用），至少随机抽查 5 条真实会话跑一遍 `scan_sessions`/`load_conversation`，断言没有空文本、字面量 `"None"`、角色标错或时间戳非单调，不能只信手写的单测小样例。
