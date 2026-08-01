# Railmux — session manager for Claude Code & Codex

[![Tests](https://github.com/Rightglow/Railmux/actions/workflows/test.yml/badge.svg)](https://github.com/Rightglow/Railmux/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/railmux.svg)](https://pypi.org/project/railmux/)
[![Python](https://img.shields.io/pypi/pyversions/railmux.svg)](https://pypi.org/project/railmux/)
[![License](https://img.shields.io/github/license/Rightglow/Railmux.svg)](https://github.com/Rightglow/Railmux/blob/main/LICENSE)

A persistent terminal workspace to navigate, resume, and run
[Claude Code](https://claude.com/claude-code) and
[Codex](https://github.com/openai/codex) sessions across all your projects.
Railmux gives tmux a project-aware sidebar and one or two live agent panes.
Every agent runs independently, so browsing, switching, detaching, and
responsive layout changes do not interrupt in-progress work.

**[Website & live demo](https://rightglow.github.io/Railmux/)** ·
**[Releases](https://github.com/Rightglow/Railmux/releases)** ·
**[PyPI](https://pypi.org/project/railmux/)**

<p align="center">
  <a href="https://rightglow.github.io/Railmux/">
    <img
      src="https://raw.githubusercontent.com/Rightglow/Railmux/main/web/public/generated/dual-agent-workspace.png"
      alt="Railmux running Claude Code and Codex side by side with a project and session sidebar"
      width="1100"
    />
  </a>
</p>

- **Manage sessions from one sidebar** — start new work, preview or resume
  history, and switch among live Claude Code and Codex agents.
- **Find existing work automatically** — resumable conversations started
  outside Railmux appear in the same project-aware sidebar.
- **Run two agents together** — use single, side-by-side, or stacked layouts
  without stopping the agent hidden by a layout change.
- **Leave without losing momentum** — detach one terminal or soft-quit the
  shared UI while detached agent sessions keep running.
- **Use it comfortably over SSH** — `railmux ssh` coalesces superseded redraws
  and provides responsive, locally cached history scrolling plus local
  drag-to-copy inside either Claude Code or Codex panes.

One-off non-resumable invocations such as `codex exec` are intentionally
filtered from the sidebar.

**Quick answers:** [use the mouse](#mouse) ·
[switch Claude Code and Codex](#switching-between-claude-code-and-codex) ·
[start or resume a session](#starting-and-resuming) ·
[copy text from an agent pane](#1-how-do-i-copy-text-from-the-agent-pane)

## Why Railmux?

Agent conversations tend to outlive the terminal tab where they began.
Railmux replaces tmux-window bookkeeping and copied session IDs with one
sidebar: select a project, preview or resume its history, and switch among live
agents without losing their context.

## Quick start

Railmux requires Python 3.9+, `tmux`, `less`, and Claude Code or Codex on
`PATH`. The two agent CLIs are independent, so either one is enough to start.

```bash
pip install railmux
# or: pip3 install railmux
railmux
```

If installation succeeds but your shell says `railmux: command not found`,
the Python user scripts directory is not on `PATH`—this is especially common
with user-level Python installs on macOS. Add it for the current shell, then
put the same line in `~/.zshrc` (macOS default) or `~/.bashrc`:

```bash
export PATH="$(python3 -m site --user-base)/bin:$PATH"
```

If `tmux` is missing, an interactive Railmux launch can offer to install it
with Homebrew on macOS or `apt-get` on Debian/Ubuntu/WSL, always after showing
the exact command and asking for confirmation. See
[FAQ 5](#5-pip-reports-externally-managed-environment) when system Python
requires a virtual environment.

Preference prompts appear only when relevant—for updates, Codex auto-run,
layout retention, or Claude history over `railmux ssh`. Persistent choices are
not permanent traps: press `o`, or choose **More → Options**, to change them
later. One-time safety confirmations such as dependency installation and
session deletion are intentionally asked at the action itself.

If ordinary SSH cannot keep up with full-screen redraws, install Railmux on
your local macOS, Linux, or Windows WSL environment and use its responsive,
locally cached SSH display:

```bash
railmux ssh your-server
```

The local machine needs an OpenSSH-compatible `ssh` executable on `PATH`;
Railmux checks this before entering its full-screen display. macOS, most Linux
distributions, and Windows WSL normally include one.

For several SSH options, use a single quoted group, for example
`railmux ssh your-server --ssh-args='-J jump-host -p 2222'`.

The remote needs Python 3.9+, `tmux`, and the `pyte` display dependency. With
permission, the local client installs the matching `railmux[ssh]` extra into
the remote user environment; it never uses `sudo`. If provisioning manually,
install `railmux[ssh]` remotely. Railmux keeps its managed tmux workspace
isolated from your default tmux server and does not rewrite provider histories
under `~/.codex` or `~/.claude`, except for a session you explicitly confirm
deleting. Run
`railmux doctor` for a privacy-safe local setup report, or
`railmux doctor --remote your-server` for a read-only remote compatibility
preflight. Multiple
terminals share one workspace; see [FAQ 6](#6-can-i-open-railmux-in-multiple-terminal-windows)
for focus and layout limits.

## Controls

### Mouse

| Action | Effect |
|--------|--------|
| Left-click (non-running) | Preview session history in the Target pane |
| Left-click (running) | Switch the Target pane to that session |
| Double-click | Open/attach in the Target pane and move focus there |
| Right-click | Context menu (Open, Preview, Info, Rename, Star, Copy title, Kill, Term, Delete) |

Unavailable context-menu actions are hidden for the selected session state.
Single-click and `␣` act in the Target pane without moving keyboard focus;
double-click and `Enter` also move focus there. The terminal must report mouse
buttons to applications for these actions to reach Railmux. Right-click
reporting is sometimes a separate setting from ordinary mouse reporting; see
[FAQ 2](#2-mouse-buttons-or-f8f9-dont-work--whats-wrong).

Copying text out of an agent pane also depends on the connection and terminal;
see [FAQ 1](#1-how-do-i-copy-text-from-the-agent-pane).

### Navigation

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move selection within the focused pane |
| `Tab` / `Shift-Tab` | Cycle focus through Projects, Sessions, Running panes |
| `Ctrl-B Tab` | Toggle directly between the sidebar and Target pane |
| `Esc` | Move focus up: Running → Sessions → Projects |
| `/` | Filter the focused Projects, Sessions, or Running pane by name |

### Session actions

| Key | Action |
|-----|--------|
| `Enter` | Resume or start the selected session |
| `n` | Start a fresh session in the current project |
| `i` | Popup with session details |
| `r` | Rename the focused session |
| `s` | Toggle star — starred sessions pinned to top with ⭐ |
| `k` | Kill the running agent process (keeps session file) |
| `d` | Delete the focused session (prompts for confirmation) |
| `t` | Open or return to the managed terminal below the Target agent |
| `T` | Return to that agent's managed Vim viewer, when open |
| `m` | Switch agent mode (Claude Code ⇄ Codex) |
| `o` | Open persistent Railmux options |
| `␣` | Preview stopped or switch running target (like single-click) |
| `F8` | Cycle agent layout: single → side-by-side → stacked |
| `F9` | Fullscreen the focused agent, managed terminal, or Vim (toggle) |
| `?` | Full help popup with all keybindings |
| `q` or `Ctrl-C` | Quit with confirmation |

Inside Help, press `A` or click **Ask Railmux with Claude Code/Codex** to open
a separate, safety-restricted support session for the current mode. Opening
Help itself never starts an agent or uses provider tokens. Ask Railmux replaces
only the Target pane's display; the agent that was there keeps running and can
be selected again from Running. Codex help is not saved to normal history;
Railmux hides the dedicated help workspace from both providers' Projects list.
Local documentation reads and searches run without approval prompts. Codex is
still enforced by its read-only, network-disabled sandbox; Claude is restricted
to its built-in `Read`, `Glob`, and `Grep` tools, with shell and mutation tools
not exposed. These restrictions depend on the installed provider CLI accepting
the documented safety flags; an incompatible CLI fails closed instead of
starting an unrestricted help agent.
If the help agent exits in a two-pane layout, that pane returns to Railmux's
empty launch surface without collapsing or reordering the other agent pane.
After a soft restart the help session is not restored automatically—open Help
and choose Ask again to reconnect to it.

`+ New project` works in both Claude Code and Codex modes. Browse to an
existing directory and choose `. (use this path)`, or type a new relative,
absolute, or `~`-based path. When no existing entry matches, select the
explicit `+ create …` row (it is focused automatically) and press `Enter`;
railmux creates the directory before starting the agent.

The rename popup starts with the current title pre-filled. Press
`Ctrl-U` to clear the entire input, `Enter` to save a non-empty title, or `Esc`
to cancel.

Press `/` in Projects, Sessions, or Running to filter that section. Active
filters are marked with `[filtered]` and survive a soft restart; reopening `/`
shows the current value. Press `Ctrl-U` in the filter editor to clear it, then
`Enter` to return to the sidebar.

The first Button Bar row keeps Help, Quit, and Detach visible. Select **More**
to reveal a second row with **Mode**, **Layout**, and **Options**; **Less**
collapses it. The `m`, `F8`, and `o` keyboard shortcuts remain available while
the row is hidden. On tmux 3.4 or newer, the bottom-left mode name and layout
symbol are also clickable, and clicking the message at bottom-right copies its
complete text to the local clipboard. Expanding the second Button Bar row takes
its one display line from the bottom Running section; Projects and Sessions
keep the same heights.

After an explicit layout change (`F8` or `[` / `]`), quitting offers to keep
the current pane proportions: **Always** keeps the latest custom layout,
**This time** restores it on the next launch only, and **No** leaves it
unsaved for this exit; **Never** also disables future layout prompts.
`[` and `]` move only the sidebar's right divider; dual-agent layouts split
the remaining area evenly again.
Proportions, rather than cell counts or tmux pane identities, are stored, so a
later terminal may have a different size. If a saved split cannot fit, Railmux
uses safe responsive defaults for that run without overwriting the saved
profile. The first Codex auto-run prompt uses the corresponding choices:
**Always**, **This Railmux run**, **No**, or **Never**.

Press `o`, or select **More → Options**, to change persistent behavior without
editing TOML. **Layout retention**, **Codex auto-run**, and **Railmux updates**
support **Always**, **Ask every time**, and **Never**. Claude history over
`railmux ssh` supports **Local transcript**, **Ask on first scroll**, and
**Claude native**. Use arrow keys plus
`Enter`/`Space`, or click a choice with the mouse. Activating the already
selected choice confirms and closes the screen; `Esc` or `o` also closes it.
Layout changes do not resize the current workspace; Codex auto-run changes
affect new launches, not agents that are already running.

### Switching between Claude Code and Codex

Press `m`, or select **More → Mode**, to switch the sidebar between Claude Code
and Codex. The current mode controls which provider's Projects, Sessions, and
Running lists are shown, while each mode remembers its selected project and
Running filter. Agents running in the other mode keep running and reappear when
you switch back; changing modes never stops them. Either CLI alone is enough to
use Railmux; selecting a mode whose CLI is not installed shows a warning rather
than stopping the workspace.

### Dual-agent layouts

Railmux distinguishes the **Focused pane** from the **Target pane**. The Focused
pane receives keyboard input; the Target pane is where actions started from the
sidebar take effect. They can differ while you browse the sidebar.

Open the first agent normally, then press `F8` to cycle through single,
side-by-side, and stacked layouts. Pane 2 can remain empty until you choose a
session for it, and layouts that do not fit the terminal are skipped. Returning
to single leaves Pane 2's agent running in the background.

In a split, focus an agent pane to make it the Target pane. After focus returns
to the sidebar, single-click or `␣` acts in that pane without moving keyboard
focus; double-click or `Enter` opens there and transfers focus. The status bar
at the bottom-left shows the current layout and Target pane:

| Symbol | Meaning |
|--------|---------|
| `▣` | Single pane |
| `◧` / `◨` | Side-by-side, targeting left / right |
| `⬒` / `⬓` | Stacked, targeting top / bottom |

Agent borders turn green around the Focused pane. When focus is in the sidebar,
the borders return to gray while the status symbol continues to show the Target
pane. `Ctrl-B Tab` returns directly from either agent pane to the sidebar without
changing that Target, then toggles back to it. `Ctrl-B` plus an arrow remains
spatial: left/right moves across a side-by-side split, while up/down moves
between stacked agent panes.

### Phones and compact terminals

Before switching to full-page compact mode, a dual-agent workspace that can no
longer give both agents at least 50x12 temporarily shows the sidebar plus the
current Target agent. The other agent keeps running in its detached tmux
session. Widening the terminal restores both original slots, their saved
split/stacked proportions, and the attached Target; this responsive projection
does not change the user's saved F8 layout.

Railmux automatically switches to a one-page-at-a-time compact workspace when
either terminal dimension is cramped: fewer than 80 columns or fewer than 24
rows. The existing panes and layout remain alive; the bottom status bar exposes
`[R]` for the Railmux sidebar and `[1]`/`[2]` for the agent panes. The current
page is highlighted. Click a page label when mouse reporting is enabled and
the remote tmux is 3.4 or newer. `Ctrl-B Tab` remains the portable keyboard
toggle between `[R]` and the current Target agent.

F8 still creates, removes, and remembers the second agent pane in compact mode,
although only one page is shown at a time. F9 and divider-resize controls are
unnecessary while a page already fills the screen, so they safely do nothing.
Help and Options temporarily use the sidebar page and return to the previous
page afterward.

Compact mode is selected from the available character-cell geometry, not the
device name or portrait orientation. A phone landscape reported as `20 105` by
`stty size` (20 rows by 105 columns) is compact because it is short; a portrait
desktop monitor such as 60 rows by 100 columns retains the normal wide,
multi-pane UI. Railmux waits until at least 84 columns and 26 rows are available
before returning to wide mode, avoiding repeated changes around the boundary.
The minimum usable workspace is 40 columns by 12 rows.

In Termux, `railmux ssh` keeps mouse support enabled while making the focused
Claude Code or Codex prompt keyboard-friendly. Tap the prompt once to hand
touch input back to Termux, then tap it again to open the soft keyboard.
Railmux restores mouse reporting after typing, when the keyboard closes, or
after a short timeout. This assistance is local to Termux and does not affect
desktop terminals.

### Finding running sessions

Plain text matches the visible session label, project, and provider without
searching message content. Add `project:<name>` to restrict the list to one
project. Claude Code and Codex keep independent Running filters, and blocked
sessions move ahead of the other results.

## History preview

For a stopped session, left-click or press `␣` to view conversation history in
the Target pane without starting or resuming the agent. Preview is read-only: it
cannot send a message or change the session. User and assistant messages, tool
calls, and abbreviated tool output are colour-coded, while internal context and
encrypted reasoning are hidden.

Preview opens at the latest activity in `less`; large sessions are limited to
their latest 2,000 saved records. Press `/` to search, `n`/`N` to move between
matches, and `q` to exit and restore the pane. Double-click to skip preview and
open the session directly.

## Status indicators

Each running session shows a coloured • reflecting its current state:

- **Green** — idle (assistant last responded normally)
- **Yellow** — busy (assistant is processing)
- **Red** — blocked (waiting for tool approval)

An independent magenta **!** marks an outcome that still needs attention, such
as an abort or provider error. It does not replace the activity dot: a live
session can be idle and still show `!`, while a stopped historical session keeps
its neutral `◦` marker alongside the badge. Session Info and Running Info show
the available details.

A grass-green title identifies a live tmux session independently of its status;
stopped sessions use a neutral hollow ◦. The same grass green is used for the
focused pane chrome and tmux status bar. The current cursor uses a deeper green
background, while the session displayed in the agent pane remains marked in
neutral slate after keyboard focus moves away.

## Managing sessions

### Starting and resuming

Start and resume from the Railmux sidebar: select a project, press `n` for a
new session, or press `Enter` to resume the selected one. **+ New project**
picks or creates its directory first. This is the recommended path because the
new agent immediately belongs to Railmux's running workspace and Target pane.

Railmux also indexes resumable Claude Code and Codex sessions started outside
Railmux, so existing conversations appear in the sidebar and resume the same
way. Non-resumable one-off runs such as `codex exec` are filtered out.

### Restarts and quitting

Each opened agent runs in a detached tmux session, so switching sessions does
not interrupt it. To leave agents running when you quit Railmux, press `s` for
soft quit in the confirmation popup; restarting the same Railmux instance then
restores the usable workspace when those sessions are still available. A normal
quit confirmation ends all running sessions instead.

All terminals attached to one managed Railmux window view the same UI process.
Soft Quit therefore closes that shared UI for every attached view, although it
does not stop the detached agent sessions. It is not an exclusive-client
command; the quit prompt calls this out when multiple terminals are attached.
To keep the current terminal and detach every other attached client, run
`Ctrl-B :detach-client -a Enter` from the terminal you want to retain.

If Railmux stops while a provider is still creating a new session, the Running
pane may show it as unresolved. You can reopen or stop that agent, but Railmux
will not offer to delete provider history until it can identify the session
safely.

## Configuration

Use the standalone editor for normal changes; it does not require tmux to be
installed or working:

```bash
railmux config
# Or edit the configuration owned by an SSH destination:
railmux config --remote your-server
```

Use `--ssh-args='-J jump-host -p 2222'` for SSH options. Its contents are
parsed locally into an argv without executing a shell. Remote editing
first performs a bounded package/capability probe, then opens a fresh cooked
SSH PTY for the editor. It never attaches to, creates, resizes, or queries a
tmux server. If Railmux is absent or too old remotely, installation into the
remote user environment (and, if needed, Railmux's private SSH venv) requires
explicit consent and never uses `sudo`.

In an interactive terminal the editor uses a temporary full-screen surface.
Exiting, cancelling, or encountering an error restores the original shell
screen and leaves its scrollback unchanged. Redirected input/output and
`--help` remain plain text without terminal control sequences.

Choose **Behavior / Options**, **Program paths**, or **Environment**, then the
specific setting. Every category supports Back, Exit, and a confirmed category
reset; individual settings can also be reset to their default. The editor
validates executable paths and UTF-8 locales before saving. It updates the same
optional `~/.config/railmux/config.toml` used by in-app Options:

```toml
[tmux]
# Command name, or an executable path ending in /tmux (default: "tmux")
binary = "tmux"

[claude]
# Path to the claude binary (default: "claude")
binary = "claude"

[codex]
# Path to the codex binary (default: "codex")
binary = "codex"
home = "~/.codex"
# New Codex launches: "always", "ask", or "never" (default: "ask")
auto_run = "ask"

[environment]
# "inherit", or an installed UTF-8 locale such as C.UTF-8 / en_US.UTF-8
locale = "inherit"

[ui]
# Save custom pane proportions: "always", "ask", or "never"
layout_retention = "ask"

[updates]
# PyPI update checks at normal Railmux startup: "always", "ask", or "never"
auto_update = "ask"

[projects]
# Show projects with no resumable sessions (default: false)
show_empty_projects = false

[live]
# How often to refresh the session list (ms)
poll_interval_ms = 1000

# Agent display mode (default: "swap").
# Set "nested" only when troubleshooting an unusual tmux environment.
agent_transport = "swap" # or "nested"

[ssh]
# Local railmux ssh history cap (default: 10000; range: 2000-20000)
history_lines = 10000
# Claude Code wheel history: "ask", "local", or "native"
claude_history = "ask"
# Clicked remote paths: "ask", "internal", or "external"
path_open = "ask"
```

Most users should leave `agent_transport` unchanged. Railmux automatically uses
the compatible `nested` display when the default `swap` mode is not safe for the
current tmux environment.

This is Railmux's only user settings file. Manual edits, `railmux config`, and
the in-app Options screen share the same authority and preserve comments,
formatting, order, and unknown keys. The local-only `ssh.history_lines` setting
is intentionally file/command-line controlled because the remote TUI cannot
configure the machine that initiated `railmux ssh`. By contrast,
`ssh.claude_history` and `ssh.path_open` belong to the remote workspace and are
exposed in that workspace's Options screen. A one-run Codex choice is kept only in memory. A
`This time` layout profile is stored here until it is successfully applied on
the next launch, then removed.

Program and locale changes affect new Railmux-managed processes. They never
restart, replace, or kill an existing tmux server or running agent. If a newly
selected tmux client cannot communicate with an existing Railmux server, the
next launch stops with a bounded compatibility message; run `railmux config`
to select the matching executable. `railmux doctor` reports configured command
status and whether the effective locale is UTF-8 without printing custom paths.

For `railmux ssh`, local display settings come from the local config, while
tmux, provider paths, locale, and in-workspace policies come from the remote
host's config. Run `railmux config --remote HOST`, or log in and run
`railmux config`, to change them. The remote editor intentionally hides the
local-client-only `ssh.history_lines` value and preserves it during category or
global resets. The compatibility preflight distinguishes an invalid remote config or a
missing configured tmux from a missing Railmux installation; it never repairs
either by mutating tmux.

When the default `auto_update = "ask"` finds a newer stable PyPI release,
Railmux offers **Always**, **This time**, **No**, or **Never** before opening
the TUI.
**Always** installs this and future releases automatically, **This time**
updates only now, **No** skips only this launch, and **Never** disables future
checks. Update checks time out quickly and never prevent offline startup;
failed installs continue with the installed version and print a manual
command. Editable source installations are reported but never overwritten.

## Diagnostics

```bash
railmux doctor
```

Use `railmux doctor --json` for the same privacy-safe snapshot in a versioned,
machine-readable form suitable for issue tooling.

Before connecting, `railmux doctor --remote your-server` checks SSH reachability,
the remote Railmux and protocol versions, the optional SSH display dependency,
remote config status, and remote `tmux`. It stops after the compatibility hello: it does not attach,
create, resize, replace, install, or upgrade anything. Repeat
`--ssh-args='-J jump-host -p 2222'` when the connection needs SSH options. The
hostname is omitted from both text and JSON output.

The doctor command works even when `tmux` is missing. It reports component versions, terminal capability
hints, configuration health, dedicated-server reachability, watchdog state,
the number of legacy candidates on the default server, the age and bounded
category of the last recorded tmux incident, and whether provider data
directories are accessible. When available, it also summarizes the most recent
`railmux ssh` connection with bounded frame, reconnect, transfer, and history
counters; the host is deliberately not recorded. Its output is designed for issue
reports: it does not include hostnames, usernames, session IDs, transcripts,
credentials, environment values, configured commands, socket paths, or raw
custom paths.

## FAQ

### 1. How do I copy text from the agent pane?

Under tmux the sidebar and agent share the screen, and over SSH your clipboard
lives on the *local* machine. Ordinary Railmux copying depends on terminal
clipboard support; `railmux ssh` additionally provides direct, pane-bounded
drag-to-local-copy.

**With `railmux ssh`**: drag directly inside one agent pane. Railmux briefly
highlights the visible selection and copies it to the local clipboard on
release, using a native clipboard command where available and bounded OSC 52
as fallback. The
initial implementation intentionally stops at the pane border and current
visible viewport; it does not autoscroll or merge application soft-wrapped
physical lines. A click without drag is replayed normally, including pane focus
and double-click actions.

**OSC 52** (iTerm2, kitty, WezTerm, Alacritty, foot, Windows Terminal):
drag-select in the agent pane copies to the local clipboard automatically,
over ordinary SSH, no Shift needed. (iTerm2: enable *Settings → General →
Selection → "Applications in terminal may access clipboard"*.)

**Without OSC 52** (Terminal.app, etc.): press **F9** to fullscreen the agent →
**Shift‑drag** to select → `Cmd+C` / `Ctrl+C` to copy → **F9** to return.

> `Ctrl-B z` also toggles fullscreen (built into tmux) but zooms whichever
> pane has focus — it may fullscreen the sidebar instead of the agent.

### 2. Mouse buttons or F8/F9 don't work — what's wrong?

These are usually terminal‑side settings, not tmux or railmux.

**Mouse**: enable your terminal's “Report mouse events” or “Mouse reporting”
setting. Railmux already enables tmux mouse support for its own sessions, but it
cannot receive an event that the terminal keeps for its own UI.

Right-click may have a separate forwarding switch. In iTerm2, open *Settings →
Pointer → General*, then enable **“Right click reported to apps, does not open
menu.”** Without it, iTerm2 opens its own menu instead of sending the click to
Railmux.

![iTerm2 Pointer settings with “Right click reported to apps, does not open menu” enabled](https://raw.githubusercontent.com/Rightglow/Railmux/main/docs/assets/iterm2-right-click.png)

VS Code and Cursor users can change **Terminal › Integrated: Right Click
Behavior** when the editor's own menu prevents right-click from reaching
Railmux. **copyPaste** is the recommended starting point: Railmux handles
right-click while its mouse-aware sidebar is active, and the editor retains its
convenient copy/paste behavior elsewhere in the terminal. Cursor exposes the
same VS Code setting. Configure it in User Settings JSON:

```json
{
  "terminal.integrated.rightClickBehavior": "copyPaste"
}
```

Use `nothing` instead if your editor version still intercepts right-click or you
prefer all right-click events to pass directly to terminal applications.

**CJK input methods in VS Code/Cursor**: if Chinese, Japanese, or Korean input
occasionally stops while ASCII still works, click outside the integrated
terminal and then back into the agent pane. This resets a stuck xterm.js
composition focus without reconnecting Railmux. If `Ctrl-Space` switches input
sources, it can conflict with terminal suggestions; use the OS input-source
menu/Globe key to confirm, rebind the terminal suggestion action, or disable it
with `"terminal.integrated.suggest.enabled": false`.

**F8 (layout) and F9 (fullscreen)**: the operating system or terminal may
consume function keys before tmux sees them. On macOS, either hold `Fn` when
pressing the key or enable *System Settings → Keyboard → “Use F1, F2, etc. keys
as standard function keys”*; also remove any Mission Control shortcut using the
same key. On Windows laptops, `Fn+Esc` commonly toggles Fn Lock. If the terminal
has its own shortcut or key-mapping editor, remove the conflicting mapping or
configure it to send the corresponding F8/F9 function-key sequence to the
terminal session.

### 3. Using railmux over SSH

#### Choose a connection mode

Railmux supports two SSH workflows:

- Run ordinary `ssh your-server`, then `railmux`. This uses the terminal's
  normal tmux rendering path and works without a local Railmux installation.
- Install Railmux locally and run `railmux ssh your-server`. Its latest-state
  display coalesces superseded redraws and keeps bounded history locally. This
  is usually smoother for Codex rewinds, Claude redraws, and slower links.

The local SSH client supports macOS, Linux, and Windows WSL and requires an
OpenSSH-compatible `ssh` executable on `PATH`. Railmux checks for it before
entering the full-screen display. The remote host needs Python 3.9+, tmux, and
the `pyte` display dependency; Linux and other Unix-like servers are the
primary targets. Automatic setup installs the matching `railmux[ssh]` extra,
or you can install that extra manually on the remote.

#### Quick start with `railmux ssh`

```bash
railmux ssh your-server
# Multiple OpenSSH arguments belong in one quoted group:
railmux ssh your-server --ssh-args='-J jump-host -p 2222'
```

If Railmux is missing remotely, the client can install the matching version
into the remote user environment. It never uses `sudo`; when user-site installs
are blocked, it can offer an isolated private environment. Compatible package
versions may connect directly, while an incompatible or newer remote version
produces a guided update prompt.

The default remote workspace starts automatically when absent. Use `Ctrl-B d`
to detach normally or `Ctrl-]` for an emergency local disconnect.

#### Reconnect and startup

Automatic reconnect is on by default for an established display. For up to 60
seconds Railmux leaves the last frame visible and reports each attempt in the
bottom-right status area. `Ctrl-]` or `Ctrl-C` cancels locally; `--no-reconnect`
disables retries for one invocation. Explicit detach, quit, soft quit, or local
disconnect is never retried, and reconnect never recreates or kills the remote
workspace.

Startup shows **Restoring your workspace** plus the current connection stage.
The first validated frame replaces it; a 30-second first-frame timeout exits
only the local display and leaves remote agents running. Password or MFA input
is supported during the initial connection, but automatic reconnect is
non-interactive and asks you to rerun the command when authentication changes.

#### History, Claude behavior, and copying

Mouse-wheel and `Page Up` / `Page Down` browsing use a responsive local cache
in agent panes. Each pane keeps its own position; scroll to the bottom, press
`Esc`, or type to return that pane to live output. The default cap is 10000
lines and the supported range is 2000-20000:

```bash
railmux ssh --history-lines 10000 your-server
```

Set `ssh.history_lines` persistently with `railmux config`. Reaching the
available top shows a one-time `History top` message. Higher caps use more local
memory and do not change remote tmux's own history limit.

Claude Code can use either Railmux's smooth read-only transcript or Claude's
native clickable history. The first upward scroll asks which behavior to use,
with **Always** and **this time** choices; change it later under **More →
Options → Claude history in railmux ssh**.

Dragging inside a visible agent pane selects and copies locally without tmux
copy-mode. Selection stays within that pane and viewport. `Ctrl-B [` remains
available for explicit tmux copy-mode; `--no-mouse` gives terminal-native
selection priority instead.

#### Click URLs and remote paths

Hoverable URLs and paths are recognized in either agent pane. A clean click on
a URL opens the local browser. Remote paths are checked read-only and can open
in a managed Vim pane inside Railmux or in a separate local terminal, with
**Always** and **this time** choices. The first click in an unfocused agent pane
only focuses it.

Each agent slot has at most one reusable managed shell and one Vim viewer. Use
`t` for the shell, `T` for Vim, and Vim's `gt` / `gT` for multiple file tabs.
Managed tool panes require tmux 3.0+; core Railmux remains compatible with tmux
2.7. Paths with spaces must be quoted. Relative paths from old output resolve
against the pane's current directory, and remote localhost URLs still require
your own SSH port forwarding.

#### Mobile and small terminals

On mobile terminals such as Termux, opening the soft keyboard may temporarily
report fewer than 12 rows. At startup, `railmux ssh` waits for the keyboard to
close. While attached, a bottom-anchored local projection keeps the status bar,
prompt cursor, and nearby input visible without shrinking the remote layout.
Close the keyboard before rotating the device. Terminals narrower than 40
columns are rejected; hide the keyboard or reduce the font size.

#### Ordinary SSH tuning and diagnostics

For ordinary SSH, these optional settings reduce Escape latency and increase
remote tmux scrollback:

```tmux
# ~/.tmux.conf on the server
set -sg escape-time 0
set -g history-limit 10000
```

OpenSSH compression can help some links:

```sshconfig
# ~/.ssh/config on the client
Host your-server
    Compression yes
```

If mouse redraws remain slow, `↑↓`, `Tab`, and `Enter` cover the full UI without
mouse traffic. Cursor's integrated terminal is also worth trying for ordinary
SSH because it handles queued full-screen redraws well. Run `railmux doctor
--remote your-server` for a read-only compatibility check. Railmux's display
watchdog and remote heartbeat detach only the exact failed display client; they
do not kill tmux, the shared workspace, or its agents. See
[the architecture document](https://github.com/Rightglow/Railmux/blob/main/docs/ARCHITECTURE.md) for protocol, lease,
history-cache, and failure-containment details.

### 4. Will automated review sessions pollute my session list?

**Codex**: sessions created by `codex exec` (headless automation, pre‑commit
hooks, CI) are filtered automatically — railmux only shows interactive
sessions (`codex-tui`, `codex_cli_rs`).

**Claude Code**: for one-shot automated reviews, disable session persistence so
the consultation never appears in `/resume`:

```bash
# Print mode only; the review is not saved as a resumable session
claude -p --no-session-persistence "review this diff"
```

(`--no-session-persistence` is a Claude Code print-mode option.)

### 5. pip reports "externally-managed-environment"

Create a virtual environment, then install Railmux with that environment's
`pip`. This works on macOS, Linux, and WSL without modifying the system Python:

```bash
python3 -m venv ~/.venvs/railmux
source ~/.venvs/railmux/bin/activate
pip install railmux
```

`pipx install railmux` is an optional convenience for a globally available CLI;
it is not required on macOS or any other platform.

### 6. Can I open Railmux in multiple terminal windows?

Yes, as multiple views of one shared workspace. Normal launches and current
`railmux ssh` helpers may attach to the same managed `railmux` tmux session.
Railmux pins that shared window to tmux's `smallest` sizing policy, so a smaller
terminal does not see clipped content and focus changes no longer make geometry
jump between client sizes.

These are not independent workspaces: every client still shares focus, Target
pane, layout, pane proportions, and the one tmux window geometry chosen for the
smallest attached viewport. A new client may therefore open with its cursor in
the sidebar or a preview selected by another client; use `Ctrl-B` plus an arrow,
`Ctrl-B Tab`, or a mouse click to focus the intended agent pane. Simultaneous
input can interfere. Use `Ctrl-B d`
to detach exactly the terminal issuing the key; the clickable Detach action
asks for that shortcut when several clients are attached because a sidebar
process cannot identify which tmux client clicked it.

To make the current terminal the only attached view, use
`Ctrl-B :detach-client -a Enter`. This detaches the other clients without
stopping Railmux or its agents. Soft Quit is different: it closes the one shared
Railmux UI, so every attached view loses that UI while the detached agents stay
alive for the next launch.

For independent full-screen geometry and pane proportions, use one interactive
Railmux window at a time for now. That behavior requires separate display
workspaces rather than another option on one tmux window.
Detached agent sessions keep running in the background, so closing or detaching
the visible client does not stop them. Advanced users can launch separate
instances inside separate tmux sessions or servers, but that is not yet a
polished multi-window workflow and the instances still share provider history
and Railmux configuration on disk.

## Acknowledgements

The tmux sidebar idea and initial architecture came from [regmi-saugat/ccmgr](https://github.com/regmi-saugat/ccmgr). railmux extends it with Codex support, session history preview, starring, in-app renaming, mouse interaction, and a status bar integrated into the tmux status line.

## Contributing

Contributions are welcome; see [CONTRIBUTING.md](https://github.com/Rightglow/Railmux/blob/main/CONTRIBUTING.md).
Security issues should follow the private-reporting guidance in
[SECURITY.md](https://github.com/Rightglow/Railmux/blob/main/SECURITY.md).
