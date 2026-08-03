# Wiki Index

This is the project's working lab notebook, published as-is (lightly scrubbed).
Note for readers: `Evidence: logs/llm/...` and `scratchpad/...` pointers
reference **untracked local artifacts** — LLM call logs and one-off scripts are
gitignored by design (the logs contain excerpts of paper full texts). The wiki
entries summarize what those artifacts showed.

## Architecture
- [../AGENTS.md](../AGENTS.md) — Project charter: positioning, settled architecture decisions, ontology v0, gap templates, phased plan (owner-authored, read-only)

## Decisions
- [decisions.md](decisions.md) — Session-level decisions, grounding records, rejected approaches

## Active Work
- [active-work.md](active-work.md) — Current workstreams, blockers, next steps

## Chronological log
- [log.md](log.md) — Dated session log: every run, result, and pivot

## Phase designs
- [phase1-design.md](phase1-design.md) — Ingestion MVP spec (incl. cold-review resolutions)
- [phase2-design.md](phase2-design.md) — Gap engine / adversarial kill-gate spec
- [phase3-design.md](phase3-design.md) — Temporal backtest (eval) spec
- [phase4-design.md](phase4-design.md) — Corpus scale + uniform re-extraction spec

## Research notes
- [phase0-research.md](phase0-research.md) — SciMuse internals, HySemRAG/GAPMAP patterns, base-repo survey
- [phase2-research.md](phase2-research.md) — Kill-gate prior art deep-reads (Refute-or-Promote, GAPMAP)
- [deepseek-api.md](deepseek-api.md) — Live-verified DeepSeek API behavior notes
- [gotchas.md](gotchas.md) — Operational failure patterns (oMLX, DuckDB, arXiv API)
