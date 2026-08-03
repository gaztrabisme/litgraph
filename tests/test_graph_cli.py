"""Contract tests for litgraph.graph.build and litgraph.cli.main.

Executable specification for the Phase 1 graph rebuild + CLI surface. Modules
under test do not exist yet; collection fails with ModuleNotFoundError until the
implementation agents land graph.py and cli.py. Fixtures are the smallest DB
state that forces each contract clause — no mocks, pure DuckDB + argparse.

Contract sources: wiki/phase1-design.md ("Data model" Key decision, Runtime
Semantics W11/W13) and the coordinator-fixed interfaces:
  graph.build(conn, *, as_of=None) -> networkx.MultiDiGraph
  cli.main(argv) -> int   # global --db, subcommands ingest/extract/resolve/graph/stats
"""

import datetime
import json

import pytest

from litgraph import cli, db, graph
from litgraph.models import EXTRACTION_MODEL, PROMPT_VERSION

# --- row builders (synthetic rows straight into the storage contract) ---------


def _add_doc(conn, doc_id, published_date, *, source_type="paper"):
    conn.execute(
        "INSERT INTO documents (doc_id, source_type, url, title, published_date) "
        "VALUES (?, ?, ?, ?, ?)",
        [doc_id, source_type, f"http://example/{doc_id}", doc_id, published_date],
    )


def _add_extraction(conn, extraction_id, doc_id, *, status="ok"):
    conn.execute(
        "INSERT INTO extractions (extraction_id, doc_id, prompt_version, model, status) "
        "VALUES (?, ?, ?, ?, ?)",
        [extraction_id, doc_id, PROMPT_VERSION, EXTRACTION_MODEL, status],
    )


def _add_canonical(conn, node_id, node_type, canonical_name):
    conn.execute(
        "INSERT INTO canonical_nodes (node_id, node_type, canonical_name) VALUES (?, ?, ?)",
        [node_id, node_type, canonical_name],
    )


def _add_alias(conn, alias_name, node_type, node_id, *, method="exact", score=1.0):
    conn.execute(
        "INSERT INTO aliases (alias_name, node_type, node_id, method, score) "
        "VALUES (?, ?, ?, ?, ?)",
        [alias_name, node_type, node_id, method, score],
    )


def _add_entity_mention(conn, mention_id, extraction_id, doc_id, entity_type, name):
    conn.execute(
        "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, entity_type, name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [mention_id, extraction_id, doc_id, "entity", entity_type, name],
    )


def _add_relation_mention(
    conn,
    mention_id,
    extraction_id,
    doc_id,
    *,
    src_name,
    src_type,
    edge_type,
    dst_name,
    dst_type,
    evidence="ev",
):
    conn.execute(
        "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, "
        "src_name, src_type, dst_name, dst_type, edge_type, evidence) "
        "VALUES (?, ?, ?, 'relation', ?, ?, ?, ?, ?, ?)",
        [
            mention_id,
            extraction_id,
            doc_id,
            src_name,
            src_type,
            dst_name,
            dst_type,
            edge_type,
            evidence,
        ],
    )


def _two_nodes(conn):
    """Canonical Method 'Foo' (n1) and Problem 'Bar' (n2) with lowercase aliases."""
    _add_canonical(conn, "n1", "Method", "Foo")
    _add_canonical(conn, "n2", "Problem", "Bar")
    _add_alias(conn, "foo", "Method", "n1")
    _add_alias(conn, "bar", "Problem", "n2")


def _relation(conn, mention_id, extraction_id, doc_id, edge_type="USES", evidence="ev"):
    """A foo->bar relation mention resolvable through the _two_nodes aliases."""
    _add_relation_mention(
        conn,
        mention_id,
        extraction_id,
        doc_id,
        src_name="foo",
        src_type="Method",
        edge_type=edge_type,
        dst_name="bar",
        dst_type="Problem",
        evidence=evidence,
    )


# --- graph.build ---------------------------------------------------------------


def test_graph_build_joins_relations_through_aliases():
    """One relation whose endpoints resolve via aliases -> exactly one edge with
    all attrs (edge_type, doc_id, published_date, evidence) and typed nodes."""
    conn = db.connect(":memory:")
    _two_nodes(conn)
    _add_doc(conn, "d1", datetime.date(2020, 1, 1))
    _add_extraction(conn, "e1", "d1")
    _relation(conn, "m1", "e1", "d1", edge_type="USES", evidence="the quote")

    g = graph.build(conn)

    assert g.number_of_edges() == 1
    assert g.nodes["n1"]["node_type"] == "Method"
    assert g.nodes["n1"]["name"] == "Foo"
    assert g.nodes["n2"]["node_type"] == "Problem"
    assert g.has_edge("n1", "n2")
    (attrs,) = g.get_edge_data("n1", "n2").values()
    assert attrs["edge_type"] == "USES"
    assert attrs["doc_id"] == "d1"
    assert attrs["published_date"] == datetime.date(2020, 1, 1)
    assert attrs["evidence"] == "the quote"


def test_graph_as_of_excludes_later_docs_but_includes_on_date():
    """W11 cutoff: a doc published after --as-of contributes zero edges; a doc
    published exactly ON the cutoff date is INCLUDED (<=, boundary)."""
    conn = db.connect(":memory:")
    _two_nodes(conn)
    _add_doc(conn, "d_after", datetime.date(2021, 6, 1))
    _add_doc(conn, "d_on", datetime.date(2021, 1, 1))
    _add_extraction(conn, "e_after", "d_after")
    _add_extraction(conn, "e_on", "d_on")
    _relation(conn, "m_after", "e_after", "d_after")
    _relation(conn, "m_on", "e_on", "d_on")

    g = graph.build(conn, as_of=datetime.date(2021, 1, 1))

    assert g.number_of_edges() == 1
    (attrs,) = g.get_edge_data("n1", "n2").values()
    assert attrs["doc_id"] == "d_on"  # on-cutoff kept, later dropped


def test_graph_as_of_excludes_null_dates():
    """W11: NULL-dated docs are excluded from any --as-of graph."""
    conn = db.connect(":memory:")
    _two_nodes(conn)
    _add_doc(conn, "d_null", None)
    _add_extraction(conn, "e_null", "d_null")
    _relation(conn, "m_null", "e_null", "d_null")

    g = graph.build(conn, as_of=datetime.date(2021, 1, 1))

    assert g.number_of_edges() == 0


def test_graph_full_includes_null_dates():
    """W11: without --as-of, NULL-dated docs DO contribute to the full graph."""
    conn = db.connect(":memory:")
    _two_nodes(conn)
    _add_doc(conn, "d_null", None)
    _add_extraction(conn, "e_null", "d_null")
    _relation(conn, "m_null", "e_null", "d_null")

    g = graph.build(conn)

    assert g.number_of_edges() == 1
    (attrs,) = g.get_edge_data("n1", "n2").values()
    assert attrs["doc_id"] == "d_null"
    assert attrs["published_date"] is None


def test_graph_unresolvable_endpoint_drops_edge_without_crash():
    """A relation whose src OR dst has no alias row is dropped, not fatal."""
    conn = db.connect(":memory:")
    # Only the src side is resolvable; dst 'bar' has no alias.
    _add_canonical(conn, "n1", "Method", "Foo")
    _add_alias(conn, "foo", "Method", "n1")
    _add_doc(conn, "d1", datetime.date(2020, 1, 1))
    _add_extraction(conn, "e1", "d1")
    _relation(conn, "m1", "e1", "d1")

    g = graph.build(conn)  # must not raise

    assert g.number_of_edges() == 0


def test_graph_multiedges_preserved():
    """Two docs asserting the same src/dst/edge_type -> two parallel edges
    (MultiDiGraph keeps per-doc provenance, edges never deduped canonically)."""
    conn = db.connect(":memory:")
    _two_nodes(conn)
    _add_doc(conn, "d1", datetime.date(2020, 1, 1))
    _add_doc(conn, "d2", datetime.date(2021, 1, 1))
    _add_extraction(conn, "e1", "d1")
    _add_extraction(conn, "e2", "d2")
    _relation(conn, "m1", "e1", "d1", edge_type="USES")
    _relation(conn, "m2", "e2", "d2", edge_type="USES")

    g = graph.build(conn)

    assert g.number_of_edges() == 2
    assert len(g.get_edge_data("n1", "n2")) == 2
    assert {a["doc_id"] for a in g.get_edge_data("n1", "n2").values()} == {"d1", "d2"}


# --- cli.main ------------------------------------------------------------------

STATS_KEYS = {
    "documents",
    "mentions",
    "canonical_nodes",
    "edges",
    "mentions_per_canonical",
    "null_published_dates",
    "extraction_status",
}


def _seed_file_db(path, populate):
    """Create a file-backed DB, run populate(conn), and close so the CLI can
    reopen it (single-writer)."""
    conn = db.connect(str(path))
    populate(conn)
    conn.close()


def test_cli_stats_outputs_valid_json_with_required_keys(tmp_path, capsys):
    """`stats` prints exactly one json.loads-able object with the fixed key set."""
    dbfile = tmp_path / "s.duckdb"

    def populate(conn):
        _two_nodes(conn)
        _add_doc(conn, "d1", datetime.date(2020, 1, 1))
        _add_extraction(conn, "e1", "d1")
        _add_entity_mention(conn, "en1", "e1", "d1", "Method", "Foo")
        _relation(conn, "m1", "e1", "d1")

    _seed_file_db(dbfile, populate)

    rc = cli.main(["--db", str(dbfile), "stats"])
    assert rc == 0

    obj = json.loads(capsys.readouterr().out)
    assert set(obj) == STATS_KEYS
    assert obj["documents"] == {"paper": 1}
    assert obj["extraction_status"] == {"ok": 1}
    assert obj["edges"] == {"USES": 1}
    assert obj["null_published_dates"] == 0


def test_cli_stats_mentions_per_canonical_matches_formula(tmp_path, capsys):
    """W13: mentions_per_canonical = entity-kind mentions / canonical_nodes.
    Fixture 3 entity mentions over 2 canonicals -> exactly 1.5."""
    dbfile = tmp_path / "mpc.duckdb"

    def populate(conn):
        _add_canonical(conn, "n1", "Method", "Foo")
        _add_canonical(conn, "n2", "Problem", "Bar")
        _add_doc(conn, "d1", datetime.date(2020, 1, 1))
        _add_extraction(conn, "e1", "d1")
        _add_entity_mention(conn, "en1", "e1", "d1", "Method", "Foo")
        _add_entity_mention(conn, "en2", "e1", "d1", "Method", "Foo")
        _add_entity_mention(conn, "en3", "e1", "d1", "Problem", "Bar")

    _seed_file_db(dbfile, populate)

    cli.main(["--db", str(dbfile), "stats"])
    obj = json.loads(capsys.readouterr().out)

    assert obj["mentions_per_canonical"] == 1.5


def test_cli_graph_prints_counts_and_honors_as_of(tmp_path, capsys):
    """`graph` prints {"nodes", "edges"}; --as-of drops later-dated edges while
    the node count (all canonicals) is unaffected."""
    dbfile = tmp_path / "g.duckdb"

    def populate(conn):
        _two_nodes(conn)
        _add_doc(conn, "d1", datetime.date(2021, 6, 1))
        _add_extraction(conn, "e1", "d1")
        _relation(conn, "m1", "e1", "d1")

    _seed_file_db(dbfile, populate)

    assert cli.main(["--db", str(dbfile), "graph"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert full == {"nodes": 2, "edges": 1}

    assert cli.main(["--db", str(dbfile), "graph", "--as-of", "2021-01-01"]) == 0
    filtered = json.loads(capsys.readouterr().out)
    assert filtered["nodes"] == 2
    assert filtered["edges"] == 0


def test_cli_db_option_respected(tmp_path, capsys):
    """--db selects the target file: two DBs with different contents yield
    different stats."""
    db_one = tmp_path / "one.duckdb"
    db_two = tmp_path / "two.duckdb"

    def one(conn):
        _add_doc(conn, "d1", datetime.date(2020, 1, 1))

    def two(conn):
        _add_doc(conn, "d1", datetime.date(2020, 1, 1))
        _add_doc(conn, "d2", datetime.date(2020, 1, 2), source_type="blog")

    _seed_file_db(db_one, one)
    _seed_file_db(db_two, two)

    cli.main(["--db", str(db_one), "stats"])
    obj_one = json.loads(capsys.readouterr().out)
    cli.main(["--db", str(db_two), "stats"])
    obj_two = json.loads(capsys.readouterr().out)

    assert sum(obj_one["documents"].values()) == 1
    assert sum(obj_two["documents"].values()) == 2
    assert obj_one["documents"] != obj_two["documents"]


@pytest.mark.parametrize(
    "argv",
    [
        ["ingest", "--help"],
        ["ingest", "arxiv", "--help"],
        ["ingest", "seeds", "--help"],
        ["extract", "--help"],
        ["resolve", "--help"],
        ["graph", "--help"],
        ["stats", "--help"],
    ],
)
def test_cli_parser_knows_all_subcommands(argv):
    """Every subcommand (incl. ingest's arxiv/seeds) parses; --help exits 0."""
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0
