"""Frontier-gaps runner (4c): the validated loop over the FULL current graph.

Wires the pieces the 4b-ext cycle validated — structural hole leads (T2, leads
only, never candidates), hole-seeded proposer sessions with the graph as tools,
extractive grounding with verbatim snap-verify — and inserts survivors-to-be as
FG:<arm>:nnn gap_candidates (status='proposed') for the standing kill-gate
(`litgraph assassinate`) and dossier renderer (`litgraph gaps`).
"""

import difflib
import json
import re
from concurrent.futures import ThreadPoolExecutor

from litgraph import llm, models, proposer, templates

PROMPT_VERSION = "frontier_qwen_v1"
QUOTES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["quotes"],
    "properties": {"quotes": {"type": "array", "maxItems": 3, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["doc_id", "quote"],
        "properties": {"doc_id": {"type": "string"}, "quote": {"type": "string"}}}}},
}


def hole_leads(conn, k: int = 25) -> list[dict]:
    """Top-k structural-hole leads from the current graph, best (widest) first.

    Leads are proposer seeds, not candidates: nothing is written to the DB.
    """
    view = templates._View(conn)
    leads = []
    for ca, cb, members, expected, observed, bridges, pr in templates._t2_holes(view):
        top_a = max(members[ca], key=lambda n: pr.get(n, 0.0))
        top_b = max(members[cb], key=lambda n: pr.get(n, 0.0))
        leads.append({
            "cluster_a": view.name.get(top_a),
            "cluster_b": view.name.get(top_b),
            "expected": round(expected, 1),
            "observed": observed,
            "bridges": sorted(view.name.get(n, n) for n in bridges)[:6],
            "score": expected - observed,
        })
    leads.sort(key=lambda d: -d["score"])
    return leads[:k]


def _lead_seed(lead: dict) -> str:
    return ("STRUCTURAL LEAD (the graph flags this region as under-explored):\n"
            f"Cluster led by '{lead['cluster_a']}' and cluster led by "
            f"'{lead['cluster_b']}' are under-linked "
            f"({lead['observed']} cross-edges vs {lead['expected']} expected).\n"
            f"Entities touching both: {', '.join(lead['bridges']) or 'none'}")


def _snap(quote: str, text: str) -> str | None:
    """Return the verbatim-in-text version of ``quote`` (whitespace-normalized),
    rescuing near-misses >=0.9 ratio; None when the quote is not in the text."""
    norm_q = re.sub(r"\s+", " ", quote).strip()
    norm_t = re.sub(r"\s+", " ", text)
    if norm_q in norm_t:
        return norm_q
    n = len(norm_q)
    best, best_r = None, 0.9
    for i in range(0, max(1, len(norm_t) - n), max(1, n // 2)):
        window = norm_t[i:i + n + 40]
        m = difflib.SequenceMatcher(None, norm_q, window, autojunk=False)
        if m.ratio() > best_r:
            a, b, size = m.find_longest_match(0, n, 0, len(window))
            if size > 0.7 * n:
                best, best_r = window[b:b + size].strip(), m.ratio()
    return best


def ground(proposal: dict, full_texts: dict, ground_system: str, chat_json_fn) -> dict | None:
    """Extractive grounding: pull verbatim quotes for the claim from its doc_ids."""
    doc_ids = [d for d in proposal["doc_ids"] if d in full_texts][:4]
    if not doc_ids:
        return None
    excerpts = "\n\n".join(f"=== {d} ===\n{full_texts[d][:5000]}" for d in doc_ids)
    user = f"CLAIM: {proposal['claim']}\n\nEXCERPTS\n\n{excerpts}"
    for _ in range(2):
        try:
            quotes = chat_json_fn(ground_system, user)["quotes"]
        except Exception:  # noqa: BLE001 — retry once, then drop the proposal
            continue
        ok = []
        for q in quotes:
            if q["doc_id"] in doc_ids:
                s = _snap(q["quote"], full_texts[q["doc_id"]])
                if s:
                    ok.append(q | {"quote": s})
        if ok:
            return proposal | {"grounds": {"doc_ids": sorted({q["doc_id"] for q in ok}),
                                           "quotes": [q["quote"] for q in ok]}}
        user += "\n\nYour previous quotes were not verbatim. Copy exact text."
    return None


def dedupe(proposals: list[dict]) -> list[dict]:
    seen, unique = set(), []
    for p in proposals:
        key = re.sub(r"\W+", " ", p["claim"].lower()).strip()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def insert(conn, grounded: list[dict], *, arm: str, proposer_name: str) -> int:
    """Insert grounded proposals as FG:<arm>:nnn, continuing the existing sequence."""
    prefix = f"FG:{arm}:"
    (n,) = conn.execute(
        "SELECT count(*) FROM gap_candidates WHERE candidate_id LIKE ?", [prefix + "%"]
    ).fetchone()
    for i, g in enumerate(grounded, n + 1):
        conn.execute(
            "INSERT INTO gap_candidates "
            "(candidate_id, template, claim, grounds, params, status) "
            "VALUES (?, 'T1', ?, ?, ?, 'proposed')",
            [f"{prefix}{i:03d}", g["claim"],
             json.dumps(g["grounds"], ensure_ascii=False),
             json.dumps({"arm": "frontier", "proposer": proposer_name,
                         "warrant": g["warrant"],
                         "self_critique": g["self_critique"]})])
    conn.commit()
    return len(grounded)


def run_qwen(conn, session_system: str, ground_system: str, *,
             max_holes: int = 25, max_workers: int = 4,
             chat_fn=None, ground_chat_fn=None, embed_fn=None,
             leads: list[dict] | None = None) -> dict:
    """Hole-seeded Qwen sessions -> ground -> verify -> insert FG:qwen candidates."""
    if chat_fn is None:
        def chat_fn(messages, schema_name, schema):
            return llm.chat_json_messages(
                command="frontier", model=models.ADJUDICATION_MODEL,
                messages=messages, schema_name=schema_name, schema=schema,
                prompt_version=PROMPT_VERSION, temperature=0.0, max_tokens=2048)
    if ground_chat_fn is None:
        def ground_chat_fn(system, user):
            return llm.chat_json(
                command="frontier", model=models.ADJUDICATION_MODEL, system=system,
                user=user, schema_name="quotes", schema=QUOTES_SCHEMA,
                prompt_version=PROMPT_VERSION, temperature=0.0, max_tokens=2048)
    if embed_fn is None:
        def embed_fn(name):
            return llm.embed([name], models.EMBEDDING_MODEL, command="frontier")[0]

    if leads is None:
        leads = hole_leads(conn, k=max_holes)

    def _session(lead):
        user = (f"{_lead_seed(lead)}\n\nInvestigate with tools, then propose up to 2 "
                "distinct gaps.")
        try:
            return proposer.run_session(
                conn.cursor(), session_system, user, chat_fn, max_steps=10,
                tool_budget=18_000, embed_fn=embed_fn, max_proposals=2)
        except Exception as exc:  # noqa: BLE001 — one broken session must not sink the run
            print(f"frontier: session failed, skipped: {exc!r}", flush=True)
            return []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        proposals = [p for r in ex.map(_session, leads[:max_holes]) for p in r]
    unique = dedupe(proposals)

    full_texts = dict(conn.execute(
        "SELECT doc_id, coalesce(full_text, '') FROM documents").fetchall())
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        grounded = [g for g in ex.map(
            lambda p: ground(p, full_texts, ground_system, ground_chat_fn), unique) if g]
    inserted = insert(conn, grounded, arm="qwen", proposer_name="qwen-tools")
    return {"sessions": len(leads[:max_holes]), "proposals": len(proposals),
            "unique": len(unique), "grounded": len(grounded), "inserted": inserted}
