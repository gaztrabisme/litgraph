# Phase 3 Design — Temporal Backtest (the eval)

**Date:** 2026-07-16 · **Mode:** Eval (charter: non-negotiable before scaling corpus)
**Question:** do graph-proposed, assassin-filtered gaps predict what the field actually did?
**Secondary:** does the graph layer beat a well-prompted deep-research agent on raw corpus?

## Why a backfill comes first (recon 2026-07-16)

Phase 1 sampled arXiv by recency: 172/197 dated docs are 2026 Q2–Q3; every pre-2026
cutoff leaves ≤25 docs — no graph to backtest. Fix: ingest historical papers (same
DEFAULT_QUERY + submittedDate windows, 2023-01 → 2025-12, target ≈250–300 stored docs),
extract, re-resolve. This is eval infrastructure, not product corpus-scaling; it also
de-biases the main graph.

## Protocol

```
backfill:  ingest arxiv (date-windowed) → extract → resolve → embed → communities  [main DB]
snapshot:  backtest.duckdb = documents/extractions/mentions WHERE published_date <= CUTOFF
           (NULL-dated docs excluded)                       [then resolve/embed/communities on it]
propose@T: litgraph --db backtest.duckdb propose (theta re-swept on the snapshot)
assassinate@T: full kill-gate INSIDE the snapshot (tools see only pre-cutoff docs)
score:     for each backtest survivor, critics run against the FULL current DB;
           verdict evidence partitioned by published_date vs CUTOFF
baseline:  deep-research agent (cross-family) over the same snapshot corpus, no graph,
           produces the same-shaped gap claims; scored identically
```

**Cutoff rule (data-driven, decided after backfill):** earliest quarter boundary with
≥60 pre-cutoff docs AND ≥800 pre-cutoff relation mentions AND ≥80 post-cutoff docs.
Recon expectation: 2025-07-01 or 2025-10-01. Cutoff is recorded in decisions.md.

## Scoring (per backtest survivor — symmetric across arms)

Run the Phase 2 Stage-A critic pair against the FULL DB, with two protocol constraints
from spec review:
- **Symmetric tools (C3):** scoring critics get ONLY name/doc-based tools
  (who_tried_this + full_text); grounds.node_ids are stripped at scoring so neither
  arm gets structural tooling the other lacks (snapshot node_ids only sometimes
  resolve in the full DB anyway — variable advantage is worse than none).
- **Closure judge (C1):** a kill alone does not mint a HIT. Every kill's evidence is
  passed to a cross-family closure judge (Claude, single-shot): "does this evidence
  entail that THIS gap was actually closed/done, not merely that related work exists?"
  Kills failing the judge → OPEN (evidence was topical, not closing). All judged
  dossiers are kept for human audit — at N≤~15 the operator can eyeball every one.

Disposition:
- **HIT:** killed + ≥1 verifier-passed evidence doc dated > CUTOFF + closure judge affirms.
- **LEAK:** killed + closure judge affirms + all closing evidence dated ≤ CUTOFF
  (snapshot assassin should have caught it — pipeline error, not field signal).
- **OPEN:** survives, or killed-but-judge-rejects.

**Headline metric (W10): decided-precision = HIT / (HIT + LEAK).** Secondary:
HIT / (HIT + LEAK + OPEN) as the lower bound, with OPEN stated plainly as
"not yet closed ≠ wrong" (an open frontier gap may be a success we can't score yet).
Ranking for any top-N selection is within-template rank, interleaved (W5) — template
scores are incomparable scales. If survivors ≤ 15, score ALL (no selection).
**Statistical honesty (W4): at N≈10–15 everything is directional; the arm-vs-arm
comparison is explicitly NOT powered. The report must say this in its first paragraph.**

**Contamination stance:** models have read 2025–26 literature; we never score a model's
claim — only verifier-passed in-index evidence with a DB-recorded published_date decides
the partition. A critic "remembering" a closing paper scores nothing unless it finds it
in the index.

## Baseline arm (deep-research agent, no graph) — same gauntlet, different proposer (C2)

Both arms are {proposer} → snapshot assassin → survivors → scoring. They differ ONLY
in the proposer:
- Graph arm: templates over the snapshot graph.
- Baseline arm: cross-family deep-research agent (Claude subagent) over the snapshot
  corpus — doc list (title/date) + on-demand full texts, honest strongest-effort prompt,
  emits the same CGW candidate shape (template label 'B' equivalent recorded in params;
  rows use a template value satisfying the DB CHECK — see build surface).
- Baseline claims then face the SAME snapshot assassinate@T (who_tried_this/full_text
  work name/doc-based; no node_ids needed) before any scoring — otherwise the
  comparison measures filtered-vs-unfiltered, not graph-vs-agent.
- Baseline emits ~2× the graph arm's candidate count pre-filter (kill gate will thin it);
  both arms' survivors are scored identically.
- Known asymmetry (reported, not hidden): Claude's training may recall post-cutoff
  papers, shaping baseline claims; index-evidence-only scoring neutralizes the metric,
  not the claim distribution.

## Done When — gates

Unit (deterministic, fakes):
- [ ] snapshot builder: doc/extraction/mention counts match the cutoff filter exactly;
      NULL-dated docs excluded; snapshot is a valid pipeline DB (resolve runs on it)
- [ ] scoring partition: kill evidence dated pre/post cutoff → LEAK/HIT respectively;
      mixed evidence (any post-cutoff doc) → HIT; survive → OPEN
- [ ] full suite still green (backfill touches no Phase 1/2 code paths)

Acceptance (live):
- [ ] Backfill: ≥60 new pre-2026 docs stored with ok/ok_empty extraction ≥90%
- [ ] Snapshot pipeline runs end-to-end; propose@T yields ≥15 candidates from ≥2 templates
      (thin-graph reality: don't demand Phase 2 volumes)
- [ ] assassinate@T dispositions 100%; same C5 hard gates (abstain ≤20%, kill ∈ (0,100))
- [ ] Scoring: every backtest survivor gets HIT/LEAK/OPEN with evidence dossiers
- [ ] Baseline arm produces matched-N claims and is scored by the same machinery
- [ ] Report: validated-gap rate (graph) vs (baseline), with per-candidate dossiers —
      **no minimum rate is gated**; the honest number IS the deliverable, win or lose
- [ ] decisions.md records cutoff + protocol choices; all LLM calls logged

## Build surface (small — this is an eval, not a subsystem)

```
src/litgraph/backtest.py     # snapshot(conn, cutoff, dest_path) [papers-only],
                             # score(full_conn, cutoff, candidates, *, chat_fn, judge_fn)
                             #   -> HIT/LEAK/OPEN rows in backtest_results
cli: backtest snapshot --cutoff | backtest score --cutoff
prompts/baseline_v1.md       # deep-research baseline prompt (CGW output shape)
prompts/closure_judge_v1.md  # cross-family closure judge (C1)
```
Everything else composes existing commands via --db. Extraction/resolve/embed/communities/
propose/assassinate are untouched. Scoring reuses assassin._run_critic with a scoring
tool whitelist (who_tried_this, full_text only — C3) via a parameter, not a fork.

## Contract Changelog (review resolutions)
- [PRE-RUN] C1: HIT requires cross-family closure judgment, not provenance alone;
  judge-rejected kills → OPEN. Reason: verifier checks provenance, not relevance (P2-W5);
  topical post-cutoff quotes would inflate HIT.
- [PRE-RUN] C2: baseline claims pass the same snapshot assassinate@T before scoring.
  Reason: otherwise the comparison is filtered-vs-unfiltered, tilted to the graph arm.
- [PRE-RUN] C3: scoring critics use who_tried_this + full_text only; node_ids stripped.
  Reason: snapshot node_ids resolve erratically in the full DB — variable asymmetry.
- [PRE-RUN] W2: snapshot papers-only. W5: within-template interleaved ranking; score all
  if ≤15. W6: DB backup before destructive steps. W10: headline = HIT/(HIT+LEAK).

## Risks & mitigations

- **DB backup BEFORE extract/resolve of backfill (W6):** `cp litgraph.duckdb
  litgraph-phase2-final.duckdb` once the ingest writer releases the lock. Re-resolve is
  destructive (new node_ids for changed clusters, Phase 2 grounds go stale) — the copy
  preserves the reported Phase 2 state.
- **Snapshot is papers-only (W2 + own audit):** seed blog/repo dates are guessed
  ("best-effort era estimates") AND their content is fetched as-of-today — a mis-dated
  or drifted doc smuggles future knowledge into the snapshot. Papers keep authoritative
  v1 dates. Non-paper docs stay in the FULL DB for scoring only.
- **arXiv HTML is the latest revision (W1):** a pre-cutoff paper revised post-cutoff
  carries drifted text into the snapshot. Bounded, one-directional; reported as a
  limitation (v1-pinned re-fetch is the future fix).
- **Vocabulary bias (W3):** the snapshot is a query-defined slice by TODAY's terms; the
  2023–24 field used different vocabulary. The eval measures gaps within this
  subcorpus, not the field at large — stated in the report.
- Resolve cost at ~450 docs (W7): O(n²) per type + uncapped adjudication band — measure
  name-universe size before running; if the 0.70–0.80 band exceeds ~500 pairs, cap
  adjudication (take highest-cosine 500) and log the cap. Runs twice (main + snapshot).
- arXiv 429 on backfill: FETCH_DELAY=1.0; 3 windows; resumable (ON CONFLICT DO NOTHING).
- Historical HTML coverage worse for 2023–24 → abstract fallbacks; ≥70% full-text gate
  does NOT apply to backfill (actual rate reported).
- Baseline/scoring storage (W8): backtest results live in a dedicated `backtest_results`
  table (candidate_id, arm, disposition HIT|LEAK|OPEN, judge JSON, evidence JSON) — NOT
  in gap_candidates.status (whose CHECK stays Phase 2's four states). Baseline rows in
  the snapshot's gap_candidates use template='T1' + params.arm='baseline' to satisfy the
  CHECK; the assassin's mandate default is fine for CGW-shaped claims.
- Cutoff rule: the ≥800-mention bound is the binding constraint (W9) — decide from
  post-backfill data; expect 2025-10-01 if 2025-07-01 misses on mentions.
- Deep-research baseline cost: one long Claude session + judge calls — bounded, logged.

## Contract Changelog
<!-- - [PHASE-3] Changed: X → Y. Reason: Z. Approved by: coordinator. -->
