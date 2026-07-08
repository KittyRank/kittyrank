"""
Cornerstone link auditor.

For each cornerstone slug in CORNERSTONE_SLUGS, report:
  - INBOUND: which other posts already link to it (in body content)
  - MISSING: which high-topic-overlap posts DON'T link to it but should

Mechanism:
  1. For every published post on the site, fetch its rendered HTML body and
     extract the set of internal slugs it links to.
  2. For each cornerstone, build its topic-token set from title + slug + tracked
     keywords (reusing the same token logic + brand-stripping the post-ideas
     dedupe uses).
  3. Score every other post against each cornerstone by token overlap.
  4. Bucket each scored post as inbound (already links) or missing (overlap >=
     threshold but no link). Rank missing by overlap desc.

Output: cornerstone-link-audit.json + a human-readable report.

The fetched-content cache (wp-post-content-cache.json) avoids re-fetching every
post on every run — entries older than CACHE_TTL_HOURS are refreshed.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from config import *
import seo_quality


def _post_idea_tokens(text, brand_tokens):
    """Topic tokens from a string, with stopwords + brand + generic content-type
    words removed. Local copy — was previously imported from claude_analyze but
    that's no longer in the OSS carve since it lived in the post-ideas section."""
    import re
    words = re.findall(r"[a-z0-9]+", (text or '').lower())
    drop = (set(t.lower() for t in (brand_tokens or [])) |
            {'a','an','the','to','for','of','and','or','in','on','with','how','your',
             'you','is','are','this','that','at','by','from','be','as','into','but',
             'new','vs','via','about','what','why','when','where','who','which','than'} |
            {'guide','tutorial','complete','explained','introduction','intro','basics',
             'beginner','beginners','best','top','ultimate','practical','simple','easy',
             'getting','started','overview','walkthrough','tips','tricks','examples',
             'fundamentals','essentials','step','steps'})
    return set(w for w in words if len(w) > 1 and w not in drop)

CACHE_TTL_HOURS = 24
MIN_OVERLAP_FOR_CANDIDATE = 3   # below this, a post isn't relevant enough to flag
MAX_MISSING_PER_CORNERSTONE = 30

# Type definitions (see config.py CORNERSTONE_SLUGS doc block for full rationale)
TYPE_DESCRIPTIONS = {
    'A': {
        'label': 'Content Pillar',
        'priority': 'HIGH',
        'short': 'Topic-specific deep dive — primary keyword appears naturally in many posts. '
                 'In-body editorial links carry strong topical-authority signal.',
        'flags': ('reciprocal_gap', 'high_overlap'),
    },
    'B': {
        'label': 'Architectural Hub',
        'priority': 'LOW',
        'short': 'Index/learning-path post — title phrase does NOT naturally appear in body content. '
                 'Banner mu-plugin already provides body-content link from every cluster post. '
                 'Adding more identical-anchor links would look templated to Google.',
        'flags': ('high_overlap',),    # suppress pure reciprocal gaps
    },
}


def _normalize_cornerstones(raw):
    """Accept dict {slug: {type: 'A'}} OR plain list (defaults to Type A).
    Returns ordered list of (slug, meta) tuples — A first, then B."""
    if isinstance(raw, dict):
        items = [(slug, dict(meta or {})) for slug, meta in raw.items()]
    else:
        items = [(slug, {'type': 'A'}) for slug in (raw or [])]
    for _, meta in items:
        meta.setdefault('type', 'A')
        if meta['type'] not in TYPE_DESCRIPTIONS:
            meta['type'] = 'A'
    # Sort: A first (higher priority), then B
    items.sort(key=lambda x: (x[1]['type'], x[0]))
    return items


def _wp_session():
    """Return (session, api_url) configured for the LIVE site (read-only here)."""
    import requests
    from requests.auth import HTTPBasicAuth
    site_url = SITE_DOMAIN.rstrip('/')
    api_url = f"{site_url}/wp-json/wp/v2"
    s = requests.Session()
    s.headers.update({'User-Agent': 'NerdySEOAuditor/1.0'})
    # Use live creds if present, else unauthenticated (works for public posts)
    user = globals().get('WP_LIVE_USER', '') or globals().get('LIVE_USER', '')
    pw = globals().get('WP_LIVE_PASS', '') or globals().get('LIVE_PASS', '')
    if user and pw:
        s.auth = HTTPBasicAuth(user, pw)
    return s, api_url


def _site_link_pattern():
    """Regex matching any href to a post on the configured site."""
    # Match both https://domain.tld/SLUG/ and relative /SLUG/ — covers theme
    # variations and migration artifacts.
    domain = re.escape(SITE_DOMAIN.rstrip('/').replace('https://', '').replace('http://', ''))
    return re.compile(
        rf'href=["\'][^"\']*(?:{domain}|^)/([a-z0-9_-]+)/?["\']',
        re.IGNORECASE,
    )


def _extract_internal_links(html):
    """Return a set of slugs linked from this HTML, excluding nav/header/footer."""
    if not html:
        return set()
    # Trim to <article> body if present — strips sitewide nav/footer noise
    m = re.search(r'<article[^>]*>(.*?)</article>', html, re.S | re.I)
    body = m.group(1) if m else html
    pattern = _site_link_pattern()
    return {m.group(1) for m in pattern.finditer(body)}


def _load_cache(cache_path):
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache_path, cache):
    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f"  [WARN] failed to save cache: {e}")


def _fetch_all_posts(session, api_url):
    """Fetch every published post + page (slug, title, content) via WP REST.
    Pillar posts like embedded-systems-learning-path are stored as WP pages,
    not posts, so we hit both endpoints. Paginates 100/page."""
    all_items = []
    for endpoint in ('posts', 'pages'):
        page = 1
        before = len(all_items)
        while True:
            try:
                r = session.get(f"{api_url}/{endpoint}",
                                params={'per_page': 100, 'page': page, '_fields': 'slug,title,content,link'},
                                timeout=30)
                if r.status_code == 400 and 'rest_post_invalid_page_number' in r.text:
                    break
                r.raise_for_status()
                batch = r.json()
            except Exception as e:
                print(f"  [WARN] {endpoint} page {page} fetch failed: {e}")
                break
            if not batch:
                break
            all_items.extend(batch)
            print(f"  [WP] {endpoint} page {page}: {len(batch)} items (total so far {len(all_items)})")
            page += 1
            if page > 50:
                print(f"  [WARN] hit page-50 safety cap for {endpoint}")
                break
            time.sleep(0.1)
        print(f"  [WP] {endpoint}: {len(all_items) - before} items total")
    return all_items


def run_audit():
    raw_cs = globals().get('CORNERSTONE_SLUGS', {})
    cornerstones = _normalize_cornerstones(raw_cs)
    if not cornerstones:
        print("[CORNERSTONE] CORNERSTONE_SLUGS not set in config.py — nothing to audit.")
        return

    brand_tokens = globals().get('SITE_BRAND_TOKENS', [])
    output_dir = OUTPUT_DIR
    cache_path = os.path.join(output_dir, 'wp-post-content-cache.json')

    type_a = sum(1 for _, m in cornerstones if m['type'] == 'A')
    type_b = sum(1 for _, m in cornerstones if m['type'] == 'B')
    print(f"[CORNERSTONE] Auditing {len(cornerstones)} cornerstones for site {SITE_DOMAIN}")
    print(f"[CORNERSTONE]   Type A (Content Pillars, HIGH priority): {type_a}")
    print(f"[CORNERSTONE]   Type B (Architectural Hubs, lower priority): {type_b}")

    # 1. Fetch every published post (with cache)
    session, api_url = _wp_session()
    cache = _load_cache(cache_path)
    now = datetime.now()
    cache_ttl = timedelta(hours=CACHE_TTL_HOURS)

    # Refresh full post list always (cheap-ish), then re-use cached content where fresh
    print("[CORNERSTONE] Fetching post list from WP REST...")
    posts = _fetch_all_posts(session, api_url)
    print(f"[CORNERSTONE] Total posts on site: {len(posts)}")

    # 2. Build per-post token set + outbound-link set (cached on (slug, content_hash))
    post_records = {}
    refreshed = 0
    for p in posts:
        slug = p.get('slug', '')
        if not slug:
            continue
        title = (p.get('title') or {}).get('rendered', '') or ''
        content = (p.get('content') or {}).get('rendered', '') or ''
        # Cache by slug; refresh if content changed or older than TTL
        cached = cache.get(slug) or {}
        content_len = len(content)
        cache_fresh = (
            cached.get('content_len') == content_len
            and cached.get('fetched_at')
            and (now - datetime.fromisoformat(cached['fetched_at'])) < cache_ttl
        )
        if cache_fresh:
            outbound = set(cached.get('outbound', []))
        else:
            outbound = _extract_internal_links(content)
            cache[slug] = {
                'fetched_at': now.isoformat(),
                'content_len': content_len,
                'outbound': sorted(outbound),
            }
            refreshed += 1
        # Token set from title + slug (NOT content — content tokens are noisy
        # and would falsely inflate every overlap)
        tokens = _post_idea_tokens(
            f"{title} {slug.replace('-', ' ')}",
            brand_tokens=brand_tokens,
        )
        post_records[slug] = {
            'slug': slug,
            'title': title,
            'tokens': tokens,
            'outbound': outbound,
        }

    _save_cache(cache_path, cache)
    print(f"[CORNERSTONE] Indexed {len(post_records)} posts ({refreshed} freshly fetched, {len(post_records) - refreshed} from cache)")

    # 3. For each cornerstone, build its token set + classify every other post
    audit = []
    for cs_slug, cs_meta in cornerstones:
        cs_type = cs_meta['type']
        type_info = TYPE_DESCRIPTIONS[cs_type]
        allowed_flags = set(type_info['flags'])

        cs = post_records.get(cs_slug)
        if not cs:
            print(f"  [WARN] cornerstone '{cs_slug}' not found in site posts — skipping")
            audit.append({
                'cornerstone': cs_slug,
                'type': cs_type,
                'type_label': type_info['label'],
                'priority': type_info['priority'],
                'title': '',
                'error': 'not found in WP REST results — verify the slug is correct and post is published',
                'inbound_count': 0, 'inbound': [], 'missing_count': 0, 'missing': [],
            })
            continue

        cs_tokens = cs['tokens']
        if len(cs_tokens) < 2:
            print(f"  [WARN] cornerstone '{cs_slug}' has too few topic tokens ({len(cs_tokens)}) — overlap-matching will be unreliable")

        inbound = []
        missing_by_slug = {}   # slug -> {reasons, overlap, ...} — merge duplicates
        suppressed_count = 0   # gaps the type rule excluded (Type B reciprocal-only)
        cs_outbound = cs.get('outbound', set())   # what THIS cornerstone links to

        for slug, p in post_records.items():
            if slug == cs_slug:
                continue
            overlap_set = cs_tokens & p['tokens']
            overlap = len(overlap_set)
            links_to_cs = cs_slug in p['outbound']
            in_cs_outbound = slug in cs_outbound

            if links_to_cs:
                inbound.append({
                    'slug': slug,
                    'title': p['title'],
                    'overlap': overlap,
                    'reciprocates': in_cs_outbound,
                })
            else:
                # Detect ALL applicable reasons, then filter by type-allowed flags
                raw_reasons = []
                if overlap >= MIN_OVERLAP_FOR_CANDIDATE:
                    raw_reasons.append('high_overlap')
                if in_cs_outbound:
                    raw_reasons.append('reciprocal_gap')
                if not raw_reasons:
                    continue
                # Apply Type A vs Type B rules
                kept_reasons = [r for r in raw_reasons if r in allowed_flags]
                if not kept_reasons:
                    # Reason existed but type rules suppress it (e.g. Type B pure
                    # reciprocal gap — banner already handles)
                    suppressed_count += 1
                    continue
                missing_by_slug[slug] = {
                    'slug': slug,
                    'title': p['title'],
                    'overlap': overlap,
                    'shared_tokens': sorted(overlap_set, key=lambda w: (-len(w), w)),
                    'reasons': kept_reasons,
                }

        # Score for ranking: reciprocal_gap is high-priority (structural), then overlap
        def _score(m):
            recip = 1000 if 'reciprocal_gap' in m['reasons'] else 0
            return recip + m['overlap']

        missing_all = sorted(missing_by_slug.values(), key=_score, reverse=True)
        total_reciprocal = sum(1 for m in missing_all if 'reciprocal_gap' in m['reasons'])
        total_overlap_only = sum(1 for m in missing_all if 'reciprocal_gap' not in m['reasons'])
        missing = missing_all[:MAX_MISSING_PER_CORNERSTONE]
        inbound.sort(key=lambda x: x['overlap'], reverse=True)

        audit.append({
            'cornerstone': cs_slug,
            'type': cs_type,
            'type_label': type_info['label'],
            'priority': type_info['priority'],
            'type_explanation': type_info['short'],
            'title': cs['title'],
            'inbound_count': len(inbound),
            'inbound': inbound[:20],   # top 20, sorted by overlap
            'inbound_total_listed': len(inbound),
            'missing_count': len(missing_all),         # TRUE count after type filtering
            'missing_displayed': len(missing),         # what's in 'missing' array
            'missing_reciprocal_count': total_reciprocal,
            'missing_overlap_only_count': total_overlap_only,
            'missing_suppressed_by_type_rules': suppressed_count,
            'cornerstone_outbound_count': len(cs_outbound),
            'missing': missing,
        })

    # 4. Write output
    output = {
        'generated_at': now.isoformat(),
        'site_domain': SITE_DOMAIN,
        'cornerstone_count': len(cornerstones),
        'total_posts_indexed': len(post_records),
        'min_overlap_for_candidate': MIN_OVERLAP_FOR_CANDIDATE,
        'audit': audit,
    }
    json_path = os.path.join(output_dir, 'cornerstone-link-audit.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n[CORNERSTONE] Audit JSON saved: {json_path}")

    # 5. Human-readable report
    report_lines = [
        "CORNERSTONE LINK AUDIT",
        "=" * 72,
        f"Generated: {now.strftime('%Y-%m-%d %H:%M')}",
        f"Site: {SITE_DOMAIN}",
        f"Cornerstones audited: {len(cornerstones)}",
        f"Total posts indexed: {len(post_records)}",
        "",
        "─" * 72,
        "CORNERSTONE TYPES",
        "─" * 72,
        "",
        "TYPE A — CONTENT PILLAR  (HIGH PRIORITY)",
        "  " + TYPE_DESCRIPTIONS['A']['short'].replace('\n', '\n  '),
        "  Audit flags BOTH reciprocal gaps AND topic-overlap gaps.",
        "",
        "TYPE B — ARCHITECTURAL HUB  (LOWER PRIORITY)",
        "  " + TYPE_DESCRIPTIONS['B']['short'].replace('\n', '\n  '),
        "  Audit flags ONLY topic-overlap gaps. Reciprocal gaps are suppressed",
        "  because the mu-plugin banner already provides the body-content link",
        "  from every cluster post — adding 100+ more identical-anchor links",
        "  would look templated to Google.",
        "",
    ]
    # Sort audit by type (A first), then by missing_count desc within type
    sorted_audit = sorted(audit, key=lambda a: (a.get('type', 'A'),
                                                  -a.get('missing_count', 0)))
    last_type = None
    for a in sorted_audit:
        if a.get('type') != last_type:
            last_type = a.get('type')
            tinfo = TYPE_DESCRIPTIONS.get(last_type, {})
            report_lines.append(f"\n{'═' * 72}")
            report_lines.append(f"  TYPE {last_type} — {tinfo.get('label', '')}  "
                                f"(priority: {tinfo.get('priority', '?')})")
            report_lines.append(f"{'═' * 72}")
        report_lines.append(f"\n{'─' * 72}")
        report_lines.append(f"CORNERSTONE: {a['cornerstone']}  [Type {a.get('type')}]")
        if a.get('error'):
            report_lines.append(f"  ⚠ {a['error']}")
            continue
        report_lines.append(f"  Title: {a['title']}")
        report_lines.append(f"  Inbound links (other posts → this): {a['inbound_count']}")
        report_lines.append(f"  This cornerstone links out to: {a['cornerstone_outbound_count']} posts")
        report_lines.append(f"  Missing links to add: {a['missing_count']} "
                            f"({a['missing_reciprocal_count']} reciprocal + "
                            f"{a['missing_overlap_only_count']} overlap)")
        if a.get('missing_suppressed_by_type_rules', 0):
            report_lines.append(f"  (Suppressed by Type-{a['type']} rules: "
                                f"{a['missing_suppressed_by_type_rules']} reciprocal-only gaps "
                                f"— banner mechanism already covers these)")
        if a['missing']:
            report_lines.append(f"\n  Top missing-link candidates (add an in-content link from these):")
            for m in a['missing'][:20]:
                reasons = '/'.join(m['reasons'])
                tokens_preview = ', '.join(m['shared_tokens'][:5]) if m['shared_tokens'] else '(no shared tokens)'
                report_lines.append(f"    [{reasons:<25}] overlap={m['overlap']}  [{tokens_preview}]")
                report_lines.append(f"      → {m['slug']}")
    report_path = os.path.join(output_dir, 'cornerstone-link-audit-report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines))
    print(f"[CORNERSTONE] Human-readable report: {report_path}")

    # Console summary — grouped by type, A first
    print(f"\n{'─' * 80}")
    print(f"  {'CORNERSTONE':<60} {'in':>4} {'rec':>4} {'ovl':>4} {'sup':>4}")
    last_type = None
    for a in sorted_audit:
        if a.get('type') != last_type:
            last_type = a.get('type')
            tinfo = TYPE_DESCRIPTIONS.get(last_type, {})
            print(f"\n  ─ TYPE {last_type} — {tinfo.get('label', ''):<25} priority: {tinfo.get('priority', '?')}")
        if a.get('error'):
            print(f"  ✗ {a['cornerstone']}: {a['error']}")
            continue
        recip = a.get('missing_reciprocal_count', 0)
        ovl = a.get('missing_overlap_only_count', 0)
        sup = a.get('missing_suppressed_by_type_rules', 0)
        flag = '⭐' if (recip + ovl) >= 5 else '  '
        print(f"  {flag} {a['cornerstone']:<60} {a['inbound_count']:>4} {recip:>4} {ovl:>4} {sup:>4}")
    print("")
    print("  in  = in-body inbound links from other posts")
    print("  rec = reciprocal gaps  (cornerstone lists post X but X doesn't link back)")
    print("  ovl = overlap gaps     (high-topic-overlap posts not linking)")
    print("  sup = suppressed by type rules (Type B reciprocal-only gaps the banner already covers)")
    print("  ⭐ = 5+ actionable gaps — high-leverage to fix")


if __name__ == '__main__':
    if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    run_audit()
