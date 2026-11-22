/**
 * The token and cache view (R1.34 Part A): what `GET /v1/savings/tokens` says, mapped for the
 * Savings tab of the Budgets screen. Read only. Nothing here is rounded or guessed: counts are
 * integers, money and rates stay the server's text, and an unknown stays `null` or is counted in
 * `unknownSavingCalls`, never shown as zero.
 */
import type { Money } from "./types";

export type TokenGroupBy = "none" | "provider" | "model" | "role";

export interface RawTokenRow {
  group: string;
  calls: number;
  unknown_calls: number;
  known_total: string;
  complete: boolean;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  cache_write_tokens: number;
  calls_without_usage: number;
  /** Six places, or null when no prompt token was recorded. */
  cache_hit_rate: string | null;
  cache_saving: string;
  cache_write_premium: string;
  net_cache_saving: string;
  unknown_saving_calls: number;
  saving_complete: boolean;
  /** Six places (may be negative), or null when it cannot be stated. */
  cache_saving_share: string | null;
}

export interface RawTokenSavings {
  organization: string;
  currency: string;
  as_of: string;
  from: string;
  to: string;
  group_by: TokenGroupBy;
  rows: RawTokenRow[];
}

export interface TokenRow {
  group: string;
  calls: number;
  /** Calls whose cost was not recorded: they are counted, and excluded from the money figures. */
  unknownCalls: number;
  knownTotal: Money;
  complete: boolean;
  inputTokens: number;
  outputTokens: number;
  cachedInputTokens: number;
  cacheWriteTokens: number;
  callsWithoutUsage: number;
  cacheHitRate: string | null;
  cacheSaving: Money;
  cacheWritePremium: Money;
  netCacheSaving: Money;
  /** Calls whose saving cannot be known; the saving figures exclude them. */
  unknownSavingCalls: number;
  savingComplete: boolean;
  cacheSavingShare: string | null;
}

export interface TokenSavings {
  organization: string;
  asOf: string;
  from: string;
  to: string;
  groupBy: TokenGroupBy;
  rows: TokenRow[];
}

export interface SavingsApi {
  /** Tokens by kind, cache hit rate and what the cache saved, for the current month by default. */
  tokens(options?: { groupBy?: TokenGroupBy }): Promise<TokenSavings>;
}

/** The server's answer as the screens read it. */
export function tokenSavings(raw: RawTokenSavings): TokenSavings {
  const money = (amount: string): Money => ({ amount, currency: raw.currency });
  return {
    organization: raw.organization,
    asOf: raw.as_of,
    from: raw.from,
    to: raw.to,
    groupBy: raw.group_by,
    rows: raw.rows.map((r) => ({
      group: r.group,
      calls: r.calls,
      unknownCalls: r.unknown_calls,
      knownTotal: money(r.known_total),
      complete: r.complete,
      inputTokens: r.input_tokens,
      outputTokens: r.output_tokens,
      cachedInputTokens: r.cached_input_tokens,
      cacheWriteTokens: r.cache_write_tokens,
      callsWithoutUsage: r.calls_without_usage,
      cacheHitRate: r.cache_hit_rate,
      cacheSaving: money(r.cache_saving),
      cacheWritePremium: money(r.cache_write_premium),
      netCacheSaving: money(r.net_cache_saving),
      unknownSavingCalls: r.unknown_saving_calls,
      savingComplete: r.saving_complete,
      cacheSavingShare: r.cache_saving_share,
    })),
  };
}

/** "0.495164" as "49.5164%", "-0.000001" as "-0.0001%": from the text, never through a float. */
export function formatSavingShare(rate: string | null): string {
  if (rate === null) return "not stated";
  const match = /^(-?)(\d+)\.(\d{6})$/.exec(rate);
  if (match === null) return rate;
  const [, sign = "", whole = "0", fraction = ""] = match;
  const n = BigInt(`${whole}${fraction}`); // micro-units: 1.000000 is 100%
  const percent = n / 10_000n;
  const rest = (n % 10_000n).toString().padStart(4, "0").replace(/0+$/, "");
  return `${n === 0n ? "" : sign}${percent.toString()}${rest === "" ? "" : `.${rest}`}%`;
}

/** "$0.0027648", "-$0.000768": the server's decimal text with at least two places and no trailing zeros beyond them. */
export function formatSaved(money: Money): string {
  const negative = money.amount.startsWith("-");
  const [whole = "0", fraction = ""] = (negative ? money.amount.slice(1) : money.amount).split(".");
  const kept = fraction.replace(/0+$/, "").padEnd(2, "0");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const text = money.currency === "USD" ? `$${grouped}.${kept}` : `${grouped}.${kept} ${money.currency}`;
  return negative && /[1-9]/.test(money.amount) ? `-${text}` : text;
}
