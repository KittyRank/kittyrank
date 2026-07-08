"""
seo_quality.py — quality gates for the SEO pipeline.

Four gates, all pure functions so they can be unit-tested and reused by
claude_analyze.py, crawl_fix.py and review.py:

  1. validate_title / validate_metadesc  — reject truncated, dangling,
     keyword-stuffed, or out-of-range generated text BEFORE it reaches the
     review queue (stops "Deepen Intimacy with").
  2. keyword_is_junk / filter_keywords   — drop low-signal / garbled target
     keywords (stops "bdsm divergence paths ... restarints").
  3. build_ctr_curve / ctr_gap / should_rewrite — only propose a rewrite when a
     page actually under-earns clicks for its position (kills the rewrite loop).
  4. linking_candidates                   — for pages that rank but weakly,
     suggest internal links instead of a text rewrite.

Nothing here calls an API or touches the network.
"""

import re
from collections import defaultdict

# ── tunables (override via config if desired) ──────────────────────────────
TITLE_MIN, TITLE_MAX = 30, 60
META_MIN, META_MAX = 110, 160
KW_MIN_IMPRESSIONS = 15      # below this a query is too sparse to optimize for
KW_MAX_WORDS = 8             # run-on queries are usually garbled/typo'd
KW_MAX_LEN = 70
CTR_GAP_THRESHOLD = 1.0      # pp below expected before a rewrite is worth it
LINK_POS_BAND = (10.0, 30.0)  # "ranks but weakly" — a visibility problem

# words a real title/description should not END on (truncation tells)
_DANGLING = {
    'with', 'and', 'or', 'to', 'for', 'of', 'in', 'on', 'at', 'by', 'from',
    'the', 'a', 'an', 'that', 'than', 'into', 'as', 'but', 'nor', 'your',
    'our', 'their', 'his', 'her', 'its', 'this', 'these', 'those', 'is',
    'are', 'was', 'were', 'be', 'about', 'vs', 'via', '&',
}
_STOPWORDS = {'a', 'an', 'the', 'to', 'for', 'of', 'and', 'or', 'in', 'on',
              'with', 'how', 'your', 'you', 'is', 'are', 'this', 'that'}


def _words(s):
    return [w for w in re.findall(r"[a-z0-9']+", (s or '').lower())]


# ── Gate 1: generated-text validators ──────────────────────────────────────
def validate_title(title, original=None, keyword=None,
                   min_len=TITLE_MIN, max_len=TITLE_MAX):
    """Return (ok: bool, reason: str). reason is '' when ok."""
    t = (title or '').strip()
    if not t:
        return False, 'empty'
    if '...' in t or '…' in t:
        return False, 'contains ellipsis (truncated)'
    if t[-1] in ',:;-—|/&':
        return False, f'ends on dangling punctuation {t[-1]!r}'
    last = _words(t)[-1] if _words(t) else ''
    if last in _DANGLING:
        return False, f'ends on dangling word {last!r} (truncated)'
    if len(t) < min_len:
        return False, f'too short ({len(t)}c < {min_len})'
    if len(t) > max_len:
        return False, f'too long ({len(t)}c > {max_len})'
    if original and t.strip().lower() == original.strip().lower():
        return False, 'identical to current title (no-op)'
    if keyword:
        kw = keyword.strip().lower()
        if kw and t.lower().count(kw) > 1:
            return False, 'keyword stuffed (appears twice)'
        # title that is *only* the keyword reads like spam
        if kw and _words(t) and set(_words(t)) - _STOPWORDS == set(_words(kw)) - _STOPWORDS:
            return False, 'title is just the bare keyword'
    return True, ''


def validate_metadesc(meta, keyword=None, min_len=META_MIN, max_len=META_MAX):
    m = (meta or '').strip()
    if not m:
        return False, 'empty'
    if m[-1] in ',:;-—|/&':
        return False, f'ends on dangling punctuation {m[-1]!r}'
    last = _words(m)[-1] if _words(m) else ''
    # a trailing ellipsis is allowed for meta, but not a dangling word
    if last in _DANGLING and not m.endswith(('...', '…')):
        return False, f'ends on dangling word {last!r}'
    if len(m) < min_len:
        return False, f'too short ({len(m)}c < {min_len})'
    if len(m) > max_len:
        return False, f'too long ({len(m)}c > {max_len})'
    if keyword:
        kw = keyword.strip().lower()
        if kw and m.lower().count(kw) > 2:
            return False, 'keyword stuffed'
    return True, ''


def safe_shorten_title(title, max_len=TITLE_MAX):
    """Trim whole words off a too-long title without leaving a dangling word or
    punctuation. Returns a clean title, or None if it can't be salvaged."""
    t = (title or '').strip()
    if len(t) <= max_len:
        ok, _ = validate_title(t, max_len=max_len, min_len=1)
        return t if ok else None
    words = t.split()
    while words and len(' '.join(words)) > max_len:
        words.pop()
    while words and (words[-1].lower().strip(",.:;-—|/&") in _DANGLING
                     or words[-1] in ',.:;-—|/&'):
        words.pop()
    out = ' '.join(words).rstrip(' ,:;-—|/&')
    ok, _ = validate_title(out, max_len=max_len, min_len=1)
    return out if ok else None


def safe_shorten_meta(meta, max_len=META_MAX):
    """Cut a too-long description at the last sentence end (or clean word)."""
    m = (meta or '').strip()
    if len(m) <= max_len:
        return m
    cut = m[:max_len]
    for p in ('. ', '! ', '? '):
        idx = cut.rfind(p)
        if idx > META_MIN:
            return cut[:idx + 1].strip()
    words = cut.split()
    while words and words[-1].lower().strip(",.:;-—|/&") in _DANGLING:
        words.pop()
    return (' '.join(words).rstrip(' ,:;-—|/&') + '.') if words else None


# ── Gate 2: keyword quality ─────────────────────────────────────────────────
def keyword_is_junk(kw, min_impressions=KW_MIN_IMPRESSIONS):
    """kw is a dict with query/impressions/clicks/position."""
    q = (kw.get('query') or '').strip()
    if not q:
        return True
    if kw.get('impressions', 0) < min_impressions:
        return True
    words = q.split()
    if len(words) > KW_MAX_WORDS or len(q) > KW_MAX_LEN:
        return True          # run-on / garbled long-tail
    # a long query that never earns a click despite "ranking" is noise
    if kw.get('clicks', 0) == 0 and kw.get('position', 99) > 10 and len(words) >= 5:
        return True
    return False


def filter_keywords(keywords, min_impressions=KW_MIN_IMPRESSIONS):
    """Drop junk queries. If EVERY query is junk, return [] — the page has no
    real keyword opportunity, so the caller should target the page's own topic
    rather than a garbled/low-signal query (don't fall back to junk)."""
    return [k for k in keywords if not keyword_is_junk(k, min_impressions)]


# ── Gate 3: expected-CTR curve + rewrite decision ──────────────────────────
_DEFAULT_CURVE = {1: 28, 2: 15, 3: 10, 4: 8, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2.5, 10: 2}


def build_ctr_curve(all_keywords, min_samples=8, min_keyword_impr=30, percentile=0.60, floor_ratio=0.5):
    """CTR (%) per integer position bucket, derived from the site's own data
    BUT with three guards against the self-reinforcing-low-CTR trap:

      1. min_keyword_impr — only keywords with enough impressions count, so the
         curve isn't dragged down by thousands of long-tail 1-impression
         zero-click queries that all read as 0% CTR.
      2. percentile (default 60th) — use a slightly-above-median value to
         reflect "what a decently-tuned page at this position achieves",
         not "what the median struggling page achieves".
      3. floor_ratio — never let the site curve drop below floor_ratio × the
         default Google-standard curve. Otherwise a site with universally bad
         CTR ends up with a self-justifying expected-CTR of 0% and the
         pipeline never proposes any rewrites.
    """
    buckets = defaultdict(list)
    for k in all_keywords:
        impr = k.get('impressions', 0)
        if impr < min_keyword_impr:    # filter long-tail noise
            continue
        pos = int(round(k.get('position', 0)))
        if pos < 1:
            continue
        buckets[pos].append(k.get('clicks', 0) / impr * 100)
    curve = {}
    for pos in range(1, 21):
        vals = sorted(buckets.get(pos, []))
        default = _DEFAULT_CURVE.get(pos, 1.5)
        if len(vals) >= min_samples:
            pct_idx = min(int(len(vals) * percentile), len(vals) - 1)
            pct_val = vals[pct_idx]
            # Floor against the default curve — site-specific value never falls
            # below floor_ratio × default. Prevents the "everything is fine
            # because everything is bad" feedback loop.
            curve[pos] = round(max(pct_val, default * floor_ratio), 2)
        else:
            curve[pos] = default
    return curve


def expected_ctr(position, curve=None):
    curve = curve or _DEFAULT_CURVE
    pos = max(1, min(20, int(round(position or 20))))
    return curve.get(pos, 1.5)


def ctr_gap(position, actual_ctr, curve=None):
    """Positive = under-earning clicks for its position (a real CTR problem)."""
    return round(expected_ctr(position, curve) - (actual_ctr or 0), 1)


def should_rewrite(avg_position, page_ctr, curve=None, threshold=CTR_GAP_THRESHOLD):
    """Return (bool, reason). A title/meta rewrite only moves CTR, so only
    propose one when the page ranks well enough to get impressions AND
    under-earns clicks for that position."""
    if avg_position is None:
        return True, 'no position data — analyze'
    if avg_position > 30:
        return False, f'barely ranks (pos {avg_position:.1f}) — a rewrite will not help; needs links/content'
    gap = ctr_gap(avg_position, page_ctr, curve)
    if gap <= threshold:
        return False, f'CTR already at/above expected (gap {gap:+.1f}pp at pos {avg_position:.1f})'
    return True, f'CTR gap {gap:+.1f}pp at pos {avg_position:.1f}'


# ── Gate 4: internal-linking candidates ─────────────────────────────────────
def needs_linking(avg_position):
    lo, hi = LINK_POS_BAND
    return avg_position is not None and lo < avg_position <= hi


def linking_candidates(target_page, all_pages, max_sources=5, brand_tokens=None):
    """Suggest published pages that could internally link to target_page, ranked
    by keyword/topic token overlap.

    brand_tokens — site-name words (e.g. {'dark','desires'} or {'nerdy','electronics'})
    that appear in nearly every title because of the WP site-name suffix. They get
    stripped from BOTH token sets so they no longer inflate overlap. A candidate
    is also REJECTED if its entire overlap is brand words (i.e. zero real topic
    tokens shared — that's a brand-bleed false positive, not editorial relevance).

    Returns: list of {slug, title, overlap, shared_tokens} sorted by overlap desc.
    """
    brand = set(t.lower() for t in (brand_tokens or [])) | _STOPWORDS
    t_slug = target_page.get('slug')
    t_tokens = set(_words(target_page.get('title', '')) +
                   _words(t_slug.replace('-', ' ') if t_slug else ''))
    for k in target_page.get('keywords', []):
        t_tokens |= set(_words(k.get('query', '')))
    t_tokens -= brand
    scored = []
    for p in all_pages:
        if p.get('slug') == t_slug:
            continue
        tokens = (set(_words(p.get('title', ''))) |
                  set(_words((p.get('slug') or '').replace('-', ' ')))) - brand
        shared = t_tokens & tokens
        if len(shared) >= 2:
            # Sort tokens for stable display (longest-first so the
            # most distinctive word appears first)
            shared_sorted = sorted(shared, key=lambda w: (-len(w), w))
            scored.append({'slug': p.get('slug'), 'title': p.get('title', ''),
                           'overlap': len(shared),
                           'shared_tokens': shared_sorted})
    scored.sort(key=lambda x: x['overlap'], reverse=True)
    return scored[:max_sources]
