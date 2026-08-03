# Phase 0 Research Findings

**Status:** COMPLETE (2026-07-15). Three research streams merged: SciMuse deep dive, HySemRAG+GAPMAP patterns, base-repo survey. Synthesis + decision forks at bottom.

---

## SciMuse (arXiv 2405.17044, Gu & Krenn)

Repo: `artificial-scientist-lab/SciMuse` (MIT). Graph machinery inherited from prior **Impact4Cast** project (also MIT). No peer-reviewed journal version as of v3 (Jan 2025) — ICML 2024 workshop. Repo pivoted Feb 2025 into a benchmark leaderboard (LLMs ranked by ability to predict expert interest) — distinct from the original pipeline.

Correction to our charter's numbers: 4,451 personalized ideas evaluated by >100 group leaders; the interest model trains on 2,996 rated ideas with usable binary labels (not "~2,400 ratings").

### 1. Graph construction
- **Node = concept.** 123,128 final concepts. RAKE over titles+abstracts of ~2.44M preprints (arXiv/bioRxiv/chemRxiv/medRxiv, cutoff Feb 2023); 2-word concepts kept if in ≥9 articles, 3+-word if ≥6; NLP + manual cleanup; GPT-3.5 removed 286,311 entries; Wikipedia-title restoration added back 40,614. Concept list shipped as `data/full_concepts.txt`.
- **Edge = co-occurrence** in title/abstract of same paper, counted over **OpenAlex ~58M papers** (vocabulary learned from preprints, edges populated from OpenAlex).
- **Edges are dynamic + weighted**: carry time (year; graph spans 1665→Apr 2023, snapshot-able at any past year) and citation info. This payload enables the 144 features below.
- Not verified: exact edge count (varies by snapshot).

### 2. Candidate generation vs ranking split — THE key negative result
- Selection per researcher: personalized subgraph (their last-2yr papers → their concepts → neighborhoods), then pair chosen by (1) random connected pair, (2) highest-predicted-impact pair (Impact4Cast citation NN), (3) no pair at all (control — GPT-4 gets only paper titles).
- **Verbatim: "no significant difference in interest levels between projects generated using random concept pairs, high-impact concept pairs, or without concept pairs."** Topology/impact-driven selection did NOT beat random, or even no-graph. High predicted impact ≠ high interest.
- **144 per-pair graph features** (Impact4Cast's 138-feature scheme + extras), used by the supervised ranker, not selection:
  - Node: degree, neighbor growth, PageRank — at multiple year snapshots
  - Node-citation: yearly/cumulative citations, mention counts, citation-rank deltas
  - Pair: shared neighbors; cosine/geometric/Simpson/Sørensen-Dice/Jaccard similarity; preferential attachment
  - Pair-citation: ratios/sums/min/max over time windows
  - Plus semantic-distance between the two subnetworks + predicted-impact score as a feature
- **Shape: selection = topology or random; ranking = supervised NN over topological+citation features. The graph's value showed up only in ranking, not selection.**

**Implication for LitGraph:** this is direct evidence *against* naive "topology proposes" — SciMuse's untyped co-occurrence topology proposed no better than random. Our bet must be that (a) *typed* templates (transfer/contradiction/assumption) are a stronger proposer than untyped co-occurrence pairs, and (b) the adversarial killer replaces their supervised ranker. Their negative result is the strongest argument for the assassin stage: candidates are cheap and mostly boring; the filter is where value concentrates.

### 3. Interest-prediction model
- Small FC NN: 25 best features (of 144) → 1 hidden layer (50 neurons) → binary output. Dropout 20%, LR 0.003, MSE. Tree/forest variant underperformed.
- **Features purely structural/citation — no text embeddings; model never sees the idea text.**
- Labels: 2,996 ideas rated 1-5, binarized high (≥4) vs low. Monte Carlo CV (~130 iters).
- Results: graph-feature NN **AUC 64.5%, top-1 precision 70%, top-3 66.4%**. GPT-4o zero-shot ranking: AUC 67.3% but top-1 51%, top-3 45% — better AUC, much worse at the top.
- Directional: **semantic distance NEGATIVE** (cross-disciplinary distance hurt interest); **connectivity/citation-impact NEGATIVE**. No single topological "gap" metric predicts value — only the fitted combination.

### 4. GPT-4's role
Drafting only, never ranking. Inputs: the 2 concepts + ~7 paper titles per researcher (NOT subgraph structure, NOT abstracts). Self-reflection loop: generate 3 → critique → refine ×2 → pick best. Output: title + objective + 2 research questions.

### 5. Weaknesses & lessons
- Authors' own: selection method didn't matter (null result); small skewed evaluator pool; 25/144 features (interpretability); measures *interestingness-to-researcher*, explicitly NOT novelty-vs-prior-art or feasibility.
- **Core lesson: graph structure proposes fine but cannot rank alone.** No unsupervised topological signature of "interesting" — intuitive ones anti-correlate. Validates "structure proposes, LLM disposes" but warns: (1) the disposer needs an external grounding signal, not graph aesthetics; (2) topological templates over-select connectivity artifacts experts find boring.
- External critique: thin — cited in surveys, no substantive takedown; strongest critiques are their own null results.

### 6. Reusable assets (all MIT)
- `data/full_concepts.txt` — 123,128-concept vocabulary (general science; we'd re-extract for RAG/agents domain)
- Zenodo graphs: SciMuse 10.5281/zenodo.13900962; Impact4Cast dynamic graph 10.5281/zenodo.10692137; benchmark data 10.5281/zenodo.14527306
- Impact4Cast feature-generator code (138/144 temporal-citation pair features) — template for our pair features
- Interest-NN training harness (`create_full_data_ML_pkl.py`, `create_fig4.py`) — PyTorch+sklearn, <15min CPU
- **NOT published: the GPT-4 generation prompt** (only procedural structure in paper)

### 7. Backtest design notes (from their eval protocol)
1. Temporal backtest dodges their central weakness: subjective, expensive, non-reproducible human "interest" labels → our objective filled-at-T+k label is a genuine advantage.
2. Expect the same null on topology — treat topology as generator only; never rank gaps by graph score.
3. **Precision-at-top-N is the metric that matters** (their AUC looked mediocre while top-N was decent; report both). Ours: "of top-N gaps surfaced, how many did the field actually fill with strong later work."
4. Personalization was load-bearing for them; our corpus-global analogue = scope backtests per subfield so gaps are judged relative to a coherent neighborhood.

Verification flags: edge count unstated; GPT-4 prompt unavailable; 144 vs 138 feature-count version drift; workshop paper only (no journal version).

---

## HySemRAG + GAPMAP

### ⚠ Two corrections to our charter's premises
1. **GAPMAP does NOT show "ensemble beats best single model" quantitatively.** No voting ensemble, no ensemble-lift number. What it shows is *complementarity*: top-4 models (GPT-5, Llama-3.3-70B, Llama-4-17B, GPT-4o-Mini) surface largely disjoint gap sets (COVID-19: only ~1.14k shared items, many unique each; IPBES: ~50% shared). It *recommends* a mixture on that basis. Supports the ensemble direction qualitatively; gives no vote threshold to copy. AGENTS.md's "ensemble the killer (per GAPMAP)" citation needs softening. The quantified adversarial-ensemble evidence is **Refute-or-Promote (arXiv 2604.19049)** — see Newer Prior Art.
2. **HySemRAG is a single-author preprint, self-evaluated only** (643 observations, "controlled developer testing"); reports −1.9%/day temporal accuracy drift; **no KG-vs-vector ablation exists** — the KG's contribution is argued qualitatively only. Use its architecture as a design menu; don't trust its numbers.

### HySemRAG (arXiv 2508.05666) — usable plumbing
- **Retrieval**: 3 legs (Qdrant vector w/ text-embedding-3-large; keyword MatchText; Neo4j KG traversal) fused by **RRF with K=60**. Dual Qdrant collections: raw 7k-token chunks AND LLM-extracted structured fields (one embedding/doc) — lets you retrieve on research *attributes* separately from text. Worth stealing.
- **Validation loop** (single-critic pattern): Claude Sonnet 4 generates → Gemini 2.5 Flash audits (cross-family on purpose) → ≤3 regeneration cycles → final QA. Hard rule: every claim cites a single source; mixed-source citations auto-rejected.
- **Post-hoc citation audit**: cited evidence vs source text by content similarity; VALID ≥0.8 / POSSIBLY_VALID 0.5–0.8 / INVALID <0.5.
- Byproduct: every loop emits (query, rejected, accepted) preference triplets — free DPO data.
- Gap methodology itself is weak: LDA topic maps + Cypher method-frequency counts, eyeballed — no formal detection, no gap-quality eval. Exactly the hole LitGraph fills.
- ETL worth noting: Zotero/citeproc pre-renders citations so the LLM never fabricates them; Docling for PDF extraction; MiniLM cosine 0.55 for synonym unification.

### GAPMAP (arXiv 2510.25055, NeurIPS 2025, code: UCDenver-ccp/GAPMAP)
- **Taxonomy**: explicit gaps (lexical cues: "unknown", "further research needed") vs implicit gaps, 3 subtypes: missing-link chain (≈ our transfer gap), generalization gap (≈ our unexamined assumption), conflicting findings (≈ our contradiction). **No analogue to our structural hole** — topology-native, our genuine differentiator. Their explicit-gap class = cheap ground-truth seeds we could harvest.
- **TABI verdict schema** (adopt): every inferred gap forced into (Claim, Grounds=evidence spans, Warrant=1-sentence reasoning, Bucket=more/least_probable). Auditable, overlap-scoreable.
- **Ground truth**: the 137-article set **masks the future-directions sentence and asks the model to infer it** — a paragraph-level mini-backtest, directly relevant to our temporal backtest. Post-hoc author survey (18 authors): 83.3% agreed inferred gaps were factually true. No inter-annotator agreement reported (flag).
- **Prompting**: 3-shot mandatory — zero-shot collapses even GPT-5 into vague restatements. Exact prompts in repo (unfetched).
- **Failure modes**: confidence miscalibration (10-24% of correct gaps self-bucketed least_probable — don't threshold hard on self-confidence); over-extraction vs under-annotated gold (the argument for adversarial adjudication over recall scoring); small models fail implicit gaps (Gemma-2-9B 22.6%, Llama-3.1-8B 30.7% — **voters must be ≥70B/frontier**); **feasibility ≠ validity: 35% of proposed directions real-but-infeasible** → killer needs a separate feasibility axis.

### Design patterns to adopt (concrete)
1. **Kill-gate asymmetry**: ≥2 distinct model *families* as killers; any single credible refutation kills; survival requires unanimous clearance (Refute-or-Promote setting; ~79% false-positive elimination in their domain).
2. **Isolated killer context**: each assassin gets the candidate *without* the proposer's rationale or other killers' verdicts — removes anchoring/sycophancy.
3. **TABI verdict schema** with Grounds = doc IDs from our index (matches charter's cite-in-index rule).
4. **Post-hoc citation audit** on every kill: verify the "killing paper" actually says what the killer claims (content similarity 0.8/0.5 thresholds). A hallucinated kill silently discards a real gap — the assassin's most dangerous failure.
5. **Feasibility axis separate from novelty** for survivors.
6. **Few-shot the killer**: one exemplar kill + one exemplar survival minimum.
7. **Log (candidate, kill-verdict, survive-verdict) triplets** — free preference data for a future tuned killer.
8. **RRF K=60** for multi-leg retrieval; chunks + structured-attributes dual collections.

### Newer prior art (2025-26)
- **Refute-or-Promote (arXiv 2604.19049)** — READ IN FULL before Phase 2. Adversarial kill-mandate reviewers w/ isolated context → empirical validation → cross-model critic; unanimous-survival gate; ~79% FP elimination (vuln-discovery domain, but the mechanism IS our assassin). The paper the charter should cite for ensemble mechanics.
- **EXATLAS (arXiv 2605.27153)** — closest published analogue to our temporal backtest: study-archive graph with link/conflict/gap trichotomy; predict-then-check on held-out studies; 98.6% direction recovery on locally-supported targets.
- **Deep Ideation (arXiv 2511.02238)** — concept-network ideation w/ critic engine trained on real reviewer feedback; alternative to a prompted killer.
- **Ideation-Execution Gap (arXiv 2506.20803) + Human-vs-LLM ideas (2607.01233)** — LLM ideas score novel but underperform on feasibility → supports the feasibility axis.
- **Bayes-Entropy Collaborative Agents (arXiv 2508.01746)** — calibrated/uncertainty-weighted aggregation as an alternative to hard voting.

---

## Synthesis — what Phase 0 research changes

1. **The assassin is now evidence-backed, but by a different paper than the charter cites.** Swap the GAPMAP ensemble citation for Refute-or-Promote's kill-gate mechanics; keep GAPMAP for complementarity (mix model families) and the TABI schema.
2. **SciMuse's null result sharpens our bet**: untyped topology proposes no better than random; intuitive gap metrics anti-predict interest. LitGraph's wager = *typed* templates propose better AND the killer (grounded in index evidence, not graph aesthetics) replaces their supervised ranker. The temporal backtest is the only way to know — it dodges their subjective-label weakness entirely.
3. **Both prior systems lack a gap-quality eval.** HySemRAG eyeballs LDA maps; GAPMAP's gold is under-annotated. Our backtest + precision-at-top-N is genuinely novel rigor (EXATLAS is the only close analogue).
4. **Build strategy: own a small codebase, transplant designs** (nano-graphrag-sized skeleton; Graphiti's bi-temporal + Pydantic ontology; ATOM's cosine incremental merge; fast-graphrag's PPR loop; MS graphrag's Leiden pipeline). NetworkX+DuckDB for MVP, Neo4j escape hatch.
5. **New requirements surfaced**: killer citation-audit step; feasibility axis; 3-shot killer prompts; ≥70B-class voters only; explicit-gap harvesting as backtest seeds.

## Base repo survey (July 2026, live GitHub state) — COMPLETE

**BLUF (agent's):** No single repo is a drop-in runtime for gap templates + PPR + temporal backtest. High-value assets are *designs*, not systems. Temporal-native repos (Graphiti, iText2KG→ATOM) worth mining hardest; smallest ownable skeleton is nano-graphrag; newer entry **circlemind fast-graphrag** is the closest design match to our PPR retrieval. Agent recommends: own a small NetworkX+DuckDB codebase, transplant designs from these four. (Full reasoning in part 3.)

### LightRAG (HKUDS)
- (a) prompts swappable: yes (but untyped keyword extraction — imposing our typed schema = big replace); (b) store exposed: yes (Neo4j/Memgraph/OpenSearch), export path unclear; (c) temporal: **no**; (d) ER hooks: weak/implicit; (e) ownable: **no** (8,741 commits, ~37.7k stars, server/UI/storage sprawl)
- MIT · v1.5.5rc1 (2026-07-13) · very active
- **Move: mine the design** — dual-level retrieval + multi-store abstraction reference only.

### microsoft/graphrag
- (a) prompts swappable: yes (Prompt Tuning Guide, untyped default); (b) store: **best export story but no graph DB** — in-memory NetworkX → Parquet + optional GraphML + LanceDB; (c) temporal: **no**; (d) ER: internal LLM merge, no clean hook; (e) ownable: **no** (workflow-engine architecture)
- MIT · v3.1.0 (2026-05-28) · active
- **Move: mine the design** — Leiden community-detection pipeline (→ our structural-hole template) + prompt tuning approach.

### nano-graphrag (gusye1234)
- (a) prompts: **yes, cleanly** — `PROMPTS` dict, `entity_extraction` one replaceable string; storage/embed fns swappable; (b) store: yes — NetworkX default (Neo4j optional), GraphML export; (c) temporal: **no**; (d) ER: minimal but everything replaceable; (e) ownable: **best in shortlist** — ~1,100 LOC excl. prompts/tests
- MIT · **maintenance stalled** (v0.0.8, 2024-10-01; 68 open issues) — a fork is a permanent hard fork
- **Move: fork or mine** — the small ownable skeleton; bolt on temporal + ER from elsewhere.

### graphiti (getzep) — temporal standout
- (a) **typed ontology natively** — custom entity/edge types via Pydantic, our 9-node/9-edge schema maps directly; (b) store: Cypher-queryable (Neo4j 5.26+, FalkorDB primary, Neptune; **Kuzu backend deprecated upstream — avoid**); (c) temporal: **yes, bi-temporal is the core feature** — validity windows, superseded facts invalidated not deleted, "what was true at time T" = our backtest primitive; (d) ER: **built in** — embedding dedup + contradiction handling, incremental; (e) ownable: medium-heavy (multi-package)
- Apache-2.0 · v0.29.2 (2026-06-08) · very active
- Caveat: built for episodic agent memory; retrieval = hybrid search, **no community detection or PPR** — gives the ingestion/temporal/typed/ER front-half, nothing for the gap-query back-half.
- **Move: use as library for ingestion, OR mine the bi-temporal edge model + Pydantic ontology.**

### itext2kg → ATOM (AuvaLab) — repo evolved, EACL 2026
- (a) typed but fixed: LLM extracts 5-tuples (subject, predicate, object, t_start, t_end); free-text predicates, not a rich swappable ontology; (b) Neo4j export, plain Python graph before export; (c) temporal: **yes, dual-time** — observation time vs validity window (avoids temporal misattribution); (d) ER: **the gem** — incremental cosine merge (entities 0.8, relations 0.7; entity embedded as name·0.8 + label·0.2), parallel binary-merge; (e) ownable-ish but **heavily LangChain-coupled**
- Apache-2.0 · v1.0.0 (2025-10-28) · paper arXiv 2510.22590
- **Move: mine** — lift dual-time tuple model + cosine incremental merge; skip the LangChain runtime.

### New 2025-26 candidates
- **circlemind-ai/fast-graphrag** — **closest design match to our retrieval**: Personalized PageRank over KG (HippoRAG-based), typed extraction via swappable ENTITY_TYPES. MIT, ~3.8k stars, but no releases + maintenance uncertain (flagged). No temporal. **Mine the PPR retrieval loop.**
- **topoteretes/cognee** — heavyweight active (27.8k stars, v1.3.0 2026-07-12), ECL pipeline, multi-store. Too big, analytics not gap-oriented; temporal depth unverified. Skip unless we want batteries-included ingestion.
- **DIGIMON** — modular GraphRAG testbed; reference for query-method modularity only (not deep-verified).

### Agent recommendation
**Adopt the design, not the system** — our back-half (gap templates, community detection, PPR, beam search, as-of-cutoff rebuild) is custom graph algorithmics no runtime exposes. Transplant table:

| Asset | Donor |
|---|---|
| Minimal extraction→graph pipeline skeleton | nano-graphrag (fork) |
| Bi-temporal edge model + Pydantic typed ontology | Graphiti |
| Cosine incremental entity/relation merge (ER stage-1) | iText2KG/ATOM |
| Personalized-PageRank retrieval loop | fast-graphrag |
| Leiden community pipeline + prompt-tuning method | microsoft/graphrag |

- **Top choice: Graphiti** as primary design donor (or as ingestion library on a real graph DB).
- **Runner-up: nano-graphrag** as hard-fork skeleton when we'd rather own every line.
- **Graph store (MVP): NetworkX + DuckDB** — PPR and Leiden are one-liners (nx.pagerank personalization, graspologic/cdlib); DuckDB holds timestamped node/edge tables; backtest = `WHERE valid_from <= cutoff` → rebuild in-memory graph; zero server, fully ownable. **Escape hatch at scale: Neo4j** (GDS has both Leiden and personalized PageRank; natural host for Graphiti-as-library). **Avoid Kuzu** (Graphiti backend deprecated upstream).

Flagged/unverifiable: exact LOC of large repos; fast-graphrag graph lib + commit recency; cognee temporal depth; DIGIMON state.
