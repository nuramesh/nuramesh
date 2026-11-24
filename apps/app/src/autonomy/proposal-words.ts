/**
 * The server's proposal words in plain language (`GET /v1/autonomy/proposals`,
 * the autonomy notes section 4.3). The status and the reason codes are the server's own
 * (`core/proposals.py`); a word this file does not know is shown as the server sent it.
 */

/** The one status a grant can be made from. */
export const PROPOSABLE = "proposable";

export const STATUS_WORDS: Record<string, string> = {
  proposable: "can be granted",
  blocked: "blocked",
  not_enough_evidence: "not enough evidence",
  ceiling: "not allowed by the ceiling",
  already_granted: "already granted",
  never_grantable: "never grantable",
  no_such_rule: "no such rule",
};

/** One sentence per reason code. No numbers: the thresholds are the organization's settings. */
export const REASON_WORDS: Record<string, string> = {
  never_grantable: "This kind of action always asks a person.",
  no_such_rule: "The role's policy has no ask rule here any more.",
  ceiling_none: "The ceiling for this role and kind of action allows no autonomy.",
  already_granted: "Autonomy is already granted here.",
  approved_below_min: "Too few approved asks in the window.",
  verdicts_below_min: "Too few gate verdicts on the approved work in the window.",
  denied_in_window: "A person denied an ask in the window.",
  pass_rate_below_min: "The gate pass rate of the approved work is below the minimum.",
  eval_blocked: "The role's required evals do not allow it now.",
  eval_failed_in_cooldown: "A required eval failed during the cooldown.",
  breach_in_cooldown: "A budget was breached during the cooldown.",
  demotion_in_cooldown: "A grant on this cell was demoted during the cooldown.",
};

export const statusWords = (status: string): string => STATUS_WORDS[status] ?? status;
export const reasonWords = (code: string): string => REASON_WORDS[code] ?? code;

/** `role dev, write file`: who the proposal is about and the kind of action its rule covers. */
export function proposalTarget(item: { subject: { kind: string; id: string }; kind: string | null }): string {
  return `${item.subject.kind} ${item.subject.id}${item.kind === null ? "" : `, ${item.kind.replaceAll("_", " ")}`}`;
}
