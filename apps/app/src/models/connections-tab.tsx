import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "@/api/context";
import { endpointHint, endpointIdProblem, keyLooksPasted, keyNameProblem, openAiEndpointHint } from "@/api/model-rules";
import type { CatalogEntry, Connection, TestResult } from "@/api/models";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { formatStarted } from "@/lib/time";

import { ActionDialog } from "./action-dialog";
import { ConnectionHistory } from "./connection-history";
import { Loading, ReadFailed, RowCard, type TabProps } from "./parts";
import { PRESETS, type ProviderPreset } from "./presets";
import { describeWrite, testAdvice } from "./problem-text";
import { useCatalog, useConnections, useRefreshModels } from "./use-models";

const KEYLESS = ["azure-openai", "azure-ai"] as const;
const OPENAI_COMPATIBLE = "openai-compatible";
const CUSTOM = "custom";

/** What names one connection in labels: the provider, plus its endpoint id when it has one. */
const nameOf = (c: { provider: string; endpointId?: string }): string => (c.endpointId === undefined ? c.provider : `${c.provider} (${c.endpointId})`);
/** What a write to an existing connection must carry to reach the same one (and keep its sign-in kind). */
const identity = (c: Connection): { endpointId?: string; auth?: "none" | "key"; key?: string } => ({
  ...(c.endpointId === undefined ? {} : { endpointId: c.endpointId }),
  ...(c.provider === OPENAI_COMPATIBLE && c.auth === "none" ? { auth: "none" as const } : {}),
  ...(c.provider === OPENAI_COMPATIBLE && c.auth === "key" && c.keyName !== undefined ? { auth: "key" as const, key: c.keyName } : {}),
});
const hintFor = (c: Connection, endpoint: string): string | null => (c.provider === OPENAI_COMPATIBLE ? openAiEndpointHint(endpoint, c.auth === "key") : endpointHint(endpoint));
const AUTH_TEXT: Record<string, string> = {
  entra: "Entra login, no stored key",
  key_file: "Key from the server's key file",
  none: "No key (a server you run)",
  stored: "Stored key (set on the Keys tab)",
  key: "Key named in the operator's key binding file",
};

type Action =
  | { kind: "connect" | "add" }
  | { kind: "repoint" | "disable" | "enable" | "disconnect"; connection: Connection };

/**
 * Where customer data can go through this connection (ND45). The processing scope of each model
 * is a claim recorded at its approval: the app cannot check it against the endpoint (the resource
 * name carries no region), so the recorded approval is the only control, and it says so.
 */
function LocationWarning({ connection, entries }: { connection: Connection; entries: CatalogEntry[] }) {
  const mine = entries.filter((e) => e.provider === connection.provider && e.endpointId === connection.endpointId && e.status === "approved");
  const customer = mine.filter((e) => e.dataClasses.includes("customer"));
  const synthetic = mine.filter((e) => !e.dataClasses.includes("customer"));
  if (mine.length === 0) {
    return <p className="text-xs text-muted-foreground">No approved model uses this connection yet, so no data goes to it.</p>;
  }
  return (
    <div className={customer.length > 0 ? "callout-warning flex flex-col gap-1 px-3 py-2.5 text-[13px]" : "flex flex-col gap-1 text-[13px]"}>
      {customer.length > 0 && (
        <p>
          <strong>Customer data can be sent to this endpoint</strong> through {customer.map((e) => `${e.ref} (${e.processing})`).join(", ")}.
        </p>
      )}
      {synthetic.length > 0 && <p>Synthetic data only: {synthetic.map((e) => `${e.ref} (${e.processing})`).join(", ")}.</p>}
      <p className="text-xs text-muted-foreground">
        The processing region of a model cannot be checked against the endpoint (the resource name carries no region): the approval recorded for each model is the only control. Repointing this connection pauses these models until they are approved again.
      </p>
    </div>
  );
}

function TestPanel({ provider, endpointId }: { provider: string; endpointId?: string }) {
  const api = useApi();
  const refresh = useRefreshModels();
  const name = nameOf({ provider, ...(endpointId === undefined ? {} : { endpointId }) });
  const test = useMutation({ mutationFn: (): Promise<TestResult> => api.models.testConnection(provider, endpointId), onSettled: () => refresh() });
  const advice = test.data === undefined || test.data.ok ? null : testAdvice(test.data.code, test.data.text);
  return (
    <div className="flex flex-col gap-2">
      <div>
        <Button variant="outline" size="sm" className="min-h-11" aria-label={`Test connection ${name}`} aria-busy={test.isPending ? true : undefined} disabled={test.isPending} onClick={() => { test.mutate(); }}>
          {test.isPending ? "Testing…" : "Test connection"}
        </Button>
      </div>
      {test.data && (
        <p role="status" aria-label="Test result" className={test.data.ok ? "callout-success px-3 py-2 text-[13px]" : "callout-danger px-3 py-2 text-[13px]"}>
          {test.data.ok ? `Test passed in ${String(test.data.latencyMs)} ms: ${test.data.text}` : `Test failed (${test.data.code}) in ${String(test.data.latencyMs)} ms: ${test.data.text}`}
          {advice !== null && <span className="block text-xs">{advice}</span>}
          <span className="block text-xs text-muted-foreground">One metadata read, no prompt, no spend; recorded in the ledger as #{test.data.ledgerSeq}. A failed test changes nothing.</span>
        </p>
      )}
      {test.error !== null && (
        <p role="alert" className="callout-danger px-3 py-2 text-[13px]">
          {describeWrite(test.error).join(" ")}
        </p>
      )}
    </div>
  );
}

function ConnectionRow({ connection, entries, act }: { connection: Connection; entries: CatalogEntry[]; act: (a: Action) => void }) {
  const [showHistory, setShowHistory] = useState(false);
  const disabled = connection.status === "disabled";
  const button = (label: string, kind: "repoint" | "disable" | "enable" | "disconnect", danger = false) => (
    <Button
      variant="outline"
      size="sm"
      aria-label={`${label} ${nameOf(connection)}`}
      className={`min-h-11 ${danger ? "border-danger text-danger hover:bg-danger-soft hover:text-danger" : ""}`}
      onClick={() => {
        act({ kind, connection });
      }}
    >
      {label}
    </Button>
  );
  return (
    <RowCard>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[14px] font-semibold">{nameOf(connection)}</span>
        <Chip tone={disabled ? "warning" : "success"}>{connection.status}</Chip>
        <span className="text-xs text-muted-foreground">{AUTH_TEXT[connection.auth] ?? "Sign-in kind unknown"}</span>
      </div>
      <p className="font-mono text-[13px] break-all">{connection.endpoint}</p>
      <p className="text-xs text-muted-foreground">
        Set by {connection.setBy} · {formatStarted(connection.setAt)} · ledger #{connection.seq}
      </p>
      <LocationWarning connection={connection} entries={entries} />
      <div className="flex flex-wrap gap-2">
        {button("Repoint", "repoint")}
        {disabled ? button("Enable", "enable") : button("Disable", "disable")}
        {button("Disconnect", "disconnect", true)}
        <Button variant="outline" size="sm" className="min-h-11" aria-label={`History ${connection.provider}${connection.endpointId === undefined ? "" : ` ${connection.endpointId}`}`} onClick={() => { setShowHistory(true); }}>
          History
        </Button>
      </div>
      {showHistory && <ConnectionHistory provider={connection.provider} {...(connection.endpointId === undefined ? {} : { endpointId: connection.endpointId })} close={() => { setShowHistory(false); }} />}
      <TestPanel provider={connection.provider} {...(connection.endpointId === undefined ? {} : { endpointId: connection.endpointId })} />
    </RowCard>
  );
}

function ConnectDialog({ taken, done, close }: { taken: string[]; done: (lines: string[]) => void; close: () => void }) {
  const api = useApi();
  const free = KEYLESS.filter((p) => !taken.includes(p));
  const [provider, setProvider] = useState<string>(free[0] ?? KEYLESS[0]);
  const [endpoint, setEndpoint] = useState("");
  const hint = endpoint === "" ? null : endpointHint(endpoint);
  return (
    <ActionDialog
      title="Connect a provider"
      description="A connection is the one destination of every model of that provider. It uses the keyless Entra login. No key is stored or typed here."
      submit="Connect"
      reason="optional"
      reasonLabel="Reason"
      fieldsValid={endpoint !== "" && hint === null}
      run={(reason) => api.models.putConnection(provider, { endpoint: endpoint.trim(), ...(reason === "" ? {} : { reason }) })}
      onDone={() => {
        done([`Connected ${provider} to ${endpoint.trim()}`]);
      }}
      onClose={close}
    >
      <div className="flex flex-col gap-1">
        <label htmlFor="conn-provider" className="text-[13px] font-semibold">
          Provider
        </label>
        <NativeSelect id="conn-provider" className="min-h-11" value={provider} onChange={(e) => { setProvider(e.target.value); }}>
          {KEYLESS.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </NativeSelect>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="conn-endpoint" className="text-[13px] font-semibold">
          Endpoint
        </label>
        <Input id="conn-endpoint" className="min-h-11 font-mono" inputMode="url" autoComplete="off" spellCheck={false} placeholder="https://<name>.openai.azure.com" value={endpoint} aria-invalid={hint === null ? undefined : true} onChange={(e) => { setEndpoint(e.target.value); }} />
        {hint !== null && <p role="alert" className="text-xs text-destructive">{hint}</p>}
      </div>
    </ActionDialog>
  );
}

interface Saved {
  provider: string;
  endpointId: string;
  keyName: string | null;
  modelHint: string | null;
}

/**
 * One form to add an openai-compatible connection: pick a preset (it prefills the base URL and a
 * model-name hint), change the URL if needed, name the operator's key binding when the endpoint
 * needs a key, save, then test. No key is typed or pasted here and none is ever shown: the
 * connection holds only the NAME of a key binding in the operator's binding file, which maps the
 * name to a key file and the hosts it may be sent to. A preset is data: it ships no price, so a
 * model on this connection has an unknown price until one is set.
 */
function AddConnectionDialog({ takenIds, onSaved, close }: { takenIds: readonly string[]; onSaved: (lines: string[]) => void; close: () => void }) {
  const api = useApi();
  const first: ProviderPreset | undefined = PRESETS[0];
  const [presetId, setPresetId] = useState<string>(first?.id ?? CUSTOM);
  const [endpointId, setEndpointId] = useState<string>(first?.id ?? "");
  const [endpoint, setEndpoint] = useState<string>(first?.baseUrl ?? "");
  const [keyName, setKeyName] = useState<string>(first?.needsKey === true ? first.id : "");
  const [saved, setSaved] = useState<Saved | null>(null);
  /** Which of the two always-required fields the person has edited: an empty field is an error only after that. */
  const [edited, setEdited] = useState({ id: false, url: false });
  const preset = PRESETS.find((p) => p.id === presetId) ?? null;
  const keyed = keyName.trim() !== "";
  const urlHint = endpoint.trim() === "" ? (edited.url ? "Enter the base URL of the endpoint" : null) : openAiEndpointHint(endpoint, keyed);
  const idTaken = takenIds.includes(endpointId.trim());
  const idHint =
    endpointId === ""
      ? edited.id ? "Enter an id for this connection" : null
      : (endpointIdProblem(endpointId) ?? (idTaken ? `A connection with the id ${endpointId.trim()} already exists. Choose another id, or close this form and use Repoint to change that connection` : null));
  const pasted = keyed && keyLooksPasted(keyName);
  const keyMissing = preset?.needsKey === true && !keyed;
  const nameHint = keyMissing ? `${preset.label} needs a key: enter the name of its key binding` : !keyed ? null : pasted ? "This looks like a key, not a name. Do not paste a key here: enter the name of the key binding your operator set up" : keyNameProblem(keyName.trim());
  const pick = (id: string): void => {
    setPresetId(id);
    setEdited({ id: false, url: false });
    const next = PRESETS.find((p) => p.id === id);
    if (next !== undefined) {
      setEndpointId(next.id);
      setEndpoint(next.baseUrl);
      setKeyName(next.needsKey ? next.id : "");
    } else if (preset !== null) {
      // Another endpoint starts empty: drop what the last preset filled in, keep what the person typed.
      if (endpointId === preset.id) setEndpointId("");
      if (endpoint === preset.baseUrl) setEndpoint("");
      if (keyName === (preset.needsKey ? preset.id : "")) setKeyName("");
    }
  };
  const optionsOf = (local: boolean) =>
    PRESETS.filter((p) => p.local === local).map((p) => (
      <option key={p.id} value={p.id}>
        {p.label}
      </option>
    ));

  if (saved !== null) {
    return (
      <Dialog open onOpenChange={(open) => { if (!open) close(); }}>
        <DialogContent className="max-h-[90dvh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Connection saved</DialogTitle>
            <DialogDescription asChild>
              <div>
                <span className="font-mono">{OPENAI_COMPATIBLE} ({saved.endpointId})</span> is connected{saved.keyName === null ? " without a key." : ` with the key binding named ${saved.keyName}. No key is shown or kept in this page.`}
              </div>
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3 text-[13px]">
            {saved.modelHint !== null && <p>Next, add a catalog entry for your model on the Catalog tab: choose Add model and pick this connection (model name, for example: {saved.modelHint}). No price is set: an unknown price stays recorded as unknown until you set one.</p>}
            <TestPanel provider={saved.provider} endpointId={saved.endpointId} />
          </div>
          <DialogFooter>
            <Button className="min-h-11" onClick={close}>Done</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <ActionDialog
      title="Add a connection"
      description="Connect an OpenAI-compatible endpoint: a hosted service or a model server you run. Pick a preset to prefill the URL, name the key binding if the endpoint needs a key, then save and test the connection."
      submit="Save connection"
      reason="optional"
      reasonLabel="Reason"
      fieldsValid={endpoint.trim() !== "" && urlHint === null && endpointId !== "" && idHint === null && nameHint === null}
      run={async (reason) => {
        const id = endpointId.trim();
        await api.models.putConnection(OPENAI_COMPATIBLE, {
          endpointId: id,
          endpoint: endpoint.trim(),
          auth: keyed ? "key" : "none",
          ...(keyed ? { key: keyName.trim() } : {}),
          ...(reason === "" ? {} : { reason }),
        });
        return id;
      }}
      onDone={(id) => {
        setSaved({ provider: OPENAI_COMPATIBLE, endpointId: id, keyName: keyed ? keyName.trim() : null, modelHint: preset?.modelHint ?? null });
        onSaved([`Connected ${OPENAI_COMPATIBLE} (${id}) to ${endpoint.trim()}${keyed ? ` with the key binding ${keyName.trim()}` : " without a key"}`]);
      }}
      onClose={close}
    >
      <div className="flex flex-col gap-1">
        <label htmlFor="add-preset" className="text-[13px] font-semibold">
          Preset
        </label>
        <NativeSelect id="add-preset" className="min-h-11" value={presetId} aria-describedby="add-preset-help" onChange={(e) => { pick(e.target.value); }}>
          <optgroup label="Hosted services">{optionsOf(false)}</optgroup>
          <optgroup label="Servers you run">{optionsOf(true)}</optgroup>
          <optgroup label="Other">
            <option value={CUSTOM}>Another OpenAI-compatible endpoint</option>
          </optgroup>
        </NativeSelect>
        <p id="add-preset-help" className="text-xs text-muted-foreground">
          {preset === null
            ? "Enter the base URL of your endpoint (it often ends in /v1) and an id that tells this connection from the others."
            : `Model name, for example: ${preset.modelHint}.${preset.local ? " 127.0.0.1 is the machine the NuraMesh server runs on, not the one your browser is on. The connection test is refused unless its operator sets NURAMESH_ALLOW_PRIVATE_NETWORK=1." : ""}`}
        </p>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="add-endpoint-id" className="text-[13px] font-semibold">
          Connection id
        </label>
        <Input id="add-endpoint-id" className="min-h-11 font-mono" autoComplete="off" spellCheck={false} maxLength={40} value={endpointId} aria-required="true" aria-invalid={idHint === null ? undefined : true} aria-describedby={idHint === null ? undefined : "add-endpoint-id-error"} onChange={(e) => { setEndpointId(e.target.value); setEdited((was) => ({ ...was, id: true })); }} />
        {idHint !== null && <p id="add-endpoint-id-error" role="alert" className="text-xs text-destructive">{idHint}</p>}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="add-endpoint" className="text-[13px] font-semibold">
          Base URL
        </label>
        <Input id="add-endpoint" className="min-h-11 font-mono" inputMode="url" autoComplete="off" spellCheck={false} value={endpoint} aria-required="true" aria-invalid={urlHint === null ? undefined : true} aria-describedby={urlHint === null ? undefined : "add-endpoint-error"} onChange={(e) => { setEndpoint(e.target.value); setEdited((was) => ({ ...was, url: true })); }} />
        {urlHint !== null && <p id="add-endpoint-error" role="alert" className="text-xs text-destructive">{urlHint}</p>}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="add-key-name" className="text-[13px] font-semibold">
          Key binding name <span className="font-normal text-foreground/75">({preset?.needsKey === true ? `required for ${preset.label}` : "optional: leave empty for a server without a key"})</span>
        </label>
        <Input id="add-key-name" className="min-h-11 font-mono" autoComplete="off" autoCapitalize="off" spellCheck={false} maxLength={40} value={keyName} aria-required={preset?.needsKey === true ? "true" : undefined} aria-invalid={nameHint === null ? undefined : true} aria-describedby={nameHint === null ? "add-key-name-help" : "add-key-name-help add-key-name-error"} onChange={(e) => { setKeyName(e.target.value); }} />
        {nameHint !== null && <p id="add-key-name-error" role="alert" className="text-xs text-destructive">{nameHint}</p>}
        <p id="add-key-name-help" className="text-xs text-muted-foreground">This is a name, never the key itself. Your operator maps the name to a key file and the hosts it may be sent to in the key binding file; a name that is not in that file fails the connection test. Do not paste a key here.</p>
      </div>
    </ActionDialog>
  );
}

function ChangeDialog({ action, entries, done, close }: { action: Extract<Action, { connection: Connection }>; entries: CatalogEntry[]; done: (lines: string[]) => void; close: () => void }) {
  const api = useApi();
  const { connection } = action;
  const [endpoint, setEndpoint] = useState(connection.endpoint);
  const approved = entries.filter((e) => e.provider === connection.provider && e.endpointId === connection.endpointId && e.status === "approved");

  if (action.kind === "disconnect") {
    return (
      <ActionDialog
        title={`Disconnect ${nameOf(connection)}`}
        description="Models of this provider are blocked at their next call (no connection) until an admin connects it again."
        submit="Disconnect"
        destructive
        reason="optional"
        run={(reason) => api.models.removeConnection(connection.provider, reason === "" ? undefined : reason, connection.endpointId)}
        onDone={() => {
          done([`Disconnected ${nameOf(connection)}`]);
        }}
        onClose={close}
      />
    );
  }
  if (action.kind === "disable" || action.kind === "enable") {
    const enable = action.kind === "enable";
    return (
      <ActionDialog
        title={`${enable ? "Enable" : "Disable"} ${nameOf(connection)}`}
        description={enable ? "Enabling lets models of this provider be called again through the same endpoint." : "Models of this provider are blocked at their next call until the connection is enabled. The endpoint is kept."}
        submit={enable ? "Enable" : "Disable"}
        reason="optional"
        run={(reason) => api.models.putConnection(connection.provider, { ...identity(connection), endpoint: connection.endpoint, status: enable ? "active" : "disabled", ...(reason === "" ? {} : { reason }) })}
        onDone={() => {
          done([`${enable ? "Enabled" : "Disabled"} ${nameOf(connection)}`]);
        }}
        onClose={close}
      />
    );
  }
  const changed = endpoint.trim().replace(/\/$/, "").toLowerCase() !== connection.endpoint.toLowerCase();
  const repointHint = endpoint === "" ? null : hintFor(connection, endpoint);
  return (
    <ActionDialog
      title={`Repoint ${nameOf(connection)}`}
      description="Repointing changes where the data of every approved model of this provider goes (data location, ND45). A reason is required."
      submit="Repoint"
      fieldsValid={changed && endpoint !== "" && repointHint === null}
      warning={
        changed && approved.length > 0 ? (
          <p>These approved models will be paused and need a fresh approval at the new endpoint: {approved.map((e) => e.ref).join(", ")}.</p>
        ) : undefined
      }
      run={(reason) => api.models.putConnection(connection.provider, { ...identity(connection), endpoint: endpoint.trim(), reason })}
      onDone={(r) => {
        done([
          `Repointed ${nameOf(connection)} to ${endpoint.trim()}`,
          ...(r.repaused.length > 0 ? [`Paused again, approve each again with a reason: ${r.repaused.join(", ")}. Their data now goes to a new endpoint.`] : []),
        ]);
      }}
      onClose={close}
    >
      <div className="flex flex-col gap-1">
        <label htmlFor="repoint-endpoint" className="text-[13px] font-semibold">
          Endpoint
        </label>
        <Input id="repoint-endpoint" className="min-h-11 font-mono" inputMode="url" autoComplete="off" spellCheck={false} value={endpoint} onChange={(e) => { setEndpoint(e.target.value); }} />
        {repointHint !== null && <p role="alert" className="text-xs text-destructive">{repointHint}</p>}
      </div>
    </ActionDialog>
  );
}

/** Provider connections (spec 10.4 and 11): add, connect, enable, disable, repoint, remove and test. Admin only. */
export function ConnectionsTab({ admin, notify }: TabProps) {
  const connections = useConnections(admin);
  const catalog = useCatalog();
  const refresh = useRefreshModels();
  const [action, setAction] = useState<Action | null>(null);

  if (!admin) {
    return <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">Only an admin can see connections: they carry the endpoint each provider is reached at and who set it.</p>;
  }
  if (connections.isPending) return <Loading what="the connections" />;
  if (connections.isError) return <ReadFailed what="the connections" error={connections.error} retry={() => void connections.refetch()} />;
  const entries = catalog.data?.entries ?? [];
  const done = (lines: string[]): void => {
    notify(lines);
    setAction(null);
    void refresh();
  };
  const close = (): void => {
    setAction(null);
  };
  const taken = connections.data.map((c) => c.provider);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-0 flex-1 basis-64 text-sm text-muted-foreground">
          A connection is where a provider is reached: a keyless Azure endpoint (Entra login), or an OpenAI-compatible endpoint, with a key named in the operator&apos;s key binding file or without a key. The endpoint comes only from here: no environment variable is read.
        </p>
        <Button className="min-h-11" onClick={() => { setAction({ kind: "add" }); }}>
          Add a connection
        </Button>
        <Button variant="outline" className="min-h-11" disabled={taken.length >= KEYLESS.length} onClick={() => { setAction({ kind: "connect" }); }}>
          Connect a provider
        </Button>
      </div>
      {connections.data.length === 0 ? (
        <div className="flex flex-col gap-1 rounded-lg border border-dashed p-6 text-sm">
          <strong>No connections yet.</strong>
          <span className="text-muted-foreground">Until a provider is connected, its models are refused with &ldquo;no connection&rdquo;. The fake model needs none. Add a connection for an OpenAI-compatible endpoint, or connect a keyless Azure provider.</span>
        </div>
      ) : (
        <ul aria-label="Connections" className="flex flex-col gap-3">
          {connections.data.map((c) => (
            <ConnectionRow key={`${c.provider}/${c.endpointId ?? ""}`} connection={c} entries={entries} act={setAction} />
          ))}
        </ul>
      )}
      {action?.kind === "connect" && <ConnectDialog taken={taken} done={done} close={close} />}
      {action?.kind === "add" && <AddConnectionDialog takenIds={connections.data.flatMap((c) => (c.provider === OPENAI_COMPATIBLE && c.endpointId !== undefined ? [c.endpointId] : []))} onSaved={(lines) => { notify(lines); void refresh(); }} close={close} />}
      {action !== null && "connection" in action && <ChangeDialog action={action} entries={entries} done={done} close={close} />}
    </div>
  );
}
