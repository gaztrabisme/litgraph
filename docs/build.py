"""Regenerate docs/index.html from the dossier JSONs. Run from repo root:

    python3 docs/build.py
"""

import html
import json
from pathlib import Path

REPO = "https://github.com/gaztrabisme/litgraph"
SURVIVED = Path("data/frontier-gaps-2026-07-19.json")
CONTESTED = Path("data/frontier-gaps-2026-07-19-contested.json")
OUT = Path("docs/index.html")

ROLE_LABEL = {
    "critic_1": "Critic 1 — Qwen3.6-35B (local)",
    "critic_2": "Critic 2 — deepseek-v4-flash",
    "stage_d": "Cross-family audit — Claude",
}


def esc(s):
    return html.escape(s or "", quote=True)


def doc_link(doc_id):
    if doc_id.startswith("arxiv:"):
        aid = doc_id.split(":", 1)[1]
        return f'<a href="https://arxiv.org/abs/{esc(aid)}" target="_blank" rel="noopener">{esc(doc_id)}</a>'
    if doc_id.startswith("repo:"):
        slug = doc_id.split(":", 1)[1]
        return f'<a href="https://github.com/{esc(slug)}" target="_blank" rel="noopener">{esc(doc_id)}</a>'
    return esc(doc_id)


def dispute_url(cid):
    title = f"Dispute {cid} — ".replace(" ", "+")
    return f"{REPO}/issues/new?template=dispute-gap.md&title={title}"


def verdict_html(v):
    label = ROLE_LABEL.get(v["role"], v["role"])
    badge = f'<span class="verdict v-{esc(v["verdict"])}">{esc(v["verdict"])}</span>'
    if v["role"] == "stage_d":
        return f'<div class="stage-d"><span class="v-label">{esc(label)}:</span> {badge}</div>'
    body = ""
    if v.get("refutation_claim"):
        body += f'<p class="refutation"><span class="k">Best refutation attempt:</span> {esc(v["refutation_claim"])}</p>'
    for e in v.get("evidence", []):
        body += (
            f'<blockquote class="ev">{esc(e["quote"])}'
            f'<footer>{doc_link(e["doc_id"])}</footer></blockquote>'
        )
    meta = " · ".join(
        esc(str(v[k]))
        for k in ("evidence_tier", "feasibility", "confidence_bucket")
        if v.get(k)
    )
    if meta:
        body += f'<p class="meta">{meta}</p>'
    return (
        f"<details><summary>{esc(label)}: {badge}</summary>"
        f'<div class="v-body">{body}</div></details>'
    )


def card_html(d, contested=False):
    cid = d["candidate_id"]
    arm = cid.split(":")[1]
    quotes = "".join(
        f'<blockquote class="ground">{esc(q)}</blockquote>' for q in d["grounds"]["quotes"]
    )
    docs = " ".join(doc_link(i) for i in d["grounds"]["doc_ids"])
    verdicts = "".join(verdict_html(v) for v in d["verdicts"])
    cbadge = '<span class="badge contested-badge">contested</span>' if contested else ""
    return f"""
<article class="card" data-arm="{esc(arm)}" data-status="{'contested' if contested else 'survived'}">
  <header>
    <code class="cid">{esc(cid)}</code>
    <span class="badge arm-{esc(arm)}">{esc(arm)} arm</span>{cbadge}
    <a class="dispute" href="{dispute_url(cid)}" target="_blank" rel="noopener">Dispute this gap</a>
  </header>
  <p class="claim">{esc(d["claim"])}</p>
  <div class="grounds">
    <p class="k">Grounded in {docs}</p>
    {quotes}
  </div>
  <div class="verdicts">{verdicts}</div>
</article>"""


def main():
    survived = json.loads(SURVIVED.read_text())
    contested = json.loads(CONTESTED.read_text())
    cards = "".join(card_html(d) for d in survived)
    ccards = "".join(card_html(d, contested=True) for d in contested)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LitGraph — 48 machine-vetted research gaps, awaiting human review</title>
<style>
:root {{
  --bg: #faf9f7; --card: #ffffff; --ink: #1a1a1a; --muted: #6b6b6b;
  --line: #e4e1db; --accent: #2456a6; --qwen: #7c3aed; --claude: #b45309;
  --survive: #15803d; --abstain: #a16207; --contested: #b91c1c;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #16181d; --card: #1e2128; --ink: #e8e6e3; --muted: #9a9a94;
    --line: #2e323b; --accent: #7aa2e8; --qwen: #b79df5; --claude: #e8a75c;
    --survive: #4ade80; --abstain: #facc15; --contested: #f87171;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}}
.wrap {{ max-width: 860px; margin: 0 auto; padding: 2rem 1rem 4rem; }}
h1 {{ font-size: 1.6rem; line-height: 1.25; margin: 0 0 .5rem; }}
h2 {{ font-size: 1.25rem; margin: 2.5rem 0 .25rem; }}
.sub {{ color: var(--muted); margin: 0 0 1.5rem; }}
.sub a {{ color: var(--accent); }}
.controls {{
  position: sticky; top: 0; background: var(--bg); padding: .75rem 0;
  display: flex; gap: .5rem; flex-wrap: wrap; align-items: center;
  border-bottom: 1px solid var(--line); z-index: 5;
}}
.controls input {{
  flex: 1; min-width: 180px; padding: .45rem .7rem; border: 1px solid var(--line);
  border-radius: 8px; background: var(--card); color: var(--ink); font-size: .95rem;
}}
.controls button {{
  padding: .45rem .9rem; border: 1px solid var(--line); border-radius: 999px;
  background: var(--card); color: var(--ink); cursor: pointer; font-size: .9rem;
}}
.controls button.active {{ border-color: var(--accent); color: var(--accent); font-weight: 600; }}
#count {{ color: var(--muted); font-size: .9rem; margin-left: auto; }}
.card {{
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 1.1rem 1.25rem; margin: 1rem 0;
}}
.card header {{ display: flex; gap: .6rem; align-items: baseline; flex-wrap: wrap; }}
.cid {{ font-size: .85rem; color: var(--muted); }}
.badge {{
  font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  padding: .12rem .55rem; border-radius: 999px; border: 1px solid currentColor;
}}
.arm-qwen {{ color: var(--qwen); }}
.arm-claude {{ color: var(--claude); }}
.contested-badge {{ color: var(--contested); }}
.dispute {{ margin-left: auto; font-size: .85rem; color: var(--accent); }}
.claim {{ font-size: 1.06rem; font-weight: 600; margin: .7rem 0; }}
.k {{ color: var(--muted); font-size: .88rem; }}
.grounds a, .ev a {{ color: var(--accent); }}
blockquote {{
  margin: .5rem 0; padding: .4rem .9rem; border-left: 3px solid var(--line);
  color: var(--muted); font-size: .92rem;
}}
blockquote footer {{ margin-top: .25rem; font-size: .82rem; }}
details {{ border-top: 1px solid var(--line); padding: .5rem 0; }}
summary {{ cursor: pointer; font-size: .92rem; color: var(--muted); }}
.v-body {{ padding: .5rem 0 .25rem .5rem; }}
.refutation {{ font-size: .92rem; }}
.verdict {{ font-weight: 700; font-size: .8rem; text-transform: uppercase; }}
.v-survive {{ color: var(--survive); }}
.v-abstain {{ color: var(--abstain); }}
.v-refute, .v-kill {{ color: var(--contested); }}
.stage-d {{ padding: .5rem 0; font-size: .92rem; color: var(--muted); border-top: 1px solid var(--line); }}
.meta {{ font-size: .8rem; color: var(--muted); }}
.foot {{ margin-top: 3rem; color: var(--muted); font-size: .9rem; }}
.foot a {{ color: var(--accent); }}
</style>
</head>
<body>
<div class="wrap">
<h1>48 machine-vetted research gaps in the LLM literature — are they real?</h1>
<p class="sub">
A <a href="{REPO}">knowledge-graph gap engine</a> running mostly on local models
proposed these from a 1,285-document corpus (RAG, agent memory, harnesses,
diffusion LLMs, RL-for-LLM, model forensics, foundations, image-gen). Every
claim is grounded in verbatim quotes and survived an adversarial critic
ensemble that tried to kill it. What the machine <em>can't</em> judge is
whether they're novel beyond its index, or interesting. That's the ask:
if you know a paper that closes one, or a reason it's a bad idea,
<strong>dispute it</strong>. Full story in the
<a href="{REPO}/blob/main/docs/writeup.md">writeup</a>.
</p>
<div class="controls">
  <input id="q" type="search" placeholder="Filter by keyword (e.g. memory, diffusion, unlearning)&hellip;" aria-label="filter">
  <button data-f="all" class="active">All</button>
  <button data-f="qwen">Qwen arm</button>
  <button data-f="claude">Claude arm</button>
  <button data-f="contested">Contested</button>
  <span id="count"></span>
</div>
<section id="cards">
{cards}
<h2 id="contested-h">Contested — the critics deadlocked; humans wanted</h2>
<p class="sub">These 7 did not survive cleanly: the critic families disagreed
or every verdict was unverifiable. They are published precisely because they
need a human tiebreak.</p>
{ccards}
</section>
<p class="foot">
Generated from
<a href="{REPO}/blob/main/data/frontier-gaps-2026-07-19.json">data/frontier-gaps-2026-07-19.json</a>
(2026-07-19 run) by <code>docs/build.py</code>. MIT.
</p>
</div>
<script>
const cards = [...document.querySelectorAll('.card')];
const contestedH = document.getElementById('contested-h');
const contestedSub = contestedH.nextElementSibling;
const count = document.getElementById('count');
const q = document.getElementById('q');
let arm = 'all';
function apply() {{
  const needle = q.value.trim().toLowerCase();
  let shown = 0;
  cards.forEach(c => {{
    const armOk = arm === 'all' ? true
      : arm === 'contested' ? c.dataset.status === 'contested'
      : (c.dataset.arm === arm && c.dataset.status === 'survived');
    const textOk = !needle || c.textContent.toLowerCase().includes(needle);
    const on = armOk && textOk;
    c.style.display = on ? '' : 'none';
    if (on) shown++;
  }});
  const contestedVisible = cards.some(c =>
    c.dataset.status === 'contested' && c.style.display !== 'none');
  contestedH.style.display = contestedVisible ? '' : 'none';
  contestedSub.style.display = contestedVisible ? '' : 'none';
  count.textContent = shown + ' shown';
}}
document.querySelectorAll('.controls button').forEach(b => b.onclick = () => {{
  document.querySelectorAll('.controls button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  arm = b.dataset.f;
  apply();
}});
q.oninput = apply;
apply();
</script>
</body>
</html>"""
    OUT.write_text(page)
    print(f"wrote {OUT} ({len(page):,} chars, {len(survived)} survived + {len(contested)} contested)")


if __name__ == "__main__":
    main()
