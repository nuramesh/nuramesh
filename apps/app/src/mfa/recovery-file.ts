/** The file a person saves: the codes, the date and a warning. Never the email, the organization or the secret (W-7). */
export function recoveryFileText(codes: readonly string[], now: Date): string {
  return [
    "NuraMesh recovery codes",
    `Made: ${now.toISOString().slice(0, 10)}`,
    "Each code works once. Anyone who has these codes can sign in as you. Keep them in a password manager and delete this file after you import it.",
    "",
    ...codes,
    "",
  ].join("\n");
}
