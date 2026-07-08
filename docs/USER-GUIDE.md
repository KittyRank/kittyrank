# KittyRank Free — User Guide

## The idea in one paragraph

Most of your future traffic is already almost yours: keywords where you rank
3–20 ("striking distance"). Google has judged the content good enough to
rank, but the title/meta/links aren't winning the click or the last few
positions. KittyRank finds those keywords, diagnoses why each page
underperforms, and writes better titles + metas with Claude. You apply them,
resubmit the URL, and measure.

## The workflow loop

```
Fetch → Analyze → Review proposals → Apply (manually in Free) → Submit URL → wait ~2-4 weeks → re-Fetch
```

Run the loop every 2–4 weeks. SEO changes need time to register; changing a
page again after a few days destroys your ability to know what worked.

## Dashboard tabs

### Pipeline
Runs the data pipeline step by step. Each step unlocks the next. Re-running
Fetch overwrites the previous data pull (that's fine — that's the loop).

### Changes
One card per proposed fix. Each card shows the target keyword, its current
position/impressions/CTR, why the page underperforms (CTR diagnosis), the
proposed title + meta, plus content-gap notes and internal-link suggestions.

Free-edition workflow per card:
1. **Copy Title** → paste into your SEO plugin's title field (Yoast, RankMath…)
2. **Copy Meta** → paste into the meta-description field, save the post
3. **Mark Applied** → records the fix locally (dated) so the next data pull
   can be compared against it
4. **Submit URL** (appears after Mark Applied) → asks Bing to
   recrawl the page so the change registers faster
5. Or **Reject** if the proposal is wrong — nothing is recorded against the page

### Reports + Audit
Five one-click audits. Run them after every Fetch:

- **Technical audit** — every URL search engines know about is HEAD-checked
  live. Categories: 5xx errors, *active bleed* (404s still getting clicks!),
  stale index, duplicates, unconsolidated redirects, unprotected
  high-traffic pages.
- **Trend analysis** — 90 days of GSC data in three 30-day buckets; flags
  rising pages, ranking losses, viral spikes, decaying content.
- **Backlink profile** — domain-level authority breakdown + which
  high-traffic pages have zero backlinks (your outreach targets).
- **Cornerstone links** — checks that your pillar/hub pages (configured in
  `CORNERSTONE_SLUGS`) receive the internal links they deserve.
- **Keyword cannibalization** — finds queries where two or more of YOUR OWN
  pages compete, splitting clicks and authority. Findings are grouped by
  page pair — each pair is one editorial decision. See below.

### Submit URLs
Manual URL submission to Bing Webmaster Tools. Use after publishing
or updating anything outside the Changes flow.

### Logs
Everything the pipeline did, timestamped. Check here first when something
looks stuck.

## Understanding cannibalization findings

Each pair card shows:
- **Severity** — HIGH means multiple queries have both pages ranking close
  together (authority split); LOW means one page shadows occasionally.
- **Winner** — the page currently earning more clicks for the shared queries.
- **Fix recommendation** — merge, differentiate, or interlink:
  - *Same intent, overlapping content* → merge into the winner + 301 the loser
  - *Same topic, different intent* → rewrite the loser's title/H1 to target
    its own query cluster
  - *Minor shadow* → add one contextual link loser → winner and re-check
    next month

**Mark as intentional** — when two pages deliberately serve different
intents (e.g. a beginner tutorial vs a selection guide), click *Mark as
intentional*: the pair moves to a collapsed "accepted" list and stops
alarming. If the overlap later escalates (more authority-split queries than
when you accepted it), the pair automatically resurfaces with an
ESCALATED badge.

In Free, execute the fixes manually (edit posts in WP admin; use a redirect
plugin for 301s — and update internal links that pointed at the merged-away
page). Pro automates the judgment and the mechanics:

- **Compare & recommend (AI)** — fetches both posts, compares actual
  content + search metrics + backlinks side by side, and recommends
  *keep A / keep B / merge (with direction)* with per-query ownership.
- **Consolidate** — the whole merge playbook in one click: site-wide
  internal-link rewrite → 301 → draft loser → resubmit to Bing.

## The static report

`python run.py report` writes `output/seo-report.md` + `seo-report.html` —
a self-contained snapshot of all audits + proposals. Useful for sharing or
for tracking month-over-month state in a folder.

## Rules of thumb

- **Never change a page twice inside ~4 weeks.** You lose attribution.
- **Trust the diagnosis, edit the wording.** Claude's title is a strong
  draft; your domain knowledge should polish it.
- **Fix active-bleed 404s first.** They're bleeding real clicks today —
  restore, 301, or let them die deliberately.
- **Impressions before CTR.** A page at position 15 with huge impressions
  beats a page at position 4 with none — work the big-inventory pages.
