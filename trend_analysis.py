"""
Trend analysis — 3-period bucketing (first30 / mid30 / last30 days) for
clicks, impressions, CTR, and position.

Catches pattern changes that a single-snapshot report can't distinguish:
  audience_growth                — clicks UP   + impressions UP
  ctr_improvement_only           — clicks UP   + impressions DOWN
  viral_spike                    — clicks UP   + impressions DOWN + CTR UP UP
  title_degradation              — clicks DOWN + impressions UP   (was being clicked, no longer)
  ranking_loss                   — clicks DOWN + impressions DOWN (lost rank)
  ctr_problem_despite_ranking    — position improving + clicks flat
  stable                         — all metrics within ±5%

Data sources, gracefully degrading:
  1. GSC API  — searchAnalytics.query(dimensions=['date'])
  2. Bing API — GetRankAndTrafficStats
  3. CSV fallback — GSC Chart.csv (inside Performance ZIP) +
                    Bing SearchPerformanceOverview CSV

Outputs:
  - trend-analysis.json          (full structured data + signals)
  - trend-analysis-report.txt    (human-readable)
"""

import csv
import io
import json
import os
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from config import *

# ─── Tunables ─────────────────────────────────────────────────────────────────
WINDOW_DAYS = 90
BUCKET_DAYS = 30           # first30 / mid30 / last30
STABLE_THRESHOLD_PCT = 5   # |delta| < 5% → 'stable'
VIRAL_CTR_DELTA_PCT = 50   # CTR up >50% qualifies for viral spike flag


# ─── Encoding-safe print (same pattern as technical_audit.py) ────────────────
import builtins as _builtins
_real_print = _builtins.print
def print(*args, **kwargs):
    try:
        _real_print(*args, **kwargs)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, 'encoding', 'ascii') or 'ascii'
        cleaned = [str(a).encode(enc, 'replace').decode(enc, 'replace') for a in args]
        try: _real_print(*cleaned, **kwargs)
        except Exception: pass
    except Exception: pass


# ─── GSC: per-day data via API ───────────────────────────────────────────────
def fetch_gsc_daily(days=WINDOW_DAYS):
    """Pull per-day clicks/impressions/CTR/position from GSC API.
    Returns list of {'date': 'YYYY-MM-DD', 'clicks', 'impressions', 'ctr', 'position'}."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        print('  [GSC] google-api-python-client not installed — skipping API')
        return []
    if not os.path.exists(GSC_CREDENTIALS):
        print(f'  [GSC] credentials file not found: {GSC_CREDENTIALS} — skipping API')
        return []
    try:
        creds = service_account.Credentials.from_service_account_file(
            GSC_CREDENTIALS,
            scopes=['https://www.googleapis.com/auth/webmasters.readonly'])
        service = build('searchconsole', 'v1', credentials=creds)
        end = datetime.now().date()
        start = end - timedelta(days=days)
        resp = service.searchanalytics().query(
            siteUrl=SITE_DOMAIN,
            body={
                'startDate': start.isoformat(),
                'endDate': end.isoformat(),
                'dimensions': ['date'],
                'rowLimit': 1000,
            }).execute()
        rows = resp.get('rows', [])
        out = []
        for r in rows:
            out.append({
                'date': r['keys'][0],
                'clicks': r.get('clicks', 0),
                'impressions': r.get('impressions', 0),
                'ctr': r.get('ctr', 0) * 100,   # API returns 0.012, we want 1.2
                'position': r.get('position', 0),
            })
        out.sort(key=lambda r: r['date'])
        return out
    except Exception as e:
        print(f'  [GSC] API failed: {e}')
        return []


# ─── Bing: per-day data via API ──────────────────────────────────────────────
def fetch_bing_daily():
    """GetRankAndTrafficStats — Bing's daily traffic per site."""
    import requests
    try:
        r = requests.get(
            'https://ssl.bing.com/webmaster/api.svc/json/GetRankAndTrafficStats',
            params={'apikey': BING_API_KEY, 'siteUrl': BING_SITE_URL.rstrip('/')},
            timeout=30)
        r.raise_for_status()
        rows = r.json().get('d', [])
        out = []
        for row in rows:
            # Bing's date format: "/Date(1716508800000)/" (ms epoch)
            date_str = row.get('Date', '')
            m = re.search(r'/Date\((\d+)\)/', date_str)
            if m:
                ts = int(m.group(1)) / 1000.0
                date = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
            else:
                continue
            impr = row.get('Impressions', 0) or 0
            clicks = row.get('Clicks', 0) or 0
            out.append({
                'date': date,
                'clicks': clicks,
                'impressions': impr,
                'ctr': (clicks / impr * 100) if impr else 0,
                'position': row.get('AvgImpressionPosition', 0) or 0,
            })
        out.sort(key=lambda r: r['date'])
        return out
    except Exception as e:
        print(f'  [BING] GetRankAndTrafficStats failed: {e}')
        return []


# ─── CSV fallback (manual exports) ──────────────────────────────────────────
def _find_reports_dir():
    """Locate latest GA-reports/YYYY-MM-DD/<site>/ for the current site."""
    candidates = [
        os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'GA-reports')),
        r'C:\xampp2\htdocs\nerdy\GA-reports',
    ]
    base = next((c for c in candidates if os.path.isdir(c)), None)
    if not base:
        return None
    site_key = 'nerdy' if 'nerdy' in SITE_DOMAIN.lower() else 'DD'
    dates = sorted([d for d in os.listdir(base) if re.match(r'\d{4}-\d{2}-\d{2}', d)], reverse=True)
    for d in dates:
        site_dir = os.path.join(base, d, site_key)
        if os.path.isdir(site_dir):
            return site_dir
    return None


def load_gsc_chart_csv(reports_dir):
    """GSC Performance ZIP → Chart.csv (per-day data: Date, Clicks, Impressions, CTR, Position)."""
    if not reports_dir:
        return []
    for fn in sorted(os.listdir(reports_dir)):
        if not (fn.endswith('.zip') and 'Performance-on-Search' in fn):
            continue
        path = os.path.join(reports_dir, fn)
        try:
            with zipfile.ZipFile(path) as z:
                if 'Chart.csv' not in z.namelist():
                    continue
                with z.open('Chart.csv') as f:
                    text = f.read().decode('utf-8-sig', errors='replace')
                reader = csv.DictReader(io.StringIO(text))
                out = []
                for row in reader:
                    try:
                        date = (row.get('Date') or '').strip()
                        if not re.match(r'\d{4}-\d{2}-\d{2}', date):
                            continue
                        ctr_str = (row.get('CTR') or '').rstrip('%').strip()
                        out.append({
                            'date': date,
                            'clicks': int(row.get('Clicks') or 0),
                            'impressions': int(row.get('Impressions') or 0),
                            'ctr': float(ctr_str) if ctr_str else 0,
                            'position': float(row.get('Position') or 0),
                        })
                    except (ValueError, TypeError):
                        continue
                out.sort(key=lambda r: r['date'])
                return out
        except Exception as e:
            print(f'  [CSV] failed to read {fn}: {e}')
    return []


def load_bing_overview_csv(reports_dir):
    """Bing SearchPerformanceOverview CSV (Date, Clicks, Impressions, Avg. CTR)."""
    if not reports_dir:
        return []
    for fn in sorted(os.listdir(reports_dir)):
        if 'SearchPerformanceOverview' not in fn or not fn.endswith('.csv'):
            continue
        path = os.path.join(reports_dir, fn)
        try:
            with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
                reader = csv.DictReader(f)
                out = []
                for row in reader:
                    date_str = (row.get('Date') or '').strip()
                    # Bing format: "3/25/2026 12:00:00 AM"
                    try:
                        if ' ' in date_str:
                            date_str = date_str.split(' ')[0]
                        dt = datetime.strptime(date_str, '%m/%d/%Y')
                        date = dt.strftime('%Y-%m-%d')
                    except ValueError:
                        continue
                    try:
                        clicks = int(row.get('Clicks') or 0)
                        impr = int(row.get('Impressions') or 0)
                        ctr_str = (row.get('Avg. CTR') or row.get('CTR') or '').rstrip('%').strip()
                        ctr = float(ctr_str) if ctr_str else (clicks/impr*100 if impr else 0)
                    except (ValueError, TypeError):
                        continue
                    out.append({
                        'date': date, 'clicks': clicks, 'impressions': impr,
                        'ctr': ctr, 'position': 0,
                    })
                out.sort(key=lambda r: r['date'])
                return out
        except Exception as e:
            print(f'  [CSV] failed to read {fn}: {e}')
    return []


# ─── Bucketing + signal detection ────────────────────────────────────────────
def bucket_series(rows, bucket_days=BUCKET_DAYS):
    """Split chronological rows into first30 / mid30 / last30 (or
    proportional thirds if fewer days available)."""
    if not rows:
        return {'first30': [], 'mid30': [], 'last30': []}
    rows = sorted(rows, key=lambda r: r['date'])
    # Use last 3*bucket_days days if available
    if len(rows) >= 3 * bucket_days:
        return {
            'first30': rows[-3 * bucket_days:-2 * bucket_days],
            'mid30':   rows[-2 * bucket_days:-1 * bucket_days],
            'last30':  rows[-1 * bucket_days:],
        }
    # Otherwise split evenly
    third = max(1, len(rows) // 3)
    return {
        'first30': rows[:third],
        'mid30':   rows[third:2 * third],
        'last30':  rows[2 * third:],
    }


def _avg(rows, key):
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 2) if vals else 0


def _sum(rows, key):
    return sum(r.get(key, 0) or 0 for r in rows)


def _delta_pct(old, new):
    if old == 0:
        return None if new == 0 else 100   # "new", any growth from zero is huge
    return round((new - old) / abs(old) * 100, 1)


def summarize_bucket(rows):
    """Per-bucket stats — totals for clicks/impr, weighted-avg for CTR/position."""
    if not rows:
        return {'days': 0, 'clicks_total': 0, 'impr_total': 0,
                'clicks_per_day': 0, 'impr_per_day': 0, 'ctr_avg': 0, 'position_avg': 0}
    clicks_total = _sum(rows, 'clicks')
    impr_total = _sum(rows, 'impressions')
    return {
        'days': len(rows),
        'clicks_total': clicks_total,
        'impr_total': impr_total,
        'clicks_per_day': round(clicks_total / len(rows), 2),
        'impr_per_day': round(impr_total / len(rows), 2),
        'ctr_avg': round(clicks_total / impr_total * 100, 2) if impr_total else 0,
        'position_avg': _avg(rows, 'position'),
    }


def interpret_signal(first, last):
    """Compare first30 vs last30 buckets, classify the trend.
    Returns (signal_code, human-readable message)."""
    if not first['days'] or not last['days']:
        return 'insufficient_data', 'Not enough data to compare periods.'

    clicks_d = _delta_pct(first['clicks_per_day'], last['clicks_per_day']) or 0
    impr_d = _delta_pct(first['impr_per_day'], last['impr_per_day']) or 0
    ctr_d = _delta_pct(first['ctr_avg'], last['ctr_avg']) or 0
    # Position: LOWER is better. Improving = position went DOWN (more negative delta = better)
    pos_d = _delta_pct(first['position_avg'], last['position_avg']) or 0
    pos_improving = pos_d < -STABLE_THRESHOLD_PCT  # avg position decreased

    clicks_up = clicks_d > STABLE_THRESHOLD_PCT
    clicks_down = clicks_d < -STABLE_THRESHOLD_PCT
    impr_up = impr_d > STABLE_THRESHOLD_PCT
    impr_down = impr_d < -STABLE_THRESHOLD_PCT
    ctr_big_up = ctr_d > VIRAL_CTR_DELTA_PCT

    # Order matters: most-specific patterns first
    if clicks_up and impr_down and ctr_big_up:
        return ('viral_spike',
                f'Clicks UP {clicks_d:+.0f}%, impressions DOWN {impr_d:+.0f}%, CTR UP {ctr_d:+.0f}%. '
                'Unusual pattern — possible Google Discover surge, Pinterest traffic, viral share, '
                'or AI Overview citations driving branded queries. Watch closely.')
    if clicks_up and impr_up:
        return ('audience_growth',
                f'Clicks UP {clicks_d:+.0f}%, impressions UP {impr_d:+.0f}%. Genuine audience growth — '
                'more searchers reaching you AND more of them clicking. Keep doing what you\'re doing.')
    if clicks_up and impr_down:
        return ('ctr_improvement_only',
                f'Clicks UP {clicks_d:+.0f}% but impressions DOWN {impr_d:+.0f}%. '
                'Efficiency improved (better titles/meta) but reach shrunk. Don\'t confuse this with growth — '
                'you need to publish more content to expand topical surface area.')
    if clicks_down and impr_up:
        return ('title_degradation',
                f'Clicks DOWN {clicks_d:+.0f}% but impressions UP {impr_d:+.0f}%. '
                'Page is appearing MORE in search but being clicked LESS — title/meta is failing as an ad. '
                'Audit top pages for title quality (run claude analyze for proposals).')
    if clicks_down and impr_down:
        return ('ranking_loss',
                f'Clicks DOWN {clicks_d:+.0f}% AND impressions DOWN {impr_d:+.0f}%. '
                'Both metrics shrinking — likely a ranking drop or recent algorithm change. '
                'Check GSC for any manual actions; run the technical audit to confirm no crawl issues.')
    if pos_improving and not clicks_up:
        return ('ctr_problem_despite_ranking',
                f'Position improving ({pos_d:+.0f}%) but clicks flat ({clicks_d:+.0f}%). '
                'You\'re ranking better but not converting impressions to clicks — CTR fix needed across the board.')
    return ('stable',
            f'All metrics within ±{STABLE_THRESHOLD_PCT}% — no significant change. '
            f'Clicks {clicks_d:+.0f}%, impressions {impr_d:+.0f}%, CTR {ctr_d:+.0f}%, position {pos_d:+.0f}%.')


def analyze_source(name, daily_rows):
    """Run the full 3-bucket analysis for one source (GSC or Bing)."""
    if not daily_rows:
        return {'name': name, 'error': 'no data available'}
    buckets = bucket_series(daily_rows)
    first_summary = summarize_bucket(buckets['first30'])
    mid_summary = summarize_bucket(buckets['mid30'])
    last_summary = summarize_bucket(buckets['last30'])
    signal, message = interpret_signal(first_summary, last_summary)
    return {
        'name': name,
        'days_available': len(daily_rows),
        'date_range': {'start': daily_rows[0]['date'], 'end': daily_rows[-1]['date']},
        'periods': {
            'first30': first_summary,
            'mid30':   mid_summary,
            'last30':  last_summary,
        },
        'deltas': {
            'clicks_per_day_pct':  _delta_pct(first_summary['clicks_per_day'], last_summary['clicks_per_day']),
            'impr_per_day_pct':    _delta_pct(first_summary['impr_per_day'], last_summary['impr_per_day']),
            'ctr_pct':             _delta_pct(first_summary['ctr_avg'], last_summary['ctr_avg']),
            'position_pct':        _delta_pct(first_summary['position_avg'], last_summary['position_avg']),
        },
        'signal': signal,
        'signal_message': message,
    }


# ─── Main runner ──────────────────────────────────────────────────────────────
def run_trend_analysis():
    print(f'[TREND] Site: {SITE_DOMAIN}')
    print('[TREND] Pulling GSC API data...')
    gsc_daily = fetch_gsc_daily(WINDOW_DAYS)
    print(f'  GSC API: {len(gsc_daily)} daily rows')

    print('[TREND] Pulling Bing API data...')
    bing_daily = fetch_bing_daily()
    print(f'  Bing API: {len(bing_daily)} daily rows')

    # CSV fallback if APIs returned empty or limited data
    reports_dir = _find_reports_dir()
    if reports_dir:
        if not gsc_daily:
            print('[TREND] No GSC API data — trying CSV (GSC Performance ZIP → Chart.csv)')
            gsc_daily = load_gsc_chart_csv(reports_dir)
            print(f'  GSC CSV: {len(gsc_daily)} daily rows')
        if not bing_daily:
            print('[TREND] No Bing API data — trying CSV (SearchPerformanceOverview)')
            bing_daily = load_bing_overview_csv(reports_dir)
            print(f'  Bing CSV: {len(bing_daily)} daily rows')

    gsc_analysis = analyze_source('GSC', gsc_daily)
    bing_analysis = analyze_source('Bing', bing_daily)

    output = {
        'generated_at': datetime.now().isoformat(),
        'site': SITE_DOMAIN,
        'window_days': WINDOW_DAYS,
        'bucket_days': BUCKET_DAYS,
        'gsc': gsc_analysis,
        'bing': bing_analysis,
    }

    out_path = os.path.join(OUTPUT_DIR, 'trend-analysis.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f'\n[TREND] JSON saved: {out_path}')

    _write_report(output)

    # Console summary
    print(f"\n{'-' * 72}")
    print(f'SUMMARY - {SITE_DOMAIN}  (3-period trend, last {WINDOW_DAYS} days)')
    for src in ('gsc', 'bing'):
        a = output[src]
        print(f"\n  {a['name']}:")
        if a.get('error'):
            print(f'    {a["error"]}')
            continue
        p = a['periods']
        d = a['deltas']
        print(f"    first30: clicks/day={p['first30']['clicks_per_day']:>7.1f}  impr/day={p['first30']['impr_per_day']:>7.0f}  ctr={p['first30']['ctr_avg']:>5.2f}%  pos={p['first30']['position_avg']:.1f}")
        print(f"    last30:  clicks/day={p['last30']['clicks_per_day']:>7.1f}  impr/day={p['last30']['impr_per_day']:>7.0f}  ctr={p['last30']['ctr_avg']:>5.2f}%  pos={p['last30']['position_avg']:.1f}")
        print(f"    delta:   clicks={d['clicks_per_day_pct']!s:>7}%   impr={d['impr_per_day_pct']!s:>7}%   ctr={d['ctr_pct']!s:>7}%   pos={d['position_pct']!s:>7}%")
        print(f"    SIGNAL:  [{a['signal']}]")
        print(f"             {a['signal_message']}")

    return output


def _write_report(output):
    lines = [
        'TREND ANALYSIS', '=' * 72,
        f"Generated: {output['generated_at']}",
        f"Site: {output['site']}",
        f"Window: last {output['window_days']} days, split into {output['bucket_days']}-day buckets",
        '',
    ]
    for src in ('gsc', 'bing'):
        a = output[src]
        lines.append('-' * 72)
        lines.append(f'{a["name"].upper()}')
        lines.append('-' * 72)
        if a.get('error'):
            lines.append(f'  ERROR: {a["error"]}')
            continue
        lines.append(f"  Days available: {a['days_available']}  ({a['date_range']['start']} → {a['date_range']['end']})")
        lines.append('')
        lines.append(f"  {'period':<10} {'days':>5} {'clicks/d':>10} {'impr/d':>10} {'ctr%':>8} {'pos':>7}")
        for period in ('first30', 'mid30', 'last30'):
            p = a['periods'][period]
            lines.append(f"  {period:<10} {p['days']:>5} {p['clicks_per_day']:>10.1f} "
                         f"{p['impr_per_day']:>10.0f} {p['ctr_avg']:>7.2f}% {p['position_avg']:>7.1f}")
        d = a['deltas']
        lines.append('')
        lines.append(f"  Deltas (first30 → last30):")
        lines.append(f"    clicks/day:  {d['clicks_per_day_pct']!s:>6}%")
        lines.append(f"    impr/day:    {d['impr_per_day_pct']!s:>6}%")
        lines.append(f"    CTR:         {d['ctr_pct']!s:>6}%")
        lines.append(f"    Position:    {d['position_pct']!s:>6}% (negative = improving)")
        lines.append('')
        lines.append(f"  SIGNAL: [{a['signal']}]")
        lines.append(f"    {a['signal_message']}")
        lines.append('')

    report_path = os.path.join(OUTPUT_DIR, 'trend-analysis-report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'[TREND] Report: {report_path}')


if __name__ == '__main__':
    if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    run_trend_analysis()
