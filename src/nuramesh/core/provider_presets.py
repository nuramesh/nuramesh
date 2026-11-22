"""Provider presets for the openai-compatible adapter: data only (#1449).

A preset prefills a base URL and a model-name hint so a connection is one click or one flag.
It is not a promise about the provider: the server still applies the URL and key rules, and the
connection test says whether the endpoint answers. No prices are shipped here: an unknown price
stays recorded as unknown. No key or key name is here either (keys go through the stored-key
route or the operator's binding file). The browser keeps the same list in
`apps/app/src/models/presets.ts`; a test keeps the two equal.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    """One preset. `id` doubles as the default connection endpoint id (a slug)."""

    id: str
    label: str
    base_url: str
    model_hint: str
    local: bool
    needs_key: bool


PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        "mistral", "Mistral", "https://api.mistral.ai/v1", "mistral-large-latest", False, True
    ),
    ProviderPreset(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.5-flash",
        False,
        True,
    ),
    ProviderPreset(
        "groq", "Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", False, True
    ),
    ProviderPreset(
        "together",
        "Together AI",
        "https://api.together.xyz/v1",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        False,
        True,
    ),
    ProviderPreset("openai", "OpenAI", "https://api.openai.com/v1", "gpt-4.1", False, True),
    ProviderPreset(
        "ollama", "Ollama (local)", "http://127.0.0.1:11434/v1", "llama3.1", True, False
    ),
    ProviderPreset(
        "vllm",
        "vLLM (local)",
        "http://127.0.0.1:8000/v1",
        "the model name vLLM was started with",
        True,
        False,
    ),
    ProviderPreset(
        "lmstudio",
        "LM Studio (local)",
        "http://127.0.0.1:1234/v1",
        "the model name shown in LM Studio",
        True,
        False,
    ),
    ProviderPreset(
        "llamacpp",
        "llama.cpp server (local)",
        "http://127.0.0.1:8080/v1",
        "the model name of the loaded file",
        True,
        False,
    ),
    ProviderPreset(
        "tgi", "Text Generation Inference (local)", "http://127.0.0.1:3000/v1", "tgi", True, False
    ),
)


def get_preset(preset_id: str) -> ProviderPreset | None:
    """The preset with exactly this id, or None."""
    return next((p for p in PRESETS if p.id == preset_id), None)
