# DeepSeek API reference (LitGraph)

Live-verified **2026-07-17** against `api.deepseek.com` with our API key. Each claim tagged
**[live]** (probed) or **[docs]** (from api-docs.deepseek.com, not independently confirmed).

## TL;DR — how to get constrained schema output

**There is no real constrained decoding on DeepSeek today.** Use
`{"type":"json_object"}` + schema pasted in the system prompt (+ "emit compact JSON" instruction)
+ client-side pydantic validation. That is what llm.py does. **[live]**

- `response_format: {"type":"json_schema"}` is **rejected on every model and both base URLs**
  (`"This response_format type is unavailable now"`). **[live]**
- Strict tool calling (below) LOOKS like the answer but **is steering, not a grammar** —
  see the counter-finding.

### Strict tool calling — tested and rejected for extraction [live 2026-07-17]

Forced-`tool_choice` strict tools work mechanically and pass toy probes (an unnatural required
key like `zebra_count` is emitted; `$refs` schemas behave the same as flat). **But on a real
full-document extraction (14k-token prompt, our real schema) it returned schema-INVALID args**:
`finish_reason=tool_calls`, clean stop, and the required top-level `"relations"` key simply
missing. Reproduced twice. It also collapsed output richness: ~700 completion tokens (34 ent /
0 rel) vs ~2,400–4,000 tokens (30+ ent / 24–32 rel) for the same doc via json_object. Strict
`required` enforcement does not hold at scale — do not use strict tools where schema shape or
extraction coverage matters.

Original probe recipe kept for reference:

```bash
curl -s https://api.deepseek.com/beta/chat/completions \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" -H "Content-Type: application/json" -d '{
  "model": "deepseek-v4-flash",
  "messages": [{"role":"user","content":"Extract the point x=5, y=9."}],
  "max_tokens": 100,
  "thinking": {"type": "disabled"},
  "tools": [{"type":"function","strict":true,"function":{
      "name":"emit_point","description":"Emit a point",
      "parameters":{"type":"object","additionalProperties":false,
        "required":["x","y"],
        "properties":{"x":{"type":"integer"},"y":{"type":"integer"}}}}}],
  "tool_choice": {"type":"function","function":{"name":"emit_point"}}
}'
# → tool_calls[0].function.arguments == "{\"x\": 5, \"y\": 9}"   [live, verified]
```

Gotchas that make or break this **[live]**:
- **Thinking is ON by default.** With thinking on, a forced `tool_choice` (`{"type":"function",...}`)
  errors: `"Thinking mode does not support this tool_choice"`. You **must** send
  `"thinking": {"type": "disabled"}` to force a specific tool.
- `strict: true` goes on the **tool object** (sibling of `type`/`function`), not inside `function`.
- Strict schema rules **[docs]**: every `object` property must be in `required`, and
  `additionalProperties: false` is mandatory. `minLength`/`maxLength` (and similar string
  constraints) are unsupported in strict mode.
- **[docs]** says `strict` requires the **beta** base URL. In our probe the forced-tool call also
  succeeded on `/v1`, but strict *enforcement* may differ — **use `/beta` for strict** to be safe.

If you only need "valid JSON, any shape", `{"type":"json_object"}` works everywhere (see below).

## Base URLs [docs, partially live]

| Base URL | Purpose |
|---|---|
| `https://api.deepseek.com` | OpenAI-compatible. `/v1` and bare root both work. **[live]** |
| `https://api.deepseek.com/beta` | Unlocks beta features: `strict` tools, chat prefix completion, FIM. **[docs]** |
| `https://api.deepseek.com/anthropic` | Anthropic Messages-format compatibility. **[docs]** |

`/v1` and `/beta` returned identical results for `json_schema` (both rejected) and for the
forced-tool call (both succeeded). **[live]**

## Models [live: model list + pricing verified against docs]

`GET /v1/models` returns exactly: **`deepseek-v4-flash`**, **`deepseek-v4-pro`**. **[live]**

`deepseek-chat` and `deepseek-reasoner` are **deprecated 2026-07-24 15:59 UTC** **[docs]**; during
transition they alias to non-thinking / thinking modes of `deepseek-v4-flash` respectively. After
that date our code must not depend on them.

| Model | Context | Max output | Backend fingerprint (live) |
|---|---|---|---|
| `deepseek-v4-flash` | 1M tokens | 384K tokens | `fp_..._prod0820_fp8_kvcache_20260402` **[live]** |
| `deepseek-v4-pro` | 1M tokens | 384K tokens | — |

Context/max-output figures are **[docs]** (pricing page).

## Pricing [docs] — USD per 1M tokens

| Model | Input cache-hit | Input cache-miss | Output |
|---|---|---|---|
| `deepseek-v4-flash` | $0.0028 | $0.14 | $0.28 |
| `deepseek-v4-pro`   | $0.003625 | $0.435 | $0.87 |

No time-of-day discount documented in the 2026 pricing page. (DeepSeek historically ran off-peak
discounts; not confirmed for v4 — treat as none.)

## Context caching [docs, usage fields live]

- **Automatic, on by default**, disk-based prefix cache. No code changes / no opt-in.
- Cache hit requires a **full prefix match** against a persisted "cache prefix unit"; partial
  prefixes don't hit. Units form at request boundaries, shared prefixes, and fixed long-input
  intervals. Implication: keep the **system prompt + schema block byte-identical and first** so it
  caches across calls.
- Usage object reports `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` (both seen live),
  plus `prompt_tokens_details.cached_tokens`. **[live]**
- Cache-hit input is ~50x cheaper than cache-miss (see pricing table) — big win for a fixed
  extraction preamble.

## Thinking / reasoning mode [live]

- **ON by default** for v4-flash (a plain `json_object` call spent 47 `reasoning_tokens`). **[live]**
- Disable per request with **`"thinking": {"type": "disabled"}"`** (top-level for raw HTTP; via
  `extra_body` in the OpenAI SDK). Verified: kills reasoning tokens, returns answer directly. **[live]**
- `reasoning_effort` accepts **`low`|`medium`|`high`|`max`|`xhigh`** — **not `none`** (that errors).
  Use the `thinking` toggle to turn it off, `reasoning_effort` only to set intensity. **[live]**
- When enabled, chain-of-thought comes back in `message.reasoning_content`, sibling to `content`. **[docs]**

## `json_object` mode [live]

- `response_format: {"type":"json_object"}` works on both models / both base URLs. **[live]**
- **[docs]** requirements: the word **"json"** must appear in the prompt, and you should show a
  sample of the desired shape; set `max_tokens` high enough to avoid mid-JSON truncation.
- **[docs]** known issue: may occasionally return empty content ("actively optimizing").
- Does **not** constrain shape — only guarantees parseable JSON. For a real schema use strict tools.

## Function / tool calling [live]

- Works on `deepseek-v4-flash` (and **[docs]** `deepseek-v4-pro`); standard OpenAI
  `tools` + `tool_choice`. Auto tool choice returned a correct `tool_calls` entry. **[live]**
- Tool use **inside thinking mode** supported from DeepSeek-V3.2 onward, **but** a *forced*
  `tool_choice` is incompatible with thinking — disable thinking to force a tool. **[live]**
- `strict` tools are **beta** (see TL;DR). **[docs]**

## Rate limits [docs]

- No RPM/TPM caps. Limit is **concurrency**: **v4-flash = 2500**, **v4-pro = 500** simultaneous
  in-flight requests (per account, and per `user_id` on expanded accounts).
- Over-limit → **HTTP 429**. Free capacity-expansion requests available (no extra cost).

## LitGraph state (llm.py, as of 2026-07-17)

What llm.py does for `deepseek*` models (all verified live end-to-end on a real extraction doc):

1. Routes to `/v1` with **`json_object` + schema in system prompt + compact-JSON instruction**;
   pydantic validation is the schema gate (extract.py's schema_error/retry path handles misses).
   Strict tool calling was implemented, tested, and **reverted** — see counter-finding in TL;DR.
2. Sends **`thinking:{"type":"disabled"}`** on every call — otherwise reasoning tokens burn
   silently (47 observed on a trivial json_object call).
3. Client built with **`max_retries=5`** — the only limit is concurrency (flash 2500), over-limit
   429s are absorbed by SDK exponential backoff.
4. Logs **usage incl. `prompt_cache_hit_tokens`** per call. Verified: repeat call on the same doc
   hit 14,080/14,087 tokens (~50x cheaper input). Keep system prompt + schema byte-stable.
5. Model choice: **`deepseek-v4-flash`** for extraction volume. `deepseek-chat`/`-reasoner`
   are deprecated 2026-07-24 — never hardcode them. `deepseek-v4-pro` only if quality justifies 3x.
6. Known residual risk: `json_object` may occasionally return empty content [docs] — surfaces as
   schema_error and is retried by the extract pipeline.
