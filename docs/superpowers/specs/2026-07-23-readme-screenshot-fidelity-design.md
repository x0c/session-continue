# README TUI 截图保真（无假窗口边框）

日期：2026-07-23  
状态：已落地

## 目标

README / 验收用的 `docs/screenshots/list.png` 在**无 OS 窗口铬**的前提下，配色、布局、CJK 与真机 TUI 一致，且可脚本重出。

## 根因

出图环境常带 `NO_COLOR=1`。Textual 在 `App.__init__` 启用 `Monochrome` 滤镜，compositor 里真彩（如 Claude `#D97757`）被压成灰阶。Rich SVG 另画红/黄/绿假标题栏，与「真实 TUI」无关。

## 方案

修现有 Pilot → SVG → PNG 管线（不引入 Pillow 栅格化 / 无头终端）：

1. `capture.py` 在创建 `PickupApp` 前清除 `NO_COLOR`，并 `COLORTERM=truecolor`、`PICKUP_LANG=en`。
2. SVG 后处理去掉 Rich 窗口铬（圆角外框、标题、三色点），内容组平移归零，`viewBox` 对齐 terminal clip。
3. 保留既有 CJK 字体替换与 `textLength`/bold 修补。
4. 重出 `list.png`；文档纠正「SVG→PNG 总会灰阶」的过时说法，改为点名 `NO_COLOR`。

## 非目标

真机窗口像素截屏、CI 自动出图、Pillow 逐格渲染（字距仍不满意时再升级）。

## 验收

- PNG 中存在 runtime 真彩（至少 Claude 橙附近像素）。
- 无红/黄/绿假按钮与「pickup」假标题栏。
- CJK 可读；左栏+右栏对话+Footer 布局完整；仍用虚构 demo 数据。
