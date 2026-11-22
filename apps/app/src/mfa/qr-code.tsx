import { QUIET, qrRuns } from "./qr-runs";

/**
 * Draws the code locally as an inline SVG made of React elements: no image URL, no request, no `innerHTML`
 * (spec/mfa-web W-5, A-T1). It is a drawing of a secret: the parent drops it on unmount like the secret itself.
 */
export function QrCode({ text, label }: { text: string; label: string }) {
  const { size, runs } = qrRuns(text);
  const side = size + QUIET * 2;
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${String(side)} ${String(side)}`}
      shapeRendering="crispEdges"
      className="size-56 max-w-full rounded-md border bg-white"
      data-testid="mfa-qr"
    >
      <rect x={0} y={0} width={side} height={side} fill="#ffffff" />
      {runs.map(([row, start, length]) => (
        <rect key={`${String(row)}-${String(start)}`} x={start + QUIET} y={row + QUIET} width={length} height={1} fill="#000000" />
      ))}
    </svg>
  );
}
