"""
Merge GSC + Bing opportunities into a single unified file.
Deduplicates keywords per page, tags source, sums impressions.

Outputs: merged-opportunities.json
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from config import *


def merge_opportunities():
    """Merge GSC and Bing opportunity data."""

    gsc_path = os.path.join(OUTPUT_DIR, 'opportunities.json')
    bing_path = os.path.join(OUTPUT_DIR, 'bing-opportunities.json')

    gsc_data = []
    bing_data = []

    if os.path.exists(gsc_path):
        with open(gsc_path, 'r', encoding='utf-8') as f:
            gsc_data = json.load(f)
        print(f"[MERGE] GSC: {len(gsc_data)} pages")

    if os.path.exists(bing_path):
        with open(bing_path, 'r', encoding='utf-8') as f:
            bing_data = json.load(f)
        print(f"[MERGE] Bing: {len(bing_data)} pages")

    # Build unified dict keyed by slug
    merged = {}

    def clean_query(query):
        """Strip non-printable and replacement characters from keyword strings."""
        import unicodedata
        cleaned = ''.join(
            c for c in query
            if unicodedata.category(c) not in ('Cc', 'Cf', 'Cs', 'Co')
        ).strip()
        return cleaned

    # Add GSC data first — deduplicate within GSC by (query, position)
    for page in gsc_data:
        slug = page['slug']
        merged[slug] = {
            'page': page.get('page', f"{SITE_DOMAIN}/{slug}/"),
            'slug': slug,
            'keywords': [],
            'gsc_impressions': page.get('total_impressions', 0),
            'bing_impressions': 0,
            'total_impressions': 0,
            'total_clicks': 0,
        }
        seen_gsc = set()
        for kw in page.get('keywords', []):
            q = clean_query(kw.get('query', ''))
            if not q:
                continue
            key = q.lower()
            if key in seen_gsc:
                continue
            seen_gsc.add(key)
            kw_copy = dict(kw)
            kw_copy['query'] = q
            kw_copy.setdefault('source', 'gsc')
            merged[slug]['keywords'].append(kw_copy)

    # Merge Bing data
    for page in bing_data:
        slug = page['slug']
        if slug not in merged:
            merged[slug] = {
                'page': page.get('page', f"{SITE_DOMAIN}/{slug}/"),
                'slug': slug,
                'keywords': [],
                'gsc_impressions': 0,
                'bing_impressions': 0,
                'total_impressions': 0,
                'total_clicks': 0,
            }

        merged[slug]['bing_impressions'] = page.get('total_impressions', 0)

        # Merge Bing keywords. When the same query appears in BOTH GSC and Bing,
        # SUM the impressions/clicks and weighted-average the position by impressions —
        # do NOT silently drop Bing's data. Previously this script kept only GSC's
        # entry, which hid the larger Bing impression counts for shared queries.
        existing_by_query = {kw['query'].lower(): kw for kw in merged[slug]['keywords']}
        for kw in page.get('keywords', []):
            q = clean_query(kw.get('query', ''))
            if not q:
                continue
            key = q.lower()
            if key in existing_by_query:
                ex = existing_by_query[key]
                old_impr = ex.get('impressions', 0) or 0
                new_impr = kw.get('impressions', 0) or 0
                total_impr = old_impr + new_impr
                old_clicks = ex.get('clicks', 0) or 0
                new_clicks = kw.get('clicks', 0) or 0
                old_pos = ex.get('position', 0) or 0
                new_pos = kw.get('position', 0) or 0
                ex['impressions'] = total_impr
                ex['clicks']      = old_clicks + new_clicks
                if total_impr > 0:
                    ex['position'] = round((old_pos * old_impr + new_pos * new_impr) / total_impr, 1)
                ex['ctr']         = round(ex['clicks'] / total_impr * 100, 1) if total_impr > 0 else 0
                ex['source']      = 'gsc+bing'
            else:
                kw_copy = dict(kw)
                kw_copy['query']  = q
                kw_copy['source'] = 'bing'
                merged[slug]['keywords'].append(kw_copy)
                existing_by_query[key] = kw_copy

    # Calculate totals and sort keywords
    for slug, data in merged.items():
        data['keywords'].sort(key=lambda x: x['impressions'], reverse=True)
        data['total_impressions'] = data['gsc_impressions'] + data['bing_impressions']
        data['total_clicks'] = sum(kw.get('clicks', 0) for kw in data['keywords'])

    # Merge GA4 data (bounce rate, sessions, avg session duration)
    ga4_path = os.path.join(OUTPUT_DIR, 'ga4-data.json')
    if os.path.exists(ga4_path):
        with open(ga4_path, 'r', encoding='utf-8') as f:
            ga4_data = json.load(f)
        merged_count = 0
        for slug, data in merged.items():
            if slug in ga4_data:
                data['bounce_rate'] = ga4_data[slug]['bounce_rate']
                data['engagement_rate'] = ga4_data[slug]['engagement_rate']
                data['sessions'] = ga4_data[slug]['sessions']
                data['avg_session_duration'] = ga4_data[slug]['avg_session_duration']
                merged_count += 1
        print(f"[MERGE] GA4 data merged for {merged_count} pages")

    # Sort pages by total impressions
    result = sorted(merged.values(), key=lambda x: x['total_impressions'], reverse=True)

    # Save
    merged_path = os.path.join(OUTPUT_DIR, 'merged-opportunities.json')
    with open(merged_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # Report
    gsc_only = sum(1 for p in result if p['gsc_impressions'] > 0 and p['bing_impressions'] == 0)
    bing_only = sum(1 for p in result if p['gsc_impressions'] == 0 and p['bing_impressions'] > 0)
    both = sum(1 for p in result if p['gsc_impressions'] > 0 and p['bing_impressions'] > 0)

    print(f"\n[MERGE] Unified: {len(result)} pages")
    print(f"  GSC only: {gsc_only} | Bing only: {bing_only} | Both: {both}")
    print(f"\n{'Slug':<55} {'GSC':>7} {'Bing':>7} {'Total':>7} {'KWs':>4}")
    print(f"{'-'*55} {'-'*7} {'-'*7} {'-'*7} {'-'*4}")

    for p in result[:15]:
        print(f"{p['slug'][:54]:<55} {p['gsc_impressions']:>7} {p['bing_impressions']:>7} {p['total_impressions']:>7} {len(p['keywords']):>4}")

    return result


if __name__ == '__main__':
    if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    merge_opportunities()
