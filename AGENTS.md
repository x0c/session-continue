# session-continue 项目规范

## 文档导航

- `README.md`：使用、修改、评审或扩展会话扫描、终端界面、标题生成、运行时适配和跨运行时接力前必读。
- `docs/MAINTAINER_GUIDE.md`：修改、评审、排查或优化标题生成、扫描排序、扫描性能/启动耗时、界面列宽、配色/可读性（含浅色背景终端适配）、状态列/会话存活判断、会话预览、运行时边界、打包发布和 GitHub 开源维护前必读。
- `docs/SKILL.md`：修改、评审 `agent_api.py` 面向 Agent 的子命令、字段或退出码语义前必读；这是 Agent 侧唯一的使用文档，改命令行为必须同步这里。
- `PRIVACY.md`：修改、评审或排查历史文件读取、缓存写入、标题生成、跨运行时接力和开源隐私边界前必读。
- `CONTRIBUTING.md`：修改开源贡献流程、验证命令、设计边界或 PR 要求前必读。

## 架构约束

- `sc.py` 只负责界面、会话展示和用户选择，不得直接拼接某个运行时的启动参数。
- 运行时私有行为必须收敛在 `runtime/` 对应适配器中；新增运行时只实现扫描、对话预览、原生恢复、历史格式提示、接力新会话（读取其他运行时历史）和空白新会话（不关联任何历史，仅指定工作目录）两种启动能力，并在默认注册表注册一次。
- 跨运行时接力统一走“源适配器导出 `Handoff` → 目标适配器生成 `LaunchPlan`”，禁止增加 Claude→Gemini、Codex→Gemini 等两两转换分支。
- 同运行时使用原生恢复；跨运行时必须新建目标会话、让目标 Agent 按需读取原始 JSONL，不能改写或伪造原会话。
- 标题生成是独立服务，不属于任何运行时适配器。标题和界面状态使用“运行时 + 会话 ID”作为唯一键，新增运行时不得退回纯会话 ID。
- 会话预览使用全屏页面；预览页回车必须直接原生恢复当前会话，关闭预览时必须清屏并让主列表按当前终端尺寸完整重绘，禁止改回覆盖主列表的居中弹窗。
- `agent_api.py`（`sc list`/`search`/`show`/`context`/`describe`）是只读数据接口，禁止新增任何执行/拉起副作用命令——sc 只负责把会话数据交出来，怎么用是调用方的事。命令参数与 `sc describe` 的输出必须共用同一份 `COMMANDS` 定义，不能各写一份导致漂移。新增或修改子命令时同步 `docs/SKILL.md`。
- Agent 接口里 `list`/`search` 的 `--limit` 固定表示每个运行时的扫描深度，`--top` 才表示最终返回条数；`--compact` 必须同时做到紧凑 JSON 和精简默认字段。改这三个参数或 `show --out` 大结果落盘行为时，同步 `sc describe`、`docs/SKILL.md` 和 `docs/MAINTAINER_GUIDE.md`。

## 验证要求

**硬性红线：`sc` 首屏（进程启动到 TUI 首次渲染完成）延迟必须 ≤1s。** 改动扫描（`scan_claude.py`/`scan_codex.py`/`runtime/`）、标题或界面相关代码后，除下面的编译/单测外，必须额外跑一次真实计时确认达标：

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

`test_session_scanning.py` 的 `StartupLatencyTests` 会在有真实会话数据时对同一调用做 <1s 断言（`python3 -m unittest -v` 已包含），但真实计时仍要单独跑一次确认，不能只信任测试里的一次采样。不达标不得提交；根因排查思路和已修过的坑见 `docs/MAINTAINER_GUIDE.md`「扫描性能」节。

改动代码、界面或运行时适配器后至少执行：

```bash
python3 -m py_compile sc.py scan_claude.py scan_codex.py titles.py models.py agent_api.py runtime/*.py test_*.py
python3 -m unittest -v
```

涉及界面时还要运行一次真实终端冒烟。标题后台生成会消耗 Claude 额度；只验证界面时，在临时目录把 `claude`、`codex` 指向本机 `true`，放到 `PATH` 最前面，再启动 `python3 sc.py --limit 5`，确认：

- 底部显示 `a 高级操作`。
- 高级操作弹窗动态列出注册表中的运行时。
- 默认选中第一个已安装的其他运行时。
- `q` 先关闭弹窗，再退出主界面。

**涉及会话扫描、标题或会话预览（`load_conversation`）时，改完必须至少随机抽查 5 条真实会话验证，不能只靠手写的单测小样例过关。** 优先用真实终端打开预览页肉眼检查内容，或写一次性脚本批量跑 `load_conversation`/`scan_sessions` 扫描本机全部真实会话文件、断言没有异常（如空文本、字面量 `"None"`、角色标错）。本机 Claude/Codex 历史里曾各自藏着单测样例覆盖不到的真实格式坑（`stop_reason` 与文本内容无关、`origin.kind` 区分真人和系统事件、`payload` 字段值可能是 JSON `null` 而不是缺失），这类坑只有跑真实数据才会暴露，见「Claude 扫描」节的具体记录。

## 本机入口

`sc` 是指向项目 `sc.py` 的符号链接。代码更新后若界面未变化，先核对 `command -v sc`、`readlink "$(command -v sc)"` 和源码哈希，确认没有运行另一台机器或旧目录中的副本。
