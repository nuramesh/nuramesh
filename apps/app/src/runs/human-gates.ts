import type { HumanGate, RunTask } from "@/api/types";

/**
 * The criteria a human gate decides, in the task's order, with their texts. The server takes a
 * decision that covers exactly the gate's checks: a task's other criteria belong to other gates.
 */
export function gateCriteria(task: RunTask, gate: HumanGate): { id: string; text: string }[] {
  const known = (task.criteria ?? []).filter((c) => gate.checks.includes(c.id));
  // A check the task's criteria do not name still has to be answered: it is shown by its id.
  const unnamed = gate.checks.filter((id) => !known.some((c) => c.id === id)).map((id) => ({ id, text: id }));
  return [...known, ...unnamed];
}

/**
 * The human gates a person can still decide: the task waits in `verifying`, the server named the
 * round and the gate's checks, and the gate has no result yet in that round (a decided gate would
 * only answer 409 `already_decided`).
 */
export function openHumanGates(task: RunTask): HumanGate[] {
  if (task.status !== "verifying" || task.submission === undefined) return [];
  const decided = (gateId: string) => task.gates.some((g) => g.gate === gateId && g.attempt === task.attempt && g.submission === task.submission);
  return (task.humanGates ?? []).filter((gate) => gate.checks.length > 0 && !decided(gate.id));
}
