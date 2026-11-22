import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type SyntheticEvent, useState } from "react";

import { useApi } from "@/api/context";
import { describeProblem } from "@/api/problems";
import type { Invite, InviteRole } from "@/api/invites";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const KEY = ["invites"] as const;

const WHEN = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" });
const when = (iso: string): string => `${WHEN.format(new Date(iso))} UTC`;

/**
 * Settings > People (owner only): invite by email with a role, see every invitation, revoke or send a pending one again.
 * The list never carries a link or a token: the server mails the link and no route returns it.
 */
export function PeoplePanel() {
  const api = useApi();
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<InviteRole>("approver");
  const [problem, setProblem] = useState<string | null>(null);
  const list = useQuery({ queryKey: KEY, queryFn: () => api.invites.list(), retry: false });
  const refresh = (): Promise<void> => queryClient.invalidateQueries({ queryKey: KEY });
  const fail = (error: unknown): void => {
    setProblem(describeProblem(error));
  };
  const create = useMutation({
    mutationFn: () => api.invites.create({ email: email.trim(), role }),
    onSuccess: async () => {
      setEmail("");
      setProblem(null);
      await refresh();
    },
    onError: fail,
  });
  const act = useMutation({
    mutationFn: ({ id, what }: { id: string; what: "revoke" | "resend" }) => (what === "revoke" ? api.invites.revoke(id) : api.invites.resend(id)),
    onSuccess: async () => {
      setProblem(null);
      await refresh();
    },
    onError: async (error: unknown) => {
      fail(error);
      await refresh();
    },
  });

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    if (create.isPending || email.trim() === "") return;
    setProblem(null);
    create.mutate();
  };

  return (
    <section aria-labelledby="people-title" className="flex flex-col gap-4 border-t pt-6">
      <header className="flex flex-col gap-1">
        <h2 id="people-title" className="text-lg font-semibold">
          People
        </h2>
        <p className="text-sm text-muted-foreground">
          Invite someone by email. They get a link that works once, set their own password and sign in with the role you chose.
        </p>
      </header>

      <form onSubmit={submit} className="flex flex-wrap items-end gap-3" noValidate>
        <div className="flex min-w-56 flex-1 flex-col gap-1.5">
          <label htmlFor="invite-email" className="text-sm font-medium">
            Email
          </label>
          <Input
            id="invite-email"
            type="email"
            className="min-h-11"
            autoComplete="off"
            inputMode="email"
            autoCapitalize="none"
            spellCheck={false}
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
            }}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="invite-role" className="text-sm font-medium">
            Role
          </label>
          <select
            id="invite-role"
            className="min-h-11 rounded-md border bg-background px-3 text-sm"
            value={role}
            onChange={(e) => {
              setRole(e.target.value === "viewer" ? "viewer" : "approver");
            }}
          >
            <option value="approver">Approver (can decide approvals)</option>
            <option value="viewer">Viewer (read only)</option>
          </select>
        </div>
        <Button type="submit" className="min-h-11" disabled={create.isPending || email.trim() === ""}>
          {create.isPending ? "Sending…" : "Send invitation"}
        </Button>
      </form>

      <div role="alert" className="empty:hidden">
        {problem !== null && <p className="callout-warning px-3 py-2 text-[13px]">{problem}</p>}
      </div>

      {list.isError ? (
        <p className="text-sm text-muted-foreground">The invitations could not be loaded: {describeProblem(list.error)}</p>
      ) : list.data === undefined ? (
        <p role="status" className="text-sm text-muted-foreground">
          Loading invitations…
        </p>
      ) : list.data.length === 0 ? (
        <p className="text-sm text-muted-foreground">No invitations yet.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Invitations, newest first</caption>
          <thead>
            <tr className="text-[13px] text-muted-foreground">
              <th scope="col" className="py-1 pr-3 font-medium">Email</th>
              <th scope="col" className="py-1 pr-3 font-medium">Role</th>
              <th scope="col" className="py-1 pr-3 font-medium">Status</th>
              <th scope="col" className="py-1 font-medium"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {list.data.map((invite) => (
              <InviteRow
                key={invite.id}
                invite={invite}
                busy={act.isPending}
                onAct={(what) => {
                  act.mutate({ id: invite.id, what });
                }}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function InviteRow({ invite, busy, onAct }: { invite: Invite; busy: boolean; onAct: (what: "revoke" | "resend") => void }) {
  const pending = invite.status === "pending";
  return (
    <tr className="border-t align-top">
      <td className="py-2 pr-3 break-all">{invite.email ?? <span className="text-muted-foreground">(accepted)</span>}</td>
      <td className="py-2 pr-3">{invite.role}</td>
      <td className="py-2 pr-3">
        <span>{invite.status}</span>
        <span className="block text-[13px] text-muted-foreground">
          {pending ? `until ${when(invite.expiresAt)}` : ""}
          {pending && invite.resentCount > 0 ? `, sent again ${String(invite.resentCount)}` : ""}
        </span>
      </td>
      <td className="py-2">
        {pending && (
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              className="min-h-11"
              disabled={busy}
              onClick={() => {
                onAct("resend");
              }}
            >
              Send again
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="min-h-11"
              disabled={busy}
              onClick={() => {
                onAct("revoke");
              }}
            >
              Revoke
            </Button>
          </div>
        )}
      </td>
    </tr>
  );
}
