"""Read token usage from an LLM API response and normalize it (record 0012 item 6).

Providers count tokens differently. After normalization every call has the
same meaning per field:

- ``input_tokens``: prompt tokens **not** served from cache;
- ``output_tokens``: generated tokens, reasoning ("thinking") tokens included;
- ``cache_read_tokens`` / ``cache_write_tokens``: prompt tokens read from or
  written to the provider's cache, ``None`` when the API reports no such count.

Where the counts come from (input is always the uncached part):

- OpenAI Chat Completions, and the OpenAI-style endpoints of vLLM, Ollama and
  llama.cpp: ``prompt_tokens`` minus ``prompt_tokens_details.cached_tokens``
  (DeepSeek: ``prompt_cache_hit_tokens``); Mistral, Groq, Together, Fireworks,
  OpenRouter and xAI use this format too;
- OpenAI Responses: ``input_tokens`` minus ``input_tokens_details.cached_tokens``;
- Anthropic Messages: ``input_tokens`` already excludes the cache, which is
  ``cache_read_input_tokens`` and ``cache_creation_input_tokens``;
- Google Gemini: ``promptTokenCount`` minus ``cachedContentTokenCount``;
  thinking (``thoughtsTokenCount``) is added to the output;
- Ollama's own API: ``prompt_eval_count`` and ``eval_count``, no cache count;
- llama.cpp ``timings``: ``prompt_n`` evaluated and ``cache_n`` reused tokens.

The body can be one JSON document, a server-sent event stream or (Ollama)
JSON lines. Streams report usage across several events, so the latest value of
each field wins. Only numbers are kept from the response: never text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

Number = int | float


@dataclass
class Usage:
    model: str | None = None
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    raw: dict[str, Number] = field(default_factory=dict)

    @property
    def found(self) -> bool:
        """Whether the response carried any token counts."""
        return bool(self.raw)


# --- reading documents -------------------------------------------------------------


def documents(body: bytes) -> list[dict[str, Any]]:
    """The JSON objects in a response body: one document, SSE ``data:`` lines or JSON lines."""
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return []
    try:
        doc = json.loads(text)
    except ValueError:
        pass
    else:
        return [d for d in (doc if isinstance(doc, list) else [doc]) if isinstance(d, dict)]
    docs: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            docs.append(doc)
    return docs or list(_salvage(text))


_USAGE_KEY = re.compile(r'"(usage|usageMetadata|timings)"\s*:\s*\{')
_MODEL_KEY = re.compile(r'"(?:model|modelVersion)"\s*:\s*"([^"\\]{1,200})"')


def _salvage(text: str) -> Iterator[dict[str, Any]]:
    """Usage objects from a body that is not valid JSON as a whole, e.g. one
    too large to keep in full (only its start and end were kept)."""
    decoder = json.JSONDecoder()
    model = _MODEL_KEY.search(text)
    if model:
        yield {"model": model.group(1)}
    for match in _USAGE_KEY.finditer(text):
        try:
            value, _ = decoder.raw_decode(text, match.end() - 1)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield {match.group(1): value}


# --- collecting counts ----------------------------------------------------------------


def _numbers(prefix: str, value: Any, out: dict[str, Number]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        out[prefix] = value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _numbers(f"{prefix}.{key}", item, out)


_OLLAMA_FIELDS = ("prompt_eval_count", "eval_count")


def _containers(doc: Mapping[str, Any]) -> Iterator[tuple[str, Any]]:
    """(name, usage object) pairs found in one document."""
    yield "usage", doc.get("usage")
    message = doc.get("message")
    if isinstance(message, Mapping):  # Anthropic stream: message_start
        yield "usage", message.get("usage")
    response = doc.get("response")
    if isinstance(response, Mapping):  # OpenAI Responses stream: response.completed
        yield "usage", response.get("usage")
    yield "usageMetadata", doc.get("usageMetadata")  # Gemini
    yield "timings", doc.get("timings")  # llama.cpp
    if any(name in doc for name in _OLLAMA_FIELDS):  # Ollama native API
        yield "ollama", {name: doc[name] for name in _OLLAMA_FIELDS if name in doc}


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _anthropic(doc: Mapping[str, Any]) -> bool:
    return str(doc.get("type", "")).startswith("message")


def extract_usage(body: bytes) -> Usage:
    """Model, response id and normalized token counts from a response body."""
    usage = Usage()
    anthropic = False
    for doc in documents(body):
        message: Mapping[str, Any] = _mapping(doc.get("message"))
        response: Mapping[str, Any] = _mapping(doc.get("response"))
        usage.model = (
            _first_str(
                doc.get("model"),
                message.get("model"),
                response.get("model"),
                doc.get("modelVersion"),
            )
            or usage.model
        )
        usage.request_id = (
            _first_str(response.get("id"), message.get("id"), doc.get("id")) or usage.request_id
        )
        anthropic = anthropic or _anthropic(doc)
        for name, container in _containers(doc):
            if isinstance(container, Mapping):
                _numbers(name, container, usage.raw)
    _normalize(usage, anthropic)
    return usage


# --- normalizing ----------------------------------------------------------------------


def _int(raw: Mapping[str, Number], key: str) -> int | None:
    value = raw.get(key)
    return int(value) if value is not None else None


def _minus(total: int | None, part: int | None) -> int | None:
    if total is None:
        return None
    return max(total - (part or 0), 0)


def _normalize(u: Usage, anthropic: bool) -> None:
    raw = u.raw
    if "usage.prompt_tokens" in raw or "usage.completion_tokens" in raw:  # Chat Completions
        cached = next(  # DeepSeek names it prompt_cache_hit_tokens, in either place
            (
                value
                for key in (
                    "usage.prompt_tokens_details.cached_tokens",
                    "usage.prompt_cache_hit_tokens",
                    "usage.prompt_tokens_details.prompt_cache_hit_tokens",
                )
                if (value := _int(raw, key)) is not None
            ),
            None,
        )
        u.input_tokens = _minus(_int(raw, "usage.prompt_tokens"), cached)
        u.output_tokens = _int(raw, "usage.completion_tokens")
        u.reasoning_tokens = _int(raw, "usage.completion_tokens_details.reasoning_tokens")
        u.cache_read_tokens = cached
    elif anthropic or any(k.startswith("usage.cache_") for k in raw):  # Anthropic Messages
        u.input_tokens = _int(raw, "usage.input_tokens")
        u.output_tokens = _int(raw, "usage.output_tokens")
        u.cache_read_tokens = _int(raw, "usage.cache_read_input_tokens")
        u.cache_write_tokens = _int(raw, "usage.cache_creation_input_tokens")
    elif "usage.input_tokens" in raw or "usage.output_tokens" in raw:  # OpenAI Responses
        cached = _int(raw, "usage.input_tokens_details.cached_tokens")
        u.input_tokens = _minus(_int(raw, "usage.input_tokens"), cached)
        u.output_tokens = _int(raw, "usage.output_tokens")
        u.reasoning_tokens = _int(raw, "usage.output_tokens_details.reasoning_tokens")
        u.cache_read_tokens = cached
    elif any(k.startswith("usageMetadata.") for k in raw):  # Gemini omits zero counts
        cached = _int(raw, "usageMetadata.cachedContentTokenCount") or 0
        thoughts = _int(raw, "usageMetadata.thoughtsTokenCount") or 0
        u.input_tokens = _minus(_int(raw, "usageMetadata.promptTokenCount"), cached)
        u.output_tokens = (_int(raw, "usageMetadata.candidatesTokenCount") or 0) + thoughts
        u.reasoning_tokens = thoughts
        u.cache_read_tokens = cached
    elif "ollama.prompt_eval_count" in raw or "ollama.eval_count" in raw:
        u.input_tokens = _int(raw, "ollama.prompt_eval_count")
        u.output_tokens = _int(raw, "ollama.eval_count")
    if "timings.prompt_n" in raw and "timings.cache_n" in raw:  # llama.cpp says what it reused
        u.input_tokens = _int(raw, "timings.prompt_n")
        u.cache_read_tokens = _int(raw, "timings.cache_n")
        if u.output_tokens is None:
            u.output_tokens = _int(raw, "timings.predicted_n")
