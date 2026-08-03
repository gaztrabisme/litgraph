"""Executable spec for litgraph.embeddings — node context-doc embeddings.

Contract under test (fixed by coordinator):
    embeddings.context_doc(node_type, name, quotes) -> str   # pure
    embeddings.populate(conn, *, embed_fn=None) -> {"embedded": n, "skipped": n}
    embeddings.wipe(conn) -> None

populate embeds every canonical_nodes row not already in node_embeddings: it
builds a context document per node (name + type + up to 3 evidence quotes, chosen
shortest-first, ties by doc_id then quote text, from the entity-kind mentions that
alias-resolve to the node), calls embed_fn once batched, and inserts each vector
under models.EMBEDDING_MODEL. Spec: phase2-design.md C4 + "Done When" embed-nodes.

Every test injects a fake embed_fn (deterministic 1024-dim vectors) — no network.
The module does not exist yet, so these fail at import until it is built.
"""

import hashlib

from litgraph import db, embeddings
from litgraph.models import EMBEDDING_MODEL, EXTRACTION_MODEL, PROMPT_VERSION

DIM = 1024


# ---------------------------------------------------------------------------
# Fake embed_fn: deterministic 1024-dim vector per text; records each batch.
# ---------------------------------------------------------------------------
def _vec(text: str, dim: int = DIM) -> list[float]:
    out: list[float] = []
    i = 0
    while len(out) < dim:
        digest = hashlib.sha256(f"{text}:{i}".encode()).digest()
        out.extend(b / 255.0 for b in digest)
        i += 1
    return out[:dim]


def make_embed():
    """Recording embed_fn; .calls is the list of batches (each a list of texts)."""

    def embed_fn(texts):
        embed_fn.calls.append(list(texts))
        return [_vec(t) for t in texts]

    embed_fn.calls = []
    return embed_fn


# ---------------------------------------------------------------------------
# Synthetic row builders (straight into the storage contract).
# ---------------------------------------------------------------------------
def new_db():
    return db.connect(":memory:")


def add_doc(conn, doc_id):
    conn.execute(
        "INSERT INTO documents (doc_id, source_type, url, title) VALUES (?, 'paper', ?, ?)",
        [doc_id, f"http://example/{doc_id}", doc_id],
    )
    conn.execute(
        "INSERT INTO extractions (extraction_id, doc_id, prompt_version, model, status) "
        "VALUES (?, ?, ?, ?, 'ok')",
        [f"e-{doc_id}", doc_id, PROMPT_VERSION, EXTRACTION_MODEL],
    )


def add_canonical(conn, node_id, node_type, name):
    conn.execute(
        "INSERT INTO canonical_nodes (node_id, node_type, canonical_name) VALUES (?, ?, ?)",
        [node_id, node_type, name],
    )


def add_alias(conn, alias_name, node_type, node_id):
    conn.execute(
        "INSERT INTO aliases (alias_name, node_type, node_id, method, score) "
        "VALUES (?, ?, ?, 'exact', 1.0)",
        [alias_name, node_type, node_id],
    )


_mid = 0


def add_entity_mention(conn, doc_id, node_type, name, evidence):
    global _mid
    _mid += 1
    conn.execute(
        "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, entity_type, name, evidence)"
        " VALUES (?, ?, ?, 'entity', ?, ?, ?)",
        [f"m{_mid}", f"e-{doc_id}", doc_id, node_type, name, evidence],
    )


def add_entity_mention_no_evidence(conn, doc_id, node_type, name):
    """Entity mention with NULL evidence — the real-data shape (0/4651 carry evidence)."""
    global _mid
    _mid += 1
    conn.execute(
        "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, entity_type, name) "
        "VALUES (?, ?, ?, 'entity', ?, ?)",
        [f"m{_mid}", f"e-{doc_id}", doc_id, node_type, name],
    )


def add_relation_mention(conn, doc_id, src, dst, evidence, *, src_type="Method", dst_type="Method"):
    """Relation mention carrying evidence; src/dst are (name) alias-resolvable to nodes."""
    global _mid
    _mid += 1
    conn.execute(
        "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, "
        "src_name, src_type, edge_type, dst_name, dst_type, evidence) "
        "VALUES (?, ?, ?, 'relation', ?, ?, 'USES', ?, ?, ?)",
        [f"m{_mid}", f"e-{doc_id}", doc_id, src, src_type, dst, dst_type, evidence],
    )


def node(conn, node_id, name, quotes, *, node_type="Method", doc_id="d1", alias=None):
    """Canonical node + one alias + one entity mention per quote (all under doc_id)."""
    add_canonical(conn, node_id, node_type, name)
    add_alias(conn, alias or name, node_type, node_id)
    for q in quotes:
        add_entity_mention(conn, doc_id, node_type, alias or name, q)


def embeddings_rows(conn):
    return conn.execute("SELECT node_id, model, len(vec) FROM node_embeddings").fetchall()


# ---------------------------------------------------------------------------
# context_doc — pure composition
# ---------------------------------------------------------------------------
def test_context_doc_composition_exact():
    """context_doc == '{type}: {name}. ' + ' '.join(quotes)."""
    assert (
        embeddings.context_doc("Method", "HyDE", ["first quote", "second quote"])
        == "Method: HyDE. first quote second quote"
    )


def test_context_doc_no_quotes_keeps_prefix_and_space():
    """Zero quotes still yields the '{type}: {name}. ' prefix (trailing space)."""
    assert embeddings.context_doc("Problem", "Long-term memory", []) == (
        "Problem: Long-term memory. "
    )


# ---------------------------------------------------------------------------
# populate — coverage, model, dimension
# ---------------------------------------------------------------------------
def test_populate_embeds_every_canonical_including_quoteless():
    """Every canonical row is embedded once, under EMBEDDING_MODEL, 1024-dim —
    including a node with no entity mentions (relation-only endpoint)."""
    conn = new_db()
    add_doc(conn, "d1")
    node(conn, "n1", "HyDE", ["a short quote"])
    add_canonical(conn, "n2", "Method", "Bare")  # no alias, no mentions
    add_alias(conn, "Bare", "Method", "n2")
    embed = make_embed()

    report = embeddings.populate(conn, embed_fn=embed)

    assert report == {"embedded": 2, "skipped": 0}
    rows = embeddings_rows(conn)
    assert {r[0] for r in rows} == {"n1", "n2"}
    assert {r[1] for r in rows} == {EMBEDDING_MODEL}
    assert {r[2] for r in rows} == {DIM}


def test_populate_idempotent_second_run_skips_all():
    """Re-running with unchanged data embeds nothing (idempotent)."""
    conn = new_db()
    add_doc(conn, "d1")
    node(conn, "n1", "HyDE", ["q one"])
    node(conn, "n2", "RAG", ["q two"])
    embed = make_embed()

    first = embeddings.populate(conn, embed_fn=embed)
    second = embeddings.populate(conn, embed_fn=embed)

    assert first == {"embedded": 2, "skipped": 0}
    assert second == {"embedded": 0, "skipped": 2}
    assert conn.execute("SELECT count(*) FROM node_embeddings").fetchone()[0] == 2


def test_populate_embeds_only_missing_nodes():
    """A node added after the first run is the only one (re)embedded."""
    conn = new_db()
    add_doc(conn, "d1")
    node(conn, "n1", "HyDE", ["quote one"])
    embed = make_embed()
    embeddings.populate(conn, embed_fn=embed)

    node(conn, "n2", "Fusion", ["quote two"])
    report = embeddings.populate(conn, embed_fn=embed)

    assert report == {"embedded": 1, "skipped": 1}
    # the second populate batches exactly the one missing node's context doc
    assert embed.calls[-1] == ["Method: Fusion. quote two"]
    assert {r[0] for r in embeddings_rows(conn)} == {"n1", "n2"}


def test_populate_uses_at_most_three_quotes_shortest_first():
    """Context doc uses <=3 quotes, shortest first, ties by doc_id then quote text."""
    conn = new_db()
    add_doc(conn, "da")
    add_doc(conn, "db")
    add_canonical(conn, "n1", "Method", "Foo")
    add_alias(conn, "Foo", "Method", "n1")
    # lengths: "aa"/"cc" == 2 (tie), "xxx" == 3, "wwww" == 4, "yyyyy" == 5.
    # tie at len 2 broken by doc_id: da before db -> "aa" then "cc".
    add_entity_mention(conn, "db", "Method", "Foo", "cc")
    add_entity_mention(conn, "da", "Method", "Foo", "aa")
    add_entity_mention(conn, "da", "Method", "Foo", "xxx")
    add_entity_mention(conn, "da", "Method", "Foo", "wwww")
    add_entity_mention(conn, "da", "Method", "Foo", "yyyyy")
    embed = make_embed()

    embeddings.populate(conn, embed_fn=embed)

    assert embed.calls == [["Method: Foo. aa cc xxx"]]


def test_populate_pulls_quotes_via_alias_resolution():
    """Quotes come from entity mentions matched by alias; the canonical name is used."""
    conn = new_db()
    add_doc(conn, "d1")
    add_canonical(conn, "n1", "Method", "HyDE")
    add_alias(conn, "hyde", "Method", "n1")  # alias differs in case from canonical
    add_entity_mention(conn, "d1", "Method", "hyde", "the evidence quote")
    embed = make_embed()

    embeddings.populate(conn, embed_fn=embed)

    assert embed.calls == [["Method: HyDE. the evidence quote"]]


def test_populate_pulls_quotes_from_relation_mention_evidence():
    """Union rule: a node whose ONLY evidence lives on a relation mention (its entity
    mention has NULL evidence) still gets that relation quote in its context doc.
    Evidence is drawn from relation mentions where EITHER endpoint resolves to the node."""
    conn = new_db()
    add_doc(conn, "d1")
    # HyDE (src endpoint) and a dst node so the relation is well-formed.
    add_canonical(conn, "n1", "Method", "HyDE")
    add_alias(conn, "HyDE", "Method", "n1")
    add_canonical(conn, "n2", "Method", "Retrieval")
    add_alias(conn, "Retrieval", "Method", "n2")
    add_entity_mention_no_evidence(conn, "d1", "Method", "HyDE")  # present but no evidence
    add_relation_mention(conn, "d1", "HyDE", "Retrieval", "HyDE augments retrieval")
    embed = make_embed()

    embeddings.populate(conn, embed_fn=embed)

    docs = {d for batch in embed.calls for d in batch}
    assert "Method: HyDE. HyDE augments retrieval" in docs, "src endpoint gets the relation quote"
    assert "Method: Retrieval. HyDE augments retrieval" in docs, "dst endpoint gets it too"


def test_wipe_clears_embeddings():
    """wipe removes all node_embeddings rows."""
    conn = new_db()
    add_doc(conn, "d1")
    node(conn, "n1", "HyDE", ["q"])
    embeddings.populate(conn, embed_fn=make_embed())
    assert conn.execute("SELECT count(*) FROM node_embeddings").fetchone()[0] == 1

    embeddings.wipe(conn)

    assert conn.execute("SELECT count(*) FROM node_embeddings").fetchone()[0] == 0
