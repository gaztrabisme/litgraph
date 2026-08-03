# Gotchas

## [2026-07-15] DuckDB: same-PK delete+reinsert fails inside one transaction
DuckDB's ART index rejects re-inserting a primary key that was deleted in the same
transaction (`ConstraintException`). Any "self-wipe then rebuild" pattern (resolve's
aliases/canonical_nodes) must either commit the wipe separately or — better — compute
everything first and keep the wipe→insert window free of network calls. resolve.py does
the latter.

## [2026-07-15] Shell: piping pytest eats its exit code
`pytest | tail -2 && git commit` commits even when tests fail — the chain sees tail's
exit status. Check `$?` explicitly or use `set -o pipefail`. (Bit us once: commit 5a4981e
contained a red test, fixed in 52feef5.)

## [2026-07-15] oMLX structured output (inherited from global setup, encoded in llm.py)
`response_format` needs BOTH `type:"json_schema"` and a `name` or enforcement silently
turns off; thinking must be disabled via `chat_template_kwargs` extra_body, `/no_think`
is ignored. $ref/$defs schemas verified working (live test 2026-07-15).

## [2026-07-15] Cold-review advisories deferred (recorded, not fixed)
- Alias method/score labels are merge-order-dependent; canonical_name ties nondeterministic.
- mentions_per_canonical denominator includes relation-only canonicals (dilution) — at
  acceptance, ALSO compute entity-only ratio: entity mentions ÷ canonicals with ≥1 entity mention.
- `--as-of` malformed date → raw traceback.
- ok_empty means "no entities" — relations may still exist on such rows.
- resolve is O(n²) per type + uncapped adjudication calls — fine at MVP scale, revisit at 1000s.

## [2026-07-15] Never store raw HTML as full_text (measured cost)
First live run fed raw arXiv HTML (~100k chars, ~80% markup) to extraction: 23 min/call
average, 1-2h stalls (8 concurrent giant prefills triggered oMLX's wired-memory guard
pauses), 20/198 docs in 8.5h ≈ 3-day ETA. trafilatura-cleaned text is ~10x smaller.
Rule: clean at ingest, store text not markup — prefill cost and extraction quality both.

## [2026-07-16] LLM "verbatim quotes" are stitched, not verbatim (measured)
Phase 1 extraction evidence quotes pass exact-substring against their own source full_text
only 40.7% of the time — but 98% of failures have ≥99% content-token containment: the model
splices spans with ellipses and lightly rewrites, it does not fabricate. Any evidence
verifier must tier: exact match | segment-fuzzy (difflib ≥0.85) / token-containment ≥0.95 |
failed. Never gate on exact substring alone.

## [2026-07-17] oMLX SpecPrefill silently degrades extraction (sparse prefill drops 80% of the doc)
SpecPrefill (keep_pct=0.2, threshold 4096 tok) got enabled server-side mid-backfill: every
paper-sized prompt was sparse-prefilled — the model extracted from ~20% of each document's
tokens. Symptom in server.log: `SpecPrefill: sparse prefill N/M conv tokens`. Fine for
retrieval-style chat; poison for coverage tasks (extraction, anything "read the whole doc").
75 affected extractions were deleted and redone. Rule: before any extraction run, verify no
SpecPrefill lines appear for a >4k-token probe, and remember server settings can change
between runs — the client has no way to see the setting per-request.

## [2026-07-17] DeepSeek "strict" tool calling is steering, not a grammar
- **Symptom:** forced-tool_choice strict tool call on a full-document extraction returns
  schema-invalid args — required top-level key ("relations") missing, finish_reason=tool_calls,
  no truncation. Passes toy probes (unnatural required keys, $refs) — the failure only shows at
  real prompt/output scale. Tool mode also collapsed extraction richness ~5x (695 vs ~2.4-4k
  completion tokens on the same doc).
- **Rule:** on DeepSeek use json_object + schema-in-prompt + pydantic gate (llm.py does this);
  never trust "strict" for shape or coverage. Re-probe with a REAL workload doc, not a toy,
  before believing any constrained-output claim.
- **Also:** thinking is ON by default and burns reasoning tokens silently — llm.py sends
  thinking:{"type":"disabled"} on every DeepSeek call. deepseek-chat/-reasoner die 2026-07-24.

## [2026-07-17] oMLX multi-model load starves the Qwen engine
- **Symptom:** assassin/adjudication calls crawl (11-16 tok/s vs ~70 normal) or stall for 20+ min; server.log shows settings updates / loads for other models (e.g. gemma-4) — oMLX's LRU engine pool is evicting or memory-squeezing Qwen.
- **Rule:** before any long oMLX-dependent run, probe sustained decode (300-token completion, check tok/s in server.log). If well under ~50 tok/s, check whether other models are loaded; coordinate with the operator before assuming failure. Background arm processes survive the stall — stop, verify recovery, re-run (assassinate resumes from 'proposed').

## Git: public remote has a DIFFERENT history
- The public repo (github.com/gaztrabisme/litgraph) was created 2026-07-25 from an orphan "public release" commit. Local `main` keeps the full 51+-commit private history (contains scrubbed-at-HEAD leaks in old revisions). **NEVER `git push origin main` from local main** — publish by fast-forwarding the `public` branch (or re-orphaning) and pushing `public:main`.
