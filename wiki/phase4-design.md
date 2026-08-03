# Phase 4 — Scale + live mode

Charter: full corpus, weekly re-ingest, frontier diffs. Split into stages; this doc covers **4a**.
Greenlit by the operator 2026-07-17 after Phase 3 validated the machinery end-to-end.

## 4a — Corpus expansion + uniform re-extraction (this stage)

**Why now:** Phase 3's eval was underpowered (120 pre-cutoff papers → 1 decided case in 53).
Scaling the corpus fattens both the graph and the backtest answer key. Extraction moves to
DeepSeek v4-flash (calibrated vs Qwen 2026-07-17: entities 1.07x, relations 0.93x, 20/20 ok —
see decisions.md + wiki/deepseek-api.md).

**Budget bound:** limited DeepSeek credit at start. Re-extracting 588 docs ≈ $1.7; each 1,000
docs ≈ $3. This stage targets ~550 new docs (~1,150 total, ≈ $3.4 spend) leaving buffer.
Full-scale expansion (weekly re-ingest, blogs/repos beyond seeds) needs a top-up — 4b/4c.

### Corpus definition (adds two subdomains, per charter differentiator #1)

| Subdomain | arXiv query (cat:cs.CL OR cs.AI OR cs.IR) | max_results |
|---|---|---|
| agent memory (existing) | DEFAULT_QUERY, unchanged | already ingested |
| RAG | abs:"retrieval-augmented generation" OR abs:"retrieval-augmented" OR abs:"GraphRAG" | 300 |
| agent harness | abs:"LLM agent" OR abs:"agentic framework" OR abs:"multi-agent LLM" OR abs:"tool-use agent" OR abs:"agent framework" | 300 |

Blog/repo seed expansion deferred to 4b (hand-curation, not budget).

### Uniform re-extraction (the no-mixed-models rule)

Extraction-model-correlated graph density is the SpecPrefill poison class. Mechanics:

1. **Extraction identity becomes (doc_id, prompt_version, model).** extract.py's un-extracted
   selection and stale-error-row delete gain `AND e.model = ?`. Switching EXTRACTION_MODEL to
   `deepseek-v4-flash` then naturally schedules re-extraction of every doc; Qwen rows stay put
   as raw data (charter: extractions are data, re-canonicalization without re-extraction).
2. **Qwen mentions leave the live table.** Every mention consumer (resolve/graph/embeddings/
   templates/tools/verify) reads mentions unfiltered — mixed rows would double-count. Qwen
   mentions are copied to `mentions_archive_qwen`, then deleted (child-before-parent not needed;
   extraction rows are kept). DB file backed up first (litgraph-pre-phase4.duckdb).
3. Graph artifacts (canonical_nodes, aliases, node embeddings, communities) fully rebuilt from
   DeepSeek mentions; W7 band cap re-measured (band_floor = 500th-highest cosine if band > 500).

### Gates (same as Phase 1, at new scale)

- extraction ok-rate ≥ 90% (extract.py retry path for schema_error)
- mentions per canonical node ≥ 1.3 (resolution health)
- spend within balance; check /user/balance between stages
- graph builds clean; communities assign; stats recorded in log.md

### Non-goals for 4a

- Re-running the temporal backtest (4b — needs the fatter corpus in place first)
- Weekly re-ingest / frontier diffs (4c)
- Blog/repo seed expansion (4b)

## 4b — Backtest at scale + curation (needs top-up)

Re-run Phase 3 protocol on the expanded corpus (cutoff re-derived by the same decision rule);
scoring-critic recall pre-pass (embedding retrieval of nearest post-cutoff docs); expand
blog/repo seeds; human spot-check OPENs.

## 4c — Live mode

Weekly re-ingest, extraction deltas, "what changed at the frontier" diffs.
