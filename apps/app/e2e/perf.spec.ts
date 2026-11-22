/**
 * Speed targets (ND10, PLAN §8): 60 fps canvas with 500+ agents while panning, zooming and
 * dragging; < 100 ms from input to visual response. Runs against the production build, after the
 * other e2e tests (separate Playwright project), so nothing else competes for the CPU.
 *
 * Thresholds: median frame ≤ 16.7 ms (+ the timer resolution, below) and p95 ≤ 25 ms; interaction
 * latency < 100 ms. They are strict and **blocking locally** (`pnpm e2e:perf`). In pull-request CI
 * the same checks run as an informational step (decision T14): GitHub's shared runners have no GPU
 * and noisy neighbours, so the tail (p95) there is timing noise, not a regression. The measurement
 * is kept and reported in the job summary. PERF_* variables can still override the limits.
 *
 * What the checks do and don't show (review notes):
 * - Frame test: headless Chrome locks `requestAnimationFrame` to 60 Hz, so the median sits at the
 *   16.7 ms refresh interval whenever the work fits inside a frame. It detects **dropped frames**
 *   (a dropped frame doubles an interval and shows in the p95), not CPU headroom: a 14 ms frame
 *   passes the same as a 2 ms one.
 * - Latency test: it times `click()` / edits inside the page until the DOM shows the change plus
 *   one frame. It excludes the browser's input pipeline, so it bounds our code's response time,
 *   not what a person's hardware adds.
 */
import { expect, type Page, test } from "@playwright/test";

import { center, drag, expectAgents, generatedOrg, importJson, node, openBuilder } from "./builder-helpers";

const MEDIAN_MAX = Number(process.env.PERF_MEDIAN_MAX_MS ?? "16.7");
const P95_MAX = Number(process.env.PERF_P95_MAX_MS ?? "25");
const INTERACTION_MAX = 100;
/** Chrome coarsens rAF timestamps to 0.1 ms, so a 16.67 ms frame reads as 16.6–16.8 ms. */
const TIMER_RESOLUTION = 0.1;

declare global {
  interface Window {
    __frames?: [number, string][];
    __recording?: boolean;
    __phase?: string;
  }
}

async function startFrames(page: Page): Promise<void> {
  await page.evaluate(() => {
    window.__frames = [];
    window.__recording = true;
    const tick = (time: number): void => {
      if (!window.__recording) return;
      window.__frames?.push([time, window.__phase ?? ""]);
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}

interface FrameStats {
  median: number;
  p95: number;
  frames: number;
}

function stats(deltas: number[]): FrameStats {
  const sorted = [...deltas].sort((a, b) => a - b);
  const pick = (q: number) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] ?? Infinity;
  return { median: pick(0.5), p95: pick(0.95), frames: sorted.length };
}

/** Frame-time stats overall and per phase (pan, zoom, drag), from rAF timestamps. */
async function stopFrames(page: Page): Promise<{ all: FrameStats; phases: Record<string, FrameStats> }> {
  const frames = await page.evaluate(() => {
    window.__recording = false;
    return window.__frames ?? [];
  });
  const all: number[] = [];
  const byPhase: Record<string, number[]> = {};
  frames.slice(1).forEach(([time, phase], i) => {
    const delta = time - (frames[i]?.[0] ?? time);
    all.push(delta);
    (byPhase[phase] ??= []).push(delta);
  });
  return { all: stats(all), phases: Object.fromEntries(Object.entries(byPhase).map(([k, v]) => [k, stats(v)])) };
}

const phase = (page: Page, name: string) =>
  page.evaluate((value) => {
    window.__phase = value;
  }, name);

const show = (s: FrameStats) => `frames=${String(s.frames)} median=${s.median.toFixed(2)}ms p95=${s.p95.toFixed(2)}ms`;

test.describe.configure({ mode: "serial" });

test.beforeEach(async ({ page }) => {
  await openBuilder(page);
  await importJson(page, "big.json", generatedOrg(500, true));
  await expectAgents(page, 500);
  await expect(node(page, "a0")).toBeVisible();
});

test("500 agents: panning, zooming and dragging stay at 60 fps", async ({ page }, info) => {
  const pane = await page.locator(".react-flow__pane").boundingBox();
  if (pane === null) throw new Error("pane not visible");
  const visible = await page.locator(".react-flow__node").count();
  expect(visible).toBe(500); // fitted view: every agent is on screen (worst case for rendering)

  await startFrames(page);
  const started = Date.now();
  const empty = { x: pane.x + 8, y: pane.y + 8 }; // fitView leaves padding around the graph
  while (Date.now() - started < 3_000) {
    await phase(page, "pan");
    await drag(page, empty, { x: empty.x + 120, y: empty.y + 60 }, 20);
    await drag(page, { x: empty.x + 120, y: empty.y + 60 }, empty, 20);
    await phase(page, "zoom");
    await page.mouse.move(pane.x + pane.width / 2, pane.y + pane.height / 2);
    for (let i = 0; i < 4; i += 1) await page.mouse.wheel(0, -60);
    for (let i = 0; i < 4; i += 1) await page.mouse.wheel(0, 60);
    await phase(page, "drag");
    const from = await center(node(page, "a0"));
    await drag(page, from, { x: from.x + 80, y: from.y + 40 }, 20);
    await drag(page, { x: from.x + 80, y: from.y + 40 }, from, 20);
  }
  const { all: result, phases } = await stopFrames(page);
  const detail = Object.entries(phases)
    .filter(([name]) => name !== "")
    .map(([name, s]) => `${name}: ${show(s)}`)
    .join("; ");
  info.annotations.push({
    type: "perf",
    description: `${show(result)} (limits ${String(MEDIAN_MAX)}/${String(P95_MAX)}) | ${detail}`,
  });
  console.log(`[perf] canvas 500 agents: ${info.annotations.at(-1)?.description ?? ""}`);
  expect(result.frames).toBeGreaterThan(60);
  expect(result.median).toBeLessThanOrEqual(MEDIAN_MAX + TIMER_RESOLUTION);
  expect(result.p95).toBeLessThanOrEqual(P95_MAX + TIMER_RESOLUTION);
});

test("500 agents: adding an agent and editing one update the canvas in < 100 ms", async ({ page }, info) => {
  await node(page, "a0").click();
  const addLatency = await page.evaluate(async () => {
    const button = [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) =>
      b.textContent.includes("Developer") && b.draggable,
    );
    if (button === undefined) throw new Error("no palette button");
    const started = performance.now();
    button.click();
    await new Promise<void>((resolve) => {
      const done = (): boolean => document.querySelector('.react-flow__node[data-id="developer"]') !== null;
      if (done()) {
        resolve();
        return;
      }
      const observer = new MutationObserver(() => {
        if (done()) {
          observer.disconnect();
          resolve();
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });
    });
    await new Promise((resolve) => requestAnimationFrame(resolve)); // until the frame is painted
    return performance.now() - started;
  });

  await node(page, "a1").click();
  const editLatency = await page.evaluate(async () => {
    const input = document.querySelector<HTMLInputElement>("#agent-name");
    if (input === null) throw new Error("no name field");
    // React tracks the value property; set it through the prototype so the input event counts.
    const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    const started = performance.now();
    // eslint-disable-next-line @typescript-eslint/unbound-method -- called with the input as `this`
    const setValue = descriptor?.set;
    setValue?.call(input, "Renamed agent");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await new Promise<void>((resolve) => {
      const target = document.querySelector('.react-flow__node[data-id="a1"]');
      if (target === null) throw new Error("node a1 not rendered");
      const done = (): boolean => target.textContent.includes("Renamed agent");
      if (done()) {
        resolve();
        return;
      }
      const observer = new MutationObserver(() => {
        if (done()) {
          observer.disconnect();
          resolve();
        }
      });
      observer.observe(target, { childList: true, subtree: true, characterData: true });
    });
    await new Promise((resolve) => requestAnimationFrame(resolve));
    return performance.now() - started;
  });

  info.annotations.push({ type: "perf", description: `add=${addLatency.toFixed(1)}ms edit=${editLatency.toFixed(1)}ms` });
  console.log(`[perf] interaction latency at 500 agents: add=${addLatency.toFixed(1)}ms edit=${editLatency.toFixed(1)}ms`);
  expect(addLatency).toBeLessThan(INTERACTION_MAX);
  expect(editLatency).toBeLessThan(INTERACTION_MAX);
  await expectAgents(page, 501);
});
