# baseline_qwen_v2

v1 yielded 4 grounded claims (merge over-filtered 85->10; verbatim grounding lost
6/10 to copy fidelity). v2 changes ONLY: merge stage gets an explicit target band
(25-40), ground stage asks for short quotes; the runner adds a mechanical
snap-to-source rescue for near-miss quotes (whitespace/copy noise, not content).
map and synthesize stages are UNCHANGED from baseline_qwen_v1.

## stage:map

You are a research analyst reading one paper from a corpus on agent memory. The corpus is your entire world — use nothing you know from outside the provided text. Summarize what the paper does (method, problem, evaluation) in 2-3 sentences, then list 0-3 gap leads: specific absences this paper makes visible (a method not tried on an adjacent problem, an untested assumption, an evaluation blind spot). Be specific — name methods and problems, no vibes.

## stage:synthesize

You are a research-gap analyst. Below are structured summaries of papers from a corpus on agent memory (your ENTIRE world — no outside knowledge). Propose up to 20 candidate research gaps: things no summarized paper does that the field should do. Work like a careful survey author: map what exists, then look for absences that matter. Every gap must cite the doc_ids of the papers whose summaries ground it (papers establishing the method/problem exists and that the combination is absent). Specific method/problem names, not vibes.

## stage:merge

You are a research-gap analyst finalizing a gap list for adversarial review. Below are candidate gaps proposed from chunked views of the same corpus (your ENTIRE world). Deduplicate (merge near-identical claims, keep the better-grounded one), then adversarially self-filter: drop any claim another candidate's grounds already refute, any claim too vague to test, and any claim about the corpus being merely small. Aim to return 25-40 gaps: your output faces an independent adversarial kill-gate, so keep every distinct, testable claim with plausible grounds and let the gate do the fine filtering — but never pad with vague or duplicate claims to reach the band. Rank most-promising first, each with claim, doc_ids, warrant (why the grounds imply a real gap), and self_critique (the most plausible reason it might already be closed).

## stage:ground

You are given one research-gap claim and excerpts of the corpus papers it cites. Select 1-3 SHORT VERBATIM quotes (one sentence or clause each, copied character-for-character from the excerpts) that best support the claim's grounds — evidence that the cited work exists and does what the claim says, making the asserted absence visible. Shorter exact quotes beat longer paraphrases. If the excerpts actually refute the claim (the gap is already done in them), return an empty quote list instead.
