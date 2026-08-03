"""Contract tests for litgraph.sources.arxiv.ingest — the arXiv Atom source.

Executable spec for the ingest contract fixed by the coordinator:

    litgraph.sources.arxiv.ingest(conn, *, query=DEFAULT_QUERY, max_results=170) -> dict

Network is faked at the module boundary (`litgraph.sources.arxiv.httpx`); no real
HTTP is issued. The DB is a fresh in-memory DuckDB with the real schema (db.connect).

Each test drives exactly one clause of the contract with the smallest fixture that
triggers it, and asserts on the observable (documents rows / return dict).
"""

import json
import urllib.parse
from datetime import date

import pytest

from litgraph.sources import arxiv  # noqa: E402  (fails until the module exists — expected)

# --------------------------------------------------------------------------- #
# Atom feed + HTML fixtures (the real arXiv API shape, minimised)
# --------------------------------------------------------------------------- #

ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"


def _entry(
    arxiv_id: str,
    *,
    title: str,
    summary: str,
    authors: tuple[str, ...],
    published: str,
    primary_cat: str = "cs.AI",
    categories: tuple[str, ...] = ("cs.AI", "cs.LG"),
) -> str:
    """One <entry>. Text nodes hug their tags so they are strip-stable."""
    author_xml = "".join(f"<author><name>{a}</name></author>" for a in authors)
    cat_xml = "".join(f'<category term="{c}"/>' for c in categories)
    return (
        "<entry>"
        f"<id>http://arxiv.org/abs/{arxiv_id}</id>"
        f"<updated>{published}</updated>"
        f"<published>{published}</published>"
        f"<title>{title}</title>"
        f"<summary>{summary}</summary>"
        f"{author_xml}"
        f'<arxiv:primary_category xmlns:arxiv="{ARXIV_NS}" term="{primary_cat}" '
        'scheme="http://arxiv.org/schemas/atom"/>'
        f"{cat_xml}"
        f'<link href="http://arxiv.org/abs/{arxiv_id}" rel="alternate" type="text/html"/>'
        "</entry>"
    )


def _feed(entries: list[str]) -> str:
    body = "".join(entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<feed xmlns="{ATOM_NS}" xmlns:arxiv="{ARXIV_NS}">'
        "<title>ArXiv Query</title>"
        f"{body}"
        "</feed>"
    )


# --------------------------------------------------------------------------- #
# Fake httpx — replaces the module's `httpx` attribute wholesale.
# Supports both httpx.get(...) and httpx.Client().get(...) call styles.
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _StatusError(f"HTTP {self.status_code}", request=None, response=self)

    def json(self):
        return json.loads(self.text)


class _HTTPError(Exception):
    pass


class _StatusError(_HTTPError):
    def __init__(self, *args, request=None, response=None):
        super().__init__(*args)
        self.request = request
        self.response = response


class _RequestError(_HTTPError):
    pass


class _Client:
    def __init__(self, dispatch, *args, **kwargs):
        self._dispatch = dispatch

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        return self._dispatch(url)

    def close(self):
        pass


class FakeHttpx:
    """Drop-in stand-in for the `httpx` module, dispatching on URL."""

    def __init__(self, dispatch):
        self._dispatch = dispatch
        self.HTTPError = _HTTPError
        self.HTTPStatusError = _StatusError
        self.RequestError = _RequestError
        self.ConnectError = _RequestError
        self.TimeoutException = _RequestError
        self.Response = _Resp

    def get(self, url, **kwargs):
        return self._dispatch(url)

    def Client(self, *args, **kwargs):
        return _Client(self._dispatch, *args, **kwargs)

    def Timeout(self, *args, **kwargs):
        return None


def _parse_max_results(url: str, default: int) -> int:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "max_results" in q:
        return int(q["max_results"][0])
    return default


def make_fake_httpx(entries: list[str], html_responder, requested: list[str]) -> FakeHttpx:
    """Build a fake httpx.

    - API-feed requests return a feed of `entries` sliced to the requested max_results
      (simulating arXiv's server-side limit — proves the module forwarded it).
    - /html/ requests are delegated to `html_responder(url)`.
    - Every requested URL is appended to `requested`.
    """

    def dispatch(url: str) -> _Resp:
        requested.append(url)
        if "api/query" in url or "export.arxiv" in url:
            n = _parse_max_results(url, default=len(entries))
            return _Resp(_feed(entries[:n]))
        if "/html/" in url:
            return html_responder(url)
        raise _RequestError(f"unexpected url {url}")

    return FakeHttpx(dispatch)


def html_404(_url: str) -> _Resp:
    """Every full-text HTML fetch misses → forces abstract fallback."""
    return _Resp("", status_code=404)


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #

_DOC_COLS = [
    "doc_id",
    "source_type",
    "url",
    "title",
    "authors",
    "published_date",
    "full_text",
    "meta",
]


def _doc(conn, doc_id: str) -> dict | None:
    row = conn.execute(
        f"SELECT {', '.join(_DOC_COLS)} FROM documents WHERE doc_id = ?", [doc_id]
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_DOC_COLS, row))


def _meta(doc: dict) -> dict:
    m = doc["meta"]
    return json.loads(m) if isinstance(m, str) else m


def _count(conn) -> int:
    return conn.execute("SELECT count(*) FROM documents").fetchone()[0]


@pytest.fixture
def conn():
    from litgraph import db

    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _no_politeness_delay(monkeypatch):
    monkeypatch.setattr(arxiv, "FETCH_DELAY", 0)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_arxiv_ingest_stores_documents_with_versionless_doc_id(conn, monkeypatch):
    """W9: doc_id = 'arxiv:' + versionless id; source_type='paper'; abs url + title stored."""
    entries = [
        _entry(
            "2405.17044v3",
            title="MemGPT: Towards LLMs as Operating Systems",
            summary="An abstract about memory.",
            authors=("Alice Smith",),
            published="2024-05-27T12:34:56Z",
        )
    ]
    fake = make_fake_httpx(entries, html_404, requested=[])
    monkeypatch.setattr(arxiv, "httpx", fake)

    result = arxiv.ingest(conn, max_results=170)

    doc = _doc(conn, "arxiv:2405.17044")
    assert doc is not None, "doc_id must be 'arxiv:' + version-stripped id"
    assert doc["source_type"] == "paper"
    assert "arxiv.org/abs/2405.17044" in doc["url"]
    assert doc["title"] == "MemGPT: Towards LLMs as Operating Systems"
    assert result["stored"] == 1


def test_arxiv_ingest_second_run_is_noop(conn, monkeypatch):
    """W8: identical feed twice → stored 0 / skipped n; row count unchanged (ON CONFLICT DO NOTHING)."""
    entries = [
        _entry(
            "2405.17044v3",
            title="Paper One",
            summary="Abstract one.",
            authors=("Alice Smith",),
            published="2024-05-27T00:00:00Z",
        ),
        _entry(
            "2312.00001v1",
            title="Paper Two",
            summary="Abstract two.",
            authors=("Bob Jones",),
            published="2023-12-01T00:00:00Z",
        ),
    ]
    monkeypatch.setattr(arxiv, "httpx", make_fake_httpx(entries, html_404, requested=[]))

    first = arxiv.ingest(conn)
    assert first["stored"] == 2
    assert first["skipped"] == 0
    assert _count(conn) == 2

    second = arxiv.ingest(conn)
    assert second["stored"] == 0
    assert second["skipped"] == 2
    assert _count(conn) == 2, "re-ingesting existing doc_ids must not add rows"


def test_arxiv_ingest_html_fallback_to_abstract(conn, monkeypatch):
    """Full text comes from arxiv.org/html/{id}; on a 404 it falls back to the Atom abstract."""
    abstract_two = "Fallback abstract body for the second paper."
    entries = [
        _entry(
            "2405.17044v3",
            title="Has HTML",
            summary="Abstract that should be overridden by HTML.",
            authors=("Alice Smith",),
            published="2024-05-27T00:00:00Z",
        ),
        _entry(
            "2312.00001v1",
            title="HTML Missing",
            summary=abstract_two,
            authors=("Bob Jones",),
            published="2023-12-01T00:00:00Z",
        ),
    ]
    html_body = (
        "<html><body><article><p>"
        + ("MARKER extracted full text of the first paper. " * 200)
        + "</p></article></body></html>"
    )

    def html_responder(url: str) -> _Resp:
        if "2405.17044" in url:
            return _Resp(html_body, status_code=200)
        return _Resp("", status_code=404)  # 2312.00001 → miss → abstract fallback

    monkeypatch.setattr(arxiv, "httpx", make_fake_httpx(entries, html_responder, requested=[]))
    arxiv.ingest(conn)

    with_html = _doc(conn, "arxiv:2405.17044")
    fell_back = _doc(conn, "arxiv:2312.00001")
    assert "MARKER" in with_html["full_text"], "successful HTML fetch must supply full_text"
    assert fell_back["full_text"] == abstract_two, "404 HTML must fall back to the abstract text"


def test_arxiv_ingest_full_text_ok_counts_over_2000_chars(conn, monkeypatch):
    """full_text_ok counts docs with full_text strictly > 2000 chars — 2000 excluded, 2001 included."""
    entries = [
        _entry(
            "2405.10000v1",
            title="Exactly 2000",
            summary="A" * 2000,
            authors=("Alice Smith",),
            published="2024-05-01T00:00:00Z",
        ),
        _entry(
            "2405.20000v1",
            title="Exactly 2001",
            summary="B" * 2001,
            authors=("Bob Jones",),
            published="2024-05-02T00:00:00Z",
        ),
    ]
    # 404 all HTML so full_text is the abstract verbatim → lengths are exact/deterministic.
    monkeypatch.setattr(arxiv, "httpx", make_fake_httpx(entries, html_404, requested=[]))

    result = arxiv.ingest(conn)

    assert len(_doc(conn, "arxiv:2405.10000")["full_text"]) == 2000
    assert len(_doc(conn, "arxiv:2405.20000")["full_text"]) == 2001
    assert result["full_text_ok"] == 1, "> 2000 is strict: only the 2001-char doc counts"


def test_arxiv_ingest_parses_published_date_and_authors(conn, monkeypatch):
    """published_date parsed to a DATE; multiple authors joined with '; '; categories captured in meta."""
    entries = [
        _entry(
            "2405.17044v3",
            title="Two Authors",
            summary="Abstract.",
            authors=("Alice Smith", "Bob Jones"),
            published="2024-05-27T12:34:56Z",
            primary_cat="cs.AI",
            categories=("cs.AI", "cs.LG"),
        )
    ]
    monkeypatch.setattr(arxiv, "httpx", make_fake_httpx(entries, html_404, requested=[]))
    arxiv.ingest(conn)

    doc = _doc(conn, "arxiv:2405.17044")
    assert doc["published_date"] == date(2024, 5, 27)
    assert doc["authors"] == "Alice Smith; Bob Jones"
    assert "cs.AI" in _meta(doc)["categories"]


def test_arxiv_ingest_max_results_forwarded_to_api(conn, monkeypatch):
    """max_results is forwarded to the arXiv API query (not hard-coded)."""
    entries = [
        _entry(
            f"2405.0000{i}v1",
            title=f"Paper {i}",
            summary="Abstract.",
            authors=("Alice Smith",),
            published="2024-05-0{}T00:00:00Z".format(i + 1),
        )
        for i in range(3)
    ]
    requested: list[str] = []
    monkeypatch.setattr(arxiv, "httpx", make_fake_httpx(entries, html_404, requested))

    result = arxiv.ingest(conn, max_results=2)

    api_urls = [u for u in requested if "api/query" in u or "export.arxiv" in u]
    assert api_urls, "an arXiv API request must have been issued"
    assert any("max_results=2" in u for u in api_urls), "max_results must be sent to the API"
    assert result["stored"] == 2, "server-side limit honoured → only 2 docs stored"


def test_arxiv_ingest_ids_uses_id_list_param(conn, monkeypatch):
    """ids=[...] fetches via the API id_list param (no search_query) and stores those docs."""
    entries = [
        _entry(
            "2501.12948v1",
            title="By Id One",
            summary="Abstract.",
            authors=("Alice Smith",),
            published="2025-01-22T00:00:00Z",
        ),
        _entry(
            "1706.03762v7",
            title="By Id Two",
            summary="Abstract.",
            authors=("Bob Jones",),
            published="2017-06-12T00:00:00Z",
        ),
    ]
    requested: list[str] = []
    monkeypatch.setattr(arxiv, "httpx", make_fake_httpx(entries, html_404, requested))

    result = arxiv.ingest(conn, ids=["2501.12948", "1706.03762"])

    api_urls = [u for u in requested if "api/query" in u or "export.arxiv" in u]
    assert api_urls and all("id_list=" in u for u in api_urls)
    assert all("search_query" not in u for u in api_urls), "id fetch must not send a search query"
    assert result["stored"] == 2
    assert _doc(conn, "arxiv:2501.12948") and _doc(conn, "arxiv:1706.03762")


def test_arxiv_ingest_ids_chunks_requests(conn, monkeypatch):
    """More ids than the per-request chunk size → several id_list requests, all ids covered."""
    ids = [f"2405.{10000 + i}" for i in range(120)]
    requested: list[str] = []
    # feed responder echoes back one entry per requested id
    def dispatch(url: str) -> _Resp:
        requested.append(url)
        if "api/query" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            batch = q["id_list"][0].split(",")
            return _Resp(
                _feed(
                    [
                        _entry(
                            f"{i}v1",
                            title=f"Paper {i}",
                            summary="Abstract.",
                            authors=("A",),
                            published="2024-05-01T00:00:00Z",
                        )
                        for i in batch
                    ]
                )
            )
        return html_404(url)

    monkeypatch.setattr(arxiv, "httpx", FakeHttpx(dispatch))
    result = arxiv.ingest(conn, ids=ids)

    api_urls = [u for u in requested if "api/query" in u]
    assert len(api_urls) >= 2, "120 ids must be fetched in more than one id_list request"
    assert result["stored"] == 120


def test_arxiv_module_exposes_default_query(conn, monkeypatch):
    """DEFAULT_QUERY exists and ingest runs on the default (no query arg needed)."""
    assert isinstance(arxiv.DEFAULT_QUERY, str) and arxiv.DEFAULT_QUERY
    entries = [
        _entry(
            "2405.17044v3",
            title="Default Query Paper",
            summary="Abstract.",
            authors=("Alice Smith",),
            published="2024-05-27T00:00:00Z",
        )
    ]
    monkeypatch.setattr(arxiv, "httpx", make_fake_httpx(entries, html_404, requested=[]))
    result = arxiv.ingest(conn)  # relies on DEFAULT_QUERY
    assert result["stored"] == 1
