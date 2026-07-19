# TUI 可观测性优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让维护者/Agent 在不猜的前提下定位 TUI 卡顿与后台故障：默认可查关键路径耗时与异常，按开关打开操作轨迹，并能一键导出当前界面截图。

**Architecture:** 在现有 `~/.cache/pickup/` + `_log_embed_error` 模式上抽出轻量 `observe.py`（无第三方指标栈）。默认只写结构化事件到滚动日志文件；`PICKUP_DEBUG=1`（或 `PICKUP_LOG=debug`）打开更细事件。截图走 Textual `save_screenshot` + 已有 `capture.py` 夹具路径，真机导出写缓存目录且默认不含真实对话正文（详情区可打码/跳过）。不引入 Prometheus/OpenTelemetry。

**Tech Stack:** 标准库 `logging`/`json`/`time`；现有 Textual Pilot / `App.save_screenshot`；落盘目录复用 `titles.CACHE_DIR`（`~/.cache/pickup/`）。

## Global Constraints

- pickup 是本地 CLI/TUI，不是常驻服务：**禁止**加 Prometheus / OTel / 网络上报。
- TUI 占住终端时 stderr 对用户不可见：诊断输出必须写文件（或退出后摘要），不能只靠 print。
- 隐私：默认日志/截图**不得**写入真实对话正文、完整提示词、API key；只记会话 id 前缀、runtime id、耗时、错误类型、计数。
- 日志写失败必须吞掉（与 `_log_embed_error` 同款），绝不能拖死抓帧/重扫线程。
- 零新第三方依赖；改动后同步 `docs/MAINTAINER_GUIDE.md`「可观测性」节与 `AGENTS.md` 本机入口相关说明。
- 默认关闭细日志（性能与噪音）；开启方式与现有 `PICKUP_DEBUG` 对齐并文档化。

---

## File map

| 文件 | 职责 |
|---|---|
| Create: `observe.py` | 事件 API、级别、滚动文件 sink、耗时 helper |
| Modify: `pickup.py` | `_log_embed_error` 改为走 observe；启动时初始化；可选退出摘要 |
| Modify: `ui/main_screen.py` | 重扫/重建/托管启动埋点；截图绑定 |
| Modify: `ui/embed_pane.py` | 抓帧循环异常与可选帧耗时埋点 |
| Modify: `docs/screenshots/capture.py` | 复用/导出「当前屏」辅助（若抽公共函数） |
| Create: `test_observe.py` | 级别、截断、隐私字段、耗时 |
| Modify: `test_ui.py` | 截图绑定 / debug 下事件出现 |
| Modify: `docs/MAINTAINER_GUIDE.md` | 可观测性节升级为「怎么用」 |
| Modify: `docs/SKILL.md` 或 `AGENTS.md` | Agent 排查时读哪些文件 |
| Modify: `pyproject.toml` / `setup.cfg` | 若新增顶层模块 `observe`，列入 `py-modules` |

---

### Task 1: `observe` 模块（文件事件日志）

**Files:**
- Create: `observe.py`
- Create: `test_observe.py`
- Modify: `pyproject.toml` / `setup.cfg`（`py-modules` 加 `observe`）

**Interfaces:**
- Produces:
  - `observe.init(*, debug: bool | None = None) -> None`
  - `observe.event(name: str, **fields) -> None` — 默认 info 级结构化一行 JSON
  - `observe.debug(name: str, **fields) -> None` — 仅 debug 开启时写入
  - `observe.timed(name: str, **fields)` — context manager，结束写 `duration_ms`
  - `observe.log_exception(where: str, exc: BaseException) -> None` — 替代/封装 `_log_embed_error`
  - 日志路径常量：`observe.EVENTS_LOG` → `~/.cache/pickup/events.log`；异常可继续写 `embed-error.log` 或合并进 `events.log`（实现时二选一，推荐：**异常仍写 embed-error.log 保持现状可读性，同时在 events.log 留一条无 traceback 的 error 事件**）

- [ ] **Step 1: 写失败测试** — `test_observe.py`：临时目录 patch `CACHE_DIR`；`event("scan_done", duration_ms=12, runtime_count=2)` 写入一行可 `json.loads` 的记录，含 `ts`/`name`；`debug(...)` 在未 init debug 时不落盘；超 256KB 截断或轮转后仍可追加。

- [ ] **Step 2: 跑测试确认 RED**

```bash
cd cli && python3 -m unittest test_observe -v
```

- [ ] **Step 3: 实现最小 `observe.py`** — 用标准库；字段白名单或显式禁止 `text`/`prompt`/`messages` 等键（传入则丢弃或改写为 `<redacted>`）；锁保护并发写。

- [ ] **Step 4: 测试 GREEN；把 `observe` 写入打包元数据**

- [ ] **Step 5: Commit**（仅当用户要求提交时再做）

---

### Task 2: 接上现有异常路径 + 启动初始化

**Files:**
- Modify: `pickup.py`（`_log_embed_error`、`main()` 启动处）
- Modify: `ui/embed_pane.py` / `ui/main_screen.py` 调用点可暂不改签名（继续调 `_log_embed_error`）
- Test: `test_observe.py` 或扩展现有 mock 测试

**Interfaces:**
- Consumes: Task 1 的 `observe.log_exception` / `observe.init`
- Produces: `_log_embed_error` 内部转调 observe，行为对外兼容

- [ ] **Step 1: 失败测试** — 调用 `_log_embed_error("抓帧", RuntimeError("x"))` 后 `events.log` 有 `name=error`/`where=抓帧`，且 `embed-error.log` 仍有 traceback（若保留双写）。

- [ ] **Step 2: RED → 实现 → GREEN**

- [ ] **Step 3: `main()` 在进 TUI 前 `observe.init(debug=bool(os.environ.get("PICKUP_DEBUG")))`；保留现有 OSC debug print 或改为 `observe.debug("osc_probe", ...)`**

- [ ] **Step 4: 跑 `python3 -m unittest test_observe test_ui.SessionCardVisualTests -v` 冒烟**

---

### Task 3: 关键路径耗时埋点（默认开启、低基数）

**Files:**
- Modify: `ui/main_screen.py`（后台重扫 `_background_refresh` / `_rebuild_list`）
- Modify: `pickup.py` 或 `runtime/registry.py`（`scan_all` 边界，优先在调用方埋点避免污染适配器）
- Modify: `ui/embed_pane.py`（抓帧循环：可选每 N 秒一条 `capture_tick`，或仅慢帧 `duration_ms > 阈值` 时写）
- Test: `test_observe.py` 或 `test_ui.py` 用 mock store + Pilot 触发一次 rebuild，断言 events 里出现 `list_rebuild` / `scan_all`

**事件名约定（固定字符串，便于 grep）：**
- `scan_all` — `duration_ms`, `session_count`（不要逐会话标题）
- `list_rebuild` — `duration_ms`, `mode=in_place|full`, `card_count`
- `host_session` — `duration_ms`, `runtime`, `ok|error`
- `capture_slow` — 仅当单次抓帧+解析超过阈值（建议 100ms）才写：`duration_ms`, `session_prefix`

- [ ] **Step 1: 为 `list_rebuild` 写失败测试（mock 时钟或注入 observe 记录器）**

- [ ] **Step 2: 实现埋点 → GREEN**

- [ ] **Step 3: 手动验证**

```bash
cd cli && PICKUP_DEBUG=1 python3 pickup.py --limit 5
# 退出后：
tail -n 20 ~/.cache/pickup/events.log
```

---

### Task 4: 一键导出当前 TUI 截图

**Files:**
- Modify: `ui/main_screen.py` — Binding（建议 `ctrl+shift+s` 或 `ctrl+p` 需避开现有 `p` 固定面板；推荐 **`f12`** 或 **`ctrl+y`**，实现前在 BINDINGS 表确认无冲突）
- Modify: `observe.py` 或小函数 `observe.save_tui_screenshot(app) -> Path`
- Modify: `docs/screenshots/capture.py` — 文档字符串指向真机热键与输出路径
- Test: `test_ui.py` Pilot：按绑定后目标路径存在非空 SVG/PNG

**行为：**
- 输出到 `~/.cache/pickup/screenshots/tui-YYYYMMDD-HHMMSS.svg`（可选再转 PNG；PNG 依赖 cairosvg，失败则只留 SVG 并 `observe.event("screenshot", format="svg_only")`）
- Footer 或 header 短暂提示路径（Textual notify）
- **隐私：** 默认截图可以是当前真实画面（用户主动触发=知情同意）；文档写明「勿把含真实对话的截图提交进仓库」。夹具截图仍只用 `capture.py`。

- [ ] **Step 1: Pilot 失败测试（绑定存在 + 调用 save 被触发）**

- [ ] **Step 2: 实现 → GREEN**

- [ ] **Step 3: 真机按一次热键，确认文件可读；用读图工具看一眼（配色仍以真机为准，SVG 灰阶已知限制写进注释）**

---

### Task 5: Agent/人可读的排查入口（薄封装）

**Files:**
- Modify: `agent_api.py` 或 `pickup.py` 子命令 — 优先 **`pickup diagnose`（只读）**：打印 cache 目录、`events.log`/`embed-error.log` 是否存在与末尾 N 行路径提示、`runtime_label_style` 自检、tmux 版本；**不**启动 TUI
- Modify: `docs/SKILL.md` — 增加「界面异常时先跑 `pickup diagnose`，再读 `~/.cache/pickup/events.log`」
- Modify: `docs/MAINTAINER_GUIDE.md`「可观测性」节 — 换成「怎么用」表格（开关、路径、事件名、热键）
- Test: `test_agent_api.py` 或新测：`diagnose` 返回 ok JSON envelope（与现有 agent_api 风格一致）或纯文本 + exit 0

**Interfaces:**
- 若走 agent_api：`{"ok": true, "data": {"cache_dir", "events_log", "embed_error_log", "tmux", "debug"}}`，遵守只读约束

- [ ] **Step 1: 失败测试（子命令存在且只读）**

- [ ] **Step 2: 实现 → GREEN**

- [ ] **Step 3: 更新 MAINTAINER_GUIDE / SKILL / AGENTS 导航描述（含「排查 TUI / 可观测性」触发词）**

- [ ] **Step 4: 全量单测**

```bash
cd cli && python3 -m unittest -v
```

---

### Task 6: 收尾验收清单（实现者自测）

- [ ] `PICKUP_DEBUG` 未设置时：正常使用 TUI，`events.log` 只有低基数事件（scan/rebuild/host/error/slow），无对话正文
- [ ] `PICKUP_DEBUG=1`：同路径下出现更细 debug 事件；体积可控
- [ ] 人为在抓帧路径抛错：`embed-error.log` 有 traceback，`events.log` 有 error 事件
- [ ] 截图热键写出文件；`pickup diagnose` 能指出路径
- [ ] `pip install --user --force-reinstall --no-deps .` 后入口含 `observe` 模块（`python3 -c "import observe; print(observe.__file__)"`）
- [ ] 首屏 `scan_all(50)` 耗时仍按 AGENTS 要求实测汇报（本改动不应明显恶化）

---

## 明确不做（本计划范围外）

- Prometheus / Grafana / 远程遥测
- 默认把每次按键/每次滚轮写入日志
- 自动把真实会话截图提交 CI 或仓库
- 替换 Textual SVG 真彩色导出限制（已知问题，验收靠真机/`render_line`）

## 建议执行顺序

Task 1 → 2 → 3 → 4 → 5 → 6。每完成一个 Task 应可独立合并试用；Task 4/5 互不阻塞，可并行（若用 subagent）。

## 执行方式

实现时用 **subagent-driven-development** 或 **executing-plans**，按 checkbox 推进；不要一次改完再测。
