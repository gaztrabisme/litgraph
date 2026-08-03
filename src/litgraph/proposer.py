"""Graph-tools proposer loop (4b-ext): an LLM proposes gaps WITH the graph as tools.

The inverse of the assassin: same four read-only tools via assassin._dispatch, but
the terminal action is `propose` (emit one CGW-shaped claim, session continues)
or `done`. No evidence verification here — proposals face extractive grounding,
verbatim verification, and the full kill-gate downstream. Scaffolding (budget,
forcing, single malformed retry) mirrors assassin._run_critic; kept separate
because the critic loop is verdict-coupled (see plan 2026-07-18) — extract a
shared helper only when a third loop appears.
"""

import json

from pydantic import ValidationError

from litgraph import assassin, models

SCHEMA_NAME = "proposer_action"
_FORCE_MSG = ("Tool budget exhausted. Propose from what you have already seen: emit "
              "your remaining `propose` actions now, then finish with `done`.")
_ACK_MSG = ("Proposal {n} recorded. Explore further, `propose` another distinct gap, "
            "or emit `done`.")


def run_session(cursor, system: str, user: str, chat_fn, *,
                max_steps: int = 20, tool_budget: int = 24_000, embed_fn=None,
                allowed_tools=None, max_proposals: int = 4,
                dispatch_fn=None) -> list[dict]:
    """Run one proposer session; return validated proposal dicts (may be empty)."""
    if dispatch_fn is None:
        dispatch_fn = assassin._dispatch
    schema = models.proposer_action_json_schema()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    proposals: list[dict] = []
    tool_calls = 0
    tool_chars = 0
    retried_malformed = False
    forced = False

    for _ in range(max_steps + 2 * max_proposals + 4):
        if not forced and (tool_calls >= max_steps or tool_chars > tool_budget):
            forced = True
            messages.append({"role": "user", "content": _FORCE_MSG})

        try:
            action = chat_fn(messages, SCHEMA_NAME, schema)
        except Exception as exc:  # noqa: BLE001 — one flaky call must not end the session
            if not retried_malformed:
                retried_malformed = True
                messages.append({"role": "user", "content":
                                 f"Your last response could not be parsed ({exc}). "
                                 "Respond with exactly one JSON action."})
                continue
            return proposals

        name = action.get("action") if isinstance(action, dict) else None

        if name == "done":
            return proposals

        if name == "propose":
            try:
                args = models.ProposalArgs.model_validate(action.get("args", {}))
            except (ValidationError, ValueError) as exc:
                if not retried_malformed:
                    retried_malformed = True
                    messages.append({"role": "user", "content":
                                     f"Your proposal was malformed ({exc}). Emit a valid "
                                     "`propose` action with all required fields."})
                    continue
                return proposals
            proposals.append(args.model_dump())
            if len(proposals) >= max_proposals:
                return proposals
            messages.append({"role": "assistant",
                             "content": json.dumps(action, ensure_ascii=False)})
            messages.append({"role": "user",
                             "content": _ACK_MSG.format(n=len(proposals))})
            continue

        if name in (allowed_tools or models.CRITIC_TOOLS) and not forced:
            result = dispatch_fn(cursor, name, action.get("args", {}), embed_fn)
            result_json = json.dumps(result, ensure_ascii=False)
            tool_calls += 1
            tool_chars += len(result_json)
            messages.append({"role": "assistant",
                             "content": json.dumps(action, ensure_ascii=False)})
            messages.append({"role": "user",
                             "content": f"TOOL RESULT ({name}): {result_json}"})
            continue

        # Unknown action, or a tool call after the forcing turn.
        if not retried_malformed:
            retried_malformed = True
            note = (_FORCE_MSG if forced else
                    f"Unknown or unusable action {name!r}. Use a tool, `propose`, or `done`.")
            messages.append({"role": "user", "content": note})
            continue
        return proposals

    return proposals
