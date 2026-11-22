import { describe, expect, it } from "vitest";
import { BUILT_IN_TEMPLATES, extraRoles, PREDEFINED_ROLES } from "../templates";

describe("palette extraRoles", () => {
  it("offers no second Chief of Staff for the dev-team template", () => {
    const dev = BUILT_IN_TEMPLATES.find((t) => t.id === "dev-team");
    if (dev === undefined) throw new Error("no dev-team");
    const names = extraRoles(dev.document.org.roles).map((r) => r.name);
    expect(names).not.toContain("Chief of Staff");
    expect(names).not.toContain("DevOps engineer");
  });
  it("offers every predefined role to an empty organization", () => {
    expect(extraRoles([])).toHaveLength(PREDEFINED_ROLES.length);
  });
  it("hides a predefined role the org already has by id", () => {
    const first = PREDEFINED_ROLES[0];
    if (first === undefined) throw new Error("none");
    expect(extraRoles([first]).map((r) => r.id)).not.toContain(first.id);
  });
  it("hides a predefined role the org has under another id and the same name in another case", () => {
    const dev = PREDEFINED_ROLES.find((r) => r.name === "Developer");
    if (dev === undefined) throw new Error("no Developer");
    const custom = { ...dev, id: "x", name: "developer" };
    expect(extraRoles([custom]).map((r) => r.name)).not.toContain("Developer");
  });
});
