/**
 * Provider presets for the openai-compatible adapter: data only (#1449). A preset prefills the base
 * URL and a model-name hint; it ships no price (an unknown price stays unknown) and no key. The
 * Python copy is `nuramesh.core.provider_presets`; a test keeps the two equal. Local servers use
 * 127.0.0.1 because the server accepts plain http only to a loopback address.
 */
export interface ProviderPreset {
  id: string;
  label: string;
  baseUrl: string;
  modelHint: string;
  local: boolean;
  needsKey: boolean;
}

// prettier-ignore
export const PRESETS: readonly ProviderPreset[] = [
  { id: "mistral", label: "Mistral", baseUrl: "https://api.mistral.ai/v1", modelHint: "mistral-large-latest", local: false, needsKey: true },
  { id: "gemini", label: "Google Gemini", baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai", modelHint: "gemini-2.5-flash", local: false, needsKey: true },
  { id: "groq", label: "Groq", baseUrl: "https://api.groq.com/openai/v1", modelHint: "llama-3.3-70b-versatile", local: false, needsKey: true },
  { id: "together", label: "Together AI", baseUrl: "https://api.together.xyz/v1", modelHint: "meta-llama/Llama-3.3-70B-Instruct-Turbo", local: false, needsKey: true },
  { id: "openai", label: "OpenAI", baseUrl: "https://api.openai.com/v1", modelHint: "gpt-4.1", local: false, needsKey: true },
  { id: "ollama", label: "Ollama (local)", baseUrl: "http://127.0.0.1:11434/v1", modelHint: "llama3.1", local: true, needsKey: false },
  { id: "vllm", label: "vLLM (local)", baseUrl: "http://127.0.0.1:8000/v1", modelHint: "the model name vLLM was started with", local: true, needsKey: false },
  { id: "lmstudio", label: "LM Studio (local)", baseUrl: "http://127.0.0.1:1234/v1", modelHint: "the model name shown in LM Studio", local: true, needsKey: false },
  { id: "llamacpp", label: "llama.cpp server (local)", baseUrl: "http://127.0.0.1:8080/v1", modelHint: "the model name of the loaded file", local: true, needsKey: false },
  { id: "tgi", label: "Text Generation Inference (local)", baseUrl: "http://127.0.0.1:3000/v1", modelHint: "tgi", local: true, needsKey: false },
];
