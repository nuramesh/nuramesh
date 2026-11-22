/**
 * Runs automatic layout. In the browser ELK runs in a Web Worker (ELK's own worker script, driven
 * by the small elk-api on the main thread); where workers don't exist (unit tests in jsdom) the
 * bundled ELK runs in-process. Both give identical results.
 */
import type { ElkNode } from "elkjs/lib/elk-api";

import type { XY } from "./document";
import { fromElkResult } from "./layout";

export interface LayoutEngine {
  layout(graph: ElkNode): Promise<Record<string, XY>>;
  dispose(): void;
}

interface Elk {
  layout(graph: ElkNode): Promise<ElkNode>;
  terminateWorker?: () => void;
}

/** Interop: these CommonJS modules export the constructor directly or as `default`. */
function constructor<T>(module: { default: T }): T {
  const value = module.default as T | { default: T };
  return typeof value === "function" ? value : (value as { default: T }).default;
}

/** How long loading ELK or laying out a graph may take before we give up and report an error. */
export const LAYOUT_TIMEOUT_MS = 10_000;

function withTimeout<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`Timed out ${what} after ${String(ms)} ms`));
    }, ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error instanceof Error ? error : new Error(String(error)));
      },
    );
  });
}

/**
 * `ownsWorker`: terminate ELK's worker on dispose (the in-process engine has none).
 * A failed load or a worker that doesn't answer in time rejects (the store shows the error) and is
 * thrown away, so the next layout starts a fresh engine instead of reusing a broken one.
 */
export function createEngine(
  load: () => Promise<Elk>,
  ownsWorker: boolean,
  timeoutMs: number = LAYOUT_TIMEOUT_MS,
): LayoutEngine {
  let elk: Promise<Elk> | null = null;
  const discard = (broken: Promise<Elk>): void => {
    if (elk !== broken) return; // already replaced
    elk = null;
    if (ownsWorker) void broken.then((instance) => instance.terminateWorker?.(), () => undefined);
  };
  return {
    async layout(graph) {
      const loading = (elk ??= load());
      try {
        const instance = await withTimeout(loading, timeoutMs, "loading the layout engine");
        return fromElkResult(await withTimeout(instance.layout(graph), timeoutMs, "laying out the agents"));
      } catch (error) {
        discard(loading);
        throw error;
      }
    },
    dispose() {
      if (ownsWorker) void elk?.then((instance) => instance.terminateWorker?.(), () => undefined);
      elk = null;
    },
  };
}

/** ELK on the calling thread (unit tests, or environments without workers). */
export function createInlineEngine(): LayoutEngine {
  return createEngine(async () => {
    const ELK = constructor(await import("elkjs/lib/elk.bundled.js"));
    return new ELK();
  }, false);
}

/** ELK in a dedicated Web Worker; the main thread only posts the graph and receives positions. */
export function createWorkerEngine(): LayoutEngine {
  return createEngine(async () => {
    const [api, worker] = await Promise.all([
      import("elkjs/lib/elk-api.js"),
      import("elkjs/lib/elk-worker.min.js?url"),
    ]);
    const ELK = constructor(api);
    return new ELK({ workerUrl: worker.default });
  }, true);
}

export function createLayoutEngine(): LayoutEngine {
  return typeof Worker === "undefined" ? createInlineEngine() : createWorkerEngine();
}
