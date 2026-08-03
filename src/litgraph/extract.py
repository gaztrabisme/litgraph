"""Per-document typed extraction — documents → extractions + mentions.

Un-extracted docs (no 'ok'/'ok_empty' row for the current prompt_version) are sent
to the LLM concurrently; on retry, stale error rows for the doc are deleted first
(C3). Worker threads run ONLY the LLM call — every DuckDB read and write happens on
the main thread (C1). Validated output becomes one extraction row plus entity/relation
mention rows; relation-mentions are deduped on (doc_id, src, edge, dst) per extraction.
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

from litgraph import llm, models

PROMPT_PATH = Path("prompts/extraction_v1.md")
MAX_INPUT_CHARS = 100_000
MAX_TOKENS = 16384


def _system_prompt() -> str:
    """Prompt body: everything after the first '---' line of the versioned file."""
    _, _, body = PROMPT_PATH.read_text().partition("\n---\n")
    return body.strip()


def _unextracted(conn) -> list[tuple[str, str]]:
    """(doc_id, full_text) for docs lacking an ok/ok_empty row for the current
    (prompt_version, model) pair — switching EXTRACTION_MODEL schedules a full
    re-extraction while prior models' rows stay put as raw data."""
    return conn.execute(
        "SELECT doc_id, full_text FROM documents d WHERE NOT EXISTS ("
        "  SELECT 1 FROM extractions e WHERE e.doc_id = d.doc_id "
        "  AND e.prompt_version = ? AND e.model = ? "
        "  AND e.status IN ('ok', 'ok_empty'))",
        [models.PROMPT_VERSION, models.EXTRACTION_MODEL],
    ).fetchall()


def _classify(fut):
    """Map a completed future to (status, raw_output, Extraction|None) on the main thread."""
    try:
        result = fut.result()
    except ValueError:  # unparseable output (chat_json) — no JSON to preserve
        return "schema_error", None, None
    except Exception:  # transport/other failure
        return "llm_error", None, None
    try:
        extraction = models.Extraction.model_validate(result)
    except ValueError:  # includes pydantic ValidationError — keep the offending JSON
        return "schema_error", json.dumps(result), None
    status = "ok" if extraction.entities else "ok_empty"
    return status, json.dumps(result), extraction


def _mention_rows(extraction_id: str, doc_id: str, extraction) -> list[tuple]:
    """Entity rows then deduped relation rows; mention_id = f'{extraction_id}:{i}'."""
    rows: list[tuple] = []
    for ent in extraction.entities:
        rows.append((f"{extraction_id}:{len(rows)}", extraction_id, doc_id, "entity",
                     ent.type.value, ent.name, None, None, None, None, None, None))
    seen: set[tuple] = set()
    for rel in extraction.relations:
        key = (doc_id, rel.src_name, rel.src_type, rel.edge_type, rel.dst_name, rel.dst_type)
        if key in seen:
            continue
        seen.add(key)
        rows.append((f"{extraction_id}:{len(rows)}", extraction_id, doc_id, "relation",
                     None, None, rel.src_name, rel.src_type.value, rel.dst_name,
                     rel.dst_type.value, rel.edge_type.value, rel.evidence))
    return rows


def _write(conn, doc_id: str, status: str, raw, extraction) -> None:
    """Delete stale error rows, then insert the extraction row and any mentions."""
    conn.execute(
        "DELETE FROM extractions WHERE doc_id = ? AND prompt_version = ? AND model = ? "
        "AND status IN ('schema_error', 'llm_error')",
        [doc_id, models.PROMPT_VERSION, models.EXTRACTION_MODEL],
    )
    extraction_id = uuid4().hex
    conn.execute(
        "INSERT INTO extractions "
        "(extraction_id, doc_id, prompt_version, model, raw_output, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [extraction_id, doc_id, models.PROMPT_VERSION, models.EXTRACTION_MODEL, raw, status],
    )
    if extraction is None:
        return
    rows = _mention_rows(extraction_id, doc_id, extraction)
    if rows:
        conn.executemany(
            "INSERT INTO mentions (mention_id, extraction_id, doc_id, kind, entity_type, "
            "name, src_name, src_type, dst_name, dst_type, edge_type, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def run(conn, *, workers: int = 8) -> dict:
    """Extract every un-extracted doc; returns per-status tallies over docs."""
    docs = _unextracted(conn)
    tally = {"docs": len(docs), "ok": 0, "ok_empty": 0, "schema_error": 0, "llm_error": 0}
    if not docs:
        return tally

    system = _system_prompt()
    schema = models.extraction_json_schema()

    def call(full_text: str) -> dict:
        return llm.chat_json(
            command="extract", model=models.EXTRACTION_MODEL, system=system,
            user=(full_text or "")[:MAX_INPUT_CHARS], schema_name="extraction",
            schema=schema, prompt_version=models.PROMPT_VERSION, max_tokens=MAX_TOKENS,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(call, full_text): doc_id for doc_id, full_text in docs}
        for fut in as_completed(futures):
            status, raw, extraction = _classify(fut)
            _write(conn, futures[fut], status, raw, extraction)
            tally[status] += 1
    return tally
