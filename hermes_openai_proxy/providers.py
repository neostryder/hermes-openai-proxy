"""Provider registry and per-provider model lists.

The proxy serves a curated allowlist of model IDs per provider. We do NOT
fetch live model lists from each provider at startup -- that's expensive,
rate-limited, and changes without notice. Instead, the allowlist lives
in code and is updated when pricing or model availability changes.

Model ID format exposed by the proxy: "<provider>/<model>" -- e.g.,
"openai/gpt-4o", "anthropic/claude-sonnet-4-5", etc. The client picks
this combined string in the OpenAI `model` field.

Adding a new provider: drop a Provider() entry into PROVIDERS below.
The minimum to wire up is name, base_url, env_key_var, style, models.
For locally-served providers (LM Studio, Ollama, vLLM, llama.cpp's HTTP
server) set base_url to "http://localhost:<port>/v1" and env_key_var to
None -- credentials aren't required for localhost endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Provider:
    """One upstream provider we know how to talk to."""

    name: str  # canonical lowercase name used in the proxy
    base_url: str  # OpenAI-compatible base URL (or anthropic native)
    style: str  # "openai" or "anthropic"
    env_base_var: str | None = None  # env var that can override base_url
    env_key_var: str | None = None  # env var that holds the API key
    models: list[str] = field(default_factory=list)


# -- Provider catalog ------------------------------------------------------

PROVIDERS: dict[str, Provider] = {
    "minimax": Provider(
        name="minimax",
        base_url="https://api.minimax.io/v1",
        env_base_var="MINIMAX_BASE_URL",
        env_key_var="MINIMAX_API_KEY",
        style="openai",
        models=["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5"],
    ),
    "minimax_cn": Provider(
        name="minimax_cn",
        base_url="https://api.minimaxi.com/v1",
        env_base_var="MINIMAX_CN_BASE_URL",
        env_key_var="MINIMAX_CN_API_KEY",
        style="openai",
        models=["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5"],
    ),
    "openai": Provider(
        name="openai",
        base_url="https://api.openai.com/v1",
        env_key_var="OPENAI_API_KEY",
        style="openai",
        models=[
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "o3",
            "o4-mini",
            "o1",
        ],
    ),
    "anthropic": Provider(
        name="anthropic",
        base_url="https://api.anthropic.com",
        env_key_var="ANTHROPIC_API_KEY",
        style="anthropic",
        models=[
            "claude-sonnet-4-5",
            "claude-opus-4-1",
            "claude-haiku-4-5",
            "claude-3-7-sonnet-latest",
            "claude-3-5-sonnet-latest",
        ],
    ),
    "google": Provider(
        name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_key_var="GEMINI_API_KEY",
        style="openai",
        models=[
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
        ],
    ),
    "xai": Provider(
        name="xai",
        base_url="https://api.x.ai/v1",
        env_key_var="XAI_API_KEY",
        style="openai",
        models=["grok-4", "grok-3", "grok-3-mini", "grok-2"],
    ),
    "openrouter": Provider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        env_key_var="OPENROUTER_API_KEY",
        style="openai",
        models=[
            # OpenRouter exposes hundreds of models. This list is a small
            # curated sample covering the major providers so the /v1/models
            # endpoint has something useful to show. Add or remove freely.
            "anthropic/claude-sonnet-4",
            "anthropic/claude-opus-4",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "google/gemini-2.5-pro",
            "google/gemini-2.5-flash",
            "xai/grok-3",
            "deepseek/deepseek-chat-v3",
            "qwen/qwen-3-235b-a22b",
            "mistralai/mistral-large-latest",
            "meta-llama/llama-4-maverick",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "openrouter/owl-alpha",
        ],
    ),
    "kimi": Provider(
        name="kimi",
        base_url="https://api.moonshot.ai/v1",
        env_key_var="KIMI_API_KEY",
        style="openai",
        models=["kimi-k2-0905-preview", "moonshot-v1-128k", "moonshot-v1-32k"],
    ),
    "z.ai": Provider(
        name="z.ai",
        base_url="https://api.z.ai/api/paas/v4",
        env_key_var="GLM_API_KEY",
        style="openai",
        models=["glm-4.6", "glm-4.5", "glm-4.5-air", "glm-4-flash"],
    ),
    "deepseek": Provider(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        env_key_var="DEEPSEEK_API_KEY",
        style="openai",
        models=["deepseek-chat", "deepseek-reasoner"],
    ),
    "alibaba": Provider(
        name="alibaba",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        env_key_var="DASHSCOPE_API_KEY",
        style="openai",
        models=["qwen-max", "qwen-plus", "qwen-turbo", "qwen3-max-preview"],
    ),
    "qwen": Provider(
        name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        env_key_var="QWEN_API_KEY",
        style="openai",
        models=["qwen-max", "qwen-plus", "qwen-turbo"],
    ),
    "mistral": Provider(
        name="mistral",
        base_url="https://api.mistral.ai/v1",
        env_key_var="MISTRAL_API_KEY",
        style="openai",
        models=[
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
            "codestral-latest",
        ],
    ),
    "xiaomi": Provider(
        name="xiaomi",
        base_url="https://api.xiaomimimo.com/v1",
        env_key_var="XIAOMI_API_KEY",
        style="openai",
        models=["mimo-v2-flash", "mimo-v2"],
    ),
    "lmstudio": Provider(
        name="lmstudio",
        base_url="http://localhost:1234/v1",
        env_base_var="LM_BASE_URL",
        env_key_var="LM_API_KEY",
        style="openai",
        models=["local-model"],  # lmstudio serves whatever you load; this is a placeholder
    ),
    "ollama": Provider(
        name="ollama",
        base_url="http://localhost:11434/v1",
        env_base_var="OLLAMA_BASE_URL",
        env_key_var="OLLAMA_API_KEY",
        style="openai",
        models=["llama3.3", "qwen2.5", "mistral", "gemma3", "deepseek-r1"],
    ),
}


# -- Public API ------------------------------------------------------------


def resolve_provider(name: str, env: dict) -> Provider:
    """Look up a provider by name and apply env-var overrides for base_url."""
    p = PROVIDERS.get(name.lower())
    if p is None:
        raise KeyError(f"unknown provider: {name}")
    # Apply env overrides for base_url / api_key
    if p.env_base_var and env.get(p.env_base_var):
        p.base_url = env[p.env_base_var].rstrip("/")
    return p


def list_available_models(creds: dict[str, str]) -> list[tuple[str, Provider, str]]:
    """Return [(model_id, provider, model_id_without_provider_prefix), ...]
    for every provider that has credentials AND every model in its allowlist.

    Only includes providers in PROVIDERS that have credentials in `creds`.
    """
    out: list[tuple[str, Provider, str]] = []
    for prov_name, _has_creds in creds.items():
        prov = PROVIDERS.get(prov_name.lower())
        if prov is None:
            continue
        # Special case: github_copilot isn't an OpenAI-compat provider yet
        if prov_name == "github_copilot":
            continue
        for model in prov.models:
            model_id = f"{prov.name}/{model}"
            out.append((model_id, prov, model))
    return out


def parse_model_id(model_id: str) -> tuple[str, str]:
    """Split 'provider/model' -> (provider, model). Returns ('<provider>',
    '<model>') even if no slash present (caller can detect)."""
    if "/" in model_id:
        p, _, m = model_id.partition("/")
        return p.strip(), m.strip()
    return "", model_id.strip()