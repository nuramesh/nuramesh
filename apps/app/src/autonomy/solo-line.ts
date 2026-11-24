/**
 * What a person is shown of a proposal's solo share (the solo approval notes SA-18 item 2):
 * `approved asks: N (solo M, P %)` and, when a solo one exists, that nobody else reviewed them.
 * `share` is the server's 4-place decimal text (`"0.3333"`); with no approved ask there is none.
 * The percentage is computed on the text, never on a float, and trailing zeros are dropped.
 */
export function soloLines(approved: number, solo: number, share: string | null): string[] {
  if (share === null) return [`approved asks: ${String(approved)} (solo ${String(solo)})`];
  const lines = [`approved asks: ${String(approved)} (solo ${String(solo)}, ${percent(share)} %)`];
  if (solo > 0) lines.push("no second person reviewed the solo ones");
  return lines;
}

/** `"0.3333"` -> `33.33`, `"0.1250"` -> `12.5`, `"1.0000"` -> `100`, `"0.0000"` -> `0`. */
function percent(share: string): string {
  const match = /^(\d+)\.(\d{1,4})$/.exec(share);
  const [, units, places] = match ?? [];
  if (units === undefined || places === undefined) return share;
  const hundredths = Number(BigInt(`${units}${places.padEnd(4, "0")}`));
  const whole = Math.floor(hundredths / 100);
  const fraction = String(hundredths % 100).padStart(2, "0").replace(/0+$/, "");
  return fraction === "" ? String(whole) : `${String(whole)}.${fraction}`;
}
