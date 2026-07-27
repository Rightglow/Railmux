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
login. It produces `public/generated/railmux-demo.cast`, which the website
plays directly as text through asciinema-player. The recorder launches Railmux
through its normal CLI, opens a local demo-agent pane, and removes the private
tmux server when it finishes. It never reads the user's normal HOME or provider
configuration.
The random temporary root is replaced by an equal-width stable demo path in
the cast, keeping generated output reviewable without changing terminal cell
positions.

The workflow then creates deterministic product screenshots:

```bash
npx playwright install chromium
npm run screenshots
```

Generated PNG files (including a playback smoke screenshot) live under
`public/generated/` and are intentionally ignored by Git. The Pages workflow
regenerates the real terminal cast and the screenshots before every deployment,
then includes them in the static artifact. The large hero workspace remains a
responsive HTML/CSS product illustration; the “Real terminal capture” section
is the source-authentic recording.
