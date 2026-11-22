import { describe, expect, it } from "vitest";

import indexHtml from "../../index.html?raw";
import { CSP_POLICY, cspMeta, cspPolicy, injectCspMeta } from "../../csp";

const directives = (policy: string): Map<string, string[]> =>
  new Map(
    policy
      .split(";")
      .map((d) => d.trim().split(/\s+/))
      .filter((t) => t[0])
      .map((t): [string, string[]] => [t.shift() ?? "", t]),
  );

describe("review-3 m5: the SPA host policy", () => {
  const d = directives(CSP_POLICY);

  it("allows scripts only from this origin: no inline, no eval, no remote host", () => {
    expect(d.get("script-src")).toEqual(["'self'"]);
    expect(d.get("object-src")).toEqual(["'none'"]);
    expect(d.get("base-uri")).toEqual(["'self'"]);
    expect(d.get("frame-ancestors")).toEqual(["'none'"]);
    expect(CSP_POLICY).not.toMatch(/unsafe-eval|\s\*(\s|;|$)/);
    for (const m of CSP_POLICY.matchAll(/\b(?:https?|wss?):\/\/[^\s:;]+/g)) expect(m[0]).toMatch(/\/\/(127\.0\.0\.1|localhost)$/);
  });

  it("pins every other directive: default-deny, same-origin fetches, form-action", () => {
    expect(d.get("default-src")).toEqual(["'none'"]);
    expect(d.get("style-src")).toEqual(["'self'", "'unsafe-inline'"]);
    expect(d.get("img-src")).toEqual(["'self'", "data:", "blob:"]);
    expect(d.get("font-src")).toEqual(["'self'", "data:"]);
    expect(d.get("worker-src")).toEqual(["'self'", "blob:"]);
    expect(d.get("manifest-src")).toEqual(["'self'"]);
    expect(d.get("form-action")).toEqual(["'self'"]);
  });

  it("connects to itself (and the loopback server address), nowhere else", () => {
    expect(d.get("connect-src")).toContain("'self'");
    for (const t of d.get("connect-src") ?? []) expect(t).toMatch(/^('self'|(https?|wss?):\/\/(127\.0\.0\.1|localhost):\*)$/);
  });

  it("is well formed: no glued tokens, every directive known", () => {
    expect(CSP_POLICY).not.toMatch(/'[a-z-]+'['a-z]/);
    for (const name of d.keys())
      expect(["default-src", "script-src", "style-src", "img-src", "font-src", "connect-src", "worker-src", "manifest-src", "object-src", "base-uri", "form-action", "frame-ancestors"]).toContain(name);
  });

  it("the page has no inline script, so script-src 'self' is enough", () => {
    const scripts = [...injectCspMeta(indexHtml).matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
    expect(scripts.length).toBeGreaterThan(0);
    for (const m of scripts) {
      expect(m[1]).toMatch(/\bsrc="/);
      expect((m[2] ?? "").trim()).toBe("");
    }
  });

  it("the built page carries one policy meta (without frame-ancestors, which a meta ignores)", () => {
    const html = injectCspMeta(indexHtml);
    expect([...html.matchAll(/http-equiv="Content-Security-Policy"/g)]).toHaveLength(1);
    expect(cspMeta()).not.toContain("frame-ancestors");
    expect(cspPolicy()).toContain("frame-ancestors 'none'");
    expect(html.indexOf("Content-Security-Policy")).toBeLessThan(html.indexOf("<script"));
  });
});
