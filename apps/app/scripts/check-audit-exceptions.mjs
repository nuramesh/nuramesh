// Guards audit exceptions (the design decision ND14). Fails when:
//  1. an exception's review date has passed (a warning is printed WARN_DAYS before, so a renewal is
//     planned instead of found red; AUDIT_TODAY=YYYY-MM-DD fixes the clock for tests),
//  2. an excepted package appears in the production dependency tree,
//  3. pnpm-workspace.yaml ignores an advisory that audit-exceptions.json doesn't list (or vice versa).
// Usage: pnpm list --prod --depth Infinity --json | node scripts/check-audit-exceptions.mjs
import { readFileSync } from "node:fs";

const { exceptions } = JSON.parse(readFileSync("audit-exceptions.json", "utf8"));
const workspace = readFileSync("pnpm-workspace.yaml", "utf8");
const ignored = new Set(workspace.match(/GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}/g) ?? []);
const listed = new Set(exceptions.map((e) => e.ghsa));
const today = process.env.AUDIT_TODAY ?? new Date().toISOString().slice(0, 10);
const WARN_DAYS = 14;
const daysUntil = (date) => Math.round((Date.parse(date) - Date.parse(today)) / 86_400_000);

const prodPackages = new Set();
const walk = (node) => {
  for (const [name, child] of Object.entries(node.dependencies ?? {})) {
    prodPackages.add(name);
    walk(child);
  }
};
for (const project of JSON.parse(readFileSync(0, "utf8"))) walk(project);

const errors = [];
for (const ghsa of ignored) {
  if (!listed.has(ghsa)) errors.push(`${ghsa} is ignored in pnpm-workspace.yaml but has no review entry`);
}
for (const e of exceptions) {
  if (!ignored.has(e.ghsa)) errors.push(`${e.ghsa} is listed but not ignored; remove the stale entry`);
  if (today > e.reviewBy) errors.push(`${e.ghsa} (${e.package}) review date ${e.reviewBy} has passed`);
  else if (daysUntil(e.reviewBy) <= WARN_DAYS) {
    console.log(`::warning::${e.ghsa} (${e.package}) review date ${e.reviewBy} is in ${daysUntil(e.reviewBy)} day(s): renew or remove the exception`);
  }
  if (prodPackages.has(e.package)) {
    errors.push(`${e.ghsa}: ${e.package} is now a production dependency; the exception no longer applies`);
  }
}

if (errors.length > 0) {
  for (const message of errors) console.error(`::error::${message}`);
  process.exit(1);
}
console.log(`Audit exceptions OK (${exceptions.length}; ${prodPackages.size} production packages checked).`);
