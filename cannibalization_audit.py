"""
Keyword Cannibalization Audit — find queries where 2+ of your own pages
compete in Google's results, splitting clicks, links, and topical authority.

Why it matters: when Google sees two of your pages for the same query it
splits the ranking signal between them. Neither accumulates enough authority
to crack page 1 — the classic reason striking-distance keywords stay stuck
at position 6-12 forever.

Data source: output/gsc-raw.json (query+page rows from fetch.py). No new
API calls — pure post-processing of data the pipeline already has.

Detection logic (per query):
  1. Group GSC rows by query; keep queries where 2+ pages got impressions.
  2. Skip brand queries (SITE_BRAND_TOKENS) — homepage + posts ranking
     together for your own brand name is normal, not cannibalization.
  3. Compute each page's impression share for the query.
  4. Classify:
       split_authority — 2+ pages each hold >= 20% share and rank within
                         SPLIT_POSITION_GAP of each other. The real problem.
       shadow          — a secondary page has 5-20% share. Watch, usually
                         harmless, sometimes an emerging competitor.
  5. Aggregate findings BY PAGE PAIR: a pair that competes on 12 queries is
     ONE editorial decision (merge/differentiate), not 12 separate findings.

Fix playbook (in the report):
  - Same intent, overlapping content  -> merge + 301 the weaker page
  - Same topic, different intent      -> differentiate titles/H1s so each
                                         targets its own query cluster
  - Right page losing                 -> interlink loser -> winner with
                                         exact-anchor, deoptimize loser

Outputs:
  output/cannibalization-audit.json    (structured, for the dashboard)
  output/cannibalization-report.txt    (human-readable)

Usage:
  python cannibalization_audit.py
  from cannibalization_audit import run_audit; run_audit()
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import *

# ─── Tunables ────────────────────────────────────────────────────────────────
# Per-query threshold is LOW on purpose: real cannibalization shows up as the
# same page pair recurring across many small long-tail queries ("c bit",
# "bits c", "bit in c" ...). Noise is filtered at the PAIR level instead.
MIN_QUERY_IMPRESSIONS = 5      # ignore queries with fewer total impressions
MIN_PAGE_SHARE = 0.05          # a page needs >= 5% of a query's impressions to count
SPLIT_SHARE = 0.20             # >= 20% share on 2+ pages = authority split
SPLIT_POSITION_GAP = 12.0      # pages must rank within this gap to be "competing"
MIN_PAIR_QUERIES = 1           # report pairs sharing at least this many queries
# NOTE: deliberately no low-position cutoff. Cannibalized pages often sit at
# position 40-80 BECAUSE the split keeps both weak — filtering them out would
# remove the very evidence we're looking for. SPLIT_POSITION_GAP already
# separates "competing" from "one ranks, one is noise".
MAX_POSITION = 100.0


def _norm_page(url):
    """Normalize page URL to a slug-ish key: strip scheme/host/trailing slash."""
    p = urlparse(url)
    path = (p.path or '/').rstrip('/')
    return path if path else '/'


def _is_excluded_query(query, brand_tokens, domain):
    """Brand queries and search operators are normal multi-page results,
    not cannibalization."""
    q = query.lower()
    if 'site:' in q or q.startswith(('inurl:', 'intitle:')):
        return True
    if domain and domain in q:
        return True
    return any(t and t in q for t in (brand_tokens or []))


def load_gsc_rows():
    path = os.path.join(OUTPUT_DIR, 'gsc-raw.json')
    if not os.path.exists(path):
        raise FileNotFoundError(
            'output/gsc-raw.json not found — run the fetch step first '
            '(python run.py fetch or the Pipeline tab).')
    with open(path, 'r', encoding='utf-8') as f:
        rows = json.load(f)
    # rows: [{keys: [query, page], clicks, impressions, ctr, position}, ...]
    out = []
    for r in rows:
        keys = r.get('keys') or []
        if len(keys) != 2:
            continue
        query, page = keys[0], keys[1]
        out.append({
            'query': query.strip().lower(),
            'page': page,
            'path': _norm_page(page),
            'clicks': r.get('clicks', 0),
            'impressions': r.get('impressions', 0),
            'position': round(float(r.get('position', 0)), 1),
            'ctr': round(float(r.get('ctr', 0)) * 100, 2),
        })
    return out


def _collapse_variants(rows):
    """Merge rows whose URLs differ only by fragment/query-string/scheme/host
    (GSC reports #jump-link sitelinks as separate rows). Cannibalization is
    only real between DISTINCT paths — same-path variants are one page.
    Position is impression-weighted; clicks/impressions sum."""
    merged = {}
    for r in rows:
        key = (r['query'], r['path'])
        m = merged.get(key)
        if m is None:
            merged[key] = dict(r)
            continue
        total = m['impressions'] + r['impressions']
        if total > 0:
            m['position'] = round(
                (m['position'] * m['impressions'] + r['position'] * r['impressions'])
                / total, 1)
        m['impressions'] = total
        m['clicks'] += r['clicks']
        m['ctr'] = round(m['clicks'] / total * 100, 2) if total else 0
    return list(merged.values())


def detect(rows):
    """Return (query_findings, pair_findings, totals)."""
    brand = [t.lower() for t in (globals().get('SITE_BRAND_TOKENS') or [])]
    domain = urlparse(SITE_DOMAIN).netloc.replace('www.', '').lower()

    rows = _collapse_variants(rows)

    by_query = defaultdict(list)
    for r in rows:
        if r['position'] > MAX_POSITION:
            continue
        by_query[r['query']].append(r)

    query_findings = []
    for query, pages in by_query.items():
        if _is_excluded_query(query, brand, domain):
            continue
        total_impr = sum(p['impressions'] for p in pages)
        if total_impr < MIN_QUERY_IMPRESSIONS:
            continue
        # keep pages with a meaningful share
        contenders = []
        for p in pages:
            share = p['impressions'] / total_impr if total_impr else 0
            if share >= MIN_PAGE_SHARE:
                contenders.append({**p, 'share': round(share, 3)})
        if len(contenders) < 2:
            continue

        contenders.sort(key=lambda p: -p['impressions'])
        top, second = contenders[0], contenders[1]
        pos_gap = abs(top['position'] - second['position'])

        strong = [c for c in contenders if c['share'] >= SPLIT_SHARE]
        if len(strong) >= 2 and pos_gap <= SPLIT_POSITION_GAP:
            kind = 'split_authority'
        else:
            kind = 'shadow'

        query_findings.append({
            'query': query,
            'kind': kind,
            'total_impressions': total_impr,
            'total_clicks': sum(p['clicks'] for p in pages),
            'position_gap': round(pos_gap, 1),
            'pages': [{
                'path': c['path'],
                'share': c['share'],
                'impressions': c['impressions'],
                'clicks': c['clicks'],
                'position': c['position'],
                'ctr': c['ctr'],
            } for c in contenders],
        })

    # ── Aggregate by page pair (the actionable unit) ──
    pair_map = defaultdict(lambda: {'queries': [], 'impressions': 0, 'split_count': 0})
    for f in query_findings:
        paths = [p['path'] for p in f['pages'][:2]]  # top-2 contenders define the pair
        key = tuple(sorted(paths))
        pair_map[key]['queries'].append({
            'query': f['query'], 'kind': f['kind'],
            'impressions': f['total_impressions'],
            'position_gap': f['position_gap'],
        })
        pair_map[key]['impressions'] += f['total_impressions']
        if f['kind'] == 'split_authority':
            pair_map[key]['split_count'] += 1

    pair_findings = []
    for (a, b), info in pair_map.items():
        if len(info['queries']) < MIN_PAIR_QUERIES:
            continue
        # Which page wins more of the shared queries? (by clicks across findings)
        wins = {a: 0, b: 0}
        for f in query_findings:
            paths = [p['path'] for p in f['pages'][:2]]
            if sorted(paths) != sorted([a, b]):
                continue
            best = max(f['pages'][:2], key=lambda p: (p['clicks'], p['share']))
            if best['path'] in wins:
                wins[best['path']] += 1
        winner = a if wins[a] >= wins[b] else b
        loser = b if winner == a else a
        severity = ('high' if info['split_count'] >= 2 or
                              (info['split_count'] >= 1 and len(info['queries']) >= 3) else
                    'medium' if info['split_count'] >= 1 or len(info['queries']) >= 3 else
                    'low')
        pair_findings.append({
            'pages': [a, b],
            'winner': winner,
            'loser': loser,
            'shared_queries': len(info['queries']),
            'split_queries': info['split_count'],
            'total_impressions': info['impressions'],
            'severity': severity,
            'queries': sorted(info['queries'], key=lambda q: -q['impressions'])[:15],
            'recommendation': _recommend(info, winner, loser),
        })

    pair_findings.sort(key=lambda p: (-p['split_queries'], -p['total_impressions']))
    query_findings.sort(key=lambda f: (-int(f['kind'] == 'split_authority'),
                                       -f['total_impressions']))

    totals = {
        'queries_analyzed': len(by_query),
        'cannibalized_queries': len(query_findings),
        'split_authority_queries': sum(1 for f in query_findings if f['kind'] == 'split_authority'),
        'shadow_queries': sum(1 for f in query_findings if f['kind'] == 'shadow'),
        'competing_page_pairs': len(pair_findings),
        'high_severity_pairs': sum(1 for p in pair_findings if p['severity'] == 'high'),
    }
    return query_findings, pair_findings, totals


def _recommend(info, winner, loser):
    n_split = info['split_count']
    if n_split >= 2:
        return (f'Strong overlap on {n_split} queries. Read both pages: if they answer the '
                f'same question, MERGE the content into {winner} and 301 {loser}. '
                f'If intents genuinely differ, rewrite {loser}\'s title/H1 to target its own '
                f'query cluster and remove the overlapping keyword from its title.')
    if n_split >= 1:
        return (f'Partial overlap. Differentiate: keep {winner} targeting these queries; '
                f'retitle {loser} toward its distinct angle, then add an exact-anchor link '
                f'from {loser} to {winner} so Google knows which is canonical for the topic.')
    return (f'Minor shadow ranking — usually harmless. Add one contextual link from '
            f'{loser} to {winner} with the query as anchor text; re-check next month.')


def run_audit():
    print('== Keyword Cannibalization Audit ==', flush=True)
    rows = load_gsc_rows()
    print(f'  {len(rows)} query+page rows loaded from GSC', flush=True)

    query_findings, pair_findings, totals = detect(rows)

    print(f'  Queries with 2+ ranking pages: {totals["cannibalized_queries"]} '
          f'({totals["split_authority_queries"]} authority-split, '
          f'{totals["shadow_queries"]} shadow)', flush=True)
    print(f'  Competing page pairs: {totals["competing_page_pairs"]} '
          f'({totals["high_severity_pairs"]} high severity)', flush=True)

    result = {
        'generated': datetime.now().isoformat(),
        'thresholds': {
            'min_query_impressions': MIN_QUERY_IMPRESSIONS,
            'split_share': SPLIT_SHARE,
            'split_position_gap': SPLIT_POSITION_GAP,
        },
        'totals': totals,
        'pairs': pair_findings,
        'queries': query_findings[:100],   # cap the per-query list; pairs are the real output
    }
    out_json = os.path.join(OUTPUT_DIR, 'cannibalization-audit.json')
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f'  -> {out_json}', flush=True)

    # Text report
    lines = [
        'KEYWORD CANNIBALIZATION AUDIT',
        f'Generated: {result["generated"]}',
        '=' * 70, '',
        f'Queries analyzed:            {totals["queries_analyzed"]}',
        f'Cannibalized queries:        {totals["cannibalized_queries"]}',
        f'  - authority split (bad):   {totals["split_authority_queries"]}',
        f'  - shadow (watch):          {totals["shadow_queries"]}',
        f'Competing page pairs:        {totals["competing_page_pairs"]}', '',
    ]
    for i, p in enumerate(pair_findings, 1):
        lines += [
            '-' * 70,
            f'#{i} [{p["severity"].upper()}] {p["pages"][0]}  <-vs->  {p["pages"][1]}',
            f'   shared queries: {p["shared_queries"]} ({p["split_queries"]} authority-split)'
            f'   · combined impressions: {p["total_impressions"]:,}',
            f'   winner (more clicks): {p["winner"]}',
            f'   FIX: {p["recommendation"]}', '',
            '   Top shared queries:',
        ]
        for q in p['queries'][:8]:
            flag = '!! ' if q['kind'] == 'split_authority' else '   '
            lines.append(f'   {flag}{q["query"]}  ({q["impressions"]:,} impr, '
                         f'pos gap {q["position_gap"]})')
        lines.append('')
    out_txt = os.path.join(OUTPUT_DIR, 'cannibalization-report.txt')
    with open(out_txt, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'  -> {out_txt}', flush=True)
    return result


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    run_audit()
