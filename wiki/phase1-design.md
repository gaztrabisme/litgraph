# Phase 1 Design — Ingestion MVP

**Date:** 2026-07-15 · **Mode:** Build (heavy, greenfield) · **Scope:** AGENTS.md Phase 1
**Corpus:** ~200 docs, agent-memory subdomain (≈170 arXiv + ≈30 curated blogs/repos)

## Done When — unit gates (deterministic, pytest; no network/LLM — fakes injected)

- [ ] `uv run pytest` → 0 failures; `uv run ruff check .` → 0 errors
- [ ] Ingest is idempotent: re-inserting an existing doc_id is a no-op (ON CONFLICT DO NOTHING), unit-tested
- [ ] Extract statuses: 'ok' (≥1 entity), 'ok_empty' (valid but empty), 'schema_error', 'llm_error' — each path unit-tested; retry semantics per Runtime Semantics
- [ ] Resolve: exact-merge, embedding-merge (fake embed fn), borderline LLM adjudication (fake chat fn), self-wipe on re-run, and the ER name universe (entity mentions ∪ relation endpoints) — unit-tested
- [ ] Graph: docs published after --as-of cutoff contribute zero edges; NULL-dated docs excluded from as-of graphs but included in the full graph — unit-tested with synthetic rows

## Done When — acceptance gates (live overnight run, checked by coordinator from `litgraph stats`)

- [ ] ≥150 arXiv docs stored; ≥70% with full_text > 2,000 chars (arXiv HTML coverage is external — actual % reported)
- [ ] ≥25 seed blog/repo docs stored with full_text > 500 chars (dead/unextractable URLs skipped and reported)
- [ ] ≥90% of docs have a status='ok' or 'ok_empty' extraction for extraction_v1; ok_empty rate reported separately (>20% flags prompt trouble, advisory)
- [ ] mentions_per_canonical **≥ 1.3** (charter: ~1.0 = broken)
- [ ] **Positive edge gate:** full graph has ≥300 edges and ≥5 distinct edge types present (an empty/edgeless graph must not pass)
- [ ] Every LLM call logged to `logs/llm/` (chat: prompt_version, model, raw response; embeddings: model + input count)

## Runtime Semantics (from spec cold-review — C1-C3, W4-W13)

- **DB write model (C1):** worker threads do LLM/HTTP only; ALL DuckDB writes happen on the main thread (workers return results; main thread inserts). One connection, no concurrent writes.
- **ER name universe (C2):** resolve operates on the UNION of entity-mention (name, type) AND relation endpoint (src_name, src_type), (dst_name, dst_type) pairs — every name reachable by graph.py's join gets an alias row.
- **Extract idempotency (C3):** "un-extracted" = no status IN ('ok','ok_empty') row for the current prompt_version. On retry, prior error rows for that (doc_id, prompt_version) are deleted before inserting the new row. The ok-rate metric is computed over docs, not rows.
- **doc_id derivation (W9):** papers → `arxiv:{base_id}` (versionless); blogs → `blog:{sha256(url)[:16]}`; repos → `repo:{owner}/{name}`.
- **Ingest idempotency (W8):** INSERT ... ON CONFLICT DO NOTHING everywhere documents are written.
- **Resolve re-run (W10):** resolve always self-wipes first (DELETE aliases, then canonical_nodes — FK order).
- **NULL published_date (W11):** allowed; NULL-dated docs appear in the full graph, are excluded from any --as-of graph, and stats reports the null-date count.
- **mentions_per_canonical (W13):** count of entity-kind mention rows ÷ count of canonical_nodes. Computed by one shared helper (resolve.py), reused by stats.
- **Extraction output budget (W12):** extraction calls use max_tokens=16384.
- **Relation dedup:** identical (doc_id, src_name, src_type, edge_type, dst_name, dst_type) mentions are inserted once per extraction.
- **W3 resolved by live test 2026-07-15:** oMLX enforces the $ref/$defs Pydantic schema correctly (MemGPT smoke doc → 8 typed entities, 7 valid relations, zero schema violations).

## Architecture (one file = one concern, ≤120 logic lines each)

```
pyproject.toml            # uv-managed; deps pre-declared by coordinator
src/litgraph/
  models.py               # CONTRACT: Pydantic ontology + extraction schema (coordinator-owned)
  db.py                   # CONTRACT: DuckDB connection + DDL (coordinator-owned)
  llm.py                  # CONTRACT: oMLX client helpers — chat+schema, embeddings, call logging (coordinator-owned)
  cli.py                  # argparse: ingest | extract | resolve | graph | stats
  sources/arxiv.py        # arXiv Atom API → documents (agent-memory query, HTML full text w/ abstract fallback)
  sources/seeds.py        # data/seeds.yaml → fetch blogs (trafilatura) + repo READMEs (raw.githubusercontent)
  extract.py              # per-doc LLM typed extraction → extractions + mentions
  resolve.py              # ER: jina embeddings → cosine merge → LLM adjudication of borderline → aliases
  graph.py                # DuckDB → NetworkX rebuild, --as-of cutoff filter
prompts/extraction_v1.md  # versioned extraction prompt (prompt change = new file = new version)
data/seeds.yaml           # ~30 curated agent-memory blogs/repos (url, type, title, published guess)
tests/                    # pytest; one test file per module
```

## Data model (DuckDB) — the load-bearing contract

```sql
documents(doc_id PK, source_type 'paper'|'blog'|'repo', external_id, url, title,
          authors, published_date DATE, fetched_at, full_text, meta JSON)
extractions(extraction_id PK, doc_id FK, prompt_version, model, raw_output JSON,
            status 'ok'|'schema_error'|'llm_error', created_at)
mentions(mention_id PK, extraction_id FK, doc_id, kind 'entity'|'relation',
         entity_type, name,                      -- entity rows
         src_name, src_type, dst_name, dst_type, -- relation rows
         edge_type, evidence)
canonical_nodes(node_id PK, node_type, canonical_name, created_at)
aliases(alias_name, node_type, node_id FK, method 'exact'|'embedding'|'llm', score,
        PK(alias_name, node_type))
```

**Key decision — edges are never materialized canonically.** `graph.py` joins relation-mentions → aliases → canonical nodes at build time; the edge timestamp is the source doc's published_date. Re-canonicalization (charter requirement) = wipe canonical_nodes+aliases, re-run resolve. Raw extractions are never touched.

**Ontology (models.py):** entity types `Method, Problem, Dataset, Metric, Claim, Assumption` (Paper/BlogPost/Repo are documents, not extracted entities). Edge types `USES, IMPROVES_ON, CONTRADICTS, EVALUATED_ON, ASSUMES, MOTIVATED_BY, APPLIED_TO, IMPLEMENTS, CITES`. Extraction output schema: `{entities: [{type, name}], relations: [{src_name, src_type, edge_type, dst_name, dst_type, evidence}]}` — evidence is a short verbatim quote (contamination guard: claims must be traceable to source text).

## LLM plumbing (llm.py contract)

- Endpoint `http://127.0.0.1:8000/v1` (oMLX), key from `OMLX_API_KEY` env (default dev key).
- Extraction model: `Qwen3.6-35B-A3B-oQ8-fp16-mtp`. **Gotchas (verified previously):** JSON-schema enforcement requires `response_format={"type":"json_schema","json_schema":{"name":...,"schema":...}}` — omitting `type` silently disables enforcement; thinking disabled via top-level `chat_template_kwargs={"enable_thinking": False}` (extra_body in the openai SDK); `/no_think` in prompt is ignored.
- Embeddings: `jina-embeddings-v5-text-small-retrieval-mlx` (1024-dim, verified live).
- Long docs: truncate full_text to ~100k chars before extraction (262k ctx available; stay well under).
- Concurrency ≤ 8 (server's max_concurrent_requests); extraction runs docs concurrently via threads.
- Every call: append JSONL to `logs/llm/{command}-{date}.jsonl`.

## ER pipeline (resolve.py)

1. Exact-match pass: identical normalized (casefold, strip) names within a type → same canonical.
2. Embedding pass: jina embeddings of names (+ type prefix); cosine ≥ 0.80 within type → merge cluster (ATOM's threshold — adopted from Phase 0 research; label-weighted embedding deferred).
3. Borderline band 0.70–0.80 → LLM adjudication (Qwen, JSON-schema `{same: bool}`) with both names + one evidence quote each.
4. Union-find over merge decisions → canonical_nodes (representative = most frequent mention name) + aliases rows with method + score.

## Deviations & risk notes

- **Parallel implementation agents share the checkout** (deviation from worktree rule): each agent's brief locks it to its own module + test files; coordinator owns all shared files (pyproject, models.py, db.py, llm.py) and the git index. Disjoint-by-construction.
- OpenAlex citation edges (charter Phase 1 mention) → **deferred to a stretch task tonight**; CITES edges are additive and don't block extraction/ER/graph.
- Ontology iteration is expected (charter); prompt_version + raw extractions make re-extraction a rerun, not a migration.

## Contract Changelog
<!-- - [PHASE-N] Changed: X → Y. Reason: Z. Approved by: coordinator. -->
- [PRE-BUILD] Changed: extractions.status gains 'ok_empty'. Reason: spec review W4 — schema-valid-empty must not inflate the ok gate. Approved by: coordinator.
- [PRE-BUILD] Changed: arXiv full-text gate 80% → 70%, gates split unit vs acceptance. Reason: W5/W6 — external HTML coverage isn't a code-quality signal; pytest must be deterministic. Approved by: coordinator.
- [PRE-BUILD] Changed: llm.embed() now logs (count + sample, not vectors). Reason: W2. Approved by: coordinator.
- [PRE-BUILD] Added: positive edge gate (≥300 edges, ≥5 types). Reason: W1 — empty graph must not pass. Approved by: coordinator.
- [LIVE-RUN] Changed: arXiv full_text stores trafilatura-extracted clean text, never raw HTML. Reason: measured 23 min/extraction call with 1-2h memory-guard stalls at 20/198 docs after 8.5h — raw HTML made prefill ~10x more expensive and poisoned extraction input. DB wiped and re-ingested; the 20 HTML-fed extractions discarded. Approved by: coordinator.
