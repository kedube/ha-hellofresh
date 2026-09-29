// Launch the Chrome that is there: $CHROME_PATH, else the usual macOS / Linux locations.
import fs from "node:fs";
import puppeteer from "puppeteer-core";

const CANDIDATES = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium-browser",
  "/usr/bin/chromium",
].filter(Boolean);

export function launchBrowser() {
  const executablePath = CANDIDATES.find((p) => fs.existsSync(p));
  if (!executablePath) throw new Error("No Chrome found; set CHROME_PATH");
  return puppeteer.launch({ executablePath, headless: "new", args: ["--no-sandbox", "--font-render-hinting=none"] });
}
