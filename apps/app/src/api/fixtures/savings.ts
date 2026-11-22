import type { RawTokenRow, RawTokenSavings, TokenGroupBy } from "../savings";

const row = (group: string, over: Partial<RawTokenRow>): RawTokenRow => ({
  group,
  calls: 0,
  unknown_calls: 0,
  known_total: "0",
  complete: true,
  input_tokens: 0,
  output_tokens: 0,
  cached_input_tokens: 0,
  cache_write_tokens: 0,
  calls_without_usage: 0,
  cache_hit_rate: null,
  cache_saving: "0",
  cache_write_premium: "0",
  net_cache_saving: "0",
  unknown_saving_calls: 0,
  saving_complete: true,
  cache_saving_share: null,
  ...over,
});

const cached = row("anthropic/claude-sonnet", {
  calls: 2,
  known_total: "0.0042372",
  input_tokens: 20,
  output_tokens: 2,
  cached_input_tokens: 1024,
  cache_write_tokens: 1024,
  cache_hit_rate: "0.495164",
  cache_saving: "0.0027648",
  cache_write_premium: "0.000768",
  net_cache_saving: "0.0019968",
  cache_saving_share: "0.320308",
});
const plain = row("azure-openai/gpt-5.4-mini", {
  calls: 3,
  known_total: "0.006",
  input_tokens: 3000,
  output_tokens: 600,
  cache_hit_rate: "0.000000",
  cache_saving_share: "0.000000",
});
const unpriced = row("lab/mystery", {
  calls: 2,
  unknown_calls: 1,
  known_total: "0.00045",
  complete: false,
  input_tokens: 100,
  output_tokens: 10,
  cached_input_tokens: 500,
  calls_without_usage: 1,
  cache_hit_rate: "0.833333",
  unknown_saving_calls: 2,
  saving_complete: false,
});

const total = (rows: RawTokenRow[]): RawTokenRow =>
  row("all", {
    calls: rows.reduce((n, r) => n + r.calls, 0),
    unknown_calls: 1,
    complete: false,
    unknown_saving_calls: 2,
    saving_complete: false,
    input_tokens: rows.reduce((n, r) => n + r.input_tokens, 0),
    output_tokens: rows.reduce((n, r) => n + r.output_tokens, 0),
    cached_input_tokens: rows.reduce((n, r) => n + r.cached_input_tokens, 0),
    cache_write_tokens: rows.reduce((n, r) => n + r.cache_write_tokens, 0),
    calls_without_usage: 1,
    known_total: "0.0106872",
    cache_saving: "0.0027648",
    cache_write_premium: "0.000768",
    net_cache_saving: "0.0019968",
  });

/** Sample data for the Savings tab: a cached model, a model with no cache and one with unknown prices. */
export function tokenSavingsFixture(groupBy: TokenGroupBy): RawTokenSavings {
  const models = [cached, plain, unpriced];
  return {
    organization: "My organization",
    currency: "USD",
    as_of: "2026-10-26T12:00:00Z",
    from: "2026-10-01T00:00:00Z",
    to: "2026-11-01T00:00:00Z",
    group_by: groupBy,
    rows: groupBy === "none" ? [total(models)] : models.map((r) => ({ ...r })),
  };
}
