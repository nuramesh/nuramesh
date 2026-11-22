import { act, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { storageScopeOf } from "@/auth/storage-scope";
import type { AuthApi, AuthState, AuthUser } from "@/auth/types";
import { renderAt, serverLike } from "@/test/server-like";

import { DRAFT_KEY, loadDraft, loadUserTemplates, purgeLegacyUnscopedStorage, saveDraft, saveUserTemplates, TEMPLATES_KEY } from "./storage";
import { createBuilderStore } from "./store";
import { sampleDoc } from "./test-data";

const ADA: AuthUser = { id: "human:u_ada", orgId: "acme", role: "owner", email: "ada@example.org", displayName: "Ada Lovelace" };
const GRACE: AuthUser = { id: "human:u_grace", orgId: "acme", role: "viewer", email: "grace@example.org", displayName: "Grace Hopper" };
const ADA_SCOPE = storageScopeOf({ kind: "signed-in", user: ADA });
const GRACE_SCOPE = storageScopeOf({ kind: "signed-in", user: GRACE });

afterEach(() => {
  window.localStorage.clear();
});

function authFor(initial: AuthState) {
  let state = initial;
  const listeners = new Set<() => void>();
  const auth: AuthApi = {
    state: () => state,
    subscribe: (l) => {
      listeners.add(l);
      return () => {
        listeners.delete(l);
      };
    },
    load: () => Promise.resolve(),
    login: () => Promise.resolve({ kind: "signed-in" as const }),
    completeMfa: () => Promise.resolve(),
    logout: () => Promise.resolve(),
    setupStatus: () => Promise.resolve(false),
    redeemSetup: () => Promise.resolve(),
  };
  return {
    auth,
    set: (next: AuthState) => {
      state = next;
      for (const l of [...listeners]) l();
    },
  };
}


describe("storage scope", () => {
  it("is the organization and the user, and nothing for anyone not signed in", () => {
    expect(ADA_SCOPE).toBe("acme:human:u_ada");
    expect(ADA_SCOPE).not.toBe(GRACE_SCOPE);
    expect(storageScopeOf({ kind: "signed-in", user: { ...ADA, orgId: "other" } })).not.toBe(ADA_SCOPE); // same user id, another org
    expect(storageScopeOf({ kind: "signed-out" })).toBeUndefined();
    expect(storageScopeOf({ kind: "open" })).toBeUndefined();
  });

  it("one user's draft and templates are never loaded for another user on the same storage", () => {
    saveDraft(sampleDoc(), ADA_SCOPE);
    saveUserTemplates([{ id: "t1", name: "Ada's team", description: "", document: sampleDoc() }], ADA_SCOPE);
    expect(loadDraft(GRACE_SCOPE)).toBeNull();
    expect(loadUserTemplates(GRACE_SCOPE)).toEqual([]);
    expect(loadDraft(ADA_SCOPE)).toEqual(sampleDoc());
    expect(loadUserTemplates(ADA_SCOPE).map((t) => t.id)).toEqual(["t1"]);
  });

  it("a signed-in user never loads what an unsigned session saved, and the reverse", () => {
    saveDraft(sampleDoc()); // sample-data mode, no sign-in
    expect(loadDraft(ADA_SCOPE)).toBeNull();
    saveDraft(sampleDoc(), ADA_SCOPE);
    window.localStorage.removeItem("nuramesh-draft");
    expect(loadDraft()).toBeNull();
  });

  it("the store saves templates under its user's scope only", () => {
    const store = createBuilderStore(sampleDoc(), ADA_SCOPE);
    expect(store.getState().saveTemplate("Mine", "")).toBe(true);
    expect(loadUserTemplates(ADA_SCOPE).map((t) => t.name)).toEqual(["Mine"]);
    expect(loadUserTemplates(GRACE_SCOPE)).toEqual([]);
    expect(createBuilderStore(sampleDoc(), GRACE_SCOPE).getState().userTemplates).toEqual([]);
    expect(createBuilderStore(sampleDoc(), ADA_SCOPE).getState().userTemplates.map((t) => t.name)).toEqual(["Mine"]);
  });

  it("signing out deletes nothing: the same user finds the draft again", () => {
    saveDraft(sampleDoc(), ADA_SCOPE);
    // a different person uses the browser, then Ada returns
    saveDraft(sampleDoc(), GRACE_SCOPE);
    expect(loadDraft(ADA_SCOPE)).toEqual(sampleDoc());
  });
});

describe("the templates page follows the signed-in user", () => {
  it("shows Ada's saved template to Ada and not to Grace, in the same tab or after a switch", async () => {
    saveUserTemplates([{ id: "t1", name: "Ada private team", description: "", document: sampleDoc() }], ADA_SCOPE);
    const { auth, set } = authFor({ kind: "signed-in", user: ADA });
    renderAt("/templates", serverLike({ auth, templates: { list: () => Promise.resolve([]) } }));
    expect(await screen.findByText("Ada private team")).toBeInTheDocument();
    act(() => {
      set({ kind: "checking" }); // what the app does on a signal from another tab
    });
    act(() => {
      set({ kind: "signed-in", user: GRACE }); // ... which signed in as Grace
    });
    expect(await screen.findByText("Grace Hopper")).toBeInTheDocument();
    expect(screen.queryByText("Ada private team")).not.toBeInTheDocument();
    act(() => {
      set({ kind: "checking" });
    });
    act(() => {
      set({ kind: "signed-in", user: ADA });
    });
    expect(await screen.findByText("Ada private team")).toBeInTheDocument();
  });
});

describe("the builder follows the signed-in user without a sign-out in between", () => {
  it("never shows Ada's draft to Grace when the user changes while the page stays mounted", async () => {
    saveDraft(sampleDoc(), ADA_SCOPE); // four agents
    const { auth, set } = authFor({ kind: "signed-in", user: ADA });
    renderAt("/builder", serverLike({ auth }));
    expect(await screen.findByTestId("builder-summary")).toHaveTextContent("4 agents");
    act(() => {
      set({ kind: "signed-in", user: GRACE }); // straight to u2: no "checking", no sign-out
    });
    await waitFor(() => {
      expect(screen.getByTestId("builder-summary")).toHaveTextContent("0 agents");
    });
    expect(loadDraft(ADA_SCOPE)).toEqual(sampleDoc()); // and Ada's draft was not overwritten
    expect(loadDraft(GRACE_SCOPE)).not.toEqual(sampleDoc());
  });
});

describe("legacy unscoped drafts and templates", () => {
  const LEGACY = "nuramesh-legacy-unscoped-purged";

  it("are deleted once, scoped data stays, and the cleanup does not run again", () => {
    saveDraft(sampleDoc());
    saveUserTemplates([{ id: "t0", name: "Old", description: "", document: sampleDoc() }]);
    saveDraft(sampleDoc(), ADA_SCOPE);
    expect(purgeLegacyUnscopedStorage()).toBe(true);
    expect(window.localStorage.getItem(DRAFT_KEY)).toBeNull();
    expect(window.localStorage.getItem(TEMPLATES_KEY)).toBeNull();
    expect(loadDraft(ADA_SCOPE)).toEqual(sampleDoc());
    expect(window.localStorage.getItem(LEGACY)).not.toBeNull();
    saveDraft(sampleDoc()); // written after the cleanup: not touched again
    expect(purgeLegacyUnscopedStorage()).toBe(false);
    expect(loadDraft()).toEqual(sampleDoc());
  });

  it("are deleted on first load against a server with accounts, but not in sample-data mode", async () => {
    saveDraft(sampleDoc());
    saveUserTemplates([{ id: "t0", name: "Old", description: "", document: sampleDoc() }]);
    renderAt("/templates", serverLike({ templates: { list: () => Promise.resolve([]) } })); // no auth: sample data
    await screen.findByRole("heading", { level: 1 });
    expect(loadDraft()).toEqual(sampleDoc()); // sample-data mode keeps its unscoped keys
    expect(loadUserTemplates().map((t) => t.id)).toEqual(["t0"]);
    expect(window.localStorage.getItem(LEGACY)).toBeNull();
  });

  it("are deleted once a signed-in session loads", async () => {
    saveDraft(sampleDoc());
    saveUserTemplates([{ id: "t0", name: "Old", description: "", document: sampleDoc() }]);
    const { auth } = authFor({ kind: "signed-in", user: ADA });
    renderAt("/templates", serverLike({ auth, templates: { list: () => Promise.resolve([]) } }));
    await screen.findByText("Ada Lovelace");
    await waitFor(() => {
      expect(window.localStorage.getItem(DRAFT_KEY)).toBeNull();
    });
    expect(window.localStorage.getItem(TEMPLATES_KEY)).toBeNull();
  });
});
