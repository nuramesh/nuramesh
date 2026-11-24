import { useState } from "react";
import { Navigate, NavLink, useParams } from "react-router";

import { Chip } from "@/components/ui/chip";
import { cn } from "@/lib/utils";

import { AssignmentsTab } from "./assignments-tab";
import { CatalogTab } from "./catalog-tab";
import { ConnectionsTab } from "./connections-tab";
import { KeysTab } from "./keys-tab";
import { Notice } from "./parts";
import { ResolutionTab } from "./resolution-tab";
import { useSession } from "./use-models";

const TABS = [
  { id: "catalog", label: "Catalog" },
  { id: "assignments", label: "Assignments" },
  { id: "connections", label: "Connections" },
  { id: "keys", label: "Keys" },
  { id: "resolution", label: "Resolution" },
] as const;

/** Who is looking, in one line: an admin can act, everyone else reads. */
function Who({ admin, name, signedIn, role }: { admin: boolean; name: string | null; signedIn: boolean; role: string | null }) {
  if (admin) {
    return (
      <p className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
        <Chip tone="info">admin</Chip> Signed in as {name}. Every change needs a reason and is recorded in the ledger.
      </p>
    );
  }
  if (!signedIn) {
    return (
      <p role="note" className="callout-warning px-4 py-3 text-sm">
        You are not signed in, so this page is read-only: the server refuses changes without a signed-in admin. Sign in as the owner to change models (a server started with <code>--auth off</code> has no sign-in).
      </p>
    );
  }
  return (
    <p role="note" className="callout-warning px-4 py-3 text-sm">
      Read only: only an admin can change models. You are signed in as {name} ({role}). The catalog and assignments are visible; connections and the resolution report are for admins.
    </p>
  );
}

/**
 * The model governance screens (L5, the model governance notes 10.4): the approved catalog, who uses
 * which model, the connections that models are reached through, and what a run would resolve.
 * Every write is an admin's, needs a reason, and is recorded in the ledger; anyone else sees the
 * same tables with no buttons.
 */
export function ModelsPage() {
  const { tab } = useParams();
  const session = useSession();
  const [notice, setNotice] = useState<string[] | null>(null);
  const current = TABS.find((t) => t.id === tab);
  if (current === undefined) return <Navigate to="/models/catalog" replace />;
  const user = session.data ?? null;
  const props = { admin: session.admin, notify: setNotice };

  return (
    <section aria-labelledby="page-title" className="flex max-w-5xl flex-col gap-5 p-4 sm:p-6">
      <title>Models · NuraMesh</title>
      <header className="flex flex-col gap-1.5">
        <h1 id="page-title" className="text-2xl font-semibold">
          Models
        </h1>
        <p className="text-sm text-muted-foreground">
          A real model runs only if it is in the approved catalog, may receive the run&apos;s data, has an active connection and a price, and (for a verifier) comes from another family than the worker.
        </p>
      </header>
      {!session.isPending && <Who admin={session.admin} name={user?.name ?? null} signedIn={user !== null} role={user?.role ?? null} />}
      <nav aria-label="Models sections" className="flex flex-wrap gap-1.5">
        {TABS.map((t) => (
          <NavLink
            key={t.id}
            to={`/models/${t.id}`}
            onClick={() => {
              setNotice(null);
            }}
            className={({ isActive }) =>
              cn(
                "flex min-h-11 items-center rounded-full border px-4 text-sm no-underline",
                isActive ? "border-pill-on-border bg-pill-on text-pill-on-text" : "border-pill-border bg-pill text-pill-text hover:bg-panel-3",
              )
            }
          >
            {t.label}
          </NavLink>
        ))}
      </nav>
      {notice !== null && (
        <Notice
          lines={notice}
          onDismiss={() => {
            setNotice(null);
          }}
        />
      )}
      {current.id === "catalog" && <CatalogTab {...props} />}
      {current.id === "assignments" && <AssignmentsTab {...props} />}
      {current.id === "connections" && <ConnectionsTab {...props} />}
      {current.id === "keys" && <KeysTab {...props} />}
      {current.id === "resolution" && <ResolutionTab admin={session.admin} />}
    </section>
  );
}
