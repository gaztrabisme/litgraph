# LitGraph: what we built, what failed, and why we need you to review 48 research gaps

*2026-07-25. This is the long-form story behind the [review
page](https://gaztrabisme.github.io/litgraph/). The dated, unpolished version
lives in [`wiki/log.md`](../wiki/log.md) — every number here has a log entry
and an LLM call log behind it.*

## The idea

Research fields leave gaps: method M applied to problem A but never to the
obviously-adjacent problem B; two literatures that should talk to each other
but share almost no citations; assumptions everyone in one subfield makes and
nobody has examined. The LLM literature (RAG, agent memory, agent harnesses)
seemed like a good target because it moves fast, and much of its real
knowledge lives in preprints, engineering blogs, and GitHub READMEs rather
than peer-reviewed venues — nobody had built a gap explorer over that corpus.

The plan: build a **typed knowledge graph** over the literature (methods,
problems, datasets, claims, assumptions, contradictions — extracted from full
text, not just abstracts), find structural gaps with graph queries, and use an
LLM **adversarially** — its job is to *kill* candidate gaps by finding the
paper that already did it, not to generate ideas. Cheap 2026 inference makes
the extraction affordable: our full 1,285-document corpus cost about **$2** to
extract with deepseek-v4-flash. Everything judgment-shaped runs on a local
Qwen3.6-35B (MLX, on a Mac); only the final cross-family audit uses Claude.

The founding slogan was *"structure proposes, LLM disposes."* Hold that
thought, because the data killed half of it.

## The machine

Five stages, each with a paper trail:

1. **Ingest + extract.** arXiv API + hand-curated blog/repo seeds →
   full-text typed extraction (9 node types, 9 edge types, including
   `CONTRADICTS` — 857 indexed contradictions, which nobody in this
   literature indexes). Every extraction logs prompt version, model, raw
   output.
2. **Entity resolution.** This field renames everything (HyDE vs
   "hypothetical document embeddings"; six things called "agentic RAG").
   Embedding merge → LLM adjudication → alias table. Health metric:
   mentions-per-canonical-node = 1.55 (if it's ~1.0, resolution is broken and
   centrality is garbage).
3. **Graph + communities.** NetworkX + DuckDB. 24,782 nodes / 29,749 edges /
   68 Louvain communities.
4. **Proposers.** Structural holes (cluster pairs with far fewer cross-links
   than a degree-preserving null model expects) are computed and handed to an
   LLM session as *leads*. The session has four read-only graph tools —
   `who_tried_this`, `full_text`, `evidence_for`, `subgraph_around` — and
   proposes claims grounded in quotes that are **mechanically verified
   verbatim** against stored text (difflib-fuzzy at worst; non-verbatim
   proposals are dropped).
5. **The kill-gate.** Two isolated critics from different families (local
   Qwen + DeepSeek) with a refute mandate and the same four tools; every kill
   must cite in-index evidence, which is provenance-verified *and* passed
   through a relevance gate (an earlier version was exploited by a critic
   citing real quotes that didn't support its refutation). Unanimous kills get
   a resurrection review. Every survivor gets a cross-family Claude audit.
   Survivors become dossiers: claim, verbatim grounds, every verdict.

## The eval, and the uncomfortable middle chapters

The charter made a temporal backtest non-negotiable before scaling: rebuild
the world as of **2024-07-01**, propose gaps inside it, then score against
what the field actually published since. A **HIT** means an independent
closure judge (Claude, evidence-checked) affirms that a specific post-cutoff
paper closes the claim. A **LEAK** means the claim "predicts" something that
already existed pre-cutoff — the contamination alarm.

What happened, in order:

- **Phase 3 (first backtest):** machinery worked end-to-end, but 1 decided
  case out of 53 scored — the answer key was too sparse to rank anything.
  Fix: an embedding recall pre-pass feeding critics the nearest post-cutoff
  papers per claim. That turned out to be the binding constraint on the whole
  eval.
- **Phase 4b (backtest at scale):** the graph-template arm — the original
  charter architecture — scored **0 HITs from 8 scored survivors**. A
  baseline arm (Claude, no graph, writing gap claims directly from the same
  frozen corpus) scored **10 HITs from 28**. We tested whether claim *shape*
  was the problem (paper-shaped rewrites of the structural holes): 0 HITs and
  the project's first LEAK. Cumulative template scoreboard: **13 scored, 1
  HIT, 1 LEAK**. Structure-as-proposer lost, twice.
- **Contamination check:** was Claude's 10/28 just training-data recall of
  2025-26 papers? Two probes: a Qwen-local baseline arm with a comparable
  harness scored 1/23 (so *harness* alone doesn't explain it), and a
  date-recallability analysis showed **6 of Claude's 11 no-tools HITs close
  with papers published after any plausible training cutoff** — impossible to
  recall directly. Verdict: contamination explains at most a minority of the
  gap. Frontier capability is real. (Zero LEAKs in all decided cases — the
  index-evidence rule held.)
- **The decisive experiment:** give both model families the graph as *tools*
  (leads + the four query tools), same kill-gate, same scoring:

  | arm | proposals | HITs | HIT/proposal |
  |---|---|---|---|
  | graph templates alone | 42 scored | 1 | ~2% |
  | Qwen, no tools | 28 | 1 | 4% |
  | Claude, no tools | 42 | 10 | 24% |
  | **Qwen + graph tools** | 44 | **10** | **23%** |
  | **Claude + graph tools** | 30 | **13** | **43%** |

  The graph lifted the **local 35B tenfold — to parity with tool-less
  Claude** — and nearly doubled Claude's per-proposal hit rate. One honest
  negative: tools did *not* reduce the rate at which proposers propose
  already-done work (the kill-gate still catches those; tool discipline is
  advisory).

So the architecture inverted, on evidence: **LLM proposes, structure informs
and disposes.** The graph's value is not generating ideas — it's (a) pointing
the proposer at under-explored regions it wouldn't look at, and (b) giving
critics a closed, queryable world in which "nobody has done X" is checkable.

## The frontier run (the thing we're asking you to review)

With the loop validated, we widened the corpus beyond RAG/memory/harnesses —
five new families (model forensics, diffusion LLMs, RL-for-LLM,
foundations/efficiency, image-gen/omni-modal), each seeded at ~25 docs
because sparse topic islands generate spurious "gaps" (every arXiv ID
fetch-verified before ingestion; no hallucinated citations). Corpus: 1,154 →
1,285 docs.

Then we ran the loop on the *current* graph. The structural holes immediately
pointed across the old/new family boundary (DeepSeek-V4 × Mem0, BAGEL ×
conversational memory, Reflexion × RL-for-reasoning). Two proposer arms — 25
hole-seeded local-Qwen sessions, plus one logged Claude agent session (60
tool calls, every one in the call log) — produced 62 verbatim-grounded
candidates. The kill-gate dispositioned all 62:

> **48 survived · 7 killed · 7 contested**

Each survivor's dossier carries the claim, the verbatim quotes it stands on,
both Stage-A critics' failed refutation attempts (with their verified
evidence), and the cross-family audit verdict.

## What we're asking

The kill-gate can verify grounding and hunt for closures *inside a 1,285-doc
index*. It cannot know the paper we never ingested, and it cannot judge
whether a gap is *interesting*. We (one operator + one AI) don't have the
domain depth across all 8 families to judge that either. You might.

- **Browse the dossiers:** [review page](https://gaztrabisme.github.io/litgraph/)
- **Know a paper that closes one?** [Dispute it](https://github.com/gaztrabisme/litgraph/issues/new?template=dispute-gap.md)
  — "already done" with a link is the most valuable possible feedback, and
  every confirmed dispute is ground truth we'll use to recalibrate the gate.
- **Think one is genuinely open and worth doing?** That's worth hearing too —
  and if it nerd-snipes you into doing it, even better.
- The **7 contested dossiers** (critics deadlocked) are on the page too;
  they're explicitly waiting for human judgment.

## Reproducing / limitations

The repo is MIT; `README.md` has the run guide. Honest limitations:

- The eval is **directional, not definitive**: n is small (36 HITs total
  across all arms), the answer key is bounded by scoring-critic recall, and
  direction-level contamination (recalling *trends* rather than papers) can't
  be fully excluded.
- "Nobody has done X" always means "nobody **in our 1,285-doc index** has
  done X." That's exactly the weakness the dispute mechanism exists to probe.
- The stack is opinionated: Apple-Silicon local serving, DeepSeek extraction,
  `claude` CLI for the cross-family audit. Swappable, but you'll touch
  `models.py`.
- One eval artifact we built and **rejected** ships in the repo for the
  record (`data/longitudinal_results.json` — longitudinal hole scoring; too
  noisy to trust, see `wiki/decisions.md`).
