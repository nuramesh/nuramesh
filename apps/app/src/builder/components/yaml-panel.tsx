import { useMemo, useState } from "react";

import { Textarea } from "@/components/ui/textarea";

import { agentSnippet, applyAgentSnippet } from "../config-yaml";
import { useBuilder, useBuilderStore } from "../store-context";

/**
 * The selected agent's configuration as YAML, edited in place (two-way sync, A.6). A change that
 * parses and validates is applied to the document at once (one undo step per burst of typing); a
 * change that doesn't is kept as text with the reasons beside it, and the document stays as it
 * was. When the document changes some other way (the canvas, undo), the text follows it.
 */
export function YamlPanel({ agentId, agentName }: { agentId: string; agentName: string }) {
  const store = useBuilderStore();
  const doc = useBuilder((s) => s.doc);
  const snippet = useMemo(() => agentSnippet(doc, agentId), [doc, agentId]);
  const [draft, setDraft] = useState(snippet);
  const [base, setBase] = useState(snippet);
  const [errors, setErrors] = useState<string[]>([]);
  const [lastApplied, setLastApplied] = useState<string | null>(null);
  if (snippet !== base) {
    setBase(snippet);
    // Our own edit coming back normalised: keep what the user typed. Anything else: follow the document.
    if (snippet !== lastApplied) {
      setDraft(snippet);
      setErrors([]);
    }
  }
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted-foreground">
        Every canvas edit is a valid config change, and this is the same data as YAML. Edit it here and the canvas follows.
      </p>
      <label htmlFor="agent-yaml" className="sr-only">
        YAML for {agentName}
      </label>
      <Textarea
        id="agent-yaml"
        value={draft}
        rows={18}
        spellCheck={false}
        autoComplete="off"
        aria-invalid={errors.length > 0 ? true : undefined}
        aria-describedby={errors.length > 0 ? "agent-yaml-errors" : undefined}
        className="font-mono text-xs leading-relaxed whitespace-pre"
        onChange={(event) => {
          const text = event.target.value;
          setDraft(text);
          const current = store.getState().doc;
          const result = applyAgentSnippet(current, agentId, text);
          if (!result.ok) {
            setErrors(result.errors);
            return;
          }
          setErrors([]);
          // An edit that leaves the configuration as it was (a comment, spacing) isn't an undo step.
          if (result.doc !== current && agentSnippet(result.doc, agentId) !== agentSnippet(current, agentId)) {
            setLastApplied(agentSnippet(result.doc, agentId));
            store.getState().edit("Edit YAML", () => result.doc, `yaml:${agentId}`);
          }
        }}
      />
      {errors.length > 0 ? (
        <ul id="agent-yaml-errors" role="alert" className="space-y-0.5 text-xs text-destructive">
          {errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      ) : (
        <p role="status" className="text-xs text-link">
          In sync with the canvas.
        </p>
      )}
    </div>
  );
}
