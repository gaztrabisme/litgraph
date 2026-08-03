"""The four read-only assassin tools — a cursor in, a plain dict out.

Every tool takes a duckdb connection/cursor as its first argument and RETURNS a
dict: never raises on bad input (a missing node/doc or a name that will not
resolve becomes ``{"error": ...}`` or an "absence not established" marker, not an
exception), because critics call these from a self-managed ReAct loop and a raw
traceback would abort the run. Outputs are deterministically truncated with an
explicit ``truncated`` count so a critic can tell a cap from an exhausted set.

Tools are pure reads over the graph.py alias join and may run on per-thread
``conn.cursor()`` handles (assassin concurrency, C3) — they never write.
"""

import math

MAX_EDGES = 40
MAX_EVIDENCE = 20
MAX_FULL_TEXT = 30_000
EMBED_ACCEPT = 0.70

CLOSED_WORLD_NOTE = (
    "Closed-world assumption: an absent edge reflects only what is in the index, "
    "not the literature — treat a missing link as unknown, not disproven."
)

# Relation mentions resolved to canonical src/dst via the aliases join (the same
# join graph.py builds edges from), carrying typed endpoints and provenance.
# Evidence-bearing edges sort first, then by doc_id — both deterministic.
_EDGE_SQL = """
SELECT m.edge_type, m.doc_id, m.evidence,
       sa.node_id, sc.canonical_name, sc.node_type,
       da.node_id, dc.canonical_name, dc.node_type
FROM mentions m
JOIN aliases sa ON sa.alias_name = m.src_name AND sa.node_type = m.src_type
JOIN aliases da ON da.alias_name = m.dst_name AND da.node_type = m.dst_type
JOIN canonical_nodes sc ON sc.node_id = sa.node_id
JOIN canonical_nodes dc ON dc.node_id = da.node_id
WHERE m.kind = 'relation' AND ({cond})
ORDER BY (m.evidence IS NULL OR m.evidence = ''), m.doc_id
"""


def _cosine(a, b) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _edge_dict(row) -> dict:
    (edge_type, doc_id, evidence,
     src_id, src_name, src_type, dst_id, dst_name, dst_type) = row
    return {
        "edge_type": edge_type,
        "src": {"node_id": src_id, "name": src_name, "type": src_type},
        "dst": {"node_id": dst_id, "name": dst_name, "type": dst_type},
        "doc_id": doc_id,
        "evidence": evidence,
    }


def _fetch_edges(cur, cond: str, params: list) -> list[dict]:
    rows = cur.execute(_EDGE_SQL.format(cond=cond), params).fetchall()
    return [_edge_dict(r) for r in rows]


def _cap(items: list, limit: int) -> tuple[list, int]:
    """Return (items[:limit], n_omitted)."""
    return items[:limit], max(0, len(items) - limit)


def _node_exists(cur, node_id: str) -> bool:
    return cur.execute(
        "SELECT 1 FROM canonical_nodes WHERE node_id = ?", [node_id]
    ).fetchone() is not None


# ---------------------------------------------------------------------------
def subgraph_around(cur, node_id: str, hops: int = 1) -> dict:
    """Node's 1-hop typed neighborhood; cap 40 edges (evidence-bearing first)."""
    if not _node_exists(cur, node_id):
        return {"error": f"unknown node_id: {node_id!r}"}
    edges = _fetch_edges(cur, "sa.node_id = ? OR da.node_id = ?", [node_id, node_id])
    kept, omitted = _cap(edges, MAX_EDGES)
    out = {"edges": kept, "hops": 1}  # hops clamped to 1 for MVP
    if omitted:
        out["truncated"] = omitted
    return out


def full_text(cur, doc_id: str) -> dict:
    """Document title + full_text, body capped at 30k chars."""
    row = cur.execute(
        "SELECT title, full_text FROM documents WHERE doc_id = ?", [doc_id]
    ).fetchone()
    if row is None:
        return {"error": f"unknown doc_id: {doc_id!r}"}
    title, body = row
    body = body or ""
    out = {"doc_id": doc_id, "title": title, "full_text": body[:MAX_FULL_TEXT]}
    if len(body) > MAX_FULL_TEXT:
        out["truncated"] = len(body) - MAX_FULL_TEXT
    return out


def evidence_for(cur, node_id: str) -> dict:
    """Mention evidence quotes for the node; cap 20 items {doc_id, quote, edge_type}.

    Sources: relation mentions where either endpoint resolves to the node (edge_type
    set), plus entity mentions whose (name, type) alias-resolves to it and that carry
    evidence (edge_type None). Only non-null quotes are returned.
    """
    if not _node_exists(cur, node_id):
        return {"error": f"unknown node_id: {node_id!r}"}
    rows = cur.execute(
        """
        SELECT doc_id, quote, edge_type FROM (
            SELECT m.doc_id AS doc_id, m.evidence AS quote, m.edge_type AS edge_type
            FROM mentions m
            JOIN aliases sa ON sa.alias_name = m.src_name AND sa.node_type = m.src_type
            JOIN aliases da ON da.alias_name = m.dst_name AND da.node_type = m.dst_type
            WHERE m.kind = 'relation' AND m.evidence IS NOT NULL
              AND (sa.node_id = ? OR da.node_id = ?)
            UNION ALL
            SELECT m.doc_id, m.evidence, NULL
            FROM mentions m
            JOIN aliases a ON a.alias_name = m.name AND a.node_type = m.entity_type
            WHERE m.kind = 'entity' AND m.evidence IS NOT NULL AND a.node_id = ?
        )
        ORDER BY doc_id, edge_type NULLS LAST, quote
        """,
        [node_id, node_id, node_id],
    ).fetchall()
    items = [{"doc_id": d, "quote": q, "edge_type": e} for d, q, e in rows]
    kept, omitted = _cap(items, MAX_EVIDENCE)
    out = {"items": kept}
    if omitted:
        out["truncated"] = omitted
    return out


def _resolve_name(cur, name: str, node_type: str, embed_fn) -> dict:
    """Casefold-exact alias, else embedding top-match ≥0.7, both scoped to node_type."""
    row = cur.execute(
        "SELECT node_id FROM aliases "
        "WHERE node_type = ? AND lower(alias_name) = lower(?) "
        "ORDER BY node_id LIMIT 1",
        [node_type, name],
    ).fetchone()
    if row is not None:
        return {"input": name, "node_id": row[0], "via": "exact", "score": 1.0}

    if embed_fn is not None:
        rows = cur.execute(
            "SELECT ne.node_id, ne.vec FROM node_embeddings ne "
            "JOIN canonical_nodes c ON c.node_id = ne.node_id WHERE c.node_type = ?",
            [node_type],
        ).fetchall()
        if rows:
            qv = embed_fn(name)
            best_id, best = None, -1.0
            for nid, vec in rows:
                s = _cosine(qv, vec)
                if s > best:
                    best, best_id = s, nid
            if best >= EMBED_ACCEPT:
                return {"input": name, "node_id": best_id, "via": "embedding", "score": best}

    return {"input": name, "node_id": None, "via": None, "score": None}


def who_tried_this(cur, method_name=None, problem_name=None, embed_fn=None) -> dict:
    """Closed-world edge lookup between a Method and/or Problem named in prose.

    Each provided name is resolved (exact alias, then embedding) scoped to its type.
    Both resolved → edges between the two nodes (any type, either direction); one
    resolved → all edges touching it; an unresolvable name establishes no absence.
    """
    provided = {}
    if method_name is not None:
        provided["method"] = (method_name, "Method")
    if problem_name is not None:
        provided["problem"] = (problem_name, "Problem")
    if not provided:
        return {"error": "who_tried_this needs a method_name and/or problem_name"}

    resolution, resolved = {}, {}
    unresolved = False
    for key, (name, node_type) in provided.items():
        r = _resolve_name(cur, name, node_type, embed_fn)
        resolution[key] = r
        if r["node_id"] is None:
            unresolved = True
        else:
            resolved[key] = r["node_id"]

    out = {"resolution": resolution, "note": CLOSED_WORLD_NOTE, "edges": []}

    # A name we cannot even resolve means we cannot speak to absence at all.
    if unresolved:
        out["absence_not_established"] = True
        return out

    if len(resolved) == 2:
        a, b = resolved["method"], resolved["problem"]
        out["edges"] = _fetch_edges(
            cur,
            "(sa.node_id = ? AND da.node_id = ?) OR (sa.node_id = ? AND da.node_id = ?)",
            [a, b, b, a],
        )
    else:
        (node_id,) = resolved.values()
        edges = _fetch_edges(cur, "sa.node_id = ? OR da.node_id = ?", [node_id, node_id])
        kept, omitted = _cap(edges, MAX_EDGES)
        out["edges"] = kept
        if omitted:
            out["truncated"] = omitted
    return out
