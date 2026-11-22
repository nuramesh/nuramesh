import { useEffect, useState } from "react";

/**
 * Whole seconds left of a wait that started at `from` (a `Date.now()` value) and lasts `seconds`;
 * 0 when there is none. It ticks once a second and reads the clock each tick, so a throttled
 * background tab catches up.
 */
export function useCountdown(wait: { from: number; seconds: number } | null): number {
  const [now, setNow] = useState(0);
  useEffect(() => {
    if (wait === null) return;
    const timer = globalThis.setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current - wait.from >= wait.seconds * 1000) globalThis.clearInterval(timer);
    }, 1000);
    return () => {
      globalThis.clearInterval(timer);
    };
  }, [wait]);
  if (wait === null) return 0;
  const elapsed = Math.max(0, now - wait.from);
  return Math.max(0, Math.ceil(wait.seconds - elapsed / 1000));
}
