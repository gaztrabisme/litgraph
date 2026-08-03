"""DuckDB -> NetworkX rebuild with an optional --as-of cutoff.

The graph is disposable: every call reconstructs it from canonical_nodes,
aliases, and relation mentions. Edges carry per-doc provenance (never deduped
canonically), so a MultiDiGraph preserves parallel assertions.
"""

import datetime

import networkx as nx

_NODES_SQL = "SELECT node_id, node_type, canonical_name FROM canonical_nodes"

# Relation mentions joined to a resolved src and dst via aliases, plus the
# source document (for published_date / as-of filtering). An INNER JOIN on both
# alias sides silently drops relations whose endpoints have no alias row.
_EDGES_SQL = """
SELECT sa.node_id AS src_id,
       da.node_id AS dst_id,
       m.edge_type,
       m.doc_id,
       d.published_date,
       m.evidence
FROM mentions m
JOIN aliases sa ON sa.alias_name = m.src_name AND sa.node_type = m.src_type
JOIN aliases da ON da.alias_name = m.dst_name AND da.node_type = m.dst_type
JOIN documents d ON d.doc_id = m.doc_id
WHERE m.kind = 'relation'
"""


def build(conn, *, as_of: datetime.date | None = None) -> nx.MultiDiGraph:
    """Rebuild the knowledge graph from the storage tables.

    Nodes are every canonical row (regardless of as_of). Edges come from
    relation mentions whose src and dst names resolve through aliases. With
    ``as_of`` set, only docs published on or before the cutoff contribute edges
    (NULL-dated docs excluded); without it, all docs including NULL-dated.
    """
    g = nx.MultiDiGraph()
    for node_id, node_type, canonical_name in conn.execute(_NODES_SQL).fetchall():
        g.add_node(node_id, node_type=node_type, name=canonical_name)

    sql, params = _EDGES_SQL, []
    if as_of is not None:
        sql += " AND d.published_date IS NOT NULL AND d.published_date <= ?"
        params = [as_of]

    for src_id, dst_id, edge_type, doc_id, published_date, evidence in conn.execute(
        sql, params
    ).fetchall():
        g.add_edge(
            src_id,
            dst_id,
            edge_type=edge_type,
            doc_id=doc_id,
            published_date=published_date,
            evidence=evidence,
        )
    return g
