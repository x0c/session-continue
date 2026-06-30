# sc — 终端会话接力工具

列出 Claude Code / Codex 最近的会话历史，挑一条直接接管终端续接进去。
默认通过原运行时原生恢复（`claude --resume` / `codex resume`）；也可以打开
高级操作，让另一个运行时读取原始 JSONL 历史并新建会话接力未完成工作。

取代了原来的两个 skill（`claude-session-continue` / `codex-session-continue`）：
skill 跑在某个 agent 会话内部，没法把终端交给一个全新的交互式进程；
`sc` 是独立终端程序，回车后用 `os.execvp` 把自己换成 claude/codex，
终端无缝交接。

## 用法

```bash
sc                # 启动 TUI，默认每个来源最多列 50 条
sc --limit 30      # 调整列表条数
```

## 键位

| 键 | 作用 |
|---|---|
| `↑` / `↓` / `j` / `k` | 上下选择会话 |
| `←` / `→` / `Tab` | 在 Claude / Codex 两个来源间切换 |
| `Enter` | 使用原运行时原生恢复选中会话 |
| `a` | 打开高级操作，选择任意已注册运行时；选择其他运行时会读取历史后新建会话接力 |
| `q` | 退出（**不要用 ESC 退出**——非阻塞输入模式下 ESC 会和方向键的转义序列冲突，所以退出键只绑了 `q`） |

## 跨运行时接力

跨运行时接力不会伪造或改写原会话，也不能把两种运行时的私有会话格式直接互转。
`sc` 会把原会话标题、工作目录、历史文件路径和格式提示组织成统一接力信息，再作为
目标运行时新会话的首条提示词。目标 Agent 会先按需读取历史、核对工作区状态，然后
继续最后一个未完成任务。原会话和目标运行时新会话会分别保留。

历史文件路径只作为命令参数传递，不会把整份历史复制进启动参数。不同运行时的会话
ID 使用“运行时 + ID”作为内部唯一键，避免未来接入更多运行时时发生标题或状态覆盖。

## 标题怎么来的

统一策略：

1. **生成标题才是正式标题**：Claude Code 自带 `aiTitle` 不稳定，可能是机器 slug 或一次性命令，不能直接作为产品展示标题。
2. **缓存优先**：按会话内容大小做指纹，缓存到 `~/.cache/session-continue/titles.json`；命中有效生成标题时直接展示，不重复花钱。展示时间变化不应导致标题缓存失效。
3. **临时兜底**：缓存缺失时，先从有信息量的用户意图或助手摘要里取临时标题，界面立即可读。`继续`、`快点`、`在吗`、`Continue from where you left off`、`No response requested`、额度错误、自产标题 prompt、系统角色提示等噪音不会进入兜底标题。
4. **脱离终端的后台进程生成**：进入 TUI 时拉起一个独立后台进程（`sc --generate-titles`）批量生成标题。它用 `start_new_session` 脱离当前终端：用户秒退、按 `q` 退出或回车原生恢复（`execvp` 替换进程）后，这个进程都继续把标题生成完并写入缓存。TUI 只读缓存、轮询缓存文件，把后台逐批写入的新标题刷到界面，自身不调用 `claude`、不写缓存。
5. **统一生成 + 单实例**：Claude 和 Codex 都走 `claude -p --model haiku` 批量生成中文标题；后台进程用文件锁（`~/.cache/session-continue/titles.lock`）保证全机单实例，反复进 sc 不会堆积多个生成进程重复花钱。生成范围是当前 `--limit` 内的会话，不再随屏幕滚动追加。

列表优先按会话文件更新时间倒序排列，排序优先用文件更新时间。时间列在 TUI 里按人性化相对时间展示（`刚刚` / `x 分钟前` / `x 小时前`，超过一天退回 `MM-DD HH:MM`），`--json` 输出仍是稳定的绝对 `display_time` + `mtime` 时间戳。若检测到一批历史 Claude 会话在同一分钟被批量 touch / 同步刷新，说明文件 mtime 已被污染，这批会话会回退到会话内部最后事件时间排序和展示。

⚠️ 标题生成会真实消耗 Claude 账号额度。缓存 + 单实例后台进程 + 批量调用共同压低成本：每批最多 5 条会话拼一个 prompt，只发一次 `claude -p`，逐批落盘以便 TUI 尽早可见。

## 文件结构

| 文件 | 职责 |
|---|---|
| `sc.py` | 入口：curses TUI + 显示列宽对齐 + 键位 + 拉起后台标题生成进程并轮询缓存刷新 + 回车接管终端；隐藏的 `--generate-titles` 模式即脱离终端的后台生成进程入口 |
| `models.py` | 跨运行时统一的会话键、接力信息、启动请求和启动计划 |
| `runtime/base.py` | 运行时适配器抽象，定义扫描、原生恢复、导出接力和新会话启动能力 |
| `runtime/registry.py` | 运行时注册表、跨运行时接力编排和最终进程替换 |
| `runtime/claude.py` | Claude Code 扫描与启动适配器 |
| `runtime/codex.py` | Codex CLI 扫描与启动适配器 |
| `scan_claude.py` | 扫描 `~/.claude/projects/`，head+tail 快扫，提取 cwd / aiTitle / 会话更新时间 |
| `scan_codex.py` | 扫描 `~/.codex/sessions/`，提取 cwd / 末轮状态 / 会话更新时间 |
| `titles.py` | 标题缓存 + Claude/Codex 批量大模型生成 |

### 新增运行时

新增 Gemini CLI、OpenCode 等运行时时，实现一个 `BaseRuntime` 适配器并在
`runtime/registry.py` 的 `default_registry()` 注册一次即可。界面来源标签、高级操作列表、
会话扫描和跨运行时接力编排都会自动接入，不需要为 Claude→Gemini、Codex→Gemini 等
组合分别编写转换逻辑。

## 维护注意事项

- 排序和展示时间必须优先用会话文件更新时间。用户对“最近会话”的直觉是“最近被续接/写入的会话”，不是文件内部最后一条可解析消息的时间。
- 文件更新时间不是绝对可信：Syncthing、复制或批量元数据刷新会让一批历史 Claude 会话拥有同一个 mtime/ctime。扫描侧必须检测“同一分钟 5 条以上且 ctime 接近 mtime”的批量 touch 簇；簇内文件时间比内部最后事件时间晚超过 1 小时时，改用内部最后事件时间，避免 TUI 前排出现一堆相同假时间。
- Claude 的 `aiTitle` 不一定在第一条用户消息之前出现，常见位置是开头若干条附件/工具声明之后；扫描头部不能拿到 cwd 和首条用户消息就立即停止，否则会漏标题。
- Claude 的 `/plan` 等本地命令会把真实用户需求放在 `<command-args>...</command-args>` 里；这类内容必须提取为用户意图，不能因为整段消息以 `<command-name>` 开头就跳过。
- Claude 的兜底标题必须走候选评分，禁止直接写成 `last_prompt or first_user_msg`。短续接词、催促、系统提示、错误消息和自产标题 prompt 都是低价值候选；如果用户侧全是低价值消息，才允许用最后助手摘要兜底。
- Claude 的 `aiTitle` 只能作为临时兜底的最后来源，不得绕过生成缓存直接展示；raw slug、纯命令和省略号都不能成为正式标题。
- 后台生成标题会留下新的 Claude 会话记录；扫描侧必须过滤自产标题 prompt 和只有低价值消息的记录，避免历史污染反过来出现在列表里。
- 标题生成只做批量调用；批量失败时保留临时标题，不逐条慢重试。逐条重试会让 TUI 后台刷新卡到数分钟，还会放大额度消耗。
- 标题缓存是用户可见体验的一部分，测试不得写真实 `~/.cache/session-continue/titles.json`。生成失败时也不得保存空缓存；已有旧标题即使指纹过期，也要先展示旧标题并后台刷新，不能退回长原文。
- 无缓存时必须先生成本地短标题，再交给后台模型优化。首屏不能依赖 `claude -p` 是否及时返回；模型慢或失败时，TUI 仍应显示可读短标题。
- TUI 表格列宽必须按终端显示宽度计算，不要用 Python 字符数直接 `ljust`/切片；中文、箭头和状态图标会占 2 个显示列，按字符数处理会导致列漂移。表格列之间用 `sc.py` 里的 `COL_GAP` 固定间隔拼接，不要让相邻列的内容直接贴在一起（尤其是右对齐列，右对齐会吃掉天然的尾部空格）。
- 大小列统一用 MB、保留两位小数、右对齐（`_format_size` + `_fit_cell_right`），不要按 KB/MB 动态切换单位，否则同一列里数字位数不一致、看着比实际更乱。
- 状态列是所有运行时共用的统一枚举，定义在 `titles.py`：`STATUS_ABORTED`（⚠️已中断）> `STATUS_PENDING`（⏳待回复）> `STATUS_DONE`（✅已完成）> `STATUS_NONE`（空，末轮角色不可判定）。新增状态判定逻辑时复用这四个常量，不要在各扫描器里分别维护字符串，否则运行时之间的标签和优先级会逐渐分叉。Claude 侧通过 tail 消息里的 `[Request interrupted by user]` 这一精确字符串识别中断，不要用宽松的关键词匹配，避免误把真实用户消息当成中断标记。
- 改标题、排序或列宽后必须做真实路径验证：除 `python3 -m unittest -v` 和 `python3 -m py_compile ...` 外，还要用 `SessionStore` 或 `scan_claude.scan_sessions` 检查真实会话列表，确认前 120 条没有 raw slug、纯命令、`...`、自产标题 prompt；再用 `script -q /tmp/sc.capture -c 'python3 sc.py --limit 20'` 启动一次真实 TUI 并退出，确认 `sc` 路径吃到当前代码。
- `titles.py` 的 `_compact_title` 里几个 `re.sub` 用的是原始字符串（`r"..."`），写 `\s`/`\S`/`\w`/`\d`/`\n` 时手不能多打一个反斜杠——`r"\\n"` 在正则里是「字面反斜杠 + 字面字母 n」，不是真正的转义序列。之前这个笔误让分隔候选标题的正则把任何含字母 "n" 的词（典型如 `OpenConductor`）从中间切断，产出"我是 Ope"这类乱码标题。改这个函数前先用真实会话文本跑一遍 `titles._compact_title(text)`，确认输出是完整的人类可读片段，而不是看起来通过了编译就当作正确。
- 标题生成必须由脱离当前终端的独立进程承载，不能放回 TUI 进程内的线程。`execute_launch` 用 `os.execvp` 整体替换进程映像（原生恢复），按 `q` 退出也会结束进程——任何 TUI 内线程都会被立即杀死。用户进 sc 往往只停留几秒，线程模型会让没跑完的标题永远生成不出来。当前模型是：`_spawn_title_daemon` 用 `subprocess.Popen(..., start_new_session=True)` 拉起 `sc --generate-titles` 后台进程，它独立成新会话/进程组，父进程退出或被 execvp 替换都不受影响；进程用 `~/.cache/session-continue/titles.lock` 的 `flock(LOCK_NB)` 单实例化，拿不到锁就静默退出。TUI 侧 `SessionStore` 只读缓存：`load` 时把无可用缓存标题的会话放进 `generating`（转圈圈），`poll_cache_updates` 按缓存文件 mtime 变化重读、把已产出的标题刷上去并停掉转圈圈。改这块时不要为「实时性」把生成又塞回 TUI 线程，否则会退回到「秒退即丢标题」的老问题。
- 界面和接力编排禁止新增 `if source == "claude"` 一类运行时分支。运行时私有扫描格式、恢复参数和新会话参数必须留在对应适配器中；公共流程只依赖注册表和统一接力模型。

## 多机安装

每台机器跑一次（`~/Codes` 本身随 Syncthing 多机同步，代码不用重复改）：

```bash
mkdir -p ~/.local/bin
ln -sf ~/Codes/Python/session-continue/sc.py ~/.local/bin/sc
chmod +x ~/Codes/Python/session-continue/sc.py
```

确保 `~/.local/bin` 在 `PATH` 中。

## 已知环境坑

- 终端 `keypad` 转义序列解码依赖 `TERM` 对应的 terminfo；正常 `xterm-256color`
  终端下方向键是 `ESC [ A/B/C/D`，不会有问题。
- Claude 会话跨机时 `cwd` 可能是别的系统的路径（例如 Mac 路径在 Linux 节点上不存在），
  这种情况下 `sc` 会跳过 `cd`，只靠 `--resume <id>` 续接。
