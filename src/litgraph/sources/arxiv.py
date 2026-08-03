"""arXiv Atom source — export.arxiv.org API → documents.

Queries the arXiv API for the agent-memory subdomain, storing one row per paper.
Full text is the rendered HTML (arxiv.org/html/{id}); when that is missing the
Atom <summary> abstract stands in verbatim. doc_id is the versionless id (W9) and
writes go through ON CONFLICT DO NOTHING, so re-ingesting a feed is a no-op (W8).
"""

import json
import re
import time
import urllib.parse
import defusedxml.ElementTree as ET
from datetime import date

import httpx
import trafilatura

ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"
_NS = {"atom": ATOM, "arxiv": ARXIV}

API_URL = "https://export.arxiv.org/api/query"
HTML_URL = "https://arxiv.org/html/{id}"
FULL_TEXT_MIN = 2000
FETCH_DELAY = 1.0  # politeness between HTML fetches; 0 delay got us 429-banned (2026-07-15)

DEFAULT_QUERY = (
    "(cat:cs.CL OR cat:cs.AI) AND ("
    'abs:"agent memory" OR abs:"memory-augmented" OR abs:"long-term memory" OR '
    'abs:"episodic memory" OR abs:"memory for LLM agents" OR '
    'abs:"long-context memory")'
)

_VERSION = re.compile(r"v\d+$")


def _text(entry, path: str) -> str:
    node = entry.find(path, _NS)
    return node.text.strip() if node is not None and node.text else ""


def _base_id(entry) -> str:
    """Versionless arXiv id from the entry <id> (e.g. '2405.17044')."""
    raw = _text(entry, "atom:id").rsplit("/abs/", 1)[-1]
    return _VERSION.sub("", raw)


def _categories(entry) -> list[str]:
    """Category terms with the primary category first, order-preserving, deduped."""
    terms: list[str] = []
    primary = entry.find("arxiv:primary_category", _NS)
    if primary is not None:
        terms.append(primary.get("term"))
    terms += [c.get("term") for c in entry.findall("atom:category", _NS)]
    return list(dict.fromkeys(t for t in terms if t))


def _full_text(base_id: str, abstract: str) -> str:
    """Clean text of the paper's rendered HTML, falling back to the abstract.

    Raw HTML is never stored: it is ~80% markup, which both poisons extraction
    quality and made LLM prefill ~10x more expensive (measured 2026-07-15).
    """
    time.sleep(FETCH_DELAY)
    try:
        resp = httpx.get(HTML_URL.format(id=base_id), follow_redirects=True, timeout=30)
    except httpx.HTTPError:
        return abstract
    if resp.status_code != 200:
        return abstract
    return trafilatura.extract(resp.text) or abstract


ID_CHUNK = 100  # ids per id_list request (arXiv API is unhappy far above this)


def _fetch_feed(params_dict: dict) -> str:
    params = urllib.parse.urlencode(params_dict)
    last_error: Exception | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(15 * attempt)
        try:
            resp = httpx.get(f"{API_URL}?{params}", follow_redirects=True, timeout=60)
        except httpx.HTTPError as e:
            last_error = e
            continue
        if resp.status_code == 200:
            return resp.text
        last_error = RuntimeError(f"arXiv API returned {resp.status_code}")
    raise RuntimeError(f"arXiv API feed fetch failed after 3 attempts: {last_error}")


def _entries(query: str, max_results: int, ids: list[str] | None):
    """Yield Atom entries — one search-query feed, or chunked id_list feeds for ``ids``."""
    if ids is None:
        feeds = [
            _fetch_feed(
                {
                    "search_query": query,
                    "start": 0,
                    "max_results": max_results,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                }
            )
        ]
    else:
        feeds = []
        for i in range(0, len(ids), ID_CHUNK):
            chunk = ids[i : i + ID_CHUNK]
            feeds.append(_fetch_feed({"id_list": ",".join(chunk), "max_results": len(chunk)}))
            time.sleep(FETCH_DELAY)
    for feed in feeds:
        yield from ET.fromstring(feed).findall("atom:entry", _NS)


def ingest(
    conn,
    *,
    query: str = DEFAULT_QUERY,
    max_results: int = 170,
    ids: list[str] | None = None,
) -> dict:
    """Ingest arXiv results for ``query`` — or exactly ``ids`` (versionless) when given.

    Returns {"stored","skipped","full_text_ok"}.
    """
    stored = skipped = full_text_ok = 0

    for entry in _entries(query, max_results, ids):
        base_id = _base_id(entry)
        doc_id = f"arxiv:{base_id}"

        if conn.execute("SELECT 1 FROM documents WHERE doc_id = ?", [doc_id]).fetchone():
            skipped += 1
            continue

        abstract = _text(entry, "atom:summary")
        full_text = _full_text(base_id, abstract)
        authors = "; ".join(
            n.text.strip() for n in entry.findall("atom:author/atom:name", _NS) if n.text
        )
        published = _text(entry, "atom:published")

        conn.execute(
            "INSERT INTO documents "
            "(doc_id, source_type, url, title, authors, published_date, full_text, meta) "
            "VALUES (?, 'paper', ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [
                doc_id,
                f"https://arxiv.org/abs/{base_id}",
                _text(entry, "atom:title"),
                authors,
                date.fromisoformat(published[:10]) if published else None,
                full_text,
                json.dumps({"categories": _categories(entry)}),
            ],
        )
        stored += 1
        if len(full_text) > FULL_TEXT_MIN:
            full_text_ok += 1

    return {"stored": stored, "skipped": skipped, "full_text_ok": full_text_ok}
