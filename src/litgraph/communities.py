"""Louvain community detection over the graph's giant component (spec T2 / W8).

assign rebuilds the graph (graph.build), collapses its MultiDiGraph into an
undirected simple projection, keeps only the largest connected component, and
runs networkx Louvain on it. The result is reproducible run-to-run: Louvain is
seed-deterministic only when node iteration order is stable, so the projection
and the component subgraph are both built with nodes and edges inserted in
sorted order. community_id is the index of a node's community in the Louvain
result list.

wipe + reinsert are separate autocommit statements, never one transaction:
DuckDB's ART index rejects reinserting a PK deleted in the same transaction
(wiki/gotchas.md).
"""

import networkx as nx

from litgraph import graph


def _simple_projection(g: nx.MultiDiGraph) -> nx.Graph:
    """Undirected, simple (multi-edges collapsed) projection with sorted insertion."""
    proj = nx.Graph()
    for node_id in sorted(g.nodes()):
        proj.add_node(node_id)
    edges = {tuple(sorted((u, v))) for u, v in g.edges() if u != v}
    for u, v in sorted(edges):
        proj.add_edge(u, v)
    return proj


def _giant_subgraph(proj: nx.Graph) -> nx.Graph:
    """The largest connected component as a fresh graph with sorted insertion order."""
    components = sorted(
        nx.connected_components(proj), key=lambda c: (len(c), sorted(c)), reverse=True
    )
    giant = components[0]
    sub = nx.Graph()
    for node_id in sorted(giant):
        sub.add_node(node_id)
    for u, v in sorted(proj.subgraph(giant).edges()):
        sub.add_edge(u, v)
    return sub


def assign(conn, *, seed: int = 42) -> dict:
    """Detect communities on the giant component; write one row per node. Report counts."""
    proj = _simple_projection(graph.build(conn))

    conn.execute("DELETE FROM communities")
    if proj.number_of_nodes() == 0:
        return {"communities": 0, "nodes": 0}

    sub = _giant_subgraph(proj)
    comms = nx.community.louvain_communities(sub, seed=seed)

    rows = [
        (node_id, community_id, "louvain", seed)
        for community_id, members in enumerate(comms)
        for node_id in members
    ]
    conn.executemany(
        "INSERT INTO communities (node_id, community_id, algo, seed) VALUES (?, ?, ?, ?)",
        rows,
    )
    return {"communities": len(comms), "nodes": len(rows)}
