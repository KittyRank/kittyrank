"""
report_generator.py — turn pipeline JSON outputs into a single readable report.

OSS-tier output. Replaces the interactive dashboard for users who just want
a deliverable they can open in a browser or send to a client.

Reads (from OUTPUT_DIR):
    - technical-audit.json
    - trend-analysis.json
    - backlink-audit.json
    - cornerstone-link-audit.json
    - page-buckets.json
    - proposed-fixes.json
    - merged-opportunities.json (optional, for summary numbers)

Writes:
    - seo-report.md   (markdown)
    - seo-report.html (standalone HTML, inline CSS, no external deps)

Usage:
    python report_generator.py
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import OUTPUT_DIR, SITE_DOMAIN, SITE_NAME


# ─── Encoding-safe print (Windows console fallback) ─────────────────────────
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


# ─── Bucket icons (used in Page Buckets section) ────────────────────────────
BUCKET_META = {
    'sleeping_giant': ('💤', '#0066cc', 'Sleeping giants', 'Rank well, CTR low — title rewrite wins'),
    'almost_there':   ('🎯', '#6f42c1', 'Almost there', 'Page 2 — push to page 1 with depth + links'),
    'converter':      ('🏆', '#28a745', 'Converters', 'PROTECT — do NOT rewrite. Use as link source.'),
    'dead_weight':    ('⚰️', '#6c757d', 'Dead weight', 'Consolidate, redirect, or delete'),
    'unclassified':   ('•',  '#adb5bd', 'Unclassified', 'Middle-ground pages'),
    'no_data':        ('○',  '#dee2e6', 'No data', 'No GSC/Bing keyword data this run'),
}

# Audit category metadata (mirror of dashboard)
AUDIT_CATEGORIES = [
    ('server_error_critical', '🚨', '#dc3545', 'Server errors (5xx)',
     'URGENT — these block indexing entirely.'),
    ('active_bleed', '⚠️', '#dc3545', '404s with active traffic',
     'Users hitting dead pages — either restore or request removal.'),
    ('stale_index', '⚠️', '#fd7e14', 'Stale index (404 but search engine shows 200)',
     'Search engine is stale — request removal so it re-evaluates.'),
    ('active_duplicate', '🔴', '#dc3545', 'Active duplicate URLs',
     'Multiple live URLs serving the same content — 301 redirect or canonical needed.'),
    ('redirect_not_consolidated', '⏳', '#0066cc', 'Pending consolidation',
     'Server 301 is in place but search engine has not updated — will self-resolve in 4-8 weeks.'),
    ('attachment_waste', '🗑️', '#ffc107', 'WordPress attachment pages indexed',
     'Yoast: Search Appearance → Media → Redirect attachment URLs = YES.'),
    ('sitemap_pollution', '🗑️', '#ffc107', 'Sitemap contains dead URLs',
     'Regenerate sitemap so it only lists 200 URLs.'),
    ('unprotected_high_traffic', '🔗', '#28a745', 'High-traffic pages with no backlinks',
     'Long-term outreach targets.'),
]

TREND_SIGNAL_META = {
    'audience_growth':              ('📈', '#28a745', 'Audience growth'),
    'viral_spike':                  ('🚀', '#0066cc', 'Viral spike'),
    'ctr_improvement_only':         ('⚙️', '#ffc107', 'CTR improvement only'),
    'title_degradation':            ('📉', '#fd7e14', 'Title degradation'),
    'ranking_loss':                 ('⚠️', '#dc3545', 'Ranking loss'),
    'ctr_problem_despite_ranking':  ('🔧', '#fd7e14', 'CTR problem despite ranking'),
    'stable':                       ('➖', '#6c757d', 'Stable'),
    'insufficient_data':            ('❓', '#6c757d', 'Insufficient data'),
}


# ─── Helpers ────────────────────────────────────────────────────────────────
def _load_json(filename):
    """Load a JSON output file from OUTPUT_DIR. Returns None if missing/invalid."""
    path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"  [WARN] failed to load {filename}: {e}")
        return None


def _md_table(headers, rows):
    """Render a markdown table from headers + rows."""
    if not rows:
        return ''
    out = '| ' + ' | '.join(str(h) for h in headers) + ' |\n'
    out += '|' + '|'.join('---' for _ in headers) + '|\n'
    for r in rows:
        out += '| ' + ' | '.join(str(c) for c in r) + ' |\n'
    return out


# ─── Markdown report ────────────────────────────────────────────────────────
def build_markdown():
    """Compose a single markdown document from all JSON inputs."""
    lines = []
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    # Header
    lines.append(f'# KittyRank Report — {SITE_NAME}')
    lines.append('')
    lines.append(f'**Site:** {SITE_DOMAIN}  ')
    lines.append(f'**Generated:** {now}')
    lines.append('')
    lines.append('---')
    lines.append('')

    # ─── 1. Trend Analysis ──────────────────────────────────────────────────
    trends = _load_json('trend-analysis.json')
    if trends and not trends.get('message'):
        lines.append('## 📊 Trend analysis (last 90 days, 3-period buckets)')
        lines.append('')
        for src in ('gsc', 'bing'):
            a = trends.get(src) or {}
            if a.get('error'):
                continue
            icon, _, label = TREND_SIGNAL_META.get(a.get('signal'), ('•', '#666', a.get('signal', '')))
            lines.append(f'### {a.get("name", src.upper())} — {icon} {label}')
            lines.append('')
            p = a.get('periods', {})
            d = a.get('deltas', {})
            rows = [
                ('first30', p.get('first30', {}).get('days', 0),
                 p.get('first30', {}).get('clicks_per_day', 0),
                 int(p.get('first30', {}).get('impr_per_day', 0)),
                 f"{p.get('first30', {}).get('ctr_avg', 0)}%",
                 p.get('first30', {}).get('position_avg', 0)),
                ('mid30',   p.get('mid30', {}).get('days', 0),
                 p.get('mid30', {}).get('clicks_per_day', 0),
                 int(p.get('mid30', {}).get('impr_per_day', 0)),
                 f"{p.get('mid30', {}).get('ctr_avg', 0)}%",
                 p.get('mid30', {}).get('position_avg', 0)),
                ('last30',  p.get('last30', {}).get('days', 0),
                 p.get('last30', {}).get('clicks_per_day', 0),
                 int(p.get('last30', {}).get('impr_per_day', 0)),
                 f"{p.get('last30', {}).get('ctr_avg', 0)}%",
                 p.get('last30', {}).get('position_avg', 0)),
            ]
            lines.append(_md_table(['period', 'days', 'clicks/d', 'impr/d', 'CTR', 'pos'], rows))
            lines.append('**Δ first30 → last30:** '
                         f"clicks {d.get('clicks_per_day_pct', '-')}% · "
                         f"impr {d.get('impr_per_day_pct', '-')}% · "
                         f"CTR {d.get('ctr_pct', '-')}% · "
                         f"pos {d.get('position_pct', '-')}%")
            lines.append('')
            lines.append(f'> {a.get("signal_message", "")}')
            lines.append('')

    # ─── 2. Technical Health ────────────────────────────────────────────────
    tech = _load_json('technical-audit.json')
    if tech:
        lines.append('## 🩺 Technical health audit')
        lines.append('')
        totals = tech.get('totals', {})
        lines.append(_md_table(
            ['URLs audited', 'Live 200s', '3xx', '4xx', '5xx', 'Findings'],
            [(totals.get('urls_audited', 0), totals.get('live_200', 0),
              totals.get('redirects_3xx', 0), totals.get('errors_4xx', 0),
              totals.get('errors_5xx', 0), totals.get('findings', 0))]
        ))
        lines.append('')
        # Group findings by rule
        grouped = {}
        for f in tech.get('findings', []):
            grouped.setdefault(f.get('rule', 'other'), []).append(f)
        for rule, icon, _, label, summary in AUDIT_CATEGORIES:
            items = grouped.get(rule, [])
            if not items:
                continue
            lines.append(f'### {icon} {label} ({len(items)})')
            lines.append(f'_{summary}_')
            lines.append('')
            for f in items[:15]:
                msg = f.get('message', '')
                lines.append(f"- **{msg}**")
                if f.get('url'):
                    lines.append(f"  - URL: `{f['url']}`")
                    if f.get('impressions'):
                        lines.append(f"  - impressions: {f['impressions']}, clicks: {f.get('clicks', 0)}")
                if f.get('duplicate_urls') or f.get('urls'):
                    for d in (f.get('duplicate_urls') or f.get('urls', []))[:3]:
                        lines.append(f"  - → `{d.get('url')}` "
                                     f"[HTTP {d.get('live_status') or d.get('csv_http_code', '?')}, "
                                     f"impr={d.get('impressions', 0)}]")
                if f.get('sample_urls'):
                    for u in f['sample_urls'][:3]:
                        if isinstance(u, dict):
                            lines.append(f"  - → `{u.get('url', u)}`")
                        else:
                            lines.append(f"  - → `{u}`")
                if f.get('fix'):
                    lines.append(f"  - **Fix:** {f['fix']}")
            if len(items) > 15:
                lines.append(f"  ... and {len(items) - 15} more")
            lines.append('')

    # ─── 3. Page Buckets ────────────────────────────────────────────────────
    buckets = _load_json('page-buckets.json')
    if buckets:
        lines.append('## 🎯 Page bucket classification')
        lines.append('')
        summary = buckets.get('summary', {})
        rows = []
        for key, (icon, _, label, _summary) in BUCKET_META.items():
            n = summary.get(key, 0)
            if n: rows.append((f'{icon} {label}', n))
        if rows:
            lines.append(_md_table(['Bucket', 'Count'], rows))
            lines.append('')
        for key, (icon, _, label, summary_text) in BUCKET_META.items():
            pages = buckets.get('buckets', {}).get(key, [])
            if not pages or key in ('unclassified', 'no_data'):
                continue
            lines.append(f'### {icon} {label} ({len(pages)})')
            lines.append(f'_{summary_text}_')
            lines.append('')
            top = pages[:10]
            tbl = [(p.get('slug', ''), p.get('impressions', 0), p.get('clicks', 0),
                    f"{p.get('ctr', 0)}%", p.get('position', '-')) for p in top]
            lines.append(_md_table(['Slug', 'Impr', 'Clicks', 'CTR', 'Pos'], tbl))
            if len(pages) > 10:
                lines.append(f'_+ {len(pages) - 10} more in JSON_')
            lines.append('')

    # ─── 4. Proposed Fixes (Claude) ─────────────────────────────────────────
    fixes = _load_json('proposed-fixes.json')
    if fixes:
        lines.append(f'## ✏️ Proposed fixes ({len(fixes)})')
        lines.append('')
        lines.append('Generated by Claude. The OSS edition does NOT apply these automatically — '
                     'copy the new title + meta into your WordPress SEO plugin manually.')
        lines.append('')
        for fix in fixes[:15]:
            slug = fix.get('slug', '')
            bucket = fix.get('bucket') or fix.get('track', '')
            icon = BUCKET_META.get(bucket, ('', '', '', ''))[0]
            lines.append(f"### {icon} `{slug}` — target: *{fix.get('target_keyword', '')}*")
            ch = fix.get('changes', {})
            if ch.get('title'):
                lines.append(f"  - **New title:** {ch['title'].get('new', '')}")
                if ch['title'].get('old'):
                    lines.append(f"    - was: {ch['title'].get('old')}")
            if ch.get('metadesc'):
                lines.append(f"  - **New meta:** {ch['metadesc'].get('new', '')}")
            if fix.get('intro_add'):
                lines.append(f"  - **Add to intro:** {fix['intro_add']}")
            if fix.get('reasoning'):
                lines.append(f"  - **Why:** {fix['reasoning']}")
            lines.append('')
        if len(fixes) > 15:
            lines.append(f'_+ {len(fixes) - 15} more in `proposed-fixes.json`_')
        lines.append('')

    # ─── 5. Backlink profile ────────────────────────────────────────────────
    backlinks = _load_json('backlink-audit.json')
    if backlinks:
        lines.append('## 🔗 Backlink profile')
        lines.append('')
        totals = backlinks.get('totals') or {}
        lines.append(f"**Total referring domains:** {totals.get('total_domains', 0)}")
        lines.append(f"**Total backlinks:** {totals.get('total_backlinks', 0)}")
        lines.append(f"**Pages with at least one backlink:** {totals.get('pages_with_backlinks', 0)}")
        lines.append('')
        domains = backlinks.get('top_domains', [])
        if domains:
            lines.append('### Top referring domains')
            rows = [(d.get('domain', ''), d.get('backlinks', 0), d.get('class', ''))
                    for d in domains[:10]]
            lines.append(_md_table(['Domain', 'Backlinks', 'Class'], rows))
            lines.append('')
        outreach = backlinks.get('outreach_targets', [])
        if outreach:
            lines.append('### Outreach targets (high impressions, 0 backlinks)')
            rows = [(t.get('url', ''), t.get('impressions', 0), t.get('clicks', 0))
                    for t in outreach[:10]]
            lines.append(_md_table(['URL', 'Impr', 'Clicks'], rows))
            lines.append('')

    # ─── 6. Cornerstone audit ───────────────────────────────────────────────
    corner = _load_json('cornerstone-link-audit.json')
    if corner:
        lines.append('## 🏛️ Cornerstone link audit')
        lines.append('')
        for entry in corner.get('audit', []):
            if entry.get('error'):
                continue
            type_label = entry.get('type_label', '')
            pri = entry.get('priority', '')
            lines.append(f"### {entry.get('cornerstone', '')}  *(Type {entry.get('type', '')} — "
                         f"{type_label}, priority: {pri})*")
            lines.append(f"  - Inbound links from other posts: **{entry.get('inbound_count', 0)}**")
            lines.append(f"  - Missing-link candidates: **{entry.get('missing_count', 0)}** "
                         f"({entry.get('missing_reciprocal_count', 0)} reciprocal + "
                         f"{entry.get('missing_overlap_only_count', 0)} overlap)")
            for m in (entry.get('missing') or [])[:5]:
                reasons = '/'.join(m.get('reasons', []))
                tok = ', '.join(m.get('shared_tokens') or [])[:60]
                lines.append(f"    - [{reasons}] overlap={m.get('overlap', 0)} `{m.get('slug', '')}` ({tok})")
            lines.append('')

    # ─── 7. Premium-upgrade CTA ─────────────────────────────────────────────
    lines.append('---')
    lines.append('')
    lines.append('### ✓ Audit complete. Want to skip the manual work?')
    lines.append('')
    lines.append('The OSS edition shows you WHAT to do. The Pro edition DOES it for you:')
    lines.append('')
    lines.append('- ⚡ One-click apply fixes to WordPress (REST API writes)')
    lines.append('- 📦 Batch operations (Accept All, Submit All Approved, Bulk Remove)')
    lines.append('- 🗑️ URL removal — Bing Block API for 404 cleanup')
    lines.append('- 🖥️ Interactive dashboard with grouped accordion findings')
    lines.append('- 🌐 Multi-site management for agencies')
    lines.append('- ⏰ Scheduled audit runs + email digest')
    lines.append('- 📄 Branded monthly PDF reports for clients')
    lines.append('- 🎨 White-label option for agency dashboards')
    lines.append('- 🔔 Slack / Discord webhook notifications')
    lines.append('')
    lines.append('**Learn more:** [github.com/KittyRank/kittyrank](https://github.com/KittyRank/kittyrank)')
    lines.append('')
    lines.append('---')
    lines.append(f'_Generated by the KittyRank (OSS Edition) on {now}._')

    return '\n'.join(lines)


# ─── HTML report (standalone, inline CSS, no external deps) ─────────────────
HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>KittyRank Report — {site}</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         line-height: 1.5; color: #333; max-width: 980px; margin: 30px auto;
         padding: 0 24px; background: #fafafa; }
  h1 { color: #1a1a2e; border-bottom: 3px solid #0066cc; padding-bottom: 10px; }
  h2 { color: #1a1a2e; margin-top: 36px; padding-top: 12px;
       border-top: 1px solid #ddd; }
  h3 { color: #444; margin-top: 24px; }
  h4 { color: #555; margin-top: 16px; }
  table { width: 100%; border-collapse: collapse; margin: 12px 0;
          background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  th { background: #f0f4f8; padding: 8px 12px; text-align: left;
       border-bottom: 2px solid #ddd; font-weight: 600; color: #555; }
  td { padding: 6px 12px; border-bottom: 1px solid #f0f0f0; font-size: 0.92em; }
  tr:hover td { background: #fafafa; }
  code { background: #f5f5f5; padding: 1px 5px; border-radius: 3px;
         font-family: Consolas, monospace; font-size: 0.9em; }
  blockquote { border-left: 4px solid #0066cc; padding: 10px 14px;
               background: #f0f7ff; color: #444; margin: 12px 0;
               font-style: italic; }
  ul li { margin: 4px 0; }
  .cta { background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px;
         padding: 18px 24px; margin-top: 32px; }
  .cta h3 { margin-top: 0; color: #856404; }
  .footer { color: #888; font-size: 0.85em; text-align: center;
            margin-top: 30px; padding-top: 12px; border-top: 1px solid #ddd; }
  a { color: #0066cc; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
"""

HTML_FOOT = """
</body>
</html>
"""


def _md_to_html(md):
    """Minimal markdown → HTML conversion. Enough for our generated content."""
    import re
    html_parts = []
    in_table = False
    table_rows = []
    in_list = False

    def flush_table():
        nonlocal in_table, table_rows
        if not in_table:
            return ''
        out = '<table>\n'
        for i, r in enumerate(table_rows):
            cells = [c.strip() for c in r.strip('|').split('|')]
            tag = 'th' if i == 0 else 'td'
            out += '  <tr>' + ''.join(f'<{tag}>{c}</{tag}>' for c in cells) + '</tr>\n'
        out += '</table>\n'
        in_table = False
        table_rows = []
        return out

    def flush_list():
        nonlocal in_list
        if in_list:
            in_list = False
            return '</ul>\n'
        return ''

    for line in md.splitlines():
        # Table detection
        if line.strip().startswith('|') and '|' in line.strip()[1:]:
            # Skip separator line (|---|---|)
            if re.match(r'^\|\s*[-:]+\s*(\|\s*[-:]+\s*)+\|?\s*$', line.strip()):
                continue
            if not in_table:
                in_table = True
                html_parts.append(flush_list())
            table_rows.append(line)
            continue
        else:
            if in_table:
                html_parts.append(flush_table())
        # Headers
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            html_parts.append(flush_list())
            level = len(m.group(1))
            text = _inline_md(m.group(2))
            html_parts.append(f'<h{level}>{text}</h{level}>\n')
            continue
        # Bullet list
        if re.match(r'^\s*[-*]\s+', line):
            if not in_list:
                in_list = True
                html_parts.append('<ul>\n')
            item = re.sub(r'^\s*[-*]\s+', '', line)
            html_parts.append(f'  <li>{_inline_md(item)}</li>\n')
            continue
        else:
            html_parts.append(flush_list())
        # Blockquote
        if line.startswith('>'):
            text = _inline_md(line[1:].strip())
            html_parts.append(f'<blockquote>{text}</blockquote>\n')
            continue
        # Horizontal rule
        if line.strip() in ('---', '***'):
            html_parts.append('<hr>\n')
            continue
        # Plain paragraph
        if line.strip():
            html_parts.append(f'<p>{_inline_md(line)}</p>\n')
        else:
            html_parts.append('\n')

    # Flush trailing structures
    if in_table:
        html_parts.append(flush_table())
    if in_list:
        html_parts.append('</ul>\n')

    return ''.join(html_parts)


def _inline_md(text):
    """Inline markdown: bold, italic, code, links."""
    import re
    # Code FIRST (so we don't process * inside code)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Links [text](url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    # Bold **text**
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    # Italic _text_ (only between word boundaries)
    text = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'<em>\1</em>', text)
    return text


def build_html(md):
    """Wrap converted markdown in the HTML shell. Uses str.replace instead of
    .format() because the CSS contains literal { } that .format() would parse."""
    body = _md_to_html(md)
    # Re-style the upgrade CTA section
    body = body.replace('<h3>✓ Audit complete', '<div class="cta"><h3>✓ Audit complete')
    body = body.replace('<hr>\n<p><em>Generated by',
                        '</div>\n<div class="footer"><em>Generated by')
    body = body.rsplit('</p>', 1)[0] + '</em></div>'
    head = HTML_HEAD.replace('{site}', SITE_NAME)
    return head + body + HTML_FOOT


# ─── Main ──────────────────────────────────────────────────────────────────
def generate():
    print(f'[REPORT] Generating SEO report for {SITE_DOMAIN}...')
    md = build_markdown()
    md_path = os.path.join(OUTPUT_DIR, 'seo-report.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f'  Markdown: {md_path}')
    html_path = os.path.join(OUTPUT_DIR, 'seo-report.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(build_html(md))
    print(f'  HTML:     {html_path}')
    print(f'\n  Open the HTML file in your browser to view the report.')


if __name__ == '__main__':
    if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    generate()
