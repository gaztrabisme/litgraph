"""Executable spec for litgraph.frontier — the frontier-gaps runner (4c).

Contract under test:
    frontier.hole_leads(conn, k) -> list of lead dicts (cluster names, expected,
        observed, bridges, score), best-first, NO writes to gap_candidates.
    frontier.run_qwen(conn, session_system, ground_system, ...) -> tally dict;
        inserts FG:qwen:nnn rows (status='proposed', params.arm='frontier').
    frontier.insert continues the FG:<arm>: sequence across runs.

LLM boundaries (chat_fn / ground_chat_fn / embed_fn) are scripted fakes; the hole
fixture reuses the test_templates Graph builder (two dense clusters, one thin
cross edge).
"""

import json

from litgraph import frontier

from test_templates import E0, Graph, new_db


def hole_graph(conn):
    """Two dense same-vector clusters joined by one cross edge -> one T2 hole."""
    g = Graph(conn)
    for i in range(6):
        g.node(f"a{i}", "Method", vec=E0, community=0)
        g.node(f"b{i}", "Problem", vec=E0, community=1)
    for i in range(1, 6):
        g.edge("a0", f"a{i}", "CITES", doc="d1")
        g.edge("b0", f"b{i}", "CITES", doc="d1")
    g.edge("a1", "b1", "CITES", doc="d2")
    return g


def test_hole_leads_returns_named_leads_without_writes():
    conn = new_db()
    hole_graph(conn)

    leads = frontier.hole_leads(conn, k=5)

    assert len(leads) == 1
    lead = leads[0]
    assert {lead["cluster_a"], lead["cluster_b"]} == {"a0", "b0"}
    assert lead["observed"] == 1
    assert lead["score"] > 0
    assert "a1" in lead["bridges"] and "b1" in lead["bridges"]
    (n,) = conn.execute("SELECT count(*) FROM gap_candidates").fetchone()
    assert n == 0, "hole_leads must be read-only"


def test_run_qwen_inserts_grounded_fg_candidates():
    """A scripted session proposes once; grounding returns a verbatim quote; the
    proposal lands as FG:qwen:001 with status='proposed' and frontier params."""
    conn = new_db()
    hole_graph(conn)
    conn.execute(
        "UPDATE documents SET full_text = 'The MARKER sentence grounds this claim.' "
        "WHERE doc_id = 'd2'")

    script = iter([
        {"action": "propose", "args": {
            "claim": "Method a0 has never been applied to problem b0.",
            "doc_ids": ["d2"], "warrant": "clusters are disjoint",
            "self_critique": "maybe d2 already does this"}},
        {"action": "done"},
    ])

    def chat_fn(messages, schema_name, schema):
        return next(script)

    def ground_chat_fn(system, user):
        return {"quotes": [{"doc_id": "d2", "quote": "The MARKER sentence"}]}

    tally = frontier.run_qwen(
        conn, "SYS", "GROUND", max_holes=1, max_workers=1,
        chat_fn=chat_fn, ground_chat_fn=ground_chat_fn, embed_fn=lambda n: E0)

    assert tally == {"sessions": 1, "proposals": 1, "unique": 1,
                     "grounded": 1, "inserted": 1}
    row = conn.execute(
        "SELECT claim, grounds, params, status FROM gap_candidates "
        "WHERE candidate_id = 'FG:qwen:001'").fetchone()
    assert row is not None
    claim, grounds, params, status = row
    assert status == "proposed"
    assert "a0" in claim
    assert json.loads(grounds)["quotes"] == ["The MARKER sentence"]
    assert json.loads(params)["arm"] == "frontier"


def test_insert_continues_sequence_and_dedupe_drops_repeats():
    conn = new_db()
    hole_graph(conn)
    g = {"claim": "c1", "grounds": {"doc_ids": ["d1"], "quotes": ["q"]},
         "warrant": "w", "self_critique": "s"}
    frontier.insert(conn, [g], arm="qwen", proposer_name="qwen-tools")
    frontier.insert(conn, [g | {"claim": "c2"}], arm="qwen", proposer_name="qwen-tools")
    ids = [r[0] for r in conn.execute(
        "SELECT candidate_id FROM gap_candidates ORDER BY candidate_id").fetchall()]
    assert ids == ["FG:qwen:001", "FG:qwen:002"]

    dupes = [{"claim": "Same   CLAIM!"}, {"claim": "same claim"}, {"claim": "other"}]
    assert [p["claim"] for p in frontier.dedupe(dupes)] == ["Same   CLAIM!", "other"]
