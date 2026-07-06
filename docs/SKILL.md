---
name: session-continue
description: Query local Claude Code and Codex CLI session history through the `sc` CLI — list recent sessions, search by topic, read a session's conversation, or build a handoff context package to continue interrupted work. Read-only, no side effects.
---

# sc：本地编程会话数据接口

`sc` 扫描本机 `~/.claude/projects/` 和 `~/.codex/sessions/` 下的会话历史，为大模型 Agent
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

跑 `sc describe` 获取全部命令的机器可读参数说明（与实现同源，不会漂移）；
`sc describe <command>` 看单个命令的完整参数和输出字段。

| 命令 | 用途 |
| --- | --- |
| `sc list [--runtime R] [--limit N] [--top N] [--compact] [--status S] [--cwd 子串] [--fields a,b]` | 结构化列出会话 |
| `sc search <关键词...> [--deep] [--runtime R] [--limit N] [--top N] [--compact] [--fields a,b]` | 按主题找会话 |
| `sc show <会话> [--messages N \| --full] [--compact] [--out 路径]` | 会话详情 + 对话内容 |
| `sc context <会话>` | 生成接续该会话所需的上下文数据包 |
| `sc describe [command]` | 查看命令 / 参数 / 输出字段说明 |

### 会话标识（`<会话>` 参数）

支持完整会话 ID、ID 前缀（如 `8892cd3d`）、或带运行时限定的 `runtime:id`（如 `claude:8892cd3d`）。
前缀在多个运行时之间重复时会返回退出码 5（`ambiguous`），`error.next_commands` 里给出具体候选
的 `sc show runtime:id` 命令，照着执行即可消歧。

## 典型流程

**按主题找到会话并接续未完成的工作：**

```bash
sc search 天气 app                    # 快速搜标题/首尾消息/工作目录
sc search 天气 app --top 3 --compact  # 只取最相关的 3 条，减少 token
sc search 天气 app --deep             # 快速搜没结果时，搜全部对话内容（较慢）
sc show <候选会话的 short_id>          # 确认是不是要找的那次会话
sc show <候选会话> --full --out /tmp/sc-session.json  # 完整大结果落盘，stdout 只返回引用
sc context <会话>                     # 拿到 history_path / suggested_prompt / resume_command
```

`sc context` 返回的 `resume_command` 是同运行时原生恢复该会话的 shell 命令（可能为 `null`，
比如原历史文件已被删除）；`suggested_prompt` 是跨运行时接力时可以直接复用的首条提示词，内含
原会话历史文件路径和格式提示，交给目标运行时自己去读取历史、判断已完成和未完成的部分。

**列出某个项目最近的会话：**

```bash
sc list --cwd my-weather-app --limit 20
sc list --cwd my-weather-app --limit 80 --top 5 --compact
```

**只要还没回复的会话：**

```bash
sc list --status pending
```

## 字段说明要点

- `title`：只读已生成的标题缓存或本地兜底标题，不会触发新的标题生成（不花账号额度）。
- `status`：英文枚举 `done` / `pending` / `aborted` / `unknown`，程序判断用这个字段，不要解析
  `status_tag`（中文 + emoji，只给人看）。
- `resumable` / `resume_command`：是否能生成同运行时原生恢复命令，以及可直接执行的恢复命令；这些字段
  只基于扫描结果和运行时适配器生成，不会额外读取会话全文。
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
  `--fields` 覆盖（`show` 当前不支持自定义字段，只支持 compact 默认字段）。
- `show --full` 可能很大；需要完整历史时优先加 `--out <path>`，stdout 会只返回输出文件路径、字节数
  和消息数量，完整 JSON envelope 写在该文件里。

## 与 OpenConductor 项目关联

`sc` 只提供 `cwd` / `cwd_display`（会话当时的原始工作目录），**不提供、也不应该提供**任何
"项目 ID"字段：OpenConductor 的项目标识是项目根目录（通常是含 `.git` 的目录）绝对路径的 SHA1
摘要（形如 `proj_a1b2c3d4e5f6a7b8`），既算不出来（`sc` 不知道 OpenConductor 的项目扫描结果），
也不能直接由 cwd 推导（会话当时的 cwd 可能是项目子目录，不等于项目根目录）。

调用方（如需要把某条会话关联到 OpenConductor 已注册项目的管家 Agent）应该：

1. 调 `oc projects --json`，拿到项目列表，每项含 `id`（`proj_` 前缀的项目标识）和 `path`（项目根目录绝对路径）。
2. 用 `sc` 返回的 `cwd` 去匹配：`cwd` 等于某个 `path`，或 `cwd` 位于该 `path` 之下（前缀匹配），命中的
   那一项的 `id` 就是要用的项目标识。
3. 不要在 `sc` 侧本地计算或猜测这个 ID——匹配逻辑属于调用方职责，不属于 `sc`。

## 非 Agent 用法

不带子命令直接运行 `sc` 会打开交互式终端 TUI，需要真实终端，供人类手动选择会话；在非真实
终端环境下会自动退化为 JSON 列表（等价于 `sc list`）。旧版 `sc --json` 参数仍然保留，但字段
少于 `sc list`（没有 `status` 英文枚举、`short_id` 等），新集成建议直接用本文档的子命令。
