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
    ["railmux-demo.cast", "railmux-workflow-demo.cast", "railmux-mobile-demo.cast"]
      .map(async (name) => {
        const cast = await readFile(join(outputDir, name), "utf8");
        return JSON.parse(cast.split("\n", 1)[0]) as {
          width: number;
          height: number;
          transcript_sha256: string;
        };
      }),
  );

  expect(headers[0].width).toBeGreaterThanOrEqual(84);
  expect(headers[0].height).toBeGreaterThanOrEqual(26);
  expect(headers[1].width).toBeGreaterThanOrEqual(84);
  expect(headers[1].height).toBeGreaterThanOrEqual(26);
  expect(headers[2].width).toBeLessThan(80);
  expect(headers.every((header) => header.transcript_sha256.length === 64))
    .toBe(true);
});

test("capture deterministic desktop and social previews", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");
  const desktopDemo = page.locator('[data-demo="desktop-recording"]');
  await expect(desktopDemo.locator(".ap-player")).toBeVisible();
  await expect(page.locator("h1")).toContainText("Keep every");
  await expect(page.getByText("REAL CLAUDE CODE RUNS · ISOLATED RAILMUX")).toBeVisible();
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

  await desktopDemo.locator(".ap-term").screenshot({
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
    "Trace SSH wheel ownership",
  );
  const legend = page.locator(".sidebar-evidence-legend");
  await expect(legend.getByText("NEW PROJECT", { exact: true })).toBeVisible();
  await expect(legend.getByText("NEW SESSION", { exact: true })).toBeVisible();
  await expect(legend.getByText("RUNNING", { exact: true })).toBeVisible();
  await page.locator(".feature-showcase-sidebar").screenshot({
    path: join(outputDir, "sidebar-workspace.png"),
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
  await page.waitForTimeout(2_000);
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

test("play the guided recording with durable mouse and key cues", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");
  const player = page.locator('[data-demo="workflow-recording"]');
  await player.scrollIntoViewIfNeeded();
  await expect(player.locator(".ap-player")).toBeVisible();
  const hud = player.getByTestId("terminal-input-hud");
  await expect(player.locator('[data-input-kind="key"]')).toBeVisible({
    timeout: 5_000,
  });
  await expect(hud).toContainText("New session");
  await expect(hud).toContainText("N");
  await expect(player.locator(".ap-term")).toContainText(
    "Trace SSH wheel ownership",
  );
  await expect(hud).toContainText("Back to the sidebar", { timeout: 7_000 });
  await expect(player.locator(".ap-term")).toContainText("PROJECTS");
  await player.screenshot({
    path: join(outputDir, "recorded-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  const pointer = player.getByTestId("terminal-pointer");
  await expect(pointer).toBeVisible({ timeout: 5_000 });
  await expect(player.getByTestId("terminal-input-hud")).toContainText(
    "Running session",
  );
  await player.screenshot({
    path: join(outputDir, "mouse-cue-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  await expect(page.locator(".mobile-capture-label")).toHaveCount(1);
  expect(pageErrors).toEqual([]);
});
