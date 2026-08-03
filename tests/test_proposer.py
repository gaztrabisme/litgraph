"""Executable spec for litgraph.proposer — the graph-tools proposer loop (4b-ext).

Contract:
    proposer.run_session(cursor, system, user, chat_fn, *, max_steps=20,
                         tool_budget=24_000, embed_fn=None, allowed_tools=None,
                         max_proposals=4, dispatch_fn=None) -> list[dict]
        ReAct loop: tool actions dispatched read-only; 'propose' validates
        models.ProposalArgs and accumulates; 'done' (or budget/hard-cap) ends the
        session. Malformed turns share one retry; budget breach forces proposals.
Returned proposals: {claim, doc_ids, warrant, self_critique} dicts.
"""

from litgraph import models, proposer


def scripted(actions):
    """chat_fn fake yielding one action per call; records message snapshots."""
    calls = {"n": 0, "messages": []}

    def chat_fn(messages, schema_name, schema):
        calls["messages"].append([m["content"] for m in messages])
        action = actions[calls["n"]]
        calls["n"] += 1
        return action

    return chat_fn, calls


PROPOSE = {"thought": "t", "action": "propose", "args": {
    "claim": "Method M has not been applied to problem P",
    "doc_ids": ["d1"], "warrant": "w", "self_critique": "s"}}
DONE = {"thought": "t", "action": "done", "args": {}}
TOOL = {"thought": "t", "action": "full_text", "args": {"doc_id": "d1"}}


def test_schema_has_propose_and_done_actions():
    schema = models.proposer_action_json_schema()
    assert set(schema["properties"]["action"]["enum"]) == {
        *models.CRITIC_TOOLS, "propose", "done"}


def test_tool_then_propose_then_done_collects_one_proposal():
    dispatched = []

    def fake_dispatch(cursor, name, args, embed_fn=None):
        dispatched.append((name, args))
        return {"text": "paper text"}

    chat_fn, calls = scripted([TOOL, PROPOSE, DONE])
    out = proposer.run_session(None, "SYS", "USER", chat_fn,
                               dispatch_fn=fake_dispatch)
    assert dispatched == [("full_text", {"doc_id": "d1"})]
    assert [p["claim"] for p in out] == [PROPOSE["args"]["claim"]]
    # the tool result reached the next turn's messages
    assert any("TOOL RESULT (full_text)" in c for c in calls["messages"][1])


def test_budget_breach_forces_proposals_and_refuses_tools():
    def fake_dispatch(cursor, name, args, embed_fn=None):
        return {"big": "x" * 10}

    chat_fn, calls = scripted([TOOL, TOOL, PROPOSE, DONE])
    out = proposer.run_session(None, "SYS", "USER", chat_fn, max_steps=1,
                               dispatch_fn=fake_dispatch)
    assert len(out) == 1
    # after the forcing message, the second TOOL was refused (nudge, not dispatch)
    forced_turns = [c for c in calls["messages"] if any("propose" in m.lower()
                    and "finish" in m.lower() for m in c)]
    assert forced_turns


def test_malformed_propose_retries_once_then_ends_with_collected():
    bad = {"thought": "t", "action": "propose", "args": {"claim": ""}}
    chat_fn, _ = scripted([PROPOSE, bad, bad])
    out = proposer.run_session(None, "SYS", "USER", chat_fn,
                               dispatch_fn=lambda *a, **k: {})
    assert len(out) == 1  # first good proposal kept, session ended on 2nd malformed


def test_max_proposals_caps_session():
    p2 = {**PROPOSE, "args": {**PROPOSE["args"], "claim": "second claim"}}
    p3 = {**PROPOSE, "args": {**PROPOSE["args"], "claim": "third claim"}}
    chat_fn, calls = scripted([PROPOSE, p2, p3])
    out = proposer.run_session(None, "SYS", "USER", chat_fn, max_proposals=2,
                               dispatch_fn=lambda *a, **k: {})
    assert [p["claim"] for p in out] == [PROPOSE["args"]["claim"], "second claim"]
    assert calls["n"] == 2  # third call never made


def test_chat_fn_exception_returns_collected_after_retry():
    calls = {"n": 0}

    def chat_fn(messages, schema_name, schema):
        calls["n"] += 1
        if calls["n"] == 1:
            return PROPOSE
        raise RuntimeError("transport")

    out = proposer.run_session(None, "SYS", "USER", chat_fn,
                               dispatch_fn=lambda *a, **k: {})
    assert len(out) == 1
    assert calls["n"] == 3  # initial + retry-after-error + final failure
