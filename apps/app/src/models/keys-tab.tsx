import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { NotFoundError } from "@/api/client";
import { useApi } from "@/api/context";
import { type Connection, type KeyState, STORED_KEY_PROVIDERS } from "@/api/models";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { Input } from "@/components/ui/input";
import { formatStarted } from "@/lib/time";

import { ActionDialog } from "./action-dialog";
import { Loading, ReadFailed, RowCard, type TabProps } from "./parts";
import { describeKeyWrite } from "./problem-text";
import { modelKeys, retryRead, useConnections, useRefreshModels } from "./use-models";

const GRACE_MAX_MINUTES = 7 * 24 * 60;
const STATE_TONE: Record<string, "success" | "warning" | "danger" | "info"> = { current: "success", previous: "info", revoked: "danger", key_missing: "warning" };

interface Action { kind: "set" | "revoke"; connection: Connection; state: KeyState | null }

const label = (c: Connection): string => (c.endpointId === undefined ? c.provider : `${c.provider} (${c.endpointId})`);
const target = (c: Connection) => ({ provider: c.provider, ...(c.endpointId === undefined ? {} : { endpointId: c.endpointId }) });
const hasLiveKey = (s: KeyState | null): boolean => s?.versions.some((v) => v.state !== "revoked") ?? false;

/** The key state of one connection; "no key yet" (404) is a normal answer, not an error. */
function useKeyState(c: Connection) {
  const api = useApi();
  return useQuery({
    queryKey: [...modelKeys.all, "key", c.provider, c.endpointId ?? null] as const,
    queryFn: async (): Promise<KeyState | null> => {
      try {
        return await api.models.keyState(target(c));
      } catch (error) {
        if (error instanceof NotFoundError) return null;
        throw error;
      }
    },
    retry: retryRead,
  });
}

/**
 * Set or rotate. The key is typed into a masked field that is never pre-filled, autocompleted or
 * spell-checked; it is sent once, held in no query cache, and the field is emptied as soon as the
 * request ends (success or refusal), so it is never on screen after submit.
 */
function SetDialog({ action, done, close }: { action: Action; done: (lines: string[]) => void; close: () => void }) {
  const api = useApi();
  const { connection } = action;
  const rotate = hasLiveKey(action.state);
  const [value, setValue] = useState("");
  const [minutes, setMinutes] = useState("");
  const grace = minutes.trim() === "" ? null : Number(minutes);
  const graceBad = grace !== null && (!Number.isInteger(grace) || grace < 0 || grace > GRACE_MAX_MINUTES);
  return (
    <ActionDialog
      title={`${rotate ? "Rotate" : "Set"} the key of ${label(connection)}`}
      description={
        rotate
          ? "The new key is tested with one request first; if the test fails nothing changes. The old key keeps working for the grace period."
          : "The key is tested with one request first; if the test fails nothing is stored. It is kept in the server's secret store and can never be shown again."
      }
      submit={rotate ? "Rotate key" : "Set key"}
      reason={rotate ? "optional" : "required"}
      describe={describeKeyWrite}
      fieldsValid={value.trim() !== "" && !graceBad}
      run={async (reason) => {
        const typed = value;
        try {
          return await api.models.setKey({
            ...target(connection),
            value: typed,
            ...(grace === null ? {} : { graceSeconds: grace * 60 }),
            ...(reason === "" ? {} : { reason }),
          });
        } finally {
          setValue("");
        }
      }}
      onDone={(r) => {
        setValue("");
        done([`${r.first ? "Set" : "Rotated"} the key of ${label(connection)}: version ${String(r.version)} is ${r.state}. The key is not shown again.`]);
      }}
      onClose={() => {
        setValue("");
        close();
      }}
    >
      <div className="flex flex-col gap-1">
        <label htmlFor="key-value" className="text-[13px] font-semibold">
          Key
        </label>
        <Input
          id="key-value"
          type="password"
          className="min-h-11 font-mono"
          autoComplete="new-password"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          maxLength={2000}
          data-1p-ignore
          data-lpignore="true"
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
          }}
        />
        <p className="text-xs text-muted-foreground">Write-only: after you send it, the key is never shown here, in the ledger or in an error message.</p>
      </div>
      {rotate && (
        <div className="flex flex-col gap-1">
          <label htmlFor="key-grace" className="text-[13px] font-semibold">
            Grace period for the old key in minutes <span className="font-normal text-foreground/75">(optional; 0 to {String(GRACE_MAX_MINUTES)})</span>
          </label>
          <Input id="key-grace" className="min-h-11" inputMode="numeric" autoComplete="off" value={minutes} aria-invalid={graceBad ? true : undefined} onChange={(e) => { setMinutes(e.target.value); }} />
          {graceBad && <p role="alert" className="text-xs text-destructive">Enter a whole number of minutes from 0 to {String(GRACE_MAX_MINUTES)}.</p>}
        </div>
      )}
    </ActionDialog>
  );
}

function RevokeDialog({ action, done, close }: { action: Action; done: (lines: string[]) => void; close: () => void }) {
  const api = useApi();
  const { connection } = action;
  return (
    <ActionDialog
      title={`Revoke the key of ${label(connection)}`}
      description="Every version of the key is revoked and its value is destroyed. Models reached through this connection are blocked at their next call until a new key is set."
      submit="Revoke key"
      destructive
      describe={describeKeyWrite}
      run={(reason) => api.models.revokeKey({ ...target(connection), reason })}
      onDone={(r) => {
        done([r.revokedVersions.length === 0 ? "Nothing changed: every version was already revoked." : `Revoked version${r.revokedVersions.length === 1 ? "" : "s"} ${r.revokedVersions.join(", ")} of the key of ${label(connection)}.`]);
      }}
      onClose={close}
    />
  );
}

function KeyRow({ connection, admin, act }: { connection: Connection; admin: boolean; act: (a: Action) => void }) {
  const query = useKeyState(connection);
  const state = query.data ?? null;
  const live = hasLiveKey(state);
  return (
    <RowCard data-ref={label(connection)}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[14px] font-semibold">{label(connection)}</span>
        <Chip tone={live ? "success" : "warning"}>{query.isPending ? "checking" : live ? "key stored" : "no key"}</Chip>
        {state?.rotateAdvised === true && <Chip tone="warning">rotate advised ({String(state.ageDays ?? 90)} days old)</Chip>}
      </div>
      <p className="font-mono text-[13px] break-all">{connection.endpoint}</p>
      {query.isError && <p role="alert" className="text-xs text-destructive">Couldn&apos;t read the key state.</p>}
      {state !== null && (
        <ul aria-label={`Versions of ${label(connection)}`} className="flex flex-col gap-1 text-[13px]">
          {state.versions.map((v) => (
            <li key={v.version} className="flex flex-wrap items-center gap-2">
              <span>Version {v.version}</span>
              <Chip tone={STATE_TONE[v.state] ?? "info"}>{v.state === "key_missing" ? "missing from the store" : v.state}</Chip>
              {v.createdAt !== undefined && <span className="text-xs text-muted-foreground">set {formatStarted(v.createdAt)}</span>}
              {v.graceUntil !== undefined && v.graceUntil !== null && <span className="text-xs text-muted-foreground">works until {formatStarted(v.graceUntil)}</span>}
            </li>
          ))}
        </ul>
      )}
      {admin && (
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" className="min-h-11" aria-label={`${live ? "Rotate key" : "Set key"} ${label(connection)}`} disabled={query.isPending || query.isError} onClick={() => { act({ kind: "set", connection, state }); }}>
            {live ? "Rotate key" : "Set key"}
          </Button>
          {live && (
            <Button variant="outline" size="sm" className="min-h-11 border-danger text-danger hover:bg-danger-soft hover:text-danger" aria-label={`Revoke key ${label(connection)}`} onClick={() => { act({ kind: "revoke", connection, state }); }}>
              Revoke key
            </Button>
          )}
        </div>
      )}
    </RowCard>
  );
}

/** Stored provider keys (R1.14c): set, rotate and revoke. The owner acts; a key is never shown after it is sent. */
export function KeysTab({ admin, notify }: TabProps) {
  const connections = useConnections(admin);
  const refresh = useRefreshModels();
  const api = useApi();
  const health = useQuery({ queryKey: [...modelKeys.all, "keys-health"] as const, queryFn: () => api.models.keysHealth(), enabled: admin, retry: retryRead });
  const [action, setAction] = useState<Action | null>(null);

  if (!admin) {
    return <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">Only an admin can see stored keys: they show which connections hold a key and when it was set.</p>;
  }
  if (connections.isPending) return <Loading what="the connections" />;
  if (connections.isError) return <ReadFailed what="the connections" error={connections.error} retry={() => void connections.refetch()} />;
  const keyed = connections.data.filter((c) => STORED_KEY_PROVIDERS.includes(c.provider));
  const done = (lines: string[]): void => {
    notify(lines);
    setAction(null);
    void refresh();
  };
  const close = (): void => {
    setAction(null);
  };
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        A key is stored on the server, never in this page: it is tested when you send it, kept in the secret store and not shown again. Only the owner can set, rotate or revoke it; every change is recorded in the ledger without the key.
      </p>
      {health.data === "unavailable" && (
        <p role="alert" className="rounded-lg border border-warning p-3 text-sm">
          Secret store unavailable: keys cannot be set, rotated or revoked until the operator restores it.
        </p>
      )}
      {keyed.length === 0 ? (
        <div className="flex flex-col gap-1 rounded-lg border border-dashed p-6 text-sm">
          <strong>No connection takes a stored key yet.</strong>
          <span className="text-muted-foreground">Connect Anthropic first; then its key is set here. Other providers take no stored key.</span>
        </div>
      ) : (
        <ul aria-label="Stored keys" className="flex flex-col gap-3">
          {keyed.map((c) => (
            <KeyRow key={label(c)} connection={c} admin={admin} act={setAction} />
          ))}
        </ul>
      )}
      {action?.kind === "set" && <SetDialog action={action} done={done} close={close} />}
      {action?.kind === "revoke" && <RevokeDialog action={action} done={done} close={close} />}
    </div>
  );
}
