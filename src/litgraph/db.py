"""DuckDB connection and schema — the storage contract.

Raw extractions are immutable once written; canonical_nodes and aliases are
disposable (re-canonicalization = wipe + re-run resolve, never re-extract).
"""

from pathlib import Path

import duckdb

DEFAULT_DB = Path("litgraph.duckdb")

DDL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('paper', 'blog', 'repo')),
    external_id TEXT,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    authors TEXT,
    published_date DATE,
    fetched_at TIMESTAMP DEFAULT current_timestamp,
    full_text TEXT,
    meta JSON
);
CREATE TABLE IF NOT EXISTS extractions (
    extraction_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents (doc_id),
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    raw_output JSON,
    status TEXT NOT NULL CHECK (status IN ('ok', 'ok_empty', 'schema_error', 'llm_error')),
    created_at TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS mentions (
    mention_id TEXT PRIMARY KEY,
    extraction_id TEXT NOT NULL REFERENCES extractions (extraction_id),
    doc_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('entity', 'relation')),
    entity_type TEXT,
    name TEXT,
    src_name TEXT,
    src_type TEXT,
    dst_name TEXT,
    dst_type TEXT,
    edge_type TEXT,
    evidence TEXT
);
CREATE TABLE IF NOT EXISTS canonical_nodes (
    node_id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS aliases (
    alias_name TEXT NOT NULL,
    node_type TEXT NOT NULL,
    node_id TEXT NOT NULL REFERENCES canonical_nodes (node_id),
    method TEXT NOT NULL CHECK (method IN ('exact', 'embedding', 'llm')),
    score DOUBLE,
    PRIMARY KEY (alias_name, node_type)
);
CREATE TABLE IF NOT EXISTS node_embeddings (
    node_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    vec FLOAT[1024] NOT NULL,
    created_at TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS communities (
    node_id TEXT PRIMARY KEY,
    community_id INTEGER NOT NULL,
    algo TEXT NOT NULL,
    seed INTEGER NOT NULL,
    run_at TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS gap_candidates (
    candidate_id TEXT PRIMARY KEY,
    template TEXT NOT NULL CHECK (template IN ('T1', 'T2', 'T3', 'T4')),
    claim TEXT NOT NULL,
    grounds JSON NOT NULL,
    score DOUBLE,
    params JSON,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'killed', 'survived', 'contested')),
    created_at TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS backtest_results (
    candidate_id TEXT NOT NULL,
    arm TEXT NOT NULL CHECK (arm IN ('graph', 'baseline')),
    cutoff DATE NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('HIT', 'LEAK', 'OPEN')),
    claim TEXT NOT NULL,
    judge JSON,
    evidence JSON,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (candidate_id, arm, cutoff)
);
CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES gap_candidates (candidate_id),
    stage TEXT NOT NULL CHECK (stage IN ('A', 'resurrect', 'D')),
    critic_model TEXT NOT NULL,
    critic_role TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('kill', 'survive', 'abstain')),
    refutation_claim TEXT,
    evidence JSON,
    evidence_tier TEXT
        CHECK (evidence_tier IN ('verified_exact', 'verified_fuzzy', 'failed')),
    feasibility TEXT,
    confidence_bucket TEXT,
    raw JSON,
    created_at TIMESTAMP DEFAULT current_timestamp
);
"""

# node_embeddings deliberately has no FK to canonical_nodes (soft reference): resolve's
# self-wipe deletes embeddings + communities before canonical_nodes (spec C2), and the
# embedding dimension is jina-v5-small's 1024.


def connect(db_path: Path | str = DEFAULT_DB) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the LitGraph database and ensure schema."""
    conn = duckdb.connect(str(db_path))
    conn.execute(DDL)
    return conn
