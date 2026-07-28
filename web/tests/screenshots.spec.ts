import { mkdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { test, expect } from "@playwright/test";

const outputDir = join(process.cwd(), "public", "generated");

test.use({ deviceScaleFactor: 2 });

test.beforeAll(async () => {
  await mkdir(outputDir, { recursive: true });
});

test("reserve compact projection for the mobile recording", async () => {
  const headers = await Promise.all(
    [
      "railmux-demo.cast",
      "railmux-dual-demo.cast",
      "railmux-workflow-demo.cast",
      "railmux-mobile-demo.cast",
      "railmux-tour-demo.cast",
      "railmux-controls-demo.cast",
    ]
      .map(async (name) => {
        const cast = await readFile(join(outputDir, name), "utf8");
        const header = JSON.parse(cast.split("\n", 1)[0]) as {
          width: number;
          height: number;
          transcript_sha256: string;
        };
        return { ...header, cast };
      }),
  );

  expect(headers[0].width).toBeGreaterThanOrEqual(84);
  expect(headers[0].height).toBeGreaterThanOrEqual(26);
  expect(headers[1].width).toBeGreaterThanOrEqual(84);
  expect(headers[1].height).toBeGreaterThanOrEqual(26);
  expect(headers[2].width).toBeGreaterThanOrEqual(84);
  expect(headers[2].height).toBeGreaterThanOrEqual(26);
  expect(headers[3].width).toBe(46);
  expect(headers[3].height).toBe(38);
  expect(headers[4].width).toBeGreaterThanOrEqual(84);
  expect(headers[4].height).toBeGreaterThanOrEqual(26);
  expect(headers[5].width).toBeGreaterThanOrEqual(84);
  expect(headers[5].height).toBeGreaterThanOrEqual(26);
  expect(headers.every((header) => header.transcript_sha256.length === 64))
    .toBe(true);
  expect(headers[0].cast).toContain("Restoring your workspace");
  expect(headers[0].cast).toContain('[2.4');
  expect(headers[1].cast).toContain("Claude Code v2.1.220");
  expect(headers[1].cast).toContain("OpenAI Codex (v0.145.0)");
  expect(headers[2].cast).toContain("Preview stopped session");
  expect(headers[2].cast).toContain("Read-only history preview");
  expect(headers[2].cast).toContain("(no running Claude Code sessions)");
  expect(headers[2].cast).toContain("Start an empty session");
  expect(headers[2].cast).not.toContain("Verify responsive layout gates");
  expect(headers[2].cast).toContain("Switch to Polish SSH history");
  expect(headers[3].cast).toContain("mouse|2|38|Open [R] sidebar");
  expect(headers[3].cast).toContain("mouse|5|38|Open [1] agent");
  expect(headers[5].cast).toContain("Soft quit — keep agents running");
  expect(headers[5].cast).toContain("Skip layout save and finish soft quit");
  expect(headers[5].cast).toContain("Keeping 1 agent session running.");
});

test("capture deterministic desktop and social previews", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");
  const desktopDemo = page.locator('[data-demo="desktop-recording"]');
  await expect(desktopDemo.locator(".ap-player")).toBeVisible();
  await expect
    .poll(async () => {
      const lines = await desktopDemo.locator(".ap-line").allTextContents();
      return lines.map((line) => line.trim());
    })
    .toContain("Restoring your workspace");
  await expect(page.locator("h1")).toContainText("Keep every");
  await expect(
    page.getByText("CLAUDE CODE 2.1.220 STARTUP · ISOLATED RAILMUX"),
  ).toBeVisible();
  await page.waitForTimeout(1_000);
  const startOverlay = desktopDemo.locator(".ap-overlay-start");
  if (await startOverlay.count()) {
    await startOverlay.evaluate(
      (element) => { (element as HTMLElement).style.display = "none"; },
    );
  }
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);

  await expect(desktopDemo.locator(".ap-term")).toContainText("❯");
  await expect(desktopDemo.locator(".ap-term")).toContainText("●");
  const hasSemanticAgentColor = await desktopDemo.locator(".ap-line").evaluateAll(
    (lines) => {
      const evidence = lines.filter((line) =>
        /handle_terminal_part|test_fast_full_window/.test(
          line.textContent ?? "",
        )
      );
      return evidence.some((line) =>
        [...line.querySelectorAll("span")].some((span) => {
          const channels = getComputedStyle(span).color
            .match(/\d+/g)
            ?.slice(0, 3)
            .map(Number);
          return channels !== undefined
            && Math.max(...channels) - Math.min(...channels) >= 20;
        }),
      );
    },
  );
  expect(hasSemanticAgentColor).toBe(true);
  await expect(desktopDemo.locator(".ap-term")).not.toContainText(
    "captured 2026",
  );
  await expect(desktopDemo.locator(".ap-control-bar")).toHaveCount(0);
  await desktopDemo.locator(".ap-term").screenshot({
    path: join(outputDir, "desktop-workspace.png"),
    animations: "disabled",
    scale: "device",
  });

  const dualDemo = page.locator('[data-demo="dual-recording"]');
  await dualDemo.scrollIntoViewIfNeeded();
  await expect(dualDemo.locator(".ap-term")).toBeVisible();
  await expect(dualDemo.locator(".ap-term")).toContainText("RUNNING");
  await expect(dualDemo.locator(".ap-term")).toContainText(
    "Claude Code v2.1.220",
  );
  await expect(dualDemo.locator(".ap-term")).toContainText(
    "OpenAI Codex (v0.145.0)",
  );
  await expect(dualDemo.locator(".ap-term")).toContainText(
    "railmux/(new)",
    { timeout: 8_000 },
  );
  await expect(dualDemo.locator(".ap-term")).not.toContainText(
    "(no running Codex sessions)",
  );
  await dualDemo.locator(".ap-term").screenshot({
    path: join(outputDir, "dual-agent-workspace.png"),
    animations: "disabled",
    scale: "device",
  });

  await page.setViewportSize({ width: 1200, height: 630 });
  await page.goto(".");
  await page.screenshot({
    path: join(outputDir, "social-card.png"),
    animations: "disabled",
    scale: "css",
  });
  expect(pageErrors).toEqual([]);
});

test("show a native sidebar evidence frame", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");
  const evidence = page.locator('[data-demo="sidebar-evidence"]');
  await evidence.scrollIntoViewIfNeeded();
  await expect(evidence.locator(".ap-player")).toBeVisible();
  await expect(evidence.locator(".ap-term")).toContainText("PROJECTS");
  await expect(evidence.locator(".ap-term")).toContainText(
    "Polish SSH history",
  );
  const legend = page.locator(".sidebar-evidence-legend");
  await expect(legend.getByText("NEW PROJECT", { exact: true })).toBeVisible();
  await expect(legend.getByText("NEW SESSION", { exact: true })).toBeVisible();
  await expect(legend.getByText("RUNNING", { exact: true })).toBeVisible();
  await expect(
    page.getByText("even when they were started outside Railmux", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(page.getByText("codex exec", { exact: true })).toBeVisible();
  await page.locator(".feature-showcase-sidebar").screenshot({
    path: join(outputDir, "sidebar-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  expect(pageErrors).toEqual([]);
});

test("show real New Project and Help entry points", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");

  const newProject = page.locator('[data-demo="new-project-recording"]');
  await newProject.scrollIntoViewIfNeeded();
  await expect(newProject.locator(".ap-term")).toContainText("Choose directory");
  await newProject.locator("xpath=..").screenshot({
    path: join(outputDir, "new-project-workspace.png"),
    animations: "disabled",
    scale: "css",
  });

  const help = page.locator('[data-demo="help-recording"]');
  await expect(help.locator(".ap-term")).toContainText("Ask Railmux with");
  await expect(help.locator(".ap-term")).toContainText("Help");
  await expect(
    page.getByText("Railmux's own help workspace", { exact: false }),
  ).toBeVisible();
  await help.locator("xpath=..").screenshot({
    path: join(outputDir, "help-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  expect(pageErrors).toEqual([]);
});

test("capture deterministic compact preview", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 760, height: 900 });
  await page.goto(".");
  const compactDemo = page.locator('[data-demo="compact-recording"]');
  await compactDemo.scrollIntoViewIfNeeded();
  await expect(compactDemo).toBeVisible();
  await expect(
    compactDemo.locator('[data-demo="mobile-recording"] .ap-player'),
  ).toBeVisible();
  await expect(page.getByText("PORTRAIT COMPACT · 46×38")).toBeVisible();
  const mobileRecording = compactDemo.locator('[data-demo="mobile-recording"]');
  await expect(mobileRecording.getByTestId("terminal-pointer")).toBeVisible({
    timeout: 7_000,
  });
  await expect(mobileRecording.getByTestId("terminal-input-hud")).toContainText(
    "Open [R] sidebar",
  );
  await expect(mobileRecording.locator(".ap-term")).toContainText("PROJECTS");
  await expect(mobileRecording.getByTestId("terminal-input-hud")).toContainText(
    "Open [1] agent",
    { timeout: 4_000 },
  );
  await expect(mobileRecording.locator(".ap-term")).toContainText("Claude Code");
  await compactDemo.screenshot({
    path: join(outputDir, "mobile-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(".");
  await expect(
    page.locator('[data-demo="desktop-recording"] .ap-player'),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(pageErrors).toEqual([]);
});

test("show real mode, layout, and quit controls", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");
  const player = page.locator('[data-demo="controls-recording"]');
  await player.scrollIntoViewIfNeeded();
  await expect(player.locator(".ap-player")).toBeVisible();
  await expect(player.locator(".ap-control-bar")).toHaveCount(0);
  const hud = player.getByTestId("terminal-input-hud");

  await expect(hud).toContainText("Show Mode, Layout, and Options", {
    timeout: 8_000,
  });
  await expect(hud).toContainText("Switch sidebar to Codex", {
    timeout: 5_000,
  });
  await expect(player.locator(".ap-term")).toContainText("Codex");
  await expect(hud).toContainText("Compare Quit and Soft Quit", {
    timeout: 8_000,
  });
  await expect(player.locator(".ap-term")).toContainText("Quit railmux?");
  await expect(player.locator(".ap-term")).toContainText("soft quit");
  await expect(player.locator(".ap-term")).toContainText("sessions alive");
  await player.screenshot({
    path: join(outputDir, "controls-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  await expect(hud).toContainText("Soft quit", { timeout: 7_000 });
  await expect(player.locator(".ap-term")).toContainText("Keep this layout?");
  await expect(hud).toContainText("finish soft quit", { timeout: 7_000 });
  await expect(player.locator(".ap-term")).toContainText(
    "Keeping 1 agent session running.",
    { timeout: 5_000 },
  );
  await player.screenshot({
    path: join(outputDir, "soft-quit-complete.png"),
    animations: "disabled",
    scale: "css",
  });
  expect(pageErrors).toEqual([]);
});

test("play the guided recording with durable mouse and key cues", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");
  const player = page.locator('[data-demo="workflow-recording"]');
  await player.scrollIntoViewIfNeeded();
  await expect(player.locator(".ap-player")).toBeVisible();
  await expect(player.locator(".ap-control-bar")).toHaveCount(0);
  const hud = player.getByTestId("terminal-input-hud");
  const pointer = player.getByTestId("terminal-pointer");
  await expect(pointer).toBeVisible({
    timeout: 5_000,
  });
  await expect(hud).toContainText("Preview stopped session");
  await player.screenshot({
    path: join(outputDir, "preview-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  await expect(player.locator('[data-input-kind="key"]')).toBeVisible({
    timeout: 5_000,
  });
  await expect(hud).toContainText("Resume this conversation");
  await expect(hud).toContainText("Enter");
  await expect(player.locator(".ap-term")).toContainText(
    "Polish SSH history",
  );
  await expect(hud).toContainText("Back to the sidebar", { timeout: 8_000 });
  await expect(player.locator(".ap-term")).toContainText("PROJECTS");
  await player.screenshot({
    path: join(outputDir, "recorded-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  await expect(hud).toContainText("Start an empty session", {
    timeout: 5_000,
  });
  await expect(player.locator(".ap-term")).toContainText(
    "Claude Code v2.1.220",
  );
  await expect(player.locator(".ap-term")).not.toContainText(
    "●",
    { timeout: 5_000 },
  );
  await expect(hud).toContainText("See both running sessions", {
    timeout: 6_000,
  });
  await expect(pointer).toBeVisible({ timeout: 5_000 });
  await expect(player.getByTestId("terminal-input-hud")).toContainText(
    "Switch to Polish SSH history",
  );
  await expect(player.locator(".ap-term")).toContainText(
    "local history overlay now skips superseded frames",
    { timeout: 5_000 },
  );
  await player.screenshot({
    path: join(outputDir, "mouse-cue-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  await expect(page.locator(".mobile-capture-label")).toHaveCount(1);
  expect(pageErrors).toEqual([]);
});
