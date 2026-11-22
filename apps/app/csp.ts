// Review-3 m5: the Content-Security-Policy of the app's host page. Shared by vite.config.ts (the
// built index.html gets it as a <meta>, `vite preview` sends it as a header) and src/lib/csp.test.ts.
//
// Why each directive: the page has one external module script and the external theme-init.js, so
// script-src needs no inline source and no hash (never 'unsafe-inline' or 'unsafe-eval'). Styles are
// one stylesheet plus style attributes React sets through the CSSOM (not blocked) and the <style>
// element Radix primitives inject, so style-src keeps 'unsafe-inline' (styles only, no script
// risk). connect-src is this origin (REST, and the WebSocket on the same origin) plus the loopback
// server address the Settings page accepts (`nuramesh serve` talks only to loopback). ELK runs in a
// same-origin worker (blob: for the bundled fallback). A <meta> cannot carry frame-ancestors, so
// that one directive goes in the preview header only; the production host sets it with its headers.
const LOOPBACK = ["http://127.0.0.1:*", "http://localhost:*", "ws://127.0.0.1:*", "ws://localhost:*"];

const DIRECTIVES: Record<string, string[]> = {
  "default-src": ["'none'"],
  "script-src": ["'self'"],
  "style-src": ["'self'", "'unsafe-inline'"],
  "img-src": ["'self'", "data:", "blob:"],
  "font-src": ["'self'", "data:"],
  "connect-src": ["'self'", ...LOOPBACK],
  "worker-src": ["'self'", "blob:"],
  "manifest-src": ["'self'"],
  "object-src": ["'none'"],
  "base-uri": ["'self'"],
  "form-action": ["'self'"],
  "frame-ancestors": ["'none'"],
};

const render = (skip: string[]): string =>
  Object.entries(DIRECTIVES)
    .filter(([name]) => !skip.includes(name))
    .map(([name, tokens]) => `${name} ${tokens.join(" ")}`)
    .join("; ");

/** The full policy (for a response header). */
export const CSP_POLICY = render([]);
export const cspPolicy = (): string => CSP_POLICY;
/** The policy for a <meta> tag: browsers ignore frame-ancestors there. */
export const cspMeta = (): string => render(["frame-ancestors"]);

/** Puts the policy meta first in <head>, before any script or stylesheet. */
export function injectCspMeta(html: string): string {
  if (/http-equiv="Content-Security-Policy"/i.test(html)) return html;
  const tag = `<meta http-equiv="Content-Security-Policy" content="${cspMeta()}" />`;
  return html.replace(/<head>/, `<head>\n    ${tag}`);
}
