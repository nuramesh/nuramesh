/**
 * Model families, for the rule "a verifier must be a different model family from the work it
 * checks" (SPEC-app-v1 §3.1): two models from one family tend to share blind spots, so a check by
 * the same family isn't independent. A model is written `provider/name` (for example
 * `anthropic/claude-sonnet-4-6`); the family comes from the name first and the provider second.
 * Unknown models get a family of their own (provider and name stem), so a rule never fires on a
 * guess. This is a builder rule; the core's `require_different_model` is the stricter exact-model
 * check on a verifier gate.
 */

const NAME_FAMILIES: readonly (readonly [RegExp, string])[] = [
  [/^claude/, "anthropic"],
  [/^(gpt|chatgpt|o[1-9]\b|o[1-9]-|text-embedding|davinci|codex)/, "openai"],
  [/^(gemini|gemma|palm|bard)/, "google"],
  [/^(llama|codellama)/, "meta"],
  [/^(mistral|mixtral|codestral|ministral|pixtral)/, "mistral"],
  [/^(deepseek)/, "deepseek"],
  [/^(qwen|qwq)/, "alibaba"],
  [/^(command|aya|embed-)/, "cohere"],
  [/^(grok)/, "xai"],
  [/^(phi)/, "microsoft"],
  [/^(olmo|molmo)/, "allen-ai"],
];

const PROVIDER_FAMILIES: Readonly<Record<string, string>> = {
  anthropic: "anthropic",
  openai: "openai",
  google: "google",
  vertex: "google",
  mistral: "mistral",
  cohere: "cohere",
  xai: "xai",
  deepseek: "deepseek",
};

/** Weights are public, so the model can run on your own hardware (the canvas draws a dashed chip). */
const OPEN_WEIGHT_NAME = /^(llama|codellama|mistral-(7b|nemo|small-3)|mixtral|gemma|qwen|qwq|deepseek|phi|olmo|molmo)/;
const SELF_HOSTED_PROVIDERS: ReadonlySet<string> = new Set(["ollama", "vllm", "local", "self-hosted", "llamacpp"]);

function split(model: string): { provider: string; name: string } {
  const slash = model.indexOf("/");
  if (slash < 0) return { provider: "", name: model.toLowerCase() };
  return { provider: model.slice(0, slash).toLowerCase(), name: model.slice(slash + 1).toLowerCase() };
}

/** The family of a `provider/name` model, or `null` when no model is chosen. */
export function modelFamily(model: string | null): string | null {
  if (model === null || model.trim() === "") return null;
  const { provider, name } = split(model);
  for (const [pattern, family] of NAME_FAMILIES) {
    if (pattern.test(name)) return family;
  }
  const byProvider = PROVIDER_FAMILIES[provider];
  if (byProvider !== undefined) return byProvider;
  // Unknown: the provider and the name without version digits ("fake/dev-1" and "fake/dev-2" match).
  const stem = name.replace(/[-_.]?\d[\w.]*$/, "");
  return `${provider === "" ? "model" : provider}:${stem === "" ? name : stem}`;
}

/** True for models whose weights are public or that run on the user's own hardware. */
export function isOpenWeight(model: string | null): boolean {
  if (model === null || model.trim() === "") return false;
  const { provider, name } = split(model);
  return SELF_HOSTED_PROVIDERS.has(provider) || OPEN_WEIGHT_NAME.test(name);
}

/** The model an agent runs: its own, or its role's default. */
export function effectiveModel(
  agent: { model: string | null },
  role: { default_model: string | null } | undefined,
): string | null {
  return agent.model ?? role?.default_model ?? null;
}
