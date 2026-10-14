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

// Puppeteer gives Chrome 30 seconds to start. On the first CI run on GitHub's 2026-10-12 Ubuntu
// image (Chrome 155) it never did, though the same Chrome starts in under a second on a Mac. So it
// gets a minute, then one more try with Chrome's own output in the log, to show why.
const LAUNCH_TIMEOUT_MS = 60_000;

export async function launchBrowser() {
  const executablePath = CANDIDATES.find((p) => fs.existsSync(p));
  if (!executablePath) throw new Error("No Chrome found; set CHROME_PATH");
  const options = {
    executablePath,
    headless: "new",
    args: ["--no-sandbox", "--font-render-hinting=none"],
    timeout: LAUNCH_TIMEOUT_MS,
  };
  try {
    return await puppeteer.launch(options);
  } catch (err) {
    console.warn(`Chrome didn't start (${err.message}); trying once more, with its output shown`);
    return puppeteer.launch({ ...options, dumpio: true });
  }
}
