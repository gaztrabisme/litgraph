"""Stage-A kill gate + cross-family audit loop (dispose half of the gap engine).

Only status='proposed' gap_candidates are processed. Per candidate two isolated
Stage-A critics run sequentially (roles critic_1/critic_2) as self-managed ReAct
loops over the four read-only tools; on the default path each role gets its own
model per models.STAGE_A_CRITICS (cross-family ensemble, GAPMAP). Each verdict's
evidence is tiered by verify.py; a KILL verdict additionally passes the W5
relevance gate — an LLM judge (ADJUDICATION_MODEL) checks the verified quotes
actually substantiate the refutation claim, closing the verified-but-irrelevant
exploit observed in the 2026-07-17 head-to-head. Judge failure is fail-open
(the kill stands) so a flaky judge cannot abstain-storm a run; an unsupported
ruling consumes the same single retry budget as failed provenance.
The decision table (phase2-design.md §Assassin) is then applied, optionally invoking
the single-shot cross-family auditor for resurrection (unanimous kill) or Stage D
(survivors). Every verdict row + the terminal status are written on the MAIN thread
in one transaction per candidate (C3): worker threads do LLM calls and read-only tool
queries via their own conn.cursor(); they never write.

Decision-table cells where the coordinator left a choice, encoded here:
  - resurrection: any parse/return failure of cross_family_fn -> overturn treated
    False (the kill stands -> killed). Conservative: a broken auditor cannot silently
    resurrect a killed gap.
  - Stage D non-concur: the refutation's evidence is verified against the dossier
    doc_ids, but the candidate is marked 'contested' REGARDLESS of whether that
    verification passes (verified refute = contested; discarded/fabricated refute =
    contested). Only concur=true promotes to 'survived'. A parse/return failure of
    cross_family_fn is treated as NON-concur with empty evidence -> contested
    (never a silent pass to survived).
"""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from litgraph import llm, models, tools, verify

PROMPT_PATH = Path("prompts/assassin_v1.md")
RESURRECT_PATH = Path("prompts/resurrect_v1.md")
STAGE_D_PATH = Path("prompts/stage_d_v1.md")
RELEVANCE_PATH = Path("prompts/relevance_v1.md")

SCHEMA_NAME = "critic_action"
CROSS_FAMILY_MODEL = "claude-cli"
CONTEXT_RADIUS = 300  # chars of source span shown around each cited quote in dossiers

# Template-specific kill mandate (W5): T1/T2/T3 want an in-index doc that already did
# it (or in-index evidence nobody would); T4 wants in-index evidence the conflict is
# already resolved or spurious.
_MANDATE_TRANSFER = (
    "This candidate asserts a gap: a method, transfer, or assumption that the index is "
    "claimed not to cover. Kill it by finding the in-index document that ALREADY did "
    "this, or in-index evidence that nobody would or could (a missing dataset, a method "
    "shown not to work). If you find it in the index, emit a kill; otherwise survive "
    "citing the nearest miss."
)
_MANDATE_CONTRADICTION = (
    "This candidate asserts an unresolved in-index contradiction. Kill it by finding "
    "in-index evidence that the asserted conflict is already resolved or spurious (a "
    "misextraction, or the two sides describe different settings/datasets). If you find "
    "it in the index, emit a kill; otherwise survive citing the nearest miss."
)
_MANDATES = {
    "T1": _MANDATE_TRANSFER,
    "T2": _MANDATE_TRANSFER,
    "T3": _MANDATE_TRANSFER,
    "T4": _MANDATE_CONTRADICTION,
}

_FORCE_MSG = (
    "You have investigated enough. Stop calling tools and emit your final `verdict` "
    "action now, citing evidence copied verbatim from the tool results above."
)
_EVIDENCE_MSG = (
    "Evidence verification failed: a cited quote was not found in the referenced "
    "document. Quotes must be copied character-for-character from a tool result. "
    "Re-issue the verdict with verifiable evidence or a different quote."
)
_RELEVANCE_MSG = (
    "Relevance check failed: your quotes exist in the cited documents but do not "
    "substantiate your refutation claim ({reason}). A kill needs evidence that "
    "states or directly entails the refutation. Re-issue the verdict with evidence "
    "that actually supports it, or survive citing the nearest miss."
)


def _prompt_body(path: Path) -> str:
    """Prompt body: everything after the first '---' line of a versioned prompt file."""
    _, _, body = path.read_text().partition("\n---\n")
    return body.strip()


def _system_prompt(template: str) -> str:
    mandate = _MANDATES.get(template, _MANDATE_TRANSFER)
    return _prompt_body(PROMPT_PATH).replace("{TEMPLATE_MANDATE}", mandate)


def _candidate_user(claim: str, grounds: dict) -> str:
    return f"CANDIDATE CLAIM\n{claim}\n\nGROUNDS\n{json.dumps(grounds, ensure_ascii=False)}"


# ---------------------------------------------------------------------------
# Default injected functions (tests always inject fakes; these only run live).
# ---------------------------------------------------------------------------
def _make_chat_fn(model: str):
    """Bind a Stage-A critic chat fn to one model (per-role ensemble routing)."""
    def chat_fn(messages, schema_name, schema):
        return llm.chat_json_messages(
            command="assassinate", model=model, messages=messages,
            schema_name=schema_name, schema=schema,
            prompt_version=models.ASSASSIN_PROMPT_VERSION,
        )
    return chat_fn


def _default_relevance_fn(claim: str, refutation_claim: str, evidence: list[dict]) -> dict:
    """W5 judge: {'supported': bool, 'reason': str}. Evidence items carry doc_id,
    quote, and (when locatable) a source-context span."""
    user = json.dumps(
        {"candidate_claim": claim, "refutation_claim": refutation_claim,
         "evidence": evidence},
        ensure_ascii=False,
    )
    out = llm.chat_json(
        command="relevance", model=models.ADJUDICATION_MODEL,
        system=_prompt_body(RELEVANCE_PATH), user=user,
        schema_name="relevance_verdict", schema=models.relevance_json_schema(),
        prompt_version=models.RELEVANCE_PROMPT_VERSION, temperature=0.0,
    )
    return models.RelevanceVerdict.model_validate(out).model_dump()


def _strip_fences(text: str) -> str:
    """Peel a ```json ... ``` fence if the auditor wrapped its JSON despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text.lstrip("`")
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _first_json(text: str) -> dict:
    """Extract the first JSON object from auditor text, tolerating surrounding prose.

    The claude CLI's result field sometimes carries prose before and/or after the
    verdict object (observed live 2026-07-17/18: 'Expecting value: char 0' and
    'Extra data' parse failures silently became non-concur -> contested). Raises
    ValueError if no JSON object parses.
    """
    text = _strip_fences(text)
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    raise ValueError(f"no JSON object in auditor output: {text[:120]!r}")


def _default_cross_family_fn(prompt: str) -> dict:
    """Single-shot cross-family critic via `claude -p ... --output-format json`.

    Parses the CLI envelope, then json.loads its "result" field. Any failure returns
    {} — callers treat that as overturn=false (resurrection) / non-concur (Stage D),
    both of which are the conservative side of the decision table.
    """
    command = "resurrect" if "resurrection auditor" in prompt else "stage-d"
    result = None
    proc = None
    for _attempt in range(2):  # W2: parse + one retry
        try:
            proc = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json"],
                capture_output=True, text=True, timeout=600,
            )
            envelope = json.loads(proc.stdout)
            result = _first_json(envelope["result"])
            break
        except Exception as exc:  # noqa: BLE001 — broken auditor must not abort the run
            raw = ""
            if proc is not None:
                try:
                    raw = json.loads(proc.stdout).get("result", "")[:500]
                except Exception:  # noqa: BLE001
                    raw = (proc.stdout or "")[:500]
            llm._log(command, {"model": CROSS_FAMILY_MODEL, "prompt_version": "cross_family_v1",
                               "prompt_prefix": prompt[:500], "error": repr(exc),
                               "raw_result": raw})
    if result is None:
        return {}
    llm._log(command, {"model": CROSS_FAMILY_MODEL, "prompt_version": "cross_family_v1",
                       "prompt_prefix": prompt[:500], "result": result})
    return result


# ---------------------------------------------------------------------------
# Tool dispatch (read-only, per-thread cursor).
# ---------------------------------------------------------------------------
def _dispatch(cursor, name: str, args: dict, embed_fn=None) -> dict:
    if name == "subgraph_around":
        return tools.subgraph_around(cursor, args.get("node_id"))
    if name == "full_text":
        return tools.full_text(cursor, args.get("doc_id"))
    if name == "evidence_for":
        return tools.evidence_for(cursor, args.get("node_id"))
    if name == "who_tried_this":
        return tools.who_tried_this(
            cursor, method_name=args.get("method_name"),
            problem_name=args.get("problem_name"), embed_fn=embed_fn,
        )
    return {"error": f"unknown tool: {name!r}"}


def _default_embed_fn(name: str) -> list[float]:
    """Single-name embedding for who_tried_this paraphrase resolution (review C3)."""
    return llm.embed([name], models.EMBEDDING_MODEL, command="assassinate")[0]


# ---------------------------------------------------------------------------
# One Stage-A critic: self-managed ReAct loop -> verdict or abstain.
# ---------------------------------------------------------------------------
def _run_critic(cursor, system: str, claim: str, grounds: dict, chat_fn,
                max_steps: int, tool_budget: int, embed_fn=None,
                allowed_tools=None, relevance_fn=None) -> dict:
    """Return {'kind': 'verdict', 'verdict', 'tier', 'args', 'raw'} or
    {'kind': 'abstain', 'raw'}.

    Malformed turns (any exception from chat_fn — parse errors AND transport/timeout
    errors, review C1 — OR unknown action OR invalid verdict) and failed-evidence
    verdicts each carry their OWN single retry budget (review W4); the next failure
    in that category abstains. After max_steps tool calls or a tool-output budget
    breach a forcing message demands a verdict on the next turn.
    """
    schema = models.critic_action_json_schema()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _candidate_user(claim, grounds)},
    ]
    tool_calls = 0
    tool_chars = 0
    retried_malformed = False
    retried_evidence = False
    forced = False
    last_raw = None
    hard_cap = max_steps + 6

    for _ in range(hard_cap):
        if not forced and (tool_calls >= max_steps or tool_chars > tool_budget):
            forced = True
            messages.append({"role": "user", "content": _FORCE_MSG})

        try:
            action = chat_fn(messages, SCHEMA_NAME, schema)
        except Exception as exc:  # noqa: BLE001 — one flaky call must not abort the run (C1)
            if not retried_malformed:
                retried_malformed = True
                messages.append({"role": "user", "content":
                                 f"Your last response could not be parsed ({exc}). "
                                 "Respond with exactly one JSON action."})
                continue
            return {"kind": "abstain", "raw": last_raw}

        last_raw = action
        name = action.get("action") if isinstance(action, dict) else None

        if name == "verdict":
            try:
                vargs = models.CriticVerdictArgs.model_validate(action.get("args", {}))
            except (ValidationError, ValueError) as exc:
                if not retried_malformed:
                    retried_malformed = True
                    messages.append({"role": "user", "content":
                                     f"Your verdict was malformed ({exc}). Emit a valid "
                                     "`verdict` action with all required fields."})
                    continue
                return {"kind": "abstain", "raw": action}
            evidence = [{"doc_id": e.doc_id, "quote": e.quote} for e in vargs.evidence]
            tier, _ = verify.verify_evidence(cursor, evidence)
            if tier == "failed":
                if not retried_evidence:
                    retried_evidence = True
                    messages.append({"role": "user", "content": _EVIDENCE_MSG})
                    continue
                return {"kind": "abstain", "raw": action}
            if vargs.verdict == "kill" and relevance_fn is not None:
                items = [{**item,
                          "context": _span_context(cursor, item["doc_id"], item["quote"])}
                         for item in evidence]
                try:
                    rel = relevance_fn(claim, vargs.refutation_claim, items)
                except Exception:  # noqa: BLE001 — fail-open: a flaky judge must not
                    rel = None     # convert verified kills into abstains
                if rel is not None and not rel.get("supported", True):
                    if not retried_evidence:
                        retried_evidence = True
                        messages.append({"role": "user", "content":
                                         _RELEVANCE_MSG.format(reason=rel.get("reason", ""))})
                        continue
                    return {"kind": "abstain", "raw": action}
            return {"kind": "verdict", "verdict": vargs.verdict, "tier": tier,
                    "args": vargs, "raw": action}

        if name in (allowed_tools or models.CRITIC_TOOLS) and not forced:
            result = _dispatch(cursor, name, action.get("args", {}), embed_fn)
            result_json = json.dumps(result, ensure_ascii=False)
            tool_calls += 1
            tool_chars += len(result_json)
            messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
            messages.append({"role": "user", "content": f"TOOL RESULT ({name}): {result_json}"})
            continue

        # Unknown action, or a tool call after the forcing turn -> malformed.
        if not retried_malformed:
            retried_malformed = True
            note = ("You must emit a `verdict` action now, not a tool call."
                    if forced else f"Unknown or unusable action {name!r}. Choose a tool "
                    "or emit a verdict.")
            messages.append({"role": "user", "content": note})
            continue
        return {"kind": "abstain", "raw": action}

    return {"kind": "abstain", "raw": last_raw}


# ---------------------------------------------------------------------------
# Dossier assembly (cross-family prompts).
# ---------------------------------------------------------------------------
def _span_context(cursor, doc_id: str, quote: str) -> str | None:
    row = cursor.execute(
        "SELECT full_text FROM documents WHERE doc_id = ?", [doc_id]
    ).fetchone()
    if not row or not row[0]:
        return None
    text = row[0]
    idx = text.find(quote)
    if idx != -1:
        start = max(0, idx - CONTEXT_RADIUS)
        return text[start:idx + len(quote) + CONTEXT_RADIUS]
    nt, nq = verify.normalize(text), verify.normalize(quote)
    j = nt.find(nq)
    if j == -1:
        return None
    return nt[max(0, j - CONTEXT_RADIUS):j + len(nq) + CONTEXT_RADIUS]


def _evidence_block(cursor, evidence: list[dict]) -> str:
    lines = []
    for item in evidence:
        doc_id, quote = item.get("doc_id"), item.get("quote", "")
        lines.append(f'  - {doc_id}: "{quote}"')
        ctx = _span_context(cursor, doc_id, quote)
        lines.append(f"    source span: ...{ctx}..." if ctx else
                     "    source span: (quote not located in the cited document)")
    return "\n".join(lines) if lines else "  (none)"


def _verdict_evidence(result: dict) -> list[dict]:
    return [{"doc_id": e.doc_id, "quote": e.quote} for e in result["args"].evidence]


def _dossier(cursor, header: Path, claim: str, grounds: dict,
             verdicts: list[tuple[str, dict]],
             verdict_label: str) -> str:
    parts = [_prompt_body(header), "", "CANDIDATE CLAIM", claim, "",
             "GROUNDS", json.dumps(grounds, ensure_ascii=False), ""]
    for role, result in verdicts:
        parts.append(f"{verdict_label} — {role}")
        parts.append(f"  refutation_claim: {result['args'].refutation_claim}")
        parts.append("  evidence:")
        parts.append(_evidence_block(cursor, _verdict_evidence(result)))
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Verdict-row shaping.
# ---------------------------------------------------------------------------
def _stage_a_row(role: str, result: dict, model: str) -> dict:
    if result["kind"] == "verdict":
        args = result["args"]
        return {
            "stage": "A", "role": role, "model": model,
            "verdict": result["verdict"], "refutation_claim": args.refutation_claim,
            "evidence": json.dumps([e.model_dump() for e in args.evidence]),
            "tier": result["tier"], "feasibility": args.feasibility,
            "confidence": args.confidence_bucket, "raw": json.dumps(result["raw"]),
        }
    raw = result.get("raw")
    return {
        "stage": "A", "role": role, "model": model,
        "verdict": "abstain", "refutation_claim": None, "evidence": None,
        "tier": None, "feasibility": None, "confidence": None,
        "raw": json.dumps(raw) if raw is not None else None,
    }


def _cross_row(stage: str, role: str, verdict: str, res: dict, tier=None) -> dict:
    ev = res.get("evidence")
    return {
        "stage": stage, "role": role, "model": CROSS_FAMILY_MODEL, "verdict": verdict,
        "refutation_claim": res.get("refutation_claim") or res.get("reason"),
        "evidence": json.dumps(ev) if ev else None, "tier": tier,
        "feasibility": None, "confidence": None, "raw": json.dumps(res),
    }


# ---------------------------------------------------------------------------
# Per-candidate processing (worker thread: LLM + read-only reads only).
# ---------------------------------------------------------------------------
def _process_candidate(cursor, cid, template, claim, grounds, critics, cross_family_fn,
                       max_steps, tool_budget, embed_fn=None, relevance_fn=None) -> dict:
    """``critics``: list of (role, chat_fn, model) — per-role ensemble routing."""
    system = _system_prompt(template)
    results = [
        (role, _run_critic(cursor, system, claim, grounds, chat_fn, max_steps,
                           tool_budget, embed_fn, relevance_fn=relevance_fn))
        for role, chat_fn, _model in critics
    ]
    rows = [_stage_a_row(role, result, model)
            for (role, _fn, model), (_r, result) in zip(critics, results, strict=True)]

    kills = [(role, r) for role, r in results
             if r["kind"] == "verdict" and r["verdict"] == "kill"]
    survives = [(role, r) for role, r in results
                if r["kind"] == "verdict" and r["verdict"] == "survive"]

    if kills:
        if len(kills) == 2:  # unanimous verified kill -> resurrection (once).
            dossier = _dossier(cursor, RESURRECT_PATH, claim, grounds, kills, "KILL VERDICT")
            res = cross_family_fn(dossier) or {}
            overturn = bool(res.get("overturn"))
            rows.append(_cross_row("resurrect", "resurrector",
                                   "survive" if overturn else "kill", res))
            status = "contested" if overturn else "killed"
        else:  # single verified kill -> killed, no resurrection.
            status = "killed"
    elif survives:  # no verified kill + >=1 verified survive -> Stage D.
        dossier = _dossier(cursor, STAGE_D_PATH, claim, grounds, survives, "SURVIVE VERDICT")
        res = cross_family_fn(dossier) or {}
        if bool(res.get("concur")):
            rows.append(_cross_row("D", "stage_d", "survive", res))
            status = "survived"
        else:
            # Verify the refutation, but the candidate is contested either way; only
            # concur promotes to survived.
            tier, _ = verify.verify_evidence(cursor, res.get("evidence") or [])
            rows.append(_cross_row("D", "stage_d", "kill", res, tier=tier))
            status = "contested"
    else:  # all-abstain / no verified verdict -> contested, no cross-family.
        status = "contested"

    return {"candidate_id": cid, "status": status, "rows": rows}


def _commit_plan(conn, plan: dict) -> None:
    """Wipe stale verdicts, write fresh verdict rows + terminal status — one txn (C3)."""
    cid = plan["candidate_id"]
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("DELETE FROM verdicts WHERE candidate_id = ?", [cid])
        for seq, row in enumerate(plan["rows"]):
            conn.execute(
                "INSERT INTO verdicts (verdict_id, candidate_id, stage, critic_model, "
                "critic_role, verdict, refutation_claim, evidence, evidence_tier, "
                "feasibility, confidence_bucket, raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [f"{cid}:{seq:02d}:{uuid4().hex[:8]}", cid, row["stage"], row["model"],
                 row["role"], row["verdict"], row["refutation_claim"], row["evidence"],
                 row["tier"], row["feasibility"], row["confidence"], row["raw"]],
            )
        conn.execute("UPDATE gap_candidates SET status = ? WHERE candidate_id = ?",
                     [plan["status"], cid])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def assassinate(conn, *, chat_fn=None, cross_family_fn=None, embed_fn=None,
                relevance_fn=None, workers=4, max_steps=8, tool_budget=24_000) -> dict:
    """Run the Stage-A kill gate + cross-family audits over all proposed candidates.

    Default path: per-role critic models from models.STAGE_A_CRITICS (ensemble) and
    the live W5 relevance judge. An injected chat_fn serves BOTH roles (tests), and
    like embed_fn it suppresses the live relevance default — inject relevance_fn
    explicitly to exercise the gate. Verdict rows always stamp STAGE_A_CRITICS[role].

    tool_budget default 24k chars (review C2): worst case one turn overshoots by a
    30k full_text, so peak history ~54k chars ≈ well inside oMLX's 32k-token window.

    Returns aggregate counts and the two C5 hard-gate rates:
    abstain_rate = abstaining Stage-A critic runs / (processed * 2);
    kill_rate    = killed / processed (0.0 when processed == 0).
    """
    default_path = chat_fn is None
    critics = [(role, chat_fn or _make_chat_fn(model), model)
               for role, model in models.STAGE_A_CRITICS.items()]
    cross_family_fn = cross_family_fn or _default_cross_family_fn
    if embed_fn is None and default_path:
        embed_fn = _default_embed_fn  # C3: paraphrase resolution live; tests keep None
    if relevance_fn is None and default_path:
        relevance_fn = _default_relevance_fn  # W5 gate live; tests keep None

    candidates = conn.execute(
        "SELECT candidate_id, template, claim, grounds FROM gap_candidates "
        "WHERE status = 'proposed' ORDER BY candidate_id"
    ).fetchall()
    processed = len(candidates)
    counts = {"killed": 0, "survived": 0, "contested": 0}
    abstain_runs = 0

    def work(cand):
        cid, template, claim, grounds_json = cand
        grounds = json.loads(grounds_json) if isinstance(grounds_json, str) else grounds_json
        cursor = conn.cursor()  # per-thread read-only handle (C3)
        return _process_candidate(cursor, cid, template, claim, grounds or {},
                                  critics, cross_family_fn, max_steps, tool_budget,
                                  embed_fn, relevance_fn=relevance_fn)

    errors = 0
    if candidates:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, cand) for cand in candidates]
            for fut in as_completed(futures):
                try:
                    plan = fut.result()
                except Exception as exc:  # noqa: BLE001 — C1: one candidate must not
                    errors += 1           # abort the run; it stays 'proposed' (resumable)
                    print(f"assassinate: candidate failed, left proposed: {exc!r}",
                          file=sys.stderr)
                    continue
                _commit_plan(conn, plan)  # all writes on the main thread
                counts[plan["status"]] += 1
                abstain_runs += sum(
                    1 for r in plan["rows"] if r["stage"] == "A" and r["verdict"] == "abstain"
                )

    completed = processed - errors
    return {
        "processed": processed,
        "killed": counts["killed"],
        "survived": counts["survived"],
        "contested": counts["contested"],
        "errors": errors,
        "abstain_rate": abstain_runs / (completed * 2) if completed else 0.0,
        "kill_rate": counts["killed"] / completed if completed else 0.0,
    }
