"""Tiered evidence verifier — exact | fuzzy | failed (W1).

Live calibration drove the tiering: Phase 1's own extraction quotes pass an exact
normalized substring test only 40.7% of the time (models stitch spans with ellipses
and lightly rewrite), yet 98% of the failures have ≥99% content-token containment —
paraphrase, not fabrication (gotchas.md 2026-07-16). So a quote earns:

    verified_exact   normalized quote is a substring of the normalized doc
    verified_fuzzy   ellipsis-split segments each best-window difflib ≥0.85, OR
                     content-token containment ≥0.95 with ≥8 quote tokens
    failed           neither (or empty quote / unknown doc)

Shared by the assassin kill gate, the gaps report, and these tests.
"""

import re
import unicodedata
from difflib import SequenceMatcher

# Punctuation/ligature folds NFKC does not already perform (curly quotes and the
# dash family). Ellipsis, ligatures and nbsp are covered by NFKC too, but listed
# for a single source of truth and defence against non-decomposing inputs.
_PUNCT_MAP = {
    "‘": "'", "’": "'",          # ‘ ’ curly single quotes
    "“": '"', "”": '"',          # “ ” curly double quotes
    "–": "-", "—": "-", "−": "-",  # – — − dash / minus
    "…": "...",                       # … horizontal ellipsis
    "ﬁ": "fi", "ﬂ": "fl",        # ﬁ ﬂ ligatures
    " ": " ",                         # non-breaking space
}

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")

FUZZY_RATIO = 0.85
CONTAINMENT = 0.95
MIN_CONTAINMENT_TOKENS = 8


def normalize(s: str) -> str:
    """NFKC + punctuation/ligature fold + casefold + whitespace-collapse + strip."""
    s = unicodedata.normalize("NFKC", s)
    for src, dst in _PUNCT_MAP.items():
        if src in s:
            s = s.replace(src, dst)
    s = s.casefold()
    return _WS.sub(" ", s).strip()


def _tokens(s: str) -> list[str]:
    return _TOKEN.findall(s)


def _best_window_ratio(needle: str, hay: str) -> float:
    """Max difflib ratio of ``needle`` against any same-length-ish window of ``hay``.

    Slides a window of ``len(needle) + slack`` with a small step, so cost stays
    O(len(hay)/step) rather than a full O(n·m) alignment over the whole doc. An
    exact occurrence short-circuits to 1.0; ``quick_ratio`` prunes windows that
    cannot reach the threshold before the real (quadratic) ratio is computed.
    """
    if not needle:
        return 1.0
    if needle in hay:
        return 1.0
    n = len(needle)
    sm = SequenceMatcher(autojunk=False)
    sm.set_seq2(needle)
    if len(hay) <= n:
        sm.set_seq1(hay)
        return sm.ratio()
    slack = max(8, n // 5)
    window = n + slack
    step = max(1, slack // 2)
    starts = list(range(0, len(hay) - window + 1, step))
    last = len(hay) - window
    if starts[-1] != last:
        starts.append(last)
    best = 0.0
    for start in starts:
        sm.set_seq1(hay[start:start + window])
        if sm.quick_ratio() < FUZZY_RATIO:
            continue
        r = sm.ratio()
        if r > best:
            best = r
            if best >= 0.999:
                break
    return best


def verify_quote(quote: str, doc_text: str) -> str:
    """Tier one quote against one document: exact | fuzzy | failed."""
    nq = normalize(quote)
    if not nq:
        return "failed"
    nd = normalize(doc_text)
    if nq in nd:
        return "verified_exact"

    # (a) ellipsis-stitched spans: every non-empty segment must land somewhere.
    segments = [seg for seg in (s.strip() for s in nq.split("...")) if seg]
    if segments and all(_best_window_ratio(seg, nd) >= FUZZY_RATIO for seg in segments):
        return "verified_fuzzy"

    # (b) high content-token containment (paraphrase / reorder), long quotes only.
    qtokens = _tokens(nq)
    if len(qtokens) >= MIN_CONTAINMENT_TOKENS:
        dtokens = set(_tokens(nd))
        contained = sum(1 for t in qtokens if t in dtokens)
        if contained / len(qtokens) >= CONTAINMENT:
            return "verified_fuzzy"

    return "failed"


def verify_evidence(conn, evidence) -> tuple[str, list[str]]:
    """Tier a list of {doc_id, quote} items; return (overall_tier, per_item_tiers).

    An item whose doc_id is absent from ``documents`` is ``failed``. Overall is
    ``failed`` if any item failed or the list is empty, else ``verified_fuzzy`` if
    any item is fuzzy, else ``verified_exact``.
    """
    per_item: list[str] = []
    for item in evidence:
        doc_id = item.get("doc_id")
        row = conn.execute(
            "SELECT full_text FROM documents WHERE doc_id = ?", [doc_id]
        ).fetchone()
        if row is None:
            per_item.append("failed")
            continue
        tier = verify_quote(item.get("quote", ""), row[0] or "")
        if tier == "failed":
            # Review W1: tools surface mentions.evidence, which is itself stitched
            # (only ~40.7% exact vs full_text). A critic that faithfully copied a
            # short mention quote must not fail verification — accept an exact
            # normalized match against that doc's own mention evidence as fuzzy.
            quote_norm = normalize(item.get("quote", ""))
            if quote_norm:
                for (mention_ev,) in conn.execute(
                    "SELECT DISTINCT evidence FROM mentions "
                    "WHERE doc_id = ? AND evidence IS NOT NULL", [doc_id]
                ).fetchall():
                    if quote_norm in normalize(mention_ev):
                        tier = "verified_fuzzy"
                        break
        per_item.append(tier)

    if not per_item or "failed" in per_item:
        overall = "failed"
    elif "verified_fuzzy" in per_item:
        overall = "verified_fuzzy"
    else:
        overall = "verified_exact"
    return overall, per_item
