import type { ElkNode } from "elkjs/lib/elk-api";
import { describe, expect, it, vi } from "vitest";

import { createEngine } from "./layout-engine";

const graph: ElkNode = { id: "root", children: [{ id: "a", width: 10, height: 10 }] };
const placed: ElkNode = { id: "root", children: [{ id: "a", x: 5, y: 7, width: 10, height: 10 }] };

describe("layout engine robustness", () => {
  it("lays out when the engine answers", async () => {
    const engine = createEngine(() => Promise.resolve({ layout: () => Promise.resolve(placed) }), false, 100);
    expect(await engine.layout(graph)).toEqual({ a: { x: 5, y: 7 } });
  });

  it("times out a worker that never answers, discards it and recovers on the next layout", async () => {
    const terminated = vi.fn();
    let created = 0;
    const engine = createEngine(
      () => {
        created += 1;
        const hung = created === 1;
        return Promise.resolve({
          layout: () => (hung ? new Promise<ElkNode>(() => undefined) : Promise.resolve(placed)),
          terminateWorker: terminated,
        });
      },
      true,
      30,
    );
    await expect(engine.layout(graph)).rejects.toThrow(/Timed out laying out/);
    await vi.waitFor(() => {
      expect(terminated).toHaveBeenCalledTimes(1); // the hung worker is terminated
    });
    expect(await engine.layout(graph)).toEqual({ a: { x: 5, y: 7 } }); // a fresh engine works
    expect(created).toBe(2);
  });

  it("does not cache a failed load: the next layout tries again", async () => {
    let attempts = 0;
    const engine = createEngine(
      () => {
        attempts += 1;
        return attempts === 1
          ? Promise.reject(new Error("chunk failed to load"))
          : Promise.resolve({ layout: () => Promise.resolve(placed) });
      },
      false,
      100,
    );
    await expect(engine.layout(graph)).rejects.toThrow(/chunk failed/);
    expect(await engine.layout(graph)).toEqual({ a: { x: 5, y: 7 } });
    expect(attempts).toBe(2);
  });

  it("times out a load that never finishes", async () => {
    const engine = createEngine(() => new Promise(() => undefined), false, 30);
    await expect(engine.layout(graph)).rejects.toThrow(/Timed out loading/);
  });
});
