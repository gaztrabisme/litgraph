"""Seed-corpus ingester — data/seeds.yaml → documents.

Blogs are fetched over HTTP and reduced to main text via trafilatura; repos have
their README pulled raw from raw.githubusercontent.com (main, then master). A doc
lands only if its text is strictly longer than 500 chars; every fetch/extract
failure or short body is skipped and its url reported, never fatal. Writes use
ON CONFLICT DO NOTHING, so a re-run over the same seeds is a no-op.
"""

import hashlib
from datetime import date
from pathlib import Path

import httpx
import trafilatura
import yaml

MIN_TEXT_CHARS = 500
DEFAULT_SEEDS = Path("data/seeds.yaml")


def _blog_text(url: str) -> str | None:
    """Fetch a blog page and return its extracted main text (None on failure)."""
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    return trafilatura.extract(resp.text)


def _repo_readme(url: str) -> tuple[str, str | None]:
    """Return (doc_id, raw README text) for a github.com repo URL.

    README is pulled from the main branch, falling back to master; text is None
    if neither branch serves a 200.
    """
    owner, name = url.split("github.com/", 1)[1].split("/")[:2]
    doc_id = f"repo:{owner}/{name}"
    for branch in ("main", "master"):
        raw = f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/README.md"
        try:
            resp = httpx.get(raw, follow_redirects=True, timeout=30)
        except httpx.HTTPError:
            continue
        if resp.status_code == 200:
            return doc_id, resp.text
    return doc_id, None


def ingest(conn, *, seeds_path: Path = DEFAULT_SEEDS) -> dict:
    """Ingest every seed in ``seeds_path``; returns {"stored", "skipped"}."""
    seeds = yaml.safe_load(seeds_path.read_text())["seeds"]
    stored = 0
    skipped: list[str] = []

    for seed in seeds:
        url = seed["url"]
        source_type = seed["source_type"]
        if source_type == "repo":
            doc_id, full_text = _repo_readme(url)
        else:
            doc_id = "blog:" + hashlib.sha256(url.encode()).hexdigest()[:16]
            full_text = _blog_text(url)

        if full_text is None or len(full_text) <= MIN_TEXT_CHARS:
            skipped.append(url)
            continue

        if conn.execute("SELECT 1 FROM documents WHERE doc_id = ?", [doc_id]).fetchone():
            continue

        published = seed.get("published")
        conn.execute(
            "INSERT INTO documents "
            "(doc_id, source_type, url, title, published_date, full_text) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [
                doc_id,
                source_type,
                url,
                seed["title"],
                date.fromisoformat(published) if published else None,
                full_text,
            ],
        )
        stored += 1

    return {"stored": stored, "skipped": skipped}
