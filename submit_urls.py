"""
Submit URLs to Bing for re-indexing after SEO changes.

Google: removed — the Indexing API is ToS-restricted to JobPosting/BroadcastEvent;
Bing:   Uses the URL Submission API.

Usage:
  from submit_urls import submit_url, submit_urls_batch

  # Single URL
  results = submit_url('https://example.com/my-post/')

  # Batch
  results = submit_urls_batch(['https://example.com/a/', 'https://example.com/b/'])
"""

import sys
import os
import json
import requests

sys.path.insert(0, os.path.dirname(__file__))

from config import (
    GSC_CREDENTIALS, SITE_DOMAIN,
    BING_API_KEY, BING_SITE_URL,
)


# ─── Google Indexing API — DISABLED (Bing-only; see module docstring) ────────

def _get_google_credentials():
    """Google credentials with indexing scope — OAuth token, else service account.
    Returns None in release builds where the indexing scope is not requested."""
    try:
        import google_oauth
        if 'indexing' not in getattr(google_oauth, 'SCOPES', {}):
            return None
        return None  # Google Indexing API removed — against Google ToS for general URLs. Bing-only; Google recrawl via sitemap + GSC 'Request Indexing'.
    except Exception as e:
        print(f"  [Google] Auth error: {e}", flush=True)
        return None


def submit_to_google(url):
    """DISABLED — Google Indexing API removed (Bing-only). Returns a skip result."""
    credentials = _get_google_credentials()
    if not credentials:
        return {'success': False, 'skipped': True, 'error': 'Google Indexing disabled in this build — use sitemap resubmission / GSC URL Inspection'}

    try:
        from google.auth.transport.requests import AuthorizedSession
        session = AuthorizedSession(credentials)
        endpoint = 'https://indexing.googleapis.com/v3/urlNotifications:publish'
        body = {'url': url, 'type': 'URL_UPDATED'}
        resp = session.post(endpoint, json=body)

        if resp.status_code == 200:
            return {'success': True, 'response': resp.json()}
        else:
            return {'success': False, 'error': f'{resp.status_code}: {resp.text}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def submit_to_google_batch(urls):
    """DISABLED — Google Indexing API removed (Bing-only). Returns skip results."""
    credentials = _get_google_credentials()
    if not credentials:
        return [{'url': u, 'success': False, 'skipped': True, 'error': 'Google Indexing disabled in this build — use sitemap resubmission / GSC URL Inspection'} for u in urls]

    try:
        from google.auth.transport.requests import AuthorizedSession
        session = AuthorizedSession(credentials)
        endpoint = 'https://indexing.googleapis.com/v3/urlNotifications:publish'
        results = []
        for url in urls:
            body = {'url': url, 'type': 'URL_UPDATED'}
            resp = session.post(endpoint, json=body)
            if resp.status_code == 200:
                results.append({'url': url, 'success': True})
            else:
                results.append({'url': url, 'success': False, 'error': f'{resp.status_code}: {resp.text}'})
        return results
    except Exception as e:
        return [{'url': u, 'success': False, 'error': str(e)} for u in urls]


# ─── Google URL REMOVAL (Indexing API URL_DELETED) — DISABLED ───────────────

def remove_from_google(url):
    """DISABLED — Google Indexing API removed (Bing-only). Returns a skip result.
    Same endpoint as submit, different 'type' value. Triggers Google to
    re-crawl and confirm the URL is gone, then drop it from the index.
    Honest caveat: Indexing API is officially documented for JobPosting +
    BroadcastEvent schemas; for other URL types Google may discount the hint
    or process it slowly. Real removal still requires the URL to return 404."""
    credentials = _get_google_credentials()
    if not credentials:
        return {'success': False, 'skipped': True, 'error': 'Google Indexing disabled in this build — use sitemap resubmission / GSC URL Inspection'}
    try:
        from google.auth.transport.requests import AuthorizedSession
        session = AuthorizedSession(credentials)
        endpoint = 'https://indexing.googleapis.com/v3/urlNotifications:publish'
        body = {'url': url, 'type': 'URL_DELETED'}
        resp = session.post(endpoint, json=body)
        if resp.status_code == 200:
            return {'success': True, 'response': resp.json()}
        return {'success': False, 'error': f'{resp.status_code}: {resp.text}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ─── Bing URL Submission API ────────────────────────────────────────────────

BING_SUBMIT_URL = 'https://ssl.bing.com/webmaster/api.svc/json/SubmitUrlbatch'
BING_BLOCK_URL = 'https://ssl.bing.com/webmaster/api.svc/json/AddBlockedUrl'


def submit_to_bing(urls):
    """Submit URLs to Bing Webmaster URL Submission API (batch, max 500)."""
    if not BING_API_KEY:
        return {'success': False, 'error': 'No Bing API key configured'}

    if isinstance(urls, str):
        urls = [urls]

    try:
        resp = requests.post(
            BING_SUBMIT_URL,
            params={'apikey': BING_API_KEY},
            json={'siteUrl': BING_SITE_URL, 'urlList': urls},
            headers={'Content-Type': 'application/json'},
        )
        if resp.status_code == 200:
            return {'success': True, 'count': len(urls)}
        else:
            return {'success': False, 'error': f'{resp.status_code}: {resp.text}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def remove_from_bing(url):
    """Submit a URL to Bing's URL-block tool (AddBlockedUrl endpoint).
    Bing's WCF API needs camelCase nested object, AND siteUrl WITHOUT
    trailing slash (its URL validator is strict). The blocked URL must
    start with the normalized siteUrl prefix.

    Errors observed:
      ErrorCode 5  = ThrottleHost  (rate-limited; retry later)
      ErrorCode 7  = InvalidUrl    (URL doesn't match siteUrl prefix, OR siteUrl
                                    has trailing slash, OR URL malformed)
      ErrorCode 8  = InvalidParameter (payload shape wrong)

    Temporary suppression (~6 months); permanent removal requires a real 404."""
    if not BING_API_KEY:
        return {'success': False, 'error': 'No Bing API key configured'}

    # Normalize siteUrl: strip trailing slash (Bing's validator rejects otherwise)
    site = BING_SITE_URL.rstrip('/')
    # URL must start with the normalized siteUrl — otherwise Bing returns InvalidUrl
    if not url.startswith(site):
        return {'success': False,
                'error': f'URL must start with siteUrl ({site}). Got: {url}'}

    payload = {
        'siteUrl': site,
        'blockedUrl': {
            'url': url,
            'entityType': 'Page',       # alt: 'Directory' to block a subtree
            'requestType': 'BlockUrl',  # alt: 'CacheOnly' to only block cached copy
            'date': None,
            'days': 0,
        },
    }
    try:
        resp = requests.post(
            BING_BLOCK_URL,
            params={'apikey': BING_API_KEY},
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=15,
        )
        if resp.status_code == 200:
            return {'success': True}
        # Parse Bing's error code for a more useful message
        body = resp.text or ''
        if 'ThrottleHost' in body or '"ErrorCode":5' in body:
            return {'success': False,
                    'error': 'Bing API rate-limited (ThrottleHost). Wait a few minutes and retry.',
                    'retryable': True}
        if 'InvalidUrl' in body or '"ErrorCode":7' in body:
            return {'success': False,
                    'error': f'Bing rejected URL as invalid: {url}. Verify it exists in Bing index.'}
        return {'success': False, 'error': f'{resp.status_code}: {body[:300]}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ─── Combined submission ────────────────────────────────────────────────────

def submit_url(url):
    """Submit a single URL to Bing (Google removed)."""
    results = {'url': url, 'google': None, 'bing': None}

    print(f"  Submitting to Google: {url}", flush=True)
    results['google'] = submit_to_google(url)
    status = 'OK' if results['google']['success'] else results['google'].get('error', 'failed')
    print(f"    Google: {status}", flush=True)

    print(f"  Submitting to Bing: {url}", flush=True)
    results['bing'] = submit_to_bing(url)
    status = 'OK' if results['bing']['success'] else results['bing'].get('error', 'failed')
    print(f"    Bing: {status}", flush=True)

    return results


def remove_url(url):
    """Request removal of a single URL from Bing (Google removed).
    Use for confirmed 404s — the removal flag is interpreted as a TEMPORARY
    suppression (~6 months on Google, similar on Bing) during which the
    search engine confirms the 404 status. After the suppression period the
    URL reappears UNLESS it still 404s — so the real removal mechanism is
    the 404 response itself; this just speeds up the hiding."""
    results = {'url': url, 'google': None, 'bing': None}
    print(f"  Removing from Google: {url}", flush=True)
    results['google'] = remove_from_google(url)
    status = 'OK' if results['google']['success'] else results['google'].get('error', 'failed')
    print(f"    Google: {status}", flush=True)

    print(f"  Removing from Bing: {url}", flush=True)
    results['bing'] = remove_from_bing(url)
    status = 'OK' if results['bing']['success'] else results['bing'].get('error', 'failed')
    print(f"    Bing: {status}", flush=True)

    return results


def remove_urls_batch(urls, bing_delay_sec=2.0):
    """Request removal of multiple URLs. Sequential per API rate limits.

    Bing's AddBlockedUrl endpoint throttles aggressively (ErrorCode 5 / ThrottleHost)
    when called rapidly — we add a delay between Bing calls and skip-with-clear-error
    rather than retrying (the dashboard surfaces ThrottleHost so the user can retry later).
    Google's Indexing API has a higher per-second quota but a 200/day total."""
    import time
    if not urls:
        return {'count': 0, 'results': [], 'google_ok': 0, 'bing_ok': 0, 'bing_throttled': 0}
    results = []
    bing_throttled = 0
    for i, u in enumerate(urls):
        if i > 0:
            time.sleep(bing_delay_sec)   # space out Bing calls
        r = remove_url(u)
        if r.get('bing') and 'ThrottleHost' in str(r['bing'].get('error', '')):
            bing_throttled += 1
        results.append(r)
    g_ok = sum(1 for r in results if r['google'] and r['google'].get('success'))
    b_ok = sum(1 for r in results if r['bing'] and r['bing'].get('success'))
    print(f"  [REMOVE BATCH] Google: {g_ok}/{len(urls)} OK   Bing: {b_ok}/{len(urls)} OK"
          + (f" (Bing throttled on {bing_throttled})" if bing_throttled else ""), flush=True)
    return {'count': len(urls), 'results': results,
            'google_ok': g_ok, 'bing_ok': b_ok, 'bing_throttled': bing_throttled}


def submit_urls_batch(urls):
    """Submit multiple URLs to Bing (Google removed)."""
    if not urls:
        return {'google': [], 'bing': None}

    results = {'google': [], 'bing': None}

    # Google: one at a time (API limitation)
    print(f"\n  [Google] Submitting {len(urls)} URLs...", flush=True)
    results['google'] = submit_to_google_batch(urls)
    ok = sum(1 for r in results['google'] if r['success'])
    print(f"  [Google] {ok}/{len(urls)} submitted successfully", flush=True)

    # Bing: batch (up to 500 per call)
    print(f"  [Bing] Submitting {len(urls)} URLs...", flush=True)
    for i in range(0, len(urls), 500):
        batch = urls[i:i+500]
        results['bing'] = submit_to_bing(batch)
    status = 'OK' if results['bing']['success'] else results['bing'].get('error', 'failed')
    print(f"  [Bing] {status}", flush=True)

    return results


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        print("Usage:")
        print("  python submit_urls.py <url>              # Submit single URL")
        print("  python submit_urls.py --applied           # Submit all approved URLs from review state")
        print("  python submit_urls.py --from-fixes        # Submit all URLs from proposed-fixes.json")
        sys.exit(0)

    if args[0] == '--applied':
        state_path = os.path.join(os.path.dirname(__file__), 'output', 'review-state.json')
        if not os.path.exists(state_path):
            print("No review-state.json found")
            sys.exit(1)
        with open(state_path) as f:
            state = json.load(f)
        urls = []
        for slug, status in state.get('statuses', {}).items():
            if status == 'approved':
                urls.append(f"{SITE_DOMAIN}/{slug}/")
        if not urls:
            print("No approved URLs found")
            sys.exit(0)
        print(f"Submitting {len(urls)} approved URLs for re-indexing...\n")
        submit_urls_batch(urls)

    elif args[0] == '--from-fixes':
        fixes_path = os.path.join(os.path.dirname(__file__), 'output', 'proposed-fixes.json')
        if not os.path.exists(fixes_path):
            print("No proposed-fixes.json found")
            sys.exit(1)
        with open(fixes_path) as f:
            fixes = json.load(f)
        urls = [f"{SITE_DOMAIN}/{fix['slug']}/" for fix in fixes]
        print(f"Submitting {len(urls)} URLs for re-indexing...\n")
        submit_urls_batch(urls)

    else:
        url = args[0]
        if not url.startswith('http'):
            url = f"{SITE_DOMAIN}/{url}/"
        submit_url(url)
