"""Executable spec for litgraph.assassin — the Stage-A kill-gate + cross-family loop.

Contract under test (locked by coordinator):
    litgraph.assassin.assassinate(
        conn, *, chat_fn=None, cross_family_fn=None,
        workers=4, max_steps=8, tool_budget=60_000) -> dict

Only status='proposed' gap_candidates are processed. Per candidate: delete prior
verdicts, run 2 Stage-A critics sequentially (roles critic_1/critic_2), apply the
decision table, optionally resurrect (unanimous kill) or Stage D (survivors), then
write every verdict row + terminal status in one main-thread transaction.

All LLM work is injected. `chat_fn(messages, schema_name, schema) -> dict` returns a
critic action (models.critic_action_json_schema shape); `cross_family_fn(prompt) -> dict`
is the single-shot cross-family critic used for BOTH resurrection and Stage D. Tests
script both and plant a tiny real DB (documents so the tiered verifier + read-only tools
work). No sleeps, no network — deterministic.

Decision table encoded (spec phase2-design.md §Assassin + stage_d_v1.md + resurrect_v1.md):
  - any verified kill                       -> killed
      * unanimous verified kill             -> resurrection (once): overturn -> contested,
                                               no-overturn -> killed
      * single verified kill (other survive/abstain) -> killed, NO resurrection
  - no verified kill AND >=1 verified survive -> Stage D:
      * concur                              -> survived
      * refute (verifiable quote)           -> contested
      * refute (fabricated quote, discarded)-> contested
  - all-abstain / no verified verdict        -> contested (no Stage D, no resurrection)

The module does not exist yet, so every test is expected to fail at import
(ModuleNotFoundError on litgraph.assassin) until it is built.
"""

import json

import pytest

import litgraph.assassin as assassin
from litgraph import db

# ---------------------------------------------------------------------------
# Planted corpus. Verified quotes are verbatim substrings of D1_TEXT; the
# tiered verifier (verify.py) must classify them verified_exact. FABRICATED is
# absent from every planted document, so it must classify as failed.
# ---------------------------------------------------------------------------
D1_TEXT = (
    "Retrieval augmented generation was applied to open domain question answering. "
    "The community summaries method reached parity with flat retrieval on MuSiQue. "
    "No prior work evaluated episodic memory on long horizon embodied agents. "
    "Session boundaries are assumed known at ingest time in the consolidation setting."
)
Q_PARITY = "reached parity with flat retrieval on MuSiQue"
Q_EMBODIED = "No prior work evaluated episodic memory on long horizon embodied agents"
Q_SESSION = "Session boundaries are assumed known at ingest time"
FABRICATED = "this fabricated sentence is absent from every document in the index"


# ---------------------------------------------------------------------------
# DB fixtures — insert rows directly (mirrors test_resolve.py style).
# ---------------------------------------------------------------------------
def new_db():
    return db.connect(":memory:")


def add_doc(conn, doc_id, text, title=None):
    conn.execute(
        "INSERT INTO documents (doc_id, source_type, url, title, full_text) "
        "VALUES (?, 'paper', ?, ?, ?)",
        [doc_id, f"http://example/{doc_id}", title or doc_id, text],
    )


def add_node(conn, node_id, name, node_type="Method"):
    conn.execute(
        "INSERT INTO canonical_nodes (node_id, node_type, canonical_name) VALUES (?, ?, ?)",
        [node_id, node_type, name],
    )


def add_candidate(conn, candidate_id, claim, *, template="T1", grounds=None, status="proposed"):
    conn.execute(
        "INSERT INTO gap_candidates "
        "(candidate_id, template, claim, grounds, score, params, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [candidate_id, template, claim, json.dumps(grounds or {"doc_ids": ["d1"]}),
         1.0, json.dumps({}), status],
    )


def add_verdict(conn, verdict_id, candidate_id, *, stage="A", verdict="kill",
                role="critic_1", model="fake", tier="verified_exact"):
    conn.execute(
        "INSERT INTO verdicts "
        "(verdict_id, candidate_id, stage, critic_model, critic_role, verdict, evidence_tier) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [verdict_id, candidate_id, stage, model, role, verdict, tier],
    )


def status_of(conn, candidate_id):
    return conn.execute(
        "SELECT status FROM gap_candidates WHERE candidate_id=?", [candidate_id]
    ).fetchone()[0]


def verdict_rows(conn, candidate_id):
    """(verdict_id, stage, critic_role, verdict, evidence_tier) ordered stably."""
    return conn.execute(
        "SELECT verdict_id, stage, critic_role, verdict, evidence_tier "
        "FROM verdicts WHERE candidate_id=? ORDER BY created_at, verdict_id",
        [candidate_id],
    ).fetchall()


# ---------------------------------------------------------------------------
# Scripted fakes (injected — no monkeypatching).
# ---------------------------------------------------------------------------
class ScriptedChat:
    """Routes each chat_fn call to a candidate's action queue by matching its claim
    inside the message contents, popping FIFO. Critics run sequentially within a
    candidate, so a claim's queue holds critic_1's actions then critic_2's. A queued
    Exception is raised (simulates the ValueError contract of chat_json_messages)."""

    def __init__(self, scripts):
        # scripts: {claim: [action_dict | Exception, ...]}
        self._q = {claim: list(actions) for claim, actions in scripts.items()}
        self.calls = []  # list of (claim, messages)

    def __call__(self, messages, schema_name, schema):
        blob = "\n".join(
            m.get("content", "") for m in messages if isinstance(m.get("content"), str)
        )
        hits = [claim for claim in self._q if claim in blob]
        if not hits:
            raise AssertionError(f"no scripted claim found in messages: {blob[:200]!r}")
        claim = max(hits, key=len)
        self.calls.append((claim, [dict(m) for m in messages]))
        if not self._q[claim]:
            raise AssertionError(f"script for {claim!r} exhausted")
        action = self._q[claim].pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    def calls_for(self, claim):
        return [msgs for c, msgs in self.calls if c == claim]

    def remaining(self):
        return {c: q for c, q in self._q.items() if q}


class FakeCrossFamily:
    """Records prompts; returns a fixed dict (resurrection: {'overturn':...};
    Stage D: {'concur':..., 'refutation_claim':..., 'evidence':[...]})."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self._result(prompt) if callable(self._result) else self._result


def never_cross_family(prompt):
    raise AssertionError("cross_family_fn must not be called for this candidate")


# ---------------------------------------------------------------------------
# Action builders (models.critic_action_json_schema shape).
# ---------------------------------------------------------------------------
def tool_action(name, **args):
    return {"thought": "investigating", "action": name, "args": args}


def verdict_action(verdict, evidence, *, refutation="stated refutation",
                   feasibility="feasible", confidence="more_probable"):
    return {"thought": "decided", "action": "verdict", "args": {
        "verdict": verdict, "refutation_claim": refutation, "evidence": evidence,
        "feasibility": feasibility, "confidence_bucket": confidence}}


def kill(quote, doc_id="d1"):
    return verdict_action("kill", [{"doc_id": doc_id, "quote": quote}])


def survive(quote, doc_id="d1"):
    return verdict_action("survive", [{"doc_id": doc_id, "quote": quote}])


def bad_action():
    return {"thought": "confused", "action": "not_a_real_tool", "args": {}}


# Non-"TOOL RESULT" user messages beyond the initial prompt are forcing / retry
# messages (locked format: tool results are user msgs prefixed "TOOL RESULT").
def injected_user_messages(messages):
    users = [m for m in messages if m.get("role") == "user"]
    return [m for m in users[1:] if not str(m.get("content", "")).startswith("TOOL RESULT")]


# ===========================================================================
# Decision table — kills
# ===========================================================================
def test_single_verified_kill_kills_without_resurrection():
    """critic_1 kill(verified) + critic_2 survive(verified): any verified kill -> killed,
    and it is NOT unanimous so no resurrection (cross_family_fn must never fire)."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-ALPHA transfer of method X to problem Y is unexplored"
    add_candidate(conn, "c-alpha", claim)
    chat = ScriptedChat({claim: [kill(Q_PARITY), survive(Q_EMBODIED)]})

    result = assassin.assassinate(
        conn, chat_fn=chat, cross_family_fn=never_cross_family, workers=1
    )

    assert status_of(conn, "c-alpha") == "killed"
    assert result["killed"] == 1 and result["processed"] == 1
    rows = verdict_rows(conn, "c-alpha")
    stage_a = [r for r in rows if r[1] == "A"]
    assert {r[2] for r in stage_a} == {"critic_1", "critic_2"}
    assert not any(r[1] == "resurrect" for r in rows), "single kill must not resurrect"


def test_unanimous_kill_triggers_resurrection_overturn_contested():
    """Both critics kill(verified) -> resurrection fires once; overturn=true -> contested.
    Resurrect row: stage='resurrect', role='resurrector', verdict='survive' (kill overturned)."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-BRAVO the contradiction between P and Q is unresolved in-index"
    add_candidate(conn, "c-bravo", claim, template="T4")
    chat = ScriptedChat({claim: [kill(Q_PARITY), kill(Q_EMBODIED)]})
    cf = FakeCrossFamily({"overturn": True, "reason": "the cited span concerns a different setting"})

    result = assassin.assassinate(conn, chat_fn=chat, cross_family_fn=cf, workers=1)

    assert status_of(conn, "c-bravo") == "contested"
    assert result["contested"] == 1
    assert len(cf.calls) == 1, "resurrection is single-shot, exactly once per candidate"
    res = [r for r in verdict_rows(conn, "c-bravo") if r[1] == "resurrect"]
    assert len(res) == 1
    assert res[0][2] == "resurrector" and res[0][3] == "survive"


def test_unanimous_kill_resurrection_no_overturn_stays_killed():
    """Both critics kill(verified) -> resurrection; overturn=false -> killed.
    Resurrect row verdict='kill'."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-CHARLIE the structural hole between clusters is real"
    add_candidate(conn, "c-charlie", claim, template="T2")
    chat = ScriptedChat({claim: [kill(Q_PARITY), kill(Q_SESSION)]})
    cf = FakeCrossFamily({"overturn": False, "reason": "both kills hold against the dossier"})

    result = assassin.assassinate(conn, chat_fn=chat, cross_family_fn=cf, workers=1)

    assert status_of(conn, "c-charlie") == "killed"
    assert result["killed"] == 1
    assert len(cf.calls) == 1
    res = [r for r in verdict_rows(conn, "c-charlie") if r[1] == "resurrect"]
    assert len(res) == 1 and res[0][3] == "kill"


# ===========================================================================
# Decision table — Stage D (survivors)
# ===========================================================================
def test_stage_d_concur_promotes_to_survived():
    """>=1 verified survive, no kill -> Stage D; concur -> survived. Stage D row:
    stage='D', role='stage_d', verdict='survive'."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-DELTA assumption Z is load-bearing and unexamined"
    add_candidate(conn, "c-delta", claim, template="T3")
    chat = ScriptedChat({claim: [survive(Q_SESSION), survive(Q_PARITY)]})
    cf = FakeCrossFamily({"concur": True, "refutation_claim": "", "evidence": []})

    result = assassin.assassinate(conn, chat_fn=chat, cross_family_fn=cf, workers=1)

    assert status_of(conn, "c-delta") == "survived"
    assert result["survived"] == 1
    assert len(cf.calls) == 1, "Stage D audits every survivor exactly once"
    d_rows = [r for r in verdict_rows(conn, "c-delta") if r[1] == "D"]
    assert len(d_rows) == 1
    assert d_rows[0][2] == "stage_d" and d_rows[0][3] == "survive"


def test_stage_d_refute_with_verifiable_quote_marks_contested():
    """Stage D non-concur whose quote verifies against the dossier -> contested (not
    survived). Stage D row present with verdict='kill'."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-ECHO no method addresses subproblem W"
    add_candidate(conn, "c-echo", claim)
    chat = ScriptedChat({claim: [survive(Q_SESSION), survive(Q_EMBODIED)]})
    cf = FakeCrossFamily({
        "concur": False,
        "refutation_claim": "the gap is already closed",
        "evidence": [{"doc_id": "d1", "quote": Q_PARITY}],  # verbatim -> verifies
    })

    result = assassin.assassinate(conn, chat_fn=chat, cross_family_fn=cf, workers=1)

    assert status_of(conn, "c-echo") == "contested"
    assert result["contested"] == 1
    d_rows = [r for r in verdict_rows(conn, "c-echo") if r[1] == "D"]
    assert len(d_rows) == 1 and d_rows[0][3] == "kill"


def test_stage_d_refute_with_fabricated_quote_marks_contested():
    """Stage D non-concur whose quote fails verification: the refutation is discarded but
    the survivor is NOT promoted -> contested (spec stage_d_v1). Stage D row still written."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-FOXTROT dataset D has never been used to test method M"
    add_candidate(conn, "c-foxtrot", claim)
    chat = ScriptedChat({claim: [survive(Q_SESSION), survive(Q_EMBODIED)]})
    cf = FakeCrossFamily({
        "concur": False,
        "refutation_claim": "closed by a paper I recall",
        "evidence": [{"doc_id": "d1", "quote": FABRICATED}],  # not in any doc -> fails
    })

    result = assassin.assassinate(conn, chat_fn=chat, cross_family_fn=cf, workers=1)

    assert status_of(conn, "c-foxtrot") == "contested", (
        "failed cross-family refutation is discarded, but promotion to survived is withheld"
    )
    assert result["contested"] == 1
    assert any(r[1] == "D" for r in verdict_rows(conn, "c-foxtrot")), "Stage D row recorded"


# ===========================================================================
# Decision table — abstain / contested
# ===========================================================================
def test_all_abstain_is_contested_and_skips_cross_family():
    """Both critics abstain (malformed twice each) -> contested; no verified survive so no
    Stage D, no unanimous kill so no resurrection: cross_family_fn never called (C5)."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-GOLF everything abstains here"
    add_candidate(conn, "c-golf", claim)
    chat = ScriptedChat({claim: [
        bad_action(), bad_action(),            # critic_1: malformed -> retry -> abstain
        ValueError("unparseable"), ValueError("unparseable"),  # critic_2: same
    ]})

    result = assassin.assassinate(conn, chat_fn=chat, cross_family_fn=FakeCrossFamily(None), workers=1)

    assert status_of(conn, "c-golf") == "contested"
    assert result["contested"] == 1
    stage_a = [r for r in verdict_rows(conn, "c-golf") if r[1] == "A"]
    assert len(stage_a) == 2 and {r[3] for r in stage_a} == {"abstain"}
    assert chat.remaining() == {}, "each critic retries exactly once then abstains"


# ===========================================================================
# Malformed-action retry contract
# ===========================================================================
def test_malformed_action_retries_once_then_abstains():
    """ValueError from chat_fn AND unknown-action both count as malformed: one retry naming
    the error, second failure -> abstain (verdict row verdict='abstain')."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-HOTEL malformed then malformed"
    add_candidate(conn, "c-hotel", claim)
    chat = ScriptedChat({claim: [
        ValueError("bad json"), ValueError("bad json again"),   # critic_1 -> abstain
        bad_action(), bad_action(),                              # critic_2 -> abstain
    ]})

    assassin.assassinate(conn, chat_fn=chat, cross_family_fn=FakeCrossFamily(None), workers=1)

    stage_a = [r for r in verdict_rows(conn, "c-hotel") if r[1] == "A"]
    assert len(stage_a) == 2 and {r[3] for r in stage_a} == {"abstain"}
    # critic_1's retry turn names the error it is retrying.
    c1_second = chat.calls_for(claim)[1]
    assert injected_user_messages(c1_second), "retry turn appends an error-naming message"


def test_malformed_action_recovers_on_retry():
    """A single malformed action followed by a valid verdict is accepted (not an abstain)."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-INDIA malformed then a clean verdict"
    add_candidate(conn, "c-india", claim)
    chat = ScriptedChat({claim: [
        bad_action(), kill(Q_PARITY),   # critic_1: recovers into a verified kill
        survive(Q_EMBODIED),            # critic_2
    ]})

    assassin.assassinate(conn, chat_fn=chat, cross_family_fn=never_cross_family, workers=1)

    assert status_of(conn, "c-india") == "killed"
    c1_rows = [r for r in verdict_rows(conn, "c-india") if r[2] == "critic_1"]
    assert c1_rows and c1_rows[0][3] == "kill", "recovered verdict is honored"


# ===========================================================================
# Evidence verification -> retry -> abstain (both verdict directions)
# ===========================================================================
def test_failed_evidence_kill_retries_then_abstains_and_falls_to_stage_d():
    """kill with a fabricated quote: verifier rejects -> one retry naming the violation ->
    still failed -> critic ABSTAINS. With the other critic a verified survive, the candidate
    has no verified kill + >=1 verified survive -> Stage D path runs."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-JULIET a kill on fabricated evidence must abstain"
    add_candidate(conn, "c-juliet", claim)
    chat = ScriptedChat({claim: [
        kill(FABRICATED), kill(FABRICATED),   # critic_1: reject -> retry -> abstain
        survive(Q_PARITY),                     # critic_2: verified survive
    ]})
    cf = FakeCrossFamily({"concur": True, "refutation_claim": "", "evidence": []})

    assassin.assassinate(conn, chat_fn=chat, cross_family_fn=cf, workers=1)

    c1_rows = [r for r in verdict_rows(conn, "c-juliet") if r[2] == "critic_1"]
    assert c1_rows and c1_rows[0][3] == "abstain"
    assert len(cf.calls) == 1, "verified survive + no kill -> Stage D fires"
    assert status_of(conn, "c-juliet") == "survived"
    # The retry turn names the evidence violation.
    retry_msgs = injected_user_messages(chat.calls_for(claim)[1])
    joined = " ".join(str(m.get("content", "")) for m in retry_msgs).lower()
    assert retry_msgs and any(k in joined for k in ("evidence", "verif", "quote", "not found"))


def test_failed_evidence_survive_retries_then_abstains():
    """survive with a fabricated nearest-miss quote: same reject -> retry -> abstain. Both
    critics do it -> no verified verdict at all -> contested."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-KILO a survive on fabricated evidence must abstain"
    add_candidate(conn, "c-kilo", claim)
    chat = ScriptedChat({claim: [
        survive(FABRICATED), survive(FABRICATED),   # critic_1 -> abstain
        survive(FABRICATED), survive(FABRICATED),   # critic_2 -> abstain
    ]})

    assassin.assassinate(conn, chat_fn=chat, cross_family_fn=never_cross_family, workers=1)

    stage_a = [r for r in verdict_rows(conn, "c-kilo") if r[1] == "A"]
    assert len(stage_a) == 2 and {r[3] for r in stage_a} == {"abstain"}
    assert status_of(conn, "c-kilo") == "contested"


# ===========================================================================
# Tool loop termination — max_steps and tool_budget
# ===========================================================================
def test_max_steps_exhaustion_forces_a_final_verdict_turn():
    """A critic that keeps calling tools is forced to a verdict turn after max_steps tool
    calls; its scripted verdict on that forcing turn is accepted."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    add_doc(conn, "d-big", "short neighborhood text for tool output")  # small: 8x < 60k budget
    claim = "GAP-LIMA always calls tools until forced"
    add_candidate(conn, "c-lima", claim, grounds={"doc_ids": ["d-big", "d1"]})
    chat = ScriptedChat({claim: [
        *[tool_action("full_text", doc_id="d-big") for _ in range(8)],  # 8 tool calls
        kill(Q_PARITY),                                                  # forced verdict
        survive(Q_EMBODIED),                                            # critic_2
    ]})

    assassin.assassinate(conn, chat_fn=chat, cross_family_fn=never_cross_family,
                         workers=1, max_steps=8)

    assert status_of(conn, "c-lima") == "killed", "forced verdict is accepted"
    assert chat.remaining() == {}, "exactly 8 tool turns + 1 forcing turn consumed"
    last = chat.calls_for(claim)[8]  # the 9th call for this claim == the forcing turn
    assert injected_user_messages(last), "a forcing message is injected before the verdict turn"


def test_tool_budget_breach_forces_verdict_early():
    """Accumulated tool output over tool_budget forces a verdict on the next turn, well
    before max_steps. One oversized tool result trips a tiny budget."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    add_doc(conn, "d-big", "X" * 400)  # one full_text result exceeds tool_budget=100
    claim = "GAP-MIKE trips the tool budget on the first tool call"
    add_candidate(conn, "c-mike", claim, grounds={"doc_ids": ["d-big", "d1"]})
    chat = ScriptedChat({claim: [
        tool_action("full_text", doc_id="d-big"),  # 1 tool call -> budget breached
        kill(Q_PARITY),                             # forced verdict
        survive(Q_EMBODIED),                        # critic_2
    ]})

    assassin.assassinate(conn, chat_fn=chat, cross_family_fn=never_cross_family,
                         workers=1, max_steps=8, tool_budget=100)

    assert status_of(conn, "c-mike") == "killed"
    assert chat.remaining() == {}, "budget forces the verdict after a single tool call"
    forcing_turn = chat.calls_for(claim)[1]  # 2nd critic_1 call == forcing turn
    assert injected_user_messages(forcing_turn), "a forcing message precedes the verdict"


# ===========================================================================
# Resume semantics (W6)
# ===========================================================================
def test_resume_ignores_terminal_candidates_and_wipes_stale_verdicts():
    """assassinate touches only status='proposed': a terminal candidate + its verdicts are
    untouched; a proposed candidate's stale verdicts are deleted before new ones are written."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    # Terminal candidate — must be left completely alone.
    add_candidate(conn, "c-done", "GAP-DONE already killed last run", status="killed")
    add_verdict(conn, "v-keep", "c-done", verdict="kill")
    # Proposed candidate carrying a stale verdict from an aborted prior run.
    claim = "GAP-NOVEMBER proposed with a stale verdict row"
    add_candidate(conn, "c-new", claim)
    add_verdict(conn, "v-stale", "c-new", verdict="abstain", tier=None)
    chat = ScriptedChat({claim: [kill(Q_PARITY), survive(Q_EMBODIED)]})

    result = assassin.assassinate(conn, chat_fn=chat, cross_family_fn=never_cross_family, workers=1)

    assert result["processed"] == 1, "only the proposed candidate is processed"
    # Terminal candidate untouched.
    assert status_of(conn, "c-done") == "killed"
    done_rows = verdict_rows(conn, "c-done")
    assert [r[0] for r in done_rows] == ["v-keep"], "terminal candidate's verdicts untouched"
    # Stale verdict wiped; fresh Stage-A rows written.
    new_ids = [r[0] for r in verdict_rows(conn, "c-new")]
    assert "v-stale" not in new_ids, "stale verdicts deleted before re-processing"
    assert new_ids, "fresh verdicts written for the re-processed candidate"
    assert status_of(conn, "c-new") == "killed"


# ===========================================================================
# Aggregate counts / rates
# ===========================================================================
def test_counts_and_rates_over_three_candidates():
    """A 3-candidate run: one killed, one survived (Stage D concur), one contested (abstain).
    Verifies processed/killed/survived/contested and both rates."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)

    claim_k = "GAP-KILLED single verified kill"
    claim_s = "GAP-SURVIVED both survive then concur"
    claim_c = "GAP-CONTESTED both abstain"
    add_candidate(conn, "c-k", claim_k)
    add_candidate(conn, "c-s", claim_s)
    add_candidate(conn, "c-c", claim_c)

    chat = ScriptedChat({
        claim_k: [kill(Q_PARITY), survive(Q_EMBODIED)],       # -> killed (single kill)
        claim_s: [survive(Q_SESSION), survive(Q_PARITY)],     # -> Stage D -> survived
        claim_c: [bad_action(), bad_action(), bad_action(), bad_action()],  # -> contested
    })
    cf = FakeCrossFamily({"concur": True, "refutation_claim": "", "evidence": []})

    result = assassin.assassinate(conn, chat_fn=chat, cross_family_fn=cf, workers=1)

    assert result["processed"] == 3
    assert result["killed"] == 1
    assert result["survived"] == 1
    assert result["contested"] == 1
    assert result["kill_rate"] == pytest.approx(1 / 3)
    # abstaining Stage-A critic runs (2, from c-c) / total Stage-A critic runs (6).
    assert result["abstain_rate"] == pytest.approx(2 / 6)
    assert len(cf.calls) == 1, "only the survivor invokes cross-family (Stage D)"

    assert status_of(conn, "c-k") == "killed"
    assert status_of(conn, "c-s") == "survived"
    assert status_of(conn, "c-c") == "contested"


def test_verified_verdict_rows_record_evidence_tier():
    """Every verified Stage-A verdict row carries a verified_* evidence_tier; abstains carry
    verdict='abstain'."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-OSCAR tiers are recorded on verified verdicts"
    add_candidate(conn, "c-oscar", claim)
    chat = ScriptedChat({claim: [kill(Q_PARITY), survive(Q_EMBODIED)]})

    assassin.assassinate(conn, chat_fn=chat, cross_family_fn=never_cross_family, workers=1)

    stage_a = [r for r in verdict_rows(conn, "c-oscar") if r[1] == "A"]
    assert len(stage_a) == 2
    for _vid, _stage, _role, verdict, tier in stage_a:
        assert verdict in ("kill", "survive")
        assert tier in ("verified_exact", "verified_fuzzy"), "verified verdicts record a tier"


def test_C1_transport_error_from_chat_fn_degrades_to_abstain(tmp_path):
    """A non-ValueError exception (oMLX timeout etc.) must not abort the run —
    retry once, then abstain; the candidate still reaches a terminal disposition."""
    from litgraph import assassin, db

    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO documents (doc_id, source_type, url, title, full_text) "
        "VALUES ('d1', 'paper', 'u', 't', 'body text')"
    )
    conn.execute(
        "INSERT INTO gap_candidates (candidate_id, template, claim, grounds, status) "
        "VALUES ('T4:aaaaaaaaaaaa', 'T4', 'claim', '{\"node_ids\": [], \"doc_ids\": []}', "
        "'proposed')"
    )

    class Boom(RuntimeError):
        pass

    def chat_fn(messages, schema_name, schema):
        raise Boom("simulated transport failure")

    called = []
    result = assassin.assassinate(
        conn, chat_fn=chat_fn, cross_family_fn=lambda p: called.append(p) or {}, workers=1
    )
    assert result["processed"] == 1
    assert result["contested"] == 1  # both critics abstain -> contested
    assert result["errors"] == 0
    assert not called  # all-abstain never reaches cross-family
    status = conn.execute(
        "SELECT status FROM gap_candidates WHERE candidate_id = 'T4:aaaaaaaaaaaa'"
    ).fetchone()[0]
    assert status == "contested"


# ===========================================================================
# W5 relevance gate + Stage-A ensemble (2026-07-17)
# ===========================================================================
class RecordingRelevance:
    """Scripted relevance_fn: pops verdicts FIFO, records every call."""

    def __init__(self, verdicts):
        self._q = list(verdicts)
        self.calls = []  # (claim, refutation_claim, evidence_items)

    def __call__(self, claim, refutation_claim, evidence):
        self.calls.append((claim, refutation_claim, evidence))
        return self._q.pop(0)


def critic_models(conn, candidate_id):
    return conn.execute(
        "SELECT critic_role, critic_model FROM verdicts "
        "WHERE candidate_id=? AND stage='A' ORDER BY critic_role", [candidate_id]
    ).fetchall()


def test_W5_supported_kill_stands_and_judge_sees_context():
    """Relevance-supported kill passes through unchanged; the judge receives the
    candidate claim, the refutation claim, and evidence items with source context."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-REL-A transfer of method X to problem Y is unexplored"
    add_candidate(conn, "c-rel-a", claim)
    chat = ScriptedChat({claim: [
        verdict_action("kill", [{"doc_id": "d1", "quote": Q_PARITY}],
                       refutation="parity already shown on MuSiQue"),
        survive(Q_EMBODIED),
    ]})
    rel = RecordingRelevance([{"supported": True, "reason": "quote states the parity result"}])

    assassin.assassinate(conn, chat_fn=chat, cross_family_fn=never_cross_family,
                         relevance_fn=rel, workers=1)

    assert status_of(conn, "c-rel-a") == "killed"
    assert len(rel.calls) == 1, "judge fires once per kill verdict, never for survive"
    got_claim, got_refutation, items = rel.calls[0]
    assert got_claim == claim
    assert got_refutation == "parity already shown on MuSiQue"
    assert items[0]["doc_id"] == "d1" and items[0]["quote"] == Q_PARITY
    assert Q_PARITY in (items[0]["context"] or "")


def test_W5_unsupported_kill_retries_then_abstains():
    """Unsupported relevance consumes the single evidence-retry budget: the critic is
    told why, may re-issue once, and a second unsupported ruling abstains it."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-REL-B assumption Z is only examined in one cluster"
    add_candidate(conn, "c-rel-b", claim, template="T3")
    chat = ScriptedChat({claim: [
        kill(Q_PARITY), kill(Q_PARITY),   # critic_1: kill, retry kill -> abstain
        survive(Q_EMBODIED),              # critic_2: verified survive
    ]})
    rel = RecordingRelevance([
        {"supported": False, "reason": "quote is about retrieval parity, not Z"},
        {"supported": False, "reason": "same irrelevant quote"},
    ])
    cf = FakeCrossFamily({"concur": True, "reason": "gap holds"})

    result = assassin.assassinate(conn, chat_fn=chat, cross_family_fn=cf,
                                  relevance_fn=rel, workers=1)

    assert status_of(conn, "c-rel-b") == "survived"  # abstain + survive -> Stage D concur
    assert result["killed"] == 0
    assert len(rel.calls) == 2
    critic_1_msgs = chat.calls_for(claim)[1]  # critic_1's retry turn
    retry = injected_user_messages(critic_1_msgs)
    assert any("Relevance check failed" in str(m["content"]) for m in retry)
    assert any("not Z" in str(m["content"]) for m in retry), "judge reason surfaced"
    rows = verdict_rows(conn, "c-rel-b")
    assert ("A", "critic_1", "abstain") in {(r[1], r[2], r[3]) for r in rows}


def test_W5_judge_exception_fails_open():
    """A crashing relevance judge must not block a provenance-verified kill (fail-open):
    infra flakiness must not convert kills into abstains."""
    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-REL-C the contradiction between P and Q is unresolved"
    add_candidate(conn, "c-rel-c", claim, template="T4")
    chat = ScriptedChat({claim: [kill(Q_PARITY), survive(Q_EMBODIED)]})

    def broken_judge(claim_, refutation_, evidence_):
        raise RuntimeError("judge transport failure")

    assassin.assassinate(conn, chat_fn=chat, cross_family_fn=never_cross_family,
                         relevance_fn=broken_judge, workers=1)

    assert status_of(conn, "c-rel-c") == "killed"


def test_ensemble_stage_a_rows_stamp_per_role_models():
    """Verdict rows stamp models.STAGE_A_CRITICS[role] (cross-family ensemble):
    critic_1 = local ADJUDICATION_MODEL, critic_2 = the DeepSeek critic."""
    from litgraph import models

    conn = new_db()
    add_doc(conn, "d1", D1_TEXT)
    claim = "GAP-ENS-A method M was never applied to problem B"
    add_candidate(conn, "c-ens-a", claim)
    chat = ScriptedChat({claim: [kill(Q_PARITY), kill(Q_EMBODIED)]})
    cf = FakeCrossFamily({"overturn": False, "reason": "kills are sound"})

    assassin.assassinate(conn, chat_fn=chat, cross_family_fn=cf, workers=1)

    assert dict(critic_models(conn, "c-ens-a")) == dict(models.STAGE_A_CRITICS)
    assert models.STAGE_A_CRITICS["critic_1"] == models.ADJUDICATION_MODEL
    assert models.STAGE_A_CRITICS["critic_2"].startswith("deepseek")


# ---------------------------------------------------------------------------
# Auditor-output parsing: the claude CLI result field may carry prose around the
# verdict JSON (live failure 2026-07-17/18: parse errors silently became
# non-concur -> contested).
# ---------------------------------------------------------------------------
def test_first_json_tolerates_prose_and_fences():
    obj = {"concur": False, "reason": "x"}
    plain = json.dumps(obj)
    assert assassin._first_json(plain) == obj
    assert assassin._first_json(f"Looking at this dossier.\n\n{plain}") == obj
    assert assassin._first_json(f"{plain}\n\nExplanation: because reasons.") == obj
    assert assassin._first_json(f"```json\n{plain}\n```") == obj
    assert assassin._first_json(
        'Prose with {broken then real: ' + plain) == obj


def test_first_json_raises_without_object():
    import pytest as _pytest
    with _pytest.raises(ValueError):
        assassin._first_json("no json here at all")
