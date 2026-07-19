# 侧边栏收窄、藏滚动条、标题省略与 runtime 配色

日期：2026-07-19  
状态：已落地（未发版）

## 目标

让左栏更紧凑、信息更清晰：少占列宽、去掉滚动条占位、长标题用 `...` 避让 runtime、runtime 用高区分度颜色标识。

## 行为

1. **宽度**：`LIST_PANE_WIDTH` 从 44 改为 39；文档与 `selftest.sh` 光标断言同步。
2. **滚动条**：`SessionListView` 垂直/水平 `scrollbar-size` 为 0；键盘与滚轮滚动保留。
3. **标题截断**：runtime 名完整右对齐；标题按显示宽度塞进剩余列，放不下时末尾 `...`（`pickup._fit_cell(..., ellipsis=True)`）。
4. **runtime 配色**（按 runtime `id`，单一来源 `pickup.RUNTIME_LABEL_STYLES` / `runtime_label_style()`）：

| id | 色 | 说明 |
|---|---|---|
| `claude` | `#D97757` | 接近 Anthropic 橙 |
| `codex` | `#60A5FA` | 蓝（用户偏好；非 OpenAI 绿） |
| `cursor` | `#A78BFA` | 紫（品牌橙与 Claude 撞色，改用紫） |
| `kimi` | `#F472B6` | 粉（品牌仅黑白） |
| `opencode` | `#34D399` | 绿（品牌近黑） |
| 其他 | `dim` | 兜底 |

展示样式为 `bold <color>`。左栏 `SessionCard`、右栏详情头、对话预览 `◆ Runtime` 行共用，禁止在 `ui/` 另写色表。

## 非目标

不发版、不加主题配置、不改第二行状态/时间配色。

## 验证与已知坑

- 单元测试覆盖省略号与配色；selftest 光标断言用 39。
- **安装副本不同步**：改完后须 `pip install --user --force-reinstall --no-deps .` 并重启 TUI；用 `runtime_label_style('claude') == 'bold #D97757'` 核对入口。
- Textual SVG 截图常把真彩色压灰，配色验收看真机或 `render_line` segment，不看 SVG fill。
