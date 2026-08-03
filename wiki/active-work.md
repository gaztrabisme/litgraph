# Active Work

## Phase 4a — Corpus scale + DeepSeek extraction
**Status:** COMPLETE (2026-07-17) — all gates pass; see log.md + phase4-design.md
**Result:** corpus 588 → 1,154 docs (+RAG, +agent-harness); uniform re-extraction on deepseek-v4-flash (99.8% ok, $2.15); graph 23,503 nodes / 27,056 edges / 75 communities, m/c 1.48.

### Next (in order)
- [x] Assassin critic comparison (task #11): 8/15 agreement, DeepSeek 0 errors/$0.05/17x faster BUT 1 specious kill exploiting W5 → keep Qwen default; ensemble both is the recommended path once W5 relevance-verifier lands
- [x] W5 relevance gate + Stage-A ensemble (Qwen+DeepSeek) shipped (65efcea); gap engine over all 77 candidates: 33 killed / 27 survived / 17 contested, C5 gates pass, W5 blocked 25% of kill attempts (log.md 2026-07-17)
- [x] 4b: backtest at scale + recall pre-pass (1c7e89e): baseline 10 HIT/0 LEAK/18 OPEN vs graph 0/0/8 — recall pre-pass was the binding constraint; see log.md 2026-07-17 for caveats (baseline contamination asymmetry, claim-shape mismatch, n=8)
- [x] **Template redesign forks BOTH tested (2026-07-18, operator: "test all"):** paper-shaped T2 → 0 HIT / 1 LEAK / 4 OPEN (first project LEAK — see decisions.md); longitudinal hole scoring → too noisy to adopt. Cumulative graph arm: 13 scored, 0 HIT vs baseline 10/28. Shape wasn't the (only) problem.
- [x] Qwen-local baseline arm + auditor-bug remediation (2026-07-18): CORRECTED scoreboard — graph 1 HIT/24, paper-T2 0 HIT+1 LEAK/18, Claude 10 HIT/28, Qwen 1 HIT/23. Every non-Claude arm scores ~1; Claude is the outlier. See log.md.
- [x] Contamination probe + graph-tools proposer arms (2026-07-19): ANSWERED. 6/11 Claude no-tools HITs provably non-recall (post-training-cutoff closers). CT 13 HIT/30 proposals (43%), QT 10/44 (23%, 10x Qwen's no-tools 1/23) — graph tools lift both families; architecture verdict in decisions.md: LLM proposes, structure informs and disposes.

### Session checkpoint (2026-07-19, pre-compact)
Tree clean at 887edd2; no background jobs in flight; all cycle tasks closed.
Pending the operator's answer: build the frontier-gaps runner (validated loop on the FULL
current graph → first real dossier set)? Explained in-session; next action on "yes":
thin runner wiring hole-detection → hole-seeded proposer sessions (Qwen and/or
Claude + tools) → kill-gate → readable dossiers, all on litgraph.duckdb (NOT the
snapshot). Everything it needs already exists: proposer.py, proposer_tools_*_v1
prompts, snapshot_tools.py pattern (repoint at main DB), assassinate, litgraph gaps.

- [x] Seed expansion (2026-07-19, operator): 5 new topic families (forensics, dLLM, RL-for-LLM, foundations/DeepSeek, image-gen/omni), 131 docs ingested+extracted, graph rebuilt — 24,782 nodes / 29,749 edges / 68 communities. `ingest arxiv --ids-file` shipped. See log.md.

### Next (4c-facing, awaiting operator)
- [x] Productize the validated loop (2026-07-19): `litgraph frontier leads|qwen` + Claude Agent-session arm shipped; first dossier set delivered — **48 survivors** in data/frontier-gaps-2026-07-19.json (claude 16, qwen 32; 7 killed, 7 contested). See log.md.
- [x] Public release (2026-07-25): repo gaztrabisme/litgraph (MIT, fresh history), review page on Pages, dispute issue template; dossier review crowdsourced to r/LocalLLaMA. See log.md.
- [ ] **Monitor release feedback**: GitHub gap-dispute issues + Reddit thread; confirmed "already done" disputes → ground truth for kill-gate recalibration
- [ ] Operator: rotate DeepSeek API key; review + post the Reddit draft (scratchpad/reddit_post_draft.md)
- [ ] 4c live mode: weekly re-ingest + frontier diffs, now with the settled architecture
- [ ] Blog/repo seed expansion (hand-curation)
- [ ] Human pass over contested dossiers (~29 across main + backtest4b) — partially covered by the public release (7 FG contested are on the review page)
- [ ] 4b: blog/repo seed expansion (hand-curation)
- [ ] Human pass over contested dossiers: 17 main-graph + 25 graph-arm + 6 baseline in backtest4b (`litgraph gaps --status contested`)

## Phase 3 — Temporal backtest (eval)
**Status:** COMPLETE (2026-07-17) — pipeline ran end-to-end both arms; answer-key too sparse for an arm-vs-arm verdict; see log.md + phase3-design.md
**Result:** cutoff 2024-07-01 (120 papers in-world, 440 post-cutoff as answer key). Graph arm: 61 candidates → 21 survivors → 0 HIT / 0 LEAK / 21 OPEN. Baseline arm (Claude deep-research, 42 claims through the same kill-gate): 32 survivors → 1 HIT / 0 LEAK / 31 OPEN. 7 scoring kills issued; strict closure judge affirmed 1 (BL:003, HippoRAG-PPR→conversational memory, closed by arXiv 2510.10397), rejected 6 as merely-topical. Zero LEAKs = no contamination detected in decided cases.
**Read:** machinery validated (evidence-verified kills, judge discriminates topical from closing, both arms symmetric); the eval is UNDERPOWERED at this corpus size — 1 decided case of 53 cannot rank graph vs baseline. Scaling the corpus is what makes this eval bite.

### Follow-ups
- [ ] Scoring-critic recall is the weak link: 440 post-cutoff papers vs who_tried_this+full_text search. Consider embedding-retrieval pre-pass feeding candidates' nearest post-cutoff docs to critics.
- [ ] Human spot-check: sample ~10 OPENs against the post-cutoff corpus to estimate missed-closure rate.
- [ ] Baseline survived the kill-gate at 76% vs graph arm's 34% — self-filtered LLM claims are harder targets for critics with no node grounds; audit whether kill-gate leans on node_ids.
- [ ] Known limits carried from spec: papers-only snapshot; arXiv-latest-revision leak (W1); vocabulary bias (W3); N too small for significance.

## Phase 0 — Research & repo selection
**Status:** COMPLETE (2026-07-15) — all forks decided, see decisions.md
**Goal:** Study prior art, pick base-repo strategy, decide graph store. ✓

### Outcome
- Research merged into [phase0-research.md](phase0-research.md); forks decided: own small codebase, NetworkX+DuckDB, charter patched, git initialized (wiki tracked)

## Phase 2 — Gap engine
**Status:** COMPLETE (2026-07-16) — all acceptance gates pass; see log.md + phase2-design.md
**Result:** 57 candidates → 33 killed / 15 survived / 9 contested; every verdict evidence-verified; cross-family (claude -p) changed 9 dispositions. Survivors: `litgraph gaps --status survived`.

### Next Steps (Phase 3 — eval, charter: non-negotiable before scaling corpus)
- [ ] Temporal backtest: rebuild graph --as-of Jan 2025, propose+assassinate, score against 2025-26 reality
- [ ] Baseline comparison: same answer key vs a well-prompted deep-research agent over raw corpus
- [ ] Human pass over the 9 contested dossiers (the review queue exists for exactly this)
- [ ] GAPMAP explicit-gap harvesting as backtest ground-truth seeds
- [ ] Known limits to carry: verifier checks provenance not relevance (W5); T3 near-inert at current scale; T1 thin (n=6, single-subdomain corpus)

## Phase 1 — Ingestion MVP
**Status:** COMPLETE (2026-07-16) — all acceptance gates pass; see log.md + phase1-design.md
**Goal:** ~200 docs, agent-memory subdomain; typed extraction w/ v0 ontology; raw extractions stored separately from canonical nodes; ER pass with mentions-per-canonical-node metric.
**Spec:** [phase1-design.md](phase1-design.md) (incl. spec-review resolutions C1-C3/W1-W13)

### Build pipeline state
- [x] Contracts scaffolded + committed (models/db/llm, extraction_v1 prompt)
- [x] Spec cold-review: 3 CRITICALs + 13 WARNINGs → all resolved into spec (commit c72419b); W3 verified by live oMLX call (schema enforcement works through $refs)
- [x] seeds.yaml: 30 verified sources committed
- [x] Test wave: 5 agents, 52 tests total, all contract-traceable names
- [x] Implementation wave: 5 modules, every suite independently re-verified by coordinator (52/52)
- [x] Verification: full suite green; ruff 0 errors; bandit → 1 real finding (B314 xml parse at trust boundary) fixed via defusedxml; commit 104fce8
- [x] Adversarial cold review: 0 CRITICAL, 4 WARNINGs fixed (commits 5a4981e..71853bd)
- [x] Live run: 198 docs, 96% ok-rate, ratio 1.488/1.546, 4,029 edges/8 types — ALL GATES PASS

### Next Steps
- [ ] Read Refute-or-Promote (2604.19049) in full before Phase 2 killer design
- [ ] Fetch GAPMAP repo prompt templates (UCDenver-ccp/GAPMAP)
- [ ] Phase 1: ingestion MVP (~200 docs, agent-memory subdomain)

### Breadcrumbs
- 2026-07-15: Graphiti's Kuzu backend deprecated upstream — avoid Kuzu for backtest
- 2026-07-15: SciMuse GPT-4 generation prompt never published — reconstruct from paper §4 procedure if needed
- 2026-07-15: GAPMAP explicit-gap class = cheap ground-truth seeds for backtest
- 2026-07-15: nano-graphrag frozen since 2024-10 — forking it = permanent hard fork

## Backlog (post-Phase-2 candidates, from the operator 2026-07-15)
- **Chat/MCP surface over LitGraph**: Phase 2's four assassin tools double as a query API; wrap as MCP server (same pattern as an existing private knowledge-base MCP). Chunk-embedding index needed for semantic prose search (match/context chunking split).
- **Knowledge-base upgrade pilot** (private side project): typed extraction over 2-3 textbooks (RAG/DB domain), add graph tools (who_uses/contradicts/subgraph_around) to that MCP. Gate: LitGraph Phase 1+2 validated first, AND graph tools must beat chunk-search baseline on ~20 relational questions (graph layers must earn their place). Ontology tweak needed (EXPLAINS/RECOMMENDS_AGAINST vs IMPROVES_ON/CITES). Shared Jina v5 embedding space makes cross-system similarity coherent.
