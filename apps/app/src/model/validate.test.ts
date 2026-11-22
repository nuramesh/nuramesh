import { describe, expect, it, vi } from "vitest"; // vi: spies that count Map/Set operations

import { makeAgent, makeRole, parseOrg } from "./org";
import { isValid, validateOrg, type OrgIssue } from "./validate";

interface Fixture {
  description: string;
  org: unknown;
  expected_issues: { code: string; severity: string; subject: string | null }[];
}

// Every shared fixture (spec/org-validation at the repository root), the same files pytest runs.
const fixtures = import.meta.glob<Fixture>("../../../../spec/org-validation/*.json", {
  eager: true,
  import: "default",
});

/** Issues as a sorted list of "code|severity|subject" keys: a multiset comparison. */
function keys(issues: readonly { code: string; severity: string; subject: string | null }[]) {
  return issues.map((i) => `${i.code}|${i.severity}|${String(i.subject)}`).sort();
}

describe("shared org-validation fixtures", () => {
  const entries = Object.entries(fixtures);

  it("finds the fixture directory", () => {
    expect(entries.length).toBeGreaterThanOrEqual(29);
    expect(entries.map(([path]) => path.split("/").at(-1))).toContain("valid-minimal.json");
  });

  it.each(entries)("%s", (_path, fixture) => {
    // validate_org itself accepts empty lists (OrgGraph requires members; see org.test.ts).
    const parsed = parseOrg(fixture.org, { allowEmpty: true });
    if (!parsed.ok) throw new Error(`fixture didn't parse: ${JSON.stringify(parsed.errors)}`);
    const issues = validateOrg(parsed.value.roles, parsed.value.agents);
    expect(keys(issues)).toEqual(keys(fixture.expected_issues));
  });
});

describe("validateOrg", () => {
  const cos = makeRole({ id: "cos", name: "Chief of Staff", kind: "chief_of_staff" });
  const dev = makeRole({ id: "dev", name: "Developer" });

  it("puts errors before warnings and keeps order otherwise", () => {
    const issues = validateOrg(
      [cos, dev, makeRole({ id: "spare", name: "Spare" })],
      [makeAgent({ id: "a", name: "A", role: "dev" })],
    );
    expect(issues.map((i) => i.severity)).toEqual(["error", "warning", "warning"]);
    expect(issues.map((i) => i.code)).toEqual([
      "root_not_chief_of_staff",
      "unused_role",
      "unused_role",
    ]);
  });

  it("writes a readable message naming the loop", () => {
    const issues = validateOrg(
      [cos, dev],
      [
        makeAgent({ id: "root", name: "Root", role: "cos" }),
        makeAgent({ id: "a", name: "A", role: "dev", reports_to: "b" }),
        makeAgent({ id: "b", name: "B", role: "dev", reports_to: "a" }),
      ],
    );
    const cycle = issues.find((i: OrgIssue) => i.code === "cycle");
    expect(cycle?.message).toBe("Reporting lines form a loop: a → b → a");
    expect(isValid(issues)).toBe(false);
  });

  it("treats warnings as valid", () => {
    const issues = validateOrg([cos, dev], [makeAgent({ id: "root", name: "Root", role: "cos" })]);
    expect(issues).toHaveLength(1);
    expect(isValid(issues)).toBe(true);
  });

  // Work count, not wall clock: every Map/Set operation validateOrg performs is counted, so the
  // result does not depend on machine load. Linear work gives a 4n/n ratio of about 4, a
  // quadratic walk about 16. Bound: 3.5 to 4.5 (the counts are exact, only fixed overhead
  // make it differ slightly from 4).
  const countOps = (run: () => void): number => {
    const spies = [
      vi.spyOn(Map.prototype, "get"),
      vi.spyOn(Map.prototype, "has"),
      vi.spyOn(Map.prototype, "set"),
      vi.spyOn(Set.prototype, "has"),
      vi.spyOn(Set.prototype, "add"),
    ];
    try {
      run();
      return spies.reduce((sum, spy) => sum + spy.mock.calls.length, 0);
    } finally {
      for (const spy of spies) spy.mockRestore();
    }
  };
  const expectLinear = (opsFor: (n: number) => number): void => {
    const small = 5_000;
    const ratio = opsFor(4 * small) / opsFor(small);
    expect(ratio).toBeGreaterThan(3.5);
    expect(ratio).toBeLessThan(4.5);
  };
  const chainOf = (n: number) =>
    Array.from({ length: n }, (_, i) =>
      makeAgent({
        id: `a${String(i)}`,
        name: `A${String(i)}`,
        role: i === 0 ? "cos" : "dev",
        reports_to: i === 0 ? null : `a${String(i - 1)}`,
      }),
    );

  it("does linear work on a long chain and one big loop (walk steps counted)", () => {
    expectLinear((n) => {
      // Reverse order makes every walk as long as possible for a quadratic implementation.
      const chain = chainOf(n).reverse();
      // Everyone in one big loop: a(k) reports to a(k+1), a(n-1) to a(0).
      const loop = chain.map((a) => ({
        ...a,
        reports_to: `a${String((Number(a.id.slice(1)) + 1) % n)}`,
      }));
      let cycles = 0;
      const ops = countOps(() => {
        expect(validateOrg([cos, dev], chain)).toEqual([]);
        cycles = validateOrg([cos, dev], loop).filter((i) => i.code === "cycle").length;
      });
      expect(cycles).toBe(n);
      return ops;
    });
  });

  it("does linear work on the verification line (addCheckIssues, shallow tree)", () => {
    const verifier = makeRole({ id: "ver", name: "Verifier", kind: "verifier" });
    expectLinear((n) => {
      // A root with n verifiers under it; each checks the next one (a peer, so no issue).
      const agents = [
        makeAgent({ id: "root", name: "Root", role: "cos" }),
        ...Array.from({ length: n }, (_, i) =>
          makeAgent({
            id: `v${String(i)}`,
            name: `V${String(i)}`,
            role: "ver",
            reports_to: "root",
            checks: [`v${String((i + 1) % n)}`],
          }),
        ),
      ];
      let issues: OrgIssue[] = [];
      const ops = countOps(() => {
        issues = validateOrg([cos, dev, verifier], agents);
      });
      expect(issues.filter((i) => i.severity === "error")).toEqual([]);
      return ops;
    });
  });
});
