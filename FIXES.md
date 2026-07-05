# ccmgr 修复路线图

> 基于 [Rightglow/ccmgr](https://github.com/Rightglow/ccmgr) 的代码审查
> 2026-07-04

---

## 🔴 必须修（直接影响可用性）

### ~~P1 — `q` 退出也要确认弹窗~~ ✅ 已完成

**修复**：`q` 走 `_open_quit_confirm()`，和 `Ctrl-C` 一样。（`app.py:377-379`）

---

### ~~P2 — 退出确认框显示 session 数量~~ ✅ 已完成

**现状**：`QuitConfirmModal` 只问 "Quit ccmgr?"，不提示有多少 session 在跑。

**修复**：显示 "N Claude sessions still running. Quit will kill them all." （`modals.py:50-61`、`app.py:292-296`）

---

## 🟡 高价值（明显改善体验）

### P4 — 修复 `i` 键在 Running 面板上的 bug

**现状**：焦点在 Running 面板时按 `i`，弹出的是 Sessions 面板里选中的 session 信息，而不是 Running 面板里的信息。

**涉及文件**：`app.py:272-286`

**预期效果**：在 Running 面板上按 `i` 显示该 running session 的信息

---

### P5 — 无项目选中时隐藏 `+ New session`

**现状**：`SessionsPane` 的 `+ New session` 行始终显示。没选项目时按 Enter 只会提示 "Pick a project first."。

**修复**：`SessionsPane.set_sessions` 在 `project is None` 时隐藏 `+ New session` 行。

**涉及文件**：`sessions_pane.py`

---

### P6 — Session 删除功能

**现状**：只能退出 ccmgr 时一起杀掉所有 session，无法关闭单个 session。

**修复**：
- Sessions 面板：按 `d` 删除选中 session（删除 JSONL 文件 + kill detached tmux）
- Running 面板：按 `d` kill 对应 detached tmux session
- 都需要确认弹窗

**涉及文件**：`app.py`、`sessions_pane.py`、`running_pane.py`

---

## 🟢 锦上添花

### ~~P7 — 快捷键支持 config 可配置~~ ❌ 不做

现有快捷键合理，过度设计不值得。

---

### ~~P8 — `/` 在 Running 面板不触发无意义操作~~ ✅ 已完成

**修复**：焦点在 Running 面板时 `/` 提示 "No filter on Running pane."（`app.py:419-422`）

---

### ~~P9 — `list_panes` 空输出 bug~~ ✅ 已完成

**修复**：`tmux_ctl.py:60-61,171-173`：空输出时返回 `[]` 而非 `['']`

---

### ~~P10 — `session_count` 统计非 UUID JSONL~~ ✅ 已完成

**修复**：`discovery.py:79-99`：`_count_and_latest_mtime` 现在过滤 UUID 格式文件名

---

### ~~P11 — 删除死代码~~ ✅ 已完成

**修复**：移除 `scrollback_lines` 和 `max_concurrent_sessions` 两个从未使用的 config 字段（`config.py`）

---

## 修复进度

| 编号 | 描述 | 状态 |
|------|------|------|
| — | `Ctrl-B d` detach 提示 | ✅ 完成 |
| P1 | `q` 退出确认 | ✅ 完成 |
| P2 | 退出显示 session 数 | ✅ 完成 |
| P3 | `--project` CLI | ❌ 删除 |
| P4 | `i` Running 面板 bug | ✅ 完成 |
| P5 | 无项目隐藏 `+ New session` | ✅ 完成 |
| P6 | Session 删除 | ✅ 完成 |
| P7 | 快捷键可配置 | ❌ 不做 |
| P8 | Running 面板 `/` 过滤 | ✅ 完成 |
| P9 | `list_panes` 空输出 bug | ✅ 完成 |
| P10 | `session_count` 统计 bug | ✅ 完成 |
| P11 | `scrollback_lines` 死代码 | ✅ 完成 |
| — | Session 重命名 (`r`) | ✅ 完成 |
| — | 实时状态检测 (🟢🟡🔴) | ✅ 完成 |
| — | 桌面通知 (blocked) | ✅ 完成 |
| — | 收藏夹 (⭐ `f` 置顶) | ✅ 完成 |
