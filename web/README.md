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
  including a second live agent pane and a reviewed real-agent response.
- `public/generated/railmux-workflow-demo.cast` records a focused 76×30
  sidebar workflow. Semantic input events drive the website's durable key HUD
  and cell-aligned mouse pointer.
- `public/generated/railmux-mobile-demo.cast` records Railmux's real 46×26
  compact layout and page controls for the small-screen section.

The website plays all three casts directly as text through asciinema-player. The
recorder launches Railmux through its normal CLI, opens isolated local
demo-agent panes, and removes the private tmux server when it finishes. It
never reads the user's normal HOME or provider configuration.
The public text in `demo/real-agent-response.txt` was generated once with the
user's existing Claude authorization in `--no-session-persistence` mode,
reviewed for private paths and identifiers, and committed as plain text. CI
only replays that fixture; it never receives or invokes provider credentials.
Every full or terminal-truncated temporary root is replaced cell-for-cell by a
stable public demo path, keeping generated output reviewable without changing
terminal positions.

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
