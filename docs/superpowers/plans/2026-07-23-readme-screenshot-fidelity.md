# README Screenshot Fidelity Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `docs/screenshots/list.png` truecolor, chrome-free, and regenerable via `capture.py` even when the host has `NO_COLOR=1`.

**Architecture:** Keep Textual Pilot → Rich SVG → cairosvg PNG. Clear `NO_COLOR` before `PickupApp` init; strip Rich window chrome in `_prepare_svg`; refresh docs that blamed SVG itself for greyscale.

**Tech Stack:** Python 3, Textual, Rich SVG, cairosvg, Noto CJK fonts.

## File map

| File | Role |
|------|------|
| `docs/screenshots/capture.py` | Env hygiene, chrome strip, regenerate PNG |
| `docs/screenshots/list.png` | README / 验收产物 |
| `AGENTS.md` | 截图验收：点名 `NO_COLOR` |
| `docs/TERMINAL_UI_KNOWLEDGE_BASE.md` | 隐性依赖：灰阶根因 |
| `docs/superpowers/specs/2026-07-23-readme-screenshot-fidelity-design.md` | 已写设计 |

### Task 1: Fix `capture.py`

**Steps:**
1. At module start (before `PickupApp` construction path): `os.environ.pop("NO_COLOR", None)`; setdefault `COLORTERM=truecolor`, `PICKUP_LANG=en`.
2. In `_prepare_svg`: remove chrome rect / title / traffic-light `<g>`; set content `translate(0,0)`; set `viewBox` from clip-terminal size.
3. Docstring note about `NO_COLOR`.
4. Run `python3 docs/screenshots/capture.py` from `cli/`.
5. Verify PNG has chromatic pixels near `#D97757` and no `#ff5f57` traffic-light blob in the title-bar region.

### Task 2: Docs

**Steps:**
1. `AGENTS.md` 截图节：说明脚本会清 `NO_COLOR`；灰阶优先查环境变量，不要再写「SVG 导出总会压灰」。
2. `TERMINAL_UI_KNOWLEDGE_BASE.md` 隐性依赖同口径。
