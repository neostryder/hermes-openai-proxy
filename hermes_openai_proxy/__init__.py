"""hermes-openai-proxy.

Exposes your Hermes LLM credentials through an OpenAI-compatible HTTP API
on the local network. Designed for clients that accept an OpenAI base URL
(Brave Leo BYOM, Continue, Cline, Aider, GitHub Copilot, etc.) so they can
use any model you've configured in Hermes without you re-entering keys
into every tool.

CRITICAL CONTRACT: this proxy passes the messages array from the OpenAI
request body straight through to the underlying provider. It does NOT
inject, prepend, append, or modify any text. It does NOT read MEMORY.md,
USER.md, or any persona/skills context. Whatever you send in is what the
model sees.
"""

__version__ = "0.1.0"