"""Temporal backtest — snapshot construction and HIT/LEAK/OPEN scoring.

The eval spine (phase3-design.md): a papers-only snapshot at a cutoff is a full
pipeline DB; candidates that survive its kill-gate are scored against the FULL
current DB with symmetric name/doc-only tools (C3), and a kill mints a HIT only
if a cross-family closure judge affirms the evidence closes THIS gap (C1) and
some closing evidence postdates the cutoff. Judge-rejected kills are OPEN.
"""

import datetime
import json
from pathlib import Path

import numpy as np

from litgraph import assassin, db, llm, models

SCORING_TOOLS = ("who_tried_this", "full_text")  # C3: symmetric across arms
JUDGE_PATH = Path("prompts/closure_judge_v1.md")
LEADS_MSG = (
    "\n\nGROUNDS includes post_cutoff_leads: documents retrieved by embedding "
    "similarity to the candidate claim. They are leads, not evidence — many will "
    "be irrelevant. Inspect promising ones with full_text before citing.")

_SNAPSHOT_TABLES = ("documents", "extractions", "mentions")


def snapshot(main_path: str, cutoff: datetime.date, dest_path: str) -> dict:
    """Create a papers-only pipeline DB at ``dest_path`` cut at ``cutoff``.

    Copies documents (papers, dated, published <= cutoff), their extractions and
    mentions. Blog/repo docs are excluded (guessed dates + as-of-today content
    would smuggle future knowledge — spec W2). The result is a normal LitGraph
    DB: resolve/embed/communities/propose/assassinate run on it via --db.
    """
    dest = db.connect(dest_path)
    try:
        dest.execute(f"ATTACH '{main_path}' AS src (READ_ONLY)")
        dest.execute(
            "INSERT INTO documents SELECT * FROM src.documents "
            "WHERE source_type = 'paper' AND published_date IS NOT NULL "
            "AND published_date <= ? ON CONFLICT DO NOTHING",
            [cutoff],
        )
        dest.execute(
            "INSERT INTO extractions SELECT e.* FROM src.extractions e "
            "JOIN documents d ON d.doc_id = e.doc_id ON CONFLICT DO NOTHING"
        )
        dest.execute(
            "INSERT INTO mentions SELECT m.* FROM src.mentions m "
            "JOIN documents d ON d.doc_id = m.doc_id ON CONFLICT DO NOTHING"
        )
        counts = {
            t: dest.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in _SNAPSHOT_TABLES
        }
        dest.execute("DETACH src")
    finally:
        dest.close()
    return counts


def _default_lead_embed(texts: list[str]) -> list[list[float]]:
    return llm.embed(texts, models.EMBEDDING_MODEL, command="backtest-recall")


def _recall_leads(conn, cutoff: datetime.date, claims: list[str],
                  lead_embed_fn, k: int) -> list[list[dict]]:
    """Per claim: top-k post-cutoff papers by cosine over title+text-head embeddings.

    The scoring-recall pre-pass (4b): critics searching by name missed closing
    papers at Phase 3 scale, so each candidate gets embedding-retrieved leads to
    inspect. Leads are hints only — evidence discipline (verified quotes + closure
    judge) is unchanged, and both arms get the identical machinery (C3).
    """
    pool = conn.execute(
        "SELECT doc_id, title, published_date, substr(coalesce(full_text, ''), 1, 1500) "
        "FROM documents WHERE source_type = 'paper' AND published_date > ? "
        "ORDER BY doc_id", [cutoff]).fetchall()
    if not pool or not claims:
        return [[] for _ in claims]
    doc_vecs = np.asarray(lead_embed_fn([f"{title}. {head}" for _, title, _, head in pool]),
                          dtype=np.float32)
    claim_vecs = np.asarray(lead_embed_fn(claims), dtype=np.float32)
    doc_vecs /= np.linalg.norm(doc_vecs, axis=1, keepdims=True) + 1e-12
    claim_vecs /= np.linalg.norm(claim_vecs, axis=1, keepdims=True) + 1e-12
    sims = claim_vecs @ doc_vecs.T
    leads = []
    for row in sims:
        top = np.argsort(-row)[:k]
        leads.append([{"doc_id": pool[i][0], "title": pool[i][1],
                       "published_date": str(pool[i][2])} for i in top])
    return leads


def _judge_prompt(claim: str, kill_evidence: list[dict]) -> str:
    body = JUDGE_PATH.read_text().split("---", 1)[1].strip()
    lines = [body, "", f"CANDIDATE GAP: {claim}", "", "KILL EVIDENCE:"]
    for ev in kill_evidence:
        lines.append(f"- [{ev['doc_id']} | published {ev['published_date']}] "
                     f"\"{ev['quote']}\"")
    return "\n".join(lines)


def _evidence_with_dates(conn, evidence: list[dict]) -> list[dict]:
    out = []
    for item in evidence:
        row = conn.execute(
            "SELECT published_date FROM documents WHERE doc_id = ?", [item.get("doc_id")]
        ).fetchone()
        out.append({"doc_id": item.get("doc_id"), "quote": item.get("quote"),
                    "published_date": str(row[0]) if row and row[0] else None})
    return out


def score(conn, cutoff: datetime.date, candidates: list[dict], *,
          chat_fn=None, judge_fn=None, embed_fn=None, lead_embed_fn=None,
          recall_k: int = 8, max_steps: int = 8, tool_budget: int = 24_000) -> dict:
    """Score snapshot survivors against the FULL DB; write backtest_results rows.

    candidates: [{candidate_id, arm ('graph'|'baseline'), template, claim, grounds}].
    grounds.node_ids are stripped before critics see them (C3). Disposition per
    candidate: verified kill + judge affirms + any closing doc > cutoff -> HIT;
    judge affirms but all closing evidence <= cutoff -> LEAK; else OPEN.

    lead_embed_fn (batch texts -> vecs) powers the recall pre-pass: top-recall_k
    post-cutoff papers per claim injected as grounds.post_cutoff_leads, identical
    machinery for both arms. None (non-default path) disables the pre-pass.
    """
    default_path = chat_fn is None
    # Scoring critics stay single-model (ADJUDICATION_MODEL): backtest comparability
    # across arms matters more than ensemble coverage here.
    chat_fn = chat_fn or assassin._make_chat_fn(models.ADJUDICATION_MODEL)
    judge_fn = judge_fn or assassin._default_cross_family_fn
    if embed_fn is None and default_path:
        embed_fn = assassin._default_embed_fn
    if lead_embed_fn is None and default_path:
        lead_embed_fn = _default_lead_embed
    all_leads = (_recall_leads(conn, cutoff, [c["claim"] for c in candidates],
                               lead_embed_fn, recall_k)
                 if lead_embed_fn is not None and recall_k
                 else [[] for _ in candidates])

    tally = {"HIT": 0, "LEAK": 0, "OPEN": 0}
    for cand, leads in zip(candidates, all_leads, strict=True):
        grounds = {k: v for k, v in (cand.get("grounds") or {}).items()
                   if k != "node_ids"}
        if leads:
            grounds["post_cutoff_leads"] = leads
        system = (assassin._system_prompt(cand.get("template", "T1"))
                  + "\n\nSCORING MODE: only the `who_tried_this` and `full_text` "
                    "tools are available in this review."
                  + (LEADS_MSG if leads else ""))
        cursor = conn.cursor()
        kills = []
        for _role in ("critic_1", "critic_2"):
            result = assassin._run_critic(
                cursor, system, cand["claim"], grounds, chat_fn,
                max_steps, tool_budget, embed_fn, allowed_tools=SCORING_TOOLS,
            )
            if result["kind"] == "verdict" and result["verdict"] == "kill":
                kills.append(result)

        disposition, judge_raw, evidence = "OPEN", None, []
        if kills:
            evidence = _evidence_with_dates(
                conn,
                [e for k in kills for e in
                 (e_.model_dump() for e_ in k["args"].evidence)],
            )
            judge_raw = judge_fn(_judge_prompt(cand["claim"], evidence)) or {}
            if bool(judge_raw.get("closes")):
                post = [e for e in evidence
                        if e["published_date"] and
                        datetime.date.fromisoformat(e["published_date"]) > cutoff]
                disposition = "HIT" if post else "LEAK"

        conn.execute(
            "INSERT OR REPLACE INTO backtest_results "
            "(candidate_id, arm, cutoff, disposition, claim, judge, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [cand["candidate_id"], cand.get("arm", "graph"), cutoff, disposition,
             cand["claim"], json.dumps(judge_raw), json.dumps(evidence)],
        )
        tally[disposition] += 1

    decided = tally["HIT"] + tally["LEAK"]
    return {
        **tally,
        "scored": sum(tally.values()),
        "decided_precision": tally["HIT"] / decided if decided else None,
        "lower_bound_rate": tally["HIT"] / sum(tally.values()) if sum(tally.values()) else None,
    }
