import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { test, expect } from "@playwright/test";

const outputDir = join(process.cwd(), "public", "generated");

test.use({ deviceScaleFactor: 2 });

test.beforeAll(async () => {
  await mkdir(outputDir, { recursive: true });
});

test("capture deterministic desktop and social previews", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");
  const desktopDemo = page.locator('[data-demo="desktop-recording"]');
  await expect(desktopDemo.locator(".ap-player")).toBeVisible();
  await expect(page.locator("h1")).toContainText("Keep every");
  await expect(page.getByText("ACTUAL RAILMUX SESSION")).toBeVisible();
  await page.waitForTimeout(1_000);
  await desktopDemo.locator(".ap-overlay-start").evaluate(
    (element) => { (element as HTMLElement).style.display = "none"; },
  );
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

test("play the guided real-terminal recording with input overlay", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");
  const player = page.locator('[data-demo="workflow-recording"]');
  await player.scrollIntoViewIfNeeded();
  await expect(player.locator(".ap-player")).toBeVisible();
  await page.waitForTimeout(3_000);
  await expect(player.locator(".ap-overlay-keystrokes")).toBeVisible();
  await player.screenshot({
    path: join(outputDir, "recorded-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  expect(pageErrors).toEqual([]);
});
