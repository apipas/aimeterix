"""Token counts from provider responses, normalized (record 0012 item 6).

The bodies are shaped like each provider's documented responses; the text in
them is placeholder only.
"""

from __future__ import annotations

import json
from typing import Any

from ai_meterix.collectors.usage import documents, extract_usage


def sse(*events: dict[str, Any], done: bool = False) -> bytes:
    lines = [f"event: {e.get('type', 'x')}\ndata: {json.dumps(e)}\n\n" for e in events]
    return ("".join(lines) + ("data: [DONE]\n\n" if done else "")).encode()


def counts(body: bytes) -> tuple[Any, ...]:
    u = extract_usage(body)
    return (
        u.model,
        u.input_tokens,
        u.output_tokens,
        u.cache_read_tokens,
        u.cache_write_tokens,
        u.reasoning_tokens,
    )


def test_openai_chat_subtracts_cached_tokens_from_input() -> None:
    body = {
        "id": "chatcmpl-1",
        "model": "gpt-x",
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "total_tokens": 1500,
            "prompt_tokens_details": {"cached_tokens": 1024},
            "completion_tokens_details": {"reasoning_tokens": 120},
        },
    }
    u = extract_usage(json.dumps(body).encode())
    assert counts(json.dumps(body).encode()) == ("gpt-x", 176, 300, 1024, None, 120)
    assert u.request_id == "chatcmpl-1"
    assert u.raw["usage.prompt_tokens_details.cached_tokens"] == 1024


def test_openai_compatible_server_without_cache_details_reports_no_cache() -> None:
    body = {"model": "llama3", "usage": {"prompt_tokens": 12, "completion_tokens": 30}}
    assert counts(json.dumps(body).encode()) == ("llama3", 12, 30, None, None, None)


def test_openai_chat_stream_takes_usage_from_the_last_chunk() -> None:
    body = sse(
        {"id": "c1", "model": "gpt-x", "choices": [{"delta": {"content": "."}}]},
        {
            "id": "c1",
            "model": "gpt-x",
            "choices": [],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        },
        done=True,
    )
    assert counts(body) == ("gpt-x", 50, 7, 0, None, None)


def test_openai_stream_without_usage_is_not_found() -> None:
    body = sse({"model": "gpt-x", "choices": [{"delta": {"content": "."}}]}, done=True)
    u = extract_usage(body)
    assert u.model == "gpt-x"
    assert not u.found
    assert u.input_tokens is None


def test_openai_responses_api() -> None:
    body = {
        "id": "resp_1",
        "object": "response",
        "model": "gpt-x",
        "usage": {
            "input_tokens": 2000,
            "input_tokens_details": {"cached_tokens": 1500},
            "output_tokens": 400,
            "output_tokens_details": {"reasoning_tokens": 250},
        },
    }
    assert counts(json.dumps(body).encode()) == ("gpt-x", 500, 400, 1500, None, 250)


def test_openai_responses_stream_reads_the_completed_event() -> None:
    body = sse(
        {"type": "response.created", "response": {"id": "resp_2", "model": "gpt-x"}},
        {"type": "response.output_text.delta", "delta": "."},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_2",
                "model": "gpt-x",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    )
    u = extract_usage(body)
    assert counts(body) == ("gpt-x", 10, 5, None, None, None)
    assert u.request_id == "resp_2"


def test_anthropic_message_keeps_input_and_reads_both_cache_counts() -> None:
    body = {
        "id": "msg_1",
        "type": "message",
        "model": "claude-x",
        "usage": {
            "input_tokens": 40,
            "cache_creation_input_tokens": 2000,
            "cache_read_input_tokens": 8000,
            "output_tokens": 512,
        },
    }
    assert counts(json.dumps(body).encode()) == ("claude-x", 40, 512, 8000, 2000, None)


def test_anthropic_stream_takes_the_final_output_count() -> None:
    body = sse(
        {
            "type": "message_start",
            "message": {
                "id": "msg_2",
                "model": "claude-x",
                "usage": {
                    "input_tokens": 25,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 100,
                    "output_tokens": 1,
                },
            },
        },
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "."}},
        {"type": "message_delta", "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    )
    u = extract_usage(body)
    assert counts(body) == ("claude-x", 25, 42, 100, 0, None)
    assert u.request_id == "msg_2"


def test_gemini_subtracts_cache_and_counts_thinking_as_output() -> None:
    body = {
        "modelVersion": "gemini-x",
        "usageMetadata": {
            "promptTokenCount": 1000,
            "cachedContentTokenCount": 600,
            "candidatesTokenCount": 80,
            "thoughtsTokenCount": 20,
        },
    }
    assert counts(json.dumps(body).encode()) == ("gemini-x", 400, 100, 600, None, 20)


def test_gemini_omits_zero_counts() -> None:
    body = {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3}}
    assert counts(json.dumps(body).encode())[1:4] == (10, 3, 0)


def test_ollama_native_stream_reads_the_done_line() -> None:
    lines = [
        {"model": "llama3.1:8b", "message": {"role": "assistant", "content": "."}, "done": False},
        {"model": "llama3.1:8b", "done": True, "prompt_eval_count": 26, "eval_count": 290},
    ]
    body = "\n".join(json.dumps(line) for line in lines).encode()
    assert counts(body) == ("llama3.1:8b", 26, 290, None, None, None)


def test_llama_cpp_timings_give_the_reused_prompt_tokens() -> None:
    body = {
        "model": "qwen",
        "usage": {"prompt_tokens": 1500, "completion_tokens": 64},
        "timings": {"prompt_n": 300, "cache_n": 1200, "predicted_n": 64, "prompt_ms": 80.5},
    }
    u = extract_usage(json.dumps(body).encode())
    assert counts(json.dumps(body).encode()) == ("qwen", 300, 64, 1200, None, None)
    assert u.raw["timings.prompt_ms"] == 80.5


def test_llama_cpp_stream_with_timings_only() -> None:
    body = sse(
        {"model": "qwen", "choices": [{"delta": {"content": "."}}]},
        {
            "model": "qwen",
            "choices": [],
            "timings": {"prompt_n": 9, "cache_n": 0, "predicted_n": 4},
        },
        done=True,
    )
    assert counts(body) == ("qwen", 9, 4, 0, None, None)


def test_raw_usage_keeps_numbers_only() -> None:
    body = {"model": "m", "usage": {"prompt_tokens": 1, "completion_tokens": 2, "note": "text"}}
    raw = extract_usage(json.dumps(body).encode()).raw
    assert raw == {"usage.prompt_tokens": 1, "usage.completion_tokens": 2}


def test_a_truncated_large_body_still_gives_its_usage() -> None:
    start = b'{"model": "emb-x", "data": [{"embedding": [0.1, 0.2'
    end = b'0.3]}], "usage": {"prompt_tokens": 7, "total_tokens": 7}}'
    assert counts(start + b"\n" + end)[:3] == ("emb-x", 7, None)


def test_bodies_that_are_not_json() -> None:
    assert documents(b"") == []
    assert not extract_usage(b"<html>bad gateway</html>").found
