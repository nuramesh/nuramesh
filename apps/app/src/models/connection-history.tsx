import type { ConnectionHistoryRow } from "@/api/models";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { formatStarted } from "@/lib/time";

import { Loading, ReadFailed } from "./parts";
import { useConnectionHistory } from "./use-models";

const TYPE_WORDS = new Map<string, string>(Object.entries({
  "model.connection_created": "Connected",
  "model.connection_updated": "Updated",
  "model.connection_removed": "Disconnected",
  "model.connection_tested": "Tested",
  "model.key_stored": "Key stored",
  "model.key_activated": "Key activated",
  "model.key_test_failed": "Key test failed",
  "model.key_revoked": "Key revoked",
  "model.key_destroyed": "Key destroyed",
  "model.key_read_failed": "Key read failed",
}));

/** The stored-key entries whose row may carry a reason text (the others never keep one). */
const KEY_REASON_TYPES = new Set(["model.key_activated", "model.key_revoked"]);

/** The details of a stored-key row (R1.14c spec 9): key id, version, and the fixed code or cause; never a fingerprint. */
function KeyDetails({ row }: { row: ConnectionHistoryRow }) {
  const version = row.version === "all" ? "all versions" : `version ${String(row.version)}`;
  return (
    <>
      <span className="font-mono text-[13px] break-all">{row.keyId} · {version}</span>
      {row.previousVersion !== undefined && (
        <span className="text-[13px]">
          replaces version {row.previousVersion}, which stays usable for {row.graceSeconds ?? 0} s
        </span>
      )}
      {row.code !== undefined && <span className="text-[13px]">Result: {row.code}</span>}
      {row.cause !== undefined && row.cause !== null && <span className="text-[13px]">Cause: {row.cause}</span>}
      {KEY_REASON_TYPES.has(row.type) && (
        <span className="text-[13px]">{row.reason === undefined || row.reason === null ? <em className="text-muted-foreground">no reason kept or it expired</em> : <>Reason: {row.reason}</>}</span>
      )}
    </>
  );
}

/** One history row in words. Only the fields the mapper kept are read; a placeholder row shows no value at all. */
function HistoryRow({ row }: { row: ConnectionHistoryRow }) {
  const what = TYPE_WORDS.get(row.type) ?? "Changed";
  const meta = (
    <span className="text-xs text-muted-foreground">
      ledger #{row.seq} · {row.actor} · {formatStarted(row.at)}
    </span>
  );
  if (!row.valid) {
    return (
      <li className="flex flex-col gap-0.5 rounded-lg border border-dashed border-card-border px-3 py-2 text-sm">
        <span className="font-medium">{what}: details not shown</span>
        {meta}
        <span className="text-[13px] text-muted-foreground">
          Not shown: the server judged this entry not valid for this server (it is malformed, unsupported or refused here, for example on a hosted server). It stays in the ledger.
        </span>
      </li>
    );
  }
  if (row.keyId !== undefined) {
    return (
      <li className="flex flex-col gap-0.5 rounded-lg border border-card-border px-3 py-2 text-sm">
        <span className="font-medium">{what}</span>
        {meta}
        <KeyDetails row={row} />
      </li>
    );
  }
  const tested = row.type === "model.connection_tested";
  return (
    <li className="flex flex-col gap-0.5 rounded-lg border border-card-border px-3 py-2 text-sm">
      <span className="flex flex-wrap items-center gap-2 font-medium">
        {tested ? (row.ok === true ? "Test passed" : "Test failed") : what}
        {row.repoint === true && <Chip tone="warning">Repointed</Chip>}
        {row.status !== undefined && !tested && <Chip tone={row.status === "disabled" ? "warning" : "neutral"}>{row.status}</Chip>}
      </span>
      {meta}
      {row.endpoint !== undefined && <span className="font-mono text-[13px] break-all">{row.endpoint}</span>}
      {(row.auth !== undefined || row.keyName !== undefined) && (
        <span className="text-xs text-muted-foreground">
          {row.auth !== undefined && <>Login: {row.auth}</>}
          {row.keyName !== undefined && <>{row.auth !== undefined ? " · " : ""}Key name: {row.keyName}</>}
        </span>
      )}
      {tested && (
        <span className="text-[13px]">
          Result: {row.code ?? "unknown"}
          {row.statusClass !== undefined && row.statusClass !== null ? ` (${row.statusClass})` : ""}
        </span>
      )}
      {!tested && (
        <span className="text-[13px]">{row.reason === undefined || row.reason === null ? <em className="text-muted-foreground">no reason kept or it expired</em> : <>Reason: {row.reason}</>}</span>
      )}
    </li>
  );
}

/** The History drawer of one connection (per `endpoint_id` for `openai-compatible`): newest page first, "Load older" for the next. */
export function ConnectionHistory({ provider, endpointId, close }: { provider: string; endpointId?: string; close: () => void }) {
  const history = useConnectionHistory({ provider, ...(endpointId === undefined ? {} : { endpointId }) });
  const label = endpointId === undefined ? provider : `${provider} ${endpointId}`;
  const rows = history.data?.pages.flatMap((page) => [...page.rows].reverse()) ?? [];
  return (
    <Dialog open onOpenChange={(open) => { if (!open) close(); }}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>History: {label}</DialogTitle>
          <DialogDescription>
            Every change and test of this connection and of its stored key from the ledger, newest first. Keys are never shown, only a key name or a stored key's id and version. A row the server cannot show is a placeholder.
          </DialogDescription>
        </DialogHeader>
        {history.isPending && <Loading what="the connection history" />}
        {history.isError && <ReadFailed what="the connection history" error={history.error} retry={() => void history.refetch()} />}
        {history.data && rows.length === 0 && <p className="text-sm text-muted-foreground">No changes recorded for this connection yet.</p>}
        {history.data && rows.length > 0 && (
          <ol className="flex flex-col gap-2.5" aria-label="Connection history">
            {rows.map((row) => (
              <HistoryRow key={row.seq} row={row} />
            ))}
          </ol>
        )}
        {history.hasNextPage && (
          <div>
            <Button variant="outline" className="min-h-11" disabled={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>
              {history.isFetchingNextPage ? "Loading…" : "Load older"}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
