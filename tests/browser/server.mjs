// Tiny static server for the HelloFresh card harness: the integration's www/ at /hellofresh/, its
// translation files at /translations/ (the fake Home Assistant serves the card's text from them),
// MDI icon paths at /mdi.js, procedural "plate" images at /img/*.svg, and the harness page itself —
// also under /hellofresh-app/..., the sidebar panel's address. `node server.mjs` serves on $PORT
// (8765); the flows and screenshots start their own on a free port.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const WWW = path.resolve(here, "../../custom_components/hellofresh/www");
const TRANSLATIONS = path.resolve(here, "../../custom_components/hellofresh/translations");

const TYPES = {
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".html": "text/html",
  ".json": "application/json",
  ".png": "image/png",
  ".svg": "image/svg+xml",
};

function hash(s) {
  let h = 2166136261;
  for (const c of String(s)) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return Math.abs(h);
}

// A stylised overhead plate on a tabletop, seeded so each recipe keeps its own look.
function plate(seed) {
  const h = hash(seed);
  const rnd = (n) => {
    const x = Math.sin(h * (n + 1)) * 10000;
    return x - Math.floor(x);
  };
  const tables = ["#2b2f33", "#e9e4dc", "#3a2d24", "#d9d4cb", "#1f3a33", "#f1ede6"];
  const plates = ["#f7f5f0", "#2a2a2a", "#e8ecef", "#c9d6cf"];
  const foods = ["#d9822b", "#7fb069", "#e4b363", "#c0392b", "#f4d35e", "#8e5a3c", "#4f7942", "#f28f3b", "#b5651d", "#e76f51", "#90be6d"];
  const table = tables[h % tables.length];
  const pl = plates[(h >> 3) % plates.length];
  let blobs = "";
  for (let i = 0; i < 7; i += 1) {
    const a = rnd(i) * Math.PI * 2;
    const r = 30 + rnd(i + 10) * 70;
    const cx = 300 + Math.cos(a) * r;
    const cy = 225 + Math.sin(a) * r * 0.9;
    const rx = 28 + rnd(i + 20) * 40;
    const ry = 20 + rnd(i + 30) * 35;
    const color = foods[Math.floor(rnd(i + 40) * foods.length)];
    blobs += `<ellipse cx="${cx.toFixed(0)}" cy="${cy.toFixed(0)}" rx="${rx.toFixed(0)}" ry="${ry.toFixed(0)}" fill="${color}" transform="rotate(${(rnd(i + 50) * 180).toFixed(0)} ${cx.toFixed(0)} ${cy.toFixed(0)})" opacity="0.95"/>`;
  }
  for (let i = 0; i < 18; i += 1) {
    const cx = 200 + rnd(i + 60) * 200;
    const cy = 140 + rnd(i + 70) * 170;
    blobs += `<circle cx="${cx.toFixed(0)}" cy="${cy.toFixed(0)}" r="${(4 + rnd(i + 80) * 7).toFixed(0)}" fill="${foods[(i + h) % foods.length]}"/>`;
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 450" width="600" height="450">
    <defs><radialGradient id="g" cx="50%" cy="45%" r="60%"><stop offset="0" stop-color="#fff" stop-opacity="0.18"/><stop offset="1" stop-color="#000" stop-opacity="0.25"/></radialGradient></defs>
    <rect width="600" height="450" fill="${table}"/>
    <rect x="${rnd(90) * 80}" y="0" width="${40 + rnd(91) * 60}" height="450" fill="#000" opacity="0.05"/>
    <ellipse cx="306" cy="236" rx="190" ry="176" fill="#000" opacity="0.18"/>
    <circle cx="300" cy="225" r="180" fill="${pl}"/>
    <circle cx="300" cy="225" r="150" fill="none" stroke="#000" stroke-opacity="0.06" stroke-width="3"/>
    ${blobs}
    <rect width="600" height="450" fill="url(#g)"/>
    <g opacity="0.8"><rect x="520" y="60" width="12" height="330" rx="6" fill="#c9a24b"/><rect x="545" y="60" width="12" height="330" rx="6" fill="#c9a24b"/></g>
  </svg>`;
}

function handle(req, res) {
  const url = new URL(req.url, "http://localhost");
  let file = null;
  let body = null;
  let type = "text/plain";
  if (url.pathname.startsWith("/hellofresh/")) {
    file = path.join(WWW, url.pathname.slice("/hellofresh/".length));
  } else if (/^\/translations\/[a-zA-Z-]+\.json$/.test(url.pathname)) {
    file = path.join(TRANSLATIONS, path.basename(url.pathname));
  } else if (url.pathname === "/mdi.js") {
    file = path.join(here, "node_modules/@mdi/js/mdi.js");
  } else if (url.pathname.startsWith("/img/")) {
    body = plate(decodeURIComponent(url.pathname.slice(5)));
    type = "image/svg+xml";
  } else if (url.pathname === "/" || url.pathname === "/index.html" || url.pathname.startsWith("/hellofresh-app")) {
    file = path.join(here, "index.html");
  } else {
    file = path.join(here, url.pathname.slice(1));
  }
  if (body !== null) {
    res.writeHead(200, { "content-type": type, "cache-control": "no-store" });
    res.end(body);
    return;
  }
  fs.readFile(file, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end("not found");
      return;
    }
    res.writeHead(200, { "content-type": TYPES[path.extname(file)] || "application/octet-stream", "cache-control": "no-store" });
    res.end(data);
  });
}

// Start serving; resolves to the port (a free one for 0).
export function startServer(port = 0) {
  const server = http.createServer(handle);
  return new Promise((resolve) => server.listen(port, () => resolve({ server, port: server.address().port })));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const { port } = await startServer(Number(process.env.PORT || 8765));
  console.log(`harness on http://localhost:${port}`);
}
