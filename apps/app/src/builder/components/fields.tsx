import { type ReactNode, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

interface FieldProps {
  id: string;
  label: string;
  error?: string | null;
  hint?: string;
  children: ReactNode;
}

/** A labelled form row with an inline error (announced through aria-describedby). */
export function Field({ id, label, error, hint, children }: FieldProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint !== undefined && (error === null || error === undefined) && (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
      {error !== null && error !== undefined && (
        <p id={`${id}-error`} className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

function describedBy(id: string, error: string | null | undefined, hint: string | undefined): string | undefined {
  if (error !== null && error !== undefined) return `${id}-error`;
  return hint === undefined ? undefined : `${id}-hint`;
}

interface TextFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string | null;
  hint?: string;
  multiline?: boolean;
  placeholder?: string;
  /** The id of a `<datalist>` of suggestions. */
  list?: string;
}

/** Controlled text input bound to the document; every keystroke is an (undo-merged) edit. */
export function TextField({ id, label, value, onChange, error, hint, multiline = false, placeholder, list }: TextFieldProps) {
  const common = {
    id,
    value,
    placeholder,
    "aria-invalid": error !== null && error !== undefined ? true : undefined,
    "aria-describedby": describedBy(id, error, hint),
  };
  return (
    <Field id={id} label={label} error={error} {...(hint !== undefined ? { hint } : {})}>
      {multiline ? (
        <Textarea
          {...common}
          rows={4}
          onChange={(e) => {
            onChange(e.target.value);
          }}
        />
      ) : (
        <Input
          {...common}
          {...(list !== undefined ? { list } : {})}
          onChange={(e) => {
            onChange(e.target.value);
          }}
        />
      )}
    </Field>
  );
}

interface DraftFieldProps {
  id: string;
  label: string;
  /** The value in the document. */
  value: string;
  /** Returns an error for a draft, or null when it may be committed. */
  check: (draft: string) => string | null;
  /** Called with a valid draft. */
  onCommit: (draft: string) => void;
  hint?: string;
}

/**
 * A field whose draft may be invalid without touching the document (ids: an invalid or taken id
 * would break references). Valid drafts are committed as you type; invalid ones show the error.
 */
export function DraftField({ id, label, value, check, onCommit, hint }: DraftFieldProps) {
  const [draft, setDraft] = useState(value);
  const [source, setSource] = useState(value);
  // Follow the document when it changes from elsewhere (undo, another agent selected).
  if (source !== value) {
    setSource(value);
    setDraft(value);
  }
  const error = draft === value ? null : check(draft);
  return (
    <Field id={id} label={label} error={error} {...(hint !== undefined ? { hint } : {})}>
      <Input
        id={id}
        value={draft}
        spellCheck={false}
        autoComplete="off"
        aria-invalid={error !== null ? true : undefined}
        aria-describedby={describedBy(id, error, hint)}
        onChange={(e) => {
          const next = e.target.value;
          setDraft(next);
          if (next !== value && check(next) === null) onCommit(next);
        }}
        onBlur={() => {
          setDraft(value);
        }}
      />
    </Field>
  );
}
