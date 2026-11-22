import type { Money } from "./types";

const SCALE = 6; // the ledger keeps six decimal places
const UNIT = 10n ** BigInt(SCALE);

/** Money as integer micro-units, so sums are exact (never JavaScript floats). */
export function toMicros(money: Money): bigint {
  const match = /^(-?)(\d+)(?:\.(\d{1,6}))?$/.exec(money.amount);
  if (match === null) throw new RangeError(`not a money amount: "${money.amount}"`);
  const [, sign, whole = "0", fraction = ""] = match;
  const micros = BigInt(whole) * UNIT + BigInt(fraction.padEnd(SCALE, "0"));
  return sign === "-" ? -micros : micros;
}

export function fromMicros(micros: bigint, currency = "USD"): Money {
  const negative = micros < 0n;
  const abs = negative ? -micros : micros;
  const whole = abs / UNIT;
  const fraction = (abs % UNIT).toString().padStart(SCALE, "0").replace(/0+$/, "").padEnd(2, "0");
  return { amount: `${negative ? "-" : ""}${whole.toString()}.${fraction}`, currency };
}

export function usd(amount: string): Money {
  const parsed: Money = { amount, currency: "USD" };
  toMicros(parsed); // throws on a malformed amount
  return parsed;
}

function sameCurrency(a: Money, b: Money): void {
  if (a.currency !== b.currency) throw new RangeError(`can't combine ${a.currency} and ${b.currency}`);
}

export function addMoney(a: Money, b: Money): Money {
  sameCurrency(a, b);
  return fromMicros(toMicros(a) + toMicros(b), a.currency);
}

export function compareMoney(a: Money, b: Money): -1 | 0 | 1 {
  sameCurrency(a, b);
  const diff = toMicros(a) - toMicros(b);
  return diff < 0n ? -1 : diff > 0n ? 1 : 0;
}

/** Whole-number percentage of `limit` used by `spent` (rounded, as the design shows), 0–100. */
export function percentUsed(spent: Money, limit: Money): number {
  const cap = toMicros(limit);
  if (cap <= 0n) return 0;
  const percent = Number((toMicros(spent) * 100n + cap / 2n) / cap);
  return Math.max(0, Math.min(100, percent));
}

/** "$42.10": two decimals, more only when they are not zero ("$0.0003"). Display only. */
export function formatMoney(money: Money): string {
  const micros = toMicros(money);
  const negative = micros < 0n;
  const abs = negative ? -micros : micros;
  const whole = (abs / UNIT).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = (abs % UNIT).toString().padStart(SCALE, "0").replace(/0+$/, "").padEnd(2, "0");
  return money.currency === "USD" ? `${negative ? "-" : ""}$${whole}.${fraction}` : `${negative ? "-" : ""}${whole}.${fraction} ${money.currency}`;
}

/**
 * "$0.00109650": the amount exactly as the server wrote it (the ledger keeps up to 8 places), at
 * least two places, thousands grouped. Text only, no rounding and no floats. Audit view (R1.27).
 */
export function formatMoneyExact(money: Money): string {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(money.amount);
  if (match === null) throw new RangeError(`not a money amount: "${money.amount}"`);
  const [, sign = "", whole = "0", fraction = ""] = match;
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const places = fraction.padEnd(8, "0");
  return money.currency === "USD" ? `${sign}$${grouped}.${places}` : `${sign}${grouped}.${places} ${money.currency}`;
}
