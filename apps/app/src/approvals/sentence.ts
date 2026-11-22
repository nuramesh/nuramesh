import { formatMoney } from "@/api/money";
import type { Approval, Money } from "@/api/types";

/** The longest target shown in the sentence; the whole text is in the detail (AE-6). */
export const MAX_TARGET = 200;

const NO_TARGET = "(no target given)";

function isControl(point: number): boolean {
  return (
    point <= 0x1f ||
    (point >= 0x7f && point <= 0x9f) ||
    point === 0x061c ||
    point === 0x200e ||
    point === 0x200f ||
    (point >= 0x202a && point <= 0x202e) ||
    (point >= 0x2066 && point <= 0x2069)
  );
}

/**
 * AE-6: text an agent chose, made safe to read. Each control or bidirectional character becomes the
 * visible six characters `\uXXXX`; nothing else changes (React shows the rest as plain text). Display only.
 */
export function escapeInert(text: string): string {
  let out = "";
  for (const ch of text) {
    const point = ch.codePointAt(0) ?? 0;
    out += isControl(point) ? `\\u${point.toString(16).toUpperCase().padStart(4, "0")}` : ch;
  }
  return out;
}

/** Escaped, and cut at {@link MAX_TARGET} characters with `...`. */
export function cutInert(text: string): string {
  const points = Array.from(text);
  return points.length > MAX_TARGET ? `${escapeInert(points.slice(0, MAX_TARGET).join(""))}...` : escapeInert(text);
}

export interface SentenceParts {
  agent: string;
  kind: string;
  target: string | null;
  amount: Money | null;
}

/** What the agent wants to do, without the agent's name: "merge main." (AE-5). */
export function actionPhrase({ kind, target, amount }: Omit<SentenceParts, "agent">): string {
  const what = target === null || target === "" ? NO_TARGET : cutInert(target);
  switch (kind) {
    case "merge":
      return `merge ${what}.`;
    case "spend":
      return `spend ${amount === null ? "money" : formatMoney(amount)}.`;
    case "external_message":
      return `send a message outside your organization: ${what}.`;
    case "run_command":
      return `run a command: ${what}.`;
    case "network":
      return `connect to ${what}.`;
    case "use_tool":
      return `use the tool ${what}.`;
    case "write_file":
      return `change the file ${what}.`;
    case "call_model":
      return `call the model ${what}.`;
    case "delegate":
      return `hand work to ${what}.`;
    default:
      return `do something called ${cutInert(kind)}: ${what}.`;
  }
}

/** AE-5: the one plain line at the top of a request. */
export function approvalSentence({ agent, ...rest }: SentenceParts): string {
  return `${escapeInert(agent)} wants to ${actionPhrase(rest)}`;
}

/** The sentence of a request: from its action, else its (escaped) title. List row and detail both call this. */
export function sentenceOf(approval: Approval): string {
  const { action } = approval;
  if (action === undefined) return escapeInert(approval.title);
  return approvalSentence({ agent: approval.requester, kind: action.kind, target: action.target, amount: action.amount });
}

const CONSEQUENCE: Record<string, string> = {
  merge: "changes the named branch",
  external_message: "the message leaves the organization and cannot be recalled",
  spend: "the amount is spent from the budget",
  run_command: "the command runs",
  network: "this one destination is opened",
  use_tool: "the tool runs with its declared effects",
  write_file: "this one file is changed",
  call_model: "one model call is made and paid for",
  delegate: "the other agent starts the work",
};

/** AE-7: fixed wording per kind; never any text from the agent. */
export function consequenceFor(kind: string): string {
  return Object.hasOwn(CONSEQUENCE, kind) ? (CONSEQUENCE[kind] ?? "") : "the agent does this one thing";
}

/** AE-8: one cost line, never a guess and never hidden. */
export function costLine(action: { kind: string; amount: Money | null }, runSpend: Approval["runSpend"]): string {
  if (action.kind === "spend") return action.amount === null ? "Cost not available" : formatMoney(action.amount);
  if (runSpend === null) return "Cost not available";
  return runSpend.limit === null ? "No direct cost" : `Spent so far ${formatMoney(runSpend.spent)} of ${formatMoney(runSpend.limit)}`;
}
