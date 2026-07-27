import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { test, expect } from "@playwright/test";

const outputDir = join(process.cwd(), "public", "generated");

test.beforeAll(async () => {
  await mkdir(outputDir, { recursive: true });
});

test("capture deterministic desktop and social previews", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");
  const desktopDemo = page.locator('[data-demo="desktop"]');
  await expect(desktopDemo).toBeVisible();
  await expect(page.locator("h1")).toContainText("Keep every");
  await expect(desktopDemo.locator(".agent-heading")).toHaveCount(0);
  await expect(desktopDemo.locator(".sidebar-hints")).toContainText(
    "C-b Tab Target",
  );
  await expect(desktopDemo.locator(".sidebar-buttons")).toContainText(
    "C-b d Detach",
  );
  await expect(desktopDemo.locator(".terminal-status")).toContainText(
    "Railmux · Codex · ◧",
  );
  expect(
    await desktopDemo.locator(".terminal-status").evaluate(
      (element) => getComputedStyle(element).backgroundColor,
    ),
  ).toBe("rgb(95, 175, 0)");
  expect(
    await desktopDemo.locator(".agent-pane-active").evaluate((element) => {
      const style = getComputedStyle(element);
      return [style.borderTopWidth, style.borderBottomWidth, style.borderLeftWidth];
    }),
  ).toEqual(["0px", "0px", "0px"]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);

  await desktopDemo.screenshot({
    path: join(outputDir, "desktop-workspace.png"),
    animations: "disabled",
    scale: "css",
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
  const compactDemo = page.locator('[data-demo="compact"]');
  await compactDemo.scrollIntoViewIfNeeded();
  await expect(compactDemo).toBeVisible();
  await compactDemo.screenshot({
    path: join(outputDir, "compact-workspace.png"),
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
  await expect(page.locator('[data-demo="desktop"]')).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(pageErrors).toEqual([]);
});

test("play the isolated real-terminal recording", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(".");
  const player = page.locator(".terminal-recording-player");
  await player.scrollIntoViewIfNeeded();
  await expect(player.locator(".ap-player")).toBeVisible();
  await page.waitForTimeout(4_000);
  await player.screenshot({
    path: join(outputDir, "recorded-workspace.png"),
    animations: "disabled",
    scale: "css",
  });
  expect(pageErrors).toEqual([]);
});
