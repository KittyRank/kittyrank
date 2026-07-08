"""
Step 1: Fetch Google Search Console data.
Outputs: gsc-raw.json, opportunities.json
"""

import json
import os
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from config import *


def fetch_gsc_data():
    """Fetch search analytics from GSC and identify opportunities."""

    print("[FETCH] Authenticating with Google Search Console...")
    import google_oauth
    credentials = google_oauth.get_credentials('gsc')   # OAuth token, else service-account JSON
    if not credentials:
        raise RuntimeError("No Google credentials — connect Google in Settings, "
                           "or set GSC_CREDENTIALS to a service-account JSON.")
    service = build('searchconsole', 'v1', credentials=credentials)

    end_date = datetime.now() - timedelta(days=3)  # GSC data has 3-day lag
    start_date = end_date - timedelta(days=GSC_DAYS_BACK)

    print(f"[FETCH] Querying {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}...")

    response = service.searchanalytics().query(
        siteUrl=GSC_SITE_URL,
        body={
            'startDate': start_date.strftime('%Y-%m-%d'),
            'endDate': end_date.strftime('%Y-%m-%d'),
            'dimensions': ['query', 'page'],
            'rowLimit': GSC_ROW_LIMIT,
            'startRow': 0
        }
    ).execute()

    rows = response.get('rows', [])
    print(f"[FETCH] Got {len(rows)} query+page rows")

    # Save raw
    raw_path = os.path.join(OUTPUT_DIR, 'gsc-raw.json')
    with open(raw_path, 'w', encoding='utf-8') as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)

    # Build opportunities: group keywords by page
    page_keywords = {}
    for row in rows:
        query = row['keys'][0]
        page = row['keys'][1]
        pos = row['position']
        impr = row['impressions']
        clicks = row['clicks']
        ctr = row['ctr']

        if not (POSITION_RANGE[0] <= pos <= POSITION_RANGE[1] and impr >= MIN_IMPRESSIONS):
            continue

        # Normalize page URL (remove fragments)
        page_clean = page.split('#')[0]

        if page_clean not in page_keywords:
            page_keywords[page_clean] = {
                'page': page_clean,
                'slug': page_clean.replace(SITE_DOMAIN, '').strip('/'),
                'keywords': [],
                'total_impressions': 0,
                'total_clicks': 0
            }

        page_keywords[page_clean]['keywords'].append({
            'query': query,
            'position': round(pos, 1),
            'impressions': impr,
            'clicks': clicks,
            'ctr': round(ctr * 100, 1)
        })
        page_keywords[page_clean]['total_impressions'] += impr
        page_keywords[page_clean]['total_clicks'] += clicks

    # Sort keywords within each page by impressions
    for page_data in page_keywords.values():
        page_data['keywords'].sort(key=lambda x: x['impressions'], reverse=True)

    # Sort pages by total impressions
    opportunities = sorted(page_keywords.values(), key=lambda x: x['total_impressions'], reverse=True)

    opp_path = os.path.join(OUTPUT_DIR, 'opportunities.json')
    with open(opp_path, 'w', encoding='utf-8') as f:
        json.dump(opportunities, f, indent=2, ensure_ascii=False)

    print(f"[FETCH] Found {len(opportunities)} pages with striking-distance keywords")
    for i, opp in enumerate(opportunities[:10]):
        top_kw = opp['keywords'][0]['query']
        print(f"  {i+1}. {opp['slug'][:55]:<55} {opp['total_impressions']:>5} impr  top: \"{top_kw}\"")

    return opportunities


if __name__ == '__main__':
    fetch_gsc_data()
