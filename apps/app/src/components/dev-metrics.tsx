import { useEffect, useState } from "react";

interface Metrics {
  inner: number;
  client: number;
  dpr: number;
  main: string;
}

function read(): Metrics {
  const main = document.getElementById("main");
  return {
    inner: window.innerWidth,
    client: document.documentElement.clientWidth,
    dpr: window.devicePixelRatio,
    main: main === null ? "-" : `${String(main.clientWidth)}${main.scrollWidth > main.clientWidth ? `+${String(main.scrollWidth - main.clientWidth)}` : ""}`,
  };
}

/**
 * Development only (rendered under `import.meta.env.DEV`, so it is not in a production build): the
 * real window numbers in the bottom-left corner, so a layout report can quote them. `inner` is the
 * window width in CSS px (it includes a classic scrollbar), `client` the layout width without it,
 * `dpr` the pixel ratio (browser zoom shows up in both), `main` the page area's width, with `+n`
 * when its content is n px wider than it. It never takes a click.
 */
export function DevMetrics() {
  const [metrics, setMetrics] = useState(read);
  useEffect(() => {
    const update = (): void => {
      setMetrics(read());
    };
    window.addEventListener("resize", update);
    const observer = new ResizeObserver(update);
    observer.observe(document.documentElement);
    return () => {
      window.removeEventListener("resize", update);
      observer.disconnect();
    };
  }, []);
  return (
    <div
      aria-hidden="true"
      data-testid="dev-metrics"
      className="pointer-events-none fixed bottom-1 left-1 z-[100] rounded bg-black/75 px-1.5 py-0.5 font-mono text-[10px] leading-tight text-white"
    >
      inner {metrics.inner} · client {metrics.client} · dpr {metrics.dpr} · main {metrics.main}
    </div>
  );
}
