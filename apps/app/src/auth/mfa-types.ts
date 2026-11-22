/** What `GET /v1/auth/mfa` says about the signed-in person (spec/identity/mfa.md section 4). */
export interface MfaStatus {
  /** The server has the key that seals secrets; without it nobody can enrol and codes cannot be checked. */
  available: boolean;
  enrolled: boolean;
  confirmedAt: string | null;
  recoveryCodesRemaining: number;
  /** The owner policy applies to this person (an owner, policy on). */
  required: boolean;
}

/** The pending factor a start of enrolment returns. These are secrets: React state only (AU-4). */
export interface MfaEnrollment {
  factorId: string;
  /** The base32 secret as text (shown only behind "Show the key"). */
  secret: string;
  otpauthUri: string;
}

export interface MfaConfirmInput {
  factorId: string;
  code: string;
}

export interface MfaPasswordCode {
  password: string;
  code: string;
}

/** The ten recovery codes: `^[A-HJ-NP-Z2-9]{4}(-[A-HJ-NP-Z2-9]{4}){3}$` each (spec/mfa-web AU-1). */
export const RECOVERY_CODE_DISPLAY = /^[A-HJ-NP-Z2-9]{4}(-[A-HJ-NP-Z2-9]{4}){3}$/;

export interface LoginConfirmInput {
  challenge: string;
  factorId: string;
  code: string;
}
