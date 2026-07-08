"""
Fetch Bing Webmaster Tools search performance data.

Uses two endpoints:
  1. GetPageStats — gets all pages with impressions/clicks/position
  2. GetPageQueryStats — for each top page, gets the keywords driving traffic

Outputs: bing-raw.json, bing-opportunities.json (same format as GSC for merging)
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from config import *

BING_API_BASE = 'https://ssl.bing.com/webmaster/api.svc/json'


def bing_api_get(endpoint, extra_params=None):
    """Call a Bing Webmaster API endpoint."""
    params = {'apikey': BING_API_KEY, 'siteUrl': BING_SITE_URL}
    if extra_params:
        params.update(extra_params)
    resp = requests.get(f"{BING_API_BASE}/{endpoint}", params=params)
    resp.raise_for_status()
    return resp.json().get('d', [])


def fetch_bing_data():
    """Fetch Bing search data and build opportunities."""

    print("[BING] Fetching page-level stats...")
    page_rows = bing_api_get('GetPageStats')
    print(f"[BING] Got {len(page_rows)} page stat rows")

    # Aggregate page stats (multiple date entries per page)
    page_agg = defaultdict(lambda: {'impressions': 0, 'clicks': 0, 'positions': []})
    for row in page_rows:
        url = row.get('Query', '')  # In PageStats, "Query" is actually the page URL
        if not url.startswith('http'):
            continue
        page_agg[url]['impressions'] += row.get('Impressions', 0)
        page_agg[url]['clicks'] += row.get('Clicks', 0)
        pos = row.get('AvgImpressionPosition', 0)
        if pos > 0:
            page_agg[url]['positions'].append(pos)

    # Calculate average position per page
    for url, data in page_agg.items():
        if data['positions']:
            data['avg_position'] = round(sum(data['positions']) / len(data['positions']), 1)
        else:
            data['avg_position'] = 0

    # Sort by impressions, take top pages
    top_pages = sorted(page_agg.items(), key=lambda x: x[1]['impressions'], reverse=True)
    top_pages = [(url, data) for url, data in top_pages if data['impressions'] >= 5]

    # Bing keyword fetch budget: previously capped to MAX_PAGES_TO_ANALYZE (20),
    # which excluded most pages with real Bing traffic from getting any keyword data.
    # Use BING_KEYWORD_FETCH_LIMIT (default 200) or fall back to a high number so
    # we capture keywords for the long tail of pages too.
    fetch_limit = getattr(__import__('config'), 'BING_KEYWORD_FETCH_LIMIT', 200)
    n_to_fetch = min(len(top_pages), fetch_limit)
    print(f"[BING] {len(top_pages)} pages with 5+ impressions")
    print(f"[BING] Fetching per-page keyword data for top {n_to_fetch} pages...")

    # For each top page, get its keywords
    opportunities = []
    for i, (page_url, page_data) in enumerate(top_pages[:n_to_fetch]):
        slug = page_url.replace(BING_SITE_URL, '').replace(SITE_DOMAIN + '/', '').strip('/')
        if not slug:
            continue

        try:
            kw_rows = bing_api_get('GetPageQueryStats', {'page': page_url})
        except Exception as e:
            print(f"  [WARN] Failed for {slug}: {e}")
            continue

        if not kw_rows:
            continue

        # Aggregate keywords (multiple date entries)
        kw_agg = defaultdict(lambda: {'impressions': 0, 'clicks': 0, 'positions': []})
        for row in kw_rows:
            query = row.get('Query', '')
            if not query:
                continue
            kw_agg[query]['impressions'] += row.get('Impressions', 0)
            kw_agg[query]['clicks'] += row.get('Clicks', 0)
            pos = row.get('AvgImpressionPosition', 0)
            if pos > 0:
                kw_agg[query]['positions'].append(pos)

        # Build keyword list
        keywords = []
        for query, kw_data in kw_agg.items():
            avg_pos = round(sum(kw_data['positions']) / len(kw_data['positions']), 1) if kw_data['positions'] else 0
            impr = kw_data['impressions']
            clicks = kw_data['clicks']

            if impr >= 1 and POSITION_RANGE[0] <= avg_pos <= POSITION_RANGE[1]:
                keywords.append({
                    'query': query,
                    'position': avg_pos,
                    'impressions': impr,
                    'clicks': clicks,
                    'ctr': round(clicks / impr * 100, 1) if impr > 0 else 0,
                    'source': 'bing'
                })

        if keywords:
            keywords.sort(key=lambda x: x['impressions'], reverse=True)
            total_impr = sum(k['impressions'] for k in keywords)
            total_clicks = sum(k['clicks'] for k in keywords)

            opportunities.append({
                'page': page_url,
                'slug': slug,
                'keywords': keywords,
                'total_impressions': total_impr,
                'total_clicks': total_clicks,
                'source': 'bing'
            })

        time.sleep(0.5)  # Rate limit

    opportunities.sort(key=lambda x: x['total_impressions'], reverse=True)

    # Save
    raw_path = os.path.join(OUTPUT_DIR, 'bing-raw.json')
    with open(raw_path, 'w', encoding='utf-8') as f:
        json.dump({'page_stats': page_rows, 'page_count': len(top_pages)}, f, indent=2, ensure_ascii=False)

    opp_path = os.path.join(OUTPUT_DIR, 'bing-opportunities.json')
    with open(opp_path, 'w', encoding='utf-8') as f:
        json.dump(opportunities, f, indent=2, ensure_ascii=False)

    print(f"\n[BING] {len(opportunities)} pages with striking-distance keywords")
    for i, opp in enumerate(opportunities[:10]):
        top_kw = opp['keywords'][0]['query']
        print(f"  {i+1}. {opp['slug'][:55]:<55} {opp['total_impressions']:>5} impr  top: \"{top_kw}\"")

    return opportunities


if __name__ == '__main__':
    if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    fetch_bing_data()
