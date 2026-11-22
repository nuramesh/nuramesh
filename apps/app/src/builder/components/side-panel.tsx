import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import type { PanelTab } from "../store";
import { useBuilder, useBuilderStore } from "../store-context";
import { Inspector } from "./inspector";
import { IssuesPanel } from "./issues-panel";
import { RolesPanel } from "./roles-panel";

function isPanelTab(value: string): value is PanelTab {
  return value === "inspector" || value === "roles" || value === "issues";
}

/** Right-hand panel: properties of the selection, roles, and validation problems. */
export function SidePanel() {
  const store = useBuilderStore();
  const panel = useBuilder((s) => s.panel);
  const errors = useBuilder((s) => s.issues.errorCount);
  const warnings = useBuilder((s) => s.issues.warningCount);
  const total = errors + warnings;

  return (
    <aside aria-label="Details" className="flex min-h-0 min-w-0 max-w-full flex-col border-l bg-pane">
      <Tabs
        value={panel}
        onValueChange={(value) => {
          if (isPanelTab(value)) store.getState().setPanel(value);
        }}
        className="flex min-h-0 flex-1 flex-col gap-0"
      >
        <TabsList className="m-2 grid grid-cols-3">
          <TabsTrigger value="inspector">Properties</TabsTrigger>
          <TabsTrigger value="roles">Roles</TabsTrigger>
          <TabsTrigger value="issues">
            Problems
            {total > 0 && (
              <span
                className={`ml-1 rounded px-1 text-[11px] font-semibold ${errors > 0 ? "bg-destructive text-white" : "bg-secondary"}`}
              >
                {total}
              </span>
            )}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="inspector" className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto p-3">
          <Inspector />
        </TabsContent>
        <TabsContent value="roles" className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto p-3">
          <RolesPanel />
        </TabsContent>
        <TabsContent value="issues" className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto p-3">
          <IssuesPanel />
        </TabsContent>
      </Tabs>
    </aside>
  );
}
