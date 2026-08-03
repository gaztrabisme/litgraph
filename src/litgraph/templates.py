"""Deterministic gap proposer — T1-T4 templates over the canonical graph.

"Structure proposes, LLM disposes": every candidate is a template string filled
from graph slots, so propose never calls a model. Reads the graph.py alias join
(relation mentions resolved through aliases on both endpoints), node_embeddings
(context-doc vectors, C4), and communities (Louvain output). Emits gap_candidates
rows with status='proposed' and a locked candidate_id derived from the slot
node_ids, so ids stay stable while node_ids do (C1).

Templates (spec wiki/phase2-design.md §Templates):
    T1 transfer gap      — Method applied to A but not the near-neighbour B
    T2 structural hole   — two clusters bridged but under-linked (config-model null)
    T3 unexamined assumption — load-bearing Assumption localised to one community
    T4 contradiction     — every CONTRADICTS canonical pair, both directions' evidence

Re-run (W6): propose deletes the verdicts of status='proposed' candidates, deletes
those candidates, commits, then reinserts — never wiping and re-inserting the same
PK inside one transaction (DuckDB ART gotcha). Non-proposed candidates (killed /
survived / contested) and their verdicts are left untouched.
"""

import hashlib
import json
import math
from collections import defaultdict

import networkx as nx

from litgraph import graph

E_EDGES = {"APPLIED_TO", "USES", "EVALUATED_ON"}  # T1 "applied" and exclusion set (W2)
COVERAGE_MIN = 0.95


# --------------------------------------------------------------------------- #
# small vector + id helpers
# --------------------------------------------------------------------------- #
def _cos(a, b) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _pagerank(proj, damping: float = 0.85, max_iter: int = 100, tol: float = 1e-9) -> dict:
    """Power-iteration PageRank on an undirected projection (no numpy dependency).

    networkx's pagerank dispatches to scipy/numpy, which aren't installed; this pure
    Python variant mirrors its semantics — mass flows both ways along each undirected
    edge, dangling (isolated) nodes redistribute uniformly.
    """
    nodes = list(proj.nodes)
    n = len(nodes)
    if n == 0:
        return {}
    deg = {v: proj.degree(v) for v in nodes}
    pr = dict.fromkeys(nodes, 1.0 / n)
    for _ in range(max_iter):
        prev = pr
        dangling = damping * sum(prev[v] for v in nodes if deg[v] == 0) / n
        pr = {}
        for v in nodes:
            rank = (1.0 - damping) / n + dangling
            for nb in proj.neighbors(v):
                if deg[nb]:
                    rank += damping * prev[nb] / deg[nb]
            pr[v] = rank
        if sum(abs(pr[v] - prev[v]) for v in nodes) < tol:
            break
    return pr


def _mean(vecs) -> list[float]:
    vecs = list(vecs)
    n = len(vecs)
    dim = len(vecs[0])
    out = [0.0] * dim
    for v in vecs:
        for i, x in enumerate(v):
            out[i] += x
    return [x / n for x in out]


def _cid(template: str, slot_node_ids) -> str:
    """Locked candidate id: {template}:{sha256(sorted slot node_ids)[:12]}."""
    digest = hashlib.sha256("\n".join(sorted(slot_node_ids)).encode()).hexdigest()[:12]
    return f"{template}:{digest}"


def _quote(doc_id, ev):
    return {"doc_id": doc_id, "quote": ev}


# --------------------------------------------------------------------------- #
# graph + storage views (built once per propose)
# --------------------------------------------------------------------------- #
class _View:
    """Everything the templates read, assembled once from a single graph build."""

    def __init__(self, conn):
        self.g = graph.build(conn)
        self.ntype = {n: d["node_type"] for n, d in self.g.nodes(data=True)}
        self.name = {n: d["name"] for n, d in self.g.nodes(data=True)}
        self.vecs = {
            nid: vec for nid, vec in conn.execute(
                "SELECT node_id, vec FROM node_embeddings"
            ).fetchall()
        }
        self.comm = {
            nid: cid for nid, cid in conn.execute(
                "SELECT node_id, community_id FROM communities"
            ).fetchall()
        }

        # undirected simple projection: degrees, cross-community counts, pagerank.
        proj = nx.Graph()
        proj.add_nodes_from(self.g.nodes)
        self.edge_docs = defaultdict(list)  # frozenset({u,v}) -> [(doc, ev, edge_type)]
        for u, v, d in self.g.edges(data=True):
            if u == v:
                continue
            proj.add_edge(u, v)
            self.edge_docs[frozenset((u, v))].append(
                (d["doc_id"], d.get("evidence"), d["edge_type"])
            )
        self.proj = proj
        self.node_docs = self._node_docs(conn)

    @staticmethod
    def _node_docs(conn):
        """node_id -> set of docs that mention it (entity names ∪ relation endpoints)."""
        rows = conn.execute(
            "SELECT a.node_id, m.doc_id FROM mentions m JOIN aliases a "
            "  ON a.alias_name = m.name AND a.node_type = m.entity_type "
            "WHERE m.kind = 'entity' AND m.name IS NOT NULL "
            "UNION "
            "SELECT sa.node_id, m.doc_id FROM mentions m JOIN aliases sa "
            "  ON sa.alias_name = m.src_name AND sa.node_type = m.src_type "
            "WHERE m.kind = 'relation' AND m.src_name IS NOT NULL "
            "UNION "
            "SELECT da.node_id, m.doc_id FROM mentions m JOIN aliases da "
            "  ON da.alias_name = m.dst_name AND da.node_type = m.dst_type "
            "WHERE m.kind = 'relation' AND m.dst_name IS NOT NULL"
        ).fetchall()
        docs = defaultdict(set)
        for node_id, doc_id in rows:
            docs[node_id].add(doc_id)
        return docs


# --------------------------------------------------------------------------- #
# T1 — transfer gap
# --------------------------------------------------------------------------- #
def _t1_pairs(view, theta):
    """Yield (method, problem_a, problem_b, sim, shared, establishing_edges).

    method reaches A via E, does NOT reach B via E; cos(A,B) >= theta; A and B
    share >= 1 method via E. Establishing edges are the method->A E-edges.
    """
    reached = defaultdict(set)                 # method -> problems via E
    problem_methods = defaultdict(set)         # problem -> methods reaching it via E
    est = defaultdict(list)                     # (method, problem) -> [(doc, ev)]
    for u, v, d in view.g.edges(data=True):
        if (d["edge_type"] in E_EDGES
                and view.ntype.get(u) == "Method" and view.ntype.get(v) == "Problem"):
            reached[u].add(v)
            problem_methods[v].add(u)
            est[(u, v)].append((d["doc_id"], d.get("evidence")))

    problems = [n for n, t in view.ntype.items() if t == "Problem"]
    for method, targets in reached.items():
        for a in targets:
            if a not in view.vecs:
                continue
            for b in problems:
                if b in targets or b == a or b not in view.vecs:
                    continue
                sim = _cos(view.vecs[a], view.vecs[b])
                if sim < theta:
                    continue
                shared = (problem_methods[a] & problem_methods[b])
                if not shared:
                    continue
                yield method, a, b, sim, shared, est[(method, a)]


def _t1(view, theta):
    out = {}
    for method, a, b, sim, shared, establishing in _t1_pairs(view, theta):
        deg = view.proj.degree(method)
        score = sim * math.log1p(deg)
        grounds = {
            "node_ids": [method, a, b],
            "doc_ids": sorted({doc for doc, _ in establishing}),
            "evidence": [_quote(doc, ev) for doc, ev in establishing],
            "sim": sim,
            "shared_methods": sorted(shared),
        }
        claim = (f"Method '{view.name[method]}' is applied to '{view.name[a]}' but not to "
                 f"the near-neighbour problem '{view.name[b]}'; the transfer is unexamined.")
        cid = _cid("T1", [method, a, b])
        out[cid] = _record(cid, "T1", claim, grounds, score, {"theta": theta})
    return out


# --------------------------------------------------------------------------- #
# T2 — structural hole (configuration-model null + real bridge, W10)
# --------------------------------------------------------------------------- #
def _t2_holes(view):
    """Yield (ca, cb, members, expected, observed, bridges, pr) per structural hole."""
    proj, comm = view.proj, view.comm
    m = proj.number_of_edges()
    if m == 0 or not comm:
        return

    members = defaultdict(list)
    for node_id, cid in comm.items():
        members[cid].append(node_id)
    sum_deg = {c: sum(proj.degree(n) for n in ns) for c, ns in members.items()}

    cross = defaultdict(int)
    for u, v in proj.edges():
        cu, cv = comm.get(u), comm.get(v)
        if cu is None or cv is None or cu == cv:
            continue
        cross[tuple(sorted((cu, cv)))] += 1

    # each node's set of neighbouring communities → bridge test
    ncomm = {n: {comm.get(w) for w in proj.neighbors(n)} for n in proj.nodes}
    pr = _pagerank(proj)

    comms = sorted(members)
    for i, ca in enumerate(comms):
        for cb in comms[i + 1:]:
            expected = sum_deg[ca] * sum_deg[cb] / (2 * m)
            if expected < 5:
                continue
            observed = cross.get((ca, cb), 0)
            if observed > 0.25 * expected:
                continue
            va = [view.vecs[n] for n in members[ca] if n in view.vecs]
            vb = [view.vecs[n] for n in members[cb] if n in view.vecs]
            if not va or not vb or _cos(_mean(va), _mean(vb)) < 0.5:
                continue
            bridges = [n for n in proj.nodes if ca in ncomm[n] and cb in ncomm[n]]
            if not bridges:
                continue
            yield ca, cb, members, expected, observed, bridges, pr


def _t2(view):
    out = {}
    for ca, cb, members, expected, observed, bridges, pr in _t2_holes(view):
            top_a = max(members[ca], key=lambda n: pr.get(n, 0.0))
            top_b = max(members[cb], key=lambda n: pr.get(n, 0.0))
            cross_ev = []
            for u, v in view.proj.edges():
                if {view.comm.get(u), view.comm.get(v)} == {ca, cb}:
                    cross_ev.extend(
                        _quote(doc, ev) for doc, ev, _ in view.edge_docs[frozenset((u, v))]
                    )
            node_ids = sorted(set(bridges) | {top_a, top_b})
            grounds = {
                "node_ids": node_ids,
                "doc_ids": sorted({q["doc_id"] for q in cross_ev}),
                "evidence": cross_ev,
                "bridge_node_ids": sorted(bridges),
                "cluster_a": view.name.get(top_a),
                "cluster_b": view.name.get(top_b),
                "expected": expected,
                "observed": observed,
            }
            claim = (f"Clusters led by '{view.name.get(top_a)}' and '{view.name.get(top_b)}' "
                     f"are under-linked ({observed} cross-edges vs {expected:.1f} expected).")
            score = expected - observed
            cid = _cid("T2", [top_a, top_b])
            out[cid] = _record(cid, "T2", claim, grounds, score,
                               {"expected": expected, "observed": observed})
    return out


def _t2_paper(view):
    """Paper-closable T2 variant: same holes, claims shaped as testable transfers.

    Per hole and per direction, the flagship (max-PageRank) Method of one cluster
    is paired with the flagship Problem of the other; the claim asserts the
    application is unexamined. Still purely structural — no LLM proposes anything.
    Pairs already linked in the projection are skipped (the claim would be false
    on its face). cid gets a 'paper' slot so ids never collide with hole-shaped
    T2 candidates over the same node pair.
    """
    def flagship(nodes, node_type, pr):
        typed = [n for n in nodes if view.ntype.get(n) == node_type]
        return max(typed, key=lambda n: pr.get(n, 0.0)) if typed else None

    def establishing(node):
        quotes = []
        for nb in sorted(view.proj.neighbors(node)):
            for doc, ev, _ in view.edge_docs[frozenset((node, nb))]:
                if ev:
                    quotes.append(_quote(doc, ev))
        quotes.sort(key=lambda q: (len(q["quote"]), q["doc_id"]))
        return quotes[:3]

    out = {}
    for ca, cb, members, expected, observed, bridges, pr in _t2_holes(view):
        for c_m, c_p in ((ca, cb), (cb, ca)):
            method = flagship(members[c_m], "Method", pr)
            problem = flagship(members[c_p], "Problem", pr)
            if method is None or problem is None or view.proj.has_edge(method, problem):
                continue
            ev = establishing(method) + establishing(problem)
            grounds = {
                "node_ids": sorted({method, problem, *bridges}),
                "doc_ids": sorted({q["doc_id"] for q in ev}),
                "evidence": ev,
                "method": view.name.get(method),
                "problem": view.name.get(problem),
                "bridge_node_ids": sorted(bridges),
                "expected": expected,
                "observed": observed,
            }
            claim = (f"Method '{view.name.get(method)}' has not been applied to problem "
                     f"'{view.name.get(problem)}': they sit in adjacent but under-linked "
                     f"clusters ({observed} cross-edges vs {expected:.1f} expected) that "
                     f"share bridge entities; the application is unexamined.")
            cid = _cid("T2", [method, problem, "paper"])
            out[cid] = _record(cid, "T2", claim, grounds, expected - observed,
                               {"variant": "paper", "expected": expected,
                                "observed": observed})
    return out


# --------------------------------------------------------------------------- #
# T3 — unexamined assumption
# --------------------------------------------------------------------------- #
def _t3(view):
    out = {}
    for node_id, t in view.ntype.items():
        if t != "Assumption":
            continue
        neighbors = set(view.proj.neighbors(node_id))
        if len(neighbors) < 2:
            continue
        if len(view.node_docs.get(node_id, set())) < 2:
            continue
        ncomms = {view.comm.get(nb) for nb in neighbors}
        if len(ncomms) != 1 or None in ncomms:
            continue
        (cluster,) = ncomms

        evidence = []
        for u, v, d in view.g.edges(data=True):
            if u == node_id or v == node_id:
                evidence.append(_quote(d["doc_id"], d.get("evidence")))
        grounds = {
            "node_ids": [node_id, *sorted(neighbors)],
            "doc_ids": sorted(view.node_docs.get(node_id, set())),
            "evidence": evidence,
            "community_id": cluster,
        }
        claim = (f"Assumption '{view.name[node_id]}' is load-bearing in cluster {cluster} "
                 f"and unexamined elsewhere.")
        cid = _cid("T3", [node_id])
        out[cid] = _record(cid, "T3", claim, grounds, float(len(neighbors)),
                           {"community_id": cluster})
    return out


# --------------------------------------------------------------------------- #
# T4 — contradiction (one candidate per CONTRADICTS canonical pair)
# --------------------------------------------------------------------------- #
def _t4(view):
    pairs = defaultdict(list)  # frozenset({u,v}) -> [(u, v, doc, ev)]
    for u, v, d in view.g.edges(data=True):
        # u == v: both endpoints resolved to one canonical node (ER merge of a
        # contradicting pair) — a self-loop, not a T4 pair
        if d["edge_type"] == "CONTRADICTS" and u != v:
            pairs[frozenset((u, v))].append((u, v, d["doc_id"], d.get("evidence")))

    out = {}
    for pair, edges in pairs.items():
        a, b = sorted(pair)
        shared = {
            n for n in (set(view.proj.neighbors(a)) & set(view.proj.neighbors(b)))
            if view.ntype.get(n) in ("Metric", "Dataset")
        }
        evidence = [_quote(doc, ev) for _, _, doc, ev in edges]
        grounds = {
            "node_ids": sorted({a, b} | shared),
            "doc_ids": sorted({doc for _, _, doc, _ in edges}),
            "evidence": evidence,
            "shared_neighbor_ids": sorted(shared),
        }
        claim = (f"'{view.name[a]}' and '{view.name[b]}' are asserted to contradict each "
                 f"other across {len(edges)} mention(s).")
        cid = _cid("T4", [a, b])
        out[cid] = _record(cid, "T4", claim, grounds, float(len(edges)),
                           {"mentions": len(edges)})
    return out


def _record(cid, template, claim, grounds, score, params):
    return {
        "candidate_id": cid,
        "template": template,
        "claim": claim,
        "grounds": grounds,
        "score": float(score),
        "params": params,
    }


# --------------------------------------------------------------------------- #
# prerequisite guard (W11) + persistence (W6)
# --------------------------------------------------------------------------- #
def _guard(conn):
    total = conn.execute("SELECT count(*) FROM canonical_nodes").fetchone()[0]
    if total == 0:
        raise RuntimeError("propose: no canonical_nodes — run resolve first.")
    covered = conn.execute(
        "SELECT count(*) FROM canonical_nodes c "
        "WHERE EXISTS (SELECT 1 FROM node_embeddings e WHERE e.node_id = c.node_id)"
    ).fetchone()[0]
    if covered / total < COVERAGE_MIN:
        raise RuntimeError(
            f"propose: node_embeddings cover {covered}/{total} canonical_nodes "
            f"(< {COVERAGE_MIN:.0%}) — run embed-nodes."
        )
    if conn.execute("SELECT count(*) FROM communities").fetchone()[0] == 0:
        raise RuntimeError("propose: communities is empty — run communities.")


def _cap(records: dict, k: int) -> list:
    """Top-K per template by score desc (candidate_id tie-break)."""
    ranked = sorted(records.values(), key=lambda r: (-r["score"], r["candidate_id"]))
    return ranked[:k]


def propose(conn, *, k: int = 25, t1_theta: float = 0.60, t2_paper: bool = False) -> dict:
    """Run T1-T4, (re)write status='proposed' gap_candidates, return per-template counts.

    t2_paper swaps T2's hole-shaped claims for the paper-closable variant (4b eval).
    """
    _guard(conn)
    view = _View(conn)

    capped = {
        "T1": _cap(_t1(view, t1_theta), k),
        "T2": _cap(_t2_paper(view) if t2_paper else _t2(view), k),
        "T3": _cap(_t3(view), k),
        "T4": _cap(_t4(view), k),
    }

    # W6 wipe: drop proposed candidates' verdicts, then those candidates, and COMMIT
    # before reinserting — a wipe+reinsert of the same PK in one transaction trips the
    # DuckDB ART index (wiki/gotchas.md). Non-proposed rows and their verdicts survive.
    conn.execute(
        "DELETE FROM verdicts WHERE candidate_id IN "
        "(SELECT candidate_id FROM gap_candidates WHERE status = 'proposed')"
    )
    conn.execute("DELETE FROM gap_candidates WHERE status = 'proposed'")
    conn.commit()

    # Fresh candidates whose id already exists as a terminal (non-proposed) row are left
    # as-is: reinserting the PK would conflict, and the terminal disposition must stand.
    existing = {r[0] for r in conn.execute("SELECT candidate_id FROM gap_candidates").fetchall()}
    for records in capped.values():
        for rec in records:
            if rec["candidate_id"] in existing:
                continue
            conn.execute(
                "INSERT INTO gap_candidates "
                "(candidate_id, template, claim, grounds, score, params, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'proposed')",
                [rec["candidate_id"], rec["template"], rec["claim"],
                 json.dumps(rec["grounds"]), rec["score"], json.dumps(rec["params"])],
            )
    conn.commit()

    counts = {t: len(records) for t, records in capped.items()}
    counts["total"] = sum(counts.values())
    return counts


def sweep_t1(conn, thetas) -> list[tuple[float, int]]:
    """Report the raw T1 candidate yield at each theta; writes nothing."""
    view = _View(conn)
    return [(theta, sum(1 for _ in _t1_pairs(view, theta))) for theta in thetas]
