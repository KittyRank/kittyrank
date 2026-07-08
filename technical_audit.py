"""
Technical SEO health audit.

Detects structural problems that block content optimization from paying off:
404s wasting crawl budget, 5xx server errors, 301s Google hasn't consolidated,
attachment-page bleed, duplicate URL structures from migrations, and
unprotected high-traffic pages with no backlinks.

Three data sources, gracefully degrading:

  1. Bing Webmaster API (always-on)
     - GetCrawlIssues       → URL + issue type (404, 5xx, etc.)
     - GetPageStats         → live URLs with impressions/clicks (we know these are 200)
     - GetBackLinks         → backlink profile
     - GetUrlBacklinks      → per-URL backlink count

  2. GSC API (always-on)
     - searchAnalytics      → traffic per page
     - sitemaps.list/get    → sitemap health (URLs vs status)
     - urlInspection.index  → per-URL coverage state (rate-limited to ~2000/day,
                              used for top-N suspicious URLs only)

  3. Manual CSV fallback (optional, if present in GA-reports/YYYY-MM-DD/<site>/)
     - SiteExplorerUrls CSV → HTTP code + backlinks + last-crawled per URL
                              (Bing's full inventory — richer than the API)
     - Coverage Critical issues CSV → GSC coverage flags
     - Coverage Non-critical issues CSV → GSC index-status flags

Outputs:
  - technical-audit.json          (full structured findings)
  - technical-audit-report.txt    (human-readable)
"""

import csv
import json
import os
import re
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(__file__))
from config import *

# ─── Tunables ─────────────────────────────────────────────────────────────────
UNPROTECTED_IMPRESSION_THRESHOLD = 500   # impr ≥ this AND backlinks=0 → unprotected
MIN_ACTIVE_BLEED_IMPRESSIONS = 1         # 404s/301s with at least N impr are urgent
HEAD_VERIFY_TOP_N = 50                   # active-HTTP-verify top N suspicious URLs
HEAD_VERIFY_DUP_CAP = 250                # max URLs to HEAD-check during duplicate-pair resolution
HEAD_TIMEOUT_SEC = 10
HEAD_USER_AGENT = 'NerdySEOAuditor/1.0'

# Path prefixes that historically existed as old WP category structures.
# Same slug appearing under multiple prefixes = signal split / canonical issue.
DUPLICATE_PATH_PREFIXES = (
    '/category/', '/tag/', '/author/',
    '/tipstricks/', '/tipstricks/fantasies/', '/tipstricks/men/',
    '/stories/', '/stories/series/',
)

# Attachment-page patterns Bing/GSC sometimes indexes despite Yoast noindex
ATTACHMENT_PATTERNS = (
    re.compile(r'\?attachment_id=\d+', re.I),
    re.compile(r'/[a-z0-9-]+-\d{2,}/?$', re.I),    # foo-bar-1234/ — WP attachment slug pattern
)


# ─── Bing API ────────────────────────────────────────────────────────────────
def _bing_get(endpoint, extra=None):
    """Call any Bing Webmaster API endpoint. Returns list (the 'd' field)."""
    import requests
    params = {'apikey': BING_API_KEY, 'siteUrl': BING_SITE_URL}
    if extra:
        params.update(extra)
    try:
        r = requests.get(f"https://ssl.bing.com/webmaster/api.svc/json/{endpoint}",
                         params=params, timeout=30)
        r.raise_for_status()
        data = r.json().get('d', [])
        return data if isinstance(data, list) else [data]
    except Exception as e:
        print(f"  [BING] {endpoint} failed: {e}")
        return []


def fetch_bing_crawl_issues():
    """GetCrawlIssues — Bing's failing URLs with error reason codes."""
    rows = _bing_get('GetCrawlIssues')
    issues = []
    for r in rows:
        url = r.get('Url') or r.get('url') or ''
        if not url:
            continue
        issues.append({
            'url': url,
            'http_code': r.get('HttpCode'),
            'issue': r.get('Issue') or r.get('IssueDescription') or '',
            'discovery_date': r.get('DiscoveryDate'),
            'severity': r.get('Severity'),
        })
    return issues


def fetch_bing_page_stats():
    """GetPageStats — every URL with impressions, aggregated."""
    rows = _bing_get('GetPageStats')
    agg = defaultdict(lambda: {'impressions': 0, 'clicks': 0})
    for r in rows:
        url = r.get('Query') or r.get('url') or ''   # 'Query' field is URL here
        if not url.startswith('http'):
            continue
        agg[url]['impressions'] += r.get('Impressions', 0)
        agg[url]['clicks'] += r.get('Clicks', 0)
    return dict(agg)


def fetch_bing_backlinks():
    """GetLinkCounts — total backlink counts per URL on the site.
    Falls back to GetBackLinks (raw list) if the count endpoint isn't enabled."""
    counts = {}
    # Try the aggregated counts endpoint first
    rows = _bing_get('GetUrlLinkCounts') or _bing_get('GetLinkCounts')
    for r in rows:
        url = r.get('Url') or r.get('url') or ''
        if url:
            counts[url] = r.get('Count') or r.get('LinkCount') or r.get('Backlinks') or 0
    if counts:
        return counts
    # Fallback: GetBackLinks gives raw rows we can count
    rows = _bing_get('GetBackLinks')
    for r in rows:
        url = r.get('TargetUrl') or r.get('Url') or ''
        if url:
            counts[url] = counts.get(url, 0) + 1
    return counts


# ─── CSV fallback (manual exports) ───────────────────────────────────────────
def _find_latest_reports_dir():
    """Look for GA-reports/YYYY-MM-DD/<site>/ with the most recent date."""
    base = os.path.join(os.path.dirname(SITE_DOMAIN), '..', 'GA-reports')
    # Try alongside the WP install (common pattern)
    candidates = [
        os.path.join(os.path.dirname(__file__), '..', 'GA-reports'),
        r'C:\xampp2\htdocs\nerdy\GA-reports',
    ]
    site_key = 'nerdy' if 'nerdy' in SITE_DOMAIN.lower() else 'DD'
    for base in candidates:
        if not os.path.isdir(base):
            continue
        dates = sorted([d for d in os.listdir(base) if re.match(r'\d{4}-\d{2}-\d{2}', d)], reverse=True)
        for d in dates:
            site_dir = os.path.join(base, d, site_key)
            if os.path.isdir(site_dir):
                return site_dir
    return None


def load_bing_siteexplorer_csv(reports_dir):
    """Load Bing SiteExplorer CSV — fields: URL, Impressions, Clicks, Last crawled,
    Discovered on, HTTP code, Document size, Backlinks.
    Returns {url: {http_code, impressions, clicks, backlinks, ...}} or {}.
    Multiple SiteExplorerUrls CSVs (paginated downloads) are merged."""
    if not reports_dir:
        return {}
    merged = {}
    for fname in sorted(os.listdir(reports_dir)):
        if 'SiteExplorerUrls' not in fname or not fname.endswith('.csv'):
            continue
        path = os.path.join(reports_dir, fname)
        try:
            with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    url = (row.get('URL') or '').strip()
                    if not url:
                        continue
                    try:
                        http_code = int(row.get('HTTP code') or 0)
                    except ValueError:
                        http_code = 0
                    try:
                        backlinks = int(row.get('Backlinks') or 0)
                    except ValueError:
                        backlinks = 0
                    try:
                        impr = int(row.get('Impressions') or 0)
                    except ValueError:
                        impr = 0
                    try:
                        clicks = int(row.get('Clicks') or 0)
                    except ValueError:
                        clicks = 0
                    merged[url] = {
                        'http_code': http_code,
                        'impressions': impr,
                        'clicks': clicks,
                        'backlinks': backlinks,
                        'last_crawled': row.get('Last crawled') or '',
                        'discovered_on': row.get('Discovered on') or '',
                        'document_size': row.get('Document size') or '',
                    }
        except Exception as e:
            print(f"  [CSV] failed to read {fname}: {e}")
    return merged


def load_gsc_coverage_csvs(reports_dir):
    """Load GSC Coverage zip(s) → Critical issues + Non-critical issues + Metadata."""
    if not reports_dir:
        return {'critical': [], 'noncritical': [], 'totals': {}}
    out = {'critical': [], 'noncritical': [], 'totals': {}}
    for fname in sorted(os.listdir(reports_dir)):
        if not (fname.endswith('.zip') and 'Coverage' in fname):
            continue
        path = os.path.join(reports_dir, fname)
        try:
            with zipfile.ZipFile(path) as z:
                for inner in z.namelist():
                    if inner == 'Critical issues.csv':
                        out['critical'].extend(_csv_rows_from_zip(z, inner))
                    elif inner == 'Non-critical issues.csv':
                        out['noncritical'].extend(_csv_rows_from_zip(z, inner))
                    elif inner == 'Metadata.csv':
                        for row in _csv_rows_from_zip(z, inner):
                            if row:
                                out['totals'].update(row)
        except Exception as e:
            print(f"  [CSV] failed to read {fname}: {e}")
    return out


def _csv_rows_from_zip(zf, inner_name):
    import io
    try:
        with zf.open(inner_name) as f:
            text = f.read().decode('utf-8-sig', errors='replace')
        reader = csv.DictReader(io.StringIO(text))
        return [dict(r) for r in reader]
    except Exception:
        return []


# ─── Active HTTP verification ────────────────────────────────────────────────
def head_verify(urls):
    """Issue HEAD requests against suspicious URLs to confirm current status.
    Returns {url: (status_code, redirect_target_or_empty)}."""
    import requests
    out = {}
    s = requests.Session()
    s.headers.update({'User-Agent': HEAD_USER_AGENT})
    for u in urls:
        try:
            r = s.head(u, timeout=HEAD_TIMEOUT_SEC, allow_redirects=False)
            target = r.headers.get('Location', '') if 300 <= r.status_code < 400 else ''
            out[u] = (r.status_code, target)
        except Exception as e:
            out[u] = (0, str(e)[:80])
    return out


# ─── Detection rules ──────────────────────────────────────────────────────────
def _slug_of(url):
    """Normalize URL → bare slug (lowercase, trailing slash stripped)."""
    p = urlparse(url)
    path = p.path.strip('/').lower()
    # Strip known path prefixes that mark old categorized structures
    for prefix in DUPLICATE_PATH_PREFIXES:
        clean = prefix.strip('/')
        if path.startswith(clean + '/'):
            path = path[len(clean) + 1:]
    return path


def _is_attachment_url(url):
    return any(p.search(url) for p in ATTACHMENT_PATTERNS)


def detect_findings(bing_url_data, bing_crawl_issues, backlinks_map, head_results):
    """Apply all detection rules. Returns flat list of finding dicts."""
    findings = []

    # Build a single normalized view: {url: {http_code, impr, clicks, backlinks, source}}
    universe = {}
    for url, d in bing_url_data.items():
        universe[url] = {
            'url': url,
            'http_code': d.get('http_code') or 200,    # PageStats URLs are typically live
            'impressions': d.get('impressions', 0),
            'clicks': d.get('clicks', 0),
            'backlinks': d.get('backlinks', 0),
            'source': 'bing',
        }
    # Layer in crawl-issue data (overrides http_code if Bing flagged it)
    for issue in bing_crawl_issues:
        url = issue['url']
        if url not in universe:
            universe[url] = {'url': url, 'impressions': 0, 'clicks': 0, 'backlinks': 0, 'source': 'bing_crawl_issue'}
        universe[url]['http_code'] = issue.get('http_code') or universe[url].get('http_code', 0)
        universe[url]['issue'] = issue.get('issue', '')
    # Backlinks override from dedicated map (more accurate)
    for url, count in backlinks_map.items():
        if url in universe:
            universe[url]['backlinks'] = count
    # HEAD-verified URLs override http_code
    for url, (status, redirect_target) in head_results.items():
        if url in universe and status > 0:
            universe[url]['http_code_verified'] = status
            if redirect_target:
                universe[url]['redirect_target'] = redirect_target

    # ─── Rule 1: active_bleed (404 with impressions > 0) ───
    for url, d in universe.items():
        code = d.get('http_code_verified') or d.get('http_code', 200)
        if code == 404 and d['impressions'] >= MIN_ACTIVE_BLEED_IMPRESSIONS:
            findings.append({
                'rule': 'active_bleed',
                'severity': 'high',
                'url': url,
                'impressions': d['impressions'],
                'clicks': d['clicks'],
                'message': f'404 with {d["impressions"]} active impressions — users hitting dead page',
                'fix': 'Either restore page, or accept 404 + use GSC Removals tool to suppress',
            })

    # ─── Rule 2: server_error_critical (5xx) ───
    for url, d in universe.items():
        code = d.get('http_code_verified') or d.get('http_code', 0)
        if 500 <= code < 600:
            findings.append({
                'rule': 'server_error_critical',
                'severity': 'critical',
                'url': url,
                'impressions': d['impressions'],
                'http_code': code,
                'message': f'HTTP {code} server error — investigate immediately',
                'fix': 'Check server logs; this URL is returning 5xx and blocking indexing',
            })

    # ─── Rule 3: redirect_not_consolidated (301 with impressions > 0) ───
    for url, d in universe.items():
        code = d.get('http_code_verified') or d.get('http_code', 0)
        if code in (301, 302, 307, 308) and d['impressions'] >= MIN_ACTIVE_BLEED_IMPRESSIONS:
            target = d.get('redirect_target', '')
            findings.append({
                'rule': 'redirect_not_consolidated',
                'severity': 'medium',
                'url': url,
                'impressions': d['impressions'],
                'clicks': d['clicks'],
                'redirect_target': target,
                'message': f'{code} redirect with {d["impressions"]} impr still hitting old URL — search engine not consolidated',
                'fix': 'Submit destination URL via GSC URL Inspection → Request Indexing to speed consolidation',
            })

    # ─── Rule 4: attachment_waste (WP attachment URLs in index) ───
    attachment_urls = [d for d in universe.values() if _is_attachment_url(d['url'])]
    if attachment_urls:
        findings.append({
            'rule': 'attachment_waste',
            'severity': 'medium',
            'count': len(attachment_urls),
            'sample_urls': [d['url'] for d in attachment_urls[:5]],
            'message': f'{len(attachment_urls)} attachment-style URLs indexed (?attachment_id= or slug-N pattern)',
            'fix': 'In Yoast SEO → Search Appearance → Media → set "Redirect attachment URLs" to YES (or add noindex globally)',
        })

    # ─── Rule 5: duplicate vs pending-consolidation (same slug, multiple URLs) ───
    slug_map = defaultdict(list)
    for url, d in universe.items():
        code = d.get('http_code_verified') or d.get('http_code', 0)
        # Include 200 AND 3xx — 3xx URLs still appear in search engines until consolidated
        if code != 200 and not (300 <= code < 400):
            continue
        slug = _slug_of(url)
        if slug:
            slug_map[slug].append(d)

    # HEAD-check every URL in any multi-URL slug group so we can use LIVE status
    # (not stale CSV status) when classifying. Critical for cases like
    # /write-and-save-files-in-python — CSV says 200 (last crawl), live says 404.
    pairs_to_verify = []
    for slug, dlist in slug_map.items():
        if len(dlist) > 1:
            pairs_to_verify.extend([d['url'] for d in dlist])
    if pairs_to_verify:
        missing = [u for u in pairs_to_verify if u not in head_results]
        if missing:
            head_results.update(head_verify(missing[:HEAD_VERIFY_DUP_CAP]))

    for slug, dlist in slug_map.items():
        if len(dlist) <= 1:
            continue
        dlist.sort(key=lambda x: x['clicks'], reverse=True)
        urls_in_pair = [d['url'] for d in dlist]
        # Check each URL's live HTTP status (live trumps CSV — CSV is days/weeks stale)
        live_status = {u: head_results.get(u, (None, ''))[0] for u in urls_in_pair}
        redirect_targets = {u: head_results.get(u, (None, ''))[1] for u in urls_in_pair}

        # Pre-classify: if ANY url in the pair is now 404/5xx live, this isn't
        # a duplicate problem — it's a stale-index problem (Bing/GSC still
        # showing a URL that the server has since killed). The active_bleed
        # rule will surface those 404s separately from live data anyway.
        any_dead = any((s and (s == 404 or 500 <= s < 600)) for s in live_status.values())
        any_redirect_to_sibling = False
        for u in urls_in_pair:
            status = live_status.get(u)
            target = (redirect_targets.get(u) or '').rstrip('/')
            other_urls = {other.rstrip('/') for other in urls_in_pair if other != u}
            if status in (301, 302, 307, 308) and target in other_urls:
                any_redirect_to_sibling = True
                break

        if any_dead:
            # Surface as stale_index — distinct from active_bleed because at least
            # one stale URL had been counted as duplicate before live verification
            total_impr = sum(d['impressions'] for d in dlist)
            dead_urls = [d for d in dlist if live_status.get(d['url']) in (404,) or
                          (live_status.get(d['url']) and 500 <= live_status.get(d['url']) < 600)]
            findings.append({
                'rule': 'stale_index',
                'severity': 'medium',
                'slug': slug,
                'urls': [{
                    'url': d['url'],
                    'impressions': d['impressions'],
                    'clicks': d['clicks'],
                    'csv_http_code': d.get('http_code'),
                    'live_status': live_status.get(d['url']),
                } for d in dlist],
                'impressions': total_impr,
                'message': f'Slug "{slug}" — Bing/GSC show {len(dlist)} indexed URLs but {len(dead_urls)} return 404/5xx live ({total_impr} stale impr)',
                'fix': 'URLs are dead but still indexed. Either restore the page, or submit for removal via GSC Removals + Bing Block URLs to suppress.',
            })
            continue

        if any_redirect_to_sibling:
            # Server is already redirecting — search engine just hasn't recrawled
            total_impr = sum(d['impressions'] for d in dlist)
            # Canonical = the one that's NOT a redirect (i.e. live 200), or highest clicks if ambiguous
            canonicals = [d for d in dlist if live_status.get(d['url']) == 200]
            canonical_url = canonicals[0]['url'] if canonicals else dlist[0]['url']
            findings.append({
                'rule': 'redirect_not_consolidated',
                'severity': 'medium',
                'slug': slug,
                'canonical_url': canonical_url,
                'duplicate_urls': [{
                    'url': d['url'],
                    'impressions': d['impressions'],
                    'clicks': d['clicks'],
                    'live_status': live_status.get(d['url']),
                } for d in dlist],
                'impressions': total_impr,
                'message': f'Slug "{slug}" — 301 in place but Bing/GSC still shows both URLs ({total_impr} total impr split)',
                'fix': 'Server-side redirect is working. Submit canonical URL via GSC URL Inspection → Request Indexing AND via Bing URL Submission to speed consolidation.',
            })
        else:
            # All URLs return 200 (or status unknown) — genuine duplicate, no redirect set up
            all_200 = all(s == 200 for s in live_status.values() if s)
            if not all_200:
                continue  # mixed/unknown statuses — skip to avoid false alarm
            findings.append({
                'rule': 'active_duplicate',
                'severity': 'high',
                'slug': slug,
                'canonical_candidate': dlist[0]['url'],
                'duplicate_urls': [{
                    'url': d['url'],
                    'impressions': d['impressions'],
                    'clicks': d['clicks'],
                    'live_status': live_status.get(d['url']),
                } for d in dlist],
                'message': f'Slug "{slug}" served from {len(dlist)} different URLs, all return 200 — active signal split',
                'fix': 'Add 301 from non-winning URLs to canonical, OR set rel=canonical on duplicates pointing to the winner.',
            })

    # ─── Rule 6: unprotected_high_traffic (high impr, 0 backlinks) ───
    for url, d in universe.items():
        code = d.get('http_code_verified') or d.get('http_code', 200)
        if code != 200:
            continue
        if d['impressions'] >= UNPROTECTED_IMPRESSION_THRESHOLD and d['backlinks'] == 0:
            findings.append({
                'rule': 'unprotected_high_traffic',
                'severity': 'medium',
                'url': url,
                'impressions': d['impressions'],
                'clicks': d['clicks'],
                'message': f'{d["impressions"]} impr but 0 backlinks — outreach target',
                'fix': 'High-traffic page with no external link equity. Target for backlink outreach.',
            })

    return findings


# ─── Main runner ──────────────────────────────────────────────────────────────
def _ensure_utf8_stdout():
    """Force stdout/stderr to UTF-8 so print() doesn't crash on Windows when
    we emit box-drawing chars or arrows. Safe to call multiple times. Needed
    because the dashboard runs us in a background thread where __main__'s
    reconfigure never runs."""
    if sys.platform == 'win32':
        for stream_name in ('stdout', 'stderr'):
            s = getattr(sys, stream_name, None)
            if s is not None and hasattr(s, 'reconfigure'):
                try:
                    s.reconfigure(encoding='utf-8', errors='replace')
                except Exception:
                    pass


# Override `print` in THIS module with an encoding-safe version + a file
# tee. Bulletproof fix — any unicode char that can't be encoded to the
# terminal still gets logged to the audit-run.log file, and falls back to
# "?" replacement on the terminal instead of raising.
import builtins as _builtins
_real_print = _builtins.print

# Log file — written next to OUTPUT_DIR; created lazily on first print
_LOG_PATH = None
def _log_file_path():
    global _LOG_PATH
    if _LOG_PATH is None:
        _LOG_PATH = os.path.join(OUTPUT_DIR, 'technical-audit-run.log')
        # Truncate at start of each run
        try:
            with open(_LOG_PATH, 'w', encoding='utf-8') as f:
                from datetime import datetime as _dt
                f.write(f"=== Technical audit started {_dt.now().isoformat()} ===\n")
        except Exception:
            pass
    return _LOG_PATH

def print(*args, **kwargs):
    # Always log to file (utf-8, safe)
    try:
        line = ' '.join(str(a) for a in args)
        with open(_log_file_path(), 'a', encoding='utf-8') as lf:
            lf.write(line + '\n')
    except Exception:
        pass
    # Also write to terminal — but never raise on encoding issues
    try:
        _real_print(*args, **kwargs)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, 'encoding', 'ascii') or 'ascii'
        cleaned = [str(a).encode(enc, 'replace').decode(enc, 'replace') for a in args]
        try:
            _real_print(*cleaned, **kwargs)
        except Exception:
            pass
    except Exception:
        pass


def run_audit():
    _ensure_utf8_stdout()
    print(f"[TECH-AUDIT] Site: {SITE_DOMAIN}")
    print("[TECH-AUDIT] Pulling Bing API data...")
    bing_crawl_issues = fetch_bing_crawl_issues()
    bing_page_stats = fetch_bing_page_stats()
    bing_backlinks = fetch_bing_backlinks()
    print(f"  GetCrawlIssues: {len(bing_crawl_issues)} crawl-error URLs")
    print(f"  GetPageStats: {len(bing_page_stats)} URLs with traffic")
    print(f"  Backlinks: {len(bing_backlinks)} URLs with backlink data")

    # CSV fallback for richer per-URL HTTP codes (Bing SiteExplorer)
    reports_dir = _find_latest_reports_dir()
    if reports_dir:
        print(f"[TECH-AUDIT] Loading manual CSV reports from {reports_dir}")
        csv_data = load_bing_siteexplorer_csv(reports_dir)
        gsc_coverage = load_gsc_coverage_csvs(reports_dir)
        print(f"  SiteExplorer CSV: {len(csv_data)} URLs")
        print(f"  GSC Coverage: {len(gsc_coverage['critical'])} critical + "
              f"{len(gsc_coverage['noncritical'])} non-critical issues")
    else:
        csv_data = {}
        gsc_coverage = {'critical': [], 'noncritical': [], 'totals': {}}
        print("[TECH-AUDIT] No manual CSV reports found - using API data only")

    # Merge sources: CSV is richer for HTTP codes; API has live traffic
    merged = dict(csv_data)
    for url, d in bing_page_stats.items():
        if url in merged:
            # CSV already has http_code — update traffic from API (more recent)
            merged[url]['impressions'] = max(merged[url].get('impressions', 0), d['impressions'])
            merged[url]['clicks'] = max(merged[url].get('clicks', 0), d['clicks'])
        else:
            merged[url] = {
                'http_code': 200, 'impressions': d['impressions'], 'clicks': d['clicks'],
                'backlinks': bing_backlinks.get(url, 0),
            }

    # HEAD-verify top-N suspicious URLs (anything 3xx/4xx/5xx OR unusual codes)
    suspicious = sorted(
        [(u, d) for u, d in merged.items() if d.get('http_code', 200) != 200],
        key=lambda x: -x[1].get('impressions', 0),
    )[:HEAD_VERIFY_TOP_N]
    head_results = {}
    if suspicious:
        print(f"[TECH-AUDIT] HEAD-verifying top {len(suspicious)} non-200 URLs...")
        head_results = head_verify([u for u, _ in suspicious])

    # Detect findings
    print("[TECH-AUDIT] Applying detection rules...")
    findings = detect_findings(merged, bing_crawl_issues, bing_backlinks, head_results)

    # Group + sort
    by_rule = defaultdict(list)
    for f in findings:
        by_rule[f['rule']].append(f)
    severity_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    findings.sort(key=lambda f: (severity_order.get(f.get('severity'), 9), -f.get('impressions', 0)))

    # Sitemap pollution check
    sitemap_findings = _check_sitemap_health(merged)
    findings.extend(sitemap_findings)
    for f in sitemap_findings:
        by_rule[f['rule']].append(f)

    output = {
        'generated_at': datetime.now().isoformat(),
        'site': SITE_DOMAIN,
        'totals': {
            'urls_audited': len(merged),
            'live_200': sum(1 for d in merged.values() if d.get('http_code', 200) == 200),
            'errors_4xx': sum(1 for d in merged.values() if 400 <= d.get('http_code', 0) < 500),
            'redirects_3xx': sum(1 for d in merged.values() if 300 <= d.get('http_code', 0) < 400),
            'errors_5xx': sum(1 for d in merged.values() if 500 <= d.get('http_code', 0) < 600),
            'findings': len(findings),
            'critical_findings': sum(1 for f in findings if f.get('severity') == 'critical'),
            'high_findings': sum(1 for f in findings if f.get('severity') == 'high'),
        },
        'findings_by_rule': {rule: len(items) for rule, items in by_rule.items()},
        'findings': findings,
        'gsc_coverage_summary': {
            'critical_count': len(gsc_coverage['critical']),
            'noncritical_count': len(gsc_coverage['noncritical']),
            'metadata': gsc_coverage.get('totals', {}),
        },
    }

    json_path = os.path.join(OUTPUT_DIR, 'technical-audit.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n[TECH-AUDIT] JSON: {json_path}")

    _write_report(output)

    # Console summary  (ASCII divider — works even if stdout reconfigure failed)
    print(f"\n{'-' * 72}")
    print(f"SUMMARY - {SITE_DOMAIN}")
    print(f"  URLs audited:    {output['totals']['urls_audited']}")
    print(f"  Live 200s:       {output['totals']['live_200']}")
    print(f"  Redirects (3xx): {output['totals']['redirects_3xx']}")
    print(f"  Errors (4xx):    {output['totals']['errors_4xx']}")
    print(f"  Errors (5xx):    {output['totals']['errors_5xx']}")
    print(f"  Findings:        {output['totals']['findings']} "
          f"({output['totals']['critical_findings']} critical + "
          f"{output['totals']['high_findings']} high)")
    print(f"\n  by rule:")
    for rule, count in sorted(by_rule.items(), key=lambda x: -len(x[1])):
        print(f"    {rule:<32} {count if isinstance(count, int) else len(count):>4}")


def _check_sitemap_health(merged):
    """Pull sitemap.xml and flag entries that aren't 200 in our audit."""
    import requests
    findings = []
    sitemap_urls = [f"{SITE_DOMAIN.rstrip('/')}/sitemap.xml",
                    f"{SITE_DOMAIN.rstrip('/')}/sitemap_index.xml"]
    found_xml = None
    for sm in sitemap_urls:
        try:
            r = requests.get(sm, timeout=15, headers={'User-Agent': HEAD_USER_AGENT})
            if r.status_code == 200 and '<' in r.text[:100]:
                found_xml = r.text
                break
        except Exception:
            continue
    if not found_xml:
        return []
    # Extract <loc>...</loc> entries
    entries = re.findall(r'<loc>([^<]+)</loc>', found_xml)
    sitemap_pollution = []
    for u in entries:
        d = merged.get(u.strip())
        if d and d.get('http_code', 200) != 200:
            sitemap_pollution.append({'url': u.strip(), 'http_code': d.get('http_code')})
    if sitemap_pollution:
        findings.append({
            'rule': 'sitemap_pollution',
            'severity': 'medium',
            'count': len(sitemap_pollution),
            'sample_urls': sitemap_pollution[:10],
            'message': f'{len(sitemap_pollution)} sitemap entries return non-200 — crawlers keep revisiting dead URLs',
            'fix': 'Regenerate sitemap (Yoast: SEO → Tools → Reset sitemap) so it only lists live 200 URLs',
        })
    return findings


def _write_report(output):
    lines = [
        "TECHNICAL SEO HEALTH AUDIT",
        "=" * 72,
        f"Generated: {output['generated_at']}",
        f"Site: {output['site']}",
        "",
        f"URLs audited:    {output['totals']['urls_audited']}",
        f"  Live 200s:     {output['totals']['live_200']}",
        f"  Redirects 3xx: {output['totals']['redirects_3xx']}",
        f"  Errors 4xx:    {output['totals']['errors_4xx']}",
        f"  Errors 5xx:    {output['totals']['errors_5xx']}",
        "",
        f"Findings: {output['totals']['findings']} "
        f"({output['totals']['critical_findings']} critical, "
        f"{output['totals']['high_findings']} high)",
        "",
        "─" * 72,
    ]

    severity_groups = defaultdict(list)
    for f in output['findings']:
        severity_groups[f.get('severity', 'low')].append(f)

    for sev in ('critical', 'high', 'medium', 'low'):
        items = severity_groups.get(sev, [])
        if not items:
            continue
        lines.append(f"\n{sev.upper()} SEVERITY  ({len(items)} findings)")
        lines.append("─" * 72)
        for f in items[:50]:
            lines.append(f"\n  [{f['rule']}] {f.get('message', '')}")
            if 'url' in f:
                lines.append(f"    URL:   {f['url']}")
            if 'impressions' in f and f['impressions']:
                lines.append(f"    impr:  {f['impressions']}  clicks: {f.get('clicks', 0)}")
            if 'http_code' in f and f['http_code']:
                lines.append(f"    HTTP:  {f['http_code']}")
            if 'redirect_target' in f and f['redirect_target']:
                lines.append(f"    → {f['redirect_target']}")
            if 'sample_urls' in f:
                lines.append(f"    Sample URLs ({min(5, len(f['sample_urls']))} of {f.get('count', len(f['sample_urls']))}):")
                for u in f['sample_urls'][:5]:
                    if isinstance(u, dict):
                        lines.append(f"      {u.get('url', u)}  [HTTP {u.get('http_code', '?')}]")
                    else:
                        lines.append(f"      {u}")
            if 'duplicate_urls' in f:
                lines.append(f"    Duplicates of slug '{f.get('slug', '')}':")
                for d in f['duplicate_urls'][:5]:
                    lines.append(f"      {d['url']}  impr={d['impressions']} clicks={d['clicks']}")
            lines.append(f"    FIX:   {f.get('fix', '')}")

    report_path = os.path.join(OUTPUT_DIR, 'technical-audit-report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"[TECH-AUDIT] Report: {report_path}")


if __name__ == '__main__':
    if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    run_audit()
