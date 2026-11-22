// M36 (spec/mfa-web OQ-1): the production build's QR chunk must hold no network API name.
// Run `pnpm build` first. The QR library is loaded by a dynamic import, so it has its own chunk: the one that holds
// the drawing component (its "mfa-qr" test id). The bare word "http" is not searched (it would match
// comments and licence text).
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const dist = new URL("../dist/assets/", import.meta.url).pathname;
const NAMES = ["fetch", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "importScripts"];
const chunks = readdirSync(dist).filter((f) => f.endsWith(".js"));
const qr = chunks.filter((f) => readFileSync(join(dist, f), "utf8").includes("mfa-qr"));
if (qr.length === 0) {
  console.error("FAILED: no chunk holds the QR library (marker mfa-qr not found)");
  process.exit(1);
}
let failed = false;
for (const file of qr) {
  const text = readFileSync(join(dist, file), "utf8");
  if (text.length > 100_000) {
    console.error(`FAILED: ${file} is ${text.length} bytes: the QR library is not in its own chunk`);
    failed = true;
  }
  for (const name of NAMES) {
    if (text.includes(name)) {
      console.error(`FAILED: ${file} contains ${name}`);
      failed = true;
    }
  }
}
if (failed) process.exit(1);
console.log(`ok: ${qr.join(", ")} hold the QR library and name no network API`);
