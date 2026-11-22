/* eslint-disable @typescript-eslint/no-non-null-assertion -- test file: fixed arrays and elements that the line above has just checked exist */
import { render } from "@testing-library/react";
import jsQR from "jsqr";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import { QrCode } from "./qr-code";

/** Rasterises the rendered SVG rects (quiet zone included) to pixels, as a camera would see them. */
function rasterise(container: HTMLElement, scale = 4): { data: Uint8ClampedArray; width: number; height: number } {
  const svg = container.querySelector("svg")!;
  const side = Number(svg.getAttribute("viewBox")?.split(" ")[2]);
  const width = side * scale;
  const data = new Uint8ClampedArray(width * width * 4).fill(255);
  for (const rect of svg.querySelectorAll("rect")) {
    if (rect.getAttribute("fill") !== "#000000") continue;
    const [x, y, w, h] = ["x", "y", "width", "height"].map((a) => Number(rect.getAttribute(a)));
    for (let py = (y!) * scale; py < ((y!) + (h!)) * scale; py += 1) {
      for (let px = (x!) * scale; px < ((x!) + (w!)) * scale; px += 1) {
        const at = (py * width + px) * 4;
        data[at] = 0;
        data[at + 1] = 0;
        data[at + 2] = 0;
      }
    }
  }
  return { data, width, height: width };
}

const uri = (length: number): string => {
  const base = "otpauth://totp/";
  return base + "a".repeat(Math.max(0, length - base.length));
};
const TYPICAL = "otpauth://totp/NuraMesh:ada%40example.org?secret=" + "A".repeat(32) + "&issuer=NuraMesh";
const LONGEST = `otpauth://totp/NuraMesh:${"b".repeat(254)}%40example.org?secret=${"A".repeat(32)}&issuer=NuraMesh`;

describe("M35: the drawn QR decodes to the address (not read from the library's matrix)", () => {
  it.each([
    ["a 20-character address", uri(20)],
    ["a typical address", TYPICAL],
    ["a 330-character address", LONGEST.slice(0, 330)],
  ])("%s", (_name, text) => {
    const { container } = render(<QrCode text={text} label="QR" />);
    const image = rasterise(container);
    const decoded = jsQR(image.data, image.width, image.height);
    expect(decoded?.data).toBe(text);
  });
});

describe("M11: drawing makes no request and builds no image URL", () => {
  it("fetch, XMLHttpRequest, WebSocket, Image and sendBeacon are not called; no img element; no innerHTML", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("no network"));
    const xhr = vi.spyOn(XMLHttpRequest.prototype, "open");
    const ws = vi.fn();
    const image = vi.fn();
    vi.stubGlobal("WebSocket", ws);
    vi.stubGlobal("Image", image);
    const beacon = vi.fn();
    Object.defineProperty(globalThis.navigator, "sendBeacon", { value: beacon, configurable: true });
    const { container } = render(<QrCode text={TYPICAL} label="QR" />);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(xhr).not.toHaveBeenCalled();
    expect(ws).not.toHaveBeenCalled();
    expect(image).not.toHaveBeenCalled();
    expect(beacon).not.toHaveBeenCalled();
    expect(container.querySelector("img")).toBeNull();
    vi.unstubAllGlobals();
  });

  it("the new source files use no dangerouslySetInnerHTML, innerHTML or network API", () => {
    for (const file of ["qr-code.tsx", "qr-runs.ts", "recovery-codes.tsx", "recovery-file.ts", "setup-body.tsx", "security-panel.tsx", "password-code-form.tsx", "codes-unsaved-notice.tsx", "notice.ts", "messages.ts"]) {
      const text = readFileSync(join(process.cwd(), "src/mfa", file), "utf8").replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
      expect(text, file).not.toMatch(/dangerouslySetInnerHTML|innerHTML|XMLHttpRequest|WebSocket|sendBeacon|\bfetch\(|new Image|<img/);
    }
  });
});
