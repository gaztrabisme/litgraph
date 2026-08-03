"""Contract tests for litgraph.extract.run — executable spec for the extraction pass.

Interface under test (fixed by coordinator):
    litgraph.extract.run(conn, *, workers: int = 8) -> dict

The module does not exist yet; these tests are expected to fail at import
(ModuleNotFoundError on litgraph.extract) until it is implemented.

Load-bearing clauses (wiki/phase1-design.md "Runtime Semantics"):
  C1  — worker threads do the LLM call only; ALL DuckDB writes on the main thread.
  C3  — "un-extracted" = no ('ok','ok_empty') row for the prompt_version; on retry,
        prior error rows for (doc_id, prompt_version) are deleted before insert.
  W4  — schema-valid-but-empty extraction gets status 'ok_empty' (not 'ok').
  W12 — extraction calls use max_tokens=16384.
  Relation dedup — identical (doc_id, src_name, src_type, edge_type, dst_name,
        dst_type) relation-mentions are inserted once per extraction.

The LLM is reached strictly through the module attribute `litgraph.llm.chat_json`;
every test monkeypatches that attribute and stubs nothing else.
"""

import json
import threading
import time

from litgraph import db, extract, llm, models


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _conn():
    return db.connect(":memory:")


def _insert_doc(conn, doc_id="doc1", *, full_text="Some paper body.",
                source_type="paper", url=None, title=None):
    conn.execute(
        "INSERT INTO documents (doc_id, source_type, url, title, full_text) "
        "VALUES (?, ?, ?, ?, ?)",
        [doc_id, source_type, url or f"http://x/{doc_id}", title or doc_id, full_text],
    )


def _extraction_rows(conn, doc_id):
    return conn.execute(
        "SELECT extraction_id, status, raw_output FROM extractions WHERE doc_id = ?",
        [doc_id],
    ).fetchall()


def _mentions(conn, doc_id, kind=None):
    sql = "SELECT * FROM mentions WHERE doc_id = ?"
    params = [doc_id]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fake_returning(result, recorder=None, sleep=0.0):
    """A chat_json stub that returns a fixed dict (optionally recording kwargs)."""
    def fake(**kwargs):
        if recorder is not None:
            recorder(kwargs)
        if sleep:
            time.sleep(sleep)
        return result
    return fake


def _fake_raising(exc, recorder=None):
    def fake(**kwargs):
        if recorder is not None:
            recorder(kwargs)
        raise exc
    return fake


_ENT_METHOD = {"type": "Method", "name": "MemGPT"}
_ENT_PROBLEM = {"type": "Problem", "name": "long-term memory"}
_REL = {
    "src_name": "MemGPT", "src_type": "Method",
    "edge_type": "APPLIED_TO",
    "dst_name": "long-term memory", "dst_type": "Problem",
    "evidence": "MemGPT is applied to long-term conversational memory.",
}


# --------------------------------------------------------------------------- #
# Happy path: rows + mentions + call parameters
# --------------------------------------------------------------------------- #

def test_extract_ok_row_and_mentions_written(monkeypatch):
    """1 doc, 2 entities, 1 relation → one 'ok' extraction + 3 mention rows, exact fields."""
    conn = _conn()
    _insert_doc(conn, "doc1", full_text="body")
    result = {"entities": [_ENT_METHOD, _ENT_PROBLEM], "relations": [_REL]}

    captured = {}
    monkeypatch.setattr(llm, "chat_json",
                        _fake_returning(result, recorder=lambda k: captured.update(k)))

    ret = extract.run(conn, workers=1)

    # Return tally.
    assert ret == {"docs": 1, "ok": 1, "ok_empty": 0, "schema_error": 0, "llm_error": 0}

    # One extraction row, status ok, raw_output stores the returned JSON.
    rows = _extraction_rows(conn, "doc1")
    assert len(rows) == 1
    extraction_id, status, raw_output = rows[0]
    assert status == "ok"
    assert raw_output is not None
    assert json.loads(raw_output) == result

    # Mentions: 2 entity rows + 1 relation row, all tied to this extraction/doc.
    ents = _mentions(conn, "doc1", "entity")
    rels = _mentions(conn, "doc1", "relation")
    assert len(ents) == 2 and len(rels) == 1
    assert {(e["entity_type"], e["name"]) for e in ents} == {
        ("Method", "MemGPT"), ("Problem", "long-term memory")}
    r = rels[0]
    assert (r["src_name"], r["src_type"], r["edge_type"], r["dst_name"], r["dst_type"]) == \
        ("MemGPT", "Method", "APPLIED_TO", "long-term memory", "Problem")
    assert r["evidence"] == _REL["evidence"]
    for m in ents + rels:
        assert m["extraction_id"] == extraction_id
        assert m["doc_id"] == "doc1"

    # Call parameters: model, schema, prompt_version, and W12 budget.
    assert captured["model"] == models.EXTRACTION_MODEL
    assert captured["prompt_version"] == models.PROMPT_VERSION
    assert captured["max_tokens"] == 16384
    assert captured["schema"] == models.extraction_json_schema()


# --------------------------------------------------------------------------- #
# W4 — schema-valid but empty
# --------------------------------------------------------------------------- #

def test_extract_ok_empty_status_for_zero_entities(monkeypatch):
    """A valid extraction with zero entities → status 'ok_empty', no mentions (W4)."""
    conn = _conn()
    _insert_doc(conn, "doc1")
    monkeypatch.setattr(llm, "chat_json",
                        _fake_returning({"entities": [], "relations": []}))

    ret = extract.run(conn, workers=1)

    assert ret == {"docs": 1, "ok": 0, "ok_empty": 1, "schema_error": 0, "llm_error": 0}
    rows = _extraction_rows(conn, "doc1")
    assert len(rows) == 1 and rows[0][1] == "ok_empty"
    assert _mentions(conn, "doc1") == []


# --------------------------------------------------------------------------- #
# schema_error — invalid output validated against the ontology
# --------------------------------------------------------------------------- #

def test_extract_schema_error_on_invalid_output(monkeypatch):
    """Bad enum value fails Extraction.model_validate → 'schema_error', no mentions."""
    conn = _conn()
    _insert_doc(conn, "doc1")
    bad = {"entities": [{"type": "NotAType", "name": "X"}], "relations": []}
    monkeypatch.setattr(llm, "chat_json", _fake_returning(bad))

    ret = extract.run(conn, workers=1)

    assert ret == {"docs": 1, "ok": 0, "ok_empty": 0, "schema_error": 1, "llm_error": 0}
    rows = _extraction_rows(conn, "doc1")
    assert len(rows) == 1
    _id, status, raw_output = rows[0]
    assert status == "schema_error"
    # The offending output is preserved (only llm_error is null).
    assert raw_output is not None and json.loads(raw_output) == bad
    assert _mentions(conn, "doc1") == []


def test_extract_schema_error_on_unparseable_valueerror(monkeypatch):
    """chat_json raising ValueError (unparseable output) → 'schema_error', no mentions."""
    conn = _conn()
    _insert_doc(conn, "doc1")
    monkeypatch.setattr(llm, "chat_json", _fake_raising(ValueError("unparseable LLM output")))

    ret = extract.run(conn, workers=1)

    assert ret == {"docs": 1, "ok": 0, "ok_empty": 0, "schema_error": 1, "llm_error": 0}
    rows = _extraction_rows(conn, "doc1")
    assert len(rows) == 1 and rows[0][1] == "schema_error"
    assert _mentions(conn, "doc1") == []


# --------------------------------------------------------------------------- #
# llm_error — any non-ValueError exception from the call
# --------------------------------------------------------------------------- #

def test_extract_llm_error_on_exception(monkeypatch):
    """A transport-level failure (non-ValueError) → 'llm_error', raw_output null, no mentions."""
    conn = _conn()
    _insert_doc(conn, "doc1")
    monkeypatch.setattr(llm, "chat_json", _fake_raising(ConnectionError("server down")))

    ret = extract.run(conn, workers=1)

    assert ret == {"docs": 1, "ok": 0, "ok_empty": 0, "schema_error": 0, "llm_error": 1}
    rows = _extraction_rows(conn, "doc1")
    assert len(rows) == 1
    _id, status, raw_output = rows[0]
    assert status == "llm_error"
    assert raw_output is None
    assert _mentions(conn, "doc1") == []


# --------------------------------------------------------------------------- #
# C3 — idempotency and retry semantics
# --------------------------------------------------------------------------- #

def test_extract_skips_docs_with_ok_row(monkeypatch):
    """A doc with an 'ok' row is un-processed on re-run: chat_json not called again (C3)."""
    conn = _conn()
    _insert_doc(conn, "doc1")
    monkeypatch.setattr(llm, "chat_json",
                        _fake_returning({"entities": [_ENT_METHOD], "relations": []}))
    first = extract.run(conn, workers=1)
    assert first["docs"] == 1 and first["ok"] == 1

    calls = []
    monkeypatch.setattr(
        llm, "chat_json",
        _fake_returning({"entities": [_ENT_METHOD], "relations": []},
                        recorder=lambda k: calls.append(k)))
    second = extract.run(conn, workers=1)

    assert second == {"docs": 0, "ok": 0, "ok_empty": 0, "schema_error": 0, "llm_error": 0}
    assert calls == []  # no LLM call for an already-extracted doc
    assert len(_extraction_rows(conn, "doc1")) == 1  # no duplicate extraction row


def test_extract_retries_error_docs_and_deletes_stale_error_rows(monkeypatch):
    """schema_error doc is retried; the stale error row is deleted before the ok insert (C3)."""
    conn = _conn()
    _insert_doc(conn, "doc1")

    # Run 1: invalid output → schema_error row exists, doc still "un-extracted".
    monkeypatch.setattr(llm, "chat_json",
                        _fake_returning({"entities": [{"type": "Nope", "name": "X"}],
                                         "relations": []}))
    assert extract.run(conn, workers=1)["schema_error"] == 1
    assert len(_extraction_rows(conn, "doc1")) == 1

    # Run 2: good output → prior error row deleted, single ok row remains.
    monkeypatch.setattr(llm, "chat_json",
                        _fake_returning({"entities": [_ENT_METHOD], "relations": []}))
    second = extract.run(conn, workers=1)

    assert second == {"docs": 1, "ok": 1, "ok_empty": 0, "schema_error": 0, "llm_error": 0}
    rows = _extraction_rows(conn, "doc1")
    assert len(rows) == 1  # stale schema_error row was deleted
    assert rows[0][1] == "ok"


# --------------------------------------------------------------------------- #
# Relation dedup
# --------------------------------------------------------------------------- #

def test_extract_relation_dedup(monkeypatch):
    """Two relations with the same (src,src_type,edge,dst,dst_type) → one relation mention."""
    conn = _conn()
    _insert_doc(conn, "doc1")
    dup = dict(_REL, evidence="a different verbatim quote for the same edge")
    result = {"entities": [_ENT_METHOD, _ENT_PROBLEM], "relations": [_REL, dup]}
    monkeypatch.setattr(llm, "chat_json", _fake_returning(result))

    ret = extract.run(conn, workers=1)
    assert ret["ok"] == 1
    # Evidence differs but the dedup key excludes it → exactly one relation row.
    assert len(_mentions(conn, "doc1", "relation")) == 1
    assert len(_mentions(conn, "doc1", "entity")) == 2


# --------------------------------------------------------------------------- #
# Input truncation
# --------------------------------------------------------------------------- #

def test_extract_truncates_input_to_100k(monkeypatch):
    """user text passed to the LLM is full_text truncated to exactly 100,000 chars."""
    conn = _conn()
    long_text = "x" * 200_000
    _insert_doc(conn, "doc1", full_text=long_text)

    captured = {}
    monkeypatch.setattr(
        llm, "chat_json",
        _fake_returning({"entities": [], "relations": []},
                        recorder=lambda k: captured.update(k)))
    extract.run(conn, workers=1)

    assert len(captured["user"]) <= 100_000
    assert captured["user"] == long_text[:100_000]


# --------------------------------------------------------------------------- #
# C1 — concurrency: workers do the LLM call, main thread does the writes
# --------------------------------------------------------------------------- #

def test_extract_concurrent_workers_write_safely(monkeypatch):
    """workers=4 over 8 docs: parallel LLM calls, exact row counts, no DB corruption (C1)."""
    conn = _conn()
    n_docs = 8
    for i in range(n_docs):
        _insert_doc(conn, f"doc{i}", full_text=f"body {i}")

    seen_threads = []
    lock = threading.Lock()

    def fake(**kwargs):
        with lock:
            seen_threads.append(threading.current_thread().name)
        time.sleep(0.01)  # widen the window for true overlap
        return {"entities": [_ENT_METHOD], "relations": []}

    monkeypatch.setattr(llm, "chat_json", fake)

    ret = extract.run(conn, workers=4)

    # Results complete and exact.
    assert ret == {"docs": n_docs, "ok": n_docs, "ok_empty": 0,
                   "schema_error": 0, "llm_error": 0}
    assert len(seen_threads) == n_docs  # called once per doc

    # Parallelism actually happened: >1 distinct non-main worker thread ran the call.
    non_main = {t for t in seen_threads if t != "MainThread"}
    assert len(non_main) > 1

    # No DB corruption: one extraction + one entity mention per doc, exact totals.
    total_extractions = conn.execute("SELECT count(*) FROM extractions").fetchone()[0]
    total_entities = conn.execute(
        "SELECT count(*) FROM mentions WHERE kind = 'entity'").fetchone()[0]
    assert total_extractions == n_docs
    assert total_entities == n_docs
    statuses = conn.execute("SELECT DISTINCT status FROM extractions").fetchall()
    assert statuses == [("ok",)]
