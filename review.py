"""
KittyRank — Web Dashboard
Single entry point for the entire SEO pipeline: fetch data, upload reports,
generate fixes with Claude, review and approve/reject changes.

Usage:
  python review.py                    # Start dashboard (local WordPress)
  python review.py --live             # Start dashboard (live WordPress)
  python review.py --port 8888        # Use custom port
"""

import io
import json
import os
import re
import signal
import sys
import string
import time
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
import urllib3

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

# Config may not exist yet on first run — handle that path before the import bombs.
_CONFIG_PATH = os.path.join(_HERE, 'config.py')
CONFIG_MISSING = not os.path.exists(_CONFIG_PATH)
if CONFIG_MISSING:
    # Create a minimal stub so the dashboard can boot and serve the setup wizard.
    _stub = "\n".join([
        "import os",
        "SITE_DOMAIN = 'https://example.com'",
        "SITE_NAME = '(unconfigured)'",
        "SITE_DESCRIPTION = ''",
        "SITE_BRAND_TOKENS = []",
        "CORNERSTONE_SLUGS = {}",
        "GSC_CREDENTIALS = ''",
        "BING_API_KEY = ''",
        "BING_SITE_URL = ''",
        "ANTHROPIC_API_KEY = ''",
        "DIRECT_MODEL = 'claude-sonnet-4-6'",
        "USE_BEDROCK = False",
        "AWS_REGION = 'us-east-1'",
        "BEDROCK_MODEL = 'anthropic.claude-sonnet-4-6:0'",
        "GA4_PROPERTY_ID = ''",
        "GA4_CREDENTIALS = ''",
        "MAX_PAGES_TO_ANALYZE = 20",
        "POSITION_RANGE = (3, 20)",
        "MIN_PAGE_IMPRESSIONS = 25",
        "BING_KEYWORD_FETCH_LIMIT = 200",
        "GSC_DAYS_BACK = 60",
        "GSC_ROW_LIMIT = 5000",
        "GSC_SITE_URL = ''",
        "MIN_IMPRESSIONS = 5",
        "TITLE_SUFFIX = ''",
        "OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')",
        "os.makedirs(OUTPUT_DIR, exist_ok=True)",
        "",
    ])
    with open(_CONFIG_PATH, 'w', encoding='utf-8') as f:
        f.write(_stub)
    print('[SETUP] No config.py found — created stub. Setup wizard will open in the dashboard.')

from config import *

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─── Global State ───────────────────────────────────────────────────────────
STATE = {
    'fixes': {},
    'live': False,
    'session': None,
    'api_url': None,
    'logs': [],
}


# ─── Pipeline State (for running fetch/analyze/claude from the UI) ────────
PIPELINE_STATE = {
    'fetch':   {'status': 'not_started', 'message': '', 'error': ''},
    'upload':  {'status': 'not_started', 'filename': '', 'path': ''},
    'analyze': {'status': 'not_started', 'message': '', 'error': ''},
    'audit':    {'status': 'idle'},
    'trends':   {'status': 'idle'},
    'backlinks':{'status': 'idle'},
    'cannibal': {'status': 'idle'},
    'logs': [],
}
PIPELINE_LOCK = threading.Lock()


class TeeStream:
    """Writes to real stdout and a buffer list simultaneously.
    Mimics enough of the file API to replace sys.stdout safely."""
    def __init__(self, real_stdout, buffer_list):
        self.real = real_stdout
        self._buf = buffer_list
        # Expose attributes that libraries check for
        self.encoding = getattr(real_stdout, 'encoding', 'utf-8')
        self.errors = getattr(real_stdout, 'errors', 'replace')
        self.newlines = getattr(real_stdout, 'newlines', None)
        # Expose real stdout's byte buffer so libraries that do
        # io.TextIOWrapper(sys.stdout.buffer, ...) get the real buffer,
        # not our accumulation list.
        if hasattr(real_stdout, 'buffer'):
            self.buffer = real_stdout.buffer

    def write(self, s):
        try:
            self.real.write(s)
        except Exception:
            pass
        with PIPELINE_LOCK:
            self._buf.append(str(s))
        return len(s)

    def flush(self):
        try:
            self.real.flush()
        except Exception:
            pass

    def fileno(self):
        return self.real.fileno()

    def isatty(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    def __getattr__(self, name):
        # Forward any other attribute access to the real stream
        return getattr(self.real, name)


def pipeline_log(msg):
    """Add a pipeline-specific log entry."""
    with PIPELINE_LOCK:
        PIPELINE_STATE['logs'].append({
            'time': datetime.now().strftime('%H:%M:%S'),
            'message': msg,
        })
        if len(PIPELINE_STATE['logs']) > 300:
            PIPELINE_STATE['logs'] = PIPELINE_STATE['logs'][-300:]


def run_pipeline_step(step_name, callable_fn, *args):
    """Run a pipeline step in a background thread with stdout capture."""
    def wrapper():
        buf = []
        tee = TeeStream(sys.stdout, buf)
        old_stdout = sys.stdout
        sys.stdout = tee
        try:
            with PIPELINE_LOCK:
                PIPELINE_STATE[step_name]['status'] = 'running'
                PIPELINE_STATE[step_name]['message'] = 'Starting...'
                PIPELINE_STATE[step_name]['error'] = ''
            callable_fn(*args)
            with PIPELINE_LOCK:
                PIPELINE_STATE[step_name]['status'] = 'done'
                output = ''.join(buf)
                PIPELINE_STATE[step_name]['message'] = output[-2000:] if len(output) > 2000 else output
        except Exception as e:
            with PIPELINE_LOCK:
                PIPELINE_STATE[step_name]['status'] = 'error'
                PIPELINE_STATE[step_name]['error'] = str(e)
                output = ''.join(buf)
                PIPELINE_STATE[step_name]['message'] = output[-2000:] if len(output) > 2000 else output
        finally:
            sys.stdout = old_stdout

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return t


def do_fetch_step():
    """Run fetch GSC + Bing + GA4 + merge."""
    from fetch import fetch_gsc_data
    from fetch_bing import fetch_bing_data
    from fetch_ga4 import fetch_ga4_data
    from merge_data import merge_opportunities
    print("[STEP 1/4] Fetching Google Search Console data...\n")
    fetch_gsc_data()
    print("\n[STEP 2/4] Fetching Bing Webmaster data...\n")
    fetch_bing_data()
    print("\n[STEP 3/4] Fetching Google Analytics 4 data (bounce rate)...\n")
    fetch_ga4_data()
    print("\n[STEP 4/4] Merging GSC + Bing + GA4 data...\n")
    merge_opportunities()
    print("\nFetch complete.")


def do_analyze_step(report_path=None):
    """Run analyze → Claude → CrawlyCat (if report uploaded)."""
    from analyze import analyze_all
    from claude_analyze import run_claude_analysis
    print("[STEP 1/3] Analyzing live pages...\n")
    analyze_all()
    print("\n[STEP 2/3] Running Claude analysis...\n")
    run_claude_analysis()
    if report_path and os.path.exists(report_path):
        print(f"\n[STEP 3/3] Processing CrawlyCat report...\n")
        from crawl_fix import run as run_crawl_fix
        run_crawl_fix(report_path, use_claude=True, generate_only=True)
    else:
        print("\n[STEP 3/3] No CrawlyCat report — skipping")
    # Reload all fixes into the review state
    STATE['fixes'] = load_all_fixes()
    # Restore saved statuses
    saved_statuses, _, saved_dates = load_state()
    STATE['applied_dates'] = saved_dates
    for slug, status in saved_statuses.items():
        if slug in STATE['fixes'] and status in ('approved', 'rejected'):
            STATE['fixes'][slug]['status'] = status
    print("\nAnalysis complete. Switch to Review tab.")


def detect_pipeline_state():
    """Check which output files exist and pre-set pipeline step statuses."""
    opps = os.path.join(OUTPUT_DIR, 'merged-opportunities.json')
    gsc = os.path.join(OUTPUT_DIR, 'gsc-raw.json')
    analysis = os.path.join(OUTPUT_DIR, 'proposed-fixes.json')

    if os.path.exists(opps) or os.path.exists(gsc):
        PIPELINE_STATE['fetch']['status'] = 'done'
        PIPELINE_STATE['fetch']['message'] = 'Previous output found'

    if os.path.exists(analysis):
        PIPELINE_STATE['analyze']['status'] = 'done'
        PIPELINE_STATE['analyze']['message'] = 'Previous output found'

    # Check for uploaded crawlycat report
    saved_report = os.path.join(OUTPUT_DIR, 'crawlycat-report.html')
    if os.path.exists(saved_report):
        PIPELINE_STATE['upload']['status'] = 'done'
        PIPELINE_STATE['upload']['filename'] = 'crawlycat-report.html'
        PIPELINE_STATE['upload']['path'] = saved_report


STATE_FILE = os.path.join(OUTPUT_DIR, 'review-state.json')


def _detect_report_type(filename):
    """Identify which report a filename represents — used to show a friendly
    label in the Reports tab and to validate uploads."""
    fn = filename.lower()
    if 'siteexplorerurls' in fn:
        return 'bing_siteexplorer'
    if 'searchperformanceoverview' in fn:
        return 'bing_search_performance'
    if 'aiperformanceoverviewstats' in fn:
        return 'bing_ai_citations'
    if 'referringdomains' in fn:
        return 'bing_referring_domains'
    if 'failingurls' in fn:
        return 'bing_failing_urls'
    if 'performance-on-search' in fn:
        return 'gsc_performance'
    if 'coverage' in fn:
        return 'gsc_coverage'
    if 'sitemaps' in fn:
        return 'gsc_sitemaps'
    return 'unknown'


def save_state():
    """Persist fix statuses, applied dates, and logs to disk."""
    data = {
        'statuses': {slug: fix['status'] for slug, fix in STATE['fixes'].items()},
        'applied_dates': STATE.get('applied_dates', {}),
        'logs': STATE['logs'][-200:],
        'last_saved': datetime.now().isoformat(),
    }
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_state():
    """Load persisted statuses, applied dates, and logs from disk."""
    if not os.path.exists(STATE_FILE):
        return {}, [], {}
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('statuses', {}), data.get('logs', []), data.get('applied_dates', {})
    except Exception:
        return {}, [], {}



def add_log(level, message):
    """Add a log entry."""
    entry = {
        'time': datetime.now().strftime('%H:%M:%S'),
        'level': level,
        'message': message,
    }
    STATE['logs'].append(entry)
    if len(STATE['logs']) > 500:
        STATE['logs'] = STATE['logs'][-500:]
    prefix = {'info': '[INFO]', 'success': '[OK]', 'error': '[ERR]', 'warn': '[WARN]'}
    try:
        print(f"  {prefix.get(level, '[LOG]')} {message}", flush=True)
    except UnicodeEncodeError:
        # Strip non-ASCII for terminal display only — STATE['logs'] keeps
        # the full unicode message for the dashboard
        enc = getattr(sys.stdout, 'encoding', 'ascii') or 'ascii'
        safe = message.encode(enc, 'replace').decode(enc, 'replace')
        try:
            print(f"  {prefix.get(level, '[LOG]')} {safe}", flush=True)
        except Exception:
            pass
    except (ValueError, OSError):
        # stdout may have been closed (e.g. a GC'd TextIOWrapper closed the
        # underlying buffer). The log entry is already in STATE['logs'] for the
        # UI; never let a dead console break the apply/indexing flow.
        pass


# ─── Fix Loading ────────────────────────────────────────────────────────────
def load_all_fixes():
    """Load and merge fixes from all pipeline sources."""
    fixes = {}

    # Source 1: Claude SEO analysis
    claude_path = os.path.join(OUTPUT_DIR, 'proposed-fixes.json')
    if os.path.exists(claude_path):
        with open(claude_path, 'r', encoding='utf-8') as f:
            claude_fixes = json.load(f)
        for fix in claude_fixes:
            slug = fix['slug']
            if not slug:
                continue
            fixes[slug] = {
                'slug': slug,
                'source': 'seo-pipeline',
                'source_label': 'GSC + Bing Keywords',
                'changes': {},
                'reason': fix.get('reason', ''),
                'target_keyword': fix.get('target_keyword', ''),
                'status': 'pending',
            }
            if fix.get('yoast_title'):
                fixes[slug]['changes']['title'] = {
                    'new': fix['yoast_title'], 'old': '',
                }
            if fix.get('yoast_metadesc'):
                fixes[slug]['changes']['metadesc'] = {
                    'new': fix['yoast_metadesc'], 'old': '',
                }
            if fix.get('intro_add'):
                fixes[slug]['changes']['intro'] = {
                    'new': fix['intro_add'], 'old': '(prepend to first paragraph)',
                }
            if fix.get('ctr_diagnosis'):
                fixes[slug]['ctr_diagnosis'] = fix['ctr_diagnosis']
            if fix.get('content_gaps'):
                fixes[slug]['content_gaps'] = fix['content_gaps']
            if fix.get('keyword_tier'):
                fixes[slug]['keyword_tier'] = fix['keyword_tier']
                fixes[slug]['keyword_tier_label'] = fix.get('keyword_tier_label', '')
                fixes[slug]['keyword_position'] = fix.get('keyword_position', 0)
            if fix.get('track'):
                fixes[slug]['track'] = fix['track']
            if fix.get('opportunity_score') is not None:
                fixes[slug]['opportunity_score'] = fix['opportunity_score']
            if fix.get('link_from'):
                fixes[slug]['link_from'] = fix['link_from']
            if fix.get('analyzed_at'):
                fixes[slug]['analyzed_at'] = fix['analyzed_at']
            if fix.get('run_id'):
                fixes[slug]['run_id'] = fix['run_id']
            if fix.get('engagement_warning'):
                fixes[slug]['engagement_warning'] = fix['engagement_warning']
            if fix.get('quality_warning'):
                fixes[slug]['quality_warning'] = fix['quality_warning']
        add_log('info', f'Loaded {len(claude_fixes)} fixes from proposed-fixes.json')

    # Enrich fixes with keyword ranking data from merged opportunities
    opp_path = os.path.join(OUTPUT_DIR, 'merged-opportunities.json')
    if not os.path.exists(opp_path):
        opp_path = os.path.join(OUTPUT_DIR, 'opportunities.json')
    if os.path.exists(opp_path):
        with open(opp_path, 'r', encoding='utf-8') as f:
            opps = json.load(f)
        opp_by_slug = {o['slug']: o for o in opps}
        for slug, fix in fixes.items():
            if slug in opp_by_slug:
                opp = opp_by_slug[slug]
                kws = opp.get('keywords', [])[:5]
                fix['keywords'] = [
                    {'query': k['query'], 'position': round(k['position'], 1),
                     'impressions': k['impressions'], 'clicks': k.get('clicks', 0),
                     'ctr': k.get('ctr', 0), 'source': k.get('source', 'gsc')}
                    for k in kws
                ]
                # Attach GA4 page-level metrics if available
                if opp.get('bounce_rate') is not None:
                    fix['bounce_rate'] = opp['bounce_rate']
                    fix['sessions'] = opp.get('sessions', 0)
                    fix['avg_session_duration'] = opp.get('avg_session_duration', 0)

    # Source 2: CrawlyCat meta description fixes
    crawl_path = os.path.join(OUTPUT_DIR, 'crawl-metadesc-fixes.json')
    if os.path.exists(crawl_path):
        with open(crawl_path, 'r', encoding='utf-8') as f:
            crawl_fixes = json.load(f)
        crawl_count = 0
        for fix in crawl_fixes:
            slug = fix['slug']
            if not slug:
                continue
            if slug in fixes:
                fixes[slug]['conflict'] = {
                    'source': 'crawlycat',
                    'metadesc': fix['new_desc'],
                    'issue': fix['issue'],
                }
            else:
                fixes[slug] = {
                    'slug': slug,
                    'source': 'crawlycat',
                    'source_label': f"CrawlyCat ({fix['issue']})",
                    'changes': {
                        'metadesc': {
                            'new': fix['new_desc'],
                            'old': fix.get('old_desc', ''),
                        }
                    },
                    'reason': f"Meta description {fix['issue']} ({fix.get('new_length', '?')}c)",
                    'target_keyword': '',
                    'status': 'pending',
                }
                crawl_count += 1
        add_log('info', f'Loaded {crawl_count} fixes from crawl-metadesc-fixes.json')

    # Source 3: CrawlyCat title fixes
    title_path = os.path.join(OUTPUT_DIR, 'crawl-title-fixes.json')
    if os.path.exists(title_path):
        with open(title_path, 'r', encoding='utf-8') as f:
            title_fixes = json.load(f)
        title_count = 0
        for fix in title_fixes:
            slug = fix['slug']
            if not slug:
                continue
            if slug in fixes:
                # Add title change to existing fix
                fixes[slug]['changes']['title'] = {
                    'new': fix['new_title'],
                    'old': fix.get('old_title', ''),
                }
            else:
                fixes[slug] = {
                    'slug': slug,
                    'source': 'crawlycat',
                    'source_label': f"CrawlyCat ({fix['issue']})",
                    'changes': {
                        'title': {
                            'new': fix['new_title'],
                            'old': fix.get('old_title', ''),
                        }
                    },
                    'reason': f"Title {fix['issue']} ({fix.get('new_length', '?')}c)",
                    'target_keyword': '',
                    'status': 'pending',
                }
                title_count += 1
        add_log('info', f'Loaded {title_count} fixes from crawl-title-fixes.json')

    # Safety-net quality gate: drop any title/metadesc change that is truncated,
    # dangling, out-of-range or a no-op, regardless of which source produced it.
    import seo_quality
    rejected = []
    for slug, fix in list(fixes.items()):
        changes = fix.get('changes', {})
        kw = fix.get('target_keyword', '')
        if 'title' in changes:
            ok, why = seo_quality.validate_title(
                changes['title'].get('new', ''),
                original=changes['title'].get('old', ''), keyword=kw)
            if not ok:
                rejected.append(f"{slug} title ({why})")
                changes.pop('title', None)
        if 'metadesc' in changes:
            ok, why = seo_quality.validate_metadesc(
                changes['metadesc'].get('new', ''), keyword=kw)
            if not ok:
                rejected.append(f"{slug} meta ({why})")
                changes.pop('metadesc', None)
        if not changes:
            fixes.pop(slug, None)
    if rejected:
        add_log('warn', f'Quality gate rejected {len(rejected)} change(s): '
                        + '; '.join(rejected[:8])
                        + (' …' if len(rejected) > 8 else ''))

    return fixes


# ─── WordPress API ──────────────────────────────────────────────────────────
def _slug_candidates(raw):
    """A WP post_name is only the last path segment, so a fix whose slug is a
    URL path ('stories/kolkata-...') or 'homepage' won't match ?slug= directly.
    Return the candidate slugs to try, most specific first."""
    s = (raw or '').strip().strip('/')
    if s in ('', 'homepage', 'home', 'index', 'front-page'):
        return ['home', 'homepage', 'front-page']
    last = s.split('/')[-1]
    cands = [s] if '/' not in s else []
    if last and last not in cands:
        cands.append(last)
    return cands


# ─── CrawlyCat Processing ──────────────────────────────────────────────────
def process_crawlycat_report(report_path):
    """Parse a CrawlyCat report and generate fixes."""
    add_log('info', f'Processing CrawlyCat report: {os.path.basename(report_path)}')

    try:
        from crawl_fix import (parse_report, scrape_page_content,
                                claude_fix_descriptions, claude_fix_titles,
                                validate_issues_live)

        report = parse_report(report_path)
        desc_issues = report['meta_desc_missing'] + report['meta_desc_length']
        title_issues = report['meta_title_length']
        h1_issues = report['h1_multiple']
        broken = report['broken_links']

        add_log('info', f'Found {len(desc_issues)} desc issues, '
                        f'{len(title_issues)} title issues, '
                        f'{len(h1_issues)} H1 issues, '
                        f'{len(broken)} broken links')

        total_fixes = 0

        # --- Meta description fixes ---
        if desc_issues:
            add_log('info', f'Validating {len(desc_issues)} description issues against live site...')
            desc_issues = validate_issues_live(desc_issues)
            add_log('info', f'{len(desc_issues)} description issues remain after live validation')

            if desc_issues:
                add_log('info', 'Scraping pages for Claude analysis...')
                for item in desc_issues:
                    if 'page_info' not in item:
                        item['page_info'] = scrape_page_content(item['url']) or {}
                    item['current_desc'] = item.get('page_info', {}).get('meta_description', '')

                add_log('info', 'Generating descriptions with Claude...')
                fixes = claude_fix_descriptions(desc_issues)

                fix_records = []
                for item in desc_issues:
                    slug = item['slug']
                    if slug in fixes:
                        fix_records.append({
                            'slug': slug,
                            'post_id': None,
                            'post_type': item.get('post_type', 'posts'),
                            'issue': item['issue'],
                            'old_desc': item.get('current_desc', ''),
                            'new_desc': fixes[slug],
                            'new_length': len(fixes[slug])
                        })

                output_path = os.path.join(OUTPUT_DIR, 'crawl-metadesc-fixes.json')
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(fix_records, f, indent=2, ensure_ascii=False)
                add_log('success', f'Saved {len(fix_records)} meta description fixes')
                total_fixes += len(fix_records)

        # --- Title fixes ---
        if title_issues:
            add_log('info', f'Validating {len(title_issues)} title issues against live site...')
            title_issues = validate_issues_live(title_issues)
            add_log('info', f'{len(title_issues)} title issues remain after live validation')

            if title_issues:
                add_log('info', 'Scraping pages for Claude title analysis...')
                for item in title_issues:
                    if 'page_info' not in item:
                        item['page_info'] = scrape_page_content(item['url']) or {}

                add_log('info', 'Generating title fixes with Claude...')
                title_fixes = claude_fix_titles(title_issues)

                title_records = []
                for item in title_issues:
                    slug = item['slug']
                    if slug in title_fixes:
                        title_records.append({
                            'slug': slug,
                            'post_id': None,
                            'post_type': item.get('post_type', 'posts'),
                            'issue': item['issue'],
                            'old_title': item.get('current_title', ''),
                            'new_title': title_fixes[slug],
                            'new_length': len(title_fixes[slug])
                        })

                output_path = os.path.join(OUTPUT_DIR, 'crawl-title-fixes.json')
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(title_records, f, indent=2, ensure_ascii=False)
                add_log('success', f'Saved {len(title_records)} title fixes')
                total_fixes += len(title_records)

        # --- Report-only issues ---
        if h1_issues:
            add_log('warn', f'{len(h1_issues)} pages with multiple H1 tags (manual fix needed)')
        if broken:
            add_log('warn', f'{len(broken)} broken internal links (manual fix needed)')

        # Reload all fixes
        STATE['fixes'] = load_all_fixes()
        # Restore saved statuses
        saved_statuses, _, saved_dates = load_state()
        STATE['applied_dates'] = saved_dates
        for slug, status in saved_statuses.items():
            if slug in STATE['fixes'] and status in ('approved', 'rejected'):
                STATE['fixes'][slug]['status'] = status

        return total_fixes

    except Exception as e:
        add_log('error', f'CrawlyCat processing failed: {e}')
        import traceback
        add_log('error', traceback.format_exc())
        return 0


# ─── Setup Wizard ───────────────────────────────────────────────────────────

def _is_unconfigured():
    """True when config.py is still the stub from first-run (SITE_DOMAIN is the placeholder)."""
    try:
        return SITE_DOMAIN == 'https://example.com' or not BING_API_KEY
    except NameError:
        return True


def _setup_state_snapshot():
    """Return current config values for the wizard form. Secret-bearing fields
    are returned as MASKED so we don't leak via JSON, but the wizard can detect
    whether they're already set."""
    def _mask(v):
        if not v: return ''
        s = str(v)
        if len(s) <= 8: return '••••' if s else ''
        return s[:4] + '•' * (len(s) - 8) + s[-4:]
    return {
        'unconfigured': _is_unconfigured(),
        'site_domain': SITE_DOMAIN if SITE_DOMAIN != 'https://example.com' else '',
        'site_name': SITE_NAME if SITE_NAME != '(unconfigured)' else '',
        'site_description': SITE_DESCRIPTION or '',
        'site_brand_tokens': ' '.join(SITE_BRAND_TOKENS or []),
        'gsc_credentials': GSC_CREDENTIALS or '',          # path, not secret
        'bing_api_key_masked': _mask(BING_API_KEY),
        'bing_site_url': BING_SITE_URL or '',
        'anthropic_api_key_masked': _mask(ANTHROPIC_API_KEY),
        'direct_model': globals().get('DIRECT_MODEL', 'claude-sonnet-4-6'),
        'ga4_property_id': globals().get('GA4_PROPERTY_ID', ''),
        'gsc_days_back': globals().get('GSC_DAYS_BACK', 60),
        'min_impressions': globals().get('MIN_IMPRESSIONS', 5),
    }


def _setup_save_config(form):
    """Write the wizard's form values to config.py. Preserves any existing
    SECRETS if the form sent empty/masked values for them (so re-saving the
    wizard without re-entering keys doesn't clobber them)."""
    site_domain = (form.get('site_domain') or '').strip().rstrip('/')
    site_name = (form.get('site_name') or '').strip()
    site_description = (form.get('site_description') or '').strip()

    brand_tokens_raw = (form.get('site_brand_tokens') or '').strip()
    brand_tokens = [t.strip().lower() for t in brand_tokens_raw.replace(',', ' ').split() if t.strip()]

    gsc_creds = (form.get('gsc_credentials') or '').strip()

    # Secrets: if form value is empty OR the masked sentinel, keep the existing one
    def _keep_or_replace(form_val, existing):
        v = (form_val or '').strip()
        if not v or '•' in v:
            return existing or ''
        return v

    bing_api_key = _keep_or_replace(form.get('bing_api_key'), globals().get('BING_API_KEY', ''))
    bing_site_url = (form.get('bing_site_url') or site_domain + '/').strip()
    anthropic_key = _keep_or_replace(form.get('anthropic_api_key'), globals().get('ANTHROPIC_API_KEY', ''))
    direct_model = (form.get('direct_model') or 'claude-sonnet-4-6').strip()

    ga4_property_id = (form.get('ga4_property_id') or '').strip()

    # Tunables
    def _safe_int(v, default, lo=None, hi=None):
        try:
            n = int(v)
        except (ValueError, TypeError):
            return default
        if lo is not None and n < lo: return lo
        if hi is not None and n > hi: return hi
        return n
    gsc_days_back = _safe_int(form.get('gsc_days_back'), 60, lo=7, hi=490)
    min_impressions = _safe_int(form.get('min_impressions'), 5, lo=1, hi=10000)

    # Basic validation
    if not site_domain or not site_domain.startswith('http'):
        return {'success': False, 'error': 'Site domain must start with https:// or http://'}
    if not site_name:
        return {'success': False, 'error': 'Site name is required'}

    # Build config.py content
    out_lines = [
        '"""',
        'Configuration for KittyRank.',
        'Generated by the setup wizard. Re-run the wizard from the Settings tab',
        'to update. You CAN edit this file by hand — wizard preserves your edits.',
        '"""',
        '',
        'import os',
        '',
        '# ─── Site identity ───────────────────────────────────────────────',
        f'SITE_DOMAIN = {site_domain!r}',
        f'SITE_NAME = {site_name!r}',
        f'SITE_DESCRIPTION = {site_description!r}',
        f'SITE_BRAND_TOKENS = {brand_tokens!r}',
        '',
        '# Cornerstone slugs — define your pillar/hub posts here for the',
        '# cornerstone link auditor. Type A = content pillar (search-heavy),',
        '# Type B = architectural hub (curated index page).',
        'CORNERSTONE_SLUGS = {',
        '    # \'your-pillar-slug\': {\'type\': \'A\'},',
        '    # \'your-hub-slug\':    {\'type\': \'B\'},',
        '}',
        '',
        '# ─── Google Search Console ────────────────────────────────────────',
        f'GSC_CREDENTIALS = os.environ.get(\'GSC_CREDENTIALS\', {gsc_creds!r})',
        '',
        '# --- Google login (OAuth) ---',
        f"GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', {globals().get('GOOGLE_OAUTH_CLIENT_ID', '')!r})",
        f"GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', {globals().get('GOOGLE_OAUTH_CLIENT_SECRET', '')!r})",
        '',
        '# ─── Bing Webmaster Tools ─────────────────────────────────────────',
        f'BING_API_KEY = os.environ.get(\'BING_API_KEY\', {bing_api_key!r})',
        f'BING_SITE_URL = os.environ.get(\'BING_SITE_URL\', {bing_site_url!r})',
        '',
        '# ─── Anthropic (Claude) ───────────────────────────────────────────',
        f'ANTHROPIC_API_KEY = os.environ.get(\'ANTHROPIC_API_KEY\', {anthropic_key!r})',
        f'DIRECT_MODEL = {direct_model!r}',
        'USE_BEDROCK = False',
        'AWS_REGION = \'us-east-1\'',
        'BEDROCK_MODEL = \'anthropic.claude-sonnet-4-6:0\'',
        '',
        '# ─── Google Analytics 4 (optional) ────────────────────────────────',
        f'GA4_PROPERTY_ID = os.environ.get(\'GA4_PROPERTY_ID\', {ga4_property_id!r})',
        f'GA4_CREDENTIALS = os.environ.get(\'GA4_CREDENTIALS\', GSC_CREDENTIALS)',
        '',
        '# ─── Pipeline tunables ────────────────────────────────────────────',
        'MAX_PAGES_TO_ANALYZE = 20',
        'POSITION_RANGE = (1, 50)',
        'MIN_PAGE_IMPRESSIONS = 25',
        'BING_KEYWORD_FETCH_LIMIT = 200',
        '',
        '# GSC date range + per-keyword filtering',
        f'GSC_DAYS_BACK = {gsc_days_back}',
        'GSC_ROW_LIMIT = 5000',
        'GSC_SITE_URL = f\'sc-domain:{SITE_DOMAIN.replace("https://", "").replace("http://", "").rstrip("/")}\'',
        f'MIN_IMPRESSIONS = {min_impressions}',
        '',
        '# Title suffix appended by SEO plugins (e.g. " - YourSite") — for length checks',
        'TITLE_SUFFIX = f\' - {SITE_NAME}\'',
        '',
        'OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), \'output\')',
        'os.makedirs(OUTPUT_DIR, exist_ok=True)',
        '',
    ]
    try:
        with open(_CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write('\n'.join(out_lines))
    except Exception as e:
        return {'success': False, 'error': f'Failed to write config: {e}'}

    # Hot-reload config in the running process so the user doesn't need to restart.
    # We re-import config and copy its UPPER-case names into review.py's module globals
    # so all the existing references (SITE_NAME, GSC_CREDENTIALS, etc.) see new values.
    reload_warn = None
    try:
        import importlib, config as _cfg_mod
        importlib.reload(_cfg_mod)
        for k in dir(_cfg_mod):
            if k.isupper() and not k.startswith('_'):
                globals()[k] = getattr(_cfg_mod, k)
    except Exception as e:
        reload_warn = f'config hot-reload failed ({e}); changes take effect on next restart'

    add_log('success', f'Config saved via setup wizard for {site_domain}'
            + (f' [WARN: {reload_warn}]' if reload_warn else ' [hot-reloaded]'))
    return {'success': True, 'restart_required': bool(reload_warn),
            'message': 'Saved. Reloading dashboard…' if not reload_warn else reload_warn}


SETUP_WIZARD_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Setup — KittyRank</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f0f2f5; color: #333; line-height: 1.5; margin: 0;
         padding: 24px; font-size: 16px; }
  .wizard { max-width: 760px; margin: 0 auto; background: #fff; border-radius: 12px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.08); padding: 32px 38px; }
  h1 { color: #1a1a2e; margin: 0 0 6px; }
  .subtitle { color: #666; font-size: 0.92em; margin-bottom: 30px; }
  .edition-badge { display: inline-block; background: #0066cc; color: #fff;
                   padding: 2px 10px; border-radius: 12px; font-size: 0.75em;
                   font-weight: 600; vertical-align: middle; margin-left: 8px; }
  .edition-badge.premium { background: #28a745; }
  .section { margin-bottom: 28px; padding: 18px 22px; background: #fafafa;
             border-radius: 8px; border-left: 4px solid #0066cc; }
  .section h3 { margin: 0 0 8px; font-size: 1em; color: #1a1a2e; }
  .section .help { color: #666; font-size: 0.85em; margin-bottom: 14px; line-height: 1.55; }
  .section .help a { color: #0066cc; }
  .field { margin-bottom: 14px; }
  .field label { display: block; font-size: 0.85em; font-weight: 600;
                 color: #444; margin-bottom: 4px; }
  .field label .required { color: #dc3545; }
  .field input, .field select, .field textarea {
    width: 100%; padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px;
    font-size: 0.95em; font-family: inherit; box-sizing: border-box; background: #fff; }
  .field input:focus, .field select:focus, .field textarea:focus {
    outline: none; border-color: #0066cc; box-shadow: 0 0 0 3px rgba(0,102,204,0.1); }
  .field .hint { color: #999; font-size: 0.78em; margin-top: 3px; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .actions { display: flex; gap: 12px; justify-content: flex-end; margin-top: 24px;
             padding-top: 18px; border-top: 1px solid #eee; }
  .actions button { padding: 10px 22px; border: 0; border-radius: 6px; font-size: 0.95em;
                    font-weight: 600; cursor: pointer; }
  .btn-save { background: #28a745; color: #fff; }
  .btn-save:hover { background: #218838; }
  .btn-save:disabled { background: #aaa; cursor: not-allowed; }
  .btn-cancel { background: #f0f0f0; color: #333; }
  .premium-only { opacity: 0.55; }
  .premium-only::after { content: ' • premium'; font-size: 0.7em; color: #28a745;
                         font-weight: 700; text-transform: uppercase; }
  .status { padding: 12px 16px; border-radius: 6px; margin-top: 14px;
            font-size: 0.9em; display: none; }
  .status.ok { background: #d4edda; color: #155724; display: block; }
  .status.err { background: #f8d7da; color: #721c24; display: block; }
  .secret-note { color: #888; font-size: 0.78em; font-style: italic; }
</style>
</head><body>
<div class="wizard">
  <h1>KittyRank — Setup</h1>
  <div class="subtitle">
    Configure your APIs and site identity. You can re-open this wizard anytime
    from <strong>Settings</strong> in the main dashboard.
  </div>

  <form id="setup-form" onsubmit="return saveSetup(event)">

    <!-- Site Identity -->
    <div class="section">
      <h3>1. Site identity</h3>
      <div class="help">Basic info about the WordPress site you're optimizing.</div>
      <div class="field">
        <label>Site URL <span class="required">*</span></label>
        <input type="url" name="site_domain" placeholder="https://example.com" required>
        <div class="hint">Without trailing slash. Include https://.</div>
      </div>
      <div class="row">
        <div class="field">
          <label>Site name <span class="required">*</span></label>
          <input type="text" name="site_name" placeholder="MySite" required>
        </div>
        <div class="field">
          <label>Brand tokens</label>
          <input type="text" name="site_brand_tokens" placeholder="mysite brand">
          <div class="hint">Space-separated. Stripped from internal-link overlap calculations.</div>
        </div>
      </div>
      <div class="field">
        <label>Short description</label>
        <input type="text" name="site_description" placeholder="A blog about embedded systems">
        <div class="hint">Used in Claude prompts when generating fix proposals.</div>
      </div>
    </div>

    <!-- GSC -->
    <div class="section">
      <h3>2. Google Search Console</h3>
      <div class="help">
        Required for: search performance data and URL inspection.<br>
        <strong>How to get it:</strong>
        Go to <a href="https://console.cloud.google.com/iam-admin/serviceaccounts" target="_blank">Google Cloud Console → IAM → Service Accounts</a>,
        create a service account, generate a JSON key file, and enable the
        <em>Search Console API</em> for it.
        Then in <a href="https://search.google.com/search-console" target="_blank">GSC</a>,
        add the service account email as an Owner of your property.
      </div>
      <div class="field" style="border:1px solid #d0d7de;border-radius:8px;padding:12px 14px;background:#f6f8fa;">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
          <div>
            <strong style="font-size:0.92em;">Recommended: connect with Google</strong>
            <div class="hint" id="google-status" style="margin-top:2px;">Not connected.</div>
          </div>
          <button type="button" id="btn-google" onclick="googleConnect()" style="padding:8px 16px;background:#1a73e8;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:0.85em;font-weight:600;white-space:nowrap;">Connect Google</button>
        </div>
        <div class="field" id="google-prop-wrap" style="display:none;margin-top:10px;margin-bottom:0;">
          <label>Search Console property</label>
          <select name="google_property" id="google-prop" onchange="googleSetProperty()"></select>
        </div>
      </div>
      <div class="hint" style="margin:-6px 0 10px;color:#8a8d9c;">&mdash; or paste a service-account JSON path (fallback) &mdash;</div>
      <div class="field">
        <label>Path to service-account JSON file</label>
        <input type="text" name="gsc_credentials"
               placeholder="C:\\path\\to\\service-account.json">
        <div class="hint">Absolute path to the JSON key file you downloaded. Keep it outside your git repo.
                          <br><strong>Real GSC JSON has these top-level keys:</strong>
                          <code>type, project_id, private_key, client_email</code>.
                          If yours has <code>clientIP</code> or <code>rayName</code>, that's a Cloudflare event log, not a service account.</div>
      </div>
      <div class="row">
        <div class="field">
          <label>GSC lookback (days)</label>
          <input type="number" name="gsc_days_back" min="7" max="490" value="60">
          <div class="hint">How far back to fetch search data. GSC caps at 490 days; 60 is a safe default.</div>
        </div>
        <div class="field">
          <label>Min impressions per keyword</label>
          <input type="number" name="min_impressions" min="1" max="100" value="5">
          <div class="hint">Drops near-zero-volume queries from the analysis set.</div>
        </div>
      </div>
    </div>

    <!-- Bing -->
    <div class="section">
      <h3>3. Bing Webmaster Tools</h3>
      <div class="help">
        Required for: Bing search performance + crawl issues (often a bigger traffic source than GSC for technical content).<br>
        <strong>How to get it:</strong>
        <a href="https://www.bing.com/webmasters" target="_blank">Bing Webmaster Tools</a> →
        Settings (cog icon, top right) → API Access → copy your API key.
      </div>
      <div class="field">
        <label>Bing API key</label>
        <input type="text" name="bing_api_key" id="bing-key" placeholder="32-char hex string">
        <div class="hint secret-note">If a value already exists and you leave this blank, the existing key is preserved.</div>
      </div>
    </div>

    <!-- Anthropic -->
    <div class="section">
      <h3>4. Anthropic (Claude) — for AI fix proposals</h3>
      <div class="help">
        Required for: generating title + meta rewrites for underperforming pages.<br>
        <strong>How to get it:</strong>
        <a href="https://console.anthropic.com/settings/keys" target="_blank">console.anthropic.com → Settings → API Keys</a> →
        Create Key (starts with <code>sk-ant-</code>).
      </div>
      <div class="row">
        <div class="field" style="grid-column: 1 / -1;">
          <label>Anthropic API key</label>
          <input type="text" name="anthropic_api_key" id="anthropic-key"
                 placeholder="sk-ant-api03-...">
          <div class="hint secret-note">If already set, leave blank to keep existing.</div>
        </div>
        <div class="field">
          <label>Model</label>
          <select name="direct_model">
            <option value="claude-sonnet-4-6">Sonnet 4.6 (recommended)</option>
            <option value="claude-opus-4-7">Opus 4.7 (stronger, slower, pricier)</option>
            <option value="claude-haiku-4-5-20251001">Haiku 4.5 (cheapest, fastest)</option>
          </select>
        </div>
      </div>
    </div>

    <!-- GA4 (optional) -->
    <div class="section">
      <h3>5. Google Analytics 4 <span style="color:#999;font-weight:normal;">(optional)</span></h3>
      <div class="help">
        Optional. Enables bounce-rate + engagement signals in audits.<br>
        <strong>How to get it:</strong>
        GA4 Admin → Property Settings → copy your <em>Property ID</em>
        (a numeric string). Uses the same service-account JSON as GSC.
      </div>
      <div class="field">
        <label>GA4 Property ID</label>
        <input type="text" name="ga4_property_id" placeholder="123456789">
      </div>
    </div>

    <div class="status" id="status"></div>
    <div class="actions">
      <button type="button" class="btn-cancel" onclick="window.location='/'">Cancel</button>
      <button type="submit" class="btn-save" id="btn-save">Save configuration</button>
    </div>
  </form>
</div>

<script>
async function loadState() {
  try {
    const r = await fetch('/api/setup/state');
    const s = await r.json();
    const f = document.forms['setup-form'];
    if (s.site_domain) f.site_domain.value = s.site_domain;
    if (s.site_name) f.site_name.value = s.site_name;
    if (s.site_description) f.site_description.value = s.site_description;
    if (s.site_brand_tokens) f.site_brand_tokens.value = s.site_brand_tokens;
    if (s.gsc_credentials) f.gsc_credentials.value = s.gsc_credentials;
    if (s.gsc_days_back != null) f.gsc_days_back.value = s.gsc_days_back;
    if (s.min_impressions != null) f.min_impressions.value = s.min_impressions;
    if (s.bing_api_key_masked) f.bing_api_key.placeholder = s.bing_api_key_masked + '  (leave blank to keep)';
    if (s.anthropic_api_key_masked) f.anthropic_api_key.placeholder = s.anthropic_api_key_masked + '  (leave blank to keep)';
    if (s.direct_model) f.direct_model.value = s.direct_model;
    if (s.ga4_property_id) f.ga4_property_id.value = s.ga4_property_id;
  } catch (e) {
    console.error('Failed to load setup state', e);
  }
}

async function loadGoogleStatus() {
  try {
    var s = await (await fetch('/api/google/status')).json();
    var el = document.getElementById('google-status');
    var btn = document.getElementById('btn-google');
    if (s.connected) {
      el.innerHTML = 'Connected' + (s.email ? ' as <strong>' + s.email + '</strong>' : '') + ' \u2713';
      btn.textContent = 'Reconnect';
      if (s.property) { showGoogleProps([s.property], s.property); }
    } else if (s.configured === false) {
      el.innerHTML = 'Set GOOGLE_OAUTH_CLIENT_ID in config to enable one-click sign-in (see docs/GOOGLE-LOGIN.md). Service-account path below still works.';
      btn.disabled = true; btn.style.opacity = 0.5;
    } else {
      el.textContent = 'Not connected.';
    }
  } catch (e) {}
}

function showGoogleProps(list, selected) {
  var wrap = document.getElementById('google-prop-wrap');
  var sel = document.getElementById('google-prop');
  if (!list || !list.length) { wrap.style.display = 'none'; return; }
  sel.innerHTML = list.map(function(p){ return '<option' + (p===selected?' selected':'') + '>' + p + '</option>'; }).join('');
  wrap.style.display = 'block';
}

async function googleConnect() {
  var btn = document.getElementById('btn-google');
  var el = document.getElementById('google-status');
  btn.disabled = true; el.textContent = 'Opening browser \u2014 approve access in the window that opens...';
  try {
    var r = await (await fetch('/api/google/connect', {method:'POST'})).json();
    if (r.success) {
      el.innerHTML = 'Connected' + (r.email ? ' as <strong>' + r.email + '</strong>' : '') + ' \u2713';
      btn.textContent = 'Reconnect';
      showGoogleProps(r.properties || [], (r.properties||[])[0]);
      if ((r.properties||[]).length) googleSetProperty();
    } else {
      el.innerHTML = '<span style="color:#c0392b;">' + (r.error || 'Connection failed') + '</span>';
    }
  } catch (e) {
    el.innerHTML = '<span style="color:#c0392b;">' + e.message + '</span>';
  } finally { btn.disabled = false; }
}

async function googleSetProperty() {
  var p = document.getElementById('google-prop').value;
  try { await fetch('/api/google/set-property', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({property: p})}); } catch(e){}
}

async function saveSetup(ev) {
  ev.preventDefault();
  const btn = document.getElementById('btn-save');
  const status = document.getElementById('status');
  btn.disabled = true; btn.textContent = 'Saving…'; status.className = 'status';
  const f = document.forms['setup-form'];
  const data = {};
  for (const el of f.elements) {
    if (el.name && !el.disabled) data[el.name] = el.value;
  }
  try {
    const r = await fetch('/api/setup/save', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(data),
    });
    const result = await r.json();
    if (result.success) {
      status.className = 'status ok';
      status.innerHTML = '<strong>✓ Config saved.</strong> Reloading dashboard…';
      btn.textContent = 'Saved';
      // Server already hot-reloaded config; just bounce to the main dashboard
      setTimeout(() => { window.location = '/'; }, 800);
    } else {
      status.className = 'status err';
      status.textContent = 'Error: ' + (result.error || 'unknown');
      btn.disabled = false; btn.textContent = 'Save configuration';
    }
  } catch (e) {
    status.className = 'status err';
    status.textContent = 'Network error: ' + e.message;
    btn.disabled = false; btn.textContent = 'Save configuration';
  }
  return false;
}

loadState();
    loadGoogleStatus();
</script>
</body></html>
"""


# ─── HTML Template ──────────────────────────────────────────────────────────
HTML_TEMPLATE = string.Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KittyRank</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f0f2f5; color: #333; font-size: 16.5px; line-height: 1.4; }
  /* Bump font on the two heaviest-read panels so all em-based sizes inside scale up */
  .changes-panel, .reports-panel { font-size: 17px; }
  .changes-panel .card, .reports-panel .reports-section { font-size: 1em; }
  .reports-panel .reports-section h3 { font-size: 1.05em; }
  .header { background: #1a1a2e; color: #fff; padding: 16px 24px; }
  .header h1 { font-size: 1.3em; display: inline; }
  .header .target { background: #e94560; padding: 2px 12px; border-radius: 12px;
                     font-size: 0.8em; margin-left: 12px; }
  .header .site { color: #aaa; font-size: 0.85em; margin-top: 4px; }
  .tabs-bar { background: #16213e; display: flex; }
  .tabs-bar button { background: none; border: none; color: #aaa; padding: 12px 24px;
                      cursor: pointer; font-size: 0.9em; font-weight: 600;
                      border-bottom: 3px solid transparent; }
  .tabs-bar button.active { color: #fff; border-bottom-color: #e94560; }
  .tabs-bar button:hover { color: #fff; }
  .main { max-width: 1200px; margin: 0 auto; padding: 20px; }

  /* Stats */
  .stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .stat { background: #fff; padding: 10px 18px; border-radius: 8px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.08); flex: 1; min-width: 100px; }
  .stat .num { font-size: 1.6em; font-weight: 700; }
  .stat .label { color: #888; font-size: 0.8em; }

  /* Toolbar */
  .toolbar { display: flex; gap: 8px; margin-bottom: 16px; align-items: center; flex-wrap: wrap; }
  .toolbar .filters button { padding: 5px 12px; border: 1px solid #ddd; border-radius: 16px;
                              background: #fff; cursor: pointer; font-size: 0.8em; }
  .toolbar .filters button.active { background: #0066cc; color: #fff; border-color: #0066cc; }
  .toolbar .bulk-actions { margin-left: auto; display: flex; gap: 6px; }
  .toolbar .bulk-actions button { padding: 6px 14px; border: 1px solid #ddd; border-radius: 6px;
                                   background: #fff; cursor: pointer; font-size: 0.8em; }
  .toolbar .bulk-actions button:hover { background: #f0f0f0; }
  .toolbar .bulk-actions button.btn-approve-sel { background: #28a745; color: #fff; border-color: #28a745; }
  .toolbar .bulk-actions button.btn-approve-sel:hover { background: #218838; }

  /* Cards */
  .card { background: #fff; border-radius: 8px; padding: 14px 18px; margin-bottom: 10px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.08); border-left: 4px solid #ddd;
          transition: opacity 0.3s; display: flex; gap: 12px; }
  .card .checkbox { display: flex; align-items: flex-start; padding-top: 2px; }
  .card .checkbox input { width: 18px; height: 18px; cursor: pointer; accent-color: #0066cc; }
  .card .card-body { flex: 1; }
  .card.source-seo-pipeline { border-left-color: #0066cc; }
  .card.source-crawlycat { border-left-color: #fd7e14; }
  .card.status-approved { border-left-color: #28a745; opacity: 0.6; }
  .card.status-rejected { border-left-color: #dc3545; opacity: 0.4; }
  .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .slug { font-weight: 700; font-size: 0.95em; }
  .slug a { color: #333; text-decoration: none; }
  .slug a:hover { color: #0066cc; }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.75em; font-weight: 600; }
  .badge-seo { background: #e7f0ff; color: #0066cc; }
  .badge-crawl { background: #fff3e0; color: #e65100; }
  .badge-conflict { background: #fce4ec; color: #c62828; }
  .outcomes-box { background: #fff; border: 1px solid #e3e3e3; border-radius: 8px; margin-bottom: 12px; }
  .outcomes-head { padding: 10px 14px; cursor: pointer; display: flex; gap: 8px; align-items: center; }
  .outcomes-head .oc-toggle { margin-left: auto; color: #888; font-size: 0.8em; }
  #outcomes-list { padding: 0 14px 10px; }
  .outcome-row { padding: 6px 0; border-top: 1px solid #f0f0f0; font-size: 0.9em;
                 display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .oc { font-weight: 600; font-size: 0.78em; padding: 1px 8px; border-radius: 10px; }
  .oc.improved { background: #d4edda; color: #155724; }
  .oc.flat { background: #fff3cd; color: #856404; }
  .oc.regressed { background: #f8d7da; color: #721c24; }
  .oc-detail { color: #666; }
  .metrics-line { display: inline-flex; gap: 5px; flex-wrap: wrap; }
  .metric { font-size: 0.78em; padding: 1px 7px; border-radius: 10px; background: #f0f0f0; color: #555; }
  .metric.up { background: #d4edda; color: #155724; }
  .metric.down { background: #f8d7da; color: #721c24; }
  .metric.neutral { background: #eef0f2; color: #667; }
  .btn-revert { margin-left: auto; background: #dc3545; color: #fff; border: none;
                border-radius: 4px; padding: 3px 10px; cursor: pointer; font-size: 0.85em; }
  .link-worksheet { background: #f5f0fb; border-left: 3px solid #6f42c1; padding: 8px 12px;
                    margin: 8px 0; border-radius: 4px; font-size: 0.9em; }
  .link-worksheet ul { margin: 4px 0 0; padding-left: 18px; }
  .analyzed-at { font-size: 0.75em; color: #999; margin-left: 6px; }
  .engagement-warn { background: #fff3cd; color: #856404; border-left: 3px solid #ffc107;
                     padding: 6px 10px; margin: 6px 0; border-radius: 4px; font-size: 0.88em; }
  .keyword { color: #888; font-size: 0.8em; margin-bottom: 6px; }
  .kw-rankings { margin: 6px 0 10px; }
  .kw-table { width: 100%; font-size: 0.78em; border-collapse: collapse; }
  .kw-table th { text-align: left; padding: 3px 8px; background: #f5f5f5; color: #666; font-weight: 600; border-bottom: 1px solid #eee; }
  .kw-table td { padding: 3px 8px; border-bottom: 1px solid #f0f0f0; color: #555; }
  .kw-table tr:hover td { background: #f9f9f9; }
  .src-gsc { background: #e8f5e9; color: #2e7d32; font-size: 0.7em; font-weight: 700;
              padding: 1px 5px; border-radius: 3px; }
  .src-bing { background: #e3f2fd; color: #1565c0; font-size: 0.7em; font-weight: 700;
               padding: 1px 5px; border-radius: 3px; }
  .ga4-metrics { display: flex; gap: 12px; align-items: center; padding: 5px 8px;
                  background: #f8f8f8; border-top: 1px solid #eee; font-size: 0.78em; }
  .ga4-label { color: #999; font-weight: 600; }
  .ga4-sessions, .ga4-duration { color: #666; }
  .bounce-rate { font-weight: 700; padding: 2px 7px; border-radius: 10px; }
  .bounce-high { background: #fde8e8; color: #c62828; }
  .bounce-mid  { background: #fff3e0; color: #e65100; }
  .bounce-low  { background: #e8f5e9; color: #2e7d32; }
  .ctr-diagnosis { font-size: 0.82em; color: #c62828; background: #fff8f8;
                    border-left: 3px solid #c62828; padding: 5px 10px; margin: 6px 0;
                    border-radius: 0 4px 4px 0; }
  .content-gaps { font-size: 0.82em; background: #fffbf0; border-left: 3px solid #f59e0b;
                   padding: 6px 10px; margin: 6px 0; border-radius: 0 4px 4px 0; }
  .content-gaps ul { margin: 4px 0 0 16px; padding: 0; }
  .content-gaps li { margin-bottom: 2px; color: #555; }

  /* Content improvement modal */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5);
                    z-index: 1000; align-items: center; justify-content: center; }
  .modal-overlay.active { display: flex; }
  .modal-box { background: #fff; border-radius: 10px; width: 90vw; max-width: 1100px;
                max-height: 90vh; display: flex; flex-direction: column;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3); }
  .modal-header { padding: 16px 20px; border-bottom: 1px solid #eee; display: flex;
                   align-items: center; justify-content: space-between; }
  .modal-header h3 { margin: 0; font-size: 1em; }
  .modal-header .modal-meta { font-size: 0.82em; color: #888; }
  .modal-close { background: none; border: none; font-size: 1.4em; cursor: pointer;
                  color: #666; padding: 0 4px; }
  .modal-body { flex: 1; overflow: hidden; display: flex; gap: 0; }
  .modal-pane { flex: 1; overflow-y: auto; padding: 16px 20px; }
  .modal-pane + .modal-pane { border-left: 1px solid #eee; }
  .modal-pane h4 { font-size: 0.82em; color: #888; font-weight: 600; margin: 0 0 12px;
                    text-transform: uppercase; letter-spacing: 0.05em; }
  .modal-pane .content-preview { font-size: 0.88em; line-height: 1.7; color: #333; }
  .modal-pane .content-preview h2 { font-size: 1.1em; margin: 16px 0 6px; }
  .modal-pane .content-preview h3 { font-size: 1em; margin: 12px 0 4px; color: #444; }
  .modal-pane .content-preview p { margin-bottom: 10px; }
  .modal-pane .content-preview code { background: #f5f5f5; padding: 1px 5px;
                                       border-radius: 3px; font-size: 0.9em; }
  .modal-pane .content-preview pre { background: #f5f5f5; padding: 12px; border-radius: 6px;
                                      overflow-x: auto; font-size: 0.82em; }
  .modal-footer { padding: 14px 20px; border-top: 1px solid #eee; display: flex;
                   gap: 10px; align-items: center; justify-content: flex-end; }
  .modal-spinner { color: #888; font-size: 0.85em; }
  .modal-wc { font-size: 0.82em; color: #666; margin-right: auto; }
  .change { margin-bottom: 6px; padding: 6px 10px; background: #f8f9fa; border-radius: 4px; font-size: 0.85em; }
  .change-label { font-size: 0.75em; font-weight: 600; color: #888; text-transform: uppercase; }
  .old { color: #999; text-decoration: line-through; }
  .new { color: #1a7f37; }
  .char-count { color: #bbb; font-size: 0.8em; }
  .reason { color: #888; font-size: 0.8em; font-style: italic; margin-top: 4px; }
  .actions { display: flex; gap: 6px; margin-top: 8px; }
  .btn { padding: 6px 16px; border: none; border-radius: 5px; cursor: pointer;
         font-size: 0.8em; font-weight: 600; }
  .btn-approve { background: #28a745; color: #fff; }
  .btn-approve:hover { background: #218838; }
  /* Pro-locked button — visible but disabled, click reveals the upsell */
  .btn-pro-locked { background: #e2e8f0 !important; color: #64748b !important;
                    cursor: pointer !important; opacity: 0.85;
                    border: 1px dashed #94a3b8 !important; pointer-events: auto !important; }
  .btn-pro-locked:hover { background: #cbd5e1 !important; opacity: 1; color: #334155 !important; }
  .btn-pro-locked .lock { display: inline-block; margin-right: 2px; filter: grayscale(0.3); }
  .btn-pro-locked .pro-pill { background: #28a745; color: #fff; font-size: 0.62em;
                              padding: 2px 7px; border-radius: 10px; margin-left: 4px;
                              font-weight: 700; letter-spacing: 0.5px; vertical-align: middle; }
  /* Pro upsell modal */
  .pro-upsell-overlay { display: none; position: fixed; inset: 0; background: rgba(15,23,42,0.55);
                       z-index: 9999; align-items: center; justify-content: center; padding: 20px; }
  .pro-upsell-overlay.active { display: flex; }
  .pro-upsell-box { background: #fff; max-width: 520px; width: 100%; border-radius: 12px;
                   box-shadow: 0 25px 60px rgba(0,0,0,0.35); padding: 28px 32px; }
  .pro-upsell-box h2 { margin: 0 0 6px; color: #1a1a2e; font-size: 1.35em; }
  .pro-upsell-box h2 .pill { background: #28a745; color: #fff; font-size: 0.5em; font-weight: 700;
                              padding: 3px 10px; border-radius: 12px; vertical-align: middle;
                              margin-left: 8px; letter-spacing: 0.5px; }
  .pro-upsell-box p { color: #475569; line-height: 1.5; margin: 12px 0; }
  .pro-upsell-box ul { color: #334155; padding-left: 22px; margin: 12px 0; }
  .pro-upsell-box li { margin: 4px 0; }
  .pro-upsell-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }
  .pro-upsell-actions .btn { padding: 9px 18px; font-size: 0.9em; font-weight: 600; cursor: pointer;
                              border-radius: 6px; border: 0; }
  .pro-upsell-actions .btn-close { background: #e2e8f0; color: #475569; }
  .pro-upsell-actions .btn-upgrade { background: #28a745; color: #fff; }
  .btn-copy { background: #fff; color: #475569; border: 1px solid #cbd5e1; }
  .btn-copy:hover { background: #f1f5f9; border-color: #64748b; }
  .btn-copy.copied { background: #d1fae5; color: #047857; border-color: #34d399; }
  .btn-reject { background: #dc3545; color: #fff; }
  .btn-reject:hover { background: #c82333; }
  .btn-submit { background: #0066cc; color: #fff; }
  .btn-submit:hover { background: #0052a3; }
  .toolbar .bulk-actions button.btn-submit-bulk { background: #0066cc; color: #fff; border-color: #0066cc; }
  .toolbar .bulk-actions button.btn-submit-bulk:hover { background: #0052a3; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .status-tag { font-size: 0.8em; font-weight: 600; padding: 1px 8px; border-radius: 10px; }
  .status-tag.approved { background: #d4edda; color: #155724; }
  .status-tag.rejected { background: #f8d7da; color: #721c24; }
  .status-tag.pending { background: #fff3cd; color: #856404; }
  .conflict-box { background: #fff3e0; border: 1px solid #ffe0b2; border-radius: 4px;
                   padding: 8px; margin-top: 6px; font-size: 0.8em; }

  /* Logs panel */
  .changes-panel { display: none; }
  .changes-panel.active { display: block; }
  .logs-panel { display: none; }
  .logs-panel.active { display: block; }
  .log-entry { padding: 4px 12px; font-family: 'Consolas', 'Courier New', monospace;
               font-size: 0.82em; border-bottom: 1px solid #f0f0f0; display: flex; gap: 10px; }
  .log-entry .log-time { color: #999; min-width: 65px; }
  .log-entry.success .log-msg { color: #28a745; }
  .log-entry.error .log-msg { color: #dc3545; }
  .log-entry.warn .log-msg { color: #fd7e14; }
  .log-entry.info .log-msg { color: #333; }
  .log-badge { display: inline-block; min-width: 48px; text-align: center; padding: 0 6px;
               border-radius: 3px; font-size: 0.8em; font-weight: 600; }
  .log-badge.success { background: #d4edda; color: #155724; }
  .log-badge.error { background: #f8d7da; color: #721c24; }
  .log-badge.warn { background: #fff3cd; color: #856404; }
  .log-badge.info { background: #e7f0ff; color: #0066cc; }
  .logs-container { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
                     max-height: 600px; overflow-y: auto; }

  /* CrawlyCat panel */
  .crawl-panel { display: none; }
  .crawl-panel.active { display: block; }
  .upload-area { background: #fff; border: 2px dashed #ddd; border-radius: 8px; padding: 40px;
                  text-align: center; cursor: pointer; transition: border-color 0.2s; }
  .upload-area:hover { border-color: #0066cc; }
  .upload-area.dragover { border-color: #0066cc; background: #f0f7ff; }
  .upload-area input { display: none; }
  .upload-area .icon { font-size: 2em; margin-bottom: 8px; }
  .upload-area p { color: #666; font-size: 0.9em; }
  .upload-status { margin-top: 16px; padding: 12px 16px; border-radius: 6px; display: none; }
  .upload-status.processing { display: block; background: #fff3cd; color: #856404; }
  .upload-status.done { display: block; background: #d4edda; color: #155724; }
  .upload-status.error { display: block; background: #f8d7da; color: #721c24; }

  /* Toast */
  .toast { position: fixed; bottom: 20px; right: 20px; padding: 10px 20px;
           border-radius: 6px; color: #fff; font-weight: 600; z-index: 1000;
           opacity: 0; transition: opacity 0.3s; font-size: 0.85em; }
  .toast.success { background: #28a745; }
  .toast.error { background: #dc3545; }

  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid #fff;
             border-top: 2px solid transparent; border-radius: 50%;
             animation: spin 0.6s linear infinite; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .select-all-row { padding: 8px 18px; background: #f8f9fa; border-radius: 8px;
                     margin-bottom: 8px; display: flex; align-items: center; gap: 8px;
                     font-size: 0.85em; }
  .select-all-row input { width: 18px; height: 18px; cursor: pointer; accent-color: #0066cc; }
  .sel-count { color: #0066cc; font-weight: 600; }

  /* Pipeline tab */
  .pipeline-panel { display: none; }
  .pipeline-panel.active { display: block; }
  .pipeline-step { background: #fff; border-radius: 8px; padding: 18px 22px; margin-bottom: 14px;
                    box-shadow: 0 1px 3px rgba(0,0,0,0.08); border-left: 4px solid #ddd;
                    transition: opacity 0.3s; }
  .pipeline-step.locked { opacity: 0.45; pointer-events: none; }
  .step-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .step-number { background: #1a1a2e; color: #fff; width: 28px; height: 28px; border-radius: 50%;
                 display: flex; align-items: center; justify-content: center; font-weight: 700;
                 font-size: 0.85em; flex-shrink: 0; }
  .step-title { font-weight: 700; font-size: 1em; }
  .step-badge { background: #e7f0ff; color: #0066cc; padding: 1px 8px; border-radius: 10px;
                font-size: 0.75em; font-weight: 600; }
  .step-status { margin-left: auto; font-size: 0.8em; font-weight: 600; padding: 2px 10px;
                 border-radius: 10px; white-space: nowrap; }
  .status-not_started { background: #f0f0f0; color: #888; }
  .status-running { background: #fff3cd; color: #856404; }
  .status-done { background: #d4edda; color: #155724; }
  .status-error { background: #f8d7da; color: #721c24; }
  .step-desc { color: #666; font-size: 0.85em; margin-bottom: 10px; }
  .btn-pipeline { background: #0066cc; color: #fff; padding: 8px 20px; border: none;
                  border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 0.85em; }
  .btn-pipeline:hover { background: #0052a3; }
  .btn-pipeline:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-pipeline.running { background: #856404; }
  .step-output { margin-top: 10px; }
  .step-log { background: #1a1a2e; color: #b0b0b0; padding: 12px; border-radius: 6px;
              font-family: 'Consolas', 'Courier New', monospace; font-size: 0.78em;
              max-height: 200px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }
  .step-error-msg { background: #f8d7da; color: #721c24; padding: 10px; border-radius: 6px;
                    font-size: 0.85em; margin-top: 8px; }
  .pipeline-upload-area { background: #f8f9fa; border: 2px dashed #ddd; border-radius: 8px;
                           padding: 24px; text-align: center; cursor: pointer;
                           transition: border-color 0.2s; }
  .pipeline-upload-area:hover { border-color: #0066cc; }
  .pipeline-upload-area.dragover { border-color: #0066cc; background: #f0f7ff; }
  .pipeline-upload-area input { display: none; }
  .pipeline-upload-done { background: #d4edda; color: #155724; padding: 10px 16px;
                           border-radius: 6px; font-size: 0.85em; }
  .pipeline-intro { color: #666; font-size: 0.9em; margin-bottom: 18px; }

  /* Submit URLs tab */
  .submit-panel { display: none; }
  .submit-panel.active { display: block; }
  .submit-section { background: #fff; border-radius: 8px; padding: 18px 22px; margin-bottom: 14px;
                     box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  .submit-section h3 { font-size: 0.95em; margin-bottom: 10px; }
  .url-textarea { width: 100%; min-height: 250px; padding: 10px; border: 1px solid #ddd;
                   border-radius: 6px; font-family: 'Consolas', monospace; font-size: 0.85em;
                   resize: vertical; box-sizing: border-box; }
  .url-textarea:focus { outline: none; border-color: #0066cc; }
  .submit-actions { display: flex; gap: 10px; margin-top: 12px; align-items: center; }
  .submit-results { margin-top: 14px; }
  .submit-result { padding: 6px 12px; border-bottom: 1px solid #f0f0f0; font-size: 0.85em;
                    font-family: 'Consolas', monospace; display: flex; gap: 10px; }
  .submit-result .url { flex: 1; word-break: break-all; }
  .submit-result .google-ok { color: #28a745; }
  .submit-result .google-fail { color: #dc3545; }
  .submit-result .bing-ok { color: #28a745; }
  .submit-result .bing-fail { color: #dc3545; }
  .or-sep { color: #999; font-size: 0.85em; text-align: center; margin: 12px 0; }


  /* Reports + Audit tab */
  .reports-panel { display: none; }
  .reports-panel.active { display: block; }


  /* Reports sub-tabs */
  .rsub-nav { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px;
              border-bottom: 2px solid #e5e7eb; }
  .rsub-nav button { background: none; border: none; border-bottom: 2px solid transparent;
                     margin-bottom: -2px; padding: 8px 16px; cursor: pointer;
                     font-size: 0.9em; color: #64748b; font-weight: 600; border-radius: 6px 6px 0 0; }
  .rsub-nav button:hover { color: #1a1a2e; background: #f1f5f9; }
  .rsub-nav button.active { color: #0066cc; border-bottom-color: #0066cc; }
  .rsub { display: none; }
  .rsub.active { display: block; }
  /* Responsive */
  @media (max-width: 900px) {
    .tabs-bar { flex-wrap: wrap; }
    .tabs-bar button { padding: 10px 14px; font-size: 0.85em; }
    .reports-section, .card, .panel { padding: 12px 14px !important; }
    .rsub-nav { overflow-x: auto; flex-wrap: nowrap; }
    .link-worksheet, table { font-size: 0.82em; }
  }
  @media (max-width: 600px) {
    body { font-size: 0.95em; }
    .tabs-bar button { padding: 9px 11px; font-size: 0.8em; }
    .actions, .bulk-actions { flex-wrap: wrap; gap: 6px !important; }
    .actions button, .bulk-actions button { flex: 1 1 auto; }
    table { display: block; overflow-x: auto; white-space: nowrap; }
    .modal-box, .pro-upsell-box { width: 96% !important; max-width: 96% !important; }
    h1 { font-size: 1.15em; }
    h2 { font-size: 1em; }
  }


</style>
</head>
<body>

<div class="header">
  <h1><svg viewBox="0 0 128 122" width="26" height="26" style="vertical-align:middle;margin-right:10px;"><rect x="10" y="84" width="22" height="28" rx="5" fill="#f4b13e"/><rect x="40" y="58" width="22" height="54" rx="5" fill="#f4b13e"/><path fill-rule="evenodd" d="M70 108 V12 L89 27 L108 8 V108 Q108 112 104 112 H74 Q70 112 70 108 Z M76.8 46 a4.2 4.2 0 1 0 8.4 0 a4.2 4.2 0 1 0 -8.4 0 Z M92.8 46 a4.2 4.2 0 1 0 8.4 0 a4.2 4.2 0 1 0 -8.4 0 Z" fill="#f0f0f5"/></svg>KittyRank <span style="background:#64748b;color:#fff;font-size:0.55em;padding:3px 10px;border-radius:12px;vertical-align:middle;font-weight:600;letter-spacing:0.5px;">FREE</span></h1>
  <span class="target">$target</span>
  <div class="site">$site_name &mdash; $site_domain</div>
</div>

<div class="tabs-bar">
  <button class="$pipeline_tab_active" onclick="switchTab('pipeline', this)">Pipeline</button>
  <button class="$changes_tab_active" onclick="switchTab('changes', this)">Changes</button>
  <button onclick="switchTab('submit', this)">Submit URLs</button>
  <button onclick="switchTab('reports', this)">Reports + Audit</button>
  <button onclick="switchTab('logs', this)">Logs</button>
  <button onclick="switchTab('crawlcat', this)">CrawlyCat</button>
  <button onclick="window.location='/settings'" title="Re-open the setup wizard to update API keys + site config"
          style="margin-left:auto;">⚙ Settings</button>
</div>

<div class="main">

  <!-- PIPELINE TAB -->
  <div id="tab-pipeline" class="pipeline-panel $pipeline_panel_active">
    <p class="pipeline-intro">Run the full SEO pipeline from your browser. Each step unlocks the next.</p>

    <!-- Step 1: Fetch -->
    <div class="pipeline-step" id="step-fetch">
      <div class="step-header">
        <span class="step-number">1</span>
        <span class="step-title">Fetch Search Data</span>
        <span class="step-status status-$fetch_status" id="fetch-status">$fetch_status_label</span>
      </div>
      <p class="step-desc">Fetches keyword data from Google Search Console and Bing Webmaster Tools, then merges the results.</p>
      <button class="btn-pipeline" id="btn-fetch" onclick="runStep('fetch')">Fetch Data</button>
      <div class="step-output" id="fetch-output"></div>
    </div>

    <!-- Step 2: Upload CrawlyCat -->
    <div class="pipeline-step $upload_locked" id="step-upload">
      <div class="step-header">
        <span class="step-number">2</span>
        <span class="step-title">Upload CrawlyCat Report</span>
        <span class="step-badge">Optional</span>
        <span class="step-status status-$upload_status" id="upload-status">$upload_status_label</span>
      </div>
      <p class="step-desc">Upload a CrawlyCat HTML crawl report. Issues will be processed alongside keyword fixes in Step 3.</p>
      <div id="pipeline-upload-content">$upload_content</div>
    </div>

    <!-- Step 3: Analyze -->
    <div class="pipeline-step $analyze_locked" id="step-analyze">
      <div class="step-header">
        <span class="step-number">3</span>
        <span class="step-title">Analyze &amp; Generate Fixes</span>
        <span class="step-status status-$analyze_status" id="analyze-status">$analyze_status_label</span>
      </div>
      <p class="step-desc">Analyzes pages for SEO issues, generates title/meta/intro fixes with Claude AI, and processes CrawlyCat report if uploaded.</p>
      <button class="btn-pipeline" id="btn-analyze" onclick="runStep('analyze')" $analyze_disabled>Generate Fixes</button>
      <div class="step-output" id="analyze-output"></div>
    </div>

    <!-- Step 4: Review -->
    <div class="pipeline-step $review_locked" id="step-review">
      <div class="step-header">
        <span class="step-number">4</span>
        <span class="step-title">Review &amp; Apply Fixes</span>
        <span class="step-status status-$review_status" id="review-status">$review_status_label</span>
      </div>
      <p class="step-desc">Review each proposed change, approve to push to WordPress, or reject to skip.</p>
      <button class="btn-pipeline" id="btn-review" onclick="switchTab('changes', document.querySelectorAll('.tabs-bar button')[1])" $review_disabled>Go to Review</button>
    </div>
  </div>

  <!-- CHANGES TAB -->
  <div id="tab-changes" class="changes-panel $changes_panel_active">
    <div class="stats">
      <div class="stat"><div class="num" id="total-count">0</div><div class="label">Total</div></div>
      <div class="stat"><div class="num" id="pending-count" style="color:#856404">0</div><div class="label">Pending</div></div>
      <div class="stat"><div class="num" id="approved-count" style="color:#28a745">0</div><div class="label">Approved</div></div>
      <div class="stat"><div class="num" id="rejected-count" style="color:#dc3545">0</div><div class="label">Rejected</div></div>
    </div>

    <div class="toolbar">
      <div class="filters">
        <button class="active" onclick="filterCards('all', this)">All</button>
        <button onclick="filterCards('pending', this)">Pending</button>
        <button onclick="filterCards('approved', this)">Approved</button>
        <button onclick="filterCards('rejected', this)">Rejected</button>
        <button onclick="filterCards('seo-pipeline', this)">KittyRank</button>
        <button onclick="filterCards('crawlycat', this)">CrawlyCat</button>
        <button onclick="filterCards('latest', this)">Latest run</button>
      </div>
      <div class="bulk-actions">
        <span class="sel-count" id="sel-count">0 selected</span>
        <button onclick="rejectSelected()">Reject Selected</button>
        <button class="btn-pro-locked" disabled onclick="showProUpsell(event)"
                title="Pro: approve and apply ALL pending fixes in one batch, then submit applied URLs to Bing.">
          <span class="lock"> 🔒 </span> Accept All Pending <span class="pro-pill">PRO</span>
        </button>
        <button id="btn-submit-all" class="btn-submit-bulk" onclick="submitAllApproved()" title="Submit all approved URLs to Bing for re-indexing">Submit All Approved</button>
      </div>
    </div>

    <div class="select-all-row">
      <input type="checkbox" id="select-all" onchange="toggleSelectAll(this.checked)">
      <label for="select-all">Select all visible pending items</label>
    </div>

    <div id="outcomes-panel-pro" style="background:#fff;border:1px dashed #94a3b8;border-radius:8px;padding:12px 16px;margin-bottom:12px;cursor:pointer;color:#64748b;font-size:0.9em;" onclick="showProUpsell(event)"><strong>🔒 Past fix outcomes</strong> <span class="pro-pill">PRO</span> <span style="margin-left:6px;">Track which fixes improved vs regressed over time — KittyRank Pro learns from your history and surfaces what works.</span></div>

  
    <div id="cards"></div>
  </div>

  <!-- REPORTS + AUDIT TAB -->
  <div id="tab-reports" class="reports-panel">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
      <h2 style="font-size:1.1em;margin:0;">Reports + Technical Audit</h2>
      <button id="btn-run-audit" onclick="runTechnicalAudit()" style="padding:8px 16px;
              background:#0066cc;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:0.85em;">
        Run Technical Audit
      </button>
    </div>

    <div class="rsub-nav">
      <button class="active" onclick="switchReportSubTab('health', this)">Site Health</button>
      <button class="" onclick="switchReportSubTab('trends', this)">Trends</button>
      <button class="" onclick="switchReportSubTab('cannibal', this)">Cannibalization</button>
      <button class="" onclick="switchReportSubTab('backlinks', this)">Backlinks</button>
      <button class="" onclick="switchReportSubTab('files', this)">Report Files</button>
    </div>

    <div class="rsub active" id="rsub-health">
<!-- Audit results -->
    <div class="reports-section" style="background:#fff;border-radius:8px;padding:18px 22px;
         margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <h3 style="margin:0 0 10px 0;font-size:0.95em;">Latest audit results</h3>
      <div id="audit-results" style="font-size:0.85em;color:#555;">Loading…</div>
    </div>

    </div>

    <div class="rsub" id="rsub-trends">
<!-- Trend analysis -->
    <div class="reports-section" style="background:#fff;border-radius:8px;padding:18px 22px;
         margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <h3 style="margin:0;font-size:0.95em;">3-period trend analysis (first30 / mid30 / last30 days)</h3>
        <button id="btn-run-trends" onclick="runTrendAnalysis()" style="padding:6px 12px;
                background:#0066cc;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:0.8em;">
          Run Trend Analysis
        </button>
      </div>
      <div style="font-size:0.82em;color:#666;line-height:1.5;margin-bottom:8px;">
        Compares first-month vs last-month metrics to catch patterns a single-snapshot view misses:
        audience growth vs CTR-only improvement vs title degradation vs ranking loss. Uses GSC + Bing API,
        falls back to manual CSV uploads (latest Performance ZIP + SearchPerformanceOverview).
      </div>
      <div id="trend-results" style="font-size:0.85em;color:#555;">Loading…</div>
    </div>

<!-- Page buckets (universal classification) -->
    <div class="reports-section" style="background:#fff;border-radius:8px;padding:18px 22px;
         margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <h3 style="margin:0 0 8px 0;font-size:0.95em;">Page buckets — every analyzed page classified</h3>
      <div style="font-size:0.82em;color:#666;line-height:1.5;margin-bottom:10px;">
        💤 Sleeping giants need title rewrites · 🎯 Almost-there need depth + links ·
        🏆 Converters should be PROTECTED + used as link sources · ⚰️ Dead weight should be consolidated or deleted.
        Updates each time you run "Analyze &amp; Generate Fixes".
      </div>
      <div id="bucket-results" style="font-size:0.85em;color:#555;">Loading…</div>
    </div>

    </div>

    <div class="rsub" id="rsub-cannibal">
<!-- Keyword cannibalization -->
    <div class="reports-section" style="background:#fff;border-radius:8px;padding:18px 22px;
         margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <h3 style="margin:0;font-size:0.95em;">Keyword cannibalization</h3>
        <button id="btn-run-cannibal" onclick="runCannibalAudit()" style="padding:6px 12px;
                background:#0066cc;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:0.8em;">
          Run Cannibalization Audit
        </button>
      </div>
      <div style="font-size:0.82em;color:#666;line-height:1.5;margin-bottom:8px;">
        Finds queries where <em>two or more of your own pages</em> compete in search results,
        splitting clicks and authority — the classic reason striking-distance keywords stay stuck.
        Groups findings by page pair: each pair is one merge/differentiate decision.
        Uses GSC data already fetched — no extra API calls.
      </div>
      <div id="cannibal-results" style="font-size:0.85em;color:#555;">Loading…</div>
    </div>

    </div>

    <div class="rsub" id="rsub-backlinks">
<!-- Backlink profile -->
    <div class="reports-section" style="background:#fff;border-radius:8px;padding:18px 22px;
         margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <h3 style="margin:0;font-size:0.95em;">Backlink profile + outreach targets</h3>
        <button id="btn-run-backlinks" onclick="runBacklinkAudit()" style="padding:6px 12px;
                background:#0066cc;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:0.8em;">
          Run Backlink Audit
        </button>
      </div>
      <div style="font-size:0.82em;color:#666;line-height:1.5;margin-bottom:8px;">
        Reads ReferringDomains CSV (domain-level breakdown with authority classification) + SiteExplorer CSV
        (per-URL backlink count). Identifies <em>unprotected high-traffic pages</em> — your top outreach targets.
        Bing API typically returns empty for this data; CSV is the reliable source.
      </div>
      <div id="backlink-results" style="font-size:0.85em;color:#555;">Loading…</div>
    </div>

    </div>

    <div class="rsub" id="rsub-files">

      <div class="reports-section" style="background:#fff;border-radius:8px;padding:18px 22px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
          <div>
            <h3 style="margin:0 0 4px 0;font-size:0.95em;">Branded PDF report</h3>
            <div style="font-size:0.82em;color:#666;">A single client-ready PDF of every audit + the fix queue, KittyRank-branded.</div>
          </div>
          <button class="btn-pro-locked" disabled onclick="showProUpsell(event)" title="Pro: export a branded, client-ready PDF of the full analysis." style="white-space:nowrap;"><span class="lock"> \ud83d\udd12 </span> Download PDF <span class="pro-pill">PRO</span></button>
        </div>
      </div>

<!-- Instructions -->
    <div class="reports-section" style="background:#fff;border-radius:8px;padding:18px 22px;
         margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <h3 style="margin:0 0 10px 0;font-size:0.95em;">Reports to download</h3>
      <div style="font-size:0.85em;color:#555;line-height:1.6;">
        The technical audit uses live API data when available, but is much richer when you
        also upload these manual exports. Drop fresh files every 1-4 weeks.
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:14px;">
        <!-- GSC column -->
        <div style="border-left:3px solid #4285f4;padding:10px 14px;background:#f8fafe;border-radius:0 6px 6px 0;">
          <strong style="color:#1a73e8;font-size:0.9em;">Google Search Console</strong>
          <div style="font-size:0.82em;color:#555;margin-top:8px;line-height:1.6;">
            <strong>Performance report (zip):</strong><br>
            <span style="color:#666;">Performance → Search results → "Export" (top right) → Download CSV.
            This gives you queries, pages, countries, devices, search appearance.</span>
            <br><br>
            <strong>Coverage report (zip):</strong><br>
            <span style="color:#666;">Pages (formerly Coverage) → "Export" → Download CSV.
            This gives you indexed vs not-indexed counts + critical and non-critical issues.</span>
            <br><br>
            <em style="color:#888;">Note: GSC API doesn't expose Coverage in bulk — manual download is the only path.</em>
          </div>
        </div>

        <!-- Bing column -->
        <div style="border-left:3px solid #00897b;padding:10px 14px;background:#f1faf7;border-radius:0 6px 6px 0;">
          <strong style="color:#00695c;font-size:0.9em;">Bing Webmaster Tools</strong>
          <div style="font-size:0.82em;color:#555;margin-top:8px;line-height:1.6;">
            <strong>Site Explorer CSV:</strong><br>
            <span style="color:#666;">Reports & Data → Site Explorer → click any folder
            → "Export" → CSV. Gives per-URL HTTP code, impressions, clicks, backlinks, document size.
            <strong>Paginated: download all pages.</strong></span>
            <br><br>
            <strong>Search Performance Overview CSV:</strong><br>
            <span style="color:#666;">Reports & Data → Search Performance → "Export". Date trend.</span>
            <br><br>
            <strong>Referring Domains CSV:</strong><br>
            <span style="color:#666;">Reports & Data → Backlinks → Referring Domains → Export.</span>
            <br><br>
            <em style="color:#888;">Optional: AI Performance Overview CSV, Failing URLs CSV.</em>
          </div>
        </div>
      </div>
    </div>

<!-- Upload area -->
    <div class="reports-section" style="background:#fff;border-radius:8px;padding:18px 22px;
         margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <h3 style="margin:0 0 10px 0;font-size:0.95em;">Upload reports</h3>
      <div id="reports-dropzone" ondragover="event.preventDefault();this.style.background='#e3f2fd'"
           ondragleave="this.style.background='#fafafa'"
           ondrop="dropReports(event)"
           style="border:2px dashed #ccc;border-radius:8px;padding:30px;text-align:center;
                  background:#fafafa;cursor:pointer;transition:background 0.2s;"
           onclick="document.getElementById('reports-file-input').click()">
        <div style="font-size:1em;color:#555;">
          📂 Drag and drop CSV/ZIP files here, or click to select
        </div>
        <div style="font-size:0.8em;color:#888;margin-top:6px;">
          Saves to GA-reports/<span id="report-today-date">today</span>/<span id="report-site-key">site</span>/
        </div>
      </div>
      <input id="reports-file-input" type="file" multiple accept=".csv,.zip"
             style="display:none" onchange="uploadReports(this.files)">
      <div id="upload-status" style="margin-top:10px;font-size:0.82em;color:#555;"></div>
    </div>

<!-- Currently stored -->
    <div class="reports-section" style="background:#fff;border-radius:8px;padding:18px 22px;
         margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <h3 style="margin:0 0 10px 0;font-size:0.95em;">Currently stored reports</h3>
      <div id="reports-list" style="font-size:0.85em;color:#555;">Loading…</div>
    </div>

    </div>
</div>

  <!-- LOGS TAB -->
  <div id="tab-logs" class="logs-panel">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
      <h2 style="font-size:1.1em;">Live Logs</h2>
      <button onclick="refreshLogs()" style="padding:6px 14px; border:1px solid #ddd; border-radius:6px;
              background:#fff; cursor:pointer; font-size:0.8em;">Refresh</button>
    </div>
    <div class="logs-container" id="logs-container"></div>
  </div>

  <!-- CRAWLYCAT TAB -->
  <div id="tab-crawlcat" class="crawl-panel">
    <h2 style="font-size:1.1em; margin-bottom:16px;">CrawlyCat Report</h2>
    <div class="upload-area" id="upload-area" onclick="document.getElementById('file-input').click()"
         ondragover="event.preventDefault(); this.classList.add('dragover')"
         ondragleave="this.classList.remove('dragover')"
         ondrop="event.preventDefault(); this.classList.remove('dragover'); handleDrop(event)">
      <div class="icon">&#128196;</div>
      <p><strong>Click to browse</strong> or drag & drop a CrawlyCat HTML report</p>
      <p style="color:#999; font-size:0.8em; margin-top:8px;">Parses the report, generates meta description fixes with Claude, and adds them to the review queue</p>
      <input type="file" id="file-input" accept=".html" onchange="uploadReport(this.files[0])">
    </div>
    <div class="upload-status" id="upload-status"></div>
  </div>

  <!-- SUBMIT URLS TAB -->
  <div id="tab-submit" class="submit-panel">
    <div class="submit-section">
      <h3>Submit URLs for Re-indexing</h3>
      <p style="color:#666; font-size:0.85em; margin-bottom:14px;">Submit URLs to Bing Webmaster Tools for faster re-crawling. (Google has no general recrawl API &mdash; keep your sitemap fresh and use Search Console &rarr; Request Indexing for Google.) Enter one URL per line, or upload a CSV file.</p>
      <textarea class="url-textarea" id="submit-urls" placeholder="https://yoursite.com/my-post/&#10;https://yoursite.com/another-post/&#10;&#10;Or just enter slugs:&#10;my-post&#10;another-post"></textarea>
      <div class="or-sep">&mdash; or &mdash;</div>
      <div style="display:flex; gap:10px; align-items:center;">
        <input type="file" id="csv-file-input" accept=".csv,.txt" style="display:inline; width:auto; border:none; padding:0;"
               onchange="loadCSVFile(this.files[0])">
        <span style="color:#999; font-size:0.8em;">CSV/TXT file with one URL per line</span>
      </div>
      <div class="submit-actions">
        <button class="btn-pipeline" id="btn-submit-urls" onclick="submitUrls()">Submit to Bing</button>
        <button class="btn-pipeline" style="background:#28a745;" id="btn-submit-approved" onclick="submitApproved()">Submit All Approved</button>
        <span id="submit-progress" style="color:#888; font-size:0.85em;"></span>
      </div>
    </div>
    <div class="submit-section submit-results" id="submit-results" style="display:none;">
      <h3>Results</h3>
      <div id="submit-results-list"></div>
    </div>
  </div>

  <!-- WRITER TAB -->

  <!-- POST IDEAS TAB -->

</div>


<div id="toast" class="toast"></div>

<!-- Pro upsell modal — shown when free-tier user clicks a locked premium button -->
<div class="pro-upsell-overlay" id="pro-upsell" onclick="if(event.target===this)closeProUpsell()">
  <div class="pro-upsell-box">
    <h2>One-click apply <span class="pill">PRO</span></h2>
    <p>You're using <strong>KittyRank Free</strong>. Approve &amp; Apply pushes the new title and meta description to WordPress automatically — no copy-paste, no Yoast tab-switching.</p>
    <p style="font-weight:600;color:#334155;">KittyRank Pro adds:</p>
    <ul>
      <li><strong>One-click Approve &amp; Apply</strong> — push fix to WordPress via REST</li>
      <li><strong>Bulk Apply</strong> — approve all pending fixes in one batch</li>
      <li><strong>Revert</strong> — restore pre-fix title/meta from history</li>
      <li><strong>Auto-insert internal links</strong> from related posts</li>
      <li><strong>Claude content rewrite</strong> — full post improvements + apply</li>
      <li><strong>URL removal</strong> from Bing for dead 404s</li>
    </ul>
    <div class="pro-upsell-actions">
      <button class="btn btn-close" onclick="closeProUpsell()">Maybe later</button>
      <button class="btn btn-upgrade" onclick="window.open('https://github.com/KittyRank/kittyrank','_blank')">Get Pro</button>
    </div>
  </div>
</div>

<script>
let fixes = $fixes_json;
let outcomes = $outcomes_json;
const siteUrl = "$site_domain";
let selected = new Set();
let currentFilter = 'all';
let logPollTimer = null;

function switchTab(tab, btn) {
  document.querySelectorAll('.tabs-bar button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  var tabs = {pipeline:'pipeline-panel', changes:'changes-panel', logs:'logs-panel',
              crawlcat:'crawl-panel', submit:'submit-panel', reports:'reports-panel'};
  for (var t in tabs) {
    var el = document.getElementById('tab-' + t);
    if (el) el.className = tabs[t] + (t === tab ? ' active' : '');
  }
  if (tab === 'logs') refreshLogs();
  if (tab === 'pipeline') pollPipelineStatus();
  if (tab === 'reports') refreshReports();
}

function switchReportSubTab(name, btn) {
  document.querySelectorAll('.rsub-nav button').forEach(function(b){ b.classList.remove('active'); });
  document.querySelectorAll('.rsub').forEach(function(p){ p.classList.remove('active'); });
  if (btn) btn.classList.add('active');
  var panel = document.getElementById('rsub-' + name);
  if (panel) panel.classList.add('active');
  // Lazy-load each sub-tab's data on first open
  if (name === 'health') refreshAuditResults();
  else if (name === 'trends') { refreshTrendResults(); refreshBucketResults(); }
  else if (name === 'cannibal') refreshCannibalResults();
  else if (name === 'backlinks') refreshBacklinkResults();
}


function renderCards() {
  const container = document.getElementById('cards');
  let html = '';
  window._latestRun = Object.values(fixes).reduce(
    (m, f) => (f.run_id && f.run_id > m ? f.run_id : m), '');
  const latestRun = window._latestRun;
  const slugs = Object.keys(fixes).sort((a, b) => {
    const order = {pending: 0, approved: 1, rejected: 2};
    const diff = order[fixes[a].status] - order[fixes[b].status];
    return diff !== 0 ? diff : a.localeCompare(b);
  });

  for (const slug of slugs) {
    const fix = fixes[slug];
    const badgeClass = fix.source === 'seo-pipeline' ? 'badge-seo' : 'badge-crawl';
    const pageUrl = siteUrl + '/' + slug + '/';
    const isPending = fix.status === 'pending';
    const isLatest = fix.run_id && fix.run_id === latestRun;
    const isVisible = currentFilter === 'all' || currentFilter === fix.status
                   || currentFilter === fix.source || (currentFilter === 'latest' && isLatest);
    const isChecked = selected.has(slug);

    html += '<div class="card source-' + fix.source + ' status-' + fix.status + '" '
         + 'data-slug="' + slug + '" data-source="' + fix.source + '" data-status="' + fix.status + '" '
         + 'data-run="' + (fix.run_id || '') + '" '
         + 'style="display:' + (isVisible ? 'flex' : 'none') + '">';

    html += '<div class="checkbox"><input type="checkbox" ' + (isChecked ? 'checked' : '')
         + (isPending ? '' : ' disabled') + ' onchange="toggleSelect(\\'' + slug + '\\', this.checked)"></div>';

    html += '<div class="card-body">';
    html += '<div class="card-header"><div>';
    html += '<span class="slug"><a href="' + pageUrl + '" target="_blank">' + slug + '</a></span> ';
    html += '<span class="badge ' + badgeClass + '">' + fix.source_label + '</span>';
    if (fix.conflict) html += ' <span class="badge badge-conflict">CONFLICT</span>';
    if (fix.track || fix.bucket) {
      // Universal bucket → preferred display. Falls back to old track names for back-compat.
      var bucket = fix.bucket || (fix.track === 'ranking' ? 'almost_there' : 'sleeping_giant');
      var BUCKETS = {
        'sleeping_giant': {color: '#0066cc', icon: '💤', label: 'SLEEPING GIANT',
                           tooltip: 'Ranks well (pos 1-10) but CTR is low. Title/meta rewrite is the win.'},
        'almost_there':   {color: '#6f42c1', icon: '🎯', label: 'ALMOST THERE',
                           tooltip: 'Page 2 (pos 11-30). Add depth + internal links to push to page 1.'},
        'converter':      {color: '#28a745', icon: '🏆', label: 'CONVERTER',
                           tooltip: 'High CTR + clicks. Protect — do NOT rewrite. Use as internal link source.'},
        'dead_weight':    {color: '#6c757d', icon: '⚰️', label: 'DEAD WEIGHT',
                           tooltip: 'Low everything. Consider improving, consolidating, or deleting.'},
      };
      var b = BUCKETS[bucket] || BUCKETS['sleeping_giant'];
      html += ' <span class="badge" style="background:' + b.color + ';color:#fff" title="' + b.tooltip + '">'
           + b.icon + ' ' + b.label + '</span>';
    }
    if (fix.opportunity_score != null)
      html += ' <span class="badge" style="background:#eef0f7;color:#334" '
           + 'title="Opportunity score (impressions x gap/proximity)">&#9733; '
           + fix.opportunity_score + '</span>';
    if (isLatest)
      html += ' <span class="badge" style="background:#28a745;color:#fff" '
           + 'title="From the latest analysis run">NEW</span>';
    if (fix.analyzed_at)
      html += ' <span class="analyzed-at">analyzed ' + esc(fix.analyzed_at) + '</span>';
    html += '</div>';
    html += '<span class="status-tag ' + fix.status + '">' + fix.status.toUpperCase() + '</span></div>';

    if (fix.target_keyword) {
      var tierColors = {1: '#2e7d32', 2: '#f57f17', 3: '#1565c0', 4: '#b71c1c'};
      var tierColor = fix.keyword_tier ? (tierColors[fix.keyword_tier] || '#555') : '#555';
      var tierBadge = fix.keyword_tier_label
        ? ' <span style="font-size:11px;font-weight:normal;background:' + tierColor + ';color:#fff;padding:1px 6px;border-radius:3px;margin-left:6px">'
          + fix.keyword_tier_label + ' · pos ' + (fix.keyword_position || '') + '</span>'
        : '';
      html += '<div class="keyword">Target: <strong>' + esc(fix.target_keyword) + '</strong>' + tierBadge + '</div>';
    }

    if (fix.keywords && fix.keywords.length) {
      html += '<div class="kw-rankings"><table class="kw-table"><tr><th>Keyword</th><th>Src</th><th>Pos</th><th>Impr</th><th>Clicks</th><th>CTR</th></tr>';
      for (var ki = 0; ki < fix.keywords.length; ki++) {
        var kw = fix.keywords[ki];
        var srcBadge = kw.source === 'bing' ? '<span class="src-bing">B</span>' : '<span class="src-gsc">G</span>';
        html += '<tr><td>' + esc(kw.query) + '</td><td>' + srcBadge + '</td><td>' + kw.position + '</td><td>' + kw.impressions + '</td><td>' + kw.clicks + '</td><td>' + kw.ctr + '%</td></tr>';
      }
      html += '</table>';
      if (fix.bounce_rate !== undefined) {
        var br = fix.bounce_rate;
        var brClass = br >= 70 ? 'bounce-high' : br >= 50 ? 'bounce-mid' : 'bounce-low';
        var dur = fix.avg_session_duration || 0;
        var durStr = Math.floor(dur/60) + 'm ' + Math.floor(dur%60) + 's';
        html += '<div class="ga4-metrics">'
          + '<span class="ga4-label">GA4:</span>'
          + '<span class="bounce-rate ' + brClass + '">Bounce ' + br + '%</span>'
          + '<span class="ga4-sessions">' + fix.sessions + ' sessions</span>'
          + '<span class="ga4-duration">Avg ' + durStr + '</span>'
          + '</div>';
      }
      html += '</div>';
    }

    for (const [key, change] of Object.entries(fix.changes)) {
      const label = key === 'metadesc' ? 'Meta Description' : key === 'title' ? 'SEO Title' : 'Intro Sentence';
      html += '<div class="change"><div class="change-label">' + label + '</div>';
      if (change.old && change.old !== '(prepend to first paragraph)')
        html += '<div class="old">' + esc(change.old) + ' <span class="char-count">(' + change.old.length + 'c)</span></div>';
      html += '<div class="new">' + esc(change.new) + ' <span class="char-count">(' + change.new.length + 'c)</span></div></div>';
    }

    if (fix.conflict)
      html += '<div class="conflict-box"><strong>CrawlyCat also suggests:</strong> '
           + esc(fix.conflict.metadesc) + ' (' + fix.conflict.metadesc.length + 'c)</div>';

    if (fix.engagement_warning)
      html += '<div class="engagement-warn">&#9888; ' + esc(fix.engagement_warning) + '</div>';
    if (fix.ctr_diagnosis)
      html += '<div class="ctr-diagnosis"><strong>CTR diagnosis:</strong> ' + esc(fix.ctr_diagnosis) + '</div>';
    if (fix.reason)
      html += '<div class="reason">' + esc(fix.reason) + '</div>';
    if (fix.content_gaps && fix.content_gaps.length) {
      html += '<div class="content-gaps"><strong>Content gaps to add:</strong><ul>';
      for (var gi = 0; gi < fix.content_gaps.length; gi++) {
        html += '<li>' + esc(fix.content_gaps[gi]) + '</li>';
      }
      html += '</ul>';
      html += '<button class="btn btn-pro-locked" disabled onclick="showProUpsell(event)" '
            + 'title="Pro: Claude rewrites the full post incorporating these gaps + applies to WordPress in one click.">'
            + '<span class="lock"> 🔒 </span> Improve Content with Claude <span class="pro-pill">PRO</span></button>';
      html += '</div>';
    }

    if (fix.link_from && fix.link_from.length) {
      html += '<div class="link-worksheet">'
           + '<strong>Add internal links from these related posts</strong> '
           + '<span class="char-count" title="Topic overlap = number of shared keyword tokens between the source post and this target page (from title, slug, and tracked keywords). 3+ = closely related; 2 = loosely related.">(ranking lever)</span>'
           + '<div class="link-help" style="font-size:0.78em;color:#666;margin:4px 0 6px 0;">'
           + 'Tick only the posts that are <em>topically</em> a good fit. Overlap is keyword-token similarity, not editorial fit.'
           + '</div>'
           + '<div class="link-toggle-all" style="font-size:0.82em;margin-bottom:4px;">'
           +   '<label><input type="checkbox" class="link-src-all" checked onchange="toggleLinkSources(this, \\'' + slug + '\\')"> Select all</label>'
           + '</div>'
           + '<ul class="link-sources" data-slug="' + slug + '" style="list-style:none;padding-left:4px;">';
      for (var lfi = 0; lfi < fix.link_from.length; lfi++) {
        var lf = fix.link_from[lfi];
        var sharedList = (lf.shared_tokens && lf.shared_tokens.length)
              ? lf.shared_tokens.join(', ')
              : '(unknown — re-run pipeline to populate)';
        var tooltip = lf.overlap + ' shared topic token(s): ' + sharedList
              + '. Higher = more topically related. Brand words are excluded.';
        html += '<li style="margin:2px 0;"><label style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;">'
             + '<input type="checkbox" class="link-src-cb" data-source-slug="' + esc(lf.slug) + '" checked onchange="syncLinkSelectAll(\\'' + slug + '\\')"> '
             + '<a href="' + siteUrl + '/' + lf.slug + '/" target="_blank" onclick="event.stopPropagation()">'
             + esc(lf.title || lf.slug) + '</a> '
             + '<span class="char-count" title="' + esc(tooltip) + '">(overlap ' + lf.overlap + ')</span>'
             + ((lf.shared_tokens && lf.shared_tokens.length)
                  ? ' <span class="shared-tokens" style="font-size:0.75em;color:#888;">[' + esc(lf.shared_tokens.join(', ')) + ']</span>'
                  : '')
             + '</label></li>';
      }
      html += '</ul>';
      html += '<button class="btn btn-pro-locked" disabled onclick="showProUpsell(event)" '
            + 'title="Pro: insert these internal links into the source posts in one click — no manual HTML editing.">'
            + '<span class="lock"> 🔒 </span> Add selected links <span class="pro-pill">PRO</span></button>';
      html += '</div>';
    }

    const dis = isPending ? '' : 'disabled';
    const isApproved = fix.status === 'approved';
    var titleNew = (fix.changes && fix.changes.title && fix.changes.title.new) || '';
    var metaNew = (fix.changes && fix.changes.metadesc && fix.changes.metadesc.new) || '';
    html += '<div class="actions">'
         + (titleNew ? '<button class="btn btn-copy" onclick=\\'copyToClipboard(this,' + JSON.stringify(titleNew) + ')\\'>Copy Title</button>' : '')
         + (metaNew  ? '<button class="btn btn-copy" onclick=\\'copyToClipboard(this,' + JSON.stringify(metaNew)  + ')\\'>Copy Meta</button>'  : '')
         + '<button class="btn btn-approve" ' + dis + ' onclick="doAction(\\'' + slug + '\\',\\'mark_applied\\')" title="After you paste these into Yoast and save the post">Mark Applied</button>'
         + '<button class="btn btn-approve btn-pro-locked" disabled onclick="showProUpsell(event)" '
         +   'title="Pro feature: one-click push to WordPress — no copy-paste needed.">'
         +   '<span class="lock"> 🔒 </span> Approve &amp; Apply <span class="pro-pill">PRO</span>'
         + '</button>'
         + '<button class="btn btn-reject" ' + dis + ' onclick="doAction(\\'' + slug + '\\',\\'reject\\')">Reject</button>'
         + (isApproved
              ? '<button class="btn btn-submit" onclick="submitUrl(\\'' + slug + '\\')" title="Submit this URL to Bing for re-indexing">Submit URL</button>'
              : '')
         + '</div></div></div>';
  }
  container.innerHTML = html;
  updateCounts();
  updateSelCount();
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function showProUpsell(ev) {
  if (ev) ev.preventDefault();
  document.getElementById('pro-upsell').classList.add('active');
}
function closeProUpsell() {
  document.getElementById('pro-upsell').classList.remove('active');
}

function copyToClipboard(btn, txt) {
  try {
    navigator.clipboard.writeText(txt).then(function(){
      var orig = btn.textContent;
      btn.classList.add('copied');
      btn.textContent = 'Copied!';
      setTimeout(function(){ btn.classList.remove('copied'); btn.textContent = orig; }, 1200);
    });
  } catch(e) {
    // Fallback for old browsers
    var ta = document.createElement('textarea'); ta.value = txt; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
    btn.textContent = 'Copied!'; setTimeout(function(){ btn.textContent = btn.textContent.replace('Copied!', ''); }, 1200);
  }
}

function metricChip(label, m, kind) {
  if (!m) return '';
  var val, good;
  if (kind === 'pp') {                     // CTR: percentage points
    val = (m.delta >= 0 ? '+' : '') + m.delta + 'pp';
    good = m.delta >= 0;
  } else if (kind === 'pos') {             // position: lower is better
    var imp = Math.round((m.before - m.after) * 10) / 10;
    val = (imp >= 0 ? '+' : '') + imp;
    good = imp >= 0;
  } else {                                  // impressions / clicks: percent
    val = (m.pct == null) ? (m.after > m.before ? 'new' : '0%')
                          : ((m.pct >= 0 ? '+' : '') + m.pct + '%');
    good = m.delta >= 0;
  }
  var cls = (m.delta === 0) ? 'neutral' : (good ? 'up' : 'down');
  return '<span class="metric ' + cls + '" title="' + m.before + ' \\u2192 ' + m.after + '">'
       + label + ' ' + val + '</span>';
}

function _selectedLinkSources(slug) {
  const ul = document.querySelector('.link-sources[data-slug="' + slug + '"]');
  if (!ul) return null;  // no worksheet rendered — fall through to "all sources"
  const out = [];
  ul.querySelectorAll('.link-src-cb:checked').forEach(cb => out.push(cb.dataset.sourceSlug));
  return out;
}

function toggleLinkSources(masterCb, slug) {
  const ul = document.querySelector('.link-sources[data-slug="' + slug + '"]');
  if (!ul) return;
  ul.querySelectorAll('.link-src-cb').forEach(cb => { cb.checked = masterCb.checked; });
}

function syncLinkSelectAll(slug) {
  const ul = document.querySelector('.link-sources[data-slug="' + slug + '"]');
  if (!ul) return;
  const cbs = ul.querySelectorAll('.link-src-cb');
  const all = Array.from(cbs).every(cb => cb.checked);
  const master = document.querySelector('.link-toggle-all input[onchange*="\\'' + slug + '\\'"]');
  if (master) master.checked = all;
}



function updateCounts() {
  const all = Object.values(fixes);
  document.getElementById('total-count').textContent = all.length;
  document.getElementById('pending-count').textContent = all.filter(f => f.status === 'pending').length;
  document.getElementById('approved-count').textContent = all.filter(f => f.status === 'approved').length;
  document.getElementById('rejected-count').textContent = all.filter(f => f.status === 'rejected').length;
}

function updateSelCount() {
  const pending = [...selected].filter(s => fixes[s] && fixes[s].status === 'pending');
  selected = new Set(pending);
  document.getElementById('sel-count').textContent = selected.size + ' selected';
}

function toggleSelect(slug, checked) {
  if (checked) selected.add(slug); else selected.delete(slug);
  updateSelCount();
}

function toggleSelectAll(checked) {
  document.querySelectorAll('.card').forEach(card => {
    if (card.style.display === 'none') return;
    const slug = card.dataset.slug;
    if (fixes[slug].status !== 'pending') return;
    const cb = card.querySelector('.checkbox input');
    cb.checked = checked;
    if (checked) selected.add(slug); else selected.delete(slug);
  });
  updateSelCount();
}

function filterCards(filter, btn) {
  currentFilter = filter;
  document.querySelectorAll('.toolbar .filters button').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.querySelectorAll('.card').forEach(card => {
    const st = card.dataset.status, src = card.dataset.source;
    const matchLatest = filter === 'latest' && card.dataset.run
                        && card.dataset.run === window._latestRun;
    card.style.display = (filter === 'all' || filter === st || filter === src || matchLatest)
                       ? 'flex' : 'none';
  });
}

function showToast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast ' + type; t.style.opacity = 1;
  setTimeout(() => t.style.opacity = 0, 3000);
}

async function doAction(slug, act) {
  const card = document.querySelector('[data-slug="' + slug + '"]');
  const btns = card.querySelectorAll('.btn');
  btns.forEach(b => b.disabled = true);
  if (act === 'approve') btns[0].innerHTML = '<span class="spinner"></span>';

  try {
    const resp = await fetch('/api/action', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({slug, action: act})
    });
    const data = await resp.json();
    if (data.success) {
      fixes[slug].status = act === 'approve' ? 'approved' : 'rejected';
      selected.delete(slug);
      renderCards();
      let toast = slug + ': ' + (act === 'approve' ? 'Applied!' : 'Rejected');
      if (act === 'approve' && data.submit) {
        toast += ' · Submit → ' + data.submit.message;
      }
      showToast(toast, 'success');
    } else {
      showToast(slug + ': ' + data.error, 'error');
      btns.forEach(b => b.disabled = false);
      btns[0].textContent = 'Mark Applied';
    }
  } catch(e) {
    showToast('Error: ' + e.message, 'error');
    btns.forEach(b => b.disabled = false);
    btns[0].textContent = 'Mark Applied';
  }
}

// Submit a single URL on demand (after approve, or as a manual re-submit)
async function submitUrl(slug) {
  const card = document.querySelector('[data-slug="' + slug + '"]');
  const btn = card ? card.querySelector('.btn-submit') : null;
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting'; }
  try {
    const resp = await fetch('/api/action', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({slug, action: 'submit'})
    });
    const data = await resp.json();
    if (data.success) {
      showToast(slug + ': ' + (data.message || 'Submitted'), 'success');
    } else {
      showToast(slug + ': ' + (data.error || 'submit failed'), 'error');
    }
  } catch(e) {
    showToast('Submit error: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Submit URL'; }
  }
}

// Bulk-submit every URL currently marked as approved
async function submitAllApproved() {
  const approvedCount = Object.values(fixes).filter(f => f.status === 'approved').length;
  if (approvedCount === 0) { showToast('No approved URLs to submit', 'error'); return; }
  if (!confirm('Submit ' + approvedCount + ' approved URLs to Bing for re-indexing?')) return;
  const btn = document.getElementById('btn-submit-all');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting ' + approvedCount + ' URLs'; }
  try {
    const resp = await fetch('/api/action', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({slug: '__bulk__', action: 'submit_all_approved'})
    });
    const data = await resp.json();
    if (data.success) {
      showToast(data.message, 'success');
    } else {
      showToast(data.error || 'bulk submit failed', 'error');
    }
  } catch(e) {
    showToast('Bulk submit error: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Submit All Approved'; }
  }
}


// Apply ALL pending fixes in one batched call; submit applied URLs in ONE Bing batch

async function rejectSelected() {
  const slugs = [...selected].filter(s => fixes[s].status === 'pending');
  if (!slugs.length) { showToast('No pending items selected', 'error'); return; }
  if (!confirm('Reject ' + slugs.length + ' selected changes?')) return;
  for (const slug of slugs) {
    await doAction(slug, 'reject');
  }
}

// Logs
async function refreshLogs() {
  try {
    const resp = await fetch('/api/logs');
    const logs = await resp.json();
    const container = document.getElementById('logs-container');
    container.innerHTML = logs.map(l =>
      '<div class="log-entry ' + l.level + '">'
      + '<span class="log-time">' + l.time + '</span>'
      + '<span class="log-badge ' + l.level + '">' + l.level.toUpperCase() + '</span>'
      + '<span class="log-msg">' + esc(l.message) + '</span></div>'
    ).reverse().join('');
  } catch(e) {}
}

// CrawlyCat upload
function handleDrop(e) {
  const file = e.dataTransfer.files[0];
  if (file && file.name.endsWith('.html')) uploadReport(file);
}

async function uploadReport(file) {
  if (!file) return;
  const status = document.getElementById('upload-status');
  status.className = 'upload-status processing';
  status.textContent = 'Processing ' + file.name + '... (scraping pages + Claude analysis, this may take a few minutes)';

  try {
    const formData = new FormData();
    formData.append('report', file);
    const resp = await fetch('/api/crawlcat', { method: 'POST', body: formData });
    const data = await resp.json();
    if (data.success) {
      status.className = 'upload-status done';
      status.textContent = 'Done! ' + data.count + ' fixes generated. Switch to Changes tab to review.';
      // Reload fixes
      const fixResp = await fetch('/api/fixes');
      fixes = await fixResp.json();
      renderCards();
    } else {
      status.className = 'upload-status error';
      status.textContent = 'Error: ' + data.error;
    }
  } catch(e) {
    status.className = 'upload-status error';
    status.textContent = 'Error: ' + e.message;
  }
}

// --- Pipeline ---
var pipelinePollTimer = null;

async function runStep(step) {
  var btn = document.getElementById('btn-' + step);
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Running...';
  btn.className = 'btn-pipeline running';
  try {
    var resp = await fetch('/api/pipeline/' + step, { method: 'POST' });
    var data = await resp.json();
    if (data.started) {
      startPipelinePolling();
    } else {
      showToast(data.error || 'Failed to start', 'error');
      btn.disabled = false;
      btn.textContent = step === 'fetch' ? 'Fetch Data' : 'Generate Fixes';
      btn.className = 'btn-pipeline';
    }
  } catch(e) {
    showToast('Error: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = step === 'fetch' ? 'Fetch Data' : 'Generate Fixes';
    btn.className = 'btn-pipeline';
  }
}

function startPipelinePolling() {
  if (pipelinePollTimer) return;
  pipelinePollTimer = setInterval(pollPipelineStatus, 2000);
  pollPipelineStatus();
}

async function pollPipelineStatus() {
  try {
    var resp = await fetch('/api/pipeline/status');
    var ps = await resp.json();
    updatePipelineUI(ps);
    var anyRunning = (ps.fetch.status === 'running' || ps.analyze.status === 'running');
    if (!anyRunning && pipelinePollTimer) {
      clearInterval(pipelinePollTimer);
      pipelinePollTimer = null;
    }
  } catch(e) {}
}

var _fixesReloaded = false;

function updatePipelineUI(ps) {
  // Update status badges
  var steps = ['fetch', 'upload', 'analyze'];
  for (var i = 0; i < steps.length; i++) {
    var s = steps[i];
    var st = ps[s];
    var statusEl = document.getElementById(s + '-status');
    if (statusEl) {
      var label = st.status.replace('_', ' ').toUpperCase();
      if (s === 'upload' && st.status === 'done' && st.filename) label = st.filename;
      statusEl.textContent = label;
      statusEl.className = 'step-status status-' + st.status;
    }
    var outputEl = document.getElementById(s + '-output');
    if (outputEl) {
      if (st.status === 'running') {
        outputEl.innerHTML = '<pre class="step-log">' + esc(st.message || 'Working...') + '</pre>';
        // Auto-scroll to bottom
        var pre = outputEl.querySelector('.step-log');
        if (pre) pre.scrollTop = pre.scrollHeight;
      } else if (st.status === 'error') {
        outputEl.innerHTML = '<pre class="step-log">' + esc(st.message || '') + '</pre>'
          + '<div class="step-error-msg">' + esc(st.error) + '</div>';
      } else if (st.status === 'done' && st.message && st.message !== 'Previous output found') {
        outputEl.innerHTML = '<pre class="step-log">' + esc(st.message) + '</pre>';
      }
    }
  }

  // Unlock steps based on fetch status
  var fetchDone = ps.fetch.status === 'done';
  document.getElementById('step-upload').classList.toggle('locked', !fetchDone);
  document.getElementById('step-analyze').classList.toggle('locked', !fetchDone);

  // Fetch button state
  var btnFetch = document.getElementById('btn-fetch');
  if (ps.fetch.status === 'running') {
    btnFetch.disabled = true;
    btnFetch.innerHTML = '<span class="spinner"></span> Fetching...';
    btnFetch.className = 'btn-pipeline running';
  } else if (ps.fetch.status === 'done') {
    btnFetch.disabled = false;
    btnFetch.textContent = 'Re-fetch Data';
    btnFetch.className = 'btn-pipeline';
  } else if (ps.fetch.status === 'error') {
    btnFetch.disabled = false;
    btnFetch.textContent = 'Retry Fetch';
    btnFetch.className = 'btn-pipeline';
  }

  // Analyze button state
  var btnAnalyze = document.getElementById('btn-analyze');
  if (ps.analyze.status === 'running') {
    btnAnalyze.disabled = true;
    btnAnalyze.innerHTML = '<span class="spinner"></span> Analyzing...';
    btnAnalyze.className = 'btn-pipeline running';
  } else if (ps.analyze.status === 'done') {
    btnAnalyze.disabled = false;
    btnAnalyze.textContent = 'Re-generate Fixes';
    btnAnalyze.className = 'btn-pipeline';
  } else if (ps.analyze.status === 'error') {
    btnAnalyze.disabled = false;
    btnAnalyze.textContent = 'Retry Analysis';
    btnAnalyze.className = 'btn-pipeline';
  } else {
    btnAnalyze.disabled = !fetchDone;
  }

  // Review step
  var analyzeDone = ps.analyze.status === 'done';
  document.getElementById('step-review').classList.toggle('locked', !analyzeDone);
  var btnReview = document.getElementById('btn-review');
  btnReview.disabled = !analyzeDone;
  var revStatus = document.getElementById('review-status');
  if (analyzeDone) {
    var total = Object.keys(fixes).length;
    if (total > 0) {
      revStatus.textContent = total + ' CHANGES';
      revStatus.className = 'step-status status-done';
    } else {
      revStatus.textContent = 'READY';
      revStatus.className = 'step-status status-done';
    }
  }

  // When analyze completes, reload fixes for the review tab
  if (analyzeDone && !_fixesReloaded) {
    _fixesReloaded = true;
    reloadFixes();
  }
  if (!analyzeDone) _fixesReloaded = false;
}

async function reloadFixes() {
  try {
    var resp = await fetch('/api/fixes');
    fixes = await resp.json();
    renderCards();
      } catch(e) {}
}

// Pipeline file upload
function handlePipelineDrop(e) {
  e.preventDefault();
  document.getElementById('pipeline-drop-area').classList.remove('dragover');
  var file = e.dataTransfer.files[0];
  if (file && file.name.endsWith('.html')) uploadPipelineReport(file);
}

async function uploadPipelineReport(file) {
  if (!file) return;
  var contentEl = document.getElementById('pipeline-upload-content');
  contentEl.innerHTML = '<div class="pipeline-upload-done">Uploading ' + esc(file.name) + '...</div>';
  try {
    var formData = new FormData();
    formData.append('report', file);
    var resp = await fetch('/api/pipeline/upload-report', { method: 'POST', body: formData });
    var data = await resp.json();
    if (data.success) {
      contentEl.innerHTML = '<div class="pipeline-upload-done">Uploaded: <strong>' + esc(data.filename) + '</strong> &mdash; will be processed in Step 3</div>';
      document.getElementById('upload-status').textContent = data.filename;
      document.getElementById('upload-status').className = 'step-status status-done';
    } else {
      contentEl.innerHTML = '<div class="step-error-msg">' + esc(data.error) + '</div>';
    }
  } catch(e) {
    contentEl.innerHTML = '<div class="step-error-msg">Upload failed: ' + esc(e.message) + '</div>';
  }
}

// --- Submit URLs ---
function loadCSVFile(file) {
  if (!file) return;
  var reader = new FileReader();
  reader.onload = function(e) {
    var text = e.target.result;
    // Parse CSV: take first column, skip header if it looks like one
    var lines = text.split(/\\r?\\n/).map(function(l) { return l.split(',')[0].trim(); })
                    .filter(function(l) { return l && l !== 'url' && l !== 'URL'; });
    document.getElementById('submit-urls').value = lines.join('\\n');
  };
  reader.readAsText(file);
}

async function submitUrls() {
  var raw = document.getElementById('submit-urls').value.trim();
  if (!raw) { showToast('Enter at least one URL', 'error'); return; }
  var urls = raw.split(/\\r?\\n/).map(function(l) { return l.trim(); }).filter(function(l) { return l; });
  await doSubmitUrls(urls);
}

async function submitApproved() {
  var approved = [];
  for (var slug in fixes) {
    if (fixes[slug].status === 'approved') approved.push(slug);
  }
  if (!approved.length) { showToast('No approved fixes to submit', 'error'); return; }
  if (!confirm('Submit ' + approved.length + ' approved URLs for re-indexing?')) return;
  await doSubmitUrls(approved);
}

async function doSubmitUrls(urls) {
  var btn = document.getElementById('btn-submit-urls');
  var btn2 = document.getElementById('btn-submit-approved');
  var progress = document.getElementById('submit-progress');
  btn.disabled = true; btn2.disabled = true;
  var resultsDiv = document.getElementById('submit-results');
  var listDiv = document.getElementById('submit-results-list');
  resultsDiv.style.display = 'block';
  listDiv.innerHTML = '';

  for (var i = 0; i < urls.length; i++) {
    progress.textContent = 'Submitting ' + (i+1) + '/' + urls.length + '...';
    try {
      var resp = await fetch('/api/submit-url', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({url: urls[i]})
      });
      var data = await resp.json();
      var gClass = data.google_ok ? 'google-ok' : 'google-fail';
      var bClass = data.bing_ok ? 'bing-ok' : 'bing-fail';
      listDiv.innerHTML += '<div class="submit-result">'
        + '<span class="url">' + esc(data.url) + '</span>'
        + '<span class="' + gClass + '">G: ' + esc(data.google_msg) + '</span>'
        + '<span class="' + bClass + '">B: ' + esc(data.bing_msg) + '</span>'
        + '</div>';
    } catch(e) {
      listDiv.innerHTML += '<div class="submit-result"><span class="url">' + esc(urls[i])
        + '</span><span class="google-fail">Error: ' + esc(e.message) + '</span></div>';
    }
  }
  progress.textContent = 'Done - ' + urls.length + ' URLs submitted.';
  btn.disabled = false; btn2.disabled = false;
}

// --- Content Writer ---

// --- Content Improvement ---
var _improveSlug = '';




// Close modal on overlay click (improve-modal is Pro-only; guard for Free)
var _improveModal = document.getElementById('improve-modal');
if (_improveModal) _improveModal.addEventListener('click', function(e) {
  if (e.target === this) closeImproveModal();
});

renderCards();
// Start polling if any step might be running
pollPipelineStatus();


</script>
<script>
// ─── Reports + Audit tab ────────────────────────────────────────────────────
var _auditPoll = null;

async function refreshReports() {
  // Set today/site labels for the dropzone hint
  var d = new Date(); var pad = n => String(n).padStart(2, '0');
  document.getElementById('report-today-date').textContent =
    d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  try {
    var r = await fetch('/api/reports/list');
    var data = await r.json();
    document.getElementById('report-site-key').textContent = data.site_key || 'site';
    renderReportsList(data);
  } catch (e) {
    document.getElementById('reports-list').textContent = 'Failed to load: ' + e.message;
  }
  refreshAuditResults();
  refreshTrendResults();
  refreshBacklinkResults();
  refreshCannibalResults();
  refreshBucketResults();
}

// ─── Page bucket display ────────────────────────────────────────────────────
// Use DOUBLE quotes for descriptions so apostrophes inside (don't, doesn't) don't break parsing.
var BUCKET_DEFS = {
  'sleeping_giant': {color: '#0066cc', icon: '💤', label: 'Sleeping giants',
                     desc: "Rank pos 1-10, impressions ≥500, CTR <2%. Title/meta rewrite is the win — these are queued in the Changes tab."},
  'almost_there':   {color: '#6f42c1', icon: '🎯', label: 'Almost there',
                     desc: "Page 2 (pos 11-20) with ≥100 impressions. Push to page 1 via depth + internal links."},
  'converter':      {color: '#28a745', icon: '🏆', label: 'Converters',
                     desc: "CTR ≥5% with ≥10 clicks. PROTECT — do NOT rewrite. Use these as internal link sources to boost other pages."},
  'dead_weight':    {color: '#6c757d', icon: '⚰️', label: 'Dead weight',
                     desc: "Low everything (impr <100, zero clicks). Consolidate, redirect, or delete."},
  'unclassified':   {color: '#adb5bd', icon: '•',  label: 'Unclassified',
                     desc: "Middle-ground pages that don't fit any pattern."},
  'no_data':        {color: '#dee2e6', icon: '○',  label: 'No data',
                     desc: "Pages with no GSC/Bing keyword data in the current run."},
};

async function refreshBucketResults() {
  try {
    var r = await fetch('/api/buckets/results');
    var data = await r.json();
    renderBucketResults(data);
  } catch(e) {
    document.getElementById('bucket-results').innerHTML =
      '<div style="color:#999;font-style:italic;">Failed to load: ' + esc(e.message) + '</div>';
  }
}

function renderBucketResults(data) {
  var el = document.getElementById('bucket-results');
  if (!data || data.message) {
    el.innerHTML = '<div style="color:#999;font-style:italic;">' + esc(data && data.message || 'No data') + '</div>';
    return;
  }
  var summary = data.summary || {};
  var buckets = data.buckets || {};
  var totalPages = Object.values(summary).reduce(function(a,b){return a+b;}, 0);

  // Summary chips
  var html = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;">';
  for (var key of ['converter', 'sleeping_giant', 'almost_there', 'dead_weight', 'unclassified', 'no_data']) {
    var n = summary[key] || 0;
    if (!n && key !== 'converter' && key !== 'dead_weight') continue;
    var b = BUCKET_DEFS[key];
    var pct = totalPages ? Math.round(n / totalPages * 100) : 0;
    html += '<div style="background:#fafafa;border:1px solid #e0e0e0;border-left:4px solid ' + b.color
         + ';padding:8px 14px;border-radius:0 6px 6px 0;min-width:140px;">'
         + '<div style="font-weight:600;color:' + b.color + ';">' + b.icon + ' ' + b.label + '</div>'
         + '<div style="font-size:1.3em;font-weight:700;">' + n + '</div>'
         + '<div style="color:#888;font-size:0.78em;">' + pct + '% of pages</div>'
         + '</div>';
  }
  html += '</div>';

  // Per-bucket collapsible lists — focus on the actionable ones
  for (var key of ['converter', 'sleeping_giant', 'almost_there', 'dead_weight']) {
    var pages = buckets[key] || [];
    if (!pages.length) continue;
    var b = BUCKET_DEFS[key];
    var openAttr = (key === 'converter' || key === 'sleeping_giant') ? ' open' : '';
    html += '<details' + openAttr + ' style="margin-bottom:10px;border:1px solid #e0e0e0;border-radius:6px;border-left:4px solid '
         + b.color + ';background:#fff;">'
         + '<summary style="cursor:pointer;padding:10px 14px;font-weight:600;display:flex;justify-content:space-between;list-style:none;">'
         +   '<span>' + b.icon + ' ' + b.label + '</span>'
         +   '<span style="background:' + b.color + ';color:#fff;padding:2px 10px;border-radius:12px;font-size:0.8em;">'
         +     pages.length + '</span>'
         + '</summary>'
         + '<div style="padding:8px 14px;font-size:0.82em;color:#555;background:#fafafa;border-bottom:1px solid #f0f0f0;">'
         + esc(b.desc)
         + '</div>'
         + '<div style="padding:8px 14px;font-size:0.85em;">';
    for (var p of pages.slice(0, 20)) {
      var siteUrlClean = (typeof siteUrl !== 'undefined') ? siteUrl : '';
      var pageUrl = siteUrlClean + '/' + p.slug + '/';
      html += '<div style="padding:4px 0;border-bottom:1px solid #f5f5f5;display:flex;justify-content:space-between;gap:10px;">'
           + '<a href="' + esc(pageUrl) + '" target="_blank" style="word-break:break-all;">' + esc(p.slug) + '</a>'
           + '<span style="color:#666;white-space:nowrap;">impr=' + p.impressions
           + ' clicks=' + p.clicks + ' ctr=' + p.ctr + '% pos=' + (p.position || '-') + '</span>'
           + '</div>';
    }
    if (pages.length > 20) html += '<div style="color:#888;font-style:italic;margin-top:4px;">... + ' + (pages.length - 20) + ' more</div>';
    html += '</div></details>';
  }

  el.innerHTML = html;
}

// ─── Cannibalization audit ─────────────────────────────────────────────────
async function runCannibalAudit() {
  var btn = document.getElementById('btn-run-cannibal');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Running...'; }
  try {
    var r = await fetch('/api/cannibal/run', {method: 'POST'});
    var data = await r.json();
    if (!data.success) {
      showToast('Cannibalization audit failed: ' + (data.error || '?'), 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Run Cannibalization Audit'; }
      return;
    }
    var poll = setInterval(async function() {
      try {
        var s = await fetch('/api/cannibal/status'); var sd = await s.json();
        if (sd.status === 'done' || sd.status === 'error') {
          clearInterval(poll);
          if (btn) { btn.disabled = false; btn.textContent = 'Run Cannibalization Audit'; }
          refreshCannibalResults();
          if (sd.status === 'error') showToast('Cannibalization audit failed: ' + (sd.error || '?'), 'error');
          else showToast('Cannibalization audit done', 'success');
        }
      } catch(e) {}
    }, 2000);
  } catch (e) {
    showToast('Cannibalization audit call failed: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Run Cannibalization Audit'; }
  }
}

async function refreshCannibalResults() {
  try {
    var r = await fetch('/api/cannibal/results');
    var data = await r.json();
    renderCannibalResults(data);
  } catch(e) {
    document.getElementById('cannibal-results').innerHTML =
      '<div style="color:#999;font-style:italic;">Failed to load: ' + esc(e.message) + '</div>';
  }
}

function renderCannibalResults(data) {
  var el = document.getElementById('cannibal-results');
  if (!data || data.message) {
    el.innerHTML = '<div style="color:#999;font-style:italic;">' + esc(data && data.message || 'No data') + '</div>';
    return;
  }
  var t = data.totals || {};
  window._cannibalPairs = data.pairs || [];
  var pairs = window._cannibalPairs;
  var html = '<div style="display:flex;gap:18px;font-size:0.95em;color:#666;margin-bottom:10px;flex-wrap:wrap;">'
           +   '<span>Queries analyzed: <strong style="color:#333;">' + (t.queries_analyzed || 0) + '</strong></span>'
           +   '<span>Cannibalized: <strong style="color:#856404;">' + (t.cannibalized_queries || 0) + '</strong>'
           +     ' (' + (t.split_authority_queries || 0) + ' authority-split)</span>'
           +   '<span>Competing page pairs: <strong style="color:#721c24;">' + (t.competing_page_pairs || 0) + '</strong></span>'
           + '</div>';
  if (!pairs.length) {
    html += '<div style="color:#155724;background:#d4edda;border-radius:6px;padding:10px 14px;">'
          + 'No competing page pairs found — every query has one clear page. Nice.</div>';
    el.innerHTML = html;
    return;
  }
  var active = [], accepted = [], done = [];
  pairs.forEach(function(p, i) {
    if (p.consolidated) done.push(i);
    else if (p.dismissed && !p.escalated) accepted.push(i);
    else active.push(i);
  });
  active.forEach(function(i) { html += _cannibalCard(i, 'active'); });
  if (accepted.length) {
    html += '<details style="margin-top:10px;"><summary style="cursor:pointer;color:#888;font-size:0.9em;padding:6px 0;">'
          + accepted.length + ' pair' + (accepted.length !== 1 ? 's' : '') + ' accepted as intentional</summary>';
    accepted.forEach(function(i) { html += _cannibalCard(i, 'accepted'); });
    html += '</details>';
  }
  if (done.length) {
    html += '<details style="margin-top:10px;" open><summary style="cursor:pointer;color:#155724;font-size:0.9em;padding:6px 0;font-weight:600;">'
          + done.length + ' pair' + (done.length !== 1 ? 's' : '') + ' consolidated</summary>';
    done.forEach(function(i) { html += _cannibalCard(i, 'done'); });
    html += '</details>';
  }
  el.innerHTML = html;
}

function _slugOf(path) { return path.charAt(0) === '/' ? path.slice(1) : path; }

function _postLink(path) {
  var url = siteUrl + path + '/';
  return '<a href="' + url + '" target="_blank" rel="noopener" '
       + 'onclick="event.stopPropagation()" '
       + 'style="color:#0066cc;text-decoration:none;border-bottom:1px dotted #0066cc;">'
       + esc(path) + '</a>';
}

function _cannibalCard(i, mode) {
  var p = window._cannibalPairs[i];
  var isAccepted = (mode === 'accepted');
  var isDone = (mode === 'done');
  var sevColor = {high: '#dc3545', medium: '#ffc107', low: '#6c757d'};
  var col = isDone ? '#28a745' : (isAccepted ? '#adb5bd' : (sevColor[p.severity] || '#6c757d'));
  var dim = (isAccepted || isDone) ? 'opacity:0.75;' : '';
  var badge;
  if (isDone) {
    badge = '<span style="background:#28a745;color:#fff;font-size:0.72em;font-weight:700;padding:2px 8px;border-radius:10px;margin-right:8px;">CONSOLIDATED ' + (p.consolidated_at || '') + '</span>';
  } else if (isAccepted) {
    badge = '<span style="background:#e9ecef;color:#495057;font-size:0.72em;font-weight:700;padding:2px 8px;border-radius:10px;margin-right:8px;">ACCEPTED ' + (p.dismissed_at || '') + '</span>';
  } else {
    badge = '<span style="background:' + col + ';color:#fff;font-size:0.72em;font-weight:700;padding:2px 8px;border-radius:10px;margin-right:8px;">' + p.severity.toUpperCase() + '</span>';
  }
  if (p.escalated && !isDone) badge += '<span style="background:#dc3545;color:#fff;font-size:0.72em;font-weight:700;padding:2px 8px;border-radius:10px;margin-right:8px;">ESCALATED since accepted</span>';
  var html = '<details style="border:1px solid #eee;border-left:4px solid ' + col + ';border-radius:6px;margin-bottom:8px;' + dim + '">'
       +  '<summary style="cursor:pointer;padding:10px 14px;">' + badge
       +    _postLink(p.pages[0]) + ' <span style="color:#bbb;">vs</span> ' + _postLink(p.pages[1])
       +    '<span style="color:#888;font-size:0.85em;"> — ' + p.shared_queries + ' shared quer'
       +      (p.shared_queries !== 1 ? 'ies' : 'y') + ', ' + p.total_impressions.toLocaleString() + ' impressions</span>'
       +  '</summary>'
       +  '<div style="padding:4px 16px 12px;">'
       +    '<div style="margin-bottom:6px;"><strong>Winner (more clicks):</strong> ' + _postLink(p.winner) + '</div>'
       + (isDone
            ? '<div style="background:#d4edda;color:#155724;border-radius:6px;padding:8px 12px;margin-bottom:8px;line-height:1.5;">'
              + 'Consolidated into ' + _postLink(p.consolidated_into) + ' on ' + (p.consolidated_at || '')
              + '. Internal links rewritten, 301 in place, loser drafted. The shared queries should '
              + 'converge on the winner over the next few weeks (tracked by the technical audit).</div>'
            : '<div style="background:#f8f9fa;border-radius:6px;padding:8px 12px;margin-bottom:8px;line-height:1.5;">'
              + '<strong>Fix:</strong> ' + esc(p.recommendation) + '</div>')
    + (isDone ? ''
       : isAccepted
         ? '<div style="margin-bottom:8px;">'
           + '<button onclick="dismissByIdx(' + i + ', false)" style="background:#6c757d;color:#fff;border:0;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:0.82em;">Re-check this pair</button>'
           + '</div>'
         : '<div style="margin-bottom:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">'
           + '<button class="btn-pro-locked" disabled onclick="showProUpsell(event)" title="Pro: fetches both posts, compares content + metrics, and recommends keep A / keep B / merge with direction."><span class="lock"> \ud83d\udd12 </span> Compare &amp; recommend (AI) <span class="pro-pill">PRO</span></button>'
           + '<button class="btn-pro-locked" disabled onclick="showProUpsell(event)" title="Pro: rewrites every internal link from the losing page to the winner across your whole site, creates the 301, drafts the loser, and resubmits both URLs."><span class="lock"> \ud83d\udd12 </span> Consolidate pair <span class="pro-pill">PRO</span></button>'
           + '<button onclick="dismissByIdx(' + i + ', true)" style="background:#fff;color:#475569;border:1px solid #cbd5e1;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:0.85em;" title="These pages intentionally serve different intents — hide this pair unless it escalates.">Mark as intentional</button>'
           + '</div>')
       +    '<table style="width:100%;border-collapse:collapse;font-size:0.9em;">'
       +      '<tr style="color:#888;text-align:left;"><th style="padding:3px 8px;">Query</th>'
       +      '<th style="padding:3px 8px;text-align:right;">Impressions</th>'
       +      '<th style="padding:3px 8px;text-align:right;">Position gap</th>'
       +      '<th style="padding:3px 8px;">Type</th></tr>';
  (p.queries || []).forEach(function(q) {
    html += '<tr style="border-top:1px solid #f0f0f0;">'
         +    '<td style="padding:4px 8px;">' + esc(q.query) + '</td>'
         +    '<td style="padding:4px 8px;text-align:right;">' + q.impressions.toLocaleString() + '</td>'
         +    '<td style="padding:4px 8px;text-align:right;">' + q.position_gap + '</td>'
         +    '<td style="padding:4px 8px;">' + (q.kind === 'split_authority'
                ? '<span style="color:#dc3545;font-weight:600;">authority split</span>'
                : '<span style="color:#888;">shadow</span>') + '</td>'
         +  '</tr>';
  });
  html += '</table></div></details>';
  return html;
}

function dismissByIdx(i, val) {
  var p = window._cannibalPairs[i];
  fetch('/api/cannibal/dismiss', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pair: p.pages, dismissed: val, split_queries: p.split_queries})
  }).then(function(r) { return r.json(); }).then(function() {
    showToast(val ? 'Pair accepted as intentional' : 'Pair re-activated', 'success');
    refreshCannibalResults();
  }).catch(function(e) { showToast('Failed: ' + e.message, 'error'); });
}


// ─── Backlink audit ────────────────────────────────────────────────────────
async function runBacklinkAudit() {
  var btn = document.getElementById('btn-run-backlinks');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Running...'; }
  try {
    var r = await fetch('/api/backlinks/run', {method: 'POST'});
    var data = await r.json();
    if (!data.success) {
      showToast('Backlink audit failed: ' + (data.error || '?'), 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Run Backlink Audit'; }
      return;
    }
    var poll = setInterval(async function() {
      try {
        var s = await fetch('/api/backlinks/status'); var sd = await s.json();
        if (sd.status === 'done' || sd.status === 'error') {
          clearInterval(poll);
          if (btn) { btn.disabled = false; btn.textContent = 'Run Backlink Audit'; }
          refreshBacklinkResults();
          if (sd.status === 'error') showToast('Backlink audit failed: ' + (sd.error || '?'), 'error');
          else showToast('Backlink audit done', 'success');
        }
      } catch(e) {}
    }, 3000);
  } catch (e) {
    showToast('Backlink audit call failed: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Run Backlink Audit'; }
  }
}

async function refreshBacklinkResults() {
  try {
    var r = await fetch('/api/backlinks/results');
    var data = await r.json();
    renderBacklinkResults(data);
  } catch(e) {
    document.getElementById('backlink-results').innerHTML =
      '<div style="color:#999;font-style:italic;">Failed to load: ' + esc(e.message) + '</div>';
  }
}

function renderBacklinkResults(data) {
  var el = document.getElementById('backlink-results');
  if (!data || data.message) {
    el.innerHTML = '<div style="color:#999;font-style:italic;">' + esc(data && data.message || 'No data') + '</div>';
    return;
  }
  var t = data.totals || {};
  var html = '<div style="display:flex;gap:18px;font-size:0.85em;color:#666;margin-bottom:12px;flex-wrap:wrap;">'
           +   '<span>Referring domains: <strong style="color:#333;">' + t.referring_domains + '</strong></span>'
           +   '<span>Total backlinks: <strong>' + t.total_backlinks + '</strong></span>'
           +   '<span>Pages with backlinks: <strong>' + t.pages_with_backlinks + '</strong> (' + t.coverage_pct + '%)</span>'
           +   '<span>Unprotected high-traffic: <strong style="color:#856404;">' + t.unprotected_high_traffic + '</strong></span>'
           + '</div>';

  // Domain class breakdown
  var classes = data.domain_classes || {counts: {}, backlinks_per_class: {}};
  var classColors = {'high_authority':'#155724','relevant':'#0066cc','aggregator':'#856404','unknown':'#6c757d'};
  html += '<div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap;">';
  for (var cls of ['high_authority', 'relevant', 'aggregator', 'unknown']) {
    var n = classes.counts[cls] || 0;
    if (!n) continue;
    var bl = classes.backlinks_per_class[cls] || 0;
    html += '<div style="background:#fafafa;border:1px solid #e0e0e0;border-left:4px solid '
         + classColors[cls] + ';padding:8px 12px;border-radius:0 6px 6px 0;font-size:0.82em;">'
         + '<div style="font-weight:600;">' + cls.replace('_', ' ') + '</div>'
         + '<div style="color:#666;">' + n + ' domain' + (n!==1?'s':'') + ' · ' + bl + ' backlink' + (bl!==1?'s':'') + '</div>'
         + '</div>';
  }
  html += '</div>';

  // Top referring domains
  if (data.top_referring_domains && data.top_referring_domains.length) {
    html += '<details open style="margin-bottom:10px;border:1px solid #e0e0e0;border-radius:6px;">'
         + '<summary style="cursor:pointer;padding:10px 14px;font-weight:600;">📡 Top referring domains (' + data.top_referring_domains.length + ')</summary>'
         + '<table style="width:100%;border-collapse:collapse;font-size:0.85em;">'
         + '<thead><tr style="color:#666;border-bottom:1px solid #ddd;background:#fafafa;">'
         +   '<th style="text-align:left;padding:6px 14px;">Domain</th>'
         +   '<th style="text-align:right;padding:6px 14px;">Backlinks</th>'
         +   '<th style="text-align:left;padding:6px 14px;">Class</th></tr></thead><tbody>';
    for (var d of data.top_referring_domains) {
      var clsColor = classColors[d.class] || '#6c757d';
      html += '<tr style="border-bottom:1px solid #f5f5f5;">'
           +   '<td style="padding:5px 14px;"><a href="' + esc(d.domain.startsWith('http')?d.domain:'https://'+d.domain) + '" target="_blank">' + esc(d.domain) + '</a></td>'
           +   '<td style="padding:5px 14px;text-align:right;font-weight:600;">' + d.backlinks + '</td>'
           +   '<td style="padding:5px 14px;"><span style="color:' + clsColor + ';">' + esc(d.class.replace('_',' ')) + '</span></td>'
           + '</tr>';
    }
    html += '</tbody></table></details>';
  } else {
    html += '<div style="color:#999;font-style:italic;font-size:0.85em;margin-bottom:10px;">'
         + 'No ReferringDomains CSV uploaded. Upload it from Bing Webmaster → Reports & Data → Backlinks → Referring Domains → Export.'
         + '</div>';
  }

  // Top backlinked pages on your site
  if (data.top_backlinked_pages && data.top_backlinked_pages.length) {
    html += '<details style="margin-bottom:10px;border:1px solid #e0e0e0;border-radius:6px;">'
         + '<summary style="cursor:pointer;padding:10px 14px;font-weight:600;">🎯 Top backlinked pages on your site (' + data.top_backlinked_pages.length + ')</summary>'
         + '<div style="padding:8px 14px;font-size:0.85em;">';
    for (var p of data.top_backlinked_pages) {
      html += '<div style="padding:4px 0;border-bottom:1px solid #f5f5f5;display:flex;justify-content:space-between;gap:10px;">'
           + '<a href="' + esc(p.url) + '" target="_blank" style="word-break:break-all;">' + esc(p.url) + '</a>'
           + '<span style="color:#666;white-space:nowrap;">' + p.backlinks + ' link' + (p.backlinks!==1?'s':'') + ' · impr=' + p.impressions + '</span>'
           + '</div>';
    }
    html += '</div></details>';
  }

  // Unprotected high-traffic — outreach priority
  if (data.unprotected_high_traffic && data.unprotected_high_traffic.length) {
    var urls = data.unprotected_high_traffic.map(function(p) { return p.url; });
    html += '<details open style="margin-bottom:10px;border:1px solid #e0e0e0;border-radius:6px;border-left:4px solid #ffc107;background:#fff;">'
         + '<summary style="cursor:pointer;padding:10px 14px;font-weight:600;">'
         +   '🔗 Outreach priority — high traffic, zero backlinks (' + data.unprotected_high_traffic.length + ')'
         + '</summary>'
         + '<div style="padding:10px 14px;font-size:0.82em;color:#666;background:#fffbf0;border-bottom:1px solid #ffe0b2;">'
         + 'These pages get 500+ impressions but no external backlinks. Each link to them compounds rank stability.'
         + ' Manual outreach: forum mentions, guest posts, community shoutouts.'
         + '</div>'
         + '<div style="padding:8px 14px;font-size:0.85em;">';
    for (var p of data.unprotected_high_traffic) {
      html += '<div style="padding:4px 0;border-bottom:1px solid #f5f5f5;display:flex;justify-content:space-between;gap:10px;">'
           + '<a href="' + esc(p.url) + '" target="_blank" style="word-break:break-all;">' + esc(p.url) + '</a>'
           + '<span style="color:#856404;white-space:nowrap;font-weight:600;">impr=' + p.impressions + ' · clicks=' + p.clicks + '</span>'
           + '</div>';
    }
    html += '</div></details>';
  }

  el.innerHTML = html;
}

// ─── Trend analysis ────────────────────────────────────────────────────────
async function runTrendAnalysis() {
  var btn = document.getElementById('btn-run-trends');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Running...'; }
  try {
    var r = await fetch('/api/trends/run', {method: 'POST', headers: {'Content-Type':'application/json'},
                                              body: JSON.stringify({})});
    var data = await r.json();
    if (!data.success) {
      showToast('Trend analysis failed: ' + data.error, 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Run Trend Analysis'; }
      return;
    }
    showToast('Trend analysis started — refreshing in 30s', 'success');
    // Poll for completion then refresh
    var poll = setInterval(async function() {
      try {
        var s = await fetch('/api/trends/status'); var sd = await s.json();
        if (sd.status === 'done' || sd.status === 'error') {
          clearInterval(poll);
          if (btn) { btn.disabled = false; btn.textContent = 'Run Trend Analysis'; }
          refreshTrendResults();
          if (sd.status === 'error') showToast('Trend analysis failed: ' + (sd.error || '?'), 'error');
          else showToast('Trend analysis done', 'success');
        }
      } catch(e) {}
    }, 3000);
  } catch(e) {
    showToast('Trend analysis call failed: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Run Trend Analysis'; }
  }
}

async function refreshTrendResults() {
  try {
    var r = await fetch('/api/trends/results');
    var data = await r.json();
    renderTrendResults(data);
  } catch(e) {
    document.getElementById('trend-results').innerHTML =
      '<div style="color:#999;font-style:italic;">Failed to load: ' + esc(e.message) + '</div>';
  }
}

// Signal colors + icons for trend results
var TREND_SIGNALS = {
  'audience_growth':              {color: '#28a745', icon: '📈', label: 'Audience growth'},
  'viral_spike':                  {color: '#0066cc', icon: '🚀', label: 'Viral spike'},
  'ctr_improvement_only':         {color: '#ffc107', icon: '⚙️', label: 'CTR improvement only'},
  'title_degradation':            {color: '#fd7e14', icon: '📉', label: 'Title degradation'},
  'ranking_loss':                 {color: '#dc3545', icon: '⚠️', label: 'Ranking loss'},
  'ctr_problem_despite_ranking':  {color: '#fd7e14', icon: '🔧', label: 'CTR problem despite good ranking'},
  'stable':                       {color: '#6c757d', icon: '➖', label: 'Stable'},
  'insufficient_data':            {color: '#6c757d', icon: '❓', label: 'Insufficient data'},
};

function renderTrendResults(data) {
  var el = document.getElementById('trend-results');
  if (!data || data.message) {
    el.innerHTML = '<div style="color:#999;font-style:italic;">' + esc(data && data.message || 'No trend analysis yet — click Run.') + '</div>';
    return;
  }
  var html = '<div style="font-size:0.78em;color:#888;margin-bottom:10px;">'
           + 'Generated: ' + (data.generated_at || '').substring(0, 16).replace('T', ' ')
           + ' &nbsp;|&nbsp; Window: ' + data.window_days + 'd, bucket: ' + data.bucket_days + 'd'
           + '</div>';
  for (var src of ['gsc', 'bing']) {
    var a = data[src];
    if (!a) continue;
    var sig = TREND_SIGNALS[a.signal] || {color:'#6c757d', icon:'•', label:a.signal||'unknown'};
    html += '<div style="border:1px solid #e0e0e0;border-radius:6px;border-left:4px solid '+sig.color+';'
         +   'padding:12px 14px;margin-bottom:10px;background:#fafafa;">'
         + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
         +   '<strong style="font-size:0.95em;">' + esc(a.name) + '</strong>'
         +   '<span style="background:'+sig.color+';color:#fff;padding:2px 10px;border-radius:12px;font-size:0.78em;">'
         +     sig.icon + ' ' + esc(sig.label) + '</span>'
         + '</div>';
    if (a.error) {
      html += '<div style="color:#dc3545;">'+esc(a.error)+'</div></div>'; continue;
    }
    var p = a.periods, d = a.deltas;
    var fmt = function(v) { return v === null || v === undefined ? '-' : v + '%'; };
    var deltaColor = function(v, inverse) {
      if (v === null || v === undefined || Math.abs(v) < 5) return '#666';
      var good = inverse ? v < 0 : v > 0;
      return good ? '#155724' : '#721c24';
    };
    html += '<table style="width:100%;border-collapse:collapse;font-size:0.82em;margin:8px 0;">'
         + '<thead><tr style="color:#666;border-bottom:1px solid #ddd;">'
         +   '<th style="text-align:left;padding:4px 8px;">period</th>'
         +   '<th style="text-align:right;padding:4px 8px;">days</th>'
         +   '<th style="text-align:right;padding:4px 8px;">clicks/day</th>'
         +   '<th style="text-align:right;padding:4px 8px;">impr/day</th>'
         +   '<th style="text-align:right;padding:4px 8px;">CTR</th>'
         +   '<th style="text-align:right;padding:4px 8px;">avg pos</th>'
         + '</tr></thead><tbody>';
    for (var pname of ['first30', 'mid30', 'last30']) {
      var per = p[pname];
      html += '<tr><td style="padding:3px 8px;">'+pname+'</td>'
           +    '<td style="padding:3px 8px;text-align:right;color:#888;">'+per.days+'</td>'
           +    '<td style="padding:3px 8px;text-align:right;">'+per.clicks_per_day+'</td>'
           +    '<td style="padding:3px 8px;text-align:right;">'+Math.round(per.impr_per_day)+'</td>'
           +    '<td style="padding:3px 8px;text-align:right;">'+per.ctr_avg+'%</td>'
           +    '<td style="padding:3px 8px;text-align:right;color:#888;">'+per.position_avg+'</td>'
           + '</tr>';
    }
    html += '<tr style="border-top:1px solid #ddd;font-weight:600;">'
         +   '<td style="padding:5px 8px;">Δ first30→last30</td>'
         +   '<td></td>'
         +   '<td style="padding:5px 8px;text-align:right;color:'+deltaColor(d.clicks_per_day_pct)+';">'+fmt(d.clicks_per_day_pct)+'</td>'
         +   '<td style="padding:5px 8px;text-align:right;color:'+deltaColor(d.impr_per_day_pct)+';">'+fmt(d.impr_per_day_pct)+'</td>'
         +   '<td style="padding:5px 8px;text-align:right;color:'+deltaColor(d.ctr_pct)+';">'+fmt(d.ctr_pct)+'</td>'
         +   '<td style="padding:5px 8px;text-align:right;color:'+deltaColor(d.position_pct, true)+';">'+fmt(d.position_pct)+'</td>'
         + '</tr></tbody></table>';
    html += '<div style="font-size:0.83em;color:#444;line-height:1.5;padding:6px 0;">'
         +   '<strong>' + sig.icon + ' ' + esc(sig.label) + '.</strong> ' + esc(a.signal_message)
         + '</div></div>';
  }
  el.innerHTML = html;
}

function renderReportsList(data) {
  var el = document.getElementById('reports-list');
  if (!data.dates || !data.dates.length) {
    el.innerHTML = '<div style="color:#999;font-style:italic;">No reports uploaded yet. Drop files above.</div>';
    return;
  }
  var html = '<div style="color:#888;font-size:0.78em;margin-bottom:8px;">Base: ' + esc(data.base_dir) + '</div>';
  for (var i = 0; i < data.dates.length; i++) {
    var df = data.dates[i];
    html += '<div style="margin-bottom:12px;">'
         +   '<div style="font-weight:600;color:#333;border-bottom:1px solid #eee;padding-bottom:4px;margin-bottom:4px;">'
         +     '📅 ' + esc(df.date) + ' <span style="color:#888;font-weight:normal;font-size:0.85em;">(' + df.files.length + ' files)</span>'
         +   '</div>';
    if (!df.files.length) {
      html += '<div style="color:#bbb;font-style:italic;padding-left:18px;">empty</div>';
    } else {
      html += '<table style="width:100%;border-collapse:collapse;font-size:0.82em;">';
      for (var j = 0; j < df.files.length; j++) {
        var f = df.files[j];
        var sizeKB = Math.round(f.size / 1024 * 10) / 10;
        var typeLabel = (f.detected_type || 'unknown').replace(/_/g, ' ');
        html += '<tr style="border-bottom:1px solid #f5f5f5;">'
             +   '<td style="padding:3px 6px;font-family:Consolas,monospace;font-size:0.92em;">' + esc(f.name) + '</td>'
             +   '<td style="padding:3px 6px;color:#888;width:120px;">' + esc(typeLabel) + '</td>'
             +   '<td style="padding:3px 6px;text-align:right;color:#888;width:70px;">' + sizeKB + ' KB</td>'
             +   '<td style="padding:3px 6px;text-align:right;width:30px;">'
             +     '<a href="#" onclick="deleteReport(\\'' + esc(df.date) + '\\',\\'' + esc(f.name.replace(/\\\\/g, "\\\\\\\\").replace(/\\'/g, "\\\\\\'")) + '\\');return false;" '
             +     'style="color:#dc3545;text-decoration:none;" title="Delete">✕</a>'
             +   '</td>'
             + '</tr>';
      }
      html += '</table>';
    }
    html += '</div>';
  }
  el.innerHTML = html;
}

function dropReports(e) {
  e.preventDefault();
  e.currentTarget.style.background = '#fafafa';
  uploadReports(e.dataTransfer.files);
}

async function uploadReports(fileList) {
  if (!fileList || !fileList.length) return;
  var status = document.getElementById('upload-status');
  status.textContent = 'Uploading ' + fileList.length + ' file(s)...';
  var ok = 0, fail = 0;
  for (var i = 0; i < fileList.length; i++) {
    var f = fileList[i];
    var fd = new FormData();
    fd.append('file', f);
    try {
      var r = await fetch('/api/reports/upload', { method: 'POST', body: fd });
      var j = await r.json();
      if (j.success) {
        ok++;
        status.textContent = 'Uploaded ' + ok + '/' + fileList.length + ' — last: ' + f.name + ' (' + j.detected_type + ')';
      } else {
        fail++;
        status.textContent = '⚠ ' + f.name + ': ' + (j.error || 'failed');
      }
    } catch (e) {
      fail++;
      status.textContent = '⚠ ' + f.name + ': ' + e.message;
    }
  }
  status.textContent = '✓ ' + ok + ' uploaded' + (fail ? ', ' + fail + ' failed' : '');
  refreshReports();
}

async function deleteReport(date, name) {
  if (!confirm('Delete ' + name + ' from ' + date + '?')) return;
  try {
    var r = await fetch('/api/reports/delete', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({date: date, name: name})
    });
    var j = await r.json();
    if (j.success) {
      refreshReports();
    } else {
      alert('Delete failed: ' + (j.error || 'unknown'));
    }
  } catch (e) {
    alert('Delete error: ' + e.message);
  }
}

async function runTechnicalAudit() {
  var btn = document.getElementById('btn-run-audit');
  btn.disabled = true; btn.textContent = 'Running...';
  try {
    var r = await fetch('/api/reports/audit', {method:'POST'});
    var j = await r.json();
    if (!j.success && j.status !== 'running') {
      alert('Could not start audit: ' + (j.error || 'unknown'));
      btn.disabled = false; btn.textContent = 'Run Technical Audit';
      return;
    }
    // Poll for completion
    _auditPoll = setInterval(pollAudit, 2000);
  } catch (e) {
    alert('Audit start error: ' + e.message);
    btn.disabled = false; btn.textContent = 'Run Technical Audit';
  }
}

async function pollAudit() {
  try {
    var r = await fetch('/api/audit/status');
    var j = await r.json();
    if (j.status !== 'running') {
      clearInterval(_auditPoll); _auditPoll = null;
      var btn = document.getElementById('btn-run-audit');
      btn.disabled = false; btn.textContent = 'Run Technical Audit';
      if (j.status === 'error') {
        alert('Audit failed: ' + (j.error || 'unknown'));
      }
      refreshAuditResults();
    }
  } catch (e) { /* keep polling */ }
}

async function refreshAuditResults() {
  var el = document.getElementById('audit-results');
  try {
    var r = await fetch('/api/audit/results');
    var data = await r.json();
    if (data.message) { el.innerHTML = '<div style="color:#999;font-style:italic;">' + esc(data.message) + '</div>'; return; }
    renderAuditResults(data);
  } catch (e) {
    el.textContent = 'Failed: ' + e.message;
  }
}

// Category metadata — ordered by urgency; explains what each rule means + recommended action
// (Use DOUBLE quotes for the summary strings so apostrophes inside don't break parsing.)
var AUDIT_CATEGORIES = [
  {rule: 'server_error_critical', icon: '🚨', color: '#dc3545', label: 'Server errors (5xx)',
   summary: "Your server returns HTTP 5xx for these URLs. URGENT — these block indexing entirely. " +
            "Check your server logs and fix the underlying error before doing anything else."},
  {rule: 'active_bleed', icon: '⚠️', color: '#dc3545', label: '404s with active traffic',
   summary: "Users are landing on dead pages (URL returns 404 but still shows in search results with impressions). " +
            "Either restore the page, or request removal from Bing (button on each card or bulk-remove above). " +
            "Removal is ~6-month suppression — permanent only if URL stays 404 when search engines re-check."},
  {rule: 'stale_index', icon: '⚠️', color: '#fd7e14', label: 'Stale index (404 but Bing shows 200)',
   summary: "Bing's SiteExplorer report says these URLs return 200, but a live HEAD check confirms they're actually 404. " +
            "Bing's index is stale. Same fix as active_bleed: request removal so Bing re-evaluates faster."},
  {rule: 'active_duplicate', icon: '🔴', color: '#dc3545', label: 'Active duplicate URLs',
   summary: "Same content served from multiple URLs, ALL returning 200 — no redirect in place. " +
            "Search engines split ranking signal across both. Fix: add 301 redirects from non-canonical → canonical, " +
            "OR set rel=canonical tags. This is a server/CMS config issue, not a wait-and-see problem."},
  {rule: 'redirect_not_consolidated', icon: '⏳', color: '#0066cc', label: 'Pending consolidation (search engine lag)',
   summary: "Server correctly 301-redirects to the canonical URL, but Google/Bing haven't updated their index yet — " +
            "they still show both URLs. NOT a bug on your end. Will self-resolve in 4-8 weeks. " +
            "To speed it up: submit the canonical URL via Bing URL Inspection → Request Indexing (and same in GSC)."},
  {rule: 'attachment_waste', icon: '🗑️', color: '#ffc107', label: 'WordPress attachment pages indexed',
   summary: "WP creates a separate page for every image attachment (e.g. ?attachment_id=1234). These thin pages waste " +
            "crawl budget. Fix once globally: Yoast SEO → Search Appearance → Media → 'Redirect attachment URLs to attachment itself' = YES."},
  {rule: 'sitemap_pollution', icon: '🗑️', color: '#ffc107', label: 'Sitemap contains dead URLs',
   summary: "Your sitemap.xml lists URLs that return 4xx/3xx. Crawlers keep re-checking them. " +
            "Regenerate the sitemap: Yoast SEO → Tools → File editor → Reset sitemap, OR equivalent in your SEO plugin."},
  {rule: 'unprotected_high_traffic', icon: '🔗', color: '#28a745', label: 'High-traffic pages with no backlinks',
   summary: "These pages get 500+ impressions but have zero external backlinks — they're vulnerable to algorithm updates. " +
            "Not urgent, but long-term outreach targets. Adding even 1-2 quality links to each compounds rank stability."},
];

function renderAuditResults(data) {
  var t = data.totals || {};
  var bleedUrls = (data.findings || []).filter(function(f) { return f.rule === 'active_bleed'; })
                                       .map(function(f) { return f.url; });
  window._auditBleedUrls = bleedUrls;

  // Summary stats line
  var html = '<div style="display:flex;gap:18px;font-size:0.82em;color:#666;margin-bottom:10px;flex-wrap:wrap;">'
           +   '<span>URLs audited: <strong style="color:#333;">' + (t.urls_audited || 0) + '</strong></span>'
           +   '<span>Live 200s: <strong style="color:#155724;">' + (t.live_200 || 0) + '</strong></span>'
           +   '<span>3xx: ' + (t.redirects_3xx || 0) + '</span>'
           +   '<span>4xx: <strong style="color:#856404;">' + (t.errors_4xx || 0) + '</strong></span>'
           +   '<span>5xx: <strong style="color:#721c24;">' + (t.errors_5xx || 0) + '</strong></span>'
           +   '<span>findings: <strong>' + (t.findings || 0) + '</strong> (' + (t.critical_findings || 0) + ' crit + ' + (t.high_findings || 0) + ' high)</span>'
           + '</div>';

  // Note: bulk URL-removal (Bing AddBlockedUrl) is a Premium feature.
  // Free tier shows the 404 list — you fix the 404s on your end, search engines re-crawl + drop them naturally.
  if (bleedUrls.length > 0) {
    html += '<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:10px 14px;margin-bottom:14px;font-size:0.85em;">'
         +   '<strong>' + bleedUrls.length + ' active 404 URL' + (bleedUrls.length !== 1 ? 's' : '') + ' indexed</strong> — '
         +   '<span style="color:#666;">these are getting search clicks but returning 404. Either restore them, 301-redirect to a relevant page, or wait for search engines to drop them on next crawl.</span><br>'
         +   '<button class="btn-pro-locked" disabled onclick="showProUpsell(event)" style="margin-top:8px;"'
         +   ' title="Pro: submit these URLs to Bing AddBlockedUrl in one click for faster removal.">'
         +     '<span class="lock"> 🔒 </span> Submit all ' + bleedUrls.length + ' for removal (Bing) <span class="pro-pill">PRO</span>'
         +   '</button>'
         + '</div>';
  }

  // Group findings by rule
  var grouped = {};
  (data.findings || []).forEach(function(f) {
    (grouped[f.rule] = grouped[f.rule] || []).push(f);
  });

  // Render each category in defined order (urgency-sorted), then any extras
  var rendered = {};
  AUDIT_CATEGORIES.forEach(function(cat) {
    if (grouped[cat.rule]) {
      html += renderAuditCategory(cat, grouped[cat.rule]);
      rendered[cat.rule] = true;
    }
  });
  // Catch-all for any rule not in our category list
  Object.keys(grouped).forEach(function(rule) {
    if (rendered[rule]) return;
    html += renderAuditCategory({rule: rule, icon: '•', color: '#6c757d',
                                 label: rule, summary: ''}, grouped[rule]);
  });

  document.getElementById('audit-results').innerHTML = html;
}

function renderAuditCategory(cat, findings) {
  // open the first 1-2 categories by default — the urgent ones
  var openByDefault = (cat.rule === 'server_error_critical' || cat.rule === 'active_bleed' ||
                       cat.rule === 'stale_index' || cat.rule === 'active_duplicate');
  var openAttr = openByDefault ? ' open' : '';
  var html = '<details' + openAttr + ' style="margin-bottom:10px;border:1px solid #e0e0e0;border-radius:6px;'
           + 'border-left:4px solid ' + cat.color + ';background:#fff;">'
           + '<summary style="cursor:pointer;padding:10px 14px;font-size:0.92em;list-style:none;display:flex;'
           +   'justify-content:space-between;align-items:center;user-select:none;">'
           +   '<span><span style="font-size:1.1em;margin-right:8px;">' + cat.icon + '</span>'
           +     '<strong>' + esc(cat.label) + '</strong></span>'
           +   '<span style="background:' + cat.color + ';color:#fff;padding:2px 10px;border-radius:12px;font-size:0.78em;font-weight:600;">'
           +     findings.length + '</span>'
           + '</summary>';

  // Category explanation
  if (cat.summary) {
    html += '<div style="padding:0 14px 10px 14px;font-size:0.82em;color:#555;line-height:1.5;border-bottom:1px solid #f0f0f0;">'
         +   esc(cat.summary)
         + '</div>';
  }

  // Helper used by per-finding and bulk buttons: HTML-escape a JSON value so
  // it survives being embedded inside an onclick="..." attribute. Without
  // this, JSON.stringify's inner double-quotes terminate the outer attribute
  // and the click handler becomes garbage — that's why buttons silently failed
  // to fire before. Defined at function scope (NOT inside forEach) so the
  // bulk submit-canonicals button at the bottom can reach it too.
  function _htmlJson(v) { return JSON.stringify(v).replace(/"/g, '&quot;'); }

  // Findings list inside this category
  html += '<div style="padding:8px 14px 12px 14px;">';
  findings.forEach(function(f) {
    html += '<div style="border-left:3px solid ' + cat.color + ';padding:8px 12px;margin-bottom:6px;background:#fafafa;border-radius:0 4px 4px 0;">';
    if (f.message) html += '<div style="font-size:0.85em;">' + esc(f.message) + '</div>';
    if (f.url) {
      html += '<div style="font-size:0.78em;color:#666;margin-top:3px;font-family:Consolas,monospace;word-break:break-all;">'
           +   '<a href="' + esc(f.url) + '" target="_blank">' + esc(f.url) + '</a>';
      if (f.impressions) html += ' &nbsp;impr=' + f.impressions + ' clicks=' + (f.clicks || 0);
      html += '</div>';
    }
    if (f.duplicate_urls || f.urls) {
      var dups = f.duplicate_urls || f.urls;
      html += '<div style="font-size:0.78em;color:#666;margin-top:3px;">';
      for (var k = 0; k < Math.min(5, dups.length); k++) {
        var d = dups[k];
        html += '<div style="font-family:Consolas,monospace;word-break:break-all;">'
             +   '→ <a href="' + esc(d.url) + '" target="_blank">' + esc(d.url) + '</a>'
             +   ' [HTTP ' + (d.live_status || d.csv_http_code || '?') + ', impr=' + (d.impressions || 0) + ']'
             + '</div>';
      }
      html += '</div>';
    }
    if (f.sample_urls) {
      html += '<div style="font-size:0.78em;color:#666;margin-top:3px;">';
      for (var k = 0; k < Math.min(5, f.sample_urls.length); k++) {
        var u = f.sample_urls[k];
        var us = (typeof u === 'string') ? u : (u.url || JSON.stringify(u));
        html += '<div style="font-family:Consolas,monospace;word-break:break-all;">→ ' + esc(us) + '</div>';
      }
      html += '</div>';
    }
    if (f.fix) html += '<div style="font-size:0.82em;color:#0066cc;margin-top:4px;"><strong>Fix:</strong> ' + esc(f.fix) + '</div>';

    // Per-finding action buttons — match button to rule
    // (uses _htmlJson defined at the renderAuditCategory scope above)
    var actionBtns = '';

    // URL removal (Bing AddBlockedUrl) is a Premium feature — show teaser button on 404s.
    if (f.rule === 'active_bleed' && f.url) {
      actionBtns += '<button class="btn-pro-locked" disabled onclick="showProUpsell(event)" '
                 + 'style="margin-right:6px;" '
                 + 'title="Pro: submit this URL to Bing AddBlockedUrl in one click.">'
                 +   '<span class="lock"> 🔒 </span> Remove from Bing <span class="pro-pill">PRO</span>'
                 + '</button>';
    }
    // For pending-consolidation + active duplicates: submit canonical URL to push reindex
    var submitUrl = f.canonical_url || f.canonical_candidate;
    if ((f.rule === 'redirect_not_consolidated' || f.rule === 'active_duplicate') && submitUrl) {
      actionBtns += '<button onclick="submitSingleUrl(' + _htmlJson(submitUrl) + ')" '
                 + 'style="background:#0066cc;color:#fff;border:0;padding:5px 12px;border-radius:4px;cursor:pointer;font-size:0.85em;">'
                 +   'Submit canonical to Bing'
                 + '</button>';
    }
    if (actionBtns) html += '<div style="margin-top:8px;">' + actionBtns + '</div>';
    html += '</div>';
  });

  // Bulk action at the bottom of redirect_not_consolidated — 85 URLs in one batch
  if (cat.rule === 'redirect_not_consolidated' && findings.length >= 5) {
    var canonicals = findings.map(function(f) { return f.canonical_url; }).filter(Boolean);
    if (canonicals.length) {
      html += '<div style="padding:10px 14px;border-top:1px solid #f0f0f0;background:#f8fafe;">'
           +   '<button onclick="submitCanonicalsBatch(' + _htmlJson(canonicals) + ')" '
           +   'style="background:#0066cc;color:#fff;border:0;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:0.85em;">'
           +     'Submit ALL ' + canonicals.length + ' canonical URLs to Bing (batch)'
           +   '</button>'
           +   '<div style="font-size:0.78em;color:#888;margin-top:6px;">Bing batch endpoint accepts up to 500 URLs in one call.</div>'
           + '</div>';
    }
  }
  html += '</div></details>';
  return html;
}

// Per-finding submit action (uses existing /api/submit-url endpoint)
async function submitSingleUrl(url) {
  if (!confirm('Submit URL to Bing for re-indexing?\\n\\n' + url)) return;
  try {
    var r = await fetch('/api/submit-url', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url: url}),
    });
    var data = await r.json();
    var g = data.google_ok ? 'OK' : (data.google_msg || 'failed');
    var b = data.bing_ok ? 'OK' : (data.bing_msg || 'failed');
    showToast('Submitted: Google ' + g + ' · Bing ' + b, (data.google_ok || data.bing_ok) ? 'success' : 'error');
  } catch (e) { showToast('Submit failed: ' + e.message, 'error'); }
}

// Bulk submit canonicals — uses /api/url/submit-batch endpoint
async function submitCanonicalsBatch(urls) {
  if (!confirm('Submit ' + urls.length + ' canonical URLs to Bing?\\n\\n' +
               'Submits to Bing Webmaster Tools.')) return;
  try {
    var r = await fetch('/api/url/submit-batch', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({urls: urls}),
    });
    var data = await r.json();
    if (data.success) {
      showToast(data.count + ' URLs submitted · Google ' + data.google_ok + '/' + data.count +
                ' OK · Bing ' + (data.bing_ok ? 'OK' : 'failed'), 'success');
    } else {
      showToast('Bulk submit failed: ' + (data.error || '?'), 'error');
    }
  } catch (e) { showToast('Bulk submit failed: ' + e.message, 'error'); }
}


</script>

</body>
</html>""")


# ─── HTTP Handler ───────────────────────────────────────────────────────────
class ReviewHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _get_template_vars(self):
        """Build all template variables for the HTML page."""
        target = 'LIVE' if STATE['live'] else 'LOCAL'
        has_fixes = len(STATE['fixes']) > 0

        # Pipeline step statuses
        with PIPELINE_LOCK:
            fs = PIPELINE_STATE['fetch']['status']
            us = PIPELINE_STATE['upload']['status']
            ans = PIPELINE_STATE['analyze']['status']
            upload_fn = PIPELINE_STATE['upload'].get('filename', '')

        fetch_done = fs == 'done'
        analyze_done = ans == 'done'

        # Upload area HTML — always show upload area; show filename if already uploaded
        upload_area = (
            '<div class="pipeline-upload-area" id="pipeline-drop-area" '
            'onclick="document.getElementById(\'pipeline-file-input\').click()" '
            'ondragover="event.preventDefault(); this.classList.add(\'dragover\')" '
            'ondragleave="this.classList.remove(\'dragover\')" '
            'ondrop="handlePipelineDrop(event)">'
            '<p><strong>Click to browse</strong> or drag &amp; drop a CrawlyCat HTML report</p>'
            '<input type="file" id="pipeline-file-input" accept=".html" '
            'onchange="uploadPipelineReport(this.files[0])">'
            '</div>'
        )
        if us == 'done' and upload_fn:
            upload_html = (f'<div class="pipeline-upload-done" style="margin-bottom:10px">'
                           f'Current: <strong>{upload_fn}</strong></div>' + upload_area)
        else:
            upload_html = upload_area

        def status_label(s):
            return s.replace('_', ' ').upper()

        # Review status
        if analyze_done and has_fixes:
            rv_status = 'done'
            rv_label = f'{len(STATE["fixes"])} CHANGES'
        elif analyze_done:
            rv_status = 'done'
            rv_label = 'READY'
        else:
            rv_status = 'not_started'
            rv_label = 'NOT STARTED'

        # Which tab is active by default
        if has_fixes:
            pipeline_tab_active = ''
            changes_tab_active = 'active'
            pipeline_panel_active = ''
            changes_panel_active = 'active'
        else:
            pipeline_tab_active = 'active'
            changes_tab_active = ''
            pipeline_panel_active = 'active'
            changes_panel_active = ''

        return {
            'site_name': SITE_NAME,
            'site_domain': SITE_DOMAIN,
            'target': target,
            'fixes_json': json.dumps(STATE['fixes']),
            'outcomes_json': json.dumps({}),  # Pro feature — empty stub keeps template happy
            'pipeline_tab_active': pipeline_tab_active,
            'changes_tab_active': changes_tab_active,
            'pipeline_panel_active': pipeline_panel_active,
            'changes_panel_active': changes_panel_active,
            'fetch_status': fs,
            'fetch_status_label': status_label(fs),
            'upload_status': us,
            'upload_status_label': status_label(us) if us != 'done' else (upload_fn or 'DONE'),
            'upload_locked': '' if fetch_done else 'locked',
            'upload_content': upload_html,
            'analyze_status': ans,
            'analyze_status_label': status_label(ans),
            'analyze_locked': '' if fetch_done else 'locked',
            'analyze_disabled': '' if fetch_done else 'disabled',
            'review_status': rv_status,
            'review_status_label': rv_label,
            'review_locked': '' if analyze_done else 'locked',
            'review_disabled': '' if analyze_done else 'disabled',
        }

    def do_GET(self):
        # First-run setup wizard: redirect to /setup if config is the stub
        if self.path == '/' or self.path == '/index.html':
            if _is_unconfigured():
                self._respond(302, 'text/html', b'', extra_headers=[('Location', '/setup')])
                return
            tvars = self._get_template_vars()
            page = HTML_TEMPLATE.substitute(**tvars)
            self._respond(200, 'text/html', page.encode('utf-8', 'replace'))  # 'replace' guards against lone surrogates in fix data

        elif self.path == '/setup' or self.path == '/settings':
            self._respond(200, 'text/html', SETUP_WIZARD_HTML.encode('utf-8'))

        elif self.path == '/api/google/status':
            try:
                import google_oauth
                self._respond(200, 'application/json', json.dumps(google_oauth.status()).encode('utf-8'))
            except Exception as e:
                self._respond(200, 'application/json', json.dumps({'connected': False, 'error': str(e)}).encode('utf-8'))

        elif self.path == '/api/setup/state':
            # Return current config snapshot (no secrets — masked) so wizard can
            # pre-populate the form when re-editing.
            self._respond(200, 'application/json',
                          json.dumps(_setup_state_snapshot()).encode('utf-8'))

        elif self.path == '/api/logs':
            self._respond(200, 'application/json', json.dumps(STATE['logs']).encode('utf-8'))

        elif self.path == '/api/fixes':
            self._respond(200, 'application/json',
                          json.dumps(STATE['fixes']).encode('utf-8'))

        elif self.path == '/api/pipeline/status':
            with PIPELINE_LOCK:
                status = {
                    'fetch': dict(PIPELINE_STATE['fetch']),
                    'upload': dict(PIPELINE_STATE['upload']),
                    'analyze': dict(PIPELINE_STATE['analyze']),
                }
            self._respond(200, 'application/json', json.dumps(status).encode('utf-8'))

        elif self.path == '/api/reports/list':
            base, dates = self._reports_base_for_site()
            self._respond(200, 'application/json', json.dumps({
                'base_dir': base,
                'site_key': 'nerdy' if 'nerdy' in SITE_DOMAIN.lower() else 'DD',
                'dates': dates,
            }).encode('utf-8'))

        elif self.path == '/api/buckets/results':
            path = os.path.join(OUTPUT_DIR, 'page-buckets.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._respond(200, 'application/json', json.dumps(data, ensure_ascii=False).encode('utf-8', 'replace'))
                except Exception as e:
                    self._respond(500, 'application/json', json.dumps({'error': str(e)}).encode('utf-8'))
            else:
                self._respond(200, 'application/json',
                              json.dumps({'message': 'No bucket data yet. Run "Analyze & Generate Fixes" to populate.'}).encode('utf-8'))

        elif self.path == '/api/cannibal/results':
            path = os.path.join(OUTPUT_DIR, 'cannibalization-audit.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    # Merge in dismissed-pair state (+ escalation re-alarm)
                    dpath = os.path.join(OUTPUT_DIR, 'cannibal-dismissed.json')
                    dismissed = {}
                    if os.path.exists(dpath):
                        try:
                            with open(dpath, 'r', encoding='utf-8') as f:
                                dismissed = json.load(f)
                        except Exception:
                            pass
                    # consolidated pairs (from consolidations.json) — show as done
                    consolidated = {}
                    cpath = os.path.join(OUTPUT_DIR, 'consolidations.json')
                    if os.path.exists(cpath):
                        try:
                            with open(cpath, 'r', encoding='utf-8') as f:
                                for c in json.load(f):
                                    k = '|'.join(sorted(['/' + c['loser'].strip('/'),
                                                         '/' + c['winner'].strip('/')]))
                                    consolidated[k] = c
                        except Exception:
                            pass
                    for p in data.get('pairs', []):
                        key = '|'.join(sorted(p.get('pages', [])))
                        c = consolidated.get(key)
                        if c:
                            p['consolidated'] = True
                            p['consolidated_at'] = c.get('date', '')[:10]
                            p['consolidated_into'] = '/' + c['winner'].strip('/')
                        d = dismissed.get(key)
                        if d:
                            p['dismissed'] = True
                            p['dismissed_at'] = d.get('date', '')[:10]
                            p['escalated'] = p.get('split_queries', 0) > d.get('split_queries', 0)
                    self._respond(200, 'application/json', json.dumps(data, ensure_ascii=False).encode('utf-8', 'replace'))
                except Exception as e:
                    self._respond(500, 'application/json', json.dumps({'error': str(e)}).encode('utf-8'))
            else:
                self._respond(200, 'application/json',
                              json.dumps({'message': 'No cannibalization audit yet — click Run.'}).encode('utf-8'))

        elif self.path == '/api/cannibal/status':
            with PIPELINE_LOCK:
                status = dict(PIPELINE_STATE.get('cannibal', {'status': 'idle'}))
            self._respond(200, 'application/json', json.dumps(status).encode('utf-8'))

        elif self.path == '/api/backlinks/results':
            path = os.path.join(OUTPUT_DIR, 'backlink-audit.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._respond(200, 'application/json', json.dumps(data, ensure_ascii=False).encode('utf-8', 'replace'))
                except Exception as e:
                    self._respond(500, 'application/json', json.dumps({'error': str(e)}).encode('utf-8'))
            else:
                self._respond(200, 'application/json',
                              json.dumps({'message': 'No backlink audit yet — click Run.'}).encode('utf-8'))

        elif self.path == '/api/backlinks/status':
            with PIPELINE_LOCK:
                status = dict(PIPELINE_STATE.get('backlinks', {'status': 'idle'}))
            self._respond(200, 'application/json', json.dumps(status).encode('utf-8'))

        elif self.path == '/api/trends/results':
            path = os.path.join(OUTPUT_DIR, 'trend-analysis.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._respond(200, 'application/json', json.dumps(data, ensure_ascii=False).encode('utf-8', 'replace'))
                except Exception as e:
                    self._respond(500, 'application/json', json.dumps({'error': str(e)}).encode('utf-8'))
            else:
                self._respond(200, 'application/json',
                              json.dumps({'message': 'No trend analysis yet. Click "Run Trend Analysis".'}).encode('utf-8'))

        elif self.path == '/api/trends/status':
            with PIPELINE_LOCK:
                status = dict(PIPELINE_STATE.get('trends', {'status': 'idle'}))
            self._respond(200, 'application/json', json.dumps(status).encode('utf-8'))

        elif self.path == '/api/audit/results':
            # Latest technical-audit.json (if present)
            path = os.path.join(OUTPUT_DIR, 'technical-audit.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._respond(200, 'application/json', json.dumps(data, ensure_ascii=False).encode('utf-8', 'replace'))
                except Exception as e:
                    self._respond(500, 'application/json',
                                  json.dumps({'error': str(e)}).encode('utf-8'))
            else:
                self._respond(200, 'application/json',
                              json.dumps({'message': 'No audit run yet. Click "Run Technical Audit" to generate.'}).encode('utf-8'))

        elif self.path == '/api/audit/status':
            with PIPELINE_LOCK:
                status = dict(PIPELINE_STATE.get('audit', {'status': 'idle'}))
            self._respond(200, 'application/json', json.dumps(status).encode('utf-8'))


        else:
            self._respond(404, 'text/plain', b'Not found')

    def do_POST(self):
        try:
            self._do_POST_inner()
        except Exception as e:
            try:
                self._respond(500, 'application/json',
                              json.dumps({'success': False, 'error': str(e)}).encode('utf-8'))
            except Exception:
                pass

    def _do_POST_inner(self):
        content_length = int(self.headers.get('Content-Length', 0))

        if self.path == '/api/action':
            body = json.loads(self.rfile.read(content_length))
            slug = body.get('slug')
            act = body.get('action')
            result = self._handle_action(slug, act, body)
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/google/connect':
            # Opens the system browser for consent, blocks until done. Local app, so OK.
            try:
                import importlib, google_oauth
                importlib.reload(google_oauth)
                result = google_oauth.start_oauth_flow()
            except Exception as e:
                result = {'success': False, 'error': str(e)}
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/google/set-property':
            try:
                body = json.loads(self.rfile.read(content_length))
                import google_oauth
                result = google_oauth.set_property(body.get('property', ''))
            except Exception as e:
                result = {'success': False, 'error': str(e)}
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/setup/save':
            # Wizard saves config — receives a JSON dict of fields to write
            try:
                body = json.loads(self.rfile.read(content_length))
                result = _setup_save_config(body)
                self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))
            except Exception as e:
                self._respond(500, 'application/json',
                              json.dumps({'success': False, 'error': str(e)}).encode('utf-8'))

        elif self.path == '/api/reports/upload':
            # Save uploaded report to GA-reports/YYYY-MM-DD/<site>/
            result = self._handle_reports_upload(content_length)
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/reports/delete':
            body = json.loads(self.rfile.read(content_length))
            result = self._handle_reports_delete(body.get('date'), body.get('name'))
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/reports/audit':
            # Trigger technical_audit in a background thread
            result = self._handle_run_audit()
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/trends/run':
            # Trigger trend_analysis in a background thread
            result = self._handle_run_trends()
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/cannibal/dismiss':
            # Mark a competing pair as intentional (or un-mark). Stored with the
            # split-query count at dismissal so we can re-alarm on escalation.
            body = json.loads(self.rfile.read(content_length))
            pair = sorted(body.get('pair') or [])
            if len(pair) != 2:
                self._respond(400, 'application/json',
                              json.dumps({'error': 'need pair: [pathA, pathB]'}).encode('utf-8'))
            else:
                dpath = os.path.join(OUTPUT_DIR, 'cannibal-dismissed.json')
                dismissed = {}
                if os.path.exists(dpath):
                    try:
                        with open(dpath, 'r', encoding='utf-8') as f:
                            dismissed = json.load(f)
                    except Exception:
                        pass
                key = '|'.join(pair)
                if body.get('dismissed', True):
                    dismissed[key] = {'date': datetime.now().isoformat(),
                                      'split_queries': int(body.get('split_queries') or 0)}
                else:
                    dismissed.pop(key, None)
                with open(dpath, 'w', encoding='utf-8') as f:
                    json.dump(dismissed, f, indent=2, ensure_ascii=False)
                self._respond(200, 'application/json',
                              json.dumps({'success': True, 'total': len(dismissed)}).encode('utf-8'))

        elif self.path == '/api/cannibal/run':
            # Trigger cannibalization_audit in a background thread
            result = self._handle_run_cannibal()
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/backlinks/run':
            # Trigger backlink_audit in a background thread
            result = self._handle_run_backlinks()
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/crawlcat':
            # Multipart upload — existing CrawlyCat tab (processes immediately)
            result = self._handle_multipart_crawlcat(content_length)
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/pipeline/fetch':
            # Check if already running
            with PIPELINE_LOCK:
                any_running = PIPELINE_STATE['fetch']['status'] == 'running' or PIPELINE_STATE['analyze']['status'] == 'running'
            if any_running:
                result = {'started': False, 'error': 'A step is already running'}
            else:
                run_pipeline_step('fetch', do_fetch_step)
                result = {'started': True}
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/pipeline/analyze':
            with PIPELINE_LOCK:
                any_running = PIPELINE_STATE['fetch']['status'] == 'running' or PIPELINE_STATE['analyze']['status'] == 'running'
                fetch_done = PIPELINE_STATE['fetch']['status'] == 'done'
                report_path = PIPELINE_STATE['upload'].get('path', '')
            if any_running:
                result = {'started': False, 'error': 'A step is already running'}
            elif not fetch_done:
                result = {'started': False, 'error': 'Run Fetch first'}
            else:
                run_pipeline_step('analyze', do_analyze_step, report_path or None)
                result = {'started': True}
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/pipeline/upload-report':
            result = self._handle_pipeline_upload(content_length)
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/submit-url':
            body = json.loads(self.rfile.read(content_length))
            result = self._handle_submit_url(body.get('url', ''))
            self._respond(200, 'application/json', json.dumps(result).encode('utf-8'))

        elif self.path == '/api/url/submit-batch':
            # Bulk-submit a list of URLs to Bing.
            # Bing's SubmitUrlbatch accepts up to 500 URLs in one call.
            body = json.loads(self.rfile.read(content_length))
            urls = body.get('urls') or []
            if not urls:
                self._respond(200, 'application/json',
                              json.dumps({'success': False, 'error': 'No URLs provided'}).encode('utf-8'))
            else:
                try:
                    from submit_urls import submit_urls_batch
                    res = submit_urls_batch(urls)
                    g_ok = sum(1 for r in res.get('google', []) if r.get('success'))
                    b_ok = bool((res.get('bing') or {}).get('success'))
                    add_log('info', f'Batch-submitted {len(urls)} URLs - Google {g_ok}/{len(urls)} OK, Bing {"OK" if b_ok else "failed"}')
                    self._respond(200, 'application/json', json.dumps({
                        'success': True,
                        'count': len(urls),
                        'google_ok': g_ok,
                        'bing_ok': b_ok,
                    }).encode('utf-8'))
                except Exception as e:
                    add_log('error', f'Batch submit failed: {e}')
                    self._respond(200, 'application/json',
                                  json.dumps({'success': False, 'error': str(e)}).encode('utf-8'))


        else:
            self._respond(404, 'text/plain', b'Not found')

    def _reports_dir_for_today(self, create=True):
        """Locate (or create) GA-reports/YYYY-MM-DD/<site>/ for today.
        Site key is 'nerdy' or 'DD' based on SITE_DOMAIN. Returns None if
        no GA-reports parent exists and create=False."""
        from datetime import datetime
        # Find the GA-reports parent dir (try a few common locations)
        candidates = [
            os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'GA-reports')),
            r'C:\xampp2\htdocs\nerdy\GA-reports',
        ]
        base = next((c for c in candidates if os.path.isdir(c)), None)
        if not base and create:
            base = candidates[0]
            try:
                os.makedirs(base)
            except Exception:
                return None
        if not base:
            return None
        site_key = 'nerdy' if 'nerdy' in SITE_DOMAIN.lower() else 'DD'
        today = datetime.now().strftime('%Y-%m-%d')
        target = os.path.join(base, today, site_key)
        if create:
            try:
                os.makedirs(target, exist_ok=True)
            except Exception:
                return None
        return target if os.path.isdir(target) else None

    def _reports_base_for_site(self):
        """List ALL date folders for the current site (for the 'currently stored' listing)."""
        candidates = [
            os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'GA-reports')),
            r'C:\xampp2\htdocs\nerdy\GA-reports',
        ]
        base = next((c for c in candidates if os.path.isdir(c)), None)
        if not base:
            return None, []
        site_key = 'nerdy' if 'nerdy' in SITE_DOMAIN.lower() else 'DD'
        date_folders = []
        for entry in sorted(os.listdir(base), reverse=True):
            site_dir = os.path.join(base, entry, site_key)
            if re.match(r'\d{4}-\d{2}-\d{2}', entry) and os.path.isdir(site_dir):
                files = []
                for fn in sorted(os.listdir(site_dir)):
                    fp = os.path.join(site_dir, fn)
                    if os.path.isfile(fp):
                        files.append({
                            'name': fn,
                            'size': os.path.getsize(fp),
                            'detected_type': _detect_report_type(fn),
                        })
                date_folders.append({'date': entry, 'files': files})
        return base, date_folders

    def _extract_multipart_file(self, content_length):
        """Extract file content from a multipart/form-data upload."""
        content_type = self.headers.get('Content-Type', '')
        if 'multipart/form-data' not in content_type:
            return None, None
        boundary = content_type.split('boundary=')[1].encode()
        raw = self.rfile.read(content_length)
        parts = raw.split(b'--' + boundary)
        for part in parts:
            if b'filename=' in part:
                # Extract filename
                fname_match = re.search(rb'filename="([^"]*)"', part)
                filename = fname_match.group(1).decode() if fname_match else 'report.html'
                header_end = part.find(b'\r\n\r\n')
                if header_end != -1:
                    file_content = part[header_end + 4:]
                    if file_content.endswith(b'\r\n'):
                        file_content = file_content[:-2]
                    return file_content, filename
        return None, None

    def _handle_reports_upload(self, content_length):
        """Save one uploaded report file to GA-reports/YYYY-MM-DD/<site>/."""
        file_content, filename = self._extract_multipart_file(content_length)
        if not file_content or not filename:
            return {'success': False, 'error': 'No file in upload'}
        # Reject non-report extensions
        lower = filename.lower()
        if not (lower.endswith('.csv') or lower.endswith('.zip')):
            return {'success': False, 'error': f'Unsupported file type: {filename} (expected .csv or .zip)'}
        target_dir = self._reports_dir_for_today(create=True)
        if not target_dir:
            return {'success': False, 'error': 'Could not locate or create GA-reports folder'}
        target_path = os.path.join(target_dir, filename)
        try:
            with open(target_path, 'wb') as f:
                f.write(file_content)
        except Exception as e:
            return {'success': False, 'error': f'Write failed: {e}'}
        detected = _detect_report_type(filename)
        add_log('info', f'Report uploaded: {filename} ({len(file_content)} bytes, type={detected})')
        return {
            'success': True,
            'filename': filename,
            'size': len(file_content),
            'detected_type': detected,
            'saved_to': target_path,
        }

    def _handle_reports_delete(self, date, name):
        """Delete a specific report from a specific date folder."""
        if not date or not name:
            return {'success': False, 'error': 'Missing date or name'}
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            return {'success': False, 'error': 'Invalid date format'}
        # Safety: no path traversal in name
        if '/' in name or '\\' in name or '..' in name:
            return {'success': False, 'error': 'Invalid filename'}
        base, _ = self._reports_base_for_site()
        if not base:
            return {'success': False, 'error': 'No GA-reports dir'}
        site_key = 'nerdy' if 'nerdy' in SITE_DOMAIN.lower() else 'DD'
        target_path = os.path.join(base, date, site_key, name)
        if not os.path.isfile(target_path):
            return {'success': False, 'error': 'File not found'}
        try:
            os.unlink(target_path)
            add_log('info', f'Report deleted: {date}/{site_key}/{name}')
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _handle_url_remove(self, single_url=None, urls_list=None):
        """Request removal from Bing for one URL or many."""
        urls = [single_url] if single_url else (urls_list or [])
        urls = [u.strip() for u in urls if u and u.strip()]
        if not urls:
            return {'success': False, 'error': 'No URL provided'}
        try:
            from submit_urls import remove_url, remove_urls_batch
            if len(urls) == 1:
                r = remove_url(urls[0])
                g_ok = bool(r['google'] and r['google'].get('success'))
                b_ok = bool(r['bing'] and r['bing'].get('success'))
                g_msg = 'OK' if g_ok else (r['google'] or {}).get('error', 'failed')
                b_msg = 'OK' if b_ok else (r['bing'] or {}).get('error', 'failed')
                add_log('info' if (g_ok or b_ok) else 'warn',
                        f'Removal requested: {urls[0]} — Google: {g_msg}, Bing: {b_msg}')
                return {'success': True,
                        'url': urls[0],
                        'google_ok': g_ok, 'bing_ok': b_ok,
                        'message': f'Google: {g_msg} · Bing: {b_msg}'}
            else:
                batch = remove_urls_batch(urls)
                throttled = batch.get('bing_throttled', 0)
                throttle_note = f' (Bing rate-limited on {throttled}; wait 5-10 min and retry)' if throttled else ''
                add_log('info', f'Bulk removal: {batch["count"]} URLs — '
                                f'Google {batch["google_ok"]}/{batch["count"]} OK, '
                                f'Bing {batch["bing_ok"]}/{batch["count"]} OK{throttle_note}')
                return {'success': True,
                        'count': batch['count'],
                        'google_ok': batch['google_ok'],
                        'bing_ok': batch['bing_ok'],
                        'bing_throttled': throttled,
                        'message': (f'{batch["count"]} URLs · Google {batch["google_ok"]}/{batch["count"]} OK · '
                                    f'Bing {batch["bing_ok"]}/{batch["count"]} OK{throttle_note}')}
        except Exception as e:
            add_log('error', f'Removal failed: {e}')
            return {'success': False, 'error': str(e)}


    def _handle_run_audit(self):
        """Run technical_audit.run_audit() in a background thread."""
        with PIPELINE_LOCK:
            current = PIPELINE_STATE.get('audit', {})
            if current.get('status') == 'running':
                return {'success': False, 'error': 'Audit already running', 'status': 'running'}
            PIPELINE_STATE['audit'] = {'status': 'running', 'started_at': datetime.now().isoformat()}

        def _run():
            try:
                # Defensive: force-reload so we always run the latest version
                # (avoids running stale module if the dashboard wasn't restarted)
                import importlib, technical_audit
                importlib.reload(technical_audit)
                technical_audit.run_audit()
                with PIPELINE_LOCK:
                    PIPELINE_STATE['audit'] = {'status': 'done', 'finished_at': datetime.now().isoformat()}
                add_log('success', 'Technical audit completed')
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                # Persist full traceback to disk so we can see exactly where it failed
                err_path = os.path.join(OUTPUT_DIR, 'technical-audit-error.txt')
                try:
                    with open(err_path, 'w', encoding='utf-8') as f:
                        f.write(f"Audit failed at {datetime.now().isoformat()}\n\n{tb}")
                except Exception:
                    err_path = None
                # Also note where the audit's own line-by-line run log is
                run_log = os.path.join(OUTPUT_DIR, 'technical-audit-run.log')
                with PIPELINE_LOCK:
                    PIPELINE_STATE['audit'] = {
                        'status': 'error',
                        'error': str(e),
                        'error_log': err_path,
                        'run_log': run_log if os.path.exists(run_log) else None,
                        'traceback': tb,
                        'finished_at': datetime.now().isoformat(),
                    }
                # Log lines from traceback so they appear in dashboard Logs tab
                for line in tb.splitlines():
                    if line.strip():
                        add_log('error', line.rstrip())
                add_log('error', f'TRACE FILE: {err_path}')
                if run_log and os.path.exists(run_log):
                    add_log('error', f'RUN LOG: {run_log}  (full audit output before crash)')

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return {'success': True, 'status': 'running'}

    def _handle_run_trends(self):
        """Run trend_analysis.run_trend_analysis() in a background thread."""
        with PIPELINE_LOCK:
            current = PIPELINE_STATE.get('trends', {})
            if current.get('status') == 'running':
                return {'success': False, 'error': 'Trend analysis already running', 'status': 'running'}
            PIPELINE_STATE['trends'] = {'status': 'running', 'started_at': datetime.now().isoformat()}

        def _run():
            try:
                import importlib, trend_analysis
                importlib.reload(trend_analysis)
                trend_analysis.run_trend_analysis()
                with PIPELINE_LOCK:
                    PIPELINE_STATE['trends'] = {'status': 'done', 'finished_at': datetime.now().isoformat()}
                add_log('success', 'Trend analysis completed')
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                err_path = os.path.join(OUTPUT_DIR, 'trend-analysis-error.txt')
                try:
                    with open(err_path, 'w', encoding='utf-8') as f:
                        f.write(f'Trend analysis failed at {datetime.now().isoformat()}\n\n{tb}')
                except Exception:
                    err_path = None
                with PIPELINE_LOCK:
                    PIPELINE_STATE['trends'] = {
                        'status': 'error', 'error': str(e), 'error_log': err_path,
                        'finished_at': datetime.now().isoformat(),
                    }
                for line in tb.splitlines():
                    if line.strip():
                        add_log('error', line.rstrip())
                add_log('error', f'TRACE FILE: {err_path}')

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return {'success': True, 'status': 'running'}

    def _handle_run_cannibal(self):
        """Run cannibalization_audit.run_audit() in a background thread."""
        with PIPELINE_LOCK:
            current = PIPELINE_STATE.get('cannibal', {})
            if current.get('status') == 'running':
                return {'success': False, 'error': 'Cannibalization audit already running', 'status': 'running'}
            PIPELINE_STATE['cannibal'] = {'status': 'running', 'started_at': datetime.now().isoformat()}

        def _run():
            try:
                import importlib, cannibalization_audit
                importlib.reload(cannibalization_audit)
                cannibalization_audit.run_audit()
                with PIPELINE_LOCK:
                    PIPELINE_STATE['cannibal'] = {'status': 'done', 'finished_at': datetime.now().isoformat()}
                add_log('success', 'Cannibalization audit completed')
            except Exception as e:
                with PIPELINE_LOCK:
                    PIPELINE_STATE['cannibal'] = {'status': 'error', 'error': str(e),
                                                  'finished_at': datetime.now().isoformat()}
                add_log('error', f'Cannibalization audit failed: {e}')

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return {'success': True, 'status': 'running'}

    def _handle_run_backlinks(self):
        """Run backlink_audit.run_audit() in a background thread."""
        with PIPELINE_LOCK:
            current = PIPELINE_STATE.get('backlinks', {})
            if current.get('status') == 'running':
                return {'success': False, 'error': 'Backlink audit already running', 'status': 'running'}
            PIPELINE_STATE['backlinks'] = {'status': 'running', 'started_at': datetime.now().isoformat()}

        def _run():
            try:
                import importlib, backlink_audit
                importlib.reload(backlink_audit)
                backlink_audit.run_audit()
                with PIPELINE_LOCK:
                    PIPELINE_STATE['backlinks'] = {'status': 'done', 'finished_at': datetime.now().isoformat()}
                add_log('success', 'Backlink audit completed')
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                err_path = os.path.join(OUTPUT_DIR, 'backlink-audit-error.txt')
                try:
                    with open(err_path, 'w', encoding='utf-8') as f:
                        f.write(f'Backlink audit failed at {datetime.now().isoformat()}\n\n{tb}')
                except Exception:
                    err_path = None
                with PIPELINE_LOCK:
                    PIPELINE_STATE['backlinks'] = {'status': 'error', 'error': str(e),
                                                   'error_log': err_path,
                                                   'finished_at': datetime.now().isoformat()}
                for line in tb.splitlines():
                    if line.strip():
                        add_log('error', line.rstrip())

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return {'success': True, 'status': 'running'}

    def _handle_multipart_crawlcat(self, content_length):
        """Handle the existing CrawlyCat tab upload (immediate processing)."""
        file_content, filename = self._extract_multipart_file(content_length)
        if not file_content:
            return {'success': False, 'error': 'No file found in upload'}

        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.html', mode='wb')
        tmp.write(file_content)
        tmp.close()
        add_log('info', f'Received CrawlyCat report ({len(file_content)} bytes)')

        def process():
            process_crawlycat_report(tmp.name)
            os.unlink(tmp.name)

        t = threading.Thread(target=process, daemon=True)
        t.start()
        t.join(timeout=300)
        return {'success': True, 'count': len(STATE.get('fixes', {}))}

    def _handle_pipeline_upload(self, content_length):
        """Handle Pipeline tab upload — save file for later processing in Step 3."""
        file_content, filename = self._extract_multipart_file(content_length)
        if not file_content:
            return {'success': False, 'error': 'No file found in upload'}

        save_path = os.path.join(OUTPUT_DIR, 'crawlycat-report.html')
        with open(save_path, 'wb') as f:
            f.write(file_content)

        with PIPELINE_LOCK:
            PIPELINE_STATE['upload']['status'] = 'done'
            PIPELINE_STATE['upload']['filename'] = filename
            PIPELINE_STATE['upload']['path'] = save_path

        add_log('info', f'CrawlyCat report saved: {filename} ({len(file_content)} bytes)')
        return {'success': True, 'filename': filename}

    def _handle_submit_url(self, url_or_slug):
        """Submit a URL to Bing for re-indexing."""
        from submit_urls import submit_url
        url = url_or_slug.strip()
        if not url:
            return {'url': url, 'google_ok': False, 'google_msg': 'Empty URL',
                    'bing_ok': False, 'bing_msg': 'Empty URL'}
        if not url.startswith('http'):
            url = f"{SITE_DOMAIN}/{url.strip('/')}/"
        try:
            result = submit_url(url)
            g_ok = result['google']['success']
            g_msg = 'OK' if g_ok else result['google'].get('error', 'failed')
            b_ok = result['bing']['success']
            b_msg = 'OK' if b_ok else result['bing'].get('error', 'failed')
            add_log('info', f'URL submitted: {url} — Google: {g_msg}, Bing: {b_msg}')
            return {'url': url, 'google_ok': g_ok, 'google_msg': g_msg,
                    'bing_ok': b_ok, 'bing_msg': b_msg}
        except Exception as e:
            add_log('error', f'URL submission failed: {url} — {e}')
            return {'url': url, 'google_ok': False, 'google_msg': str(e),
                    'bing_ok': False, 'bing_msg': str(e)}


    def _respond(self, code, content_type, body, extra_headers=None):
        self.send_response(code)
        self.send_header('Content-Type', f'{content_type}; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _handle_action(self, slug, act, body=None):
        body = body or {}
        # Bulk actions don't operate on a single slug — handle them before the per-slug lookup.
        if act == 'submit_all_approved':
            try:
                from submit_urls import submit_urls_batch
                approved_slugs = [s for s, f in STATE['fixes'].items() if f.get('status') == 'approved']
                if not approved_slugs:
                    return {'success': False, 'error': 'No approved URLs in current view'}
                urls = [f"{SITE_DOMAIN}/{s}/" for s in approved_slugs]
                res = submit_urls_batch(urls)
                g_results = res.get('google', [])
                g_ok = sum(1 for r in g_results if r.get('success'))
                b_ok = bool((res.get('bing') or {}).get('success'))
                add_log('info', f'Bulk submitted {len(urls)} URLs — Google: {g_ok}/{len(urls)} OK, Bing: {"OK" if b_ok else "failed"}')
                return {'success': True,
                        'message': f'{len(urls)} URLs · Google {g_ok}/{len(urls)} OK · Bing {"OK" if b_ok else "fail"}',
                        'count': len(urls), 'google_ok': g_ok, 'bing_ok': b_ok}
            except Exception as e:
                add_log('error', f'Bulk submit failed: {e}')
                return {'success': False, 'error': str(e)}

        if slug not in STATE['fixes']:
            return {'success': False, 'error': 'Fix not found'}

        if act == 'reject':
            STATE['fixes'][slug]['status'] = 'rejected'
            add_log('warn', f'Rejected: {slug}')
            save_state()
            return {'success': True}


        if act == 'mark_applied':
            # Free-tier: user applied the fix manually in WordPress / Yoast and is
            # marking it as done here. We record the fix history (for the feedback
            # loop + cooldown) but never touch WP.
            fix = STATE['fixes'][slug]
            fix['status'] = 'approved'
            now_iso = datetime.now().isoformat()
            STATE.setdefault('applied_dates', {})[slug] = now_iso
            kws = fix.get('keywords', [])
            total_impr = sum(k.get('impressions', 0) for k in kws)
            total_clicks = sum(k.get('clicks', 0) for k in kws)
            page_ctr = round(total_clicks / total_impr * 100, 2) if total_impr > 0 else 0
            avg_pos = round(sum(k.get('position', 0) * k.get('impressions', 1)
                                for k in kws) / max(total_impr, 1), 1) if kws else None
            add_log('success', f'Marked applied: {slug} (manual)')
            save_state()
            return {'success': True}

        if act == 'submit':
            # Submit a single URL to Bing for re-indexing.
            # Works regardless of --live (the Bing submission API is always live).
            try:
                from submit_urls import submit_url
                page_url = f"{SITE_DOMAIN}/{slug}/"
                sub = submit_url(page_url)
                g_ok = sub['google'].get('success', False)
                b_ok = sub['bing'].get('success', False)
                g_msg = 'OK' if g_ok else (sub['google'].get('error', 'failed'))
                b_msg = 'OK' if b_ok else (sub['bing'].get('error', 'failed'))
                add_log('info' if (g_ok or b_ok) else 'warn',
                        f'Submitted: {slug} — Google: {g_msg}, Bing: {b_msg}')
                return {'success': True,
                        'message': f'Google: {g_msg} · Bing: {b_msg}',
                        'google_ok': g_ok, 'bing_ok': b_ok}
            except Exception as e:
                add_log('error', f'Submit failed for {slug}: {e}')
                return {'success': False, 'error': str(e)}

        return {'success': False, 'error': 'Unknown action'}


# ─── Main ───────────────────────────────────────────────────────────────────
def main():
    live = '--live' in sys.argv
    port = 8090
    for i, arg in enumerate(sys.argv):
        if arg == '--port' and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    STATE['fixes'] = load_all_fixes()
    STATE['live'] = live

    # Restore saved statuses from previous session
    saved_statuses, saved_logs, saved_dates = load_state()
    STATE['applied_dates'] = saved_dates
    restored = 0
    for slug, status in saved_statuses.items():
        if slug in STATE['fixes'] and status in ('approved', 'rejected'):
            STATE['fixes'][slug]['status'] = status
            restored += 1
    if restored:
        add_log('info', f'Restored {restored} statuses from previous session')
    if saved_logs:
        STATE['logs'] = saved_logs + STATE['logs']

    # Detect which pipeline steps have already been completed
    detect_pipeline_state()

    if not STATE['fixes']:
        print("[INFO] No fixes found yet. Use the Pipeline tab to run the full pipeline.")
        print("       Starting dashboard...\n")

    # Free tier: no WordPress integration — dashboard is view + manual-apply only.
    STATE['session'] = None
    STATE['api_url'] = None



    target = 'LIVE' if live else 'LOCAL'
    total = len(STATE['fixes'])
    add_log('info', f'Dashboard started — {total} changes, target: {target}')

    print(f"\n{'=' * 50}", flush=True)
    print(f"  KittyRank Dashboard", flush=True)
    print(f"  {SITE_NAME} — {target}", flush=True)
    print(f"  {total} changes loaded", flush=True)
    print(f"{'=' * 50}", flush=True)
    url = f"http://localhost:{port}"
    print(f"\n  {url}", flush=True)
    print(f"\n  Press Ctrl+C to stop.\n", flush=True)

    server = ThreadingHTTPServer(('localhost', port), ReviewHandler)

    # Auto-open browser in a thread so it doesn't block
    import webbrowser
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    # Ctrl+C shutdown — use signal handler for reliable shutdown on all platforms
    _shutdown = False

    def _signal_handler(sig, frame):
        nonlocal _shutdown
        _shutdown = True

    signal.signal(signal.SIGINT, _signal_handler)

    server.timeout = 0.5
    while not _shutdown:
        server.handle_request()

    print("\nShutting down.")
    server.server_close()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        # Write to file in case stdout is broken
        err_path = os.path.join(os.path.dirname(__file__), 'review-error.log')
        with open(err_path, 'w') as f:
            f.write(err)
        try:
            print(err)
        except Exception:
            pass
        sys.exit(1)
