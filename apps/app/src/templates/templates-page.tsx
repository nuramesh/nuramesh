/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- scrollable regions must be focusable */
import { useQuery } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import { NotAvailableError, NotFoundError } from "@/api/client";
import { useApi } from "@/api/context";
import type { TemplateSummary, TemplateVersionInfo } from "@/api/types";
import { blankDocument, type BuilderDocument, SCHEMA_VERSION } from "@/builder/document";
import { exportYaml, importOrgYaml, MAX_YAML_BYTES } from "@/builder/config-yaml";
import { useStorageScope } from "@/auth/storage-scope";
import { loadUserTemplates } from "@/builder/storage";
import { BUILT_IN_TEMPLATES } from "@/builder/templates";
import { emptyConfig, emptyUi } from "@/model/config";
import type { Org } from "@/model/org";
import { Button } from "@/components/ui/button";
import { Chip, FilterPill } from "@/components/ui/chip";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

type Filter = "all" | "available" | "planned";
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "available", label: "Available now" },
  { id: "planned", label: "Planned" },
];

const LOCAL_CATALOG: TemplateSummary[] = [...BUILT_IN_TEMPLATES, { id: "blank", name: "Blank organization", description: "An empty canvas for your own roles.", document: blankDocument() }].map((t) => ({
  id: t.id,
  name: t.name,
  description: t.description,
  availability: "available",
  origin: "built-in",
  roles: t.document.org.roles.map((r) => r.name),
  meta: `${String(t.document.org.roles.length)} roles`,
  latestVersion: null,
  updatedAt: null,
}));

const retry = (count: number, error: unknown): boolean => !(error instanceof NotAvailableError || error instanceof NotFoundError) && count < 2;

function documentFromOrg(org: Org): BuilderDocument {
  return { schema_version: SCHEMA_VERSION, org, config: emptyConfig(), ui: emptyUi(), layout: { positions: {} } };
}

/** The builder's richer built-in document for a built-in id (models, gates, permissions), else null. */
function builtInDocument(id: string): BuilderDocument | null {
  if (id === "blank") return blankDocument();
  return BUILT_IN_TEMPLATES.find((t) => t.id === id)?.document ?? null;
}

function initials(name: string): string {
  const words = name.split(/\s+/).filter(Boolean);
  return (words.length > 1 ? words.slice(0, 2).map((w) => w[0] ?? "").join("") : name.slice(0, 2)).toUpperCase();
}

function Card({ t, onUse, onPreview, extra }: { t: TemplateSummary; onUse: () => void; onPreview: () => void; extra?: React.ReactNode }) {
  const planned = t.availability === "planned";
  return (
    <article className={cn("flex flex-col gap-3.5 rounded-2xl border p-4.5", planned ? "border-dashed bg-transparent" : "border-card-border bg-panel shadow-card")}>
      <div className="flex items-center gap-2.5">
        <span aria-hidden="true" className="flex size-[38px] shrink-0 items-center justify-center rounded-[10px] bg-secondary font-mono text-[11px] font-semibold text-secondary-foreground">
          {initials(t.name)}
        </span>
        <div className="flex min-w-0 flex-1 flex-col">
          <h3 className="text-base font-semibold">{t.name}</h3>
          <span className="text-[12.5px] text-small">{t.meta}</span>
        </div>
        <Chip tone={planned ? "neutral" : t.origin === "mine" ? "warning" : "info"}>
          {planned ? "planned" : t.origin === "mine" ? "mine" : "available"}
        </Chip>
      </div>
      <p className="text-[13.5px] text-muted-foreground">{t.description}</p>
      <ul aria-label="Roles" className="m-0 flex list-none flex-wrap gap-1.5 p-0">
        {t.roles.map((r) => (
          <li key={r} className="rounded-full border px-2.5 py-0.5 text-[12.5px] text-muted-foreground">
            {r}
          </li>
        ))}
      </ul>
      {extra}
      <div className="flex-1" />
      {planned ? (
        <p className="text-[12.5px] text-muted-foreground">Planned for a later release.</p>
      ) : (
        <div className="flex flex-wrap gap-2.5">
          <Button className="min-h-11" onClick={onUse}>
            Use template
          </Button>
          <Button variant="outline" className="min-h-11" onClick={onPreview}>
            Preview YAML
          </Button>
        </div>
      )}
    </article>
  );
}

/** Templates (SPEC-app-v1 3.6): built-in and planned teams, My templates with versions, Import YAML. */
export function TemplatesPage() {
  const api = useApi();
  const navigate = useNavigate();
  const [filter, setFilter] = useState<Filter>("all");
  const [preview, setPreview] = useState<{ title: string; text: string } | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const list = useQuery({ queryKey: ["templates"], queryFn: () => api.templates.list(), retry });
  const scope = useStorageScope();
  const browserSaved = useMemo(() => loadUserTemplates(scope), [scope]);

  const open = (document: BuilderDocument, label: string): void => {
    void navigate("/builder", { state: { handoff: { document, label } satisfies { document: unknown; label: string } } });
  };
  const failed = (what: string, error: unknown): void => {
    setNotice(`Couldn't ${what}: ${error instanceof Error ? error.message : String(error)}`);
  };

  const openBuiltIn = (t: TemplateSummary): void => {
    const doc = builtInDocument(t.id);
    if (doc !== null) {
      open(doc, `Loaded template ${t.name}`);
      return;
    }
    api.templates.use(t.id).then((org) => {
      open(documentFromOrg(org), `Loaded template ${t.name}`);
    }, (e: unknown) => {
      failed("load the template", e);
    });
  };
  const previewBuiltIn = (t: TemplateSummary): void => {
    const doc = builtInDocument(t.id);
    if (doc !== null) setPreview({ title: `${t.name} (YAML)`, text: exportYaml(doc) });
    else api.templates.use(t.id).then((org) => { setPreview({ title: `${t.name} (YAML)`, text: exportYaml(documentFromOrg(org)) }); }, (e: unknown) => { failed("preview the template", e); });
  };

  // Built-in and browser-saved templates are local; only the planned ones and the saved-on-server
  // ones need the API, so a source that can't list templates still shows what the app has.
  const all = list.data ?? (list.isError ? LOCAL_CATALOG : []);
  // The Planned filter is only honest when something is planned; a real server's catalog has none.
  const hasPlanned = all.some((t) => t.origin === "built-in" && t.availability === "planned");
  const filters = FILTERS.filter((f) => f.id !== "planned" || hasPlanned);
  const activeFilter: Filter = filter === "planned" && !hasPlanned ? "all" : filter;
  const catalog = all.filter((t) => t.origin === "built-in" && (activeFilter === "all" || t.availability === activeFilter));
  const mine = all.filter((t) => t.origin === "mine");
  const showMine = activeFilter !== "planned";

  return (
    <div className="flex flex-col">
      <title>Templates · NuraMesh</title>
      <header className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5 border-b bg-panel px-6 py-4">
        <div className="flex flex-col gap-0.5">
          <h1 className="text-[21px] font-semibold">Templates</h1>
          <p className="text-[13px] text-muted-foreground">Start from a team with roles, models, gates, policies and budgets already set. Every template is plain YAML you can edit and version.</p>
        </div>
        <span className="flex-1" />
        <Button
          variant="outline"
          className="min-h-11"
          onClick={() => {
            setImportOpen(true);
          }}
        >
          Import YAML
        </Button>
      </header>
      <div className="flex flex-col gap-5 px-6 pt-5 pb-7">
        <p role="status" aria-live="polite" className={notice === "" ? "sr-only" : "rounded-xl border px-4 py-3 text-sm"}>
          {notice}
        </p>
        <div role="group" aria-label="Filter templates" className="flex flex-wrap gap-2">
          {filters.map((f) => (
            <FilterPill
              key={f.id}
              pressed={activeFilter === f.id}
              onClick={() => {
                setFilter(f.id);
              }}
              className="px-4 text-[13.5px]"
            >
              {f.label}
            </FilterPill>
          ))}
        </div>
        {list.isError && (
          <p role="alert" className="rounded-xl border border-dashed p-4 text-sm">
            {list.error instanceof NotAvailableError ? "This data source can't list templates yet, so only the built-in ones and the ones saved in this browser are shown." : "Couldn't load the templates from the server; showing the built-in ones."}{" "}
            <button type="button" className="underline" onClick={() => void list.refetch()}>
              Try again
            </button>
          </p>
        )}
        {list.data === undefined && !list.isError ? (
          <p role="status" className="text-sm text-muted-foreground">
            Loading templates…
          </p>
        ) : (
          <>
            <section aria-label="Templates" className="grid grid-cols-[repeat(auto-fill,minmax(18.5rem,1fr))] gap-4">
              {catalog.map((t) => (
                <Card
                  key={t.id}
                  t={t}
                  onUse={() => {
                    openBuiltIn(t);
                  }}
                  onPreview={() => {
                    previewBuiltIn(t);
                  }}
                />
              ))}
            </section>
            {showMine && (
              <section aria-labelledby="mine-title" className="flex flex-col gap-3">
                <h2 id="mine-title" className="text-base font-semibold">
                  My templates
                </h2>
                {mine.length === 0 && browserSaved.length === 0 ? (
                  <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
                    Nothing saved yet. In the org builder, choose <Link to="/builder" className="text-link underline">Save as template</Link> to keep an organization here.
                  </p>
                ) : (
                  <div className="grid grid-cols-[repeat(auto-fill,minmax(18.5rem,1fr))] gap-4">
                    {mine.map((t) => (
                      <MineCard key={t.id} t={t} onOpen={open} onPreview={setPreview} onError={failed} />
                    ))}
                    {browserSaved.map((t) => (
                      <article key={t.id} className="flex flex-col gap-3 rounded-2xl border border-card-border bg-panel p-4.5 shadow-card">
                        <div className="flex items-center gap-2.5">
                          <h3 className="min-w-0 flex-1 truncate text-base font-semibold">{t.name}</h3>
                          <Chip tone="warning">this browser</Chip>
                        </div>
                        <p className="text-[13.5px] text-muted-foreground">{t.description === "" ? "Saved from the org builder in this browser." : t.description}</p>
                        <div className="flex flex-wrap gap-2.5">
                          <Button
                            className="min-h-11"
                            onClick={() => {
                              open(t.document, `Loaded template ${t.name}`);
                            }}
                          >
                            Use template
                          </Button>
                          <Button
                            variant="outline"
                            className="min-h-11"
                            onClick={() => {
                              setPreview({ title: `${t.name} (YAML)`, text: exportYaml(t.document) });
                            }}
                          >
                            Preview YAML
                          </Button>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            )}
          </>
        )}
      </div>

      <Dialog
        open={preview !== null}
        onOpenChange={(o) => {
          if (!o) setPreview(null);
        }}
      >
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{preview?.title}</DialogTitle>
            <DialogDescription>Plain YAML, as `nuramesh try` reads it. Read-only here; edit it in Git.</DialogDescription>
          </DialogHeader>
          {/* a scrollable region must be focusable so a keyboard can scroll it */}
          <pre tabIndex={0} role="region" aria-label="YAML" className="max-h-[60vh] overflow-auto rounded-lg border bg-panel-2 p-3 font-mono text-xs leading-relaxed whitespace-pre">
            {preview?.text}
          </pre>
          <DialogFooter>
            <Button
              variant="outline"
              className="min-h-11"
              onClick={() => {
                setPreview(null);
              }}
            >
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ImportDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        onImport={(doc) => {
          setImportOpen(false);
          open(doc, "Imported YAML");
        }}
      />
    </div>
  );
}

function MineCard({
  t,
  onOpen,
  onPreview,
  onError,
}: {
  t: TemplateSummary;
  onOpen: (doc: BuilderDocument, label: string) => void;
  onPreview: (p: { title: string; text: string }) => void;
  onError: (what: string, error: unknown) => void;
}) {
  const api = useApi();
  const [showVersions, setShowVersions] = useState(false);
  const versions = useQuery({ queryKey: ["templates", t.id, "versions"], queryFn: () => api.templates.listVersions(t.id), enabled: showVersions, retry });
  const openVersion = (v?: number): void => {
    api.templates.use(t.id, v).then((org) => {
      onOpen(documentFromOrg(org), `Loaded template ${t.name}${v === undefined ? "" : ` v${String(v)}`}`);
    }, (e: unknown) => {
      onError("load the template", e);
    });
  };
  const previewVersion = (v: number): void => {
    api.templates.versionYaml(t.id, v).then((text) => {
      onPreview({ title: `${t.name} v${String(v)} (YAML)`, text });
    }, (e: unknown) => {
      onError("preview the version", e);
    });
  };
  return (
    <Card
      t={t}
      onUse={() => {
        openVersion();
      }}
      onPreview={() => {
        if (t.latestVersion !== null) previewVersion(t.latestVersion);
      }}
      extra={
        <div className="flex flex-col gap-2">
          <p className="font-mono text-xs text-small">
            {t.latestVersion === null ? "" : `v${String(t.latestVersion)}`}
            {t.updatedAt === null ? "" : ` · updated ${new Date(t.updatedAt).toLocaleDateString("en-GB", { day: "numeric", month: "short" })}`}
          </p>
          <button
            type="button"
            aria-expanded={showVersions}
            className="min-h-11 self-start text-[13px] text-link underline"
            onClick={() => {
              setShowVersions((s) => !s);
            }}
          >
            {showVersions ? "Hide versions" : "Show versions"}
          </button>
          {showVersions && (
            <ul aria-label={`Versions of ${t.name}`} className="m-0 flex list-none flex-col gap-1.5 p-0">
              {(versions.data ?? []).map((v: TemplateVersionInfo) => (
                <li key={v.version} className="flex flex-wrap items-center gap-2 text-[13px]">
                  <span className="font-mono">v{v.version}</span>
                  <span className="text-small">{new Date(v.savedAt).toLocaleDateString("en-GB", { day: "numeric", month: "short" })}</span>
                  <Button variant="outline" size="sm" className="min-h-11" onClick={() => { openVersion(v.version); }}>
                    Use v{v.version}
                  </Button>
                  <Button variant="ghost" size="sm" className="min-h-11" onClick={() => { previewVersion(v.version); }}>
                    Preview v{v.version}
                  </Button>
                </li>
              ))}
              {versions.isLoading && <li className="text-sm text-muted-foreground">Loading versions…</li>}
              {versions.isError && <li role="alert" className="text-sm text-destructive">Couldn't load the versions.</li>}
            </ul>
          )}
        </div>
      }
    />
  );
}

function ImportDialog({ open, onOpenChange, onImport }: { open: boolean; onOpenChange: (o: boolean) => void; onImport: (doc: BuilderDocument) => void }) {
  const [text, setText] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const file = useRef<HTMLInputElement>(null);
  const submit = (): void => {
    const result = importOrgYaml(text);
    if (result.ok) {
      setErrors([]);
      setText("");
      onImport(result.doc);
    } else setErrors(result.errors);
  };
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) {
          setErrors([]);
          setText("");
        }
        onOpenChange(o);
      }}
    >
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Import YAML</DialogTitle>
          <DialogDescription>Paste a nuramesh.yaml or choose a file. It opens in the org builder, where you can review it before anything is saved.</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-2">
          <label htmlFor="import-yaml" className="text-sm font-medium">
            YAML
          </label>
          <Textarea
            id="import-yaml"
            rows={12}
            spellCheck={false}
            className="font-mono text-xs"
            value={text}
            aria-invalid={errors.length > 0 ? true : undefined}
            aria-describedby="import-errors"
            onChange={(e) => {
              setText(e.target.value);
              setErrors([]);
            }}
          />
          <input
            ref={file}
            type="file"
            accept=".yaml,.yml,text/yaml"
            className="sr-only"
            tabIndex={-1}
            aria-label="Choose a YAML file"
            data-testid="import-yaml-file"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f === undefined) return;
              if (f.size > MAX_YAML_BYTES) {
                setErrors(["The file is larger than 10 MB"]);
                return;
              }
              f.text().then(
                (t) => {
                  setText(t);
                  setErrors([]);
                },
                () => {
                  setErrors(["Couldn't read the file"]);
                },
              );
            }}
          />
          <ul id="import-errors" role="alert" className="m-0 list-none space-y-0.5 p-0 text-xs text-destructive">
            {errors.map((e, i) => (
              <li key={`${String(i)}:${e}`}>{e}</li>
            ))}
          </ul>
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            className="min-h-11"
            onClick={() => {
              file.current?.click();
            }}
          >
            Choose a file
          </Button>
          <Button className="min-h-11" disabled={text.trim() === ""} onClick={submit}>
            Open in the builder
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
