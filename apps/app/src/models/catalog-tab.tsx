import { useState } from "react";

import { type AssignmentRow, type Catalog, type CatalogEntry, type Connection, entryRef, FREE_SOURCE, modelRef, OPENAI_COMPATIBLE } from "@/api/models";
import { useApi } from "@/api/context";
import { isLoopbackEndpoint, parseRegions, regionsWiden, validProcessing, widens } from "@/api/model-rules";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import { formatStarted } from "@/lib/time";

import { ActionDialog } from "./action-dialog";
import { Loading, ReadFailed, RowCard, StatusChip, type TabProps } from "./parts";
import { resultLines } from "./problem-text";
import { useAssignments, useCatalog, useConnections, useEvents, useRefreshModels } from "./use-models";
import { dataWords, noPriceCount, originWords, priceWords } from "./words";

type Action =
  | { kind: "add" }
  | { kind: "regions" }
  | { kind: "history" }
  | { kind: "approve" | "pause" | "retire" | "remove" | "edit" | "price"; entry: CatalogEntry };

function Field({ label, help, children }: { label: string; help?: string; children: (id: string) => React.ReactNode }) {
  const id = `f-${label.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-[13px] font-semibold">
        {label}
      </label>
      {children(id)}
      {help !== undefined && <p className="text-xs text-muted-foreground">{help}</p>}
    </div>
  );
}

function Processing({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const kind = value === "canada" || value === "global" ? value : "region";
  return (
    <div className="flex flex-col gap-2">
      <NativeSelect
        id="f-processing-scope"
        className="min-h-11 w-full"
        value={kind}
        onChange={(e) => {
          onChange(e.target.value === "region" ? "region:" : e.target.value);
        }}
      >
        <option value="canada">canada: processed in Canada</option>
        <option value="global">global: may be processed anywhere</option>
        <option value="region">a chosen region</option>
      </NativeSelect>
      {kind === "region" && (
        <Input
          aria-label="Region id"
          className="min-h-11 font-mono"
          placeholder="region:eu-west"
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
          }}
        />
      )}
    </div>
  );
}

/** Processing as the ND45 rule reads it: what data classes this scope may receive. */
function ProcessingHelp({ regions }: { regions: string[] }) {
  return (
    <p className="text-xs text-muted-foreground">
      <code>global</code> takes synthetic data only; <code>canada</code> takes synthetic and customer data; <code>region:&lt;id&gt;</code> takes customer data only when the region is one of the customer&apos;s chosen regions ({regions.length === 0 ? "none chosen yet" : regions.join(", ")}). The scope is the claim made when approving; the app cannot check it against the endpoint.
    </p>
  );
}

function CatalogDialogs({ action, catalog, assignments, notify, close }: { action: Action; catalog: Catalog; assignments: AssignmentRow[]; notify: (lines: string[]) => void; close: () => void }) {
  const api = useApi();
  const refresh = useRefreshModels();
  const done = (lines: string[]): void => {
    notify(lines);
    close();
    void refresh();
  };

  if (action.kind === "history") return <History close={close} />;

  if (action.kind === "add") {
    return <AddDialog catalog={catalog} done={done} close={close} />;
  }
  if (action.kind === "regions") return <RegionsDialog catalog={catalog} done={done} close={close} />;

  const { entry } = action;
  // the entry's own connection goes with every write (the server needs `endpoint_id` for an openai-compatible entry)
  const ref = entryRef(entry);
  const on = entry.endpointId === undefined ? "" : ` on the connection ${entry.provider} (${entry.endpointId})`;
  const users = assignments.filter((a) => a.ref === entry.ref).map((a) => a.subject);
  const usedBy = users.length === 0 ? "No agent or role uses it now." : `Used by ${users.join(", ")}: their next call is blocked.`;

  switch (action.kind) {
    case "approve":
      return (
        <ActionDialog
          title={`Approve ${entry.ref}`}
          description={`Approving lets runs use ${entry.ref}${on} for ${dataWords(entry.dataClasses)}. It needs a price in effect: a price is never guessed.`}
          submit="Approve"
          run={(reason) => api.models.approve({ ...ref, reason })}
          onDone={(r) => {
            done(resultLines(r, `Approved ${entry.ref}`));
          }}
          onClose={close}
        />
      );
    case "pause":
    case "retire": {
      const retire = action.kind === "retire";
      return (
        <ActionDialog
          title={`${retire ? "Retire" : "Pause"} ${entry.ref}`}
          description={`${usedBy} ${retire ? "Retiring is final: a retired model cannot be approved again (remove it and add it again)." : "A paused model can be approved again."}`}
          submit={retire ? "Retire" : "Pause"}
          destructive={retire}
          run={(reason) => (retire ? api.models.retire({ ...ref, reason }) : api.models.pause({ ...ref, reason }))}
          onDone={(r) => {
            done(resultLines(r, `${retire ? "Retired" : "Paused"} ${entry.ref}`, { listAffected: true }));
          }}
          onClose={close}
        />
      );
    }
    case "remove":
      return (
        <ActionDialog
          title={`Remove ${entry.ref}`}
          description={`${usedBy} A model must be paused or retired, and not assigned, before it is removed. The removal is recorded; history is kept.`}
          submit="Remove"
          destructive
          run={(reason) => api.models.remove({ ...ref, reason })}
          onDone={(r) => {
            done(resultLines(r, `Removed ${entry.ref}`));
          }}
          onClose={close}
        />
      );
    case "edit":
      return <EditDialog entry={entry} catalog={catalog} done={done} close={close} />;
    case "price":
      return <PriceDialog entry={entry} done={done} close={close} />;
  }
}

function AddDialog({ catalog, done, close }: { catalog: Catalog; done: (lines: string[]) => void; close: () => void }) {
  const api = useApi();
  // only an admin opens this dialog, so the admin-only read is allowed
  const connections = useConnections(true);
  const compatible = (connections.data ?? []).filter((c): c is Connection & { endpointId: string } => c.provider === OPENAI_COMPATIBLE && c.endpointId !== undefined);
  const [endpointId, setEndpointId] = useState("");
  const [typed, setTyped] = useState("");
  const [model, setModel] = useState("");
  const [family, setFamily] = useState("");
  const [processing, setProcessing] = useState("canada");
  const picked = compatible.find((c) => c.endpointId === endpointId);
  const provider = picked === undefined ? typed.trim() : OPENAI_COMPATIBLE;
  // an openai-compatible model is named by its connection: the server refuses it without one
  const needsPick = picked === undefined && provider === OPENAI_COMPATIBLE;
  const remote = picked !== undefined && !isLoopbackEndpoint(picked.endpoint);
  const target = { provider, ...(picked === undefined ? {} : { endpointId: picked.endpointId }), model: model.trim() };
  const valid = provider !== "" && !needsPick && model.trim() !== "" && family.trim() !== "" && validProcessing(processing);
  return (
    <ActionDialog
      title="Add a model"
      description="A new model starts paused: it cannot be used until an admin approves it, and approving needs a price."
      submit="Add model"
      fieldsValid={valid}
      run={(reason) => api.models.addEntry({ ...target, family: family.trim(), processing, reason })}
      onDone={(r) => {
        done(resultLines(r, `Added ${modelRef(target)} (paused until approved)`));
      }}
      onClose={close}
    >
      <Field
        label="Connection"
        help={
          connections.isError
            ? "The connections could not be read, so only a keyless provider can be named here."
            : compatible.length === 0
              ? "To add a model of an OpenAI-compatible endpoint (a hosted service or your own model server), add its connection first on the Connections tab."
              : "Pick the connection the model is served by. A model of an OpenAI-compatible endpoint belongs to one connection."
        }
      >
        {(id) => (
          <NativeSelect
            id={id}
            className="min-h-11 w-full"
            value={picked === undefined ? "" : picked.endpointId}
            onChange={(e) => {
              const next = compatible.find((c) => c.endpointId === e.target.value);
              setEndpointId(e.target.value);
              // a hosted endpoint takes only global processing (the server's rule); a local one keeps the choice
              if (next !== undefined && !isLoopbackEndpoint(next.endpoint)) setProcessing("global");
            }}
          >
            <option value="">A keyless provider (azure-openai, azure-ai)</option>
            {compatible.map((c) => (
              <option key={c.endpointId} value={c.endpointId}>
                {`${c.provider} (${c.endpointId}) · ${c.endpoint}${c.status === "disabled" ? " (disabled)" : ""}`}
              </option>
            ))}
          </NativeSelect>
        )}
      </Field>
      {picked === undefined ? (
        <Field label="Provider" help={needsPick ? "For an openai-compatible model, pick its connection above: the entry is named by it." : "For example azure-openai or azure-ai (keyless providers only)."}>
          {(id) => <Input id={id} className="min-h-11" value={typed} autoComplete="off" aria-invalid={needsPick} onChange={(e) => { setTyped(e.target.value); }} />}
        </Field>
      ) : (
        <p className="text-[13px]">
          The entry will be <code>{`${OPENAI_COMPATIBLE}/${picked.endpointId}/<model>`}</code>.{picked.status === "disabled" ? " This connection is disabled: the model cannot be called until it is enabled." : ""}
        </p>
      )}
      <Field label="Model" help={picked === undefined ? "The deployment name, for example gpt-5.4-mini." : "The model name the endpoint expects, exactly as it is sent in a request."}>
        {(id) => <Input id={id} className="min-h-11" value={model} autoComplete="off" onChange={(e) => { setModel(e.target.value); }} />}
      </Field>
      <Field label="Family" help="The model family (openai, meta...). A verifier must come from a different family than the worker it checks.">
        {(id) => <Input id={id} className="min-h-11" value={family} autoComplete="off" onChange={(e) => { setFamily(e.target.value); }} />}
      </Field>
      <Field label="Processing scope">{() => <Processing value={processing} onChange={setProcessing} />}</Field>
      {remote && <p className="text-xs text-muted-foreground">A hosted endpoint takes global processing only (synthetic data): where it processes data cannot be verified. The server refuses any other scope for it.</p>}
      <ProcessingHelp regions={catalog.customerRegions} />
    </ActionDialog>
  );
}

function EditDialog({ entry, catalog, done, close }: { entry: CatalogEntry; catalog: Catalog; done: (lines: string[]) => void; close: () => void }) {
  const api = useApi();
  const [family, setFamily] = useState(entry.family);
  const [processing, setProcessing] = useState(entry.processing);
  const widening = entry.status === "approved" && validProcessing(processing) && widens(entry.processing, processing, catalog.customerRegions);
  const changed = family.trim() !== entry.family || processing !== entry.processing;
  return (
    <ActionDialog
      title={`Edit ${entry.ref}`}
      description="Change the family or the processing scope. The provider and model name cannot change (add a new entry instead)."
      submit="Save"
      fieldsValid={changed && family.trim() !== "" && validProcessing(processing)}
      warning={
        widening ? (
          <p>After this change the entry accepts customer data it did not before, so it will be paused until an admin approves it again (data location, ND45).</p>
        ) : undefined
      }
      run={(reason) =>
        api.models.editEntry({
          ...entryRef(entry),
          ...(family.trim() !== entry.family ? { family: family.trim() } : {}),
          ...(processing !== entry.processing ? { processing } : {}),
          reason,
        })
      }
      onDone={(r) => {
        done(resultLines(r, `Edited ${entry.ref}`));
      }}
      onClose={close}
    >
      <Field label="Family">{(id) => <Input id={id} className="min-h-11" value={family} autoComplete="off" onChange={(e) => { setFamily(e.target.value); }} />}</Field>
      <Field label="Processing scope">{() => <Processing value={processing} onChange={setProcessing} />}</Field>
      <ProcessingHelp regions={catalog.customerRegions} />
    </ActionDialog>
  );
}

function PriceDialog({ entry, done, close }: { entry: CatalogEntry; done: (lines: string[]) => void; close: () => void }) {
  const api = useApi();
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const [cached, setCached] = useState("");
  const [from, setFrom] = useState("");
  const [source, setSource] = useState("");
  // a zero price is accepted only as a declared-free price of an active connection on the server's own machine
  const connections = useConnections(entry.endpointId !== undefined);
  const link = entry.endpointId === undefined ? undefined : connections.data?.find((c) => c.provider === entry.provider && c.endpointId === entry.endpointId);
  const canDeclareFree = link?.status === "active" && isLoopbackEndpoint(link.endpoint);
  const decimal = /^\d+(\.\d+)?$/;
  const valid = decimal.test(input) && decimal.test(output) && (cached === "" || decimal.test(cached)) && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(from) && source.trim().length >= 3;
  return (
    <ActionDialog
      title={`Add a price for ${entry.ref}`}
      description={`${entry.endpointId === undefined ? "" : `This price is for the model on the connection ${entry.provider} (${entry.endpointId}). `}Prices are added as new versions and never edited. The price in effect is the latest version whose start is not in the future. A price is never guessed.`}
      submit="Add price"
      fieldsValid={valid}
      run={(reason) =>
        api.models.setPrice({
          ...entryRef(entry),
          inputPerMillion: input,
          outputPerMillion: output,
          ...(cached === "" ? {} : { cachedInputPerMillion: cached }),
          effectiveFrom: from,
          source: source.trim(),
          reason,
        })
      }
      onDone={(r) => {
        done(resultLines(r, `Price set for ${entry.ref}`));
      }}
      onClose={close}
    >
      {canDeclareFree && (
        <div className="flex flex-col gap-1">
          <div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="min-h-11"
              onClick={() => {
                setInput("0");
                setOutput("0");
                setSource(FREE_SOURCE);
              }}
            >
              Declare free (local server)
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            This connection is a model server on the NuraMesh server&apos;s own machine. A price of zero is accepted only as your declaration, with the source &quot;{FREE_SOURCE}&quot;; it stops applying when the connection is repointed or disabled, and the price is then unknown again.
          </p>
        </div>
      )}
      <Field label="Input, per million tokens (USD)" help="Decimal text such as 0.75.">
        {(id) => <Input id={id} inputMode="decimal" className="min-h-11 font-mono" value={input} onChange={(e) => { setInput(e.target.value); }} />}
      </Field>
      <Field label="Output, per million tokens (USD)">
        {(id) => <Input id={id} inputMode="decimal" className="min-h-11 font-mono" value={output} onChange={(e) => { setOutput(e.target.value); }} />}
      </Field>
      <Field label="Cached input, per million tokens (optional)">
        {(id) => <Input id={id} inputMode="decimal" className="min-h-11 font-mono" value={cached} onChange={(e) => { setCached(e.target.value); }} />}
      </Field>
      <Field label="Effective from (UTC)" help="Like 2026-03-01T00:00:00Z. A version with the same start already in the book is refused.">
        {(id) => <Input id={id} className="min-h-11 font-mono" value={from} placeholder="2026-03-01T00:00:00Z" onChange={(e) => { setFrom(e.target.value); }} />}
      </Field>
      <Field label="Source" help="Where the price comes from (a price list or a contract).">
        {(id) => <Input id={id} className="min-h-11" value={source} onChange={(e) => { setSource(e.target.value); }} />}
      </Field>
    </ActionDialog>
  );
}

function RegionsDialog({ catalog, done, close }: { catalog: Catalog; done: (lines: string[]) => void; close: () => void }) {
  const api = useApi();
  const [text, setText] = useState(catalog.customerRegions.join(", "));
  const next = parseRegions(text);
  const widened = regionsWiden(catalog.entries, catalog.customerRegions, next);
  return (
    <ActionDialog
      title="Customer regions"
      description="The regions the customer has chosen for their data (ND45). A region:<id> model accepts customer data only when its region is listed here."
      submit="Save regions"
      fieldsValid={JSON.stringify(next) !== JSON.stringify(catalog.customerRegions)}
      warning={
        widened.length > 0 ? (
          <p>These approved models would accept customer data and will be paused until approved again: {widened.join(", ")}.</p>
        ) : undefined
      }
      run={(reason) => api.models.setRegions({ customerRegions: next, reason })}
      onDone={(r) => {
        done(resultLines(r, `Customer regions set to ${next.length === 0 ? "none" : next.join(", ")}`));
      }}
      onClose={close}
    >
      <Field label="Regions" help="Lower-case region ids separated by commas, for example eu-west, ca-east. Leave it empty for none.">
        {(id) => <Textarea id={id} rows={2} className="font-mono" value={text} onChange={(e) => { setText(e.target.value); }} />}
      </Field>
    </ActionDialog>
  );
}

function History({ close }: { close: () => void }) {
  const events = useEvents(true);
  return (
    <Dialog open onOpenChange={(open) => { if (!open) close(); }}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Catalog history</DialogTitle>
          <DialogDescription>Every honored catalog and assignment change from the ledger, newest first, with the reason a person gave. A reason shows as expired once the content store has erased it; the ledger entry stays.</DialogDescription>
        </DialogHeader>
        {events.isPending && <Loading what="the history" />}
        {events.isError && <ReadFailed what="the history" error={events.error} retry={() => void events.refetch()} />}
        {events.data && (
          <ol className="flex flex-col gap-2.5" aria-label="History">
            {events.data.pages.flatMap((page) => [...page.events].reverse()).map((e) => (
              <li key={e.seq} className="flex flex-col gap-0.5 rounded-lg border border-card-border px-3 py-2 text-sm">
                <span className="font-medium">{e.summary}</span>
                <span className="text-xs text-muted-foreground">
                  ledger #{e.seq} · {e.actor} · {formatStarted(e.at)}
                </span>
                <span className="text-[13px]">{e.reason === null ? <em className="text-muted-foreground">{e.type === "model.catalog_seeded" ? "from the team configuration, no reason" : "reason expired or not kept"}</em> : <>Reason: {e.reason}</>}</span>
              </li>
            ))}
          </ol>
        )}
        {events.hasNextPage && (
          <div>
            <Button variant="outline" className="min-h-11" disabled={events.isFetchingNextPage} onClick={() => void events.fetchNextPage()}>
              {events.isFetchingNextPage ? "Loading…" : "Load older"}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Entry({ entry, admin, act }: { entry: CatalogEntry; admin: boolean; act: (a: Action) => void }) {
  const real = entry.provider !== "fake";
  const btn = (label: string, kind: "approve" | "pause" | "retire" | "remove" | "edit" | "price", danger = false) => (
    <Button
      key={kind}
      variant="outline"
      size="sm"
      aria-label={`${label} ${entry.ref}`}
      className={`min-h-11 ${danger ? "border-danger text-danger hover:bg-danger-soft hover:text-danger" : ""}`}
      onClick={() => {
        act({ kind, entry });
      }}
    >
      {label}
    </Button>
  );
  return (
    <RowCard data-ref={entry.ref} id={entryAnchor(entry.ref)}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-mono text-[14px] font-semibold break-all">{entry.ref}</h3>
        <StatusChip status={entry.status} />
        {real && <span className="text-xs text-muted-foreground">{entry.connected ? "connected" : "no active connection"}</span>}
      </div>
      <p className="text-[13px] text-foreground/85">
        Family {entry.family} · processed in <code>{entry.processing}</code> · accepts {dataWords(entry.dataClasses)}
      </p>
      {entry.price === null ? (
        <p role="status" className="flex flex-wrap items-center gap-1.5 text-[13px] text-warm">
          <span aria-hidden="true">⚠</span>
          <span>{priceWords(entry.price)}</span>
          {admin && entry.status !== "retired" && <span className="text-muted-foreground">· Set a price: use Add price</span>}
        </p>
      ) : (
        <p className="text-[13px] text-foreground/85">{priceWords(entry.price)}</p>
      )}
      <p className="text-xs text-muted-foreground">
        {originWords(entry)}
        {entry.updatedAt !== null && entry.origin !== "seed" ? ` · ${formatStarted(entry.updatedAt)}` : ""}
      </p>
      {admin && (
        <div className="flex flex-wrap gap-2 pt-1">
          {entry.status === "paused" && btn("Approve", "approve")}
          {entry.status !== "retired" && btn("Edit", "edit")}
          {entry.status !== "retired" && btn("Add price", "price")}
          {entry.status === "approved" && btn("Pause", "pause")}
          {entry.status !== "retired" && btn("Retire", "retire", true)}
          {entry.status !== "approved" && btn("Remove", "remove", true)}
        </div>
      )}
    </RowCard>
  );
}

/** The approved catalog (spec 10.4): status chips, prices, data classes; an admin can add, edit, approve, pause, retire, remove, price and set regions. */
/** The anchor id of the first entry without a price (the header count links to it). */
function firstNoPrice(entries: readonly CatalogEntry[]): string {
  const first = entries.find((e) => e.price === null);
  return first === undefined ? "" : entryAnchor(first.ref);
}

function entryAnchor(ref: string): string {
  return `entry-${ref.replace(/[^A-Za-z0-9_-]+/g, "-")}`;
}

export function CatalogTab({ admin, notify }: TabProps) {
  const catalog = useCatalog();
  const assignments = useAssignments();
  const [action, setAction] = useState<Action | null>(null);

  if (catalog.isPending) return <Loading what="the catalog" />;
  if (catalog.isError) return <ReadFailed what="the model catalog" error={catalog.error} retry={() => void catalog.refetch()} />;
  const data = catalog.data;
  return (
    <div className="flex flex-col gap-4">
      {data.configDrift && (
        <p role="note" className="callout-warning px-4 py-3 text-sm">
          The models: section of the team configuration is ignored: the catalog in the ledger is in force (it was seeded from an earlier version of that section, or created through the admin API). Change the catalog here.
        </p>
      )}
      {data.ignoredEvents > 0 && (
        <p role="note" className="callout-warning px-4 py-3 text-sm">
          {data.ignoredEvents} catalog entries in the ledger are ignored (not written by a signed-in person, or not valid), so they change nothing.
        </p>
      )}
      {noPriceCount(data.entries) !== null && (
        <p role="status" className="callout-warning px-4 py-3 text-sm">
          <span aria-hidden="true">⚠ </span>
          <a href={`#${firstNoPrice(data.entries)}`} className="underline">
            {noPriceCount(data.entries)}
          </a>
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-0 flex-1 text-sm">
          <strong>Customer regions:</strong> {data.customerRegions.length === 0 ? "none chosen" : data.customerRegions.join(", ")}
        </p>
        {admin && (
          <>
            <Button variant="outline" className="min-h-11" onClick={() => { setAction({ kind: "regions" }); }}>
              Customer regions…
            </Button>
            <Button variant="outline" className="min-h-11" onClick={() => { setAction({ kind: "history" }); }}>
              History
            </Button>
            <Button className="min-h-11" onClick={() => { setAction({ kind: "add" }); }}>
              Add model
            </Button>
          </>
        )}
      </div>
      {data.entries.length === 0 ? (
        <div className="flex flex-col gap-1 rounded-lg border border-dashed p-6 text-sm">
          <strong>No catalog yet.</strong>
          <span className="text-muted-foreground">
            No real model can run until a catalog exists: every real model is refused. {admin ? "Add a model, give it a price, approve it, connect its provider and assign it." : "An admin adds and approves models here, or the team configuration seeds a catalog (models:) before the first start."} Runs on the fake model are not affected.
          </span>
        </div>
      ) : (
        <ul aria-label="Approved model catalog" className="flex flex-col gap-3">
          {data.entries.map((entry) => (
            <Entry key={entry.ref} entry={entry} admin={admin} act={setAction} />
          ))}
        </ul>
      )}
      {action !== null && (
        <CatalogDialogs
          action={action}
          catalog={data}
          assignments={assignments.data ?? []}
          notify={notify}
          close={() => {
            setAction(null);
          }}
        />
      )}
    </div>
  );
}
