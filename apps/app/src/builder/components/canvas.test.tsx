import { ReactFlowProvider } from "@xyflow/react";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { createBuilderStore } from "../store";
import { BuilderStoreContext } from "../store-context";
import { BUILT_IN_TEMPLATES } from "../templates";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Canvas } from "./canvas";
import { FLOW_PRO_OPTIONS } from "./flow-options";

function mount(readOnly: boolean) {
  const doc = BUILT_IN_TEMPLATES.find((t) => t.id === "dev-team")?.document;
  if (doc === undefined) throw new Error("no template");
  return render(
    <BuilderStoreContext value={createBuilderStore(doc)}>
      <TooltipProvider>
        <ReactFlowProvider>
          <Canvas fullscreen={false} onToggleFullscreen={() => undefined} readOnly={readOnly} />
        </ReactFlowProvider>
      </TooltipProvider>
    </BuilderStoreContext>,
  );
}

describe("Canvas attribution (#1133)", () => {
  it("hides the React Flow attribution via proOptions", () => {
    expect(FLOW_PRO_OPTIONS.hideAttribution).toBe(true);
  });

  for (const readOnly of [false, true]) {
    it(`renders no .react-flow__attribution element (readOnly=${String(readOnly)})`, () => {
      const { container } = mount(readOnly);
      expect(container.querySelector(".react-flow")).not.toBeNull();
      expect(container.querySelector(".react-flow__attribution")).toBeNull();
    });
  }
});
