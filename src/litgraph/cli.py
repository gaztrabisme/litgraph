"""LitGraph command line.

Phase 1: ingest | extract | resolve | graph | stats.
Phase 2: embed-nodes | communities | propose | assassinate | gaps.

Heavy modules are imported lazily inside their handlers so the parser (and the
graph/stats paths) work before those modules exist.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

from litgraph import db, graph

_UNRESOLVED_SQL = """
SELECT count(*) FROM mentions m WHERE m.kind = 'relation' AND (
  NOT EXISTS (SELECT 1 FROM aliases a
              WHERE a.alias_name = m.src_name AND a.node_type = m.src_type)
  OR NOT EXISTS (SELECT 1 FROM aliases a
                 WHERE a.alias_name = m.dst_name AND a.node_type = m.dst_type))
"""


def _warn_unresolved(conn) -> None:
    """Relations whose endpoints lack alias rows are silently dropped from the
    graph — usually a sign `resolve` wasn't re-run after `extract`."""
    dropped = conn.execute(_UNRESOLVED_SQL).fetchone()[0]
    if dropped:
        print(f"warning: {dropped} relation(s) have unresolved endpoints "
              "(run `litgraph resolve`); they are excluded from the graph",
              file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="litgraph")
    parser.add_argument("--db", default="litgraph.duckdb", help="DuckDB file path")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="fetch documents into the store")
    ingest_sub = ingest.add_subparsers(dest="source", required=True)
    arxiv = ingest_sub.add_parser("arxiv", help="fetch from the arXiv API")
    arxiv.add_argument("--max", type=int, default=170, help="max papers to fetch")
    arxiv.add_argument(
        "--ids-file",
        help="fetch exactly these ids (one arXiv id per line, 'arxiv:' prefix ok) instead of querying",
    )
    ingest_sub.add_parser("seeds", help="fetch curated blog/repo seeds")

    sub.add_parser("extract", help="LLM typed extraction over un-extracted docs")
    sub.add_parser("resolve", help="entity resolution -> canonical_nodes + aliases")

    graph_cmd = sub.add_parser("graph", help="rebuild the graph and print counts")
    graph_cmd.add_argument("--as-of", dest="as_of", default=None, help="YYYY-MM-DD cutoff")

    sub.add_parser("stats", help="print store + graph statistics as json")

    sub.add_parser("embed-nodes", help="embed canonical-node context docs")
    communities = sub.add_parser("communities", help="Louvain communities over the giant component")
    communities.add_argument("--seed", type=int, default=42)

    propose = sub.add_parser("propose", help="run gap templates -> gap_candidates")
    propose.add_argument("--k", type=int, default=25, help="per-template top-K cap")
    propose.add_argument("--t1-theta", type=float, default=0.60, dest="t1_theta")
    propose.add_argument("--t2-paper", action="store_true", dest="t2_paper",
                         help="paper-closable T2 claims (flagship Method x Problem)")
    propose.add_argument("--sweep", action="store_true",
                         help="print T1 yield per theta (no writes) and exit")

    frontier = sub.add_parser("frontier", help="frontier-gaps runner on the full graph")
    frontier.add_argument("mode", choices=["leads", "qwen"],
                          help="leads: print hole leads json; qwen: run proposer arm")
    frontier.add_argument("--max-holes", type=int, default=25, dest="max_holes")
    frontier.add_argument("--workers", type=int, default=4)

    assassinate = sub.add_parser("assassinate", help="kill-gate all proposed candidates")
    assassinate.add_argument("--workers", type=int, default=4)
    assassinate.add_argument("--max-steps", type=int, default=8, dest="max_steps")

    gaps = sub.add_parser("gaps", help="render survivor/contested/kill dossiers as json")
    gaps.add_argument("--status", default=None,
                      choices=["proposed", "killed", "survived", "contested"])

    backtest = sub.add_parser("backtest", help="temporal backtest (phase 3)")
    bt_sub = backtest.add_subparsers(dest="bt_command", required=True)
    bt_snap = bt_sub.add_parser("snapshot", help="papers-only snapshot DB at a cutoff")
    bt_snap.add_argument("--cutoff", required=True, help="YYYY-MM-DD")
    bt_snap.add_argument("--dest", required=True, help="snapshot DB path")
    bt_score = bt_sub.add_parser(
        "score", help="score snapshot survivors (from --snapshot-db) against this DB")
    bt_score.add_argument("--cutoff", required=True, help="YYYY-MM-DD")
    bt_score.add_argument("--snapshot-db", required=True, dest="snapshot_db")
    bt_score.add_argument("--arm", default="graph", choices=["graph", "baseline"])
    return parser


def _counts(conn, sql: str) -> dict:
    return {key: count for key, count in conn.execute(sql).fetchall()}


def _mentions_per_canonical(conn) -> float:
    try:
        from litgraph import resolve

        return resolve.mentions_per_canonical(conn)
    except ImportError:
        entities = conn.execute(
            "SELECT count(*) FROM mentions WHERE kind = 'entity'"
        ).fetchone()[0]
        canonicals = conn.execute("SELECT count(*) FROM canonical_nodes").fetchone()[0]
        return entities / canonicals if canonicals else 0.0


def _stats(conn) -> dict:
    return {
        "documents": _counts(conn, "SELECT source_type, count(*) FROM documents GROUP BY 1"),
        "mentions": conn.execute("SELECT count(*) FROM mentions").fetchone()[0],
        "canonical_nodes": _counts(
            conn, "SELECT node_type, count(*) FROM canonical_nodes GROUP BY 1"
        ),
        "edges": _counts_by_edge_type(conn),
        "mentions_per_canonical": _mentions_per_canonical(conn),
        "null_published_dates": conn.execute(
            "SELECT count(*) FROM documents WHERE published_date IS NULL"
        ).fetchone()[0],
        "extraction_status": _counts(
            conn, "SELECT status, count(*) FROM extractions GROUP BY 1"
        ),
    }


def _counts_by_edge_type(conn) -> dict:
    g = graph.build(conn)
    tally: dict[str, int] = {}
    for _, _, edge_type in g.edges(data="edge_type"):
        tally[edge_type] = tally.get(edge_type, 0) + 1
    return tally


def _gaps(conn, *, status: str | None = None) -> list[dict]:
    """Dossiers with full provenance: candidate -> verdicts -> evidence + tier.

    Grounds whose node_ids no longer exist in canonical_nodes (cluster changed on a
    re-resolve) are flagged stale_grounds rather than silently dropped.
    """
    live_ids = {r[0] for r in conn.execute("SELECT node_id FROM canonical_nodes").fetchall()}
    out = []
    for cid, template, claim, grounds, score, cstatus in conn.execute(
        "SELECT candidate_id, template, claim, grounds, score, status "
        "FROM gap_candidates WHERE status = coalesce(?, status) "
        "ORDER BY status, score DESC", [status]
    ).fetchall():
        grounds = json.loads(grounds)
        verdicts = [
            {"stage": s, "role": role, "model": m, "verdict": v,
             "refutation_claim": rc, "evidence": json.loads(ev) if ev else [],
             "evidence_tier": tier, "feasibility": feas, "confidence_bucket": bucket}
            for s, role, m, v, rc, ev, tier, feas, bucket in conn.execute(
                "SELECT stage, critic_role, critic_model, verdict, refutation_claim, "
                "evidence, evidence_tier, feasibility, confidence_bucket "
                "FROM verdicts WHERE candidate_id = ? ORDER BY created_at", [cid]
            ).fetchall()
        ]
        out.append({
            "candidate_id": cid, "template": template, "status": cstatus,
            "claim": claim, "score": score, "grounds": grounds,
            "stale_grounds": any(n not in live_ids for n in grounds.get("node_ids", [])),
            "verdicts": verdicts,
        })
    return out


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    conn = db.connect(args.db)
    try:
        if args.command == "ingest" and args.source == "arxiv":
            from litgraph.sources import arxiv

            ids = None
            if args.ids_file:
                ids = [
                    line.strip().removeprefix("arxiv:")
                    for line in Path(args.ids_file).read_text().splitlines()
                    if line.strip()
                ]
            print(json.dumps(arxiv.ingest(conn, max_results=args.max, ids=ids)))
        elif args.command == "ingest" and args.source == "seeds":
            from litgraph.sources import seeds

            print(json.dumps(seeds.ingest(conn)))
        elif args.command == "extract":
            from litgraph import extract

            print(json.dumps(extract.run(conn)))
        elif args.command == "resolve":
            from litgraph import resolve

            print(json.dumps(resolve.run(conn)))
        elif args.command == "graph":
            as_of = datetime.date.fromisoformat(args.as_of) if args.as_of else None
            g = graph.build(conn, as_of=as_of)
            _warn_unresolved(conn)
            print(json.dumps({"nodes": g.number_of_nodes(), "edges": g.number_of_edges()}))
        elif args.command == "stats":
            _warn_unresolved(conn)
            print(json.dumps(_stats(conn)))
        elif args.command == "embed-nodes":
            from litgraph import embeddings

            print(json.dumps(embeddings.populate(conn)))
        elif args.command == "communities":
            from litgraph import communities

            print(json.dumps(communities.assign(conn, seed=args.seed)))
        elif args.command == "propose":
            from litgraph import templates

            if args.sweep:
                thetas = [x / 100 for x in range(40, 91, 5)]
                print(json.dumps({"t1_sweep": templates.sweep_t1(conn, thetas)}))
            else:
                print(json.dumps(templates.propose(
                    conn, k=args.k, t1_theta=args.t1_theta, t2_paper=args.t2_paper)))
        elif args.command == "frontier":
            from litgraph import frontier as frontier_mod

            if args.mode == "leads":
                print(json.dumps(frontier_mod.hole_leads(conn, k=args.max_holes)))
            else:
                session_system = Path("prompts/frontier_qwen_v1.md").read_text().split(
                    "## stage:session")[1].strip()
                ground_system = Path("prompts/baseline_qwen_v3.md").read_text().split(
                    "## stage:ground")[1].strip()
                print(json.dumps(frontier_mod.run_qwen(
                    conn, session_system, ground_system,
                    max_holes=args.max_holes, max_workers=args.workers)))
        elif args.command == "assassinate":
            from litgraph import assassin

            print(json.dumps(assassin.assassinate(
                conn, workers=args.workers, max_steps=args.max_steps)))
        elif args.command == "gaps":
            print(json.dumps(_gaps(conn, status=args.status), indent=2))
        elif args.command == "backtest" and args.bt_command == "snapshot":
            from litgraph import backtest

            cutoff = datetime.date.fromisoformat(args.cutoff)
            # snapshot ATTACHes args.db from its own connection; the same file can't
            # also be open on this one (same-process unique-file-handle conflict)
            conn.close()
            print(json.dumps(backtest.snapshot(args.db, cutoff, args.dest)))
        elif args.command == "backtest" and args.bt_command == "score":
            from litgraph import backtest, db as _db

            cutoff = datetime.date.fromisoformat(args.cutoff)
            snap = _db.connect(args.snapshot_db)
            try:
                candidates = [
                    {"candidate_id": cid, "arm": args.arm, "template": template,
                     "claim": claim,
                     "grounds": json.loads(g) if isinstance(g, str) else g}
                    for cid, template, claim, g in snap.execute(
                        "SELECT candidate_id, template, claim, grounds "
                        "FROM gap_candidates WHERE status = 'survived' "
                        + ("AND json_extract_string(params, '$.arm') = 'baseline'"
                           if args.arm == "baseline" else
                           "AND coalesce(json_extract_string(params, '$.arm'), 'graph') = 'graph'")
                    ).fetchall()
                ]
            finally:
                snap.close()
            print(json.dumps(backtest.score(conn, cutoff, candidates)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
