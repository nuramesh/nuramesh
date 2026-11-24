/** Every string of the first-run checklist (the first-run checklist notes FR-15), in one module for review. */
import type { StepId } from "./derive";

export const STEP_LABELS: Record<StepId, string> = {
  owner: "Create the owner account",
  run: "Start the demo run",
  approve: "Decide the first approval",
  invite: "Invite a teammate",
};

export const COPY = {
  title: "Get started",
  couldNotCheck: "Could not check. Try again.",
  retry: "Retry",
  noApprovalAsked: "No approval has been asked yet",
  approvalWhat: "An agent asked to do something that needs a person's answer.",
  soloApproval:
    "If you are the only person who can decide, your answer is recorded as a solo approval.",
  openPeople: "Invite a teammate",
  invitePeople: "Invitations are sent from Settings, People.",
  nothingToSetUp: "Nothing left to set up",
  complete: "You are set up",
  skip: "Skip for now",
  skipped: "Skipped",
  doIt: "Do it",
  hide: "Hide this checklist",
  show: "Show the setup checklist",
  cliIntro: "Start a demo run from a terminal:",
  steps: "Setup steps",
  done: "Done",
  notDone: "Not done",
  openApprovals: "Open approvals",
  openBuilder: "Open the Builder",
  ledger: "Ledger",
  builder: "Builder",
  approvals: "Approvals",
  sidebarOf: "of 4",
  cliLine: "nuramesh try my-team --fake",
} as const;
