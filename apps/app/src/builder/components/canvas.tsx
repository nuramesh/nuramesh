import "@xyflow/react/dist/style.css";
import "./builder.css";

import {
  Background,
  type Connection,
  type EdgeChange,
  type IsValidConnection,
  type NodeChange,
  type OnDelete,
  Panel,
  ReactFlow,
  useReactFlow,
} from "@xyflow/react";
import { Maximize2, Minimize2 } from "lucide-react";
import { type DragEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useTheme } from "@/lib/theme";

import type { XY } from "../document";
import { edgeKey } from "../edge-ids";
import { EdgeCache, type FlowEdge, type FlowNode, NodeCache, PERSON_HEIGHT, PERSON_WIDTH } from "../flow";
import { FIT_OPTIONS } from "../fit";
import { NODE_HEIGHT, NODE_WIDTH } from "../layout";
import { wouldCreateCycle } from "../ops";
import type { CheckItem } from "../store";
import { useBuilder, useBuilderStore } from "../store-context";
import { fitCanvas, markViewportTouched } from "../viewport-actions";
import { isFullscreenKey } from "../shortcuts";
import { PREDEFINED_ROLES } from "../templates";
import { AgentNode } from "./agent-node";
import { CanvasControls } from "./canvas-controls";
import { FLOW_PRO_OPTIONS } from "./flow-options";
import { PersonNode } from "./person-node";
import { ReportingEdgeView } from "./reporting-edge";

/** Drag-and-drop payload type for roles dragged from the palette. */
export const ROLE_MIME = "application/x-nuramesh-role";
/** Payload type for checks and people dragged from the palette: `check:<kind>` or `person`. */
export const ITEM_MIME = "application/x-nuramesh-item";

const nodeTypes = { agent: AgentNode, person: PersonNode };
const edgeTypes = { reporting: ReportingEdgeView };
const DELETE_KEYS = ["Delete", "Backspace"];
const MULTI_SELECT_KEYS = ["Shift", "Meta", "Control"];

/**
 * The organization canvas: agents as nodes, reporting lines as edges (manager above report). The
 * full-screen button floats in its top-right corner; F toggles it while the canvas has focus.
 */
export function Canvas({
  fullscreen,
  onToggleFullscreen,
  readOnly = false,
}: {
  fullscreen: boolean;
  onToggleFullscreen: () => void;
  /** Phone mode: the graph can be panned and zoomed but not changed. */
  readOnly?: boolean;
}) {
  const store = useBuilderStore();
  const doc = useBuilder((s) => s.doc);
  const computed = useBuilder((s) => s.computed);
  const dragging = useBuilder((s) => s.dragging);
  const selectedNodes = useBuilder((s) => s.selection.nodes);
  const selectedEdges = useBuilder((s) => s.selection.edges);
  const byAgent = useBuilder((s) => s.issues.byAgent);
  const layoutStatus = useBuilder((s) => s.layoutStatus);
  const fitRequest = useBuilder((s) => s.fitRequest);
  const focusRequest = useBuilder((s) => s.focusRequest);
  const colorMode = useTheme((s) => s.resolved);
  const flow = useReactFlow<FlowNode, FlowEdge>();

  const [nodeCache] = useState(() => new NodeCache());
  const [edgeCache] = useState(() => new EdgeCache());
  const nodes = useMemo(
    () => nodeCache.build({ doc, computed, dragging, selected: selectedNodes, issues: byAgent }),
    [nodeCache, doc, computed, dragging, selectedNodes, byAgent],
  );
  const edges = useMemo(() => edgeCache.build(doc, selectedEdges), [edgeCache, doc, selectedEdges]);

  const onNodesChange = useCallback(
    (changes: NodeChange<FlowNode>[]) => {
      const s = store.getState();
      let selection: Set<string> | null = null;
      const moving: Record<string, XY> = {};
      const dropped: Record<string, XY> = {};
      const nudged: Record<string, XY> = {};
      for (const change of changes) {
        if (change.type === "select") {
          selection ??= new Set(s.selection.nodes);
          if (change.selected) selection.add(change.id);
          else selection.delete(change.id);
        } else if (change.type === "position" && change.position) {
          if (change.dragging === true) moving[change.id] = change.position;
          else if (change.dragging === false) dropped[change.id] = change.position;
          else nudged[change.id] = change.position; // keyboard arrows
        }
      }
      if (selection !== null) s.setSelection(selection, s.selection.edges);
      if (Object.keys(moving).length > 0) s.dragMove(moving);
      if (Object.keys(dropped).length > 0) s.dragEnd(dropped);
      if (Object.keys(nudged).length > 0) s.nudge(nudged);
    },
    [store],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange<FlowEdge>[]) => {
      const s = store.getState();
      let selection: Set<string> | null = null;
      for (const change of changes) {
        if (change.type !== "select") continue;
        selection ??= new Set(s.selection.edges);
        if (change.selected) selection.add(edgeKey(change.id));
        else selection.delete(edgeKey(change.id));
      }
      if (selection !== null) s.setSelection(s.selection.nodes, selection);
    },
    [store],
  );

  const onNodeDragStop = useCallback(() => {
    // Safety net: commit whatever is still in flight if the final position change was skipped.
    if (store.getState().dragging !== null) store.getState().dragEnd({});
  }, [store]);

  const onConnect = useCallback(
    (connection: Connection) => {
      if (readOnly) return;
      const s = store.getState();
      if (connection.sourceHandle === "check-out") s.connectCheck(connection.source, connection.target);
      else if (connection.sourceHandle === "approve-out") s.connectApproval(connection.source, connection.target);
      else s.connect(connection.source, connection.target);
    },
    [store, readOnly],
  );

  const isValidConnection = useCallback<IsValidConnection<FlowEdge>>(
    (connection) => {
      if (connection.source === connection.target) return false;
      const isAgent = (id: string): boolean => store.getState().doc.org.agents.some((a) => a.id === id);
      // Verification and approval lines go to the left handle of an agent; reporting lines to the top.
      if (connection.sourceHandle === "check-out" || connection.sourceHandle === "approve-out") {
        return connection.targetHandle === "side-in" && isAgent(connection.target);
      }
      if (connection.targetHandle === "side-in") return false;
      return isAgent(connection.source) && !wouldCreateCycle(store.getState().doc, connection.source, connection.target);
    },
    [store],
  );

  const onReconnect = useCallback(
    (oldEdge: FlowEdge, connection: Connection) => {
      if (!oldEdge.id.startsWith("edge:") || connection.targetHandle === "side-in") return;
      store.getState().reconnect(edgeKey(oldEdge.id), connection.source, connection.target);
    },
    [store],
  );

  const onDelete = useCallback<OnDelete<FlowNode, FlowEdge>>(
    ({ nodes: deletedNodes, edges: deletedEdges }) => {
      store.getState().deleteElements(
        deletedNodes.map((n) => n.id),
        deletedEdges.map((e) => edgeKey(e.id)),
      );
    },
    [store],
  );

  const onDragOver = useCallback((event: DragEvent) => {
    if (!event.dataTransfer.types.includes(ROLE_MIME) && !event.dataTransfer.types.includes(ITEM_MIME)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const onDrop = useCallback(
    (event: DragEvent) => {
      if (readOnly) return;
      const item = event.dataTransfer.getData(ITEM_MIME);
      if (item !== "") {
        event.preventDefault();
        const s = store.getState();
        if (item === "person") {
          const point = flow.screenToFlowPosition({ x: event.clientX, y: event.clientY });
          s.addPersonNode({ x: Math.round(point.x - PERSON_WIDTH / 2), y: Math.round(point.y - PERSON_HEIGHT / 2) });
        } else if (item.startsWith("check:")) {
          // A check lands on the agent under the pointer, else on the selected one.
          const under = event.target instanceof Element ? event.target.closest<HTMLElement>(".react-flow__node-agent") : null;
          s.addCheckItem(item.slice("check:".length) as CheckItem, under?.dataset.id);
        }
        return;
      }
      const roleId = event.dataTransfer.getData(ROLE_MIME);
      if (roleId === "") return;
      event.preventDefault();
      const s = store.getState();
      const role = s.doc.org.roles.find((r) => r.id === roleId) ?? PREDEFINED_ROLES.find((r) => r.id === roleId);
      if (role === undefined) return;
      const point = flow.screenToFlowPosition({ x: event.clientX, y: event.clientY });
      s.addAgentFromRole(role, { x: Math.round(point.x - NODE_WIDTH / 2), y: Math.round(point.y - NODE_HEIGHT / 2) });
    },
    [flow, store, readOnly],
  );

  // F toggles full screen while focus is inside the canvas (a node, the pane or its buttons). A
  // native listener: the section is a landmark, not a control, so it takes no React key handler.
  const section = useRef<HTMLElement>(null);
  const toggle = useRef(onToggleFullscreen);
  useEffect(() => {
    toggle.current = onToggleFullscreen;
  });
  useEffect(() => {
    const element = section.current;
    if (element === null) return;
    const onKey = (event: KeyboardEvent): void => {
      if (!isFullscreenKey(event)) return;
      event.preventDefault();
      toggle.current();
    };
    element.addEventListener("keydown", onKey);
    return () => {
      element.removeEventListener("keydown", onKey);
    };
  }, []);

  // Fit the view after a template/import/auto-layout once layout has placed the agents.
  const fitted = useRef(-1);
  useEffect(() => {
    if (layoutStatus === "running" || fitted.current === fitRequest) return;
    fitted.current = fitRequest;
    fitCanvas(flow);
  }, [computed, fitRequest, flow, layoutStatus]);

  // Centre and focus an agent (from the issues panel).
  useEffect(() => {
    if (focusRequest === null) return;
    const node = flow.getInternalNode(focusRequest.id);
    if (node === undefined) return;
    const { x, y } = node.internals.positionAbsolute;
    void flow
      .setCenter(x + NODE_WIDTH / 2, y + NODE_HEIGHT / 2, { zoom: Math.max(flow.getZoom(), 1), duration: 0 })
      .then(() => {
        requestAnimationFrame(() => {
          const element = document.querySelector<HTMLElement>(
            `.react-flow__node[data-id="${CSS.escape(focusRequest.id)}"]`,
          );
          element?.focus();
        });
      });
  }, [flow, focusRequest]);

  return (
    <section
      ref={section}
      aria-label="Organization canvas"
      tabIndex={-1}
      className="relative h-full min-h-0 min-w-0 overflow-hidden bg-canvas focus:outline-none"
    >
      <ReactFlow<FlowNode, FlowEdge>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeDragStop={onNodeDragStop}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onReconnect={onReconnect}
        onDelete={onDelete}
        onPaneClick={() => {
          // a click on the empty canvas puts keyboard focus on it, so F (full screen) works from there
          section.current?.focus({ preventScroll: true });
        }}
        onDragOver={onDragOver}
        onDrop={onDrop}
        deleteKeyCode={readOnly ? null : DELETE_KEYS}
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        edgesReconnectable={!readOnly}
        elementsSelectable={!readOnly}
        selectionKeyCode="Shift"
        multiSelectionKeyCode={MULTI_SELECT_KEYS}
        colorMode={colorMode}
        proOptions={FLOW_PRO_OPTIONS}
        minZoom={0.05}
        maxZoom={2}
        onlyRenderVisibleElements
        onMoveStart={(event) => {
          if (event !== null) markViewportTouched(); // a drag, wheel or pinch by the person
        }}
        fitView
        fitViewOptions={FIT_OPTIONS}
      >
        <Background gap={colorMode === "light" ? 16 : 24} color="var(--t-grid-dot)" />
        <CanvasControls />
        {!readOnly && (
        <Panel position="top-right" className="m-3!">
          <Tip label={fullscreen ? "Exit full screen" : "Full screen (F)"} side="left">
            <Button
              variant="outline"
              size="icon-sm"
              id="builder-fullscreen-toggle"
              className="bg-panel shadow-md"
              aria-label={fullscreen ? "Exit full screen" : "Full screen"}
              aria-keyshortcuts={fullscreen ? "Escape F" : "F"}
              onClick={onToggleFullscreen}
            >
              {fullscreen ? <Minimize2 aria-hidden="true" /> : <Maximize2 aria-hidden="true" />}
            </Button>
          </Tip>
        </Panel>
        )}
      </ReactFlow>
      {doc.org.agents.length === 0 && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-8">
          <p className="max-w-sm rounded-lg border border-card-border bg-background/90 p-4 text-center text-sm text-muted-foreground">
            {readOnly
              ? "This organization has no agents yet. Add them on a larger screen."
              : "Drag a role from the palette onto the canvas, or start from a template. Start with a Chief of Staff: every organization has one at the top."}
          </p>
        </div>
      )}
    </section>
  );
}
