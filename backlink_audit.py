"""
Backlink audit — domain-level + per-URL profile of incoming links.

Data sources (in order of preference):
  1. Bing API GetLinkCounts + GetUrlLinks (often returns empty for newer
     sites or specific API key tiers — graceful no-op when sparse)
  2. CSV fallback (most reliable in practice):
     - <site>_ReferringDomains_*.csv  — domain + backlink count per domain
     - <site>_SiteExplorerUrls_*.csv  — per-URL Backlinks column (already
                                         loaded by technical_audit; reused here)

What it computes:
  - Total referring domains + their authority class (edu/gov/aggregator/relevant/unknown)
  - Top backlinked pages on the site
  - Backlink concentration: how many pages have ZERO backlinks vs >=1
  - Unprotected high-traffic pages (impressions ≥ 500 + 0 backlinks) — same
    rule as technical_audit, surfaced here as the outreach priority list

Output:
  - backlink-audit.json
  - backlink-audit-report.txt
"""

import csv
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(__file__))
from config import *

# ─── Tunables ─────────────────────────────────────────────────────────────────
HIGH_TRAFFIC_IMPRESSIONS = 500
TOP_DOMAINS_TO_SHOW = 30
TOP_PAGES_TO_SHOW = 30


# ─── Encoding-safe print ──────────────────────────────────────────────────────
import builtins as _builtins
_real_print = _builtins.print
def print(*args, **kwargs):
    try: _real_print(*args, **kwargs)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, 'encoding', 'ascii') or 'ascii'
        cleaned = [str(a).encode(enc, 'replace').decode(enc, 'replace') for a in args]
        try: _real_print(*cleaned, **kwargs)
        except Exception: pass
    except Exception: pass


# ─── Domain authority classification ─────────────────────────────────────────
# Lightweight heuristic — for serious analysis, integrate with Ahrefs/Moz API
DOMAIN_CLASSES = {
    'high_authority': {
        'tlds': {'.edu', '.gov', '.mil', '.ac.uk', '.ac.in', '.gov.uk', '.gov.in'},
        'patterns': {'wikipedia.org', 'github.com', 'stackoverflow.com', 'medium.com',
                     'reddit.com', 'arxiv.org', 'mit.edu', 'stanford.edu'},
    },
    'relevant_tech': {
        'patterns': {'arduino.cc', 'eevblog.com', 'hackaday.com', 'sparkfun.com',
                     'adafruit.com', 'embedded.com', 'edn.com', 'electronicdesign.com',
                     'allaboutcircuits.com', 'circuitcellar.com', 'instructables.com',
                     'esp32.com', 'arduinoforum.de', 'avrfreaks.net', 'hackster.io'},
    },
    'aggregator': {
        # Scrapers + content syndicators — pass low link equity
        'patterns': {'vuink.com', 'grokipedia.com', 'wikiwand.com', 'archive.org',
                     'webcache.googleusercontent.com', 'translate.google.com',
                     'translate.googleusercontent.com', 'cached.com'},
    },
}


def classify_domain(domain):
    """Return ('high_authority' | 'relevant' | 'aggregator' | 'unknown', explanation)."""
    d = domain.lower().lstrip('https://').lstrip('http://').lstrip('www.').rstrip('/')
    for tld in DOMAIN_CLASSES['high_authority']['tlds']:
        if d.endswith(tld):
            return 'high_authority', f'TLD {tld}'
    for pat in DOMAIN_CLASSES['high_authority']['patterns']:
        if pat in d:
            return 'high_authority', f'known auth: {pat}'
    for pat in DOMAIN_CLASSES['relevant_tech']['patterns']:
        if pat in d:
            return 'relevant', f'known tech: {pat}'
    for pat in DOMAIN_CLASSES['aggregator']['patterns']:
        if pat in d:
            return 'aggregator', f'aggregator: {pat}'
    return 'unknown', ''


# ─── Bing API (best-effort, often empty) ──────────────────────────────────────
def fetch_bing_link_counts():
    """GetLinkCounts → sitewide totals. Returns dict or empty dict."""
    import requests
    try:
        r = requests.get('https://ssl.bing.com/webmaster/api.svc/json/GetLinkCounts',
                         params={'apikey': BING_API_KEY, 'siteUrl': BING_SITE_URL.rstrip('/')},
                         timeout=10)
        r.raise_for_status()
        return r.json().get('d', {}) or {}
    except Exception as e:
        print(f'  [BING API] GetLinkCounts failed: {e}')
        return {}


# ─── CSV loaders ──────────────────────────────────────────────────────────────
def _find_reports_dir():
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


def load_referring_domains_csv(reports_dir):
    """Bing ReferringDomains CSV — fields: Domain, Backlinks Count.
    Returns [{'domain': str, 'backlinks': int, 'class': str, 'class_reason': str}]."""
    if not reports_dir:
        return []
    for fn in sorted(os.listdir(reports_dir)):
        if 'ReferringDomains' not in fn or not fn.endswith('.csv'):
            continue
        path = os.path.join(reports_dir, fn)
        try:
            with open(path, encoding='utf-8-sig', errors='replace') as f:
                reader = csv.DictReader(f)
                rows = []
                for row in reader:
                    domain = (row.get('Domain') or row.get('Referring Domain') or '').strip()
                    if not domain:
                        continue
                    try:
                        count = int(row.get('Backlinks Count') or row.get('Count') or 0)
                    except (ValueError, TypeError):
                        count = 0
                    cls, reason = classify_domain(domain)
                    rows.append({
                        'domain': domain,
                        'backlinks': count,
                        'class': cls,
                        'class_reason': reason,
                    })
                rows.sort(key=lambda x: -x['backlinks'])
                return rows
        except Exception as e:
            print(f'  [CSV] failed to read {fn}: {e}')
    return []


def load_site_explorer_per_url(reports_dir):
    """Reuse SiteExplorer CSV loading (same as technical_audit) to get per-URL
    backlink counts + impressions. Returns {url: {backlinks, impressions, clicks}}."""
    if not reports_dir:
        return {}
    out = {}
    for fn in sorted(os.listdir(reports_dir)):
        if 'SiteExplorerUrls' not in fn or not fn.endswith('.csv'):
            continue
        path = os.path.join(reports_dir, fn)
        try:
            with open(path, encoding='utf-8-sig', errors='replace') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    url = (row.get('URL') or '').strip()
                    if not url:
                        continue
                    try:
                        bl = int(row.get('Backlinks') or 0)
                        impr = int(row.get('Impressions') or 0)
                        clicks = int(row.get('Clicks') or 0)
                    except (ValueError, TypeError):
                        bl, impr, clicks = 0, 0, 0
                    if url not in out or impr > out[url]['impressions']:
                        out[url] = {'backlinks': bl, 'impressions': impr, 'clicks': clicks}
        except Exception as e:
            print(f'  [CSV] failed to read {fn}: {e}')
    return out


# ─── Main analysis ────────────────────────────────────────────────────────────
def run_audit():
    print(f'[BACKLINK-AUDIT] Site: {SITE_DOMAIN}')

    # Try Bing API (often returns empty — that's fine, CSV is the fallback)
    api_counts = fetch_bing_link_counts()
    api_total_pages = api_counts.get('TotalPages', 0)
    if api_total_pages > 0:
        print(f'  Bing API: {api_total_pages} pages with link data')
    else:
        print('  Bing API: returned no data (common for newer sites / certain key tiers)')

    # CSV is the real source of truth for this audit
    reports_dir = _find_reports_dir()
    if reports_dir:
        print(f'  Loading from {reports_dir}')
        domains = load_referring_domains_csv(reports_dir)
        per_url = load_site_explorer_per_url(reports_dir)
        print(f'  ReferringDomains CSV: {len(domains)} domains')
        print(f'  SiteExplorer CSV: {len(per_url)} URLs')
    else:
        print('  No GA-reports folder found — backlink audit cannot proceed')
        domains, per_url = [], {}

    # Aggregate domain classes
    by_class = {'high_authority': 0, 'relevant': 0, 'aggregator': 0, 'unknown': 0}
    backlinks_by_class = {'high_authority': 0, 'relevant': 0, 'aggregator': 0, 'unknown': 0}
    for d in domains:
        by_class[d['class']] = by_class.get(d['class'], 0) + 1
        backlinks_by_class[d['class']] = backlinks_by_class.get(d['class'], 0) + d['backlinks']

    # Per-URL classification
    urls_with_backlinks = sorted(
        [{'url': u, **d} for u, d in per_url.items() if d['backlinks'] > 0],
        key=lambda x: -x['backlinks']
    )
    urls_no_backlinks = [u for u, d in per_url.items() if d['backlinks'] == 0]
    unprotected_high_traffic = sorted(
        [{'url': u, **d} for u, d in per_url.items()
         if d['backlinks'] == 0 and d['impressions'] >= HIGH_TRAFFIC_IMPRESSIONS],
        key=lambda x: -x['impressions']
    )

    total_backlinks = sum(d['backlinks'] for d in domains) or sum(d['backlinks'] for d in per_url.values())
    total_referring_domains = len(domains)
    total_pages_with_backlinks = len(urls_with_backlinks)
    total_pages = len(per_url)
    coverage_pct = round(total_pages_with_backlinks / total_pages * 100, 1) if total_pages else 0

    output = {
        'generated_at': datetime.now().isoformat(),
        'site': SITE_DOMAIN,
        'totals': {
            'referring_domains': total_referring_domains,
            'total_backlinks': total_backlinks,
            'pages_with_backlinks': total_pages_with_backlinks,
            'pages_without_backlinks': len(urls_no_backlinks),
            'coverage_pct': coverage_pct,
            'unprotected_high_traffic': len(unprotected_high_traffic),
        },
        'domain_classes': {
            'counts': by_class,
            'backlinks_per_class': backlinks_by_class,
        },
        'top_referring_domains': domains[:TOP_DOMAINS_TO_SHOW],
        'top_backlinked_pages': urls_with_backlinks[:TOP_PAGES_TO_SHOW],
        'unprotected_high_traffic': unprotected_high_traffic[:TOP_PAGES_TO_SHOW],
        'api_signal': {
            'bing_total_pages': api_total_pages,
            'csv_used': bool(reports_dir),
        },
    }

    out_path = os.path.join(OUTPUT_DIR, 'backlink-audit.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f'\n[BACKLINK-AUDIT] JSON saved: {out_path}')

    _write_report(output)

    # Console summary
    print(f"\n{'-' * 72}")
    print(f'SUMMARY - {SITE_DOMAIN}')
    t = output['totals']
    print(f"  Referring domains:    {t['referring_domains']}")
    print(f"  Total backlinks:      {t['total_backlinks']}")
    print(f"  Pages WITH backlinks: {t['pages_with_backlinks']} / {total_pages} ({t['coverage_pct']}%)")
    print(f"  Unprotected high-traffic (impr>={HIGH_TRAFFIC_IMPRESSIONS} + 0 backlinks): {t['unprotected_high_traffic']}")
    print()
    print(f"  Domain classes:")
    for cls in ('high_authority', 'relevant', 'aggregator', 'unknown'):
        n = by_class.get(cls, 0)
        bl = backlinks_by_class.get(cls, 0)
        if n:
            print(f"    {cls:<18} {n} domains  ({bl} backlinks)")
    if domains:
        print(f"\n  Top referring domains:")
        for d in domains[:8]:
            cls_label = d['class'].replace('_', ' ')
            print(f"    {d['backlinks']:>4}  [{cls_label:<14}]  {d['domain']}")
    if unprotected_high_traffic:
        print(f"\n  Top outreach targets (high-traffic, no backlinks):")
        for u in unprotected_high_traffic[:8]:
            print(f"    impr={u['impressions']:>6}  clicks={u['clicks']:>4}  {u['url'][:75]}")

    return output


def _write_report(output):
    lines = [
        'BACKLINK AUDIT', '=' * 72,
        f"Generated: {output['generated_at']}",
        f"Site: {output['site']}",
        '',
    ]
    t = output['totals']
    lines.append(f"Referring domains:        {t['referring_domains']}")
    lines.append(f"Total backlinks:          {t['total_backlinks']}")
    lines.append(f"Pages WITH backlinks:     {t['pages_with_backlinks']}")
    lines.append(f"Pages WITHOUT backlinks:  {t['pages_without_backlinks']}")
    lines.append(f"Coverage:                 {t['coverage_pct']}% of URLs have at least 1 backlink")
    lines.append(f"Unprotected high-traffic: {t['unprotected_high_traffic']} pages (impr>={HIGH_TRAFFIC_IMPRESSIONS} + 0 backlinks)")
    lines.append('')

    lines.append('-' * 72)
    lines.append('DOMAIN CLASSES')
    lines.append('-' * 72)
    for cls in ('high_authority', 'relevant', 'aggregator', 'unknown'):
        n = output['domain_classes']['counts'].get(cls, 0)
        bl = output['domain_classes']['backlinks_per_class'].get(cls, 0)
        if n:
            lines.append(f"  {cls:<18}  {n} domains, {bl} backlinks")
    lines.append('')

    if output['top_referring_domains']:
        lines.append('-' * 72)
        lines.append(f"TOP REFERRING DOMAINS (showing {len(output['top_referring_domains'])})")
        lines.append('-' * 72)
        for d in output['top_referring_domains']:
            lines.append(f"  {d['backlinks']:>5}  [{d['class']:<14}] {d['domain']}")
            if d.get('class_reason'):
                lines.append(f"          ({d['class_reason']})")
        lines.append('')

    if output['top_backlinked_pages']:
        lines.append('-' * 72)
        lines.append(f"TOP BACKLINKED PAGES ON YOUR SITE")
        lines.append('-' * 72)
        for p in output['top_backlinked_pages']:
            lines.append(f"  {p['backlinks']:>3} backlinks  impr={p['impressions']:>5}  {p['url']}")
        lines.append('')

    if output['unprotected_high_traffic']:
        lines.append('-' * 72)
        lines.append('OUTREACH PRIORITY: HIGH TRAFFIC + ZERO BACKLINKS')
        lines.append(f'  These {len(output["unprotected_high_traffic"])} pages get significant search traffic but')
        lines.append('  have NO external link equity. Each backlink to these pages has')
        lines.append('  compounding effect on rank stability.')
        lines.append('-' * 72)
        for p in output['unprotected_high_traffic']:
            lines.append(f"  impr={p['impressions']:>6}  clicks={p['clicks']:>4}  {p['url']}")
        lines.append('')

    report_path = os.path.join(OUTPUT_DIR, 'backlink-audit-report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'[BACKLINK-AUDIT] Report: {report_path}')


if __name__ == '__main__':
    if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    run_audit()
