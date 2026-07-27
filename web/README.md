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
login. It produces two source-authentic recordings:

- `public/generated/railmux-demo.cast` records the full desktop workspace,
  including a second live agent pane and the keyboard actions that opened it.
- `public/generated/railmux-mobile-demo.cast` records Railmux's real 46×26
  compact layout and page controls for the small-screen section.

The website plays both casts directly as text through asciinema-player. The
recorder launches Railmux through its normal CLI, opens isolated local
demo-agent panes, and removes the private tmux server when it finishes. It
never reads the user's normal HOME or provider configuration.
The random temporary root is replaced by an equal-width stable demo path in
the cast, keeping generated output reviewable without changing terminal cell
positions.

The workflow then creates deterministic product screenshots:

```bash
npx playwright install chromium
npm run screenshots
```

The browser pass generates a high-resolution dual-agent evidence image from
the real desktop cast, plus compact, playback, and social-card smoke
screenshots. The dual-agent image is committed for local builds; transient
verification images remain ignored. The Pages workflow regenerates both casts
and every screenshot before deployment, then includes them in the static
artifact. The hero, guided workflow, feature evidence, and compact phone frame
therefore all come from the real Railmux/tmux UI rather than a hand-drawn
terminal mockup.
