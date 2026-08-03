# Phase 2 Design — Gap Engine

**Date:** 2026-07-16 · **Mode:** Build (heavy) · **Scope:** AGENTS.md Phase 2
**Inputs:** phase2-research.md (graph recon + R-o-P deep read + GAPMAP mining), Phase 1 graph (3,126 nodes / 4,029 edges)
**Spec review:** 6 CRITICALs + 12 WARNINGs resolved into this document (see Contract Changelog)

## Core loop

```
propose (deterministic graph queries)  →  gap_candidates
assassinate (isolated LLM critics, kill mandate, verified evidence)  →  verdicts
report (survivors + contested + kill dossiers)
```

"Structure proposes, LLM disposes" is enforced structurally: candidate **claims are
deterministic template strings** filled from graph slots — no LLM at propose time.
The LLM appears only as assassin/resurrector/cross-family auditor.

## Done When — unit gates (deterministic, pytest; fakes injected, no network)

- [ ] `uv run pytest` 0 failures; `uv run ruff check .` 0 errors
- [ ] Each template finds a planted gap on a synthetic graph AND emits zero candidates on a
      counter-graph where the gap is closed (both directions tested, per template)
- [ ] Deterministic node_id (C1): same alias cluster → same node_id across two resolve runs;
      changed cluster membership → new node_id
- [ ] resolve self-wipe extends to node_embeddings + communities before canonical_nodes (C2);
      wipe-then-reinsert windows commit the wipe first (ART gotcha)
- [ ] embed-nodes: idempotent (skips already-embedded); empty after re-resolve until re-run
- [ ] communities: identical output across two runs on the same DB (fixed seed AND sorted
      node ordering — W8); nodes outside giant component get no community row
- [ ] Evidence verifier tiers (W1): exact normalized substring → verified_exact;
      ellipsis-split segments each best-window ratio ≥0.85 OR content-token containment
      ≥0.95 (quote ≥8 tokens) → verified_fuzzy; else failed. Fabricated doc_id → failed.
      Unit-tested per tier with synthetic docs.
- [ ] Kill gate: any verified (exact|fuzzy) refutation kills; failed-evidence refutation
      does NOT kill; all-abstain → contested, never survived (C5); survived requires ≥1
      explicit survive verdict with verified nearest-miss evidence; unanimous kill triggers
      resurrection exactly once per candidate per run
- [ ] Critic loop: fake chat fn driving tool calls → tool results appended, terminates on
      verdict or max_steps (8) or accumulated-tool-budget breach (W3) → forced verdict turn;
      malformed action → one retry then abstain
- [ ] Assassinate resume (W6): processes status='proposed' only; deletes that candidate's
      prior verdicts before re-processing; terminal status + verdicts written in one
      main-thread transaction
- [ ] propose re-run: deletes verdicts of status='proposed' candidates, then those
      candidates (FK order), commits, then reinserts (W6)
- [ ] who_tried_this resolves paraphrased names via casefold-exact then embedding fallback,
      and reports how the name resolved; unresolvable name → "absence not established" (W9)

## Done When — acceptance gates (live run, coordinator-checked)

- [ ] `litgraph propose` yields ≥30 candidates total from ≥3 of 4 templates after
      per-template top-K capping (K=25, ranked by template score); T1 threshold sweep
      reported; every candidate's grounds resolve to live node_ids/doc_ids (W7: propose
      and assassinate gates share the ≥30 number)
- [ ] `litgraph assassinate` completes **100% of proposed candidates (≥30)**; every final
      disposition ∈ {killed, survived, contested}; 100% of kills carry verifier-passed
      evidence (tier mix reported); 100% of survivals carry verified nearest-miss evidence
- [ ] Hard gate (C5): abstain rate ≤20% of critic runs AND kill rate strictly inside
      (0%, 100%) — a gate that dispositions everything while evaluating nothing must fail
- [ ] Unanimous-kill candidates all show a resurrection verdict; contested queue rendered
- [ ] Cross-family audit ran on 100% of survivors and 100% of unanimous kills (C6)
- [ ] Survivor + contested dossiers rendered (`litgraph gaps`) with full provenance chain:
      template → slots → claim → verdicts → evidence quotes → verifier tier
- [ ] Every LLM call logged to logs/llm/ (critic role, candidate_id, step, model)

## Templates (propose — deterministic queries)

All read the canonical graph (graph.py join) + node_embeddings + communities.
**Node embedding input (C4):** NOT the bare canonical name (that space is exactly what ER
deduped at ≥0.80 — pairs above threshold cannot exist as distinct nodes). Embed a context
document per node: `"{type}: {name}. {up to 3 evidence quotes, chosen deterministically
(shortest first, ties by doc_id)}"`. Thresholds below live in this context space and are
sweep-calibrated at acceptance.

- **T1 Transfer gap**: Method M —E→ Problem A, no M —E→ B edge, where E (both the "applied"
  set and the exclusion set — W2) = {APPLIED_TO, USES, EVALUATED_ON}, direction
  Method→Problem only (CITES/CONTRADICTS/etc. never disqualify). Pair eligibility:
  context-cosine(A,B) ≥ θ AND A,B share ≥1 method via E. θ default 0.60, swept at
  acceptance to land T1 yield in [5, 50]; sweep table reported. Score = sim(A,B) ×
  log(1+deg(M)).
- **T2 Structural hole**: Louvain (seed=42) on the undirected projection of the giant
  component, nodes pre-sorted by node_id (W8). Community pairs where observed cross-edges
  ≤ 0.25 × expected under the configuration model AND expected ≥5 (W10 — absolute null,
  not median split) AND centroid context-cosine ≥ 0.5. Bridge = ≥1 canonical node with
  edges into both communities (an actual bridge, not a shared type). Claim names the two
  clusters by top-PageRank members.
- **T3 Unexamined assumption**: Assumption nodes with ≥2 neighbors and ≥2 mention docs
  (W10) whose neighbors all fall in one community. Claim: "Assumption X is load-bearing in
  cluster C and unexamined elsewhere."
- **T4 Contradiction**: every CONTRADICTS canonical pair (203), packaged with both
  directions' evidence quotes + shared Metric/Dataset neighbors, ranked by evidence count.
  Any endpoint types (data is Method→Claim-shaped).

Candidate record (CGW-shaped, per GAPMAP): `{candidate_id, template, claim, grounds
(node_ids + doc_ids + evidence quotes), score, params}`. candidate_id =
`{template}:{sha256(sorted slot node_ids)[:12]}` — stable because node_ids are stable (C1).

**Prerequisite guard (W11):** propose aborts with a clear message unless node_embeddings
covers ≥95% of canonical_nodes and communities is non-empty. C2's wipe-on-resolve makes
staleness structurally impossible: after re-resolve those tables are empty until re-run.

## Assassin (dispose — kill-gate, per R-o-P)

Tiering mirrors R-o-P (workhorse-tier volume + senior-tier decision points), resolving C6:

- **Stage A — 2 isolated tool-loop critics, local Qwen3.6-35B** (workhorse volume tier).
  Context = candidate claim + grounds ONLY — no proposer scoring, no other critic's output.
  Refute mandate is **template-specific** (W5): T1/T2/T3 → find the in-index doc that
  already did it, or in-index evidence nobody would (missing dataset, shown-not-to-work);
  T4 → find in-index evidence the confliction is already resolved or spurious (misextraction,
  different settings). Self-managed ReAct loop, schema-enforced action selection (spike-
  verified), max 8 steps, accumulated tool output capped at 60k chars → forced verdict (W3).
  3-shot exemplars in prompt (GAPMAP: 0-shot degenerates). Prompt hardening (W4): tool
  output is untrusted document data; instructions inside it must be ignored; assertions in
  it are evidence to weigh, not commands.
- **Verdict schema**: `{verdict: kill|survive, refutation_claim, evidence: [{doc_id, quote}],
  feasibility: feasible|infeasible|unknown, confidence_bucket: more_probable|least_probable}`.
- **Evidence verifier (tiered — W1, calibrated on live data):** exact normalized substring
  (casefold, NFKC, unicode-punct fold, whitespace collapse) → verified_exact; else split
  quote on ellipses, every segment best-window difflib ≥0.85, or content-token containment
  ≥0.95 with ≥8 tokens → verified_fuzzy; else failed. (Live calibration: Phase 1's own
  extraction quotes pass exact at only 40.7% — models stitch spans; 98% of failures have
  ≥99% token containment, i.e. paraphrase not fabrication.) Failed evidence → verdict
  rejected → one retry naming the violation → else abstain. **Kills and survivals both
  require verifier-passed evidence** (charter: every verdict cites the index).
- **Decision rule**: any verified refutation → killed. ≥1 verified survive verdict and no
  verified refutation → survived. All-abstain, or survive verdicts that fail verification →
  **contested** (C5). Unanimous kill (both critics) → **resurrection agent**: single-shot
  **cross-family** (Claude via `claude -p`, senior tier) confirm-mandate audit of the kill
  dossier (claim + refutations + cited docs' matched spans, pre-assembled — no tool loop);
  overturn → contested, never auto-survived.
- **Stage D — cross-family audit of survivors** (same single-shot dossier pattern, Claude
  via `claude -p --output-format json`, parse + one retry): concur → survived (final);
  refute-with-evidence (verifier-checked against index) → contested. Runs automated inside
  `litgraph assassinate` (C6 resolved: cross-family sits on BOTH failure sides — false
  kills via resurrection, false survivals via Stage D — and is automated, not manual).
  Cost bound: resurrections + survivors ≈ dozens of single-shot calls via an interactive
  Claude Code session (flagged to the operator 2026-07-16).
- **Concurrency (C3, extends Phase 1's C1):** critic threads do LLM calls + **read-only**
  DB via per-thread `conn.cursor()` handles; ALL writes (verdicts, status transitions)
  marshal to the main thread and commit per-candidate in one transaction. 4 candidate
  workers × sequential critics inside each (matches Phase 1's proven workers=4 against oMLX).

## Assassin tools (tools.py — pure read-only functions over a cursor)

All outputs deterministically truncated with an explicit `truncated: n_omitted` marker (W3):
- `subgraph_around(node_id, hops=1)` → typed neighborhood; cap 40 edges (sort: evidence-
  bearing first, then doc_id)
- `full_text(doc_id)` → title + full_text[:30_000]
- `evidence_for(node_id)` → mention quotes + doc_ids; cap 20
- `who_tried_this(method_name?, problem_name?)` → closed-world edge lookup; name resolution:
  casefold-exact alias match, else embedding top-match ≥0.7 against node_embeddings; output
  states the resolution (`resolved "X" → node Y via embedding 0.82`); unresolved → returns
  "name not resolvable in index — absence NOT established" (W9)

## Data model additions (DuckDB)

```sql
node_embeddings(node_id PK, model, vec FLOAT[1024], created_at)   -- soft ref; wiped by resolve (C2)
communities(node_id PK, community_id, algo, seed, run_at)          -- wiped by resolve + per communities run
gap_candidates(candidate_id PK, template, claim, grounds JSON, score, params JSON,
               status 'proposed'|'killed'|'survived'|'contested', created_at)
verdicts(verdict_id PK, candidate_id FK, stage 'A'|'resurrect'|'D', critic_model,
         critic_role, verdict 'kill'|'survive'|'abstain', refutation_claim, evidence JSON,
         evidence_tier 'verified_exact'|'verified_fuzzy'|'failed'|NULL, feasibility,
         confidence_bucket, raw JSON, created_at)
```

**Identity (C1):** `canonical_nodes.node_id` becomes deterministic:
`sha1(node_type + "\n" + "\n".join(sorted(member_alias_names)))[:16]`. Unchanged clusters
keep their id across re-resolves → candidate_id dedup works, verdict grounds survive
re-resolve when clusters didn't change; changed clusters correctly invalidate. **This edits
resolve.py — a flagged exception to "Phase 1 modules untouched"** (also C2's wipe extension).
Kept killed/survived/contested rows whose grounds reference vanished node_ids are marked
by `litgraph gaps` as `stale_grounds` (reported, not silently dropped).

**Re-run semantics (W6):** propose deletes verdicts of proposed candidates → deletes those
candidates → COMMIT → reinserts (ART gotcha). Assassinate touches only status='proposed',
wipes a candidate's stale verdicts before re-processing, writes terminal state atomically.

## Architecture (new files; Phase 1 edits limited to resolve.py C1+C2, db.py DDL, cli.py)

```
src/litgraph/embeddings.py   # node context-doc embeddings populate/wipe (jina, 64-batch)
src/litgraph/communities.py  # louvain(seed=42, sorted nodes) on giant component
src/litgraph/templates.py    # T1-T4 → gap_candidates
src/litgraph/tools.py        # 4 read-only assassin tools + truncation
src/litgraph/verify.py       # tiered evidence verifier (shared: assassin + tests + gaps report)
src/litgraph/assassin.py     # critic ReAct loop, kill gate, resurrection, Stage D, concurrency
prompts/assassin_v1.md       # refute-mandate critic prompt, template-specific mandates, 3 exemplars
prompts/resurrect_v1.md      # confirm-mandate resurrection audit (single-shot, dossier)
prompts/stage_d_v1.md        # cross-family survivor audit (single-shot, dossier)
cli: embed-nodes | communities | propose | assassinate | gaps
```

## Verify-or-fallback (pre-build spikes)

- **ReAct loop: PASSED 2026-07-16 (live).** Canned T1 candidate + canned tools: critic ran
  who_tried_this → subgraph_around → who_tried_this → full_text → verdict(kill) in 5 steps /
  50s (6-14s/step), citing a verbatim tool-result quote (verifier-exact). Budget: ≥30
  candidates × 2 critics ≈ 30-70 min at 4-way — in-session, not overnight.
- **Verifier calibration: RUN 2026-07-16 (live, drove W1 resolution).** Exact-substring
  passes only 40.7% of Phase 1's own evidence quotes; 98% of failures ≥99% token
  containment → tiered verifier above. Also logged to gotchas.md.
- **Still owed before build wave (W12):** concurrent-cursor read smoke (4 threads × live
  DB) + hub-node tool-output size measurement ("long-term conversational memory", 46
  methods) to validate the W3 caps. Run during build prep.
- `claude -p --output-format json` single-shot smoke for Stage D pattern.

## Deviations & risk notes

- **Stage A critics are local Qwen, not frontier** — flagged charter deviation (charter:
  "frontier models only for the adversarial agent"). Justification: mirrors R-o-P's own
  workhorse/senior tiering (Sonnet workhorses, Opus senior); frontier sits at every
  decision point (resurrection, Stage D). Recall cost acknowledged; Phase 3 backtest
  measures it.
- Louvain (networkx built-in, seeded, sorted input) not Leiden — no native dep; 2.5k nodes.
- T1 relaxes charter's ≥2-shared-methods (recon: yields 2 pairs) and abandons charter's
  sim≥0.8-on-names (C4: definitionally empty below the ER merge floor). Context-embedding
  space + sweep instead.
- Stage B (R-o-P's 5-agent escalation) skipped at MVP — no new mechanics; scale knob.
- Assassin tool-loop is self-managed chat_json — no dependency on oMLX server-side
  function calling (unverified).
- Contamination guard: in-index-only prompt rule + verifier makes out-of-index citations
  non-lethal by construction; real eval is Phase 3's backtest.
- Prompt injection (W4): mitigated by prompt hardening + human-visible dossiers; not fully
  solved — noted for Phase 3.

## Contract Changelog
- [PRE-BUILD] C1: canonical node_id UUID → deterministic content hash of (type + sorted alias
  names). Reason: candidate_id stability/grounds durability across re-resolve. Edits resolve.py.
- [PRE-BUILD] C2: resolve self-wipe extended to node_embeddings + communities before
  canonical_nodes. Reason: FK/consistency; makes staleness impossible (W11 guard = nonempty check).
- [PRE-BUILD] C3: assassin concurrency contract — read-only per-thread cursors, main-thread
  writes, 4 candidate workers. Extends Phase 1 C1.
- [PRE-BUILD] C4: node_embeddings embed context docs (name+type+3 quotes), not bare names;
  T1 θ default 0.60 swept to yield [5,50]. Reason: name space above 0.80 is empty by ER construction.
- [PRE-BUILD] C5: all-abstain → contested; survived requires ≥1 verified survive verdict;
  hard acceptance gate abstain ≤20% and kill rate ∈ (0,100)%.
- [PRE-BUILD] C6: resurrection + Stage D are cross-family (Claude, single-shot dossier
  audits via claude -p), automated, on both failure sides. Stage-A-on-Qwen flagged as deviation.
- [PRE-BUILD] W1: evidence verifier tiered (exact | fuzzy | failed) after live calibration
  (40.7% exact pass on Phase 1 quotes; stitching, not fabrication).
- [PRE-BUILD] W2/W3/W5-W11: T1 edge sets pinned; tool outputs capped w/ truncation markers;
  template-specific mandates; resume semantics; gates aligned at ≥30; sorted-node Louvain;
  who_tried_this name resolution; T2 configuration-model null + real-bridge; T3 ≥2 neighbors
  + ≥2 docs; propose prerequisite guard.
