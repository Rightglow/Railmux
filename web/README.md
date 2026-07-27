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
login. It produces three source-authentic recordings:

- `public/generated/railmux-demo.cast` records the full desktop workspace,
  including a second live agent pane and two reviewed real-agent runs.
- `public/generated/railmux-workflow-demo.cast` records a focused 160×38
  wide-layout sidebar workflow. Semantic input events drive the website's
  durable key HUD and cell-aligned mouse pointer.
- `public/generated/railmux-mobile-demo.cast` records Railmux's real 46×26
  compact layout and page controls for the small-screen section.

The website plays all three casts directly as text through asciinema-player. The
recorder launches Railmux through its normal CLI, opens isolated local
demo-agent panes, and removes the private tmux server when it finishes. It
never reads the user's normal HOME or provider configuration.
The two public source-analysis runs in `demo/real-agent-runs.json` were captured
once with the user's existing Claude authorization in
`--no-session-persistence` mode. Each run records its real prompt, inspected
repository-relative files, answer, capture date, and source commit. The
recorder embeds a SHA-256 provenance value, rejects credential-like fragments,
and replays the sanitized transcripts with deterministic pacing. CI never
receives or invokes provider credentials.
Every full or terminal-truncated temporary root is replaced cell-for-cell by a
stable public demo path, keeping generated output reviewable without changing
terminal positions.

The desktop and workflow profiles are also checked against Railmux's real
responsive-layout function: both must remain `WIDE`, while only the 46×26
mobile profile may use the `COMPACT` projection.

The workflow then creates deterministic product screenshots:

```bash
npx playwright install chromium
npm run screenshots
```

The browser pass generates a high-resolution dual-agent evidence image from
the real desktop cast, plus sidebar, compact, mouse-cue, playback, and
social-card smoke screenshots. The dual-agent image is committed for local
builds; transient verification images remain ignored. The Pages workflow
regenerates all three casts and every screenshot before deployment, then
includes them in the static artifact. The recorder waits for the Railmux
sidebar and verifies every scripted milestone before writing a cast, so a
partial CI recording fails closed instead of being published.
