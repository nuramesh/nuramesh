/**
 * Line ids. A reporting line is `edge:<report>` on the canvas and just `<report>` in the selection;
 * verification lines are `check:<verifier>:<checked>` and approval lines `approve:<person>:<agent>`
 * (colons can't appear in agent ids, so the kinds never collide).
 */

/** Edge ids on the canvas: one reporting line per reporting agent. */
export const edgeId = (reportId: string): string => `edge:${reportId}`;
export const reportOfEdge = (id: string): string => id.slice("edge:".length);

export const checkEdgeId = (verifier: string, checked: string): string => `check:${verifier}:${checked}`;
export const approvalEdgeId = (person: string, agent: string): string => `approve:${person}:${agent}`;

/** The selection key of an edge: a reporting line is its report's id (as before); the others keep their id. */
export function edgeKey(id: string): string {
  return id.startsWith("edge:") ? id.slice("edge:".length) : id;
}

/** Where a line's selection key leads. */
export type EdgeRef =
  | { kind: "reporting"; report: string }
  | { kind: "check"; verifier: string; checked: string }
  | { kind: "approval"; person: string; agent: string };

export function edgeRef(key: string): EdgeRef {
  if (key.startsWith("check:")) {
    const [verifier = "", checked = ""] = key.slice("check:".length).split(":");
    return { kind: "check", verifier, checked };
  }
  if (key.startsWith("approve:")) {
    const rest = key.slice("approve:".length); // the person id itself contains a colon
    const at = rest.lastIndexOf(":");
    return { kind: "approval", person: rest.slice(0, at), agent: rest.slice(at + 1) };
  }
  return { kind: "reporting", report: key };
}

