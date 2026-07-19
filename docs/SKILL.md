---
name: pickup
description: Query local Claude Code, Codex CLI, OpenCode, and Kimi Code CLI session history through the `pickup` CLI — list recent sessions, search by topic, read a session's conversation, or build a handoff context package to continue interrupted work. Read-only, no side effects.
---

# pickup：本地编程会话数据接口

`pickup` 扫描本机 `~/.claude/projects/`、`~/.codex/sessions/`、`~/.kimi-code/sessions/`、`~/.cursor/chats/` 和 OpenCode 的
SQLite 数据库（`~/.local/share/opencode/opencode.db`，只读打开）下的会话历史，为大模型 Agent
提供结构化查询命令。**这些命令只读、无副作用**：不会拉起新会话、不会自动接续任务、不会修改
任何历史文件。拿到数据之后要做什么（继续任务、汇总给用户、转发给另一个 Agent）由调用方决定。

所有命令输出统一 JSON envelope，写到 stdout：

```json
{"ok": true, "data": {...}, "error": null, "meta": {"version": 1}}
```

失败时 `ok` 为 `false`，`data` 为 `null`，`error` 包含：

- `code`：程序可判断的错误分类（`usage_error` / `not_found` / `ambiguous` / `history_unavailable`）
- `message`：人类可读的错误说明
- `hint`：建议的排查方向
- `next_commands`：可以直接执行的后续命令列表

退出码：`0` 成功、`1` 一般失败、`2` 用法错误（参数不对）、`3` 会话不存在、`5` 会话标识有歧义。
不要只看 stdout 是否有内容来判断成功，检查退出码或 `ok` 字段。

## 命令

跑 `pickup describe` 获取全部命令的机器可读参数说明（与实现同源，不会漂移）；
`pickup describe <command>` 看单个命令的完整参数和输出字段。

| 命令 | 用途 |
| --- | --- |
| `pickup list [--runtime R] [--limit N] [--top N] [--compact] [--status S] [--cwd 子串] [--live] [--fields a,b]` | 结构化列出会话 |
| `pickup search <关键词...> [--deep] [--runtime R] [--limit N] [--top N] [--compact] [--live] [--fields a,b]` | 按主题找会话 |
| `pickup show <会话> [--messages N \| --full] [--compact] [--out 路径] [--fields a,b]` | 会话详情 + 对话内容 |
| `pickup context <会话>` | 生成接续该会话所需的上下文数据包 |
| `pickup plan continue <会话> --instruction <文本>` | 生成带新指令的非交互式原生续接计划；只返回数据，不执行 |
| `pickup describe [command]` | 查看命令 / 参数 / 输出字段说明 |
| `pickup diagnose` | 只读诊断：events.log / embed-error.log / 截图目录 / tmux / 配色自检；不启动 TUI |

### 会话标识（`<会话>` 参数）

支持完整会话 ID、ID 前缀（如 `8892cd3d`）、或带运行时限定的 `runtime:id`（如 `claude:8892cd3d`、
`opencode:ses_0ae26219`、`kimi:session_ef8275b0`、`cursor:<chat-uuid>`）。
前缀在多个运行时之间重复时会返回退出码 5（`ambiguous`），`error.next_commands` 里给出具体候选
的 `pickup show runtime:id` 命令，照着执行即可消歧。

## 典型流程

**按主题找到会话并接续未完成的工作：**

```bash
pickup search 天气 app                    # 快速搜标题/首尾消息/工作目录
pickup search 天气 app --top 3 --compact  # 只取最相关的 3 条，减少 token
pickup search 天气 app --deep             # 快速搜没结果时，搜全部对话内容（较慢）
pickup show <候选会话的 short_id>          # 确认是不是要找的那次会话
pickup show <候选会话> --full --out /tmp/pickup-session.json  # 完整大结果落盘，stdout 只返回引用
pickup context <会话>                     # 拿到 history_path / suggested_prompt / resume_command
pickup plan continue <会话> --instruction "继续完成剩余工作并汇报结果" # 只生成外部执行器可运行的计划
```

`pickup context` 返回的 `resume_command` 是同运行时原生恢复该会话的 shell 命令（可能为 `null`，
比如原历史文件已被删除）；`suggested_prompt` 是跨运行时接力时可以直接复用的首条提示词，内含
原会话历史文件路径、格式提示和从原会话自动提取的对话摘录（原始需求 + 最近数条对话，
截断版）——摘录给目标运行时一个任务与进展锚点，原始历史文件仍是权威来源，目标运行时应以摘录
为线索去读取历史、判断已完成和未完成的部分。

`pickup plan continue` 用于调用方需要把同运行时续接交给后台执行器时。它会依据运行时适配器返回
`session_ref`、`runtime`、原 `cwd`、能力边界和 `launch`；其中 `launch.argv` 是**参数数组**，新指令
作为一个独立元素保留，调用方必须以 `execve` / `subprocess` 的 argv 形式执行，不能重新拼成 shell
字符串。`launch.cwd` 为 `null` 时表示原目录已不可用，调用方应自行拒绝或选择安全的工作目录。

该命令同样只读：不会启动 CLI、不会写入原会话历史、不会创建新会话，也不会消耗模型额度。返回的
`capabilities.execution=external_only` 明确表示执行责任属于调用方；`pickup` 不提供执行、停止或向运行中
会话下发消息的能力。内置 Claude/Codex 计划使用各自的非交互式原生续接入口，以便外部后台任务接收
完成输出；人类在 TUI 中的原生交互恢复行为不受影响。

**列出某个项目最近的会话：**

```bash
pickup list --cwd my-weather-app --limit 20
pickup list --cwd my-weather-app --limit 80 --top 5 --compact
```

**只要还没回复的会话：**

```bash
pickup list --status pending
```

## 管家编排典型流程

**找到当前正在运行的 CodingAgent,判断能不能直接下指令:**

```bash
pickup list --live --compact                 # 一步拿到「现在有哪些会话进程真的在跑」
pickup list --live --status pending --compact # 更进一步：正在跑、且已在等你回复的
```

`live` 是按进程真实判活（pid 存活/写文件锁定），不是文件时间推断；`status` 是最后一轮角色推断的
会话内容状态。两者组合判断能不能打扰：`live=true` 且 `status=done` 说明进程还开着但已经把当前
任务处理完，可以直接接着聊；`live=true` 且 `status=pending` 说明正忙着等你回复，插话前先看
`last_user`/`last_agent` 摘要搞清楚它在问什么。`live=false` 的会话只能走 `resume_command` 重新
拉起一个进程，不能"接管"——pickup 不提供向运行中进程注入输入的能力（本文档开头即说明：pickup 只读、无
副作用,拿到数据后要做什么由调用方决定）。

`keepalive=true` 的会话额外要注意：它的进程挂在 pickup 的后台保活层里，`live` 通常也是 `true`，但
`resume_command` 此时不安全——执行它会另起一个和保活进程抢同一份会话文件的新进程。这种会话只能
由人类回到 `pickup` TUI 按 Enter 接回现场，管家 Agent 判断"能不能直接下指令"时应把 `keepalive=true`
当作"这条会话已经有人在管，只能提示人类去接，不要建议或代为执行 resume_command"的信号。

`list`/`search` 默认输出已经带 `last_user`/`last_agent`（最近一轮真人消息和助手回复，硬截断精简），
多数情况下看这两个字段就够判断"这条会话在干嘛"，不必为每条候选都跑一次 `pickup show`。

## 字段说明要点

- `title`：只读已生成的标题缓存或本地兜底标题，不会触发新的标题生成（不花账号额度）。
- `live` / `pid`：`live` 是运行中会话的进程是否真实存活（Claude 用 pid 注册表 + `os.kill`，Codex
  用 `pgrep`+`lsof` 探测持有对应会话文件的进程，OpenCode 与 Kimi 用 `pgrep` 找进程后读取其工作目录
  与会话 `cwd`/`workDir` 匹配——历史不在可独占定位的单文件里，同目录下多个历史会话
  只有最新一条会被标记存活），不是根据文件时间猜的。`pid` 只在 `live=true`
  时非空，供调用方定位/给该进程发信号；pickup 本身不提供拉起/接管等副作用命令，`pid` 只是可见性。
- `keepalive`：会话是否正挂在 pickup 自己的后台保活（tmux）里（人类在 `pickup` TUI 里用 Enter/`a` 启动会话时
  默认会经过这层，SSH 断开也不中断）。为 `true` 时该会话的进程实际跑在保活层里，`resume_command`
  会另起一个直接抢同一份会话文件的新进程，不应该在这种情况下使用；pickup 不提供从命令行直接"接管"
  保活会话的能力（这属于交互式 TUI 的 Enter 键行为），调用方需要接管时只能提示人类回到 `pickup` TUI 操作。
- `last_user` / `last_agent`：最后一条真人消息和助手最后一轮回复的硬截断摘要（约 120 字），用于
  快速判断会话在干嘛；需要完整对话仍然用 `pickup show`。
- `status`：英文枚举 `done` / `pending` / `aborted` / `unknown`，程序判断用这个字段，不要解析
  `status_tag`（中文 + emoji，只给人看）。
- `resumable` / `resume_command`：是否能生成同运行时原生恢复命令，以及可直接执行的恢复命令；这些字段
  只基于扫描结果和运行时适配器生成，不会额外读取会话全文。
- `session_ref` / `launch`：仅 `plan continue` 返回。`session_ref` 是带运行时的唯一标识；`launch.argv`
  是不经 shell 解释的启动参数数组，`launch.cwd` 是推荐工作目录。不要把 `argv` 拼为 `resume_command`
  风格的字符串，也不要让 `pickup` 代为执行。
- `score` / `matched_via` / `matched_fields`：仅 `search` 返回。`score` 是相关性分数，排序先按分数倒序，
  再按更新时间倒序；`matched_via` 是 `quick` 或 `deep`，兼容旧调用方；`matched_fields` 是命中的字段
  列表，如 `title`、`first_user_msg`、`conversation`。
- `mtime`：Unix 时间戳，按更新时间排序或做"最近"过滤时用这个，不要解析 `time` 的人类可读格式。
- `--limit` 控制的是**扫描深度**（每个运行时最多看多少条历史），不是"最多返回几条"——过滤条件
  （`--status`、`--cwd`、关键词）是在扫描出的这批里再筛选，如果确定目标会话较早，适当调大
  `--limit`（`show`/`context` 默认扫描深度是 200，比 `list`/`search` 的 50 更大）。
- `--top` 才是**结果数量上限**。给 Agent 调用时推荐同时传 `--limit` 和 `--top`：前者决定找多深，
  后者控制 stdout 体积。
- `--compact` 会输出无缩进 JSON；`list`/`search`/`show` 还会默认裁剪到常用字段。需要精确字段时用
  `--fields` 覆盖（`list`/`search`/`show` 均支持）——`--compact` 单独使用时的默认字段集不含
  `cwd`/`pid`，只要调用方需要这两项（如判断会话在哪个目录、能否对运行中进程发信号），必须显式传
  `--fields` 指名，不能只传 `--compact` 就假设拿得到。
- `show --full` 可能很大；需要完整历史时优先加 `--out <path>`，stdout 会只返回输出文件路径、字节数
  和消息数量，完整 JSON envelope 写在该文件里。

## 与 OpenConductor 项目关联

`pickup` 只提供 `cwd` / `cwd_display`（会话当时的原始工作目录），**不提供、也不应该提供**任何
"项目 ID"字段：OpenConductor 的项目标识是项目根目录（通常是含 `.git` 的目录）绝对路径的 SHA1
摘要（形如 `proj_a1b2c3d4e5f6a7b8`），既算不出来（`pickup` 不知道 OpenConductor 的项目扫描结果），
也不能直接由 cwd 推导（会话当时的 cwd 可能是项目子目录，不等于项目根目录）。

调用方（如需要把某条会话关联到 OpenConductor 已注册项目的管家 Agent）应该：

1. 调 `oc projects --json`，拿到项目列表，每项含 `id`（`proj_` 前缀的项目标识）和 `path`（项目根目录绝对路径）。
2. 用 `pickup` 返回的 `cwd` 去匹配：`cwd` 等于某个 `path`，或 `cwd` 位于该 `path` 之下（前缀匹配），命中的
   那一项的 `id` 就是要用的项目标识。
3. 不要在 `pickup` 侧本地计算或猜测这个 ID——匹配逻辑属于调用方职责，不属于 `pickup`。

## 非 Agent 用法

不带子命令直接运行 `pickup` 会打开交互式终端 TUI，需要真实终端，供人类手动选择会话；在非真实
终端环境下会自动退化为 JSON 列表（等价于 `pickup list`）。旧版 `pickup --json` 参数仍然保留，但字段
少于 `pickup list`（没有 `status` 英文枚举、`short_id` 等），新集成建议直接用本文档的子命令。

## 界面异常排查（只读）

TUI 卡顿、侧边栏不刷新、内嵌面板异常时：

1. 先跑 `pickup diagnose`，确认 `events_log` / `embed_error_log` 路径与是否存在。
2. 读 `~/.cache/pickup/events.log`（JSON 行：`scan_all` / `list_rebuild` / `host_session` / `capture_slow` / `error`）。
3. 读 `~/.cache/pickup/embed-error.log` 看后台线程 traceback。
4. 真机 TUI 内按 **F12** 导出当前画面到 `~/.cache/pickup/screenshots/`（勿把含真实对话的截图提交仓库）。
5. 需要细日志时设 `PICKUP_DEBUG=1` 后重启 TUI。
