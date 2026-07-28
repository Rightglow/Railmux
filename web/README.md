# Railmux website

The product site is a static Vite + React application deployed at
<https://rightglow.github.io/Railmux/>.

## Local development

```bash
npm ci
npm run dev
```

The app uses `/Railmux/` as its Vite base path to match the GitHub Pages
project site.

## Verification

```bash
npm run typecheck
npm run build
```

The website workflow first records the real Railmux/tmux UI in a private tmux
server, temporary HOME, and synthetic provider history:

```bash
python -m pip install -e ..
npm run record-demo
```

The command needs `tmux` and the Railmux checkout but no Claude Code or Codex
login. It produces six source-authentic recordings:

- `public/generated/railmux-demo.cast` records the opening single-agent
  workspace, beginning with Railmux's product-native
  `Restoring your workspace` startup surface, opening a fresh Claude Code
  session, and typing a short prompt one character at a time.
- `public/generated/railmux-dual-demo.cast` records the full desktop workspace,
  including Claude Code and Codex in separate live agent panes.
- `public/generated/railmux-workflow-demo.cast` records a focused 160×38
  wide-layout history workflow: single-click preview, Enter resume, return to
  the sidebar, start a genuinely empty second conversation, and click the
  other running conversation to switch the agent pane. Composite semantic
  input events keep the recorded keyboard action in the HUD while placing a
  cell-aligned mouse pointer on the equivalent live control.
- `public/generated/railmux-mobile-demo.cast` records Railmux's real compact
  layout at a representative 46×38 portrait-phone geometry. It taps New
  session, `[R]`, and `[1]` with touch-only cues; no keyboard action is shown.
  The narrow width selects compact projection; the separate reported 105×21
  Termux geometry is landscape.
- `public/generated/railmux-tour-demo.cast` records the real New Project
  directory browser and built-in Help view without creating a project or
  launching an agent.
- `public/generated/railmux-controls-demo.cast` records More, provider Mode,
  F8 Layout, and the real Quit/Soft Quit confirmation. It completes a soft quit
  in the isolated workspace and leaves the recorded agent session running. The
  final layout choice is submitted without a separate input HUD because the
  resulting soft-quit state is the user-visible outcome.

The website plays all six casts directly as text through asciinema-player. The
recorder launches Railmux through its normal CLI, opens isolated local
demo-agent panes, and removes the private tmux server when it finishes. It
never reads the user's normal HOME or provider configuration.
The public source-analysis runs in `demo/real-agent-runs.json` were captured
once with non-persistent, read-only Claude Code and Codex invocations. Each run
records its producing agent, capture method, real prompt, inspected
repository-relative files, answer, capture date, and source commit. The same
audited asset records the user-supplied Claude Code 2.1.220 identity block;
its cwd is templated to the stable public demo path. The recorder embeds a
SHA-256 provenance value, rejects credential-like fragments, and replays the
sanitized transcripts inside provider-specific deterministic shells. It is a
transparent replay, not a live provider call; CI never receives or invokes
provider credentials.
Every full or terminal-truncated temporary root is replaced cell-for-cell by a
stable public demo path, keeping generated output reviewable without changing
terminal positions.

The desktop, dual-agent, workflow, tour, and controls profiles are also checked
against Railmux's real responsive-layout function: all must remain `WIDE`,
while only the 46×38 portrait mobile profile may use the `COMPACT` projection.

The workflow then creates deterministic product screenshots:

```bash
npx playwright install chromium
npm run screenshots
```

The browser pass generates a high-resolution dual-agent evidence image from
the real desktop cast, plus sidebar, compact, mouse-cue, playback, and
social-card smoke screenshots. The dual-agent image is committed for local
builds; transient verification images remain ignored. The Pages workflow
regenerates all six casts and every screenshot before deployment, then
includes them in the static artifact. The recorder waits for the Railmux
sidebar and verifies every scripted milestone before writing a cast, so a
partial CI recording fails closed instead of being published.
