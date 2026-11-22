import qrcode from "qrcode-generator";

/** Quiet zone in modules (the QR standard asks for 4). */
export const QUIET = 4;

/** The dark modules of the code as `[row, start, length]` runs. Error correction M, the smallest version that fits. */
export function qrRuns(text: string): { size: number; runs: [number, number, number][] } {
  const code = qrcode(0, "M");
  code.addData(text, "Byte");
  code.make();
  const count = code.getModuleCount();
  const runs: [number, number, number][] = [];
  for (let row = 0; row < count; row += 1) {
    let start = -1;
    for (let col = 0; col <= count; col += 1) {
      const dark = col < count && code.isDark(row, col);
      if (dark && start < 0) start = col;
      if (!dark && start >= 0) {
        runs.push([row, start, col - start]);
        start = -1;
      }
    }
  }
  return { size: count, runs };
}
