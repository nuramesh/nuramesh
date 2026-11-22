import { useState } from "react";

import { sourcePinned } from "@/api";
import { useApi } from "@/api/context";
import { describeProblem } from "@/api/problems";
import { createServerClient } from "@/api/server-client";
import {
  crossOriginNote,
  type DataSource,
  normalizeServerUrl,
  saveServerUrl,
  saveSource,
  storedServerUrl,
  storedSource,
} from "@/api/settings";
import { ChangeEmailPanel } from "@/account/change-email-panel";
import { useAuthHeld, useAuthState } from "@/auth/use-session";
import { SecurityPanel } from "@/mfa/security-panel";
import { ShowChecklist } from "@/first-run/show-checklist-link";
import { PeoplePanel } from "@/invites/people-panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type Test = { state: "idle" } | { state: "testing" } | { state: "ok"; text: string } | { state: "failed"; text: string };

/** Where the app gets its data: sample data, or a running `nuramesh serve`. Nothing secret is kept here. */
export function SettingsPage() {
  const api = useApi();
  const pinned = sourcePinned();
  const [source, setSource] = useState<DataSource>(import.meta.env.DEV ? (storedSource() ?? api.source) : "server");
  const [url, setUrl] = useState(storedServerUrl());
  const [test, setTest] = useState<Test>({ state: "idle" });
  const [saved, setSaved] = useState(false);
  const auth = useAuthState();
  const held = useAuthHeld();
  const note = crossOriginNote(url);
  const invalid = url.trim() !== "" && normalizeServerUrl(url) === "";

  const check = async (): Promise<void> => {
    setTest({ state: "testing" });
    try {
      const info = await createServerClient({ baseUrl: normalizeServerUrl(url) }).info();
      setTest({ state: "ok", text: `Connected to ${info.name} (${info.detail}).` });
    } catch (error) {
      setTest({ state: "failed", text: describeProblem(error) });
    }
  };

  const save = (): void => {
    saveSource(source);
    saveServerUrl(url);
    setSaved(true);
    globalThis.location.reload();
  };

  return (
    <section aria-labelledby="page-title" className="flex max-w-2xl flex-col gap-6 p-6">
      <title>Settings · NuraMesh</title>
      <header className="flex flex-col gap-1">
        <h1 id="page-title" className="text-2xl font-semibold">
          Settings
        </h1>
        <p className="text-sm text-muted-foreground">
          Where this app gets its data. Only these two choices are kept, in this browser. No password, token or key is ever stored here.
        </p>
      </header>

      <p role="status" data-testid="current-source" className="rounded-lg border bg-panel px-4 py-3 text-sm">
        Showing now: <strong>{api.source === "fixtures" ? "sample data" : "a NuraMesh server"}</strong>
        {pinned ? " (this build fixes the choice, so the options below do not apply)" : ""}.
      </p>

      <fieldset disabled={pinned} className="flex flex-col gap-3 border-0 p-0">
        <legend className="mb-1 text-sm font-semibold">Data source</legend>
        {(
          [
            ["fixtures", "Sample data", "Made-up runs for trying the app offline. Nothing here is real."],
            ["server", "NuraMesh server", "Real runs, budgets and ledger from nuramesh serve."],
          ] as const
        ).filter(([value]) => value !== "fixtures" || import.meta.env.DEV).map(([value, label, help]) => (
          <div key={value} className="flex min-h-11 items-start gap-3 rounded-lg border bg-panel px-4 py-3">
            <input
              id={`source-${value}`}
              type="radio"
              name="source"
              value={value}
              checked={source === value}
              aria-describedby={`source-${value}-help`}
              onChange={() => {
                setSource(value);
                setSaved(false);
              }}
              className="mt-1 size-4"
            />
            <div className="flex flex-col">
              <label htmlFor={`source-${value}`} className="cursor-pointer text-sm font-medium">
                {label}
              </label>
              <span id={`source-${value}-help`} className="text-[13px] text-muted-foreground">
                {help}
              </span>
            </div>
          </div>
        ))}
      </fieldset>

      <div className="flex flex-col gap-2">
        <label htmlFor="server-url" className="text-sm font-semibold">
          Server address <span className="font-normal text-muted-foreground">(optional)</span>
        </label>
        <Input
          id="server-url"
          className="min-h-11 font-mono"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          value={url}
          placeholder="empty = this site, forwarded to nuramesh serve"
          aria-invalid={invalid ? true : undefined}
          aria-describedby="server-url-help"
          onChange={(e) => {
            setUrl(e.target.value);
            setTest({ state: "idle" });
            setSaved(false);
          }}
        />
        <p id="server-url-help" className="text-[13px] text-muted-foreground">
          Leave it empty for local use: the dev server forwards <code>/v1</code> to <code>http://127.0.0.1:8765</code> (set <code>NURAMESH_API_URL</code> to change that). The server only talks to its own loopback address.
        </p>
        {invalid && (
          <p role="alert" className="text-xs text-destructive">
            Use an address such as http://127.0.0.1:8765
          </p>
        )}
        {note !== null && !invalid && <p className="callout-warning px-3 py-2 text-[13px]">{note}</p>}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="outline"
          className="min-h-11"
          disabled={invalid || test.state === "testing"}
          onClick={() => {
            void check();
          }}
        >
          {test.state === "testing" ? "Testing…" : "Test connection"}
        </Button>
        <Button className="min-h-11" disabled={invalid || pinned} onClick={save}>
          Save and reload
        </Button>
        {saved && <span className="text-sm text-muted-foreground">Saved.</span>}
      </div>
      <p role="status" aria-live="polite" data-testid="connection-test" className={test.state === "failed" ? "text-sm text-destructive" : "text-sm text-muted-foreground"}>
        {test.state === "ok" || test.state === "failed" ? test.text : ""}
      </p>
      <ShowChecklist />
      {auth.kind === "signed-in" && <ChangeEmailPanel />}
      {(auth.kind === "signed-in" || held) && <SecurityPanel />}
      {auth.kind === "signed-in" && auth.user.role === "owner" && <PeoplePanel />}
    </section>
  );
}
