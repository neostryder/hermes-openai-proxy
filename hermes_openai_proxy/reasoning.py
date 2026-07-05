"""Reasoning-block stripper for streaming and non-streaming LLM responses.

Different providers emit "chain-of-thought" reasoning differently:

  - Anthropic with thinking enabled:    separate thinking content blocks
  - DeepSeek R1 / QwQ:                  inline  <think>...</think>
  - MiniMax M3 (and similar "thinking mode" models): BOTH inline  <think>
                                          tags AND a parallel `reasoning`
                                          channel in the streaming delta

This module handles all three forms. Callers feed it either:

  (a) The full assistant text at once:    ``strip_reasoning(text) -> str``
  (b) A running buffer of streamed deltas: ``ReasoningFilter`` (stateful)

The streaming filter handles three subtlety cases:

  1. Tags that span multiple deltas (a "think" tag opened in delta N
     may not close until delta N+5). We buffer until we find the close.
  2. Tags nested or interleaved with normal text. We emit only the visible
     portions.
  3. Providers that emit reasoning as a separate `reasoning` field on each
     delta instead of inline in `content`. The filter exposes a
     ``consume_reasoning_field`` method for that path.

The non-streaming filter uses a single regex sweep, which is good enough
for full text.
"""

from __future__ import annotations

import re

# Tag names that some providers (MiniMax, Anthropic thinking, DeepSeek,
# Qwen, GLM thinking, Kimi k2) use to inline reasoning inside the same
# content stream. Built from a list so the literal angle brackets never
# appear as a token in this file's source.
_REASONING_TAGS = ["think", "reasoning", "antml_thinking", "reflection", "thought"]
_TAGS_GROUP = "|".join(_REASONING_TAGS)
_REASONING_OPEN_RE = re.compile(
    r"<(" + _TAGS_GROUP + r")\b[^>]*>", re.IGNORECASE
)
_REASONING_CLOSE_RE = re.compile(
    r"</(" + _TAGS_GROUP + r")>", re.IGNORECASE
)
_REASONING_FULL_RE = re.compile(
    r"<(" + _TAGS_GROUP + r")\b[^>]*>.*?</(" + _TAGS_GROUP + r")>",
    re.DOTALL | re.IGNORECASE,
)


def strip_reasoning(text: str) -> str:
    """Remove complete inline reasoning blocks from a full response.
    Returns the cleaned text."""
    if not text:
        return text
    return _REASONING_FULL_RE.sub("", text).strip()


class ReasoningFilter:
    """Stateful filter for streaming responses.

    Usage::

        f = ReasoningFilter()
        for delta in upstream_stream:
            visible = f.feed(delta)
            if visible:
                client.send(visible)
        tail = f.flush()
        if tail:
            client.send(tail)

    If the upstream also exposes reasoning via a separate field per delta
    (MiniMax M3 does this: ``delta.reasoning`` in addition to ``delta.content``),
    call ``consume_reasoning_field(text)`` for that text -- it's dropped
    silently. (OpenAI standard has no such field; the stripper handles it
    when present.)
    """

    def __init__(self) -> None:
        self._buffer: str = ""
        self._in_reasoning: bool = False

    def consume_reasoning_field(self, text: str) -> None:
        """Drop reasoning text that arrives on a parallel channel.
        MiniMax M3 puts thinking text in delta.reasoning. We never emit it."""
        if text:
            # Intentionally do nothing with the text -- we drop it.
            return

    def feed(self, chunk: str) -> str:
        """Feed a delta chunk. Returns the visible (non-reasoning) text to
        emit. Empty string means "nothing visible yet, keep buffering"."""
        if not chunk:
            return ""
        self._buffer += chunk

        emitted: list[str] = []

        # Drain as much of the buffer as possible, in order:
        #   1. If we're in reasoning mode, look for the next CLOSE.
        #      Drop everything through the close; resume visible mode.
        #   2. If we're in visible mode, look for the next OPEN.
        #      Emit any visible text before it; mark ourselves inside a block.
        # We loop until no progress can be made (waiting for more deltas).
        while self._buffer:
            if self._in_reasoning:
                close_match = _REASONING_CLOSE_RE.search(self._buffer)
                if not close_match:
                    # No close yet -- keep buffering until more deltas arrive.
                    break
                self._buffer = self._buffer[close_match.end():]
                self._in_reasoning = False
                # Loop continues: there may be more visible text or another block.
            else:
                open_match = _REASONING_OPEN_RE.search(self._buffer)
                if not open_match:
                    # No opening tag visible anywhere; safe to emit the buffer.
                    emitted.append(self._buffer)
                    self._buffer = ""
                    break
                # Emit any visible text before the OPEN
                pre = self._buffer[: open_match.start()]
                if pre:
                    emitted.append(pre)
                # Drop the OPEN itself; mark ourselves inside a block.
                self._buffer = self._buffer[open_match.end():]
                self._in_reasoning = True
                # Loop continues: now we look for the close.

        return "".join(emitted)

    def flush(self) -> str:
        """End of stream. Return any remaining visible text. If we ended
        mid-reasoning-block (no close came), drop the buffer -- never leak
        partial reasoning to the client."""
        if not self._buffer:
            return ""
        if self._in_reasoning:
            # Mid-reasoning, no close arrived. Drop it.
            self._buffer = ""
            return ""
        # Visible-mode leftover. Emit.
        out = self._buffer
        self._buffer = ""
        return out