"""Executable spec for litgraph.sources.seeds.ingest — the seed-corpus ingester.

Contract under test (fixed by coordinator):

    seeds.ingest(conn, *, seeds_path: Path = Path("data/seeds.yaml")) -> dict

- Reads a YAML file {seeds: [{url, source_type, title, published, why}, ...]}.
- blog  -> fetch the page over HTTP, extract main text, store the extracted text.
- repo  -> parse owner/name from the github.com URL, fetch the README from
           raw.githubusercontent.com (branch "main", falling back to "master").
- doc_id: blog -> "blog:" + sha256(url)[:16] ; repo -> "repo:{owner}/{name}".
- Stores source_type, title, url, published_date (NULL allowed), full_text.
- Skip-and-report: a fetch failure OR full_text length NOT > 500 chars means the
  doc is not stored and its url is appended to the returned "skipped" list.
  (Store predicate is strictly len(full_text) > 500: the design's acceptance gate
  is "full_text > 500 chars", so a 500-char body is skipped and 501 is stored.)
- Idempotent: writes use ON CONFLICT DO NOTHING, so a second run over the same
  seeds stores 0 and does not raise.
- Returns {"stored": <int>, "skipped": [<url>, ...]}.

No network here. The implementation is pinned to reach the network via two
module-level names — `litgraph.sources.seeds.httpx.get(url, ...)` returning a
response with `.status_code` / `.text` / `.raise_for_status()`, and
`litgraph.sources.seeds.trafilatura.extract(html, ...)` returning the extracted
text (or None) — which these tests replace at the module namespace.

Expected to FAIL until sources/seeds.py exists (ModuleNotFoundError at import).
"""

import hashlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from litgraph import db
from litgraph.sources import seeds

TEXT_600 = "a" * 600  # comfortably over the > 500 store threshold
TEXT_500 = "b" * 500  # exactly the boundary -> skipped (not > 500)
TEXT_501 = "c" * 501  # one over the boundary -> stored


# --- fake network/extraction boundary ---------------------------------------


class FakeHTTPError(Exception):
    """Stand-in for httpx.HTTPError (base of the fake exception hierarchy)."""


class FakeRequestError(FakeHTTPError):
    """Stand-in for httpx.RequestError (transport failure -> fetch failure)."""


class FakeHTTPStatusError(FakeHTTPError):
    """Stand-in for httpx.HTTPStatusError (raised by raise_for_status on >=400)."""


class FakeResponse:
    def __init__(self, status_code: int, text: str, url: str):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeHTTPStatusError(f"{self.status_code} for {self.url}")


def _install(monkeypatch, *, http_routes=None, http_fail=(), traf_map=None, calls=None):
    """Point seeds.httpx and seeds.trafilatura at in-memory fakes.

    http_routes: {url: (status_code, body_text)}   (unlisted url -> 404)
    http_fail:   iterable of urls whose GET raises a transport error
    traf_map:    {html_body: extracted_text_or_None}
    calls:       optional list that records every requested url, in order
    """
    http_routes = http_routes or {}
    http_fail = set(http_fail)
    traf_map = traf_map or {}

    def get(url, *args, **kwargs):
        if calls is not None:
            calls.append(url)
        if url in http_fail:
            raise FakeRequestError(f"transport boom: {url}")
        status, text = http_routes.get(url, (404, ""))
        return FakeResponse(status, text, url)

    monkeypatch.setattr(
        seeds,
        "httpx",
        SimpleNamespace(
            get=get,
            HTTPError=FakeHTTPError,
            RequestError=FakeRequestError,
            HTTPStatusError=FakeHTTPStatusError,
        ),
    )

    def extract(html, *args, **kwargs):
        return traf_map.get(html)

    monkeypatch.setattr(seeds, "trafilatura", SimpleNamespace(extract=extract))


# --- fixtures / helpers ------------------------------------------------------


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def write_seeds(tmp_path, items) -> Path:
    p = tmp_path / "seeds.yaml"
    p.write_text(yaml.safe_dump({"seeds": items}))
    return p


def blog_seed(url, title="A post", published="2025-01-01"):
    return {
        "url": url,
        "source_type": "blog",
        "title": title,
        "published": published,
        "why": "because",
    }


def repo_seed(url, title="A repo", published="2024-01-01"):
    return {
        "url": url,
        "source_type": "repo",
        "title": title,
        "published": published,
        "why": "because",
    }


def rows(conn):
    return conn.execute(
        "SELECT doc_id, source_type, url, title, published_date, full_text FROM documents"
    ).fetchall()


# --- tests -------------------------------------------------------------------


def test_seeds_blog_stored_with_hashed_doc_id(conn, tmp_path, monkeypatch):
    """blog doc_id is exactly 'blog:' + sha256(url)[:16]; extracted text is stored."""
    url = "https://example.com/memory-post"
    html = "<html>blog body</html>"
    _install(monkeypatch, http_routes={url: (200, html)}, traf_map={html: TEXT_600})

    result = seeds.ingest(conn, seeds_path=write_seeds(tmp_path, [blog_seed(url)]))

    expected_id = "blog:" + hashlib.sha256(url.encode()).hexdigest()[:16]
    assert result == {"stored": 1, "skipped": []}
    all_rows = rows(conn)
    assert len(all_rows) == 1
    doc_id, source_type, stored_url, _title, _pub, full_text = all_rows[0]
    assert doc_id == expected_id
    assert source_type == "blog"
    assert stored_url == url
    assert full_text == TEXT_600


def test_seeds_repo_doc_id_is_owner_slash_name(conn, tmp_path, monkeypatch):
    """repo doc_id is 'repo:{owner}/{name}' and the README is fetched from raw main."""
    url = "https://github.com/acme/widget"
    raw_main = "https://raw.githubusercontent.com/acme/widget/main/README.md"
    calls = []
    _install(monkeypatch, http_routes={raw_main: (200, TEXT_600)}, calls=calls)

    result = seeds.ingest(conn, seeds_path=write_seeds(tmp_path, [repo_seed(url)]))

    assert result == {"stored": 1, "skipped": []}
    assert raw_main in calls  # README pulled from raw.githubusercontent, main branch
    doc_id, source_type, _url, _title, _pub, full_text = rows(conn)[0]
    assert doc_id == "repo:acme/widget"
    assert source_type == "repo"
    assert full_text == TEXT_600


def test_seeds_repo_falls_back_to_master_branch(conn, tmp_path, monkeypatch):
    """When main 404s, the README is fetched from the master branch instead."""
    url = "https://github.com/acme/legacy"
    raw_main = "https://raw.githubusercontent.com/acme/legacy/main/README.md"
    raw_master = "https://raw.githubusercontent.com/acme/legacy/master/README.md"
    calls = []
    _install(
        monkeypatch,
        http_routes={raw_main: (404, ""), raw_master: (200, TEXT_600)},
        calls=calls,
    )

    result = seeds.ingest(conn, seeds_path=write_seeds(tmp_path, [repo_seed(url)]))

    assert result == {"stored": 1, "skipped": []}
    assert raw_main in calls and raw_master in calls
    assert calls.index(raw_main) < calls.index(raw_master)  # main tried first
    doc_id, _st, _url, _title, _pub, full_text = rows(conn)[0]
    assert doc_id == "repo:acme/legacy"
    assert full_text == TEXT_600


def test_seeds_short_text_skipped_and_reported(conn, tmp_path, monkeypatch):
    """Boundary: 500 chars is skipped (not > 500); 501 chars is stored."""
    url_500 = "https://example.com/short"
    url_501 = "https://example.com/long"
    html_500 = "<html>short</html>"
    html_501 = "<html>long</html>"
    _install(
        monkeypatch,
        http_routes={url_500: (200, html_500), url_501: (200, html_501)},
        traf_map={html_500: TEXT_500, html_501: TEXT_501},
    )

    result = seeds.ingest(
        conn,
        seeds_path=write_seeds(tmp_path, [blog_seed(url_500), blog_seed(url_501)]),
    )

    assert result == {"stored": 1, "skipped": [url_500]}
    all_rows = rows(conn)
    assert len(all_rows) == 1
    assert all_rows[0][2] == url_501  # only the 501-char doc landed
    assert all_rows[0][5] == TEXT_501


def test_seeds_fetch_failure_skipped_not_fatal(conn, tmp_path, monkeypatch):
    """A GET that raises is skipped-and-reported; other seeds still store."""
    bad = "https://down.example.com/post"
    good = "https://up.example.com/post"
    good_html = "<html>up</html>"
    _install(
        monkeypatch,
        http_routes={good: (200, good_html)},
        http_fail={bad},
        traf_map={good_html: TEXT_600},
    )

    result = seeds.ingest(
        conn, seeds_path=write_seeds(tmp_path, [blog_seed(bad), blog_seed(good)])
    )

    assert result == {"stored": 1, "skipped": [bad]}
    all_rows = rows(conn)
    assert len(all_rows) == 1
    assert all_rows[0][2] == good


def test_seeds_second_run_is_noop(conn, tmp_path, monkeypatch):
    """Re-running over the same seeds stores 0 (ON CONFLICT DO NOTHING) and never raises."""
    url = "https://example.com/idempotent"
    html = "<html>same</html>"
    seeds_path = write_seeds(tmp_path, [blog_seed(url)])
    _install(monkeypatch, http_routes={url: (200, html)}, traf_map={html: TEXT_600})

    first = seeds.ingest(conn, seeds_path=seeds_path)
    second = seeds.ingest(conn, seeds_path=seeds_path)

    assert first == {"stored": 1, "skipped": []}
    assert second == {"stored": 0, "skipped": []}
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1


def test_seeds_null_published_date_allowed(conn, tmp_path, monkeypatch):
    """published: null is allowed and stored as a NULL published_date."""
    url = "https://example.com/undated"
    html = "<html>undated</html>"
    _install(monkeypatch, http_routes={url: (200, html)}, traf_map={html: TEXT_600})

    result = seeds.ingest(
        conn,
        seeds_path=write_seeds(tmp_path, [blog_seed(url, published=None)]),
    )

    assert result == {"stored": 1, "skipped": []}
    published_date = conn.execute(
        "SELECT published_date FROM documents WHERE url = ?", [url]
    ).fetchone()[0]
    assert published_date is None


def test_seeds_published_date_parsed_as_date(conn, tmp_path, monkeypatch):
    """A present ISO published date is stored as a DATE (sanity anchor for NULL test)."""
    url = "https://example.com/dated"
    html = "<html>dated</html>"
    _install(monkeypatch, http_routes={url: (200, html)}, traf_map={html: TEXT_600})

    seeds.ingest(
        conn,
        seeds_path=write_seeds(tmp_path, [blog_seed(url, published="2023-06-23")]),
    )

    published_date = conn.execute(
        "SELECT published_date FROM documents WHERE url = ?", [url]
    ).fetchone()[0]
    assert published_date == date(2023, 6, 23)
