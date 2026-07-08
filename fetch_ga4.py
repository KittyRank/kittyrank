"""
Fetch Google Analytics 4 data — bounce rate, sessions, engagement per page.

Uses the GA4 Data API with the same service account as GSC.
The service account must be added as a Viewer in GA4 property settings.

Outputs: ga4-data.json  (keyed by slug: {bounce_rate, sessions, avg_session_duration})
"""

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from config import *


def fetch_ga4_data():
    """Fetch per-page GA4 metrics: bounce rate, sessions, avg session duration."""

    if not GA4_PROPERTY_ID:
        print("[GA4] GA4_PROPERTY_ID not set in config.py — skipping")
        return {}

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            DateRange, Dimension, Metric, RunReportRequest
        )
        from google.oauth2 import service_account
    except ImportError:
        print("[GA4] google-analytics-data not installed. Run: pip install google-analytics-data")
        return {}

    print("[GA4] Authenticating with Google Analytics 4...")
    import google_oauth
    credentials = google_oauth.get_credentials('ga4')   # OAuth token, else service-account JSON
    if not credentials:
        print("[GA4] No Google credentials — skipping GA4.")
        return {}
    client = BetaAnalyticsDataClient(credentials=credentials)

    end_date = datetime.now() - timedelta(days=3)
    start_date = end_date - timedelta(days=GSC_DAYS_BACK)
    date_str = f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"
    print(f"[GA4] Querying {date_str}...")

    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        dimensions=[Dimension(name="pagePath")],
        metrics=[
            Metric(name="bounceRate"),
            Metric(name="sessions"),
            Metric(name="averageSessionDuration"),
            Metric(name="engagementRate"),
        ],
        date_ranges=[DateRange(
            start_date=start_date.strftime('%Y-%m-%d'),
            end_date=end_date.strftime('%Y-%m-%d')
        )],
        limit=5000,
    )

    response = client.run_report(request)
    print(f"[GA4] Got {len(response.rows)} page rows")

    # Build per-slug lookup
    ga4_by_slug = {}
    for row in response.rows:
        path = row.dimension_values[0].value  # e.g. /my-post/
        slug = path.strip('/')

        bounce_rate = float(row.metric_values[0].value or 0)
        sessions = int(row.metric_values[1].value or 0)
        avg_duration = float(row.metric_values[2].value or 0)
        engagement_rate = float(row.metric_values[3].value or 0)

        if sessions < 5:  # Skip pages with too few sessions for meaningful data
            continue

        ga4_by_slug[slug] = {
            'bounce_rate': round(bounce_rate * 100, 1),        # as percentage
            'engagement_rate': round(engagement_rate * 100, 1),
            'sessions': sessions,
            'avg_session_duration': round(avg_duration, 1),    # seconds
        }

    # Save
    out_path = os.path.join(OUTPUT_DIR, 'ga4-data.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(ga4_by_slug, f, indent=2, ensure_ascii=False)

    print(f"[GA4] Saved data for {len(ga4_by_slug)} pages to ga4-data.json")

    # Print top pages by bounce rate (worst first)
    high_bounce = sorted(
        [(slug, d) for slug, d in ga4_by_slug.items() if d['sessions'] >= 20],
        key=lambda x: x[1]['bounce_rate'],
        reverse=True
    )[:10]
    if high_bounce:
        print(f"\n{'Slug':<50} {'Bounce':>7} {'Sessions':>9} {'Avg Duration':>13}")
        print(f"{'-'*50} {'-'*7} {'-'*9} {'-'*13}")
        for slug, d in high_bounce:
            duration_str = f"{int(d['avg_session_duration']//60)}m{int(d['avg_session_duration']%60)}s"
            print(f"{slug[:49]:<50} {d['bounce_rate']:>6.1f}% {d['sessions']:>9} {duration_str:>13}")

    return ga4_by_slug


if __name__ == '__main__':
    fetch_ga4_data()
