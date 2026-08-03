"""LLM client helpers — schema-enforced chat, embeddings, and call logging.

Gotchas this module encodes (do not "simplify" them away):
- oMLX: response_format MUST include type="json_schema" AND a name, or
  enforcement is silently disabled and the model free-talks.
- oMLX: thinking is ON by default and must be disabled via chat_template_kwargs
  (extra_body); "/no_think" in the prompt is ignored by the server.
- DeepSeek (models named "deepseek*"): json_schema response_format is rejected
  ("unavailable now", probed 2026-07-17, every model, /v1 and /beta), and
  strict tool calling is NOT real grammar enforcement — on full-document
  extraction it returned schema-invalid args (dropped the required "relations"
  key, finish_reason=tool_calls) and cut output richness ~5x vs json_object.
  So: json_object + schema-in-prompt + compact-JSON instruction is the primary
  path; callers' pydantic validation is the gate. Thinking is ON by default and
  must be disabled via {"thinking": {"type": "disabled"}} or every call burns
  reasoning tokens. Full reference: wiki/deepseek-api.md.
  deepseek-chat/-reasoner die 2026-07-24; use deepseek-v4-flash.
"""

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from openai import OpenAI

BASE_URL = os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8000/v1")
# "unset" placeholder keeps the module importable offline; real key comes from .env
API_KEY = os.environ.get("OMLX_API_KEY") or "unset"
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
LOG_DIR = Path("logs/llm")

_client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=600)
_deepseek_client: OpenAI | None = None
_log_lock = threading.Lock()


def _provider(model: str) -> tuple[OpenAI, bool]:
    """(client, schema_native) for a model. schema_native=False → json_object path."""
    global _deepseek_client
    if model.startswith("deepseek"):
        if _deepseek_client is None:
            key = os.environ.get("DEEPSEEK_API_KEY", "")
            if not key:
                raise RuntimeError("DEEPSEEK_API_KEY not set (see .env)")
            # No RPM/TPM limits, only concurrency (429 when over) — let the SDK
            # absorb transient 429s with its exponential backoff.
            _deepseek_client = OpenAI(
                base_url=DEEPSEEK_BASE_URL, api_key=key, timeout=600, max_retries=5
            )
        return _deepseek_client, False
    return _client, True


def _schema_suffix(schema: dict) -> str:
    """System-prompt tail for the json_object path (no grammar enforcement)."""
    return (
        "\n\nRespond with a single JSON object (no prose, no markdown fences) "
        "that validates against this JSON Schema. Emit compact JSON — no "
        "indentation or newlines; whitespace wastes your output budget.\n"
        f"{json.dumps(schema)}"
    )


def _usage(resp) -> dict:
    """Token usage for the call log; cache fields confirm prefix-cache hits."""
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    d = {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens}
    for k in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        v = getattr(u, k, None)
        if v is not None:
            d[k] = v
    return d


def _complete(
    model: str,
    messages: list[dict],
    schema_name: str,
    schema: dict,
    temperature: float,
    max_tokens: int,
) -> tuple[str, dict]:
    """Provider-routed schema-enforced completion → (raw_json_text, usage)."""
    client, schema_native = _provider(model)
    if schema_native:  # oMLX: grammar-enforced response_format
        resp = client.chat.completions.create(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema},
            },
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        return resp.choices[0].message.content or "", _usage(resp)
    # DeepSeek: json_object + schema in system prompt (strict tools tested and
    # rejected — see module docstring); thinking must be disabled explicitly.
    if messages and messages[0].get("role") == "system":
        messages = [
            {**messages[0], "content": messages[0]["content"] + _schema_suffix(schema)},
            *messages[1:],
        ]
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=messages,
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "disabled"}},
    )
    return resp.choices[0].message.content or "", _usage(resp)


def _log(command: str, record: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{command}-{datetime.now(UTC):%Y%m%d}.jsonl"
    line = json.dumps({"ts": datetime.now(UTC).isoformat(), **record}, ensure_ascii=False)
    with _log_lock, path.open("a") as f:
        f.write(line + "\n")


def chat_json(
    *,
    command: str,
    model: str,
    system: str,
    user: str,
    schema_name: str,
    schema: dict,
    prompt_version: str,
    temperature: float = 0.0,
    max_tokens: int = 8192,
) -> dict:
    """Schema-enforced chat completion. Returns the parsed JSON object.

    Raises ValueError on unparseable output (callers record status='schema_error').
    """
    raw, usage = _complete(
        model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        schema_name, schema, temperature, max_tokens,
    )
    _log(command, {"model": model, "prompt_version": prompt_version, "usage": usage,
                   "user_prefix": user[:500], "raw_response": raw})
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"unparseable LLM output: {raw[:200]}") from e


def chat_json_messages(
    *,
    command: str,
    model: str,
    messages: list[dict],
    schema_name: str,
    schema: dict,
    prompt_version: str,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> dict:
    """Multi-turn variant of chat_json for agent loops (Phase 2 critic).

    Caller owns the full message list (system + alternating turns). Same schema
    enforcement, logging, and ValueError contract as chat_json.
    """
    raw, usage = _complete(model, messages, schema_name, schema, temperature, max_tokens)
    _log(command, {"model": model, "prompt_version": prompt_version, "usage": usage,
                   "n_messages": len(messages),
                   "last_user_prefix": str(messages[-1].get("content", ""))[:500],
                   "raw_response": raw})
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"unparseable LLM output: {raw[:200]}") from e


def embed(texts: list[str], model: str, command: str = "embed") -> list[list[float]]:
    """Batch-embed texts; preserves input order. Logs counts, not vectors."""
    out: list[list[float]] = []
    for i in range(0, len(texts), 64):
        resp = _client.embeddings.create(model=model, input=texts[i : i + 64])
        out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
    _log(command, {"model": model, "n_inputs": len(texts), "sample": texts[:5]})
    return out
