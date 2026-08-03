"""Node context-document embeddings — the graph's semantic index (spec C4).

The embedding input is NOT the bare canonical name (that space is exactly what
entity resolution collapsed at cosine ≥ 0.80, so above-threshold pairs cannot
survive as distinct nodes). Each node is embedded as a context document:
``"{type}: {name}. {up to 3 evidence quotes}"``, the quotes chosen
deterministically (shortest first, ties by doc_id then quote text) from every
mention that alias-resolves to the node — entity-kind evidence unioned with the
evidence on any relation whose src OR dst endpoint resolves here.

populate is idempotent: it embeds only canonical rows missing from
node_embeddings, in one batched embed_fn call, and inserts under
models.EMBEDDING_MODEL. resolve's self-wipe empties this table, so a re-resolve
leaves nodes unembedded until embed-nodes reruns.
"""

from collections import defaultdict

from litgraph import llm, models

# Every (node_id, doc_id, evidence) reachable through aliases: entity-kind
# evidence, plus relation evidence attributed to each resolving endpoint. UNION
# (not UNION ALL) folds a quote asserted twice for the same node into one row.
_EVIDENCE_SQL = """
SELECT a.node_id, m.doc_id, m.evidence
FROM mentions m
JOIN aliases a ON a.alias_name = m.name AND a.node_type = m.entity_type
WHERE m.kind = 'entity' AND m.evidence IS NOT NULL
UNION
SELECT sa.node_id, m.doc_id, m.evidence
FROM mentions m
JOIN aliases sa ON sa.alias_name = m.src_name AND sa.node_type = m.src_type
WHERE m.kind = 'relation' AND m.evidence IS NOT NULL
UNION
SELECT da.node_id, m.doc_id, m.evidence
FROM mentions m
JOIN aliases da ON da.alias_name = m.dst_name AND da.node_type = m.dst_type
WHERE m.kind = 'relation' AND m.evidence IS NOT NULL
"""

_MISSING_SQL = """
SELECT node_id, node_type, canonical_name
FROM canonical_nodes
WHERE node_id NOT IN (SELECT node_id FROM node_embeddings)
ORDER BY node_id
"""


def context_doc(node_type: str, name: str, quotes: list[str]) -> str:
    """Compose a node's embedding input: type + name prefix, then the quotes."""
    return f"{node_type}: {name}. " + " ".join(quotes)


def _quotes_by_node(conn) -> dict[str, list[str]]:
    """node_id -> up to 3 evidence quotes, shortest first, ties by doc_id then text."""
    per_node: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for node_id, doc_id, evidence in conn.execute(_EVIDENCE_SQL).fetchall():
        per_node[node_id].append((doc_id, evidence))
    quotes: dict[str, list[str]] = {}
    for node_id, rows in per_node.items():
        rows.sort(key=lambda r: (len(r[1]), r[0], r[1]))
        quotes[node_id] = [evidence for _, evidence in rows[:3]]
    return quotes


def _default_embed(texts: list[str]) -> list[list[float]]:
    return llm.embed(texts, models.EMBEDDING_MODEL, command="embed-nodes")


def populate(conn, *, embed_fn=None) -> dict:
    """Embed every canonical node missing from node_embeddings; returns a report."""
    if embed_fn is None:
        embed_fn = _default_embed

    total = conn.execute("SELECT count(*) FROM canonical_nodes").fetchone()[0]
    missing = conn.execute(_MISSING_SQL).fetchall()
    if not missing:
        return {"embedded": 0, "skipped": total}

    quotes = _quotes_by_node(conn)
    docs = [
        context_doc(node_type, name, quotes.get(node_id, []))
        for node_id, node_type, name in missing
    ]
    vectors = embed_fn(docs)

    rows = [
        (node_id, models.EMBEDDING_MODEL, vec)
        for (node_id, _, _), vec in zip(missing, vectors)
    ]
    conn.executemany(
        "INSERT INTO node_embeddings (node_id, model, vec) VALUES (?, ?, ?)", rows
    )
    return {"embedded": len(missing), "skipped": total - len(missing)}


def wipe(conn) -> None:
    """Clear all node_embeddings rows."""
    conn.execute("DELETE FROM node_embeddings")
