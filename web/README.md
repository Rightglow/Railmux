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
login. It produces five source-authentic recordings:

- `public/generated/railmux-demo.cast` records the opening single-agent
  workspace.
- `public/generated/railmux-dual-demo.cast` records the full desktop workspace,
  including a second live agent pane and two reviewed real-agent runs.
- `public/generated/railmux-workflow-demo.cast` records a focused 160×38
  wide-layout history workflow: single-click preview, Enter resume, return to
  the sidebar, and click the running conversation. Semantic input events drive
  the website's durable key HUD and cell-aligned mouse pointer.
- `public/generated/railmux-mobile-demo.cast` records Railmux's real compact
  layout and page controls at a representative 46×38 portrait-phone geometry.
  The narrow width selects compact projection; the separate reported 105×21
  Termux geometry is landscape.
- `public/generated/railmux-tour-demo.cast` records the real New Project
  directory browser and built-in Help view without creating a project or
  launching an agent.

The website plays all five casts directly as text through asciinema-player. The
recorder launches Railmux through its normal CLI, opens isolated local
demo-agent panes, and removes the private tmux server when it finishes. It
never reads the user's normal HOME or provider configuration.
The two public source-analysis runs in `demo/real-agent-runs.json` were captured
once with the user's existing Claude authorization in
`--no-session-persistence` mode. Each run records its real prompt, inspected
repository-relative files, answer, capture date, and source commit. The
recorder embeds a SHA-256 provenance value, rejects credential-like fragments,
and replays the sanitized transcripts with deterministic pacing inside a shell
modelled on the current native Claude Code TUI structure. It is a transparent
replay, not a live provider call; CI never receives or invokes provider
credentials.
Every full or terminal-truncated temporary root is replaced cell-for-cell by a
stable public demo path, keeping generated output reviewable without changing
terminal positions.

The desktop, dual-agent, workflow, and tour profiles are also checked against
Railmux's real responsive-layout function: all must remain `WIDE`, while only
the 46×38 portrait mobile profile may use the `COMPACT` projection.

The workflow then creates deterministic product screenshots:

```bash
npx playwright install chromium
npm run screenshots
```

The browser pass generates a high-resolution dual-agent evidence image from
the real desktop cast, plus sidebar, compact, mouse-cue, playback, and
social-card smoke screenshots. The dual-agent image is committed for local
builds; transient verification images remain ignored. The Pages workflow
regenerates all five casts and every screenshot before deployment, then
includes them in the static artifact. The recorder waits for the Railmux
sidebar and verifies every scripted milestone before writing a cast, so a
partial CI recording fails closed instead of being published.
