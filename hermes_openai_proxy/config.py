"""Reads Hermes's config.yaml to find the pinned default model + provider.

The proxy pins its default to whatever `model.default` and `model.provider`
are set in Hermes. The client can override via the OpenAI `model` field,
but if the client sends nothing (or sends the pinned default), the proxy
uses this value.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Tuple


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def config_path() -> Path:
    return hermes_home() / "config.yaml"


def read_default_model() -> Tuple[str, str]:
    """Return (model_id_with_provider_prefix, provider_name).

    Mirrors ask.py's precedence rules. Order:
      1. model.default + model.provider from config.yaml.
      2. Ask-specific overrides (ask.default_model + ask.default_provider).
      3. The first credentialled provider + its first model. This keeps
         the proxy functional for any Hermes user without hardcoding a
         specific vendor as the default. If you use MiniMax, OpenAI,
         Anthropic, or anyone else, the right key in your .env makes the
         right model the default automatically.
      4. None (returned as empty string) -- the proxy will pick the
         first model from /v1/models at request time.

    The model string is normalized: if it has no '/', prefix with the
    provider name so it becomes 'provider/model'.
    """
    from .credentials import find_credentials  # local import to avoid cycle
    from .providers import PROVIDERS

    p = config_path()
    text = ""
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""

    # Regex the scalars out of the YAML. We accept either top-level
    # (e.g. "model: foo") or one-level-nested (e.g. "model:\\n  default: foo").
    # Multi-key nested blocks: when searching for `model.provider` we must
    # skip over any sibling keys (`model.default`, `model.base_url`, ...)
    # before reaching the desired key, so the regex matches the key no
    # matter its order in the YAML block.
    def grab_scalar(path: Tuple[str, ...]) -> str:
        if len(path) == 1:
            r = re.search(
                rf"^{re.escape(path[0])}:\s*(\S+)\s*$", text, re.MULTILINE
            )
        else:
            sec, key = path
            # Anchor at the section header; then allow any indented lines
            # before our target key (skipping siblings).
            r = re.search(
                rf"^{re.escape(sec)}:\s*\n(?:[ \t]+\S.*\n)*[ \t]+{re.escape(key)}:\s*(\S+)\s*$",
                text,
                re.MULTILINE,
            )
        if not r:
            return ""
        v = r.group(1).strip()
        if v.startswith(('"', "'")) and v.endswith(v[0]):
            v = v[1:-1]
        return v

    ask_model = grab_scalar(("ask", "default_model"))
    ask_provider = grab_scalar(("ask", "default_provider"))
    model = grab_scalar(("model", "default"))
    provider = grab_scalar(("model", "provider"))

    chosen_model = ask_model or model
    chosen_provider = ask_provider or provider

    if chosen_model and chosen_provider:
        if "/" not in chosen_model:
            chosen_model = f"{chosen_provider}/{chosen_model}"
        return chosen_model, chosen_provider

    # No config.yaml directive. Pick from the first credentialled provider.
    # Generic across vendors -- anyone with a key in .env gets a sensible
    # default without us hardcoding MiniMax, OpenAI, or anyone else.
    creds = find_credentials()
    for prov_name in sorted(creds.keys()):
        prov = PROVIDERS.get(prov_name.lower())
        if prov and prov.models:
            return f"{prov.name}/{prov.models[0]}", prov.name

    # Nothing available. Return empty; the proxy will handle the no-default
    # case explicitly at request time rather than guessing wrong.
    return "", ""