import type { ChipTone } from "@/components/ui/chip";
import { formatMoneyExact } from "@/api/money";
import type { Money } from "@/api/types";
import { type CatalogEntry, type DataClass, type EntryStatus, FREE_SOURCE, type ModelPrice } from "@/api/models";

export const STATUS_TONE: Record<EntryStatus, ChipTone> = { approved: "success", paused: "warning", retired: "neutral" };

/** "synthetic data only" or "synthetic and customer data": the ND45 rule, in words. */
export function dataWords(classes: readonly DataClass[]): string {
  if (classes.includes("customer")) return "synthetic and customer data";
  return classes.includes("synthetic") ? "synthetic data only" : "no data";
}

/** The fixed words for a model with no price (spec default-prices 1): the call is blocked, nothing is sent or charged. Never "cost unknown": that is a different condition (a price exists, the answer had no usage). */
export const NO_PRICE_WORDS = "No price: calls to this model are blocked";

/** The fixed words for a call whose cost is unknown (spec default-prices 6.2): a price exists, the answer had no usable usage, the reservation was charged. Never "no price": that call was blocked. The words are the server's contract (`nuramesh.core.price_words`), pinned by a Python test. */
export const COST_UNKNOWN_WORDS = "Cost unknown (charged the reservation)";

/** `at least $0.0600`: a total that holds at least one call of unknown cost (the KNOWN total only, from the server: a reservation charge is an upper bound and is never added). Never a bare total. */
export const atLeastWords = (total: Money): string => `at least ${formatMoneyExact(total)}`;

/** The price in effect, as the server's decimal text; unknown is said, never guessed. */
export function priceWords(price: ModelPrice | null): string {
  if (price === null) return NO_PRICE_WORDS;
  const rates = `$${price.inputPerMillion} in · $${price.outputPerMillion} out per 1M tokens`;
  // a zero price is only ever a declared one (the server refuses any other): say so, it is not a missing price
  const declared = price.source === FREE_SOURCE ? " (declared free, local)" : "";
  if (price.provisional) return `${rates}${declared} (provisional)`;
  if (price.origin === "shipped") {
    const old = price.stale === true && price.ageDays != null ? ` · list is ${String(price.ageDays)} days old` : "";
    return `${rates} · shipped list, ${price.retrievedOn == null ? "date unknown" : formatListDate(price.retrievedOn)}${old}`;
  }
  if (price.origin === "operator") return `${rates}${declared} · set by your organization`;
  return `${rates}${declared}`;
}

/** `2026-11-17` as `17 Nov 2026` (taken from the text, so no time zone can move the day). */
function formatListDate(day: string): string {
  const [y, m, d] = day.split("-").map(Number);
  if (!y || !m || !d) return day;
  const month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m - 1];
  return `${String(d)} ${month ?? ""} ${String(y)}`;
}

/** The line over the catalog: how many entries have no price (they are blocked), or `null` when none. */
export function noPriceCount(entries: readonly CatalogEntry[]): string | null {
  const n = entries.filter((e) => e.price === null).length;
  if (n === 0) return null;
  return `${String(n)} ${n === 1 ? "model has" : "models have"} no price and ${n === 1 ? "is" : "are"} blocked`;
}

export function originWords(entry: CatalogEntry): string {
  if (entry.origin === "seed") return "seeded from the configuration";
  return entry.updatedBy === null ? "added by an admin" : `by ${entry.updatedBy}`;
}

