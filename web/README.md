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

The website workflow also creates deterministic product screenshots:

```bash
npx playwright install chromium
npm run screenshots
```

Generated PNG files live under `public/generated/` and are intentionally
ignored by Git. The Pages workflow regenerates them before every deployment,
then includes them in the static artifact. The visible terminal mock is HTML
and CSS rather than a claim that a real provider session was recorded.

Real Claude Code or Codex recordings should use an isolated environment and a
manually triggered, reviewable workflow. They must never be added to ordinary
pull-request CI with provider credentials.
