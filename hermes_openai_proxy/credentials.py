"""Credentials loader.

Reads API keys from $HERMES_HOME/.env (default: ~/.hermes/.env).
Never logs values; never returns masked values; only returns the dict.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def env_path() -> Path:
    return hermes_home() / ".env"


_ENV_LINE_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")


def load_env() -> dict[str, str]:
    """Parse .env into a dict. Comments (lines starting with '#') and blank
    lines are skipped. Values wrapped in single or double quotes have the
    quotes stripped."""
    p = env_path()
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if v and v[0] in ("'", '"') and v[-1] == v[0]:
            v = v[1:-1]
        out[k] = v
    return out


def find_credentials() -> dict[str, str]:
    """Walk the .env dict and return {provider: api_key} for every provider
    that has a key set AND a non-empty value. Also includes the raw base
    URLs for each provider via a parallel dict (see providers.py)."""
    env = load_env()
    creds: dict[str, str] = {}
    mapping = {
        "minimax": "MINIMAX_API_KEY",
        "minimax_cn": "MINIMAX_CN_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GEMINI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "xai": "XAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "kimi": "KIMI_API_KEY",
        "z.ai": "GLM_API_KEY",
        "glm": "GLM_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "alibaba": "DASHSCOPE_API_KEY",
        "dashscope": "DASHSCOPE_API_KEY",
        "qwen": "QWEN_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "xiaomi": "XIAOMI_API_KEY",
        "mimo": "XIAOMI_API_KEY",
        "novita": "NOVITA_API_KEY",
        "kilocode": "KILOCODE_API_KEY",
        "opencode_zen": "OPENCODE_ZEN_API_KEY",
        "opencode_go": "OPENCODE_GO_API_KEY",
    }
    for provider, env_key in mapping.items():
        val = env.get(env_key, "").strip()
        if val and not val.lower().startswith("#"):
            creds[provider] = val
    # LM Studio / Ollama local counts as 'has credentials' if LM_BASE_URL is set
    if env.get("LM_BASE_URL"):
        creds["lmstudio"] = env.get("LM_API_KEY", "lm-studio")
    if env.get("OLLAMA_BASE_URL") or env.get("LM_BASE_URL", "").startswith("http"):
        creds["ollama"] = "ollama"
    # GitHub Copilot OAuth-style (just presence of token, not validated)
    if env.get("COPILOT_GITHUB_TOKEN"):
        creds["github_copilot"] = env.get("COPILOT_GITHUB_TOKEN", "")
    return creds