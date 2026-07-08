"""
Crawl Fix: Parse CrawlyCat HTML report and fix SEO issues via WordPress REST API.

Handles:
  - meta_description_missing   → Claude generates description
  - meta_description_length    → Claude shortens (too long) or rewrites (too short)
  - meta_title_length          → Claude shortens/rewrites title
  - h1_multiple                → Report only (needs manual fix)
  - internal_broken_link       → Report only (needs manual fix)

Usage:
  python crawl_fix.py report.html                          # Dry run
  python crawl_fix.py report.html --execute                # Apply to local
  python crawl_fix.py report.html --execute --live         # Apply to live
  python crawl_fix.py report.html --claude                 # Use Claude to generate/fix
  python crawl_fix.py report.html --claude --execute       # Generate + apply locally
  python crawl_fix.py report.html --claude --generate      # Generate fixes only (no WP needed)
"""

import io
import json
import os
import re
import sys
import time
import requests
import urllib3
from bs4 import BeautifulSoup

# Reconfigure in place — do NOT wrap sys.stdout.buffer in a new TextIOWrapper:
# the throwaway wrapper closes the underlying buffer when garbage-collected,
# which breaks every later print() in a long-running parent process.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))
from config import *
import seo_quality

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAX_DESC_LENGTH = 155
MIN_DESC_LENGTH = 50
MAX_TOTAL_TITLE = 65                           # Google's display limit
TITLE_SUFFIX_LEN = len(TITLE_SUFFIX)           # e.g. len(' - NerdyElectronics') = 19
MAX_TITLE_LENGTH = MAX_TOTAL_TITLE - TITLE_SUFFIX_LEN  # Budget for title part only
MIN_TITLE_LENGTH = 15

# Pages we can't fix via post/page REST API (taxonomies, pagination, archives)
SKIP_PATTERNS = [
    '/category/', '/tag/', '/blog/', '/page/', '/author/',
    '/data-subject-access-request-form/',
    '/articles/',       # redirected old URLs
    '/tutorials/',      # redirected old URLs
    '/atmega/',         # redirected old URLs
]


def parse_report(report_path):
    """Parse CrawlyCat HTML report and extract all SEO issues.

    Returns dict with keys:
      meta_desc_missing, meta_desc_length, meta_title_length,
      h1_multiple, broken_links
    """
    with open(report_path, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

    seo_panel = soup.find('div', id='tab-seo')
    if not seo_panel:
        print("[ERROR] No SEO issues panel found in report")
        return {'meta_desc_missing': [], 'meta_desc_length': [], 'meta_title_length': [],
                'h1_multiple': [], 'broken_links': []}

    rows = seo_panel.find_all('tr')
    results = {
        'meta_desc_missing': [],
        'meta_desc_length': [],
        'meta_title_length': [],
        'h1_multiple': [],
        'broken_links': [],
        'img_alt_missing': [],
    }

    for row in rows:
        cells = row.find_all('td')
        if len(cells) < 4:
            continue

        url = cells[0].get_text(strip=True)
        issue = cells[1].get_text(strip=True)
        details = cells[3].get_text(strip=True)

        # Extract slug from URL
        slug = url.replace(SITE_DOMAIN + '/', '').replace(SITE_DOMAIN, '').strip('/')
        if not slug:
            continue

        # Skip non-post pages
        if any(pat.strip('/') in f'/{slug}/' for pat in SKIP_PATTERNS):
            continue

        length_match = re.search(r'(?:length|Found)\s+(\d+)', details)
        current_len = int(length_match.group(1)) if length_match else 0

        if issue == 'meta_description_missing':
            results['meta_desc_missing'].append({
                'url': url, 'slug': slug, 'issue': 'desc_missing',
            })
        elif issue == 'meta_description_length':
            issue_type = 'desc_too_short' if current_len < MIN_DESC_LENGTH else 'desc_too_long'
            results['meta_desc_length'].append({
                'url': url, 'slug': slug, 'issue': issue_type,
                'current_length': current_len,
            })
        elif issue == 'meta_title_length':
            issue_type = 'title_too_short' if current_len < MIN_TITLE_LENGTH else 'title_too_long'
            results['meta_title_length'].append({
                'url': url, 'slug': slug, 'issue': issue_type,
                'current_length': current_len,
            })
        elif issue == 'h1_multiple':
            results['h1_multiple'].append({
                'url': url, 'slug': slug, 'issue': 'h1_multiple',
                'count': current_len,
            })
        elif issue == 'internal_broken_link':
            broken_url_match = re.search(r'Links to broken URL\s+(https?://\S+)', details)
            broken_url = broken_url_match.group(1) if broken_url_match else details
            results['broken_links'].append({
                'url': url, 'slug': slug, 'issue': 'broken_link',
                'broken_url': broken_url,
            })
        elif issue == 'img_alt_missing':
            # Extract real image URLs from details — skip data: URI lazy-load placeholders
            img_urls = [u.strip() for u in details.split(',') if u.strip()]
            real_missing = [u for u in img_urls if not u.startswith('data:')]
            if real_missing:
                results.setdefault('img_alt_missing', []).append({
                    'url': url, 'slug': slug, 'issue': 'img_alt_missing',
                    'images': real_missing,
                })

    return results


def get_wp_session(live=False):
    """Get authenticated WordPress REST API session.
    Free-tier note: WP write requires Premium. This function returns (None, None)
    when WP credentials are absent — callers should handle that and skip the
    --execute path (use --generate to write fix proposals to JSON without applying)."""
    user = globals().get('WP_LIVE_USER' if live else 'WP_LOCAL_USER', '')
    pw   = globals().get('WP_LIVE_PASS' if live else 'WP_LOCAL_PASS', '')
    url  = globals().get('WP_LIVE_URL'  if live else 'WP_LOCAL_URL',  '')
    if not (user and pw and url):
        print("[INFO] WP write disabled (Premium feature). Use --generate to write fix proposals to JSON.")
        return None, None
    session = requests.Session()
    session.auth = (user, pw)
    session.verify = False
    session.headers.update({'Content-Type': 'application/json'})
    api_url = f"{url}/wp-json/wp/v2"
    return session, api_url


def fetch_post_data(session, api_url, slug):
    """Fetch post by slug with edit context (to get Yoast meta).
    Returns (post_data, post_type) where post_type is 'posts' or 'pages'."""
    resp = session.get(f"{api_url}/posts", params={'slug': slug, 'context': 'edit'})
    resp.raise_for_status()
    posts = resp.json()
    if posts:
        return posts[0], 'posts'
    # Try pages
    resp = session.get(f"{api_url}/pages", params={'slug': slug, 'context': 'edit'})
    resp.raise_for_status()
    posts = resp.json()
    if posts:
        return posts[0], 'pages'
    return None, None


def validate_issues_live(items):
    """Check each issue against live page state. Remove already-fixed issues."""
    validated = []
    skipped = 0
    for item in items:
        page_info = scrape_page_content(item['url'])
        if not page_info:
            print(f"  SKIP {item['slug']}: could not scrape")
            continue

        item['page_info'] = page_info
        issue = item['issue']

        if issue in ('desc_missing',):
            current = page_info.get('meta_description', '')
            if current and len(current) >= MIN_DESC_LENGTH:
                print(f"  FIXED {item['slug']}: description now exists ({len(current)}c)")
                skipped += 1
                continue
            item['current_desc'] = current

        elif issue in ('desc_too_long', 'desc_too_short'):
            current = page_info.get('meta_description', '')
            if MIN_DESC_LENGTH <= len(current) <= MAX_DESC_LENGTH:
                print(f"  FIXED {item['slug']}: description length OK now ({len(current)}c)")
                skipped += 1
                continue
            item['current_desc'] = current

        elif issue in ('title_too_long', 'title_too_short'):
            current = page_info.get('title', '')
            # Strip site name suffix (e.g. " - NerdyElectronics" or " | SiteName")
            for sep in [' - ', ' | ', ' — ']:
                if sep in current:
                    current = current[:current.rfind(sep)]
            if MIN_TITLE_LENGTH <= len(current) <= MAX_TITLE_LENGTH:
                print(f"  FIXED {item['slug']}: title length OK now ({len(current)}c)")
                skipped += 1
                continue
            item['current_title'] = current

        validated.append(item)

    if skipped:
        print(f"  Skipped {skipped} already-fixed issues")
    return validated


def scrape_page_content(url):
    """Scrape live page to get title, H1, first paragraphs for description generation."""
    try:
        resp = requests.get(url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; NerdySEOBot/1.0)'
        })
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        title = soup.find('title')
        title_text = title.get_text(strip=True) if title else ''

        h1 = soup.find('h1')
        h1_text = h1.get_text(strip=True) if h1 else ''

        # Get current meta description
        meta_desc_tag = soup.find('meta', attrs={'name': 'description'})
        meta_description = meta_desc_tag['content'] if meta_desc_tag and meta_desc_tag.get('content') else ''

        # Get first few paragraphs from article
        article = soup.find('article') or soup.find('div', class_='entry-content')
        paragraphs = []
        if article:
            for p in article.find_all('p'):
                text = p.get_text(strip=True)
                if len(text) > 30:
                    paragraphs.append(text)
                if len(paragraphs) >= 3:
                    break

        return {
            'title': title_text,
            'h1': h1_text,
            'meta_description': meta_description,
            'paragraphs': paragraphs,
            'intro': ' '.join(paragraphs[:2])[:500]
        }
    except Exception as e:
        print(f"    [WARN] Could not scrape {url}: {e}")
        return None


def shorten_description(current_desc):
    """Simple shortening: truncate at last complete sentence/phrase within limit."""
    if len(current_desc) <= MAX_DESC_LENGTH:
        return current_desc

    # Try to cut at sentence boundary
    truncated = current_desc[:MAX_DESC_LENGTH]
    last_period = truncated.rfind('.')
    last_comma = truncated.rfind(',')
    last_dash = truncated.rfind(' -')
    last_pipe = truncated.rfind(' |')

    # Pick the best cut point
    cut = max(last_period, last_comma, last_dash, last_pipe)
    if cut > MIN_DESC_LENGTH:
        return truncated[:cut + 1].strip()

    # Fall back to word boundary
    last_space = truncated.rfind(' ')
    if last_space > MIN_DESC_LENGTH:
        return truncated[:last_space].strip() + '.'

    return truncated.strip() + '.'


def generate_description_from_content(page_info):
    """Generate a basic meta description from scraped content (no Claude)."""
    if not page_info or not page_info.get('paragraphs'):
        return None

    intro = page_info['paragraphs'][0]
    # Clean up and truncate
    intro = re.sub(r'\s+', ' ', intro).strip()
    if len(intro) <= MAX_DESC_LENGTH:
        return intro

    return shorten_description(intro)


def claude_fix_titles(issues_with_data):
    """Use Claude API to fix title length issues in batch."""
    import anthropic

    if not ANTHROPIC_API_KEY:
        print("[ERROR] Set ANTHROPIC_API_KEY in config.py")
        return {}

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    results = {}

    pages_text = []
    for item in issues_with_data:
        slug = item['slug']
        issue = item['issue']
        current_title = item.get('current_title', '')
        page_info = item.get('page_info', {})

        entry = f"Slug: {slug}\n"
        entry += f"Issue: {issue} ({len(current_title)} chars)\n"
        entry += f"Current title: {current_title}\n"
        entry += f"H1: {page_info.get('h1', 'N/A')}\n"
        entry += f"Intro text: {page_info.get('intro', 'N/A')[:200]}\n"
        pages_text.append(entry)

    batch_size = 10
    for batch_start in range(0, len(pages_text), batch_size):
        batch = pages_text[batch_start:batch_start + batch_size]

        prompt = "For each page below, write a better SEO title.\n"
        prompt += "Rules:\n"
        prompt += f"- Length: {MIN_TITLE_LENGTH}-{MAX_TITLE_LENGTH} characters (STRICT — the site name '{TITLE_SUFFIX.strip(' -—|')}' is auto-appended, so total will be +{TITLE_SUFFIX_LEN}c)\n"
        prompt += "- Keep the primary keyword/topic — do NOT change the meaning\n"
        prompt += "- For 'too_long' titles: shorten while keeping key terms\n"
        prompt += "- For 'too_short' titles: expand with relevant context\n"
        prompt += "- Do NOT include the site name or separator (e.g. ' | Site Name') — it's auto-appended\n"
        prompt += "- Make it compelling for search results\n\n"
        prompt += "Return ONLY a JSON object mapping slug to new title. No other text.\n"
        prompt += "Example: {\"my-post-slug\": \"New Title Here\"}\n\n"
        prompt += "Pages:\n" + "\n---\n".join(batch)

        max_retries = 4
        for attempt in range(max_retries):
            try:
                print(f"  [CLAUDE] Processing title batch {batch_start // batch_size + 1} "
                      f"({len(batch)} pages)...")

                response = client.messages.create(
                    model=DIRECT_MODEL,
                    max_tokens=2000,
                    messages=[{'role': 'user', 'content': prompt}],
                    system=f"You are an SEO specialist for {SITE_DESCRIPTION}. "
                           f"Return only valid JSON, no markdown fences."
                )

                text = response.content[0].text.strip()
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)

                batch_results = json.loads(text)
                results.update(batch_results)

                for slug, title in list(batch_results.items()):
                    salvaged = seo_quality.safe_shorten_title(title, MAX_TITLE_LENGTH)
                    ok, why = ((False, 'unsalvageable') if salvaged is None
                               else seo_quality.validate_title(
                                   salvaged, max_len=MAX_TITLE_LENGTH,
                                   min_len=MIN_TITLE_LENGTH))
                    if ok:
                        results[slug] = salvaged
                    else:
                        print(f"    [REJECT] {slug}: title {why}: {title!r}")
                        results.pop(slug, None)

                break
            except json.JSONDecodeError as e:
                print(f"    [WARN] JSON parse error: {e}")
                if attempt == max_retries - 1:
                    print(f"    [ERROR] Skipping batch after {max_retries} attempts")
            except Exception as e:
                err_name = type(e).__name__
                if 'OverloadedError' in err_name or '529' in str(e):
                    wait = 15 * (attempt + 1)
                    print(f"    [RETRY] Overloaded, waiting {wait}s")
                    time.sleep(wait)
                else:
                    print(f"    [ERROR] {err_name}: {e}")
                    break

    return results


def claude_fix_descriptions(issues_with_data):
    """Use Claude API to generate/fix meta descriptions in batch."""
    import anthropic

    if not ANTHROPIC_API_KEY:
        print("[ERROR] Set ANTHROPIC_API_KEY in config.py")
        return {}

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    results = {}

    # Build batch prompt
    pages_text = []
    for item in issues_with_data:
        slug = item['slug']
        issue = item['issue']
        current_desc = item.get('current_desc', '')
        page_info = item.get('page_info', {})

        entry = f"Slug: {slug}\n"
        entry += f"Issue: {'missing description' if issue == 'missing' else f'too long ({len(current_desc)} chars)'}\n"
        if current_desc:
            entry += f"Current description: {current_desc}\n"
        entry += f"Page title: {page_info.get('title', 'N/A')}\n"
        entry += f"H1: {page_info.get('h1', 'N/A')}\n"
        entry += f"Intro text: {page_info.get('intro', 'N/A')}\n"
        pages_text.append(entry)

    # Process in batches of 10 to avoid huge prompts
    batch_size = 10
    for batch_start in range(0, len(pages_text), batch_size):
        batch = pages_text[batch_start:batch_start + batch_size]
        batch_items = issues_with_data[batch_start:batch_start + batch_size]

        prompt = "For each page below, write an SEO-optimized meta description.\n"
        prompt += "Rules:\n"
        prompt += "- Length: 120-155 characters (STRICT — never exceed 155)\n"
        prompt += "- Include the primary topic/keyword naturally\n"
        prompt += "- End with a clear value proposition or call to action\n"
        prompt += "- For 'too long' descriptions: shorten the existing one, preserving its key message\n"
        prompt += "- For 'missing' descriptions: write a new one based on the page content\n\n"
        prompt += "Return ONLY a JSON object mapping slug to new description. No other text.\n"
        prompt += "Example: {\"my-post-slug\": \"New description here.\"}\n\n"
        prompt += "Pages:\n" + "\n---\n".join(batch)

        max_retries = 4
        for attempt in range(max_retries):
            try:
                print(f"  [CLAUDE] Processing batch {batch_start // batch_size + 1} "
                      f"({len(batch)} pages)...")

                response = client.messages.create(
                    model=DIRECT_MODEL,
                    max_tokens=2000,
                    messages=[{
                        'role': 'user',
                        'content': prompt
                    }],
                    system=f"You are an SEO specialist for {SITE_DESCRIPTION}. "
                           f"Return only valid JSON, no markdown fences."
                )

                text = response.content[0].text.strip()
                # Strip markdown code fences if present
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)

                batch_results = json.loads(text)
                results.update(batch_results)

                # Shorten + validate; drop any description that can't be made clean
                for slug, desc in list(batch_results.items()):
                    if len(desc) > 160:
                        desc = shorten_description(desc)
                    ok, why = seo_quality.validate_metadesc(desc)
                    if ok:
                        results[slug] = desc
                    else:
                        print(f"    [REJECT] {slug}: meta {why}")
                        results.pop(slug, None)

                break  # Success

            except json.JSONDecodeError as e:
                print(f"    [WARN] JSON parse error: {e}")
                print(f"    Raw response: {text[:200]}")
                if attempt == max_retries - 1:
                    print(f"    [ERROR] Skipping batch after {max_retries} attempts")
            except Exception as e:
                err_name = type(e).__name__
                if 'OverloadedError' in err_name or '529' in str(e):
                    wait = 15 * (attempt + 1)
                    print(f"    [RETRY] Overloaded, waiting {wait}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
                else:
                    print(f"    [ERROR] {err_name}: {e}")
                    break

    return results


def run(report_path, use_claude=False, execute=False, live=False, generate_only=False, approve=False):
    """Main entry point."""
    target = "LIVE" if live else "LOCAL"
    mode = "GENERATE ONLY" if generate_only else ("EXECUTE" if execute else "DRY RUN")

    print(f"{'=' * 80}")
    print(f"CRAWL FIX — All SEO Issues — {mode}")
    print(f"{'=' * 80}\n")

    # Step 1: Parse report
    print("[PARSE] Reading CrawlyCat report...")
    report = parse_report(report_path)

    desc_issues = report['meta_desc_missing'] + report['meta_desc_length']
    title_issues = report['meta_title_length']
    h1_issues = report['h1_multiple']
    broken_links = report['broken_links']
    alt_issues = report['img_alt_missing']

    print(f"  Meta description missing:  {len(report['meta_desc_missing'])}")
    print(f"  Meta description length:   {len(report['meta_desc_length'])}")
    print(f"  Meta title length:         {len(title_issues)}")
    print(f"  Multiple H1 tags:          {len(h1_issues)}")
    print(f"  Internal broken links:     {len(broken_links)}")
    print(f"  Images missing alt text:   {len(alt_issues)} (data: placeholders excluded)")

    # Report-only issues (not auto-fixable)
    if alt_issues:
        print(f"\n[INFO] Images missing alt text (manual fix needed):")
        for item in alt_issues:
            for img_url in item['images']:
                print(f"  - {item['slug']}: {img_url[:80]}")

    if h1_issues:
        print(f"\n[INFO] Multiple H1 issues (manual fix needed):")
        for item in h1_issues:
            print(f"  - {item['slug']} ({item['count']} H1 tags)")

    if broken_links:
        print(f"\n[INFO] Broken internal links (manual fix needed):")
        for item in broken_links:
            print(f"  - {item['slug']} -> {item['broken_url']}")

    # Fixable issues
    all_fixable = desc_issues + title_issues
    if not all_fixable:
        total_raw = len(report['meta_desc_missing']) + len(report['meta_desc_length']) + len(title_issues)
        if total_raw > 0:
            print(f"\n  All {total_raw} meta/title issue(s) are on archive, taxonomy, or pagination")
            print(f"  pages (category/, tag/, author/, /page/N/) — these cannot be fixed")
            print(f"  via the WordPress REST API. Fix them manually in WordPress admin:")
            print(f"    • Category/tag descriptions: WP Admin → Posts → Categories/Tags → Edit")
            print(f"    • Author bio: WP Admin → Users → Edit Profile → Biographical Info")
            print(f"    • Pagination pages: handled automatically by Yoast (no action needed)")
        else:
            print("\n  No auto-fixable issues found.")
        return

    output_desc_path = os.path.join(OUTPUT_DIR, 'crawl-metadesc-fixes.json')
    output_title_path = os.path.join(OUTPUT_DIR, 'crawl-title-fixes.json')

    # Check for cached fixes
    saved_desc_fixes = {}
    if os.path.exists(output_desc_path) and not generate_only:
        with open(output_desc_path, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        if saved:
            saved_desc_fixes = {rec['slug']: rec['new_desc'] for rec in saved}
            print(f"[CACHE] Loaded {len(saved_desc_fixes)} cached description fixes")

    saved_title_fixes = {}
    if os.path.exists(output_title_path) and not generate_only:
        with open(output_title_path, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        if saved:
            saved_title_fixes = {rec['slug']: rec['new_title'] for rec in saved}
            print(f"[CACHE] Loaded {len(saved_title_fixes)} cached title fixes")

    # ── Validate against live state (skip already-fixed issues) ──
    print(f"\n[VALIDATE] Checking {len(all_fixable)} issues against live site...")
    all_fixable = validate_issues_live(all_fixable)
    print(f"  {len(all_fixable)} issues still need fixing\n")

    if not all_fixable:
        print("  All issues from the report are already fixed!")
        return

    # ── Fetch post IDs from WordPress (needed for applying fixes) ──
    if generate_only:
        for item in all_fixable:
            item['post_id'] = None
    else:
        session, api_url = get_wp_session(live=live)
        if not session:
            return

        print(f"[FETCH] Getting post IDs from {target}...")
        valid_items = []
        for item in all_fixable:
            slug = item['slug']
            try:
                post, post_type = fetch_post_data(session, api_url, slug)
            except Exception as e:
                print(f"  ERROR {slug}: {type(e).__name__} — is XAMPP/WordPress running?")
                print(f"  Aborting. Start your local WordPress or use --live / --generate.")
                return

            if not post:
                print(f"  SKIP {slug}: post/page not found")
                continue

            item['post_id'] = post['id']
            item['post_type'] = post_type
            valid_items.append(item)

        all_fixable = valid_items
        print(f"  Found {len(all_fixable)} posts\n")

    # Separate by type
    desc_items = [i for i in all_fixable if i['issue'] in ('desc_missing', 'desc_too_long', 'desc_too_short')]
    title_items = [i for i in all_fixable if i['issue'] in ('title_too_long', 'title_too_short')]

    # ── Generate description fixes ────────────────────────────────
    desc_fixes = {}
    if desc_items:
        if saved_desc_fixes:
            desc_fixes = saved_desc_fixes
            print(f"[CACHE] Using {len(desc_fixes)} cached description fixes")
        elif use_claude:
            print("[CLAUDE] Generating meta descriptions...")
            desc_fixes = claude_fix_descriptions(desc_items)
        else:
            print("[FIX] Generating description fixes (simple mode)...")
            for item in desc_items:
                slug = item['slug']
                if item['issue'] in ('desc_too_long', 'desc_too_short'):
                    new = shorten_description(item['current_desc'])
                    if new != item['current_desc']:
                        desc_fixes[slug] = new
                elif item['issue'] == 'desc_missing':
                    page_info = item.get('page_info') or scrape_page_content(item['url'])
                    if page_info:
                        desc = generate_description_from_content(page_info)
                        if desc:
                            desc_fixes[slug] = desc

    # ── Generate title fixes ──────────────────────────────────────
    title_fixes = {}
    if title_items:
        if saved_title_fixes:
            title_fixes = saved_title_fixes
            print(f"[CACHE] Using {len(title_fixes)} cached title fixes")
        elif use_claude:
            print("[CLAUDE] Fixing titles...")
            title_fixes = claude_fix_titles(title_items)
        else:
            print("[FIX] Title fixes require --claude flag (can't auto-shorten titles meaningfully)")

    # ── Save description fix records ──────────────────────────────
    desc_records = []
    for item in desc_items:
        slug = item['slug']
        if slug in desc_fixes:
            desc_records.append({
                'slug': slug,
                'post_id': item.get('post_id'),
                'post_type': item.get('post_type', 'posts'),
                'issue': item['issue'],
                'old_desc': item.get('current_desc', ''),
                'new_desc': desc_fixes[slug],
                'new_length': len(desc_fixes[slug])
            })

    if desc_records:
        with open(output_desc_path, 'w', encoding='utf-8') as f:
            json.dump(desc_records, f, indent=2, ensure_ascii=False)
        print(f"\n[SAVE] {len(desc_records)} description fixes -> {output_desc_path}")

    # ── Save title fix records ────────────────────────────────────
    title_records = []
    for item in title_items:
        slug = item['slug']
        if slug in title_fixes:
            title_records.append({
                'slug': slug,
                'post_id': item.get('post_id'),
                'post_type': item.get('post_type', 'posts'),
                'issue': item['issue'],
                'old_title': item.get('current_title', ''),
                'new_title': title_fixes[slug],
                'new_length': len(title_fixes[slug])
            })

    if title_records:
        with open(output_title_path, 'w', encoding='utf-8') as f:
            json.dump(title_records, f, indent=2, ensure_ascii=False)
        print(f"[SAVE] {len(title_records)} title fixes -> {output_title_path}")

    # ── Show results / Apply ──────────────────────────────────────
    all_records = desc_records + title_records
    print(f"\n{'=' * 80}")
    applied = 0
    skipped = 0
    for rec in all_records:
        slug = rec['slug']
        is_title = 'new_title' in rec
        field_label = 'Title' if is_title else 'Description'
        old_val = rec.get('old_title', rec.get('old_desc', ''))
        new_val = rec.get('new_title', rec.get('new_desc', ''))

        print(f"\n[{slug}]")
        print(f"  {field_label} ({rec['issue']}): {len(old_val)}c -> {len(new_val)}c")
        if old_val:
            print(f"  Old:  {old_val[:80]}{'...' if len(old_val) > 80 else ''}")
        print(f"  New:  {new_val[:80]}{'...' if len(new_val) > 80 else ''}")

        if execute and not generate_only:
            if approve:
                choice = input("  Apply? [y]es / [n]o / [a]ll remaining / [q]uit: ").strip().lower()
                if choice == 'q':
                    break
                elif choice == 'a':
                    approve = False
                elif choice != 'y':
                    skipped += 1
                    continue

            post_id = rec['post_id']
            pt = rec.get('post_type', 'posts')
            try:
                if is_title:
                    data = {'yoast_title': new_val}
                else:
                    data = {'yoast_metadesc': new_val}
                resp = session.post(f"{api_url}/{pt}/{post_id}", json=data)
                resp.raise_for_status()
                print(f"  UPDATED")
                applied += 1
            except Exception as e:
                print(f"  FAILED: {e}")
        else:
            tag = "GENERATE ONLY" if generate_only else "DRY RUN"
            print(f"  [{tag}]")

    print(f"\n{'=' * 80}")
    total = len(all_records)
    if execute and not generate_only:
        print(f"Applied {applied}, skipped {skipped} of {total} fixes.")
    elif generate_only:
        print(f"Generated {total} fixes ({len(desc_records)} descriptions, {len(title_records)} titles).")
    else:
        print(f"DRY RUN: {total} fixes ready ({len(desc_records)} descriptions, {len(title_records)} titles).")
    print(f"{'=' * 80}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python crawl_fix.py <report.html> [--claude] [--execute] [--live]")
        sys.exit(1)

    report_file = sys.argv[1]
    if not os.path.exists(report_file):
        print(f"[ERROR] Report not found: {report_file}")
        sys.exit(1)

    use_claude = '--claude' in sys.argv
    execute = '--execute' in sys.argv
    live = '--live' in sys.argv
    generate_only = '--generate' in sys.argv
    approve = '--approve' in sys.argv

    run(report_file, use_claude=use_claude, execute=execute, live=live,
        generate_only=generate_only, approve=approve)
