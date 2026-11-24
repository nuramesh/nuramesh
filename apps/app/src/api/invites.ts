/**
 * Invitations (R1.37, the invitation notes). An owner invites a person by email with a role; the
 * person opens a single-use link, sets a password and exists. The app never sees a stored token or a
 * hash: the list shows status only, and the link token lives in the invitee's fragment and memory alone.
 */

export type InviteRole = "approver" | "viewer";
export type InviteStatus = "pending" | "accepted" | "revoked" | "expired";

/** One row of the invite list. `email` is null once the invite was accepted. */
export interface Invite {
  id: string;
  email: string | null;
  role: InviteRole;
  status: InviteStatus;
  createdBy: string;
  createdAt: string;
  expiresAt: string;
  acceptedUserId: string | null;
  resentCount: number;
}

/** What a link is for (`POST /v1/auth/invites/inspect`). */
export interface InviteInspection {
  email: string;
  role: InviteRole;
  organization: string;
  expiresAt: string;
}

export interface AcceptInviteInput {
  token: string;
  password: string;
  displayName?: string;
}

/** The shortest password the server accepts for a new account. */
export const INVITE_MIN_PASSWORD = 12;

/**
 * Owner-side calls need an owner session (a refusal is `NotAllowedError`). `create` rejects `ProblemError`
 * `email_not_configured` (503) when mail is off, `rate_limited` (429), and `ConflictError` codes `already_exists`,
 * `invite_pending`, `invite_limit`. `resend` rejects `invite_not_pending`, `resend_limit`, `creator_inactive`.
 * `inspect` and `accept` are public; every refusal of a bad link is one `ProblemError` 403 `invalid_invite`.
 */
export interface InvitesApi {
  list(): Promise<Invite[]>;
  create(input: { email: string; role: InviteRole }): Promise<Invite>;
  revoke(id: string): Promise<Invite>;
  resend(id: string): Promise<Invite>;
  inspect(token: string): Promise<InviteInspection>;
  accept(input: AcceptInviteInput): Promise<{ id: string; role: InviteRole; email: string }>;
}
