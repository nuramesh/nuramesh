import { useQuery } from "@tanstack/react-query";
import { Check, ChevronDown, Download, FileText, MoreHorizontal, Play, Upload } from "lucide-react";
import { use, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { ApiContext } from "@/api/context";
import { usd } from "@/api/money";
import { keys } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

import { exportYaml } from "../config-yaml";
import { blankDocument, readDocumentFile, serializeDocument } from "../document";
import { useBuilder, useBuilderStore } from "../store-context";
import { BUILT_IN_TEMPLATES } from "../templates";
import { ImportErrorsDialog, ManageTemplatesDialog, SaveTemplateDialog } from "./template-dialogs";

function download(fileName: string, text: string, type: string): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/**
 * The builder's header (SPEC-app-v1 3.1): breadcrumb, the config file and Git ref, whether the
 * organization is valid, and the actions: templates, import, Export YAML, Save as template and
 * Start a run. The YAML is generated from the document, so it is always in sync with the canvas.
 */
export function BuilderHeader() {
  const store = useBuilderStore();
  const api = use(ApiContext);
  const navigate = useNavigate();
  const orgName = useBuilder((s) => s.doc.org.name);
  const orgId = useBuilder((s) => s.doc.org.id);
  const errors = useBuilder((s) => s.issues.errorCount);
  const warnings = useBuilder((s) => s.issues.warningCount);
  const userTemplates = useBuilder((s) => s.userTemplates);
  const snapshot = useQuery({ queryKey: keys.org, queryFn: () => api?.orgs.get() ?? Promise.reject(new Error("no api")), enabled: api !== null });
  const fileInput = useRef<HTMLInputElement>(null);
  const [saveOpen, setSaveOpen] = useState(false);
  const [manageOpen, setManageOpen] = useState(false);
  const [starting, setStarting] = useState(false);
  const [importProblem, setImportProblem] = useState<{ fileName: string; errors: string[] }>({ fileName: "", errors: [] });
  const workspace = snapshot.data?.org.name ?? "Workspace";
  const ref = snapshot.data?.gitRef;

  const startRun = (): void => {
    if (api === null || errors > 0 || starting) return;
    setStarting(true);
    api.runs.start({ template: orgName, budget: usd("10.00") }).then(
      (started) => {
        store.getState().announce(`Started ${started.result.title}`);
        void navigate(`/runs/${started.result.id}`);
      },
      (error: unknown) => {
        setStarting(false);
        store.getState().announce(`Couldn't start the run: ${error instanceof Error ? error.message : String(error)}`);
      },
    );
  };

  const exportJson = (): void => {
    const { doc } = store.getState();
    download(`${doc.org.id}.nuramesh.json`, serializeDocument(doc), "application/json");
    store.getState().announce("Exported the organization as JSON");
  };

  return (
    <div role="group" aria-label="Organization" className="flex min-w-0 max-w-full flex-wrap items-center gap-x-3 gap-y-2 border-b bg-panel px-3 py-2">
      <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
        <span className="truncate">{workspace}</span>
        <span aria-hidden="true" className="text-small">
          /
        </span>
        <h1 className="max-w-64 truncate font-semibold text-foreground" title={orgName}>
          {orgName}
        </h1>
      </nav>
      <span
        data-testid="config-chip"
        className="inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-md border px-2 py-0.5 font-mono text-xs text-muted-foreground"
      >
        <FileText aria-hidden="true" className="size-3 shrink-0" />
        <span className="min-w-0 truncate">
          {orgId}.yaml{ref === undefined ? "" : ` · ${ref}`}
        </span>
      </span>
      <button
        type="button"
        data-testid="validity-chip"
        onClick={() => {
          if (errors > 0 || warnings > 0) store.getState().setPanel("issues");
        }}
        aria-label={
          errors > 0
            ? `${String(errors)} problem${errors === 1 ? "" : "s"}: show them`
            : warnings > 0
              ? `Valid, ${String(warnings)} warning${warnings === 1 ? "" : "s"}: show them`
              : "Valid, in sync with YAML"
        }
        className={cn("chip text-xs", errors > 0 ? "chip-danger" : warnings > 0 ? "chip-warning" : "chip-success")}
      >
        {errors === 0 && <Check aria-hidden="true" className="size-3.5" />}
        {errors > 0
          ? `${String(errors)} problem${errors === 1 ? "" : "s"}`
          : warnings > 0
            ? `Valid · ${String(warnings)} warning${warnings === 1 ? "" : "s"}`
            : "Valid · in sync with YAML"}
      </button>

      <div className="ml-auto flex max-w-full min-w-0 flex-wrap items-center justify-end gap-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm">
              Templates <ChevronDown aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-64">
            <DropdownMenuLabel>Start from</DropdownMenuLabel>
            <DropdownMenuItem
              onSelect={() => {
                store.getState().load("Started a blank organization", blankDocument());
              }}
            >
              Blank organization
            </DropdownMenuItem>
            {BUILT_IN_TEMPLATES.map((t) => (
              <DropdownMenuItem
                key={t.id}
                onSelect={() => {
                  store.getState().load(`Loaded template ${t.name}`, t.document);
                }}
              >
                {t.name}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuLabel>Saved templates</DropdownMenuLabel>
            {userTemplates.length === 0 ? (
              <DropdownMenuItem disabled>None yet</DropdownMenuItem>
            ) : (
              userTemplates.map((t) => (
                <DropdownMenuItem
                  key={t.id}
                  onSelect={() => {
                    store.getState().load(`Loaded template ${t.name}`, t.document);
                  }}
                >
                  {t.name}
                </DropdownMenuItem>
              ))
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={() => {
                setManageOpen(true);
              }}
            >
              Manage saved templates…
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <Button
          variant="outline"
          size="sm"
          className="hidden xl:inline-flex"
          onClick={() => {
            fileInput.current?.click();
          }}
        >
          <Upload aria-hidden="true" /> Import
        </Button>
        <input
          ref={fileInput}
          type="file"
          accept=".json,application/json"
          className="sr-only"
          tabIndex={-1}
          aria-label="Import a JSON file"
          data-testid="import-file"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file === undefined) return;
            void readDocumentFile(file).then((result) => {
              if (result.ok) store.getState().load(`Imported ${file.name}`, result.value);
              else setImportProblem({ fileName: file.name, errors: result.errors });
            });
          }}
        />
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            const { doc } = store.getState();
            download(`${doc.org.id}.yaml`, exportYaml(doc), "application/yaml");
            store.getState().announce("Exported the organization as YAML");
          }}
        >
          <Download aria-hidden="true" /> Export YAML
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="hidden xl:inline-flex"
          title="The builder's own format: keeps people, appearance and positions"
          onClick={exportJson}
        >
          Export JSON
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="icon-sm" className="xl:hidden" aria-label="More actions" title="More actions">
              <MoreHorizontal aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem
              onSelect={() => {
                fileInput.current?.click();
              }}
            >
              Import a JSON file
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={exportJson}>Export JSON</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setSaveOpen(true);
          }}
        >
          Save as template
        </Button>
        {/* The one primary action: filled indigo (brand token); disabled keeps the style, dimmed. */}
        <Button
          size="sm"
          className="bg-brand text-primary-foreground hover:bg-brand/90 disabled:opacity-60"
          disabled={api === null || errors > 0 || starting}
          title={errors > 0 ? "Fix the problems first" : undefined}
          onClick={startRun}
        >
          <Play aria-hidden="true" /> Start
        </Button>
      </div>

      <SaveTemplateDialog open={saveOpen} onOpenChange={setSaveOpen} />
      <ManageTemplatesDialog open={manageOpen} onOpenChange={setManageOpen} />
      <ImportErrorsDialog
        fileName={importProblem.fileName}
        errors={importProblem.errors}
        onClose={() => {
          setImportProblem({ fileName: "", errors: [] });
        }}
      />
    </div>
  );
}
