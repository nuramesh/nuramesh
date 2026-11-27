/**
 * Fill `{name}` path parameters. One linear pass (no regex backtracking, CodeQL
 * js/polynomial-redos): same matches as /\{([^}]+)\}/g, i.e. a `{`, one or more non-`}`
 * characters (a `{` counts), then the next `}`.
 */
export function fillPath(path: string, resolve: (name: string) => string): string {
  let out = "";
  let last = 0;
  let i = path.indexOf("{");
  while (i !== -1) {
    const close = path.indexOf("}", i + 1);
    if (close === -1) break; // no `}` left: no later `{` can match either
    if (close === i + 1) {
      i = path.indexOf("{", i + 1); // `{}`: empty name, not a match
      continue;
    }
    out += path.slice(last, i) + resolve(path.slice(i + 1, close));
    last = close + 1;
    i = path.indexOf("{", last);
  }
  return out + path.slice(last);
}
