"""Entity resolution — mentions → canonical_nodes + aliases.

Within each entity type: exact-merge (casefold+strip) → embedding-merge
(cosine ≥ 0.80) → LLM adjudication of the 0.70–0.80 band → union-find closure.
The name universe (C2) is every (name, type) graph.py can reach: entity mentions
plus both endpoints of every relation mention. Resolve self-wipes first (W10) so
a rerun is idempotent. canonical_name is the cluster's most frequent raw mention
name; each distinct name becomes one alias row tagged with the method that
attached it (the representative stays 'exact').
"""

import hashlib
import math
from collections import defaultdict, deque

import numpy as np

from litgraph import llm, models

EMBED_MERGE = 0.80
BAND_FLOOR = 0.70

_ADJUDICATE_SCHEMA = {
    "type": "object",
    "properties": {"same": {"type": "boolean"}},
    "required": ["same"],
    "additionalProperties": False,
}


def _normalize(name: str) -> str:
    return name.strip().casefold()


def _node_id(node_type: str, member_names: list[str]) -> str:
    """Deterministic node id (Phase 2 C1): content hash of the alias cluster.

    An unchanged cluster keeps its id across re-resolves, so gap-candidate ids and
    verdict grounds survive re-canonicalization; any membership change mints a new id.
    """
    payload = node_type + "\n" + "\n".join(sorted(member_names))
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:16]


def _cosine(a, b) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _default_embed(texts):
    return llm.embed(texts, models.EMBEDDING_MODEL, command="resolve")


def _default_adjudicate(name_a: str, name_b: str, node_type: str) -> bool:
    result = llm.chat_json(
        command="resolve",
        model=models.ADJUDICATION_MODEL,
        system="You judge whether two names denote the same concept in a knowledge graph.",
        user=(f"Node type: {node_type}\nName A: {name_a}\nName B: {name_b}\n"
              'Do A and B denote the same concept? Reply JSON {"same": true|false}.'),
        schema_name="adjudicate",
        schema=_ADJUDICATE_SCHEMA,
        prompt_version="adjudicate_v1",
    )
    return bool(result.get("same"))


def _name_universe(conn):
    """Distinct (name, type) over entity mentions ∪ both relation endpoints (C2)."""
    return conn.execute(
        "SELECT DISTINCT name, entity_type FROM mentions "
        "WHERE kind = 'entity' AND name IS NOT NULL "
        "UNION "
        "SELECT DISTINCT src_name, src_type FROM mentions "
        "WHERE kind = 'relation' AND src_name IS NOT NULL "
        "UNION "
        "SELECT DISTINCT dst_name, dst_type FROM mentions "
        "WHERE kind = 'relation' AND dst_name IS NOT NULL"
    ).fetchall()


def _mention_counts(conn) -> dict:
    """(name, type) -> occurrence count across entity names and relation endpoints."""
    rows = conn.execute(
        "SELECT nm, t, count(*) FROM ("
        "  SELECT name AS nm, entity_type AS t FROM mentions WHERE kind = 'entity' "
        "  UNION ALL "
        "  SELECT src_name, src_type FROM mentions WHERE kind = 'relation' "
        "  UNION ALL "
        "  SELECT dst_name, dst_type FROM mentions WHERE kind = 'relation'"
        ") GROUP BY nm, t"
    ).fetchall()
    return {(nm, t): c for nm, t, c in rows}


def _cluster(names, node_type, counts, embed_fn, adjudicate_fn, band_floor=BAND_FLOOR):
    """Cluster one type's names; yield (representative, [(alias, method, score), ...])."""
    parent = {n: n for n in names}
    edges = []  # spanning-tree edges: (a, b, method, score)

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def merge(a, b, method, score):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
            edges.append((a, b, method, score))

    # (1) exact merge on casefold+strip
    groups = defaultdict(list)
    for n in names:
        groups[_normalize(n)].append(n)
    for group in groups.values():
        for other in group[1:]:
            merge(group[0], other, "exact", None)

    # (2)/(3) embedding merge, then LLM adjudication of the borderline band.
    # Vectorized (numpy) — the pure-Python pair loop stopped scaling past ~10k names.
    # triu_indices preserves the original (i, j) i<j enumeration order, so merge
    # order and results are identical to the loop it replaced.
    if len(names) > 1:
        V = np.asarray(embed_fn([f"{node_type}: {n}" for n in names]), dtype=np.float64)
        norms = np.linalg.norm(V, axis=1)
        norms[norms == 0] = 1.0
        V /= norms[:, None]
        ii, jj = np.triu_indices(len(names), k=1)
        sims = (V @ V.T)[ii, jj]
        for idx in np.nonzero(sims >= EMBED_MERGE)[0]:
            merge(names[ii[idx]], names[jj[idx]], "embedding", float(sims[idx]))
        band = [(names[ii[idx]], names[jj[idx]], float(sims[idx]))
                for idx in np.nonzero((sims >= band_floor) & (sims < EMBED_MERGE))[0]]
        for a, b, cos in band:
            if find(a) != find(b) and adjudicate_fn(a, b, node_type):
                merge(a, b, "llm", cos)

    # (4) transitive closure: root each cluster at its most-frequent name
    adj = defaultdict(list)
    for a, b, method, score in edges:
        adj[a].append((b, method, score))
        adj[b].append((a, method, score))
    members = defaultdict(list)
    for n in names:
        members[find(n)].append(n)

    for member_names in members.values():
        rep = max(member_names, key=lambda n: counts.get((n, node_type), 0))
        attached = {rep: ("exact", None)}
        queue = deque([rep])
        while queue:
            cur = queue.popleft()
            for nb, method, score in adj[cur]:
                if nb not in attached:
                    attached[nb] = (method, score)
                    queue.append(nb)
        yield rep, [(n, *attached[n]) for n in member_names]


def mentions_per_canonical(conn) -> float:
    """W13: entity-kind mention rows ÷ canonical_nodes (shared with stats)."""
    mentions = conn.execute("SELECT count(*) FROM mentions WHERE kind = 'entity'").fetchone()[0]
    canonical = conn.execute("SELECT count(*) FROM canonical_nodes").fetchone()[0]
    return mentions / canonical if canonical else 0.0


def run(conn, *, embed_fn=None, adjudicate_fn=None, band_floor=BAND_FLOOR) -> dict:
    """Rebuild canonical_nodes + aliases from mentions; returns a report.

    band_floor raises the adjudication band's lower edge (spec W7): when the full
    0.70-0.80 band is too large to adjudicate, callers pass the cosine of the
    N-th-highest band pair so only the top N reach the LLM.
    """
    if embed_fn is None:
        embed_fn = _default_embed
    if adjudicate_fn is None:
        adjudicate_fn = _default_adjudicate

    # All flaky work (embeddings, LLM adjudication) happens BEFORE the self-wipe,
    # so a network failure can never leave the canonical tables emptied. A single
    # DELETE+reINSERT transaction is not an option: DuckDB's ART index rejects
    # reinserting a PK deleted in the same transaction.
    counts = _mention_counts(conn)
    by_type = defaultdict(list)
    for name, node_type in _name_universe(conn):
        by_type[node_type].append(name)

    node_rows, alias_rows = [], []
    for node_type, names in by_type.items():
        for rep, aliases in _cluster(names, node_type, counts, embed_fn, adjudicate_fn,
                                     band_floor=band_floor):
            node_id = _node_id(node_type, [a for a, _, _ in aliases])
            node_rows.append((node_id, node_type, rep))
            for alias_name, method, score in aliases:
                alias_rows.append((alias_name, node_type, node_id, method, score))

    # W10 self-wipe, extended by Phase 2 C2: derived tables (soft refs) first, then
    # aliases before canonical_nodes (FK order). Leaves node_embeddings/communities
    # empty until their commands re-run — propose's prerequisite guard relies on this.
    conn.execute("DELETE FROM node_embeddings")
    conn.execute("DELETE FROM communities")
    conn.execute("DELETE FROM aliases")
    conn.execute("DELETE FROM canonical_nodes")
    if node_rows:
        conn.executemany(
            "INSERT INTO canonical_nodes (node_id, node_type, canonical_name) VALUES (?, ?, ?)",
            node_rows,
        )
    if alias_rows:
        conn.executemany(
            "INSERT INTO aliases (alias_name, node_type, node_id, method, score) "
            "VALUES (?, ?, ?, ?, ?)",
            alias_rows,
        )
    return {
        "canonical_nodes": len(node_rows),
        "aliases": len(alias_rows),
        "mentions_per_canonical": mentions_per_canonical(conn),
    }
