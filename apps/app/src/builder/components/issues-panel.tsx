import { AlertCircle, AlertTriangle, CheckCircle2 } from "lucide-react";

import type { BuilderIssue } from "../issues";
import { useBuilder, useBuilderStore } from "../store-context";

/** Every validation problem; pressing one focuses the agent or role it's about. */
export function IssuesPanel() {
  const store = useBuilderStore();
  const report = useBuilder((s) => s.issues);

  const open = (issue: BuilderIssue): void => {
    const s = store.getState();
    if (issue.target?.kind === "agent") s.focusAgent(issue.target.id);
    else if (issue.target?.kind === "role") s.focusRole(issue.target.id);
    else if (issue.target?.kind === "org") {
      s.setSelection([]);
      s.setPanel("inspector");
    }
  };

  if (report.issues.length === 0) {
    return (
      <p className="flex items-center gap-2 text-sm">
        <CheckCircle2 aria-hidden="true" className="size-4 text-link" />
        No problems. This organization is valid.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm">
        {report.errorCount} error{report.errorCount === 1 ? "" : "s"}, {report.warningCount} warning
        {report.warningCount === 1 ? "" : "s"}
      </p>
      <ul className="flex flex-col gap-1" aria-label="Problems">
        {report.issues.map((issue) => {
          const Icon = issue.severity === "error" ? AlertCircle : AlertTriangle;
          const content = (
            <>
              <Icon
                aria-hidden="true"
                className={`mt-0.5 size-4 shrink-0 ${issue.severity === "error" ? "text-destructive" : "text-warning"}`}
              />
              <span className="min-w-0 flex-1 break-words">
                <span className="sr-only">{issue.severity === "error" ? "Error: " : "Warning: "}</span>
                {issue.message}
              </span>
            </>
          );
          return (
            <li key={issue.key} data-code={issue.code}>
              {issue.target === null ? (
                <div className="flex gap-2 rounded-md px-2 py-1.5 text-left text-sm">
                  {content}
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    open(issue);
                  }}
                  className="flex w-full gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent"
                >
                  {content}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
