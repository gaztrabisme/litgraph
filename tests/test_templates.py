"""Executable spec for litgraph.templates — the deterministic gap proposer (T1-T4).

Contract under test (fixed by coordinator):
    templates.propose(conn, *, k=25, t1_theta=0.60) -> dict
        runs T1-T4, writes gap_candidates rows (status='proposed'),
        returns {"T1": n, "T2": n, "T3": n, "T4": n, "total": n}
    templates.sweep_t1(conn, thetas) -> list[tuple[float, int]]   # yield per theta, no writes

Spec: wiki/phase2-design.md §Templates (T1-T4 semantics, C4 context-embedding space,
W2/W10/W11), §Data model additions (candidate_id, prerequisite guard W11), §Re-run
semantics W6. Fixtures are synthetic rows straight into the storage contract, read back
through graph.build (graph.py join). Communities and node_embeddings are inserted directly
(NOT via communities.assign / embeddings) so this file is independent of those modules.

candidate_id == f"{template}:{sha256('\\n'.join(sorted(slot_node_ids))).hexdigest()[:12]}"
    slot_node_ids: T1 [method, problem_a, problem_b]; T2 sorted([top_pagerank_a,
    top_pagerank_b]); T3 [assumption]; T4 [src, dst].

The module does not exist yet, so collection fails at import (ModuleNotFoundError)
until an implementation agent lands templates.py.
"""

import hashlib
import json
import math

import pytest

from litgraph import db, templates
from litgraph.models import EMBEDDING_MODEL, EXTRACTION_MODEL, PROMPT_VERSION

# ---------------------------------------------------------------------------
# Synthetic 1024-dim vectors with hand-picked cosines (only first two dims used).
# cos(E0, ·) == the vector's first component (all are unit vectors).
# ---------------------------------------------------------------------------
DIM = 1024


def _vec(x0: float, x1: float = 0.0) -> list[float]:
    v = [0.0] * DIM
    v[0], v[1] = x0, x1
    return v


E0 = _vec(1.0, 0.0)                         # reference
E1 = _vec(0.0, 1.0)                         # orthogonal to E0 (cos 0.0)
V080 = _vec(0.8, 0.6)                       # cos(E0, ·) == 0.80 exactly
V050 = _vec(0.5, math.sqrt(1.0 - 0.25))     # cos(E0, ·) == 0.50 exactly


def cid(template: str, slot_node_ids: list[str]) -> str:
    """Recompute the locked candidate_id from defining slot node_ids."""
    digest = hashlib.sha256("\n".join(sorted(slot_node_ids)).encode()).hexdigest()[:12]
    return f"{template}:{digest}"


# ---------------------------------------------------------------------------
# Fixture builder: canonical_nodes + aliases + embeddings + communities, and
# relation/entity mentions that graph.build joins back into edges.
# ---------------------------------------------------------------------------
class Graph:
    def __init__(self, conn):
        self.conn = conn
        self._docs: set[str] = set()
        self._mid = 0
        self._nodes: dict[str, tuple[str, str]] = {}  # node_id -> (name, type)

    def node(self, node_id, node_type, *, name=None, vec=E0, community=0, mention_docs=()):
        name = name or node_id
        self._nodes[node_id] = (name, node_type)
        self.conn.execute(
            "INSERT INTO canonical_nodes (node_id, node_type, canonical_name) VALUES (?, ?, ?)",
            [node_id, node_type, name],
        )
        self.conn.execute(
            "INSERT INTO aliases (alias_name, node_type, node_id, method, score) "
            "VALUES (?, ?, ?, 'exact', 1.0)",
            [name, node_type, node_id],
        )
        if vec is not None:
            self.conn.execute(
                "INSERT INTO node_embeddings (node_id, model, vec) VALUES (?, ?, ?)",
                [node_id, EMBEDDING_MODEL, vec],
            )
        if community is not None:
            self.conn.execute(
                "INSERT INTO communities (node_id, community_id, algo, seed) "
                "VALUES (?, ?, 'louvain', 42)",
                [node_id, community],
            )
        for d in mention_docs:
            self._doc(d)
            self._mid += 1
            self.conn.execute(
                "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, entity_type, name) "
                "VALUES (?, ?, ?, 'entity', ?, ?)",
                [f"m{self._mid}", f"e-{d}", d, node_type, name],
            )
        return node_id

    def _doc(self, doc_id):
        if doc_id in self._docs:
            return
        self._docs.add(doc_id)
        self.conn.execute(
            "INSERT INTO documents (doc_id, source_type, url, title) VALUES (?, 'paper', ?, ?)",
            [doc_id, f"http://x/{doc_id}", doc_id],
        )
        self.conn.execute(
            "INSERT INTO extractions (extraction_id, doc_id, prompt_version, model, status) "
            "VALUES (?, ?, ?, ?, 'ok')",
            [f"e-{doc_id}", doc_id, PROMPT_VERSION, EXTRACTION_MODEL],
        )

    def edge(self, src_id, dst_id, edge_type, *, doc="d1", evidence="ev"):
        self._doc(doc)
        sn, st = self._nodes[src_id]
        dn, dt = self._nodes[dst_id]
        self._mid += 1
        self.conn.execute(
            "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, "
            "src_name, src_type, edge_type, dst_name, dst_type, evidence) "
            "VALUES (?, ?, ?, 'relation', ?, ?, ?, ?, ?, ?)",
            [f"m{self._mid}", f"e-{doc}", doc, sn, st, edge_type, dn, dt, evidence],
        )


def new_db():
    return db.connect(":memory:")


def candidates(conn, template=None):
    rows = conn.execute(
        "SELECT candidate_id, template, claim, grounds, score, status FROM gap_candidates"
    ).fetchall()
    out = []
    for cand_id, tmpl, claim, grounds, score, status in rows:
        out.append(
            {
                "candidate_id": cand_id,
                "template": tmpl,
                "claim": claim,
                "grounds": json.loads(grounds) if isinstance(grounds, str) else grounds,
                "score": score,
                "status": status,
            }
        )
    if template is not None:
        out = [c for c in out if c["template"] == template]
    return out


# ---------------------------------------------------------------------------
# T1 — transfer gap.  E = {APPLIED_TO, USES, EVALUATED_ON}, Method->Problem only.
# Planted gap: m1 USES pa; shared method m2 USES pa AND pb; no m1->pb E-edge;
# cos(pa, pb) == 0.80.  Candidate == (m1, pa, pb).
# ---------------------------------------------------------------------------
def t1_graph(conn, *, m1_to_b=False, m2_to_b=True, extra_edge=None, vec_b=V080):
    g = Graph(conn)
    g.node("m1", "Method", vec=E0)
    g.node("m2", "Method", vec=E0)
    g.node("pa", "Problem", vec=E0)
    g.node("pb", "Problem", vec=vec_b)
    g.edge("m1", "pa", "USES", doc="d1", evidence="m1 applies to pa")
    g.edge("m2", "pa", "USES", doc="d1", evidence="m2 applies to pa")
    if m2_to_b:
        g.edge("m2", "pb", "USES", doc="d2", evidence="m2 applies to pb")
    if m1_to_b:
        g.edge("m1", "pb", "USES", doc="d2", evidence="m1 already applies to pb")
    if extra_edge is not None:
        g.edge("m1", "pb", extra_edge, doc="d3", evidence="non-E edge to pb")
    return g


def test_t1_planted_gap_found():
    """m1 USES pa, never pb; pa/pb cos 0.80 >= theta; shared method m2 -> one T1
    candidate with the locked id and node_ids/doc_ids/evidence in grounds."""
    conn = new_db()
    t1_graph(conn)

    result = templates.propose(conn)

    assert result["T1"] == 1
    (cand,) = candidates(conn, "T1")
    assert cand["candidate_id"] == cid("T1", ["m1", "pa", "pb"])
    assert cand["status"] == "proposed"
    assert cand["score"] is not None and cand["score"] > 0
    grounds = cand["grounds"]
    assert "node_ids" in grounds and "doc_ids" in grounds
    assert {"m1", "pa", "pb"} <= set(grounds["node_ids"])
    # evidence quotes for the establishing edge travel in grounds
    assert "m1 applies to pa" in json.dumps(grounds)


def test_t1_countergraph_method_already_applied_no_candidate():
    """m1 USES pb as well (E-edge) -> the gap is closed -> zero T1 candidates."""
    conn = new_db()
    t1_graph(conn, m1_to_b=True)

    result = templates.propose(conn)

    assert result["T1"] == 0
    assert candidates(conn, "T1") == []


def test_t1_theta_above_pair_cosine_no_candidate():
    """t1_theta above the pair cosine (0.80) drops the pair -> zero T1 candidates."""
    conn = new_db()
    t1_graph(conn)

    result = templates.propose(conn, t1_theta=0.90)

    assert result["T1"] == 0


def test_t1_pair_sharing_no_method_no_candidate():
    """pa and pb share zero methods via E (m2 no longer USES pb) -> not eligible."""
    conn = new_db()
    t1_graph(conn, m2_to_b=False)

    result = templates.propose(conn)

    assert result["T1"] == 0


@pytest.mark.parametrize("edge", ["CITES", "CONTRADICTS"])
def test_t1_non_e_edge_to_b_still_emits(edge):
    """A CITES/CONTRADICTS m1->pb edge is NOT in E and must NOT disqualify: the
    transfer candidate is still emitted (only APPLIED_TO/USES/EVALUATED_ON count)."""
    conn = new_db()
    t1_graph(conn, extra_edge=edge)

    result = templates.propose(conn)

    assert result["T1"] == 1
    (cand,) = candidates(conn, "T1")
    assert cand["candidate_id"] == cid("T1", ["m1", "pa", "pb"])


def test_sweep_t1_reports_yield_per_theta_without_writing():
    """sweep_t1 returns (theta, count) with the pair (cos 0.80) counted at
    theta<=0.80 and dropped above it; it writes no gap_candidates."""
    conn = new_db()
    t1_graph(conn)

    swept = templates.sweep_t1(conn, [0.5, 0.7, 0.9])

    assert swept == [(0.5, 1), (0.7, 1), (0.9, 0)]
    assert conn.execute("SELECT count(*) FROM gap_candidates").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# T2 — structural hole (configuration-model null + real bridge, W10).
# Two star communities (hubs a0/b0) with a single a0-b0 bridge:
# expected 5.5 (>=5), observed cross 1 (<= 0.25*expected), centroid cos 1.0.
# ---------------------------------------------------------------------------
def t2_graph(conn, *, cross_edges=1, bridge=True):
    g = Graph(conn)
    for i in range(6):
        g.node(f"a{i}", "Method", vec=E0, community=0)
        g.node(f"b{i}", "Method", vec=E0, community=1)
    for i in range(1, 6):
        g.edge("a0", f"a{i}", "CITES", doc="d1")
        g.edge("b0", f"b{i}", "CITES", doc="d1")
    if bridge:
        for i in range(cross_edges):
            g.edge(f"a{i}", f"b{i}", "CITES", doc="d2")
    return g


def test_t2_planted_hole_found():
    """Sparse cross-linkage between two dense communities with a bridge node ->
    one T2 candidate naming both clusters in grounds."""
    conn = new_db()
    t2_graph(conn)

    result = templates.propose(conn)

    assert result["T2"] == 1
    (cand,) = candidates(conn, "T2")
    assert cand["candidate_id"].startswith("T2:")
    assert "node_ids" in cand["grounds"] and "doc_ids" in cand["grounds"]
    # both community hubs are the bridge/defining nodes
    assert {"a0", "b0"} <= set(cand["grounds"]["node_ids"])


def test_t2_countergraph_dense_cross_edges_no_hole():
    """Many observed cross-edges (> 0.25 * expected) -> not a structural hole."""
    conn = new_db()
    t2_graph(conn, cross_edges=4)

    result = templates.propose(conn)

    assert result["T2"] == 0


def test_t2_countergraph_no_bridge_no_candidate():
    """Zero cross-edges: sparse enough, but no node bridges both communities ->
    the bridge requirement fails -> zero T2 candidates."""
    conn = new_db()
    t2_graph(conn, bridge=False)

    result = templates.propose(conn)

    assert result["T2"] == 0


# ---------------------------------------------------------------------------
# T2 paper-closable variant (4b): same holes, flagship Method x Problem claims.
# ---------------------------------------------------------------------------
def t2_paper_graph(conn, *, bridge_hubs=False):
    """Methods cluster 0 (hub a0) vs Problems cluster 1 (hub b0); one cross edge:
    a0-b0 when bridge_hubs (flagships already linked) else a1-b1."""
    g = Graph(conn)
    for i in range(6):
        g.node(f"a{i}", "Method", vec=E0, community=0)
        g.node(f"b{i}", "Problem", vec=E0, community=1)
    for i in range(1, 6):
        g.edge("a0", f"a{i}", "CITES", doc="d1")
        g.edge("b0", f"b{i}", "CITES", doc="d1")
    u, v = ("a0", "b0") if bridge_hubs else ("a1", "b1")
    g.edge(u, v, "CITES", doc="d2")
    return g


def test_t2_paper_flagship_method_problem_claim():
    """t2_paper=True reshapes the hole into 'M has not been applied to P' with the
    max-PageRank Method/Problem flagships; params.variant marks it; cid carries a
    'paper' slot so it can never collide with the hole-shaped id."""
    conn = new_db()
    t2_paper_graph(conn)

    result = templates.propose(conn, t2_paper=True)

    assert result["T2"] == 1
    (cand,) = candidates(conn, "T2")
    assert "has not been applied to" in cand["claim"]
    assert cand["grounds"]["method"] == "a0"
    assert cand["grounds"]["problem"] == "b0"
    params = json.loads(conn.execute(
        "SELECT params FROM gap_candidates WHERE candidate_id = ?",
        [cand["candidate_id"]]).fetchone()[0])
    assert params["variant"] == "paper"
    assert cand["candidate_id"] == cid("T2", ["a0", "b0", "paper"])


def test_t2_paper_skips_flagships_already_linked():
    """When the flagship Method-Problem pair already shares an edge the claim would
    be false on its face -> no candidate from that direction (and the reverse
    direction has no Method/Problem flagships here)."""
    conn = new_db()
    t2_paper_graph(conn, bridge_hubs=True)

    result = templates.propose(conn, t2_paper=True)

    assert result["T2"] == 0


# ---------------------------------------------------------------------------
# T3 — unexamined assumption: Assumption node, >=2 neighbors, >=2 mention docs,
# all neighbors in one community.
# ---------------------------------------------------------------------------
def t3_graph(conn, *, n2_community=0, two_neighbors=True, two_docs=True):
    g = Graph(conn)
    g.node("asm", "Assumption", vec=E0, community=0, mention_docs=("d1", "d2"))
    g.node("n1", "Method", vec=E0, community=0)
    g.node("n2", "Problem", vec=E0, community=n2_community)
    g.edge("asm", "n1", "ASSUMES", doc="d1", evidence="asm underlies n1")
    if two_neighbors:
        g.edge("asm", "n2", "ASSUMES", doc="d2" if two_docs else "d1",
               evidence="asm underlies n2")
    return g


def test_t3_planted_assumption_found():
    """Assumption with 2 neighbors, 2 mention docs, neighbors in one community ->
    one T3 candidate with the locked single-slot id."""
    conn = new_db()
    t3_graph(conn)

    result = templates.propose(conn)

    assert result["T3"] == 1
    (cand,) = candidates(conn, "T3")
    assert cand["candidate_id"] == cid("T3", ["asm"])
    assert "asm" in set(cand["grounds"]["node_ids"])


def test_t3_neighbors_span_two_communities_no_candidate():
    """Neighbors split across two communities -> assumption is not localized ->
    zero T3 candidates."""
    conn = new_db()
    t3_graph(conn, n2_community=1)

    result = templates.propose(conn)

    assert result["T3"] == 0


def test_t3_single_neighbor_no_candidate():
    """Fewer than 2 neighbors -> zero T3 candidates."""
    conn = new_db()
    t3_graph(conn, two_neighbors=False)

    result = templates.propose(conn)

    assert result["T3"] == 0


def test_t3_single_mention_doc_no_candidate():
    """Assumption mentioned in only one doc -> fails the >=2 mention-doc gate."""
    conn = new_db()
    # collapse both edges + entity mentions onto d1 only
    g = Graph(conn)
    g.node("asm", "Assumption", vec=E0, community=0, mention_docs=("d1",))
    g.node("n1", "Method", vec=E0, community=0)
    g.node("n2", "Problem", vec=E0, community=0)
    g.edge("asm", "n1", "ASSUMES", doc="d1")
    g.edge("asm", "n2", "ASSUMES", doc="d1")

    result = templates.propose(conn)

    assert result["T3"] == 0


# ---------------------------------------------------------------------------
# T4 — contradiction: one candidate per CONTRADICTS canonical pair, both
# directions' evidence + shared Metric/Dataset neighbor in grounds.
# ---------------------------------------------------------------------------
def test_t4_emits_one_candidate_per_pair_with_both_evidence():
    """X<->Y CONTRADICTS (both directions) + a shared Metric neighbor Z -> exactly
    one T4 candidate whose grounds carry both quotes and Z's node_id."""
    conn = new_db()
    g = Graph(conn)
    g.node("X", "Method", vec=E0)
    g.node("Y", "Claim", vec=E0)
    g.node("Z", "Metric", vec=E0)
    g.edge("X", "Y", "CONTRADICTS", doc="d1", evidence="X refutes Y")
    g.edge("Y", "X", "CONTRADICTS", doc="d2", evidence="Y refutes X")
    g.edge("X", "Z", "EVALUATED_ON", doc="d1")
    g.edge("Y", "Z", "EVALUATED_ON", doc="d2")

    result = templates.propose(conn)

    assert result["T4"] == 1
    (cand,) = candidates(conn, "T4")
    assert cand["candidate_id"] == cid("T4", ["X", "Y"])
    blob = json.dumps(cand["grounds"])
    assert "X refutes Y" in blob and "Y refutes X" in blob
    assert "Z" in set(cand["grounds"]["node_ids"])


def test_t4_self_loop_contradicts_skipped():
    """A CONTRADICTS edge whose endpoints ER-merged into one canonical node is a
    self-loop, not a pair — it must be skipped, not crash the unpack (Phase 4a
    regression: 1-element frozenset -> ValueError)."""
    conn = new_db()
    g = Graph(conn)
    g.node("X", "Method", vec=E0)
    g.node("Y", "Claim", vec=E0)
    g.edge("X", "X", "CONTRADICTS", doc="d1", evidence="X refutes X")
    g.edge("X", "Y", "CONTRADICTS", doc="d2", evidence="X refutes Y")

    result = templates.propose(conn)

    assert result["T4"] == 1
    (cand,) = candidates(conn, "T4")
    assert cand["candidate_id"] == cid("T4", ["X", "Y"])


# ---------------------------------------------------------------------------
# Prerequisite guard (W11): >=95% embedding coverage AND non-empty communities.
# ---------------------------------------------------------------------------
def test_propose_guard_missing_embeddings_raises():
    """No node_embeddings coverage -> propose aborts with RuntimeError."""
    conn = new_db()
    g = Graph(conn)
    g.node("X", "Method", vec=None, community=0)
    g.node("Y", "Problem", vec=None, community=0)

    with pytest.raises(RuntimeError):
        templates.propose(conn)


def test_propose_guard_empty_communities_raises():
    """Embeddings present but communities empty -> propose aborts with RuntimeError."""
    conn = new_db()
    g = Graph(conn)
    g.node("X", "Method", vec=E0, community=None)
    g.node("Y", "Problem", vec=E0, community=None)

    with pytest.raises(RuntimeError):
        templates.propose(conn)


# ---------------------------------------------------------------------------
# Return shape, id stability, re-run semantics (W6), top-K capping.
# ---------------------------------------------------------------------------
def test_propose_return_shape_total_is_sum():
    """Result carries all four template keys plus total == their sum."""
    conn = new_db()
    t1_graph(conn)

    result = templates.propose(conn)

    assert set(result) == {"T1", "T2", "T3", "T4", "total"}
    assert result["total"] == result["T1"] + result["T2"] + result["T3"] + result["T4"]


def test_candidate_ids_stable_across_runs():
    """Two propose runs on the same DB yield identical candidate_ids (C1: node_ids
    are deterministic, so slot hashes are stable)."""
    conn = new_db()
    t1_graph(conn)

    templates.propose(conn)
    first = {c["candidate_id"] for c in candidates(conn)}
    templates.propose(conn)
    second = {c["candidate_id"] for c in candidates(conn)}

    assert first == second and first


def _two_contradiction_pairs(conn, *, p1_docs=("d1", "d2"), p2_docs=("d1",)):
    g = Graph(conn)
    for nid in ("x1", "y1", "x2", "y2"):
        g.node(nid, "Method" if nid.startswith("x") else "Claim", vec=E0)
    for d in p1_docs:
        g.edge("x1", "y1", "CONTRADICTS", doc=d, evidence=f"x1 refutes y1 in {d}")
    for d in p2_docs:
        g.edge("x2", "y2", "CONTRADICTS", doc=d, evidence=f"x2 refutes y2 in {d}")
    return g


def test_rerun_preserves_nonproposed_candidates_and_wipes_proposed_verdicts():
    """W6: re-propose keeps a killed candidate + its verdict, but rebuilds proposed
    candidates and deletes their prior verdicts."""
    conn = new_db()
    _two_contradiction_pairs(conn)
    templates.propose(conn)

    ids = sorted(c["candidate_id"] for c in candidates(conn, "T4"))
    keep_id, other_id = ids[0], ids[1]
    # kill one candidate and attach a verdict to each
    conn.execute("UPDATE gap_candidates SET status='killed' WHERE candidate_id = ?", [keep_id])
    conn.execute(
        "INSERT INTO verdicts (verdict_id, candidate_id, stage, critic_model, critic_role, verdict) "
        "VALUES ('vk', ?, 'A', 'm', 'r', 'kill')",
        [keep_id],
    )
    conn.execute(
        "INSERT INTO verdicts (verdict_id, candidate_id, stage, critic_model, critic_role, verdict) "
        "VALUES ('vp', ?, 'A', 'm', 'r', 'survive')",
        [other_id],
    )

    templates.propose(conn)

    surviving = {c["candidate_id"]: c["status"] for c in candidates(conn)}
    assert surviving.get(keep_id) == "killed", "non-proposed candidate untouched"
    assert surviving.get(other_id) == "proposed", "proposed candidate rebuilt"
    verdict_ids = {
        r[0] for r in conn.execute("SELECT verdict_id FROM verdicts").fetchall()
    }
    assert "vk" in verdict_ids, "killed candidate's verdict preserved"
    assert "vp" not in verdict_ids, "rebuilt candidate's stale verdict deleted"


def test_top_k_caps_each_template_by_score():
    """k=1 caps a template to its single top-ranked candidate; T4 ranks by evidence
    count, so the 2-mention pair is kept and the 1-mention pair dropped."""
    conn = new_db()
    _two_contradiction_pairs(conn)  # pair1 (x1,y1) 2 mentions; pair2 (x2,y2) 1 mention

    full = templates.propose(conn, k=25)
    assert full["T4"] == 2

    capped = templates.propose(conn, k=1)
    assert capped["T4"] == 1
    (cand,) = candidates(conn, "T4")
    assert cand["candidate_id"] == cid("T4", ["x1", "y1"]), "higher-evidence pair kept"
