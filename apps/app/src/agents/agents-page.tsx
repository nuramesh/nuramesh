import { AccessBanner } from "./access-banner";
import { AccessPanel } from "./access-panel";
import { useAgentList, useCallerRole } from "./queries";

const NOT_OWNER = "Only an owner can manage agent access.";

/**
 * The organization's agents, each with its Access panel (E2, the agent identity notes section 10). Owners
 * only: a person known to be an approver or a viewer sees one sentence and the app calls nothing;
 * where the role is unknown the server decides and its refusal shows the same sentence.
 */
export function AgentsPage() {
  const role = useCallerRole();
  const known = role.isSuccess || role.isError;
  const blocked = role.isSuccess && role.data !== null && role.data !== "owner";
  const agents = useAgentList(known && !blocked);
  const denied = blocked || agents.notOwner;
  return (
    <section aria-labelledby="page-title" className="flex max-w-3xl flex-col gap-4 p-6">
      <h1 id="page-title" className="text-xl font-semibold">
        Agents
      </h1>
      {denied && <p className="text-sm text-muted-foreground">{NOT_OWNER}</p>}
      {!denied && known && <AccessBanner />}
      {!denied && (!known || agents.isPending) && (
        <p role="status" className="text-sm text-muted-foreground">
          Reading agents…
        </p>
      )}
      {!denied && agents.isError && (
        <p role="alert" className="text-sm text-destructive">
          Could not read the organization's agents.
        </p>
      )}
      {!denied && agents.isSuccess && agents.data.length === 0 && (
        <p className="text-sm text-muted-foreground">This organization has no agents yet. Add one in the Workflow builder.</p>
      )}
      {!denied && (
        <ul className="m-0 flex list-none flex-col gap-4 p-0">
          {agents.data?.map((a) => (
            <li key={a.id} id={`access-${a.id}`} className="rounded-md border border-card-border bg-card p-3">
              <h2 className="text-[15px] font-semibold">
                {a.name}{" "}
                <span className="text-xs font-normal text-muted-foreground">
                  {a.id} · role {a.role} · {a.activeCredentials} live
                </span>
              </h2>
              <AccessPanel agent={a.id} role={a.role} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
