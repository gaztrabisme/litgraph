"""Executable spec for litgraph.resolve — the entity-resolution pipeline.

Contract under test (fixed by coordinator):
    litgraph.resolve.run(conn, *, embed_fn=None, adjudicate_fn=None) -> dict
    litgraph.resolve.mentions_per_canonical(conn) -> float

Every test injects fake embed_fn / adjudicate_fn — no network, no LLM. Cosines
are forced by hand-picked unit vectors (dot product == cosine). Spec references:
phase1-design.md "ER pipeline" + Runtime Semantics C2 (name universe), W10
(self-wipe), W13 (mentions_per_canonical). The module does not exist yet, so
these are expected to fail at import (ModuleNotFoundError) until it is built.
"""

import math

import pytest

from litgraph import db, resolve
from litgraph.models import EXTRACTION_MODEL, PROMPT_VERSION

# ---------------------------------------------------------------------------
# Hand-picked unit vectors: cosine with V_REF == first component (both unit).
# ---------------------------------------------------------------------------
V_REF = [1.0, 0.0]
V_080 = [0.8, 0.6]                                 # cos(V_REF, ·) == 0.80 exactly
V_075 = [0.75, math.sqrt(1.0 - 0.75**2)]           # cos == 0.75 exactly (band)
V_060 = [0.6, 0.8]                                 # cos == 0.60 (below floor)
V_ORTH = [0.0, 1.0]                                # cos == 0.0


# ---------------------------------------------------------------------------
# Fakes (injected — no monkeypatching).
# ---------------------------------------------------------------------------
def make_embed(mapping):
    """Return an embed_fn keyed by name content, robust to a type prefix.

    resolve may embed bare names or names with a type prefix ("Method: HyDE").
    We match a mapping key by (case-insensitive) substring so either shape
    resolves to the intended vector. All test names are chosen to be mutually
    non-substring so matches are unambiguous.
    """

    def embed_fn(texts):
        out = []
        for text in texts:
            if text in mapping:
                out.append(mapping[text])
                continue
            low = text.casefold()
            hits = [k for k in mapping if k.casefold() in low]
            if not hits:
                raise KeyError(f"embed fake has no vector for {text!r}")
            out.append(mapping[max(hits, key=len)])
        return out

    return embed_fn


class FakeAdjudicator:
    """Records every call; returns a fixed bool (or a callable's verdict)."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def __call__(self, name_a, name_b, node_type):
        self.calls.append((name_a, name_b, node_type))
        if callable(self._result):
            return self._result(name_a, name_b, node_type)
        return self._result


def never_adjudicate(*args, **kwargs):
    raise AssertionError("adjudicate_fn must not be called for this input")


# ---------------------------------------------------------------------------
# Fixture builder: insert documents/extractions/mentions rows directly.
# ---------------------------------------------------------------------------
def seed(conn, *, entities=(), relations=(), doc_id="d1"):
    """entities: (name, entity_type). relations: (src, src_type, edge, dst, dst_type)."""
    conn.execute(
        "INSERT INTO documents (doc_id, source_type, url, title) VALUES (?, 'paper', ?, ?)",
        [doc_id, f"http://example/{doc_id}", doc_id],
    )
    ext_id = f"e-{doc_id}"
    conn.execute(
        "INSERT INTO extractions (extraction_id, doc_id, prompt_version, model, status) "
        "VALUES (?, ?, ?, ?, 'ok')",
        [ext_id, doc_id, PROMPT_VERSION, EXTRACTION_MODEL],
    )
    for i, (name, etype) in enumerate(entities):
        conn.execute(
            "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, entity_type, name) "
            "VALUES (?, ?, ?, 'entity', ?, ?)",
            [f"m-{doc_id}-{i}", ext_id, doc_id, etype, name],
        )
    for j, (sn, st, et, dn, dt) in enumerate(relations):
        conn.execute(
            "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, "
            "src_name, src_type, edge_type, dst_name, dst_type, evidence) "
            "VALUES (?, ?, ?, 'relation', ?, ?, ?, ?, ?, 'ev')",
            [f"r-{doc_id}-{j}", ext_id, doc_id, sn, st, et, dn, dt],
        )
    return conn


def new_db():
    return db.connect(":memory:")


def counts(conn):
    cn = conn.execute("SELECT count(*) FROM canonical_nodes").fetchone()[0]
    al = conn.execute("SELECT count(*) FROM aliases").fetchone()[0]
    return cn, al


def alias_rows(conn):
    return conn.execute(
        "SELECT alias_name, node_type, node_id, method, score FROM aliases"
    ).fetchall()


def canonical_names(conn):
    return conn.execute("SELECT node_type, canonical_name FROM canonical_nodes").fetchall()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_resolve_exact_merge_casefold_whitespace():
    """Names equal after casefold+strip merge into one canonical, method='exact'."""
    conn = new_db()
    seed(conn, entities=[("HyDE", "Method"), ("hyde ", "Method")])
    embed = make_embed({"HyDE": V_REF, "hyde ": V_REF})

    report = resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)

    cn, al = counts(conn)
    assert cn == 1, "casefold/whitespace-equal names must collapse to one canonical"
    assert al == 2, "both distinct raw names remain as alias rows"
    assert {r[3] for r in alias_rows(conn)} == {"exact"}
    assert {"canonical_nodes", "aliases", "mentions_per_canonical"} <= report.keys()


def test_resolve_embedding_merge_at_080_boundary():
    """cos == 0.80 merges via method='embedding' (score==cos); adjudicator NOT called."""
    conn = new_db()
    seed(conn, entities=[("Falcon", "Method"), ("Sparrow", "Method")])
    embed = make_embed({"Falcon": V_REF, "Sparrow": V_080})

    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)  # no exception == not called

    cn, al = counts(conn)
    assert cn == 1, "cos>=0.80 is an embedding merge"
    assert al == 2
    emb = [r for r in alias_rows(conn) if r[3] == "embedding"]
    assert emb, "the joining alias must be recorded with method='embedding'"
    assert emb[0][4] == pytest.approx(0.80, abs=1e-9), "embedding score is the cosine"


def test_resolve_borderline_band_calls_adjudicator():
    """0.70<=cos<0.80: adjudicator decides. True->merge method='llm'; False->separate."""
    conn_yes = new_db()
    seed(conn_yes, entities=[("Otter", "Method"), ("Beaver", "Method")])
    embed = make_embed({"Otter": V_REF, "Beaver": V_075})
    adj_yes = FakeAdjudicator(True)

    resolve.run(conn_yes, embed_fn=embed, adjudicate_fn=adj_yes)

    assert adj_yes.calls, "cos in [0.70,0.80) must consult the adjudicator"
    assert {c[2] for c in adj_yes.calls} == {"Method"}
    assert {c[0] for c in adj_yes.calls} | {c[1] for c in adj_yes.calls} == {"Otter", "Beaver"}
    cn_yes, _ = counts(conn_yes)
    assert cn_yes == 1, "adjudicator True -> merged"
    assert "llm" in {r[3] for r in alias_rows(conn_yes)}

    conn_no = new_db()
    seed(conn_no, entities=[("Otter", "Method"), ("Beaver", "Method")])
    adj_no = FakeAdjudicator(False)
    resolve.run(conn_no, embed_fn=make_embed({"Otter": V_REF, "Beaver": V_075}), adjudicate_fn=adj_no)
    assert adj_no.calls, "adjudicator is still consulted when it will say no"
    cn_no, _ = counts(conn_no)
    assert cn_no == 2, "adjudicator False -> two separate canonicals"


def test_resolve_below_070_never_adjudicated():
    """cos < 0.70: never merged and the adjudicator is never called."""
    conn = new_db()
    seed(conn, entities=[("Walrus", "Method"), ("Penguin", "Method")])
    embed = make_embed({"Walrus": V_REF, "Penguin": V_060})

    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)  # raises if called

    cn, al = counts(conn)
    assert cn == 2, "below the 0.70 floor the names stay distinct"
    assert al == 2


def test_resolve_no_cross_type_merge():
    """Same name under two types never merges — even with identical embeddings."""
    conn = new_db()
    seed(conn, entities=[("Memory", "Method"), ("Memory", "Problem")])
    embed = make_embed({"Memory": V_REF})  # identical vector for both

    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)

    cn, al = counts(conn)
    assert cn == 2, "merging is within-type only"
    assert al == 2
    assert {r[0] for r in canonical_names(conn)} == {"Method", "Problem"}


def test_resolve_relation_endpoints_get_aliases():
    """C2: relation endpoints with no entity mention still yield alias + canonical rows."""
    conn = new_db()
    seed(conn, relations=[("Transformer", "Method", "USES", "Attention", "Method")])
    embed = make_embed({"Transformer": V_REF, "Attention": V_ORTH})

    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)

    cn, al = counts(conn)
    assert cn == 2, "each relation endpoint (name,type) becomes a canonical node"
    assert al == 2
    assert {r[0] for r in alias_rows(conn)} == {"Transformer", "Attention"}


def test_resolve_canonical_name_is_most_frequent_mention():
    """canonical_name == the cluster's most frequent raw mention name."""
    conn = new_db()
    # 'GPT' x3 rows, 'gpt' x1 row — exact-merge cluster; majority raw name is 'GPT'.
    seed(conn, entities=[("GPT", "Method"), ("GPT", "Method"), ("GPT", "Method"), ("gpt", "Method")])
    embed = make_embed({"GPT": V_REF})

    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)

    cn, al = counts(conn)
    assert cn == 1
    assert al == 2, "both raw spellings are aliases of the one canonical"
    assert canonical_names(conn) == [("Method", "GPT")]


def test_resolve_transitive_union_closure():
    """A~B exact and B~C embedding collapse to a single canonical of three aliases."""
    conn = new_db()
    seed(conn, entities=[("HyDE", "Method"), ("Hyde", "Method"), ("Sparrow", "Method")])
    embed = make_embed({"HyDE": V_REF, "Hyde": V_REF, "Sparrow": V_080})  # cos(ref,Sparrow)=0.80

    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)

    cn, al = counts(conn)
    assert cn == 1, "union-find closes {HyDE,Hyde} (exact) with Sparrow (embedding)"
    assert al == 3, "all three distinct names alias the single canonical"


def test_resolve_rerun_self_wipes():
    """W10: resolve self-wipes first; a second run is idempotent (no PK/FK errors)."""
    conn = new_db()
    seed(conn, entities=[("Alpha", "Method"), ("Alpha", "Method"), ("Beta", "Method")])
    embed = make_embed({"Alpha": V_REF, "Beta": V_ORTH})

    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)
    first = counts(conn)
    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)  # must not raise
    second = counts(conn)

    assert first == second, "re-running yields identical row counts"
    assert first[0] == 2 and first[1] == 2


def test_mentions_per_canonical_formula():
    """W13: entity-mention rows / canonical_nodes — exact float on a known fixture."""
    conn = new_db()
    # 4 entity rows (Alpha x3, Beta x1), no merges -> 2 canonicals -> 4/2 == 2.0
    seed(conn, entities=[("Alpha", "Method"), ("Alpha", "Method"), ("Alpha", "Method"),
                         ("Beta", "Method")])
    embed = make_embed({"Alpha": V_REF, "Beta": V_ORTH})

    report = resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)

    assert resolve.mentions_per_canonical(conn) == pytest.approx(2.0)
    assert report["mentions_per_canonical"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Phase 2 additions: C1 deterministic node_id + C2 derived-table self-wipe.
# (resolve.py already implements both — these tests must PASS, unlike the
# module-missing tests in test_embeddings.py / test_communities.py.)
# ---------------------------------------------------------------------------
def node_id_of(conn, alias_name, node_type):
    row = conn.execute(
        "SELECT node_id FROM aliases WHERE alias_name = ? AND node_type = ?",
        [alias_name, node_type],
    ).fetchone()
    return row[0] if row else None


def test_C1_node_id_stable_across_reresolve():
    """C1: identical mentions -> identical node_ids across two resolve runs."""
    conn = new_db()
    seed(conn, entities=[("Alpha", "Method"), ("Gamma", "Problem")])
    embed = make_embed({"Alpha": V_REF, "Gamma": V_ORTH})

    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)
    first = {node_id_of(conn, "Alpha", "Method"), node_id_of(conn, "Gamma", "Problem")}
    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)
    second = {node_id_of(conn, "Alpha", "Method"), node_id_of(conn, "Gamma", "Problem")}

    assert None not in first
    assert first == second, "unchanged clusters keep their node_id across re-resolve"


def test_C1_membership_change_mints_new_id_others_stable():
    """C1: a cluster whose membership changes gets a new node_id; untouched clusters don't."""
    conn = new_db()
    seed(conn, entities=[("Alpha", "Method"), ("Gamma", "Problem")], doc_id="d1")
    embed = make_embed({"Alpha": V_REF, "alpha": V_REF, "Gamma": V_ORTH})

    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)
    id_alpha_1 = node_id_of(conn, "Alpha", "Method")
    id_gamma_1 = node_id_of(conn, "Gamma", "Problem")

    # a new exact-variant name ("alpha") merges into Alpha's cluster; Gamma untouched.
    seed(conn, entities=[("alpha", "Method")], doc_id="d2")
    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)
    id_alpha_2 = node_id_of(conn, "Alpha", "Method")
    id_gamma_2 = node_id_of(conn, "Gamma", "Problem")

    assert id_alpha_2 != id_alpha_1, "changed cluster membership mints a new node_id"
    assert id_gamma_2 == id_gamma_1, "untouched cluster keeps its node_id"
    assert node_id_of(conn, "alpha", "Method") == id_alpha_2, "both variants share the node"


def test_C2_resolve_wipes_embeddings_and_communities():
    """C2: resolve self-wipe extends to node_embeddings + communities."""
    conn = new_db()
    seed(conn, entities=[("Alpha", "Method")])
    embed = make_embed({"Alpha": V_REF})
    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)
    node_id = node_id_of(conn, "Alpha", "Method")

    conn.execute(
        "INSERT INTO node_embeddings (node_id, model, vec) VALUES (?, 'm', ?)",
        [node_id, [0.0] * 1024],
    )
    conn.execute(
        "INSERT INTO communities (node_id, community_id, algo, seed) "
        "VALUES (?, 0, 'louvain', 42)",
        [node_id],
    )
    assert conn.execute("SELECT count(*) FROM node_embeddings").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM communities").fetchone()[0] == 1

    resolve.run(conn, embed_fn=embed, adjudicate_fn=never_adjudicate)

    assert conn.execute("SELECT count(*) FROM node_embeddings").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM communities").fetchone()[0] == 0
