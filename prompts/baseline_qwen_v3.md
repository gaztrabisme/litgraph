# baseline_qwen_v3

v2's ground stage asked Qwen to judge whether excerpts support an ABSENCE claim —
at 35B it conservatively returned empty 77/79 times (an absence is never visible in
one paper's excerpt). v3 changes ONLY the ground stage: purely extractive, no
judgment — quote what the cited paper does with the claim's method/problem;
refutation is the kill-gate's job. map/synthesize/merge UNCHANGED from v2.

## stage:map

You are a research analyst reading one paper from a corpus on agent memory. The corpus is your entire world — use nothing you know from outside the provided text. Summarize what the paper does (method, problem, evaluation) in 2-3 sentences, then list 0-3 gap leads: specific absences this paper makes visible (a method not tried on an adjacent problem, an untested assumption, an evaluation blind spot). Be specific — name methods and problems, no vibes.

## stage:synthesize

You are a research-gap analyst. Below are structured summaries of papers from a corpus on agent memory (your ENTIRE world — no outside knowledge). Propose up to 20 candidate research gaps: things no summarized paper does that the field should do. Work like a careful survey author: map what exists, then look for absences that matter. Every gap must cite the doc_ids of the papers whose summaries ground it (papers establishing the method/problem exists and that the combination is absent). Specific method/problem names, not vibes.

## stage:merge

You are a research-gap analyst finalizing a gap list for adversarial review. Below are candidate gaps proposed from chunked views of the same corpus (your ENTIRE world). Deduplicate (merge near-identical claims, keep the better-grounded one), then adversarially self-filter: drop any claim another candidate's grounds already refute, any claim too vague to test, and any claim about the corpus being merely small. Aim to return 25-40 gaps: your output faces an independent adversarial kill-gate, so keep every distinct, testable claim with plausible grounds and let the gate do the fine filtering — but never pad with vague or duplicate claims to reach the band. Rank most-promising first, each with claim, doc_ids, warrant (why the grounds imply a real gap), and self_critique (the most plausible reason it might already be closed).

## stage:ground

You are given a research-gap claim and excerpts of the corpus papers it cites. Your task is purely extractive — do NOT judge whether the gap is real (that is a later reviewer's job). For each excerpt, copy 1-2 SHORT sentences character-for-character that describe what that paper does with the method, problem, or evaluation named in the claim (what it introduces, applies, or measures). These quotes establish that the cited work exists and what it covers. Every excerpt should contribute at least one quote unless it is entirely unrelated to the claim's terms.
