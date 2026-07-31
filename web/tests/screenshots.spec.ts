import { mkdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { test, expect } from "@playwright/test";

const outputDir = join(process.cwd(), "public", "generated");

test.use({ deviceScaleFactor: 2 });

test.beforeAll(async () => {
  await mkdir(outputDir, { recursive: true });
});

test("honor direct section links after React mounts", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 1280, height: 800 });

  await page.goto("./#install");

  const install = page.locator("#install");
  await expect(install).toBeVisible();
  await install.evaluate((element) => {
    const delayedContent = document.createElement("div");
    delayedContent.dataset.testid = "delayed-content";
    delayedContent.style.height = "600px";
    element.before(delayedContent);
  });
  await expect
    .poll(async () => install.evaluate((element) => {
      const top = element.getBoundingClientRect().top;
      return top >= 70 && top < window.innerHeight;
    }))
    .toBe(true);

  await page.evaluate(() => {
    window.dispatchEvent(new WheelEvent("wheel"));
    window.scrollTo(0, 0);
    const laterContent = document.createElement("div");
    laterContent.style.height = "600px";
    document.querySelector("#install")?.before(laterContent);
  });
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
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
          duration: number;
          transcript_sha256: string;
        };
        const timestamps = cast
          .trimEnd()
          .split("\n")
          .slice(1)
          .map((line) => Number(JSON.parse(line)[0]));
        return { ...header, cast, timestamps };
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
  expect(
    headers[0].timestamps.every(
      (timestamp) => timestamp <= headers[0].duration,
    ),
  ).toBe(true);
  expect(headers[0].cast).toContain("Restoring your workspace");
  expect(headers[0].cast).toContain('[2.4');
  expect(headers[0].cast).toContain("start your work");
  expect(headers[1].cast).toContain("Claude Code v2.1.220");
  expect(headers[1].cast).toContain("OpenAI Codex (v0.145.0)");
  expect(headers[2].cast).toContain("Preview stopped session");
  expect(headers[2].cast).toContain("Read-only history preview");
  expect(headers[2].cast).toContain("(no running Claude Code sessions)");
  expect(headers[2].cast).toContain("Start an empty session");
  expect(headers[2].cast).not.toContain("Verify responsive layout gates");
  expect(headers[2].cast).toContain("Switch to Polish SSH history");
  expect(headers[2].cast).not.toContain('"key|');
  expect(headers[2].cast).toContain(
    "keymouse|Enter|10|12|Resume this conversation",
  );
  expect(headers[2].cast).toContain(
    "keymouse|N|10|10|Start an empty session",
  );
  expect(headers[3].cast).toContain("touch|10|10|Tap New session");
  expect(headers[3].cast).toContain("touch|2|38|Tap [R] sidebar");
  expect(headers[3].cast).toContain("touch|5|38|Tap [1] agent");
  expect(headers[3].cast).not.toContain("key|");
  expect(headers[5].cast).toContain("Soft quit — keep agents running");
  expect(headers[5].cast).toContain(
    "keymouse|+|48|37|Show Mode, Layout, and Options",
  );
  expect(headers[5].cast).toContain(
    "key|S|Soft quit — keep agents running",
  );
  expect(headers[5].cast).not.toContain(
    "Skip layout save and finish soft quit",
  );
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
  await expect(desktopDemo.locator(".ap-term")).toContainText(
    "start your work",
    { timeout: 6_000 },
  );
  await expect(desktopDemo.locator(".ap-term")).not.toContainText("●");
  const hasSemanticAgentColor = await desktopDemo.locator(".ap-line").evaluateAll(
    (lines) => {
      const evidence = lines.filter((line) =>
        /Claude Code v2\.1\.220/.test(line.textContent ?? "")
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

test("show real entry points and the workflow session menu", async ({ page }) => {
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

  await expect(
    page.locator(".feature-entrypoints [data-demo]"),
  ).toHaveCount(2);
  await expect(
    page.locator(".feature-entrypoints").getByText(
      "Start fresh. Get help.",
      { exact: true },
    ),
  ).toBeVisible();
  const sessionMenu = page.locator(
    '#workflow [data-demo="session-menu-recording"]',
  );
  await expect(
    page.locator('#features [data-demo="session-menu-recording"]'),
  ).toHaveCount(0);
  await sessionMenu.scrollIntoViewIfNeeded();
  await expect(sessionMenu.locator(".ap-term")).toContainText("Copy title");
  await expect(sessionMenu.locator(".ap-term")).toContainText("Rename");
  await expect(sessionMenu.locator(".ap-term")).toContainText("Codex");
  await expect(sessionMenu.locator(".ap-term")).toContainText(
    "Explain workspace layout",
  );
  await expect(sessionMenu.locator(".ap-term")).toContainText(
    "Read-only history preview",
  );
  await page.locator(".workflow-menu-proof").screenshot({
    path: join(outputDir, "session-menu.png"),
    animations: "disabled",
    scale: "css",
  });
  await expect(
    page.getByText("without entering tmux copy-mode", { exact: false }),
  ).toBeVisible();
  const terminalWidths = await page
    .locator(".entrypoint-terminal-viewport")
    .evaluateAll((items) =>
      items.map((item) => item.getBoundingClientRect().width),
    );
  expect(Math.min(...terminalWidths)).toBeGreaterThan(600);
  await page.locator(".feature-entrypoints").screenshot({
    path: join(outputDir, "entrypoints-section.png"),
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
  await expect(mobileRecording.getByTestId("terminal-touch")).toBeVisible({
    timeout: 7_000,
  });
  await expect(mobileRecording.getByTestId("terminal-input-hud")).toContainText(
    "Tap New session",
  );
  await expect(mobileRecording.getByTestId("terminal-input-hud")).toContainText(
    "Tap [R] sidebar",
    { timeout: 5_000 },
  );
  await expect(mobileRecording.locator(".ap-term")).toContainText("PROJECTS");
  await expect(mobileRecording.getByTestId("terminal-input-hud")).toContainText(
    "Tap [1] agent",
    { timeout: 4_000 },
  );
  await expect(mobileRecording.locator(".ap-term")).toContainText(
    "Explain why a 76x30 Railmux terminal uses",
  );
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
  await expect(
    page.getByText(
      "Mode switches the sidebar between Claude Code and Codex",
      { exact: false },
    ),
  ).toBeVisible();
  await expect(
    page.getByText("agents in the other mode keep running", { exact: false }),
  ).toBeVisible();
  const player = page.locator('[data-demo="controls-recording"]');
  await player.scrollIntoViewIfNeeded();
  await expect(player.locator(".ap-player")).toBeVisible();
  await expect(player.locator(".ap-control-bar")).toHaveCount(0);
  const hud = player.getByTestId("terminal-input-hud");
  const pointer = player.getByTestId("terminal-pointer");
  const mouseTarget = player.getByTestId("terminal-mouse-target");

  await expect(hud).toContainText("Start a Claude Code session", {
    timeout: 5_000,
  });
  await expect(pointer).toBeVisible();
  await expect(mouseTarget).toContainText("Click New session");
  await expect(hud).toContainText("Return to Railmux", {
    timeout: 5_000,
  });
  await expect(pointer).toBeVisible();
  await expect(mouseTarget).toContainText("Click the sidebar");
  await expect(hud).toContainText("Show Mode, Layout, and Options", {
    timeout: 5_000,
  });
  await expect(pointer).toBeVisible();
  await expect(mouseTarget).toContainText("Click More");
  await expect(hud).toContainText("Switch sidebar to Codex", {
    timeout: 5_000,
  });
  await expect(pointer).toBeVisible();
  await expect(mouseTarget).toContainText("Click Mode");
  await expect(player.locator(".ap-term")).toContainText("Codex");
  await expect(hud).toContainText("Cycle workspace layout", {
    timeout: 5_000,
  });
  await expect(pointer).toBeVisible();
  await expect(mouseTarget).toContainText("Click Layout");
  await expect(hud).toContainText("Compare Quit and Soft Quit", {
    timeout: 5_000,
  });
  await expect(pointer).toBeVisible();
  await expect(mouseTarget).toContainText("Click Quit");
  await expect(player.locator(".ap-term")).toContainText("Quit railmux?");
  await expect(player.locator(".ap-term")).toContainText("soft quit");
  await expect(player.locator(".ap-term")).toContainText("sessions alive");
  await player.screenshot({
    path: join(outputDir, "controls-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  await expect(hud).toContainText("Soft quit", { timeout: 7_000 });
  await expect(pointer).toHaveCount(0);
  await expect(mouseTarget).toHaveCount(0);
  await expect(player.locator(".ap-term")).toContainText("Keep this layout?");
  await expect(player.locator(".ap-term")).toContainText(
    "Keeping 1 agent session running.",
    { timeout: 7_000 },
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
  const sectionIds = await page.locator("main > section").evaluateAll(
    (sections) => sections.map((section) => section.id).filter(Boolean),
  );
  expect(sectionIds.indexOf("workflow")).toBeLessThan(
    sectionIds.indexOf("features"),
  );
  const workflowSteps = page.locator(".workflow-steps");
  await expect(workflowSteps.getByText("Preview", { exact: true })).toBeVisible();
  await expect(workflowSteps.getByText("Resume", { exact: true })).toBeVisible();
  await expect(workflowSteps.getByText("Switch", { exact: true })).toBeVisible();
  await expect(workflowSteps.getByText("Manage", { exact: true })).toBeVisible();
  await expect(workflowSteps).toContainText("RIGHT-CLICK");
  const pointerNote = page.locator(".workflow-pointer-note");
  await expect(pointerNote).toBeVisible();
  await expect(pointerNote).toContainText("MOUSE INPUT");
  await expect(pointerNote).toContainText("COPY");
  await expect(pointerNote).toContainText("railmux ssh");
  await expect(
    pointerNote.getByRole("link", { name: "Check terminal setup" }),
  ).toHaveAttribute(
    "href",
    "https://github.com/Rightglow/Railmux#2-mouse-buttons-or-f8f9-dont-work--whats-wrong",
  );
  await expect(
    pointerNote.getByRole("link", { name: "See every copy path" }),
  ).toHaveAttribute(
    "href",
    "https://github.com/Rightglow/Railmux#1-how-do-i-copy-text-from-the-agent-pane",
  );
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
  await expect(pointer).toBeVisible();
  await expect(player.getByTestId("terminal-mouse-target")).toContainText(
    "Double-click the selected session",
  );
  await expect(pointer).toHaveAttribute("data-clicks", "2");
  await expect(pointer.locator("i")).toHaveCount(2);
  await expect(player.locator(".ap-term")).toContainText(
    "Polish SSH history",
  );
  await expect(hud).toContainText("Back to the sidebar", { timeout: 8_000 });
  await expect(pointer).toBeVisible();
  await expect(player.getByTestId("terminal-mouse-target")).toContainText(
    "Click the sidebar",
  );
  await expect(player.locator(".ap-term")).toContainText("PROJECTS");
  await player.screenshot({
    path: join(outputDir, "recorded-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  await expect(hud).toContainText("Start an empty session", {
    timeout: 5_000,
  });
  await expect(pointer).toBeVisible();
  await expect(player.getByTestId("terminal-mouse-target")).toContainText(
    "Click New session",
  );
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
  await expect(pointer).toBeVisible();
  await expect(player.getByTestId("terminal-mouse-target")).toContainText(
    "Click the sidebar",
  );
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
