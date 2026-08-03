# Phase 2 Research — Gap Engine Inputs

## Graph recon (2026-07-16, live litgraph.duckdb, coordinator)

Template feasibility over the Phase 1 graph (3,126 nodes / 4,029 edges / 189 edge-contributing docs):

1. **Transfer gap as charter-spec'd is starved.** "Problems A,B share ≥2 other methods"
   yields **2 pairs** via APPLIED_TO (13 via any edge type). The corpus is one subdomain, so
   problems concentrate on a hub: "long-term conversational memory" has 46 APPLIED_TO methods,
   next is 11. Design must relax the precondition — embedding similarity as the primary
   pairing signal, ≥1 shared method, and/or count USES/EVALUATED_ON as "applied".
2. **Structural hole has one giant component**: 2,575 non-isolate nodes, giant component
   2,365, next component 35. Communities must be computed *within* the giant component
   (Leiden/Louvain on undirected projection); cross-component "holes" are just disconnection.
3. **No stored node embeddings.** resolve.py embeds names transiently; transfer-gap sim(A,B)
   and structural-hole bridging need vectors. Add a `node_embeddings` table (node_id PK,
   model, vec FLOAT[1024]) populated post-resolve — 3,126 names ≈ 1 min via jina batch.
4. **CONTRADICTS is Method→Claim-shaped, not Claim-vs-Claim.** 133/206 are Method→Claim,
   47 Method→Method; only 2 Claim→Claim. Charter template 4 ("Paper X claims Y improves Z;
   Paper X' claims it doesn't") must match the data: treat any CONTRADICTS edge as a
   candidate and let evidence quotes carry the claim content.
5. Assumptions: 288/300 have ≥1 edge (344 ASSUMES) — template 3 has material once
   communities exist (needs "appears in only one cluster" so depends on #2's clustering).
6. Edge-type spread (mention-edges / distinct canonical pairs): EVALUATED_ON 1392/1229,
   USES 733/697, IMPROVES_ON 437/381, ASSUMES 344/337, APPLIED_TO 341/331, CITES 327/301,
   MOTIVATED_BY 249/241, CONTRADICTS 206/203.

## Refute-or-Promote deep read (2026-07-16, agent rp-reader, verified against arXiv 2604.19049)

**Framing caveat:** it's a security-vulnerability-discovery paper (candidates = defects,
ground truth = CVEs/committee acceptance), not a research-gap paper. Mechanics transplant;
numbers don't.

### Kill-gate protocol (what we adopt)
- Four stages A–D. **Decision rule = any-refutation-kills AND-gate, not majority.**
  "A candidate survives Stage A only if no adversarial agent produces a code-grounded
  refutation." Stage A: 1 creative + 2 adversarial; Stage B: 2 creative + 3 adversarial
  (one senior tier); Stage C: mandatory empirical confirmation; Stage D: cross-model-family
  critic with minimal context.
- **Context asymmetry is load-bearing:** adversaries get "only the candidate claim, not the
  creative agent's reasoning" — anti-anchoring. Stage B runs parallel contexts: informed
  attacker / naive attacker / senior with selective summary. "Cold-start agents that
  independently reach a different conclusion provide higher-value signal than consensus
  among informed agents."
- Kill distribution: Stage A ~63% of entrants, B ~42% of survivors, cross-model critic ~3% of kills.
- **Candidate generation (SCH):** hunters stratified by source/scope/wave, each with a
  self-critique requirement — must articulate why its own candidate might be wrong.

### Numbers, in context
- ~79% = retrospective (135/171 killed); 83% = prospective controlled (25/30).
- **Recall is unmeasured by design** — "structural mechanisms against false positives …
  relies on the human orchestrator to rescue true positives incorrectly killed."
  Documented false-kill: lcms2 CubeSize overflow unanimously killed ("DO NOT FILE"),
  later confirmed as CVE-2026-41254 — rescued only by a human-triggered role-swap agent.
- **No ablations** ("leave-one-out ablation was not performed") — critic counts/model mix
  are asserted, not tuned. Open knobs for us.

### What does NOT work (their words)
- Adversarial framing alone: "the methodology's most instructive failure was killed not by
  better adversarial framing but by a single empirical test" (Bleichenbacher: 80+ agents
  incl. senior arbiter unanimously confirmed a nonexistent padding oracle; one fresh-context
  empirical test killed it — correlated training-data priors).
- **Unanimity is a low-signal event in both directions** (unanimous FP and unanimous
  false-kill both happened) → treat consensus as a re-examination flag, not confidence.
- Same-family convergence-by-debate amplifies shared priors; cross-family critic caught
  3 bugs + 16% bad fixes that extra same-family agents missed.

### Gaps in the paper we must fill ourselves
- **No published prompts/schemas** — the 56 codified rules + strat.md playbook live in an
  unlinked GitHub/Zenodo release (URL placeholder unresolved; UNVERIFIED). Worth chasing later.
- **No citation schema / hallucinated-refutation defense** — kill bar is "code-grounded
  refutation," never rigorously defined. This is exactly LitGraph's core axis (in-index
  citation-grounded verdicts) — design it ourselves, don't inherit.
- Their Stage C empirical gate has no analog for us; our substitute is evidence-cited
  refutation checked against the index.

### Design implications adopted for Phase 2
1. Any-refutation-kills AND-gate + context-isolated critics (gap claim only, no proposer reasoning).
2. Cross-family critic stage (Qwen local + a second family, e.g. Claude, for the final gate).
3. Self-critique at proposal time (template output includes "why this might not be a gap").
4. **Resurrection agent with confirm mandate after unanimous kills** — their proposed-but-
   unbuilt fix; mandatory for us since no human reviews our kills.
5. Report kill-rate but do NOT expect 79/83% to transfer; recall tolerance is ours to set
   (Phase 3 temporal backtest is the recall instrument).

## GAPMAP mining (2026-07-16, agent gapmap-miner, verified against repo + arXiv 2510.25055)

Repo `UCDenver-ccp/GAPMAP` is near-empty: one prompt file (`scripts/ex_gap_xtract.py`,
explicit-gap extraction, GPT-5). README advertises TABI templates/eval scripts that are
NOT committed. The TABI implicit-gap prompt and its 3 exemplars are unpublished — schema
reconstructed from paper prose only; we author our own exemplars.

**Corrections to shorthand we'd been carrying:** GAPMAP has NO voting/ensemble/quorum
mechanism and no "≥70B voter" rule — single model + self-reported confidence Bucket.
(Phase 0's "voters must be ≥70B" stands as OUR requirement derived from their small-model
failure rates: Gemma-2-9B 22.6%, Llama-3.1-8B 30.7% on implicit gaps.) Ensemble-of-families
is motivated by GAPMAP complementarity + R-o-P cross-family critic, nothing else.

### Transplantable verbatim (from ex_gap_xtract.py)
- Explicit-gap prompt: extract "explicit uncertainty, limitation, missing evidence,
  contradiction, or untested area stated by the authors"; fields
  `{Ignorance Statement (exact sentence), support_sentence/s[], justification, Ignorance Cues[]}`;
  "Use only the provided document"; return `[]` if none. (Their prompt has a Statement/
  "Ignorance Statement" key inconsistency — fix on transplant.)
- Plumbing worth copying: sentence-aligned ≤1000-token chunking; dedup by normalized
  statement; per-doc error diagnostics. (Their JSON-salvage regex is unnecessary for us —
  oMLX schema enforcement.)
- Explicit-gap harvesting = cheap ground-truth seeds for the Phase 3 backtest (per Phase 0).

### TABI schema (reconstruct, don't copy — prompt unpublished)
- CGW+Bucket: Claim ("the implied gap"), Grounds (evidence spans), Warrant (one sentence
  reasoning Grounds→Claim, used as entailment sanity check), Bucket ∈ {more_probable,
  least_probable} (self-confidence; weak signal — 10-24% of correct claims mis-bucketed).
- 0-shot implicit inference degenerates "into vague restatements or unsupported
  speculations" even on GPT-5 — 3-shot + template is mandatory for any gap-articulation step.
- Eval trick worth stealing: bi-directional entailment (RoBERTa-large, threshold 0.4)
  between predicted and gold claims; Warrant-vs-Grounds entailment as a cheap self-check.
- Feasibility: the 35% real-but-infeasible figure is a HUMAN survey finding, not a
  classifier — no prompt exists to copy. Feasibility axis in our verdict schema is ours to design.

### Verdict-record fields adopted for Phase 2
Gap candidate = CGW+Bucket-shaped: `{claim, grounds[] (in-index node/doc refs), warrant,
self_critique (why this might not be a gap — R-o-P SCH), template, params}`. Assassin
verdict = `{verdict: kill|survive, refutation_claim, evidence[] (in-index doc_ids + quotes),
feasibility, confidence_bucket}`.
