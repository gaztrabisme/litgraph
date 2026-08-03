"""Backtest unit gates (phase3-design.md): snapshot filter + scoring partition."""

import datetime

from litgraph import backtest, db

CUTOFF = datetime.date(2025, 7, 1)


def _seed_main(path):
    conn = db.connect(path)
    rows = [
        ("p_old", "paper", "2025-01-01", "old paper text about episodic memory buffers"),
        ("p_edge", "paper", "2025-07-01", "on-cutoff paper text"),
        ("p_new", "paper", "2026-01-01", "future paper: we apply reflexion memory to embodied agents"),
        ("b_old", "blog", "2024-01-01", "blog text that must never enter a snapshot"),
        ("p_null", "paper", None, "undated paper text"),
    ]
    for doc_id, st, pub, text in rows:
        conn.execute(
            "INSERT INTO documents (doc_id, source_type, url, title, published_date, full_text) "
            "VALUES (?, ?, 'u', ?, ?, ?)", [doc_id, st, doc_id, pub, text])
        conn.execute(
            "INSERT INTO extractions (extraction_id, doc_id, prompt_version, model, status) "
            "VALUES (?, ?, 'v1', 'm', 'ok')", [f"e_{doc_id}", doc_id])
        conn.execute(
            "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, src_name, src_type, "
            "dst_name, dst_type, edge_type, evidence) VALUES (?, ?, ?, 'relation', 'A', 'Method', "
            "'B', 'Problem', 'USES', 'ev')", [f"m_{doc_id}", f"e_{doc_id}", doc_id])
    conn.close()


def test_snapshot_papers_only_and_cutoff_inclusive(tmp_path):
    main = str(tmp_path / "main.duckdb")
    dest = str(tmp_path / "snap.duckdb")
    _seed_main(main)
    counts = backtest.snapshot(main, CUTOFF, dest)
    assert counts == {"documents": 2, "extractions": 2, "mentions": 2}
    snap = db.connect(dest)
    ids = {r[0] for r in snap.execute("SELECT doc_id FROM documents").fetchall()}
    assert ids == {"p_old", "p_edge"}  # papers <= cutoff; blog + NULL + future excluded
    snap.close()


def _mk_full(tmp_path):
    main = str(tmp_path / "full.duckdb")
    _seed_main(main)
    return db.connect(main)


def _kill_script(quote, doc_id):
    """chat_fn emitting one immediate kill verdict with the given evidence."""
    def chat_fn(messages, schema_name, schema):
        return {"thought": "t", "action": "verdict", "args": {
            "verdict": "kill", "refutation_claim": "already done",
            "evidence": [{"doc_id": doc_id, "quote": quote}],
            "feasibility": "feasible", "confidence_bucket": "more_probable"}}
    return chat_fn


CAND = [{"candidate_id": "T1:x", "arm": "graph", "template": "T1",
         "claim": "gap claim", "grounds": {"node_ids": ["n1"], "doc_ids": ["p_old"]}}]


def test_score_hit_post_cutoff_evidence_and_judge_affirms(tmp_path):
    conn = _mk_full(tmp_path)
    r = backtest.score(conn, CUTOFF, CAND,
                       chat_fn=_kill_script("we apply reflexion memory to embodied agents", "p_new"),
                       judge_fn=lambda p: {"closes": True, "reason": "p_new closes it"})
    assert (r["HIT"], r["LEAK"], r["OPEN"]) == (1, 0, 0)
    assert r["decided_precision"] == 1.0
    row = conn.execute("SELECT disposition FROM backtest_results").fetchone()
    assert row[0] == "HIT"


def test_score_leak_pre_cutoff_evidence(tmp_path):
    conn = _mk_full(tmp_path)
    r = backtest.score(conn, CUTOFF, CAND,
                       chat_fn=_kill_script("old paper text about episodic memory buffers", "p_old"),
                       judge_fn=lambda p: {"closes": True, "reason": "p_old"})
    assert (r["HIT"], r["LEAK"], r["OPEN"]) == (0, 1, 0)


def test_score_judge_rejection_is_open(tmp_path):
    conn = _mk_full(tmp_path)
    r = backtest.score(conn, CUTOFF, CAND,
                       chat_fn=_kill_script("we apply reflexion memory to embodied agents", "p_new"),
                       judge_fn=lambda p: {"closes": False, "reason": "merely topical"})
    assert (r["HIT"], r["LEAK"], r["OPEN"]) == (0, 0, 1)
    assert r["decided_precision"] is None


def test_score_survive_is_open_and_judge_never_called(tmp_path):
    conn = _mk_full(tmp_path)
    called = []

    def chat_fn(messages, schema_name, schema):
        return {"thought": "t", "action": "verdict", "args": {
            "verdict": "survive", "refutation_claim": "nearest miss",
            "evidence": [{"doc_id": "p_old", "quote": "old paper text about episodic memory buffers"}],
            "feasibility": "feasible", "confidence_bucket": "more_probable"}}

    r = backtest.score(conn, CUTOFF, CAND, chat_fn=chat_fn,
                       judge_fn=lambda p: called.append(p) or {"closes": True})
    assert (r["HIT"], r["LEAK"], r["OPEN"]) == (0, 0, 1)
    assert not called


def _kw_embed(texts):
    """Deterministic fake batch embedder: axis 0 = 'reflexion', axis 1 = other."""
    return [[1.0, 0.0] if "reflexion" in t else [0.0, 1.0] for t in texts]


def test_score_recall_leads_topk_post_cutoff_papers_only(tmp_path):
    conn = _mk_full(tmp_path)
    seen = {}

    def chat_fn(messages, schema_name, schema):
        seen["user"] = messages[1]["content"]
        seen["system"] = messages[0]["content"]
        return {"thought": "t", "action": "verdict", "args": {
            "verdict": "survive", "refutation_claim": "nearest miss",
            "evidence": [{"doc_id": "p_old", "quote": "old paper text about episodic memory buffers"}],
            "feasibility": "unknown", "confidence_bucket": "least_probable"}}

    cand = [{"candidate_id": "T1:x", "arm": "graph", "template": "T1",
             "claim": "reflexion memory transfer gap",
             "grounds": {"node_ids": ["n1"], "doc_ids": ["p_old"]}}]
    backtest.score(conn, CUTOFF, cand, chat_fn=chat_fn, judge_fn=lambda p: {},
                   lead_embed_fn=_kw_embed, recall_k=1)
    leads = __import__("json").loads(seen["user"].split("GROUNDS\n", 1)[1])["post_cutoff_leads"]
    # only post-cutoff papers are lead-eligible; top-1 by cosine is p_new
    assert leads == [{"doc_id": "p_new", "title": "p_new", "published_date": "2026-01-01"}]
    assert "post_cutoff_leads" in seen["system"]  # LEADS_MSG explains the field
    assert "node_ids" not in seen["user"]  # C3 stripping unaffected


def test_score_no_lead_embed_fn_means_no_leads(tmp_path):
    conn = _mk_full(tmp_path)
    seen = {}

    def chat_fn(messages, schema_name, schema):
        seen["user"] = messages[1]["content"]
        seen["system"] = messages[0]["content"]
        return {"thought": "t", "action": "verdict", "args": {
            "verdict": "survive", "refutation_claim": "nearest miss",
            "evidence": [{"doc_id": "p_old", "quote": "old paper text about episodic memory buffers"}],
            "feasibility": "unknown", "confidence_bucket": "least_probable"}}

    backtest.score(conn, CUTOFF, CAND, chat_fn=chat_fn, judge_fn=lambda p: {})
    assert "post_cutoff_leads" not in seen["user"]
    assert "post_cutoff_leads" not in seen["system"]


def test_score_strips_node_ids_and_whitelists_tools(tmp_path):
    conn = _mk_full(tmp_path)
    seen = {}

    def chat_fn(messages, schema_name, schema):
        seen["user"] = messages[1]["content"]
        seen["system"] = messages[0]["content"]
        if "calls" not in seen:
            seen["calls"] = 1
            return {"thought": "t", "action": "subgraph_around", "args": {"node_id": "n1"}}
        return {"thought": "t", "action": "verdict", "args": {
            "verdict": "survive", "refutation_claim": "nearest miss",
            "evidence": [{"doc_id": "p_old", "quote": "old paper text about episodic memory buffers"}],
            "feasibility": "unknown", "confidence_bucket": "least_probable"}}

    backtest.score(conn, CUTOFF, CAND, chat_fn=chat_fn, judge_fn=lambda p: {})
    assert "node_ids" not in seen["user"]  # C3: grounds stripped
    assert "SCORING MODE" in seen["system"]
    # off-whitelist tool call was NOT dispatched: no TOOL RESULT message follows;
    # the critic got a malformed-action retry instead, then survived -> OPEN row exists
    assert conn.execute("SELECT disposition FROM backtest_results").fetchone()[0] == "OPEN"
