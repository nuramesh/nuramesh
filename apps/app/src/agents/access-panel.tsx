import { useState } from "react";

import { ConflictError } from "@/api/client";
import type { AgentCredential, AgentScope, IssuedAgentCredential } from "@/api/types";
import { useApi } from "@/api/context";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ConfirmDialog } from "@/org-safety/confirm-dialog";
import { formatLocalWithRelative } from "@/lib/time";
import { useOrgStatus } from "@/org-safety/use-org-safety";

import { AccessBanner } from "./access-banner";
import { useAgentCredentials, useRefreshCredentials } from "./queries";

/** A date in the viewer's local format plus relative time; the raw ISO stays in the attributes. */
function When({ iso }: { iso: string }) {
  return (
    <time dateTime={iso} title={iso}>
      {formatLocalWithRelative(iso)}
    </time>
  );
}

const HALTED_TEXT = "Halted: agent credentials cannot run or be issued or renewed, and can still be revoked here.";

type Action =
  | { kind: "issue" }
  | { kind: "rotate"; credential: AgentCredential }
  | { kind: "revoke"; credential: AgentCredential }
  | { kind: "revoke-all" };

const TONE = { active: "success", expired: "neutral", revoked: "neutral" } as const;

/** The secret, once. It lives only in this component's state: closing the dialog drops it. Never stored, never in the address. */
function SecretDialog({ issued, onClose }: { issued: IssuedAgentCredential; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <Dialog open onOpenChange={(open) => {
        if (!open) onClose();
      }}>
      <DialogContent showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>Credential for {issued.agent}</DialogTitle>
          <DialogDescription>Shown once. Copy it now; it cannot be shown again.</DialogDescription>
        </DialogHeader>
        <code className="block break-all rounded-md border border-card-border bg-panel-2 p-2 text-[13px]" data-testid="secret">
          {issued.secret}
        </code>
        <p className="text-xs text-muted-foreground">Expires <When iso={issued.expiresAt} />. Send it as Authorization: Bearer.</p>
        <DialogFooter>
          <Button
            variant="outline"
            className="min-h-11"
            onClick={() => {
              void navigator.clipboard.writeText(issued.secret).then(() => {
                setCopied(true);
              });
            }}
          >
            {copied ? "Copied" : "Copy"}
          </Button>
          <Button className="min-h-11" onClick={onClose}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function number(text: string, low: number, high: number): number | null {
  const n = Number(text);
  return Number.isInteger(n) && n >= low && n <= high ? n : null;
}

/** The Access panel of one agent (the agent identity notes section 10). Owner only; nothing is read for anyone else. */
export function AccessPanel({ agent, role }: { agent: string; role: string }) {
  const api = useApi();
  const refresh = useRefreshCredentials();
  const list = useAgentCredentials(agent);
  const status = useOrgStatus().data;
  const [action, setAction] = useState<Action | null>(null);
  const [issued, setIssued] = useState<IssuedAgentCredential | null>(null);
  const [scopes, setScopes] = useState<Set<AgentScope>>(new Set(["read"]));
  const [days, setDays] = useState("30");
  const [grace, setGrace] = useState("0");
  const suspended = status?.halted === true || status?.roles.some((r) => r.role === role && r.paused) === true;
  const daysOk = number(days, 1, 90);
  const graceOk = number(grace, 0, 1440);

  if (list.notOwner)
    return (
      <p className="text-xs text-muted-foreground">Only an owner can manage agent access.</p>
    );
  if (list.isPending) return <p role="status" className="text-xs text-muted-foreground">Reading credentials…</p>;
  if (list.isError) return <p role="alert" className="text-xs text-destructive">Could not read this agent's credentials.</p>;

  const done = (message?: string): Promise<string | undefined> => refresh().then(() => message);
  const rows = list.data;
  const toggle = (scope: AgentScope): void => {
    const next = new Set(scopes);
    if (next.has(scope)) next.delete(scope);
    else next.add(scope);
    setScopes(next);
  };
  const conflict = (error: unknown): never => {
    // a 409 reads as the server's own sentence
    throw error instanceof ConflictError ? new Error(error.message) : error;
  };

  return (
    <div id={`access-${agent}`} className="flex flex-col gap-2" aria-label={`Access for ${agent}`} role="group">
      <AccessBanner agent={agent} />
      {suspended && <p className="text-xs text-warning-foreground" role="status">{HALTED_TEXT}</p>}
      {rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">No credentials.</p>
      ) : (
        <ul className="m-0 flex list-none flex-col p-0">
          {rows.map((c) => (
            <li key={c.id} className={`flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-card-border py-1.5 first:border-t-0 ${c.state === "active" ? "" : "opacity-60"}`} data-state={c.state}>
              <code className="text-xs">{c.id}</code>
              <span className="text-xs">{c.scopes.join(", ")}</span>
              <Chip tone={TONE[c.state]}>{c.state}</Chip>
              {c.issuerErased && <Chip tone="warning">issuer erased</Chip>}
              <span className="text-xs text-muted-foreground">issued <When iso={c.createdAt} /> · expires <When iso={c.expiresAt} /></span>
              {c.state === "active" && (
                <span className="ml-auto flex flex-col gap-2 sm:flex-row">
                  <Button variant="outline" size="sm" className="min-h-11 whitespace-nowrap" disabled={suspended} aria-label={`Rotate ${c.id}`} onClick={() => { setAction({ kind: "rotate", credential: c }); }}>
                    Rotate
                  </Button>
                  <Button variant="outline" size="sm" className="min-h-11 whitespace-nowrap" aria-label={`Revoke ${c.id}`} onClick={() => { setAction({ kind: "revoke", credential: c }); }}>
                    Revoke
                  </Button>
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      <fieldset className="flex flex-wrap items-center gap-3 border-0 p-0">
        <legend className="sr-only">Issue a credential</legend>
        {(["read", "run"] as const).map((scope) => (
          <label key={scope} className="flex min-h-11 items-center gap-1.5 text-[13px]">
            <input type="checkbox" checked={scopes.has(scope)} onChange={() => { toggle(scope); }} />
            {scope}
          </label>
        ))}
        <label htmlFor={`days-${agent}`} className="flex items-center gap-1.5 text-[13px]">
          Days
          <Input id={`days-${agent}`} className="w-20" inputMode="numeric" value={days} aria-invalid={daysOk === null} onChange={(e) => { setDays(e.target.value); }} />
        </label>
        <Button className="min-h-11" disabled={suspended || scopes.size === 0 || daysOk === null} onClick={() => { setAction({ kind: "issue" }); }}>
          Issue
        </Button>
        <Button variant="outline" className="min-h-11" disabled={!rows.some((c) => c.state === "active")} onClick={() => { setAction({ kind: "revoke-all" }); }}>
          Revoke all for this agent
        </Button>
      </fieldset>

      <ConfirmDialog
        open={action?.kind === "issue"}
        onOpenChange={(open) => { if (!open) setAction(null); }}
        title={`Issue a credential to ${agent}?`}
        description={<p>Scopes: {[...scopes].sort().join(", ")}. Valid for {days} days. The secret is shown once.</p>}
        confirmLabel="Issue"
        pendingLabel="Issuing…"
        errorPrefix="Could not issue"
        onConfirm={async () => {
          try {
            setIssued(await api.agentCredentials.issue(agent, { scopes: [...scopes], days: daysOk ?? 30 }));
          } catch (error) {
            conflict(error);
          }
          return done();
        }}
      />
      <ConfirmDialog
        open={action?.kind === "rotate"}
        onOpenChange={(open) => { if (!open) setAction(null); }}
        title={`Rotate ${action?.kind === "rotate" ? action.credential.id.slice(-6) : ""}?`}
        description={<p>A new credential with the same scopes. The old one stops after the grace period.</p>}
        confirmLabel="Rotate"
        pendingLabel="Rotating…"
        errorPrefix="Could not rotate"
        confirmDisabled={daysOk === null || graceOk === null}
        onConfirm={async () => {
          if (action?.kind !== "rotate") return undefined;
          try {
            setIssued(await api.agentCredentials.rotate(agent, action.credential.id, { days: daysOk ?? 30, graceMinutes: graceOk ?? 0 }));
          } catch (error) {
            conflict(error);
          }
          return done();
        }}
      >
        <label htmlFor={`grace-${agent}`} className="flex items-center gap-1.5 text-[13px]">
          Grace (minutes)
          <Input id={`grace-${agent}`} className="w-24" inputMode="numeric" value={grace} aria-invalid={graceOk === null} onChange={(e) => { setGrace(e.target.value); }} />
        </label>
      </ConfirmDialog>
      <ConfirmDialog
        open={action?.kind === "revoke"}
        onOpenChange={(open) => { if (!open) setAction(null); }}
        title={`Revoke ${action?.kind === "revoke" ? action.credential.id.slice(-6) : ""} of ${agent}?`}
        description={<p>Agent {agent}, credential ending {action?.kind === "revoke" ? action.credential.id.slice(-6) : ""}. It stops working at once.</p>}
        confirmLabel="Revoke"
        pendingLabel="Revoking…"
        danger
        errorPrefix="Could not revoke"
        onConfirm={async () => {
          if (action?.kind === "revoke") await api.agentCredentials.revoke(agent, action.credential.id);
          return done();
        }}
      />
      <ConfirmDialog
        open={action?.kind === "revoke-all"}
        onOpenChange={(open) => { if (!open) setAction(null); }}
        title={`Revoke every credential of ${agent}?`}
        description={<p>All live credentials of {agent} stop working at once.</p>}
        confirmLabel="Revoke all"
        pendingLabel="Revoking…"
        danger
        errorPrefix="Could not revoke"
        onConfirm={async () => {
          await api.agentCredentials.revokeAll(agent);
          return done();
        }}
      />
      {issued !== null && <SecretDialog issued={issued} onClose={() => { setIssued(null); }} />}
    </div>
  );
}
