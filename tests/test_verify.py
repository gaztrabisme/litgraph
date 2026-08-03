"""Executable spec for litgraph.verify — the tiered evidence verifier (W1).

Contract under test (locked by coordinator; phase2-design.md "Evidence verifier"):

    verify.normalize(s) -> str
        NFKC, unicode-punct fold (curly quotes -> ' and ", en/em-dash/minus -> -,
        ellipsis -> ..., ligatures fi/fl, nbsp -> space), casefold, whitespace
        collapse, strip.

    verify.verify_quote(quote, doc_text) -> str
        'verified_exact' if normalize(quote) is a substring of normalize(doc_text);
        else 'verified_fuzzy' if EITHER (a) splitting the normalized quote on "..."
        yields segments that EACH have a best sliding-window difflib ratio >= 0.85,
        OR (b) content-token containment >= 0.95 with the quote holding >= 8 content
        tokens (tokens = regex [a-z0-9]+ on normalized text); else 'failed'. Empty /
        whitespace quote -> 'failed'.

    verify.verify_evidence(conn, evidence) -> (overall_tier, per_item_tiers)
        evidence items are {"doc_id":..., "quote":...}; per item tier is 'failed'
        when doc_id is absent from documents; overall tier is 'failed' if any item
        failed or the list is empty, else 'verified_fuzzy' if any item fuzzy, else
        'verified_exact'.

The module does not exist yet, so collection fails with ModuleNotFoundError until
the implementation agent lands verify.py. All fixtures are direct DuckDB inserts.
"""

from litgraph import db, verify

# ---------------------------------------------------------------------------
# Fixtures with unicode variants. Sources use the plain ASCII form; quotes (or
# the doc, tested both directions) carry the punctuation/ligature/nbsp variants
# that normalize must fold away. ﬁ == ligature "fi", — == em-dash,
#   == nbsp, “/” == curly double quotes, … == ellipsis.
# ---------------------------------------------------------------------------
DOC_EXACT = 'The "fine" self-attention layer reads the input.'
QUOTE_VARIANTS = "the “ﬁne” self—attention layer reads"  # folds to a prefix of DOC_EXACT

DOC_VARIANTS = "The “ﬁne” self—attention layer reads the input."
QUOTE_PLAIN = 'the "fine" self-attention layer reads'

# Stitched span: neither contiguous span is the whole quote, but each half is a
# verbatim substring -> ellipsis-segment path (a) -> verified_fuzzy.
DOC_STITCH = "First span appears here. Much later, the second span appears again."
QUOTE_STITCH = "First span appears … second span appears"

# Paraphrase: every content token of the quote is present in the doc but reordered
# (not a substring) -> token-containment path (b) -> verified_fuzzy. 10 content tokens.
DOC_PARA = (
    "On the benchmark dataset, retrieval accuracy improves significantly when "
    "contrastive pretraining is applied with careful tuning and evaluation."
)
QUOTE_PARA = (
    "contrastive pretraining improves retrieval accuracy on the benchmark dataset significantly"
)

# Fabrications: tokens absent from any source doc.
QUOTE_FABRICATED = "quantum chromodynamics governs stellar nucleosynthesis across galaxies entirely"
QUOTE_SHORT_FABRICATED = "purple monkey dishwasher"


def add_doc(conn, doc_id, full_text):
    conn.execute(
        "INSERT INTO documents (doc_id, source_type, url, title, full_text) "
        "VALUES (?, 'paper', ?, ?, ?)",
        [doc_id, f"http://ex/{doc_id}", doc_id, full_text],
    )


def new_db():
    return db.connect(":memory:")


# ---------------------------------------------------------------------------
# normalize — per-property, so a failure names the exact fold that broke.
# ---------------------------------------------------------------------------
def test_normalize_folds_curly_double_quotes():
    assert verify.normalize("“x”") == '"x"'


def test_normalize_folds_curly_single_quotes():
    assert verify.normalize("‘x’") == "'x'"


def test_normalize_folds_dashes_to_hyphen():
    assert verify.normalize("a–b") == "a-b"  # en-dash
    assert verify.normalize("a—b") == "a-b"  # em-dash
    assert verify.normalize("a−b") == "a-b"  # minus sign


def test_normalize_folds_ellipsis():
    assert verify.normalize("a…b") == "a...b"


def test_normalize_folds_ligatures():
    assert verify.normalize("ﬁne") == "fine"  # fi ligature
    assert verify.normalize("ﬂow") == "flow"  # fl ligature


def test_normalize_folds_nbsp_to_space():
    assert verify.normalize("a b") == "a b"


def test_normalize_casefolds():
    assert verify.normalize("ABC") == "abc"


def test_normalize_collapses_whitespace_and_strips():
    assert verify.normalize("  a   b\t c \n ") == "a b c"


def test_normalize_applies_nfkc():
    # Fullwidth letters (U+FF21..) compatibility-decompose under NFKC.
    assert verify.normalize("ＡＢ") == "ab"


def test_normalize_empty_and_whitespace():
    assert verify.normalize("") == ""
    assert verify.normalize("   \t\n ") == ""


# ---------------------------------------------------------------------------
# verify_quote — exact tier (with unicode variants on either side)
# ---------------------------------------------------------------------------
def test_verify_quote_exact_plain_substring():
    assert verify.verify_quote("self-attention layer", DOC_EXACT) == "verified_exact"


def test_verify_quote_exact_when_quote_has_variants():
    assert verify.verify_quote(QUOTE_VARIANTS, DOC_EXACT) == "verified_exact"


def test_verify_quote_exact_when_doc_has_variants():
    assert verify.verify_quote(QUOTE_PLAIN, DOC_VARIANTS) == "verified_exact"


# ---------------------------------------------------------------------------
# verify_quote — fuzzy tier
# ---------------------------------------------------------------------------
def test_verify_quote_stitched_spans_are_fuzzy():
    """"span one ... span two" where each span exists separately -> verified_fuzzy."""
    assert verify.verify_quote(QUOTE_STITCH, DOC_STITCH) == "verified_fuzzy"


def test_verify_quote_high_token_containment_paraphrase_is_fuzzy():
    """>=8 content tokens, >=0.95 containment, not a substring -> verified_fuzzy."""
    assert verify.verify_quote(QUOTE_PARA, DOC_PARA) == "verified_fuzzy"


# ---------------------------------------------------------------------------
# verify_quote — failed tier
# ---------------------------------------------------------------------------
def test_verify_quote_fabricated_is_failed():
    assert verify.verify_quote(QUOTE_FABRICATED, DOC_PARA) == "failed"


def test_verify_quote_short_fabricated_is_failed():
    """<8 tokens (containment path disabled) and not a substring -> failed."""
    assert verify.verify_quote(QUOTE_SHORT_FABRICATED, DOC_PARA) == "failed"


def test_verify_quote_empty_is_failed():
    assert verify.verify_quote("", DOC_PARA) == "failed"


def test_verify_quote_whitespace_only_is_failed():
    assert verify.verify_quote("   \n\t ", DOC_PARA) == "failed"


# ---------------------------------------------------------------------------
# verify_evidence — DB-backed overall/per-item tiering
# ---------------------------------------------------------------------------
def test_verify_evidence_all_exact_overall_exact():
    conn = new_db()
    add_doc(conn, "d1", DOC_EXACT)
    add_doc(conn, "d2", DOC_VARIANTS)
    evidence = [
        {"doc_id": "d1", "quote": QUOTE_VARIANTS},
        {"doc_id": "d2", "quote": QUOTE_PLAIN},
    ]

    overall, per_item = verify.verify_evidence(conn, evidence)

    assert per_item == ["verified_exact", "verified_exact"]
    assert overall == "verified_exact"


def test_verify_evidence_mixed_exact_and_fuzzy_overall_fuzzy():
    conn = new_db()
    add_doc(conn, "dexact", DOC_EXACT)
    add_doc(conn, "dstitch", DOC_STITCH)
    evidence = [
        {"doc_id": "dexact", "quote": QUOTE_PLAIN},
        {"doc_id": "dstitch", "quote": QUOTE_STITCH},
    ]

    overall, per_item = verify.verify_evidence(conn, evidence)

    assert per_item == ["verified_exact", "verified_fuzzy"]
    assert overall == "verified_fuzzy"


def test_verify_evidence_fabricated_doc_id_is_failed():
    """A doc_id absent from documents -> that item failed and overall failed."""
    conn = new_db()
    add_doc(conn, "d1", DOC_EXACT)
    evidence = [{"doc_id": "ghost", "quote": QUOTE_PLAIN}]

    overall, per_item = verify.verify_evidence(conn, evidence)

    assert per_item == ["failed"]
    assert overall == "failed"


def test_verify_evidence_any_failed_forces_overall_failed():
    conn = new_db()
    add_doc(conn, "d1", DOC_EXACT)
    evidence = [
        {"doc_id": "d1", "quote": QUOTE_PLAIN},          # exact
        {"doc_id": "ghost", "quote": QUOTE_PLAIN},        # unknown doc -> failed
    ]

    overall, per_item = verify.verify_evidence(conn, evidence)

    assert per_item == ["verified_exact", "failed"]
    assert overall == "failed"


def test_verify_evidence_empty_list_is_failed():
    conn = new_db()
    overall, per_item = verify.verify_evidence(conn, [])

    assert overall == "failed"
    assert per_item == []


def test_W1_mention_evidence_fallback_rescues_stitched_quote(conn_with_doc=None):
    """A quote copied verbatim from a mention row (itself stitched, absent from
    full_text) verifies as fuzzy via the mention-evidence fallback."""
    from litgraph import db, verify

    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO documents (doc_id, source_type, url, title, full_text) "
        "VALUES ('d1', 'paper', 'u', 't', 'The system stores episodic traces in a buffer.')"
    )
    conn.execute(
        "INSERT INTO extractions (extraction_id, doc_id, prompt_version, model, status) "
        "VALUES ('e1', 'd1', 'v1', 'm', 'ok')"
    )
    conn.execute(
        "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, src_name, src_type, "
        "dst_name, dst_type, edge_type, evidence) VALUES ('m1', 'e1', 'd1', 'relation', "
        "'A', 'Method', 'B', 'Problem', 'USES', 'stores traces ... a short stitched line')"
    )
    stitched = "stores traces ... a short stitched line"  # not in full_text, <8 tokens
    overall, tiers = verify.verify_evidence(conn, [{"doc_id": "d1", "quote": stitched}])
    assert tiers == ["verified_fuzzy"]
    assert overall == "verified_fuzzy"
    # fabricated text still fails despite the fallback
    overall, tiers = verify.verify_evidence(conn, [{"doc_id": "d1", "quote": "made up entirely"}])
    assert overall == "failed"
