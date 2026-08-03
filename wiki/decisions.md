# Decisions

Settled architecture decisions live in [../AGENTS.md](../AGENTS.md) ("Core architecture decisions") — not duplicated here. This file records session-level decisions, KB grounding, and rejected approaches as they accumulate.

## [2026-07-15] KB Grounding — Phase 0
- KB: searched "building knowledge graphs from text with LLM entity and relation extraction" → a graph-ML textbook in a private knowledge base covers LLM-based KG construction (LangChain LLMGraphTransformer; spaCy SVO extraction as rule-based baseline). Confirms LLM extraction over NER/rule-based for our typed ontology. Nothing in KB on research-gap detection — grounded via paper deep-reads instead (SciMuse/HySemRAG/GAPMAP agents).

## [2026-07-15] Phase 0 forks — DECIDED (operator)
- **Build strategy: own small codebase**, transplant designs (Graphiti bi-temporal + Pydantic ontology; ATOM cosine incremental merge; fast-graphrag PPR loop; MS graphrag Leiden pipeline). Rejected: Graphiti-as-library (couples to episodic-memory abstractions + server DB at MVP), nano-graphrag hard fork (frozen since 2024-10, temporal+ER missing anyway).
- **Graph store: NetworkX + DuckDB** for MVP. Timestamped node/edge tables in DuckDB; as-of-cutoff backtest = SQL filter → rebuild in-memory graph. Neo4j = escape hatch at scale; Kuzu avoided (Graphiti backend deprecated upstream). Scale note: 200 docs is Phase 1 validation only — full corpus (thousands–tens of thousands of docs) still fits in-memory; real bottleneck is extraction cost, not the store.
- **Charter patched** (AGENTS.md): GAPMAP citation softened to complementarity; Refute-or-Promote (2604.19049) added as the kill-gate mechanics reference.
- **Git: initialized, wiki tracked.**

## [2026-07-15] Phase 0 research conclusions
- **Evidence update**: "ensemble the killer per GAPMAP" rests on a complementarity finding, not a quantified ensemble win. New mechanics reference: Refute-or-Promote (arXiv 2604.19049) — any-refutation-kills / unanimous-survival gate, isolated killer context, cross-family critics.
- **SciMuse lesson locked in**: never rank gaps by graph score alone (their intuitive topological signals anti-predicted interest). Topology = generator only; judgment = adversarial killer grounded in index evidence; eval = temporal backtest with precision-at-top-N as headline metric.
- **Agent recommendation (not yet decided)**: own small codebase, NetworkX+DuckDB store for MVP, Neo4j escape hatch, avoid Kuzu; transplant designs from Graphiti/ATOM/fast-graphrag/MS-graphrag.

## [2026-07-17] Phase 3 execution decisions
- **Backtest cutoff: 2024-07-01** — the spec rule (earliest quarter boundary with ≥60 pre-cutoff papers, ≥800 pre-cutoff relation mentions, ≥80 post-cutoff papers) passed at every boundary after the 390-doc backfill; earliest wins. Snapshot world: 120 papers / 1,814 relation mentions; answer key: 440 post-cutoff papers (2 years of field activity).
- **W7 adjudication cap applied on main-DB re-resolve**: 0.70–0.80 band was 10,401 pairs (corpus tripled); capped to the top-500 by cosine via band_floor=0.790741. Pairs below the raised floor stay unmerged. Resolution health unchanged (mentions/canonical 1.54).
- **resolve._cluster vectorized with numpy** (dependency added): pure-Python cosine loop was O(n²)·1024 ≈ 10^10 ops at 10,788 names. Same merge order/results (triu_indices preserves i<j enumeration); 13 resolve tests green.
- **SpecPrefill must be OFF on oMLX during extraction** — it was enabled mid-backfill (keep=20%, threshold 4096 tok) and sparse-read 75 docs; those extractions were deleted and redone with full prefill. Coverage tasks (extraction) are incompatible with lossy sparse prefill; see gotchas.md.

## Rejected Approaches
(none yet this session — see AGENTS.md for pre-settled rejections: LLM-free-generation of gaps, LLM-chooses-every-hop traversal)

## [2026-07-17] Extraction provider for corpus scaling: DeepSeek API
- **Why not LFM2.5-8B-A1B (1B-active):** extraction quality silently poisons graph density (SpecPrefill lesson); a 1B-active model on 30-50k-token scientific full text is the same failure class. Local throughput was the bottleneck either way.
- **DeepSeek wiring:** `deepseek*` model names route to api.deepseek.com in llm.py. json_schema response_format REJECTED by their API (probed 2026-07-17) → json_object + schema in system prompt + existing pydantic gate. Key in `.env` (gitignored), env var `DEEPSEEK_API_KEY`. Backend served is deepseek-v4-flash.
- **Compact-JSON instruction required:** without grammar constraint DeepSeek pretty-prints; 2/20 calibration docs (the densest) blew max_tokens=16384 mid-JSON. "Emit compact JSON" in the schema suffix fixed both.
- **Calibration (20 docs vs stored Qwen):** after fix 20/20 ok; entities/doc ratio 1.07, relations/doc 0.93 — density comparable, no systematic under-extraction. ~10-19s/doc at 10 workers (94s wall for 20).
- **Rule carried:** never mix extraction models in one graph — if we switch to DeepSeek for scale, re-extract the whole corpus uniformly (extraction-model-correlated density = same poison as SpecPrefill).

## [2026-07-17] Phase 4a decisions
- **ADJUDICATION_MODEL split from EXTRACTION_MODEL:** judgment roles (ER adjudication, assassin critics) stay on local Qwen/oMLX — grammar-enforced decoding is load-bearing for multi-turn ReAct loops, kill-gate behavior stays comparable with Phase 2/3, and the token-hungriest component runs free. Extraction volume = DeepSeek v4-flash. Assassin swap to be decided by head-to-head test (task #11), not config edit.
- **Extraction identity = (doc_id, prompt_version, model):** switching EXTRACTION_MODEL schedules full re-extraction; prior models' extraction rows are kept (extractions are data), their mentions archived out of the live table (all mention consumers read unfiltered).
- **Phase 4a corpus queries:** RAG + agent-harness subdomains, 300 max each, recency-sorted (phase4-design.md). Blog/repo seed expansion deferred to 4b.

## [2026-07-17] Assassin critic model: keep Qwen, ensemble as upgrade path
- Head-to-head on 15 candidates (log.md): DeepSeek v4-flash mechanically clean (0 errors, abstain parity, 17x faster, ~$0.05) but produced 1 specious kill whose false refutation passed the provenance-only verifier (W5). Decision rule (evidence-quality parity) not met → ADJUDICATION_MODEL stays Qwen/oMLX.
- Upgrade path: W5 relevance-checking verifier FIRST, then ensemble Stage-A critics across families (Qwen + DeepSeek voting, per GAPMAP/charter). Not a config flip.

## [2026-07-17] W5 relevance gate + Stage-A ensemble are now the default pipeline
- Implements the recorded upgrade path (65efcea): provenance verify → relevance judge (relevance_v1 on ADJUDICATION_MODEL, kill verdicts only, fail-open, shares evidence-retry budget) → per-role STAGE_A_CRITICS (Qwen + deepseek-v4-flash), claude-cli third-family auditor unchanged.
- Validated on the 77-candidate run: per-family kill propensity converged (30 vs 31), W5 blocked 25% of kill attempts, C5 gates pass. Survive verdicts are exempt from the relevance judge (nearest-miss evidence is fuzzy by design).
- Backtest scoring critics deliberately stay single-model (ADJUDICATION_MODEL) for arm comparability.

## [2026-07-17] Phase 4b backtest decisions
- **Cutoff stays 2024-07-01 (deviation from earliest-wins recorded):** at the 1,154-doc corpus the rule's earliest passing boundary moved to 2024-01-01 (65 docs / 1,008 rel-mentions / 1,061 post). Rejected: −55 in-world docs (−46% snapshot graph) for +55 answer-key docs (+5%), and it would break same-cutoff comparability with Phase 3 (identical 120 in-world papers isolates the changed variables: DeepSeek extraction, 2.3x answer key, recall pre-pass, ensemble+W5 gate).
- **Snapshot θ=0.60:** sweep cliff 16→1 between 0.60/0.65 (0.55 plateau at 16 → higher threshold dominates). Phase 3 analogue: 0.65 at its scale.
- **Baseline claims reused verbatim from Phase 3** (42 CGW claims, quotes re-verified 42/42 against snapshot text) under fresh BL4B: candidate IDs — backtest_results PK is (candidate_id, arm, cutoff); same-cutoff reuse of BL: IDs would have clobbered Phase 3 rows. Re-gating (not reusing Phase 3 dispositions) because the kill-gate changed (ensemble+W5).
- **Scoring-recall pre-pass default k=8:** lead pool = post-cutoff papers (title + 1,500-char head, Jina v5 via oMLX), cosine top-k per claim injected as grounds.post_cutoff_leads with a leads-not-evidence instruction. Symmetric both arms (C3); evidence discipline unchanged (verified quotes + closure judge). Non-default chat_fn paths stay lead-free unless lead_embed_fn passed.

## [2026-07-18] 4b extension: both template-redesign forks tested (operator: "test all")
- **T2 paper-closable variant (templates._t2_paper, --t2-paper):** same hole detection, claims reshaped mechanically to "flagship Method M (max PageRank, typed) not applied to flagship Problem P of the adjacent cluster"; already-linked pairs skipped; cid gains a 'paper' slot. Structure still proposes — no LLM generation.
- **Longitudinal structural scoring (scratchpad/longitudinal_t2.py):** snapshot hole clusters mapped to the 2026 graph by (name, type); FILLED = pair no longer under-linked by the template's own 0.25x rule. Verdict: measurement too noisy to trust — absolute cross-edges stayed 0-4, FILLED flips driven by the shrinking config-model expectation in the larger graph, and cross-extraction edge counts wobble ±2. Not adopted as an eval; kept as a diagnostic.
- **First LEAK of the project (CREEM):** paper-variant claim "CREEM not applied to long-term memory learning" was false in-world (arxiv 2403.04787, pre-cutoff, introduces CREEM as exactly that) — flagship pairing can emit facially-false claims when extraction misses the M->P edge, and the snapshot kill-gate missed it too. Scoring critics + recall leads caught it. Known failure mode to guard in any production use of --t2-paper.

## [2026-07-18] Auditor parsing + Qwen baseline decisions
- **_first_json for all cross-family CLI verdicts:** claude -p result text may carry prose around the JSON (live 2026-07-17/18). Conservative {}-fallback stays (broken auditor never silently passes), but parse now tolerates prose/fences, and failures log raw text so silent-artifact classes are visible in logs. Re-audit of affected dossiers is the remediation pattern (scratchpad/redo_stage_d.py — rebuilds dossiers from stored Stage-A verdicts; valid because stored kill/survive rows are verified-tier by construction).
- **Absence claims need extractive grounding for small models:** asking a 35B to select quotes "supporting" an absence triggers its refusal/empty path (an absence is never visible in one excerpt). Grounding is now purely extractive (quote what the cited paper does with the claim's terms); refutation belongs to the kill-gate. General rule for any future local-proposer harness.
- **Baseline proposer family matters and is now measured:** Claude 10/28 vs Qwen 1/23 validated on identical gauntlets. Do not treat 'LLM proposer' as one thing in future design talk — specify family, and treat Claude-proposed hit rates as contamination-suspect until a pre-cutoff-trained proposer exists.

## [2026-07-19] Architecture verdict: LLM proposes, structure informs and disposes
- Graph-as-tools lifts both proposer families (Qwen 1→10 HITs, Claude 24%→43% per-proposal); graph-as-proposer scored ~2% across both template shapes. The product architecture is settled: LLM proposer + graph tools (leads + duplicate-check + evidence) + adversarial kill-gate. Templates remain as LEAD GENERATORS (hole-seeded sessions drove QT's lift), not as claim emitters.
- Contamination probe method: date-recallability (closers post-training-cutoff = clean) beats citation-fame for young corpora; 6/11 of Claude's no-tools HITs are provably non-recall.
- who_tried_this as proposer-side duplicate-check is advisory only — gate kill rates unchanged. Keep the kill-gate as the binding check; do not trust proposer self-filtering claims.
- 2026-07-19: **Corpus scope widened beyond the charter's three subdomains** (operator): five new families — model forensics/weight archaeology, diffusion LLMs, RL-for-LLM, transformer foundations/DeepSeek/efficiency, image-gen/omni-modal — seeded at ~25-27 docs each (fan-out density rule: a topic below ~20 docs is a sparse island that generates spurious cross-cluster "gaps"). Templates now range over "LLM research broadly"; expect T1 transfer gaps across old↔new families.
- 2026-07-19: **Seed lists are agent-curated but fetch-verified** — every arXiv ID/URL confirmed live before ingestion (fabricated-ID guard); unverifiable sources dropped, never guessed (OpenAI 4o page dropped on 403+dead-Wayback).
- 2026-07-19: **JS-only pages get static-mirror rescue, same doc_id** — HF Spaces ingested from their *.hf.space static mirrors with doc_id keyed to the canonical URL hash, so `ingest seeds` re-runs stay no-ops.
- 2026-07-19: **Frontier runner: holes are leads, never candidates** — frontier.hole_leads computes T2 holes read-only and feeds them to proposer sessions; template candidates no longer enter the gate directly (templates scored ~2% in 4b; the validated loop is proposer+tools at 23-43%).
- 2026-07-19: **FG:<arm>:nnn candidate namespace** for frontier proposals (params.arm='frontier', proposer='qwen-tools'|'claude-tools'); insert() continues the sequence across runs so weekly re-runs append.
- 2026-07-19: **oMLX ops rule: diagnose by port, not process name** — the GUI app is a wrapper; the real listener is `omlx-server` (find via `lsof -nP -ti :8000`). After a Metal fault the engine pool caches load failures until restart, and a killed GUI can leave a zombie server racing a new one for the port. Always verify recovery through the SDK path, not curl.

## [2026-07-25] Public release — crowdsourced dossier review
- **The human pass over the 48 frontier dossiers is crowdsourced**: the operator lacks domain depth across the widened 8-family span, so the repo + dossiers + review page go public (r/LocalLLaMA) with a dispute-via-GitHub-issue mechanism. Confirmed "already done" disputes become ground truth for recalibrating the kill-gate.
- **MIT license; repo `gaztrabisme/litgraph`; review page on GitHub Pages (`/docs`).**
- **Fresh public history** (single release commit, noreply author): the 51-commit local history contains a `/Users/<name>` symlink target, a hardcoded local dev key, account balances, and the operator's name — scrubbed at HEAD, unscrubable in history without a rewrite. Local `main` keeps full history and must NEVER be pushed to the public remote (see gotchas.md).
- **Scrub policy**: personal name → "the operator"; account balances dropped (spend figures kept); private side-project names genericized; blunt-but-sourced critiques of prior work kept as-is (operator decision). oMLX dev-key fallback removed from llm.py (env-only now).
- **Corpus/DB/logs stay private**: full paper texts are not redistributable; the dossier JSONs are the reproducible public artifacts. `data/frontier-gaps-2026-07-19-contested.json` exported (7 FG dossiers) so the deadlocked cases are also reviewable.
