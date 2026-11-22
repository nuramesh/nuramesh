/**
 * The rules of the model screens that are decided in the browser only to explain things before a
 * request is sent (the server decides again and its answer is always the one shown): reason
 * length, which data classes a processing scope accepts (ND45), and whether an edit widens one.
 */
import { pyStrip } from "@/model/org";

import type { DataClass } from "./models";

export const REASON_MIN = 3;
export const REASON_MAX = 500;
export const REASON_TEXT = "A reason of 3 to 500 characters is required";


/** `null` when the reason is acceptable, else what to tell the person. */
export function reasonProblem(reason: string): string | null {
  const length = Array.from(pyStrip(reason)).length;
  return length >= REASON_MIN && length <= REASON_MAX ? null : REASON_TEXT;
}

const REGION = /^[a-z][a-z0-9-]{1,39}$/;
const PROCESSING_REGION = /^region:([a-z][a-z0-9-]{1,39})$/;

export function validProcessing(value: string): boolean {
  return value === "canada" || value === "global" || PROCESSING_REGION.test(value);
}

export function validRegionId(value: string): boolean {
  return REGION.test(value);
}

/** The data classes a deployment with this processing scope may receive (core.residency). */
export function acceptedClasses(processing: string, customerRegions: readonly string[]): DataClass[] {
  if (processing === "canada") return ["synthetic", "customer"];
  const region = PROCESSING_REGION.exec(processing)?.[1];
  if (region !== undefined && customerRegions.includes(region)) return ["synthetic", "customer"];
  return processing === "global" || region !== undefined ? ["synthetic"] : [];
}

/** Whether `after` accepts a data class that `before` did not (a widening needs a fresh approval, spec 2.6). */
export function widens(before: string, after: string, customerRegions: readonly string[]): boolean {
  const had = acceptedClasses(before, customerRegions);
  return acceptedClasses(after, customerRegions).some((c) => !had.includes(c));
}

/** Approved entries a regions write would pause again: a `region:<id>` entry gaining `customer`. */
export function regionsWiden(
  entries: readonly { ref: string; processing: string; status: string }[],
  before: readonly string[],
  after: readonly string[],
): string[] {
  return entries
    .filter(
      (e) =>
        e.status === "approved" &&
        acceptedClasses(e.processing, after).length > acceptedClasses(e.processing, before).length,
    )
    .map((e) => e.ref);
}

/** A comma, space or newline separated list as a sorted, de-duplicated list of region ids. */
export function parseRegions(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).filter((r) => r !== ""))].sort();
}

/** `https://<name>.openai.azure.com` or `.cognitiveservices.azure.com`: a hint only; the server decides. */
export function endpointHint(endpoint: string): string | null {
  const value = endpoint.trim();
  if (/^https:\/\/[a-z0-9][a-z0-9-]*\.(openai|cognitiveservices)\.azure\.com\/?$/i.test(value)) return null;
  return "The endpoint must be https://<name>.openai.azure.com (or .cognitiveservices.azure.com)";
}

const LOOPBACK = /^http:\/\/(127\.0\.0\.1|\[::1\])(:\d{1,5})?(\/|$)/i;

/**
 * An openai-compatible base URL (a hint only; the server decides): http or https, no sign-in
 * parts, no query or fragment, http only to 127.0.0.1 or [::1], and never http with a key.
 */
export function openAiEndpointHint(endpoint: string, keyed: boolean): string | null {
  const value = endpoint.trim();
  if (!/^https?:\/\//i.test(value) || /\s/.test(value)) return "Enter a base URL that starts with https:// (or http:// for a server on this machine)";
  if (/[?#]/.test(value) || /^https?:\/\/[^/]*@/i.test(value)) return "The URL can't hold a query, a fragment or a login: it is the base URL only";
  if (/^http:/i.test(value) && !LOOPBACK.test(value)) return "Plain http is only allowed to 127.0.0.1 or [::1]; use https for anything else";
  if (/^http:/i.test(value) && keyed) return "A key is never sent over plain http: use https, or connect without a key";
  return null;
}

/** A key binding name (the server's own rule): 1 to 40 of `a-z`, `0-9`, `_`, `-`, starting with a letter or digit. */
export function keyNameProblem(name: string): string | null {
  return /^[a-z0-9][a-z0-9_-]{0,39}$/.test(name) ? null : "Use 1 to 40 lowercase letters, digits, hyphens or underscores, starting with a letter or digit";
}

/**
 * A client-side warning only (the server rule is unchanged): a value that looks like a pasted key, not a binding name.
 * Conservative: 20+ characters with a digit and a letter and no separator, or a well-known key prefix. Never echoes the value.
 */
export function keyLooksPasted(name: string): boolean {
  const v = name.trim();
  if (v.length < 20) return false;
  if (/^(sk|gsk|pk|xai|key)[-_]/i.test(v)) return true;
  return /^[A-Za-z0-9]+$/.test(v) && /[0-9]/.test(v) && /[A-Za-z]/.test(v);
}

/** An endpoint id: a slug of 1 to 40 characters (`a-z`, `0-9`, `-`), the server's own rule. */
export function endpointIdProblem(id: string): string | null {
  return /^[a-z0-9][a-z0-9-]{0,39}$/.test(id) ? null : "Use 1 to 40 lowercase letters, digits or hyphens, starting with a letter or digit";
}

/** The parts of a ref, or `null`: `provider/model`, or `openai-compatible/<endpoint id>/<model>` (a model name may hold slashes). */
export function splitRef(ref: string): { provider: string; endpointId?: string; model: string } | null {
  const slash = ref.indexOf("/");
  if (slash <= 0 || slash >= ref.length - 1) return null;
  const provider = ref.slice(0, slash);
  const rest = ref.slice(slash + 1);
  if (provider !== "openai-compatible") return { provider, model: rest };
  const next = rest.indexOf("/");
  return next > 0 && next < rest.length - 1 ? { provider, endpointId: rest.slice(0, next), model: rest.slice(next + 1) } : null;
}

/** True when the URL's host is exactly `127.0.0.1` or `[::1]`: the server's own machine (core.endpoints `is_loopback`). */
export function isLoopbackEndpoint(endpoint: string): boolean {
  return /^https?:\/\/(127\.0\.0\.1|\[::1\])(:\d{1,5})?(\/|$)/i.test(endpoint.trim());
}

/** Decimal text that is exactly zero (`0`, `0.00`); the text is never turned into a float. */
export function isZeroRate(text: string): boolean {
  return /^0+(\.0+)?$/.test(text);
}

/** A provider other than the local `fake` one sends data to a real endpoint. */
export function isRealProvider(provider: string): boolean {
  return provider !== "fake";
}
