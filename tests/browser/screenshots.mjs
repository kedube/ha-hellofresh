// Regenerate the README / docs screenshots in images/ from the harness — made-up account data
// only. `npm run screenshots`. The hero is composed from two shots (hero.html).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { launchBrowser } from "./chrome.mjs";
import { startServer } from "./server.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const IMAGES = path.resolve(here, "../../images");
const SHOTS = path.join(here, "shots");
fs.mkdirSync(SHOTS, { recursive: true });

const { server, port } = await startServer(0);
const browser = await launchBrowser();
const problems = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The sidebar panel with the fixtures' "clean" account (no expiring-card banner).
async function open(params, [w, h], { scale = 1, calendar = false } = {}) {
  const page = await browser.newPage();
  await page.setViewport({ width: w, height: h, deviceScaleFactor: scale });
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  const qs = new URLSearchParams({ latency: "20", panel: "1", scenario: "clean", ...params });
  await page.goto(`http://localhost:${port}/index.html?${qs}`, { waitUntil: "networkidle0" });
  await page.evaluate((cal) => {
    localStorage.clear();
    if (cal) localStorage.setItem("hellofresh-card:schedule-mode", "calendar");
  }, calendar);
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForFunction(() => window.__ready === true, { timeout: 15000 });
  await page.waitForFunction(
    () => {
      const main = window.__card.shadowRoot.querySelector(".js-main");
      return main && main.innerHTML.trim() && !main.querySelector(".hf-skeleton");
    },
    { timeout: 15000 }
  );
  await sleep(1200); // images
  return page;
}
const click = (page, sel) =>
  page.evaluate((s) => {
    const el = window.__card.shadowRoot.querySelector(s);
    if (!el) return false;
    el.click();
    return true;
  }, sel);
// Scroll the panel so an element sits `offset` px from the top.
const scrollTo = (page, sel, offset = 12) =>
  page.evaluate(
    ([s, off]) => {
      const el = window.__card.shadowRoot.querySelector(s);
      if (el && window.__panel) window.__panel.scrollTop += el.getBoundingClientRect().top - off;
    },
    [sel, offset]
  );
async function shot(page, file, clip) {
  await sleep(500);
  await page.screenshot({ path: file, ...(clip ? { clip } : {}) });
  console.log("wrote", path.relative(path.resolve(here, "../.."), file));
  await page.close();
}
const image = (name) => path.join(IMAGES, name);
const W = [1180, 760];

// Hero parts: the panel beside a Home Assistant-style sidebar (desktop, light) and a phone (dark).
await shot(await open({ frame: "ha" }, [1440, 900]), path.join(SHOTS, "hero-desktop.png"));
await shot(await open({ narrow: "1", theme: "dark", view: "menu" }, [390, 844], { scale: 2 }), path.join(SHOTS, "hero-phone.png"));

await shot(await open({}, W), image("card-overview.png"));
let page = await open({ view: "menu" }, W);
await scrollTo(page, ".hf-weekbar", 6);
await shot(page, image("card-menu.png"));
page = await open({ view: "menu" }, W);
if (!(await click(page, ".hf-grid .hf-optselect"))) problems.push("no customize button");
await sleep(800);
await shot(page, image("card-customize.png"));
page = await open({ view: "menu" }, W);
await click(page, '[data-action="toggle-filters"]');
await sleep(300);
await click(page, '[data-action="f-protein"][data-value="Seafood"]');
await sleep(700);
await click(page, '[data-action="toggle-filters"]');
await sleep(500);
await scrollTo(page, ".hf-toolbar", 6);
await shot(page, image("card-filters.png"));
page = await open({}, [1400, 800], { calendar: true });
await scrollTo(page, ".hf-cal", 70);
await shot(page, image("card-calendar.png"));
page = await open({}, W);
if (!(await click(page, '.hf-row[data-action="delivery"]'))) problems.push("no delivery row");
await sleep(900);
await shot(page, image("card-delivery.png"));
await shot(await open({ view: "market" }, W), image("card-market.png"));
await shot(await open({ view: "recipes" }, W), image("card-recipes.png"));
await shot(await open({ view: "account" }, W), image("card-account.png"));
await shot(await open({ narrow: "1" }, [390, 844], { scale: 2 }), image("card-phone.png"));

// Live tracking (the Netherlands) on delivery day, cropped to the next box.
page = await open({ scenario: "live", now: "2026-10-05T17:40:00" }, [1180, 900]);
const hero = await page.evaluate(() => {
  const r = window.__card.shadowRoot.querySelector(".hf-hero").getBoundingClientRect();
  return { x: Math.max(0, r.left - 16), y: Math.max(0, r.top - 16), width: r.width + 32, height: r.height + 32 };
});
await shot(page, image("card-live.png"), hero);

// Compose the hero.
page = await browser.newPage();
await page.setViewport({ width: 1600, height: 980, deviceScaleFactor: 1 });
await page.goto(`http://localhost:${port}/hero.html`, { waitUntil: "networkidle0" });
await sleep(400);
await shot(page, image("hero.png"));

await browser.close();
server.close();
if (problems.length) {
  console.log("PROBLEMS:\n" + problems.join("\n"));
  process.exit(1);
}
