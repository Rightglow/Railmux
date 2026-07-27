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
  await expect(page.locator('[data-demo="desktop"]')).toBeVisible();
  await expect(page.locator("h1")).toContainText("Keep every");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);

  await page.locator('[data-demo="desktop"]').screenshot({
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
