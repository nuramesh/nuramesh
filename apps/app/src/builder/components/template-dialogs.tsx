import { Trash2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { fieldRules } from "@/model/org";

import { useBuilder, useBuilderStore } from "../store-context";
import { TextField } from "./fields";

interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** Saves the current document as a user template (kept in this browser). */
export function SaveTemplateDialog({ open, onOpenChange }: DialogProps) {
  const store = useBuilderStore();
  const orgName = useBuilder((s) => s.doc.org.name);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [touched, setTouched] = useState(false);
  const error = touched ? fieldRules.name(name) : null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (next) {
          setName(orgName);
          setDescription("");
          setTouched(false);
        }
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            setTouched(true);
            if (fieldRules.name(name) !== null) return;
            if (store.getState().saveTemplate(name.trim(), description.trim())) onOpenChange(false);
          }}
        >
          <DialogHeader>
            <DialogTitle>Save as template</DialogTitle>
            <DialogDescription>Saves this organization in this browser so you can start new ones from it.</DialogDescription>
          </DialogHeader>
          <TextField
            id="template-name"
            label="Template name"
            value={name}
            error={error}
            onChange={(value) => {
              setName(value);
              setTouched(true);
            }}
          />
          <TextField id="template-description" label="Description (optional)" value={description} onChange={setDescription} />
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                onOpenChange(false);
              }}
            >
              Cancel
            </Button>
            <Button type="submit">Save template</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Lists saved templates with load and delete. */
export function ManageTemplatesDialog({ open, onOpenChange }: DialogProps) {
  const store = useBuilderStore();
  const templates = useBuilder((s) => s.userTemplates);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Saved templates</DialogTitle>
          <DialogDescription>Templates saved in this browser.</DialogDescription>
        </DialogHeader>
        {templates.length === 0 ? (
          <p className="text-sm text-muted-foreground">No saved templates yet.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {templates.map((t) => (
              <li key={t.id} className="flex items-center gap-2">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{t.name}</p>
                  {t.description !== "" && <p className="truncate text-xs text-muted-foreground">{t.description}</p>}
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    store.getState().load(`Loaded template ${t.name}`, t.document);
                    onOpenChange(false);
                  }}
                >
                  Use
                </Button>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label={`Delete template ${t.name}`}
                  onClick={() => {
                    store.getState().deleteTemplate(t.id);
                  }}
                >
                  <Trash2 aria-hidden="true" />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** Explains why an imported file was rejected, one problem per line. */
export function ImportErrorsDialog({
  fileName,
  errors,
  onClose,
}: {
  fileName: string;
  errors: string[];
  onClose: () => void;
}) {
  return (
    <Dialog
      open={errors.length > 0}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Couldn't import {fileName}</DialogTitle>
          <DialogDescription>
            The file isn't a valid NuraMesh document or organization. Nothing was changed.
          </DialogDescription>
        </DialogHeader>
        <p id="import-errors-title" className="text-sm font-medium">
          {errors.length} problem{errors.length === 1 ? "" : "s"}
        </p>
        <ul aria-labelledby="import-errors-title" className="max-h-64 list-inside list-disc overflow-y-auto text-sm">
            {errors.slice(0, 100).map((message, index) => (
              <li key={index}>{message}</li>
            ))}
            {errors.length > 100 && <li>…and {errors.length - 100} more</li>}
        </ul>
        <DialogFooter>
          <Button onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
