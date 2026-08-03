"""Executable spec for litgraph.tools — the 4 read-only assassin tools.

Contract under test (locked by coordinator; phase2-design.md "Assassin tools").
Every tool takes a duckdb connection/cursor first, RETURNS a dict (never raises on
bad input -> {"error": ...}), and marks caps with an explicit "truncated": n_omitted:

    subgraph_around(cur, node_id, hops=1) -> dict
        node + neighbors via the graph.py alias join; cap 40 edges (evidence-bearing
        first, then by doc_id); each edge {edge_type, src{node_id,name,type},
        dst{...}, doc_id, evidence}. Unknown node_id -> {"error": ...}. hops clamped
        to 1 for MVP (documented in output).
    full_text(cur, doc_id) -> dict
        {doc_id, title, full_text} with full_text capped at 30_000 chars + truncated
        marker. Unknown doc_id -> {"error": ...}.
    evidence_for(cur, node_id) -> dict
        mention evidence quotes for the node (entity mentions resolving via aliases
        AND relation mentions where either endpoint resolves); cap 20 items
        {doc_id, quote, edge_type|null}.
    who_tried_this(cur, method_name=None, problem_name=None, embed_fn=None) -> dict
        name resolution: casefold-exact alias (per node_type), else embedding
        top-match >= 0.7 against node_embeddings of that type; output reports the
        resolution; unresolvable name -> "absence_not_established"; both resolved ->
        edges between them; one name -> edges touching it (cap 40); closed-world note.

The module does not exist yet, so collection fails with ModuleNotFoundError until
the implementation agent lands tools.py. All fixtures are direct DuckDB inserts.
"""

from litgraph import db
from litgraph import tools
from litgraph.models import EMBEDDING_MODEL, EXTRACTION_MODEL, PROMPT_VERSION

DIM = 1024


# ---------------------------------------------------------------------------
# Row builders (straight into the storage contract).
# ---------------------------------------------------------------------------
def new_db():
    return db.connect(":memory:")


def add_doc(conn, doc_id, *, title=None, full_text=None, published_date=None):
    conn.execute(
        "INSERT INTO documents (doc_id, source_type, url, title, published_date, full_text) "
        "VALUES (?, 'paper', ?, ?, ?, ?)",
        [doc_id, f"http://ex/{doc_id}", title or doc_id, published_date, full_text],
    )


def add_extraction(conn, ext_id, doc_id):
    conn.execute(
        "INSERT INTO extractions (extraction_id, doc_id, prompt_version, model, status) "
        "VALUES (?, ?, ?, ?, 'ok')",
        [ext_id, doc_id, PROMPT_VERSION, EXTRACTION_MODEL],
    )


def add_canonical(conn, node_id, node_type, name):
    conn.execute(
        "INSERT INTO canonical_nodes (node_id, node_type, canonical_name) VALUES (?, ?, ?)",
        [node_id, node_type, name],
    )


def add_alias(conn, alias_name, node_type, node_id, *, method="exact", score=1.0):
    conn.execute(
        "INSERT INTO aliases (alias_name, node_type, node_id, method, score) VALUES (?, ?, ?, ?, ?)",
        [alias_name, node_type, node_id, method, score],
    )


def add_relation(conn, mid, ext_id, doc_id, *, src_name, src_type, edge_type, dst_name, dst_type, evidence):
    conn.execute(
        "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, "
        "src_name, src_type, edge_type, dst_name, dst_type, evidence) "
        "VALUES (?, ?, ?, 'relation', ?, ?, ?, ?, ?, ?)",
        [mid, ext_id, doc_id, src_name, src_type, edge_type, dst_name, dst_type, evidence],
    )


def add_entity(conn, mid, ext_id, doc_id, entity_type, name, *, evidence=None):
    conn.execute(
        "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, entity_type, name, evidence) "
        "VALUES (?, ?, ?, 'entity', ?, ?, ?)",
        [mid, ext_id, doc_id, entity_type, name, evidence],
    )


def add_embedding(conn, node_id, vec, *, model=EMBEDDING_MODEL):
    conn.execute(
        "INSERT INTO node_embeddings (node_id, model, vec) VALUES (?, ?, ?)",
        [node_id, model, vec],
    )


def axis(i):
    """Unit vector along dimension i (cosine with another axis vec == Kronecker delta)."""
    v = [0.0] * DIM
    v[i] = 1.0
    return v


# ---------------------------------------------------------------------------
# Fake embed_fn: accepts a str or a list of str (either calling convention);
# matches a mapping key by casefold substring, mirroring the resolve.py fake.
# ---------------------------------------------------------------------------
def make_embed(mapping):
    def _pick(text):
        if text in mapping:
            return mapping[text]
        low = text.casefold()
        hits = [k for k in mapping if k.casefold() in low]
        if not hits:
            raise KeyError(f"embed fake has no vector for {text!r}")
        return mapping[max(hits, key=len)]

    def embed_fn(arg):
        if isinstance(arg, str):
            return _pick(arg)
        return [_pick(t) for t in arg]

    return embed_fn


# ---------------------------------------------------------------------------
# subgraph_around
# ---------------------------------------------------------------------------
def test_subgraph_around_returns_typed_edges():
    """One outgoing edge -> one edge record with typed src/dst and provenance."""
    conn = new_db()
    add_canonical(conn, "n1", "Method", "Foo")
    add_canonical(conn, "n2", "Problem", "Bar")
    add_alias(conn, "foo", "Method", "n1")
    add_alias(conn, "bar", "Problem", "n2")
    add_doc(conn, "d1")
    add_extraction(conn, "e1", "d1")
    add_relation(conn, "m1", "e1", "d1", src_name="foo", src_type="Method",
                 edge_type="APPLIED_TO", dst_name="bar", dst_type="Problem", evidence="the quote")

    out = tools.subgraph_around(conn, "n1")

    assert "error" not in out
    (edge,) = out["edges"]
    assert edge["edge_type"] == "APPLIED_TO"
    assert edge["doc_id"] == "d1"
    assert edge["evidence"] == "the quote"
    assert edge["src"] == {"node_id": "n1", "name": "Foo", "type": "Method"}
    assert edge["dst"] == {"node_id": "n2", "name": "Bar", "type": "Problem"}


def test_subgraph_around_caps_at_40_with_truncated_count():
    """45 edges touching the node -> 40 returned, truncated == 5."""
    conn = new_db()
    add_canonical(conn, "n1", "Method", "Hub")
    add_alias(conn, "hub", "Method", "n1")
    add_doc(conn, "d1")
    add_extraction(conn, "e1", "d1")
    for i in range(45):
        add_canonical(conn, f"p{i}", "Problem", f"P{i}")
        add_alias(conn, f"p{i}", "Problem", f"p{i}")
        add_relation(conn, f"m{i}", "e1", "d1", src_name="hub", src_type="Method",
                     edge_type="APPLIED_TO", dst_name=f"p{i}", dst_type="Problem", evidence=f"ev{i}")

    out = tools.subgraph_around(conn, "n1")

    assert len(out["edges"]) == 40
    assert out["truncated"] == 5


def test_subgraph_around_orders_evidence_bearing_first():
    """Evidence-bearing edges precede evidence-less ones, overriding doc_id order."""
    conn = new_db()
    add_canonical(conn, "n1", "Method", "Hub")
    add_canonical(conn, "n2", "Problem", "A")
    add_canonical(conn, "n3", "Problem", "B")
    add_alias(conn, "hub", "Method", "n1")
    add_alias(conn, "a", "Problem", "n2")
    add_alias(conn, "b", "Problem", "n3")
    # Evidence-less edge sits on the alphabetically-FIRST doc; the evidence-bearing
    # edge on a later doc. Evidence-first must still put the evidence edge ahead.
    add_doc(conn, "d_a")
    add_doc(conn, "d_b")
    add_extraction(conn, "ea", "d_a")
    add_extraction(conn, "eb", "d_b")
    add_relation(conn, "m_noev", "ea", "d_a", src_name="hub", src_type="Method",
                 edge_type="APPLIED_TO", dst_name="a", dst_type="Problem", evidence=None)
    add_relation(conn, "m_ev", "eb", "d_b", src_name="hub", src_type="Method",
                 edge_type="APPLIED_TO", dst_name="b", dst_type="Problem", evidence="has evidence")

    out = tools.subgraph_around(conn, "n1")

    assert len(out["edges"]) == 2
    assert out["edges"][0]["evidence"] == "has evidence"
    assert not out["edges"][1]["evidence"]


def test_subgraph_around_clamps_hops_to_one():
    """hops>1 is clamped to 1 for MVP and the clamp is documented in the output."""
    conn = new_db()
    add_canonical(conn, "n1", "Method", "Foo")
    add_canonical(conn, "n2", "Problem", "Bar")
    add_alias(conn, "foo", "Method", "n1")
    add_alias(conn, "bar", "Problem", "n2")
    add_doc(conn, "d1")
    add_extraction(conn, "e1", "d1")
    add_relation(conn, "m1", "e1", "d1", src_name="foo", src_type="Method",
                 edge_type="APPLIED_TO", dst_name="bar", dst_type="Problem", evidence="q")

    out = tools.subgraph_around(conn, "n1", hops=3)

    assert "error" not in out
    assert out["hops"] == 1


def test_subgraph_around_unknown_node_is_error():
    conn = new_db()
    out = tools.subgraph_around(conn, "does-not-exist")
    assert "error" in out


# ---------------------------------------------------------------------------
# full_text
# ---------------------------------------------------------------------------
def test_full_text_returns_title_and_body():
    conn = new_db()
    add_doc(conn, "d1", title="A Paper", full_text="body text here")

    out = tools.full_text(conn, "d1")

    assert out["doc_id"] == "d1"
    assert out["title"] == "A Paper"
    assert out["full_text"] == "body text here"


def test_full_text_caps_at_30k_with_truncated_marker():
    conn = new_db()
    big = "x" * 40_000
    add_doc(conn, "d1", title="Big", full_text=big)

    out = tools.full_text(conn, "d1")

    assert len(out["full_text"]) == 30_000
    assert out["truncated"] == 10_000


def test_full_text_unknown_doc_is_error():
    conn = new_db()
    out = tools.full_text(conn, "nope")
    assert "error" in out


# ---------------------------------------------------------------------------
# evidence_for
# ---------------------------------------------------------------------------
def test_evidence_for_collects_relation_quotes_from_either_endpoint():
    """Relation mentions where the node is src OR dst are both returned, edge_type set."""
    conn = new_db()
    add_canonical(conn, "n1", "Method", "Foo")
    add_canonical(conn, "n2", "Problem", "Bar")
    add_canonical(conn, "n3", "Method", "Baz")
    add_alias(conn, "foo", "Method", "n1")
    add_alias(conn, "bar", "Problem", "n2")
    add_alias(conn, "baz", "Method", "n3")
    add_doc(conn, "d1")
    add_extraction(conn, "e1", "d1")
    # n1 as src
    add_relation(conn, "m_src", "e1", "d1", src_name="foo", src_type="Method",
                 edge_type="APPLIED_TO", dst_name="bar", dst_type="Problem", evidence="as source")
    # n1 as dst
    add_relation(conn, "m_dst", "e1", "d1", src_name="baz", src_type="Method",
                 edge_type="IMPROVES_ON", dst_name="foo", dst_type="Method", evidence="as target")

    out = tools.evidence_for(conn, "n1")

    quotes = {(it["quote"], it["edge_type"]) for it in out["items"]}
    assert ("as source", "APPLIED_TO") in quotes
    assert ("as target", "IMPROVES_ON") in quotes


def test_evidence_for_includes_entity_mentions_with_null_edge_type():
    """An entity mention carrying evidence, resolving via alias, appears with edge_type None."""
    conn = new_db()
    add_canonical(conn, "n1", "Method", "Foo")
    add_alias(conn, "foo", "Method", "n1")
    add_doc(conn, "d1")
    add_extraction(conn, "e1", "d1")
    add_entity(conn, "en1", "e1", "d1", "Method", "foo", evidence="entity quote")

    out = tools.evidence_for(conn, "n1")

    entity_items = [it for it in out["items"] if it["quote"] == "entity quote"]
    assert entity_items, "entity mention resolving via alias must be included"
    assert entity_items[0]["edge_type"] is None


def test_evidence_for_caps_at_20_with_truncated_count():
    conn = new_db()
    add_canonical(conn, "n1", "Method", "Hub")
    add_canonical(conn, "n2", "Problem", "P")
    add_alias(conn, "hub", "Method", "n1")
    add_alias(conn, "p", "Problem", "n2")
    add_doc(conn, "d1")
    add_extraction(conn, "e1", "d1")
    for i in range(25):
        add_relation(conn, f"m{i}", "e1", "d1", src_name="hub", src_type="Method",
                     edge_type="APPLIED_TO", dst_name="p", dst_type="Problem", evidence=f"ev{i}")

    out = tools.evidence_for(conn, "n1")

    assert len(out["items"]) == 20
    assert out["truncated"] == 5


def test_evidence_for_unknown_node_is_error():
    conn = new_db()
    out = tools.evidence_for(conn, "ghost")
    assert "error" in out


# ---------------------------------------------------------------------------
# who_tried_this
# ---------------------------------------------------------------------------
def _method_problem_db(conn, *, with_edge):
    """Method 'HyDE' (mn) and Problem 'Retrieval' (pn), optionally joined by an edge."""
    add_canonical(conn, "mn", "Method", "HyDE")
    add_canonical(conn, "pn", "Problem", "Retrieval")
    add_alias(conn, "hyde", "Method", "mn")
    add_alias(conn, "retrieval", "Problem", "pn")
    add_doc(conn, "d1")
    add_extraction(conn, "e1", "d1")
    if with_edge:
        add_relation(conn, "m1", "e1", "d1", src_name="hyde", src_type="Method",
                     edge_type="APPLIED_TO", dst_name="retrieval", dst_type="Problem", evidence="ev")


def test_who_tried_this_exact_resolution():
    """Casefold-exact alias match reports via='exact' and the right node_id."""
    conn = new_db()
    _method_problem_db(conn, with_edge=False)

    out = tools.who_tried_this(conn, method_name="HyDE")

    res = out["resolution"]["method"]
    assert res["input"] == "HyDE"
    assert res["node_id"] == "mn"
    assert res["via"] == "exact"


def test_who_tried_this_embedding_fallback_is_type_scoped():
    """No exact alias -> embedding top-match >= 0.7 against Method-type embeddings only."""
    conn = new_db()
    add_canonical(conn, "mn", "Method", "HyDE")
    add_canonical(conn, "pn", "Problem", "Decoy")
    add_alias(conn, "hyde", "Method", "mn")
    add_alias(conn, "decoy", "Problem", "pn")
    # Both a Method and a Problem embedding sit at axis 0; the query embeds to axis 0.
    # A Method-name query must resolve to the Method node, never the Problem.
    add_embedding(conn, "mn", axis(0))
    add_embedding(conn, "pn", axis(0))
    embed = make_embed({"hypothetical document embeddings": axis(0)})

    out = tools.who_tried_this(conn, method_name="hypothetical document embeddings", embed_fn=embed)

    res = out["resolution"]["method"]
    assert res["node_id"] == "mn"
    assert res["via"] == "embedding"
    assert res["score"] >= 0.7


def test_who_tried_this_unresolvable_name_establishes_no_absence():
    """A name that resolves via neither path -> absence_not_established, node_id None."""
    conn = new_db()
    _method_problem_db(conn, with_edge=False)

    out = tools.who_tried_this(conn, method_name="a method that does not exist")

    assert out["resolution"]["method"]["node_id"] is None
    assert out["absence_not_established"] is True


def test_who_tried_this_resolved_pair_finds_edge():
    """Both names resolve and a relation between them exists -> it is returned."""
    conn = new_db()
    _method_problem_db(conn, with_edge=True)

    out = tools.who_tried_this(conn, method_name="HyDE", problem_name="Retrieval")

    assert out["resolution"]["method"]["node_id"] == "mn"
    assert out["resolution"]["problem"]["node_id"] == "pn"
    node_ids = {(e["src"]["node_id"], e["dst"]["node_id"]) for e in out["edges"]}
    assert ("mn", "pn") in node_ids
    assert out.get("absence_not_established") is not True


def test_who_tried_this_resolved_pair_no_edge_is_closed_world_absence():
    """Both resolve but no edge -> empty edges, closed-world note, absence IS established."""
    conn = new_db()
    _method_problem_db(conn, with_edge=False)

    out = tools.who_tried_this(conn, method_name="HyDE", problem_name="Retrieval")

    assert out["edges"] == []
    assert out.get("absence_not_established") is not True
    assert any(isinstance(v, str) and "closed" in v.casefold() for v in out.values())


def test_who_tried_this_single_name_returns_touching_edges():
    """One name given -> edges touching that node are returned."""
    conn = new_db()
    _method_problem_db(conn, with_edge=True)

    out = tools.who_tried_this(conn, method_name="HyDE")

    node_ids = {(e["src"]["node_id"], e["dst"]["node_id"]) for e in out["edges"]}
    assert ("mn", "pn") in node_ids


def test_who_tried_this_no_names_is_error():
    conn = new_db()
    out = tools.who_tried_this(conn)
    assert "error" in out


# ---------------------------------------------------------------------------
# Garbage input: every tool returns a dict with "error" and never raises.
# ---------------------------------------------------------------------------
def test_all_tools_return_error_dict_on_garbage_without_raising():
    conn = new_db()
    results = [
        tools.subgraph_around(conn, "garbage-id"),
        tools.full_text(conn, "garbage-id"),
        tools.evidence_for(conn, "garbage-id"),
        tools.who_tried_this(conn),  # no names at all
    ]
    for out in results:
        assert isinstance(out, dict)
        assert "error" in out
