// Render evals/vision/sheets/*.html to evals/vision/<id>.jpg with the e2e Playwright Chromium
// (design doc 9.3 vision set). Run after `uv run python -m scripts.vision_sheets`:
//     node scripts/render_sheets.mjs
import { readdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(join(root, "web", "package.json"));
const { chromium } = require("playwright");

const dir = join(root, "evals", "vision");
const browser = await chromium.launch({ channel: "chromium" }); // as in playwright.config.ts
const page = await browser.newPage({ deviceScaleFactor: 1.5 });
for (const f of readdirSync(join(dir, "sheets")).filter((x) => x.endsWith(".html"))) {
  await page.goto(pathToFileURL(join(dir, "sheets", f)).href);
  const out = join(dir, f.replace(/\.html$/, ".jpg"));
  await page.locator(".paper").screenshot({ path: out, type: "jpeg", quality: 80 });
  console.log("rendered", out.slice(root.length + 1));
}
await browser.close();
