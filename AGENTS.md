# LitGraph — Gap Explorer for the RAG / Agent-Harness / Agent-Memory Literature

## What this project is

A knowledge graph built over the RAG, agentic-harness, and agent-memory literature
(papers + blog posts + GitHub repos), with an **adversarial LLM gap explorer** on top.
The goal is a live map of the field's frontier: what's settled, what's cutting edge,
and — the core deliverable — **typed, defensible research gaps** ranked by plausibility.

Inspiration: InfraNodus's structural-gap feature, but rebuilt on a typed entity graph
instead of word co-occurrence.

## Why this isn't a SciMuse clone (positioning)

Prior art exists and must be studied, not reinvented:

- **SciMuse** (arXiv 2405.17044, repo: `artificial-scientist-lab/SciMuse`) —
  concept co-occurrence graph over 58M OpenAlex papers + GPT-4 idea generation,
  validated with 100+ Max Planck group leaders. Closest prior art.
- **HySemRAG** (arXiv 2508.05666) — hybrid semantic + structured + KG-traversal
  retrieval with multi-agent validation, aimed at methodological gap analysis.
- **GAPMAP** (arXiv 2510.25055) — LLM gap detection in biomedical lit; key finding:
  different LLMs surface largely *disjoint* gap sets (complementarity), so mix
  model families — note: no quantified ensemble-vs-best-single result.
- **Refute-or-Promote** (arXiv 2604.19049) — adversarial kill-gate mechanics:
  isolated-context reviewers with a refute mandate, any-refutation-kills /
  unanimous-survival, cross-family critics; ~79% false-positive elimination.
- Adjacent: SciMON, ResearchAgent, SciAgents, SCI-IDEA, ORKG, OpenAlex.
- Curated lists to mine: `DEEP-PolyU/Awesome-GraphRAG`, `zjukg/KG-LLM-Papers`,
  `XiaoxinHe/Awesome-Graph-LLM`.

**Our differentiators (defend these in every design decision):**

1. **Corpus**: prior systems target peer-reviewed domains (biomed, materials).
   The RAG/agents field's real knowledge lives in arXiv preprints, engineering
   blogs (Anthropic, LangChain, LlamaIndex...), GitHub READMEs, and eval
   leaderboards. Nobody has built a gap explorer over a blogs+repos corpus.
2. **Typed full-text extraction**: cheap 2026 inference lets us extract Claims,
   Assumptions, and Contradictions from paper *bodies* — a graph SciMuse
   couldn't afford to build in 2024. The upgrade is at ingestion, not generation.
3. **Adversarial filtering**: SciMuse's weakness was ranking (they needed a
   trained model on human ratings to find interesting ideas). We filter instead:
   the agent's job is to *kill* candidate gaps, not to generate ideas.

## Core architecture decisions (settled — don't relitigate without new evidence)

- **Structure proposes, LLM disposes.** Gap candidates come from graph topology
  via typed templates. The LLM never free-generates gaps. Rationale: LLM-only
  gap generation produces plausible slop; retrieval-based agent exploration is
  biased toward what exists, and closed-world absence is an *index property*,
  not a reasoning property — `MATCH (m:Method)-[:APPLIED_TO]->(p:Problem)` gives
  a complete answer over the indexed world; "I searched and found nothing" does not.
- **The agent is an assassin, not a muse.** Given one candidate gap + tools
  (`subgraph_around`, `full_text`, `evidence_for`, `who_tried_this`), its task is
  adversarial: find the paper that already did it, or the reason nobody would
  (stupid idea / missing datasets / tried-and-failed quietly). Survivors promote.
- **LLM traversal = reranker over cheap candidates.** Personalized PageRank /
  beam search over `embedding_sim × edge_weight` produces ~50-node candidate
  sets; LLM picks the 10 that matter. Never let the LLM choose every hop.
- **Contamination guard.** 2026 models have read this literature. An agent
  "finding" a gap may be recalling a paper. Mitigations: (a) every kill or
  survival verdict must cite a node/document in *our* index; (b) temporal
  backtest (below) is the only trusted eval.
- **Entity resolution is a first-class subsystem**, not a cleanup step. This
  field renames everything (HyDE vs "hypothetical document embeddings"; six
  things called "agentic RAG"). Pipeline: embedding merge → LLM adjudication →
  alias table. Without it, centrality metrics are garbage.

## Ontology (v0 — expect iteration, but start here)

Node types: `Paper`, `BlogPost`, `Repo`, `Method`, `Problem`, `Dataset`,
`Metric`, `Claim`, `Assumption`.

Edge types: `USES`, `IMPROVES_ON`, `CONTRADICTS`, `EVALUATED_ON`, `ASSUMES`,
`MOTIVATED_BY`, `APPLIED_TO`, `IMPLEMENTS` (repo→paper), `CITES`.

Source-specific signals: citations for papers (OpenAlex / Semantic Scholar
APIs); stars/downloads/dependents as adoption proxies for repos; none of these
are interchangeable — keep them as separate node properties.

## Gap templates (computable, typed)

1. **Transfer gap**: Method M applied to Problem A, never to Problem B, where
   sim(A,B) ≥ 0.8 and A,B share ≥ 2 other methods.
2. **Structural hole**: two dense clusters, high internal modularity, near-zero
   cross edges, sharing a bridging entity type.
3. **Unexamined assumption**: Assumption nodes appearing in only one cluster.
4. **Contradiction**: Paper X claims Y improves metric Z; Paper Z' claims it
   doesn't. Index these explicitly — nobody in the RAG lit does.

## Phased plan

### Phase 0 — Research & repo selection (do this first)
- Read the SciMuse repo and paper: how exactly is gap-scoring implemented?
  What's their candidate-generation vs ranking split?
- Read HySemRAG and GAPMAP for the agentic/ensemble patterns.
- Survey candidate base repos to fork/customize. Shortlist to evaluate
  (verify current state — training data may be stale):
  - `HKUDS/LightRAG` — lightweight, hackable graph-RAG; good customization target
  - `microsoft/graphrag` — heavyweight, strong community-detection pipeline,
    harder to bend
  - `gusye1234/nano-graphrag` — minimal GraphRAG reimplementation, easiest to gut
  - `getzep/graphiti` — temporal KG with entity resolution built in; temporal
    edges matter for the backtest
  - `AuvaLab/itext2kg` — LLM entity/relation extraction to KG
  - Selection criteria: (a) swappable extraction prompts, (b) doesn't hide the
    graph store, (c) temporal metadata on edges, (d) sane entity-resolution
    hooks, (e) small enough to actually own.
- Decide graph store: Neo4j vs NetworkX+DuckDB vs whatever the chosen repo uses.
  Bias toward boring and queryable.

### Phase 1 — Ingestion MVP (~200 documents, one subdomain: agent memory)
- Sources: arXiv API (cs.CL/cs.AI filtered), OpenAlex for citation edges,
  hand-curated blog/repo seed list (~30 items).
- Full-text typed extraction with the v0 ontology. Store raw extractions +
  canonical nodes separately (re-canonicalization must be possible without
  re-extracting).
- Entity resolution pass. Measure: how many raw mentions per canonical node?
  If it's ~1.0, resolution is broken.

### Phase 2 — Gap engine
- Implement the 4 gap templates as graph queries.
- Build the adversarial agent with the 4 tools. Every verdict must cite
  in-index evidence.
- Ensemble the killer (per GAPMAP): 2–3 different models vote on kill/survive.

### Phase 3 — Eval (non-negotiable before scaling corpus)
- **Temporal backtest**: rebuild the graph as of a past cutoff (e.g. Jan 2025),
  generate gaps, score against what the field actually did in 2025–26.
  This doubles as the contamination-resistant benchmark AND the headline
  result if it works. It also settles the deferred question of whether the
  graph layer beats a well-prompted deep-research agent over raw corpus —
  run both pipelines against the same answer key while we're here; it's
  nearly free at this point and de-risks the architecture.

### Phase 4 — Scale + live mode
- Full corpus, weekly re-ingest, "what changed at the frontier" diffs.

## Conventions
- Python, uv for env management. Ruff for lint.
- Every LLM extraction call: log prompt version, model, raw output. Extractions
  are data; treat prompt changes like schema migrations.
- Keep a `decisions.md` — one line per architectural decision with date and why.
- Cheap models for extraction volume, frontier models only for the adversarial
  agent and adjudication.
