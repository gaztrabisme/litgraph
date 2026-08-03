"""Executable spec for litgraph.communities — Louvain on the giant component.

Contract under test (fixed by coordinator):
    communities.assign(conn, *, seed=42) -> {"communities": n, "nodes": n}

assign builds the graph (graph.build), takes its undirected simple projection,
keeps ONLY the largest connected component, sorts nodes by node_id, runs
networkx Louvain (seed), wipes the communities table, and inserts one
(node_id, community_id, 'louvain', seed) row per assigned node. Spec:
phase2-design.md T2 / W8 + "Done When" communities gates.

Fixtures build the graph straight from the storage tables (canonical_nodes +
aliases + relation mentions). The module does not exist yet, so these fail at
import until it is built.
"""

from litgraph import communities, db
from litgraph.models import EXTRACTION_MODEL, PROMPT_VERSION


# ---------------------------------------------------------------------------
# Synthetic graph builders
# ---------------------------------------------------------------------------
def new_db():
    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO documents (doc_id, source_type, url, title) "
        "VALUES ('d1', 'paper', 'http://example/d1', 'd1')"
    )
    conn.execute(
        "INSERT INTO extractions (extraction_id, doc_id, prompt_version, model, status) "
        "VALUES ('e1', 'd1', ?, ?, 'ok')",
        [PROMPT_VERSION, EXTRACTION_MODEL],
    )
    return conn


def add_node(conn, node_id):
    """One canonical Method node whose alias name equals its node_id."""
    conn.execute(
        "INSERT INTO canonical_nodes (node_id, node_type, canonical_name) VALUES (?, 'Method', ?)",
        [node_id, node_id],
    )
    conn.execute(
        "INSERT INTO aliases (alias_name, node_type, node_id, method, score) "
        "VALUES (?, 'Method', ?, 'exact', 1.0)",
        [node_id, node_id],
    )


_eid = 0


def add_edge(conn, src_id, dst_id):
    global _eid
    _eid += 1
    conn.execute(
        "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, "
        "src_name, src_type, edge_type, dst_name, dst_type, evidence) "
        "VALUES (?, 'e1', 'd1', 'relation', ?, 'Method', 'USES', ?, 'Method', 'ev')",
        [f"r{_eid}", src_id, dst_id],
    )


def two_component_db():
    """Giant component {a,b,c,d,e,f} (two bridged triangles) + small {x,y}."""
    conn = new_db()
    for n in ["a", "b", "c", "d", "e", "f", "x", "y"]:
        add_node(conn, n)
    for s, t in [("a", "b"), ("b", "c"), ("a", "c"),        # triangle 1
                 ("d", "e"), ("e", "f"), ("d", "f"),        # triangle 2
                 ("c", "d"),                                # bridge
                 ("x", "y")]:                               # separate component
        add_edge(conn, s, t)
    return conn


def community_rows(conn):
    return conn.execute(
        "SELECT node_id, community_id, algo, seed FROM communities"
    ).fetchall()


def pairs(conn):
    return {(r[0], r[1]) for r in community_rows(conn)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_assign_deterministic_across_runs():
    """Same DB, two assign() calls -> identical (node_id, community_id) sets (W8)."""
    conn = two_component_db()

    first_report = communities.assign(conn)
    first = pairs(conn)
    second_report = communities.assign(conn)
    second = pairs(conn)

    assert first == second, "fixed seed + sorted nodes must give identical assignment"
    assert first_report == second_report


def test_assign_only_giant_component_gets_rows():
    """Nodes outside the largest connected component get no community row."""
    conn = two_component_db()

    report = communities.assign(conn)

    assigned = {r[0] for r in community_rows(conn)}
    assert assigned == {"a", "b", "c", "d", "e", "f"}
    assert {"x", "y"}.isdisjoint(assigned), "small component excluded"
    assert report["nodes"] == 6


def test_assign_records_algo_and_seed():
    """Every row carries algo='louvain' and the seed used."""
    conn = two_component_db()

    communities.assign(conn, seed=7)

    rows = community_rows(conn)
    assert rows, "the giant component must yield rows"
    assert {r[2] for r in rows} == {"louvain"}
    assert {r[3] for r in rows} == {7}


def test_assign_report_matches_rows():
    """Return {'communities', 'nodes'} matches the rows actually written."""
    conn = two_component_db()

    report = communities.assign(conn)

    rows = community_rows(conn)
    assert report["nodes"] == len(rows)
    assert report["communities"] == len({r[1] for r in rows})


def test_assign_wipes_prior_rows():
    """A stale communities row (node not in the current graph) is wiped, not kept."""
    conn = two_component_db()
    conn.execute(
        "INSERT INTO communities (node_id, community_id, algo, seed) "
        "VALUES ('ghost', 99, 'louvain', 42)"
    )

    communities.assign(conn)

    assigned = {r[0] for r in community_rows(conn)}
    assert "ghost" not in assigned, "prior rows must be wiped before reinsert"
