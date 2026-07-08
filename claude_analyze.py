"""
Step 2b: Claude API analysis of pages against their target keywords.
Reads scraped page data + GSC opportunities, calls Claude to propose fixes.

Outputs: proposed-fixes.json (same format as FIXES in apply.py)

Supports two backends:
  - AWS Bedrock (default for AWS deployment, single billing)
  - Direct Anthropic API (for local testing)

Set USE_BEDROCK=1 for Bedrock, or ANTHROPIC_API_KEY for direct API.

Designed to run in AWS Lambda, EC2, or locally.
"""

import json
import os
import sys
import time
import anthropic

sys.path.insert(0, os.path.dirname(__file__))
from config import *
import seo_quality

SYSTEM_PROMPT = f"""You are a conversion-focused SEO strategist for {SITE_DESCRIPTION}.
Your job is not just to include keywords — it is to write titles and meta descriptions that
make someone choose THIS page over the 9 other results on the page.

## The core problem you are solving
Most technical pages rank at position 4-10 but have CTR below 1%. This means the page IS
visible in Google but people are NOT clicking it. The title is failing as an advertisement.
The fix is never just "include the keyword" — every competitor already does that. The fix is
to promise something SPECIFIC that the reader actually wants and that competing pages don't.

## CTR benchmarks (organic search)
- Position 1: ~28% CTR | Position 3: ~10% | Position 5: ~6% | Position 10: ~2%
- If a page at position 5-8 has CTR below 2%, the title is underperforming significantly
- If CTR is below 1% at any position, the title is essentially invisible despite ranking

## Title writing rules
- MUST be under 60 characters (Google truncates at ~580px)
- Include the primary keyword naturally — but this alone is NOT enough
- Make a SPECIFIC promise: what will the reader know/be able to do after reading?
- Use specifics: numbers, comparisons, "how to fix X", "why Y fails" — these outperform generic titles
- BAD: "DHT11 and DHT22 Sensor: Working Principle and Interfacing Guide" (generic, same as every competitor)
- GOOD: "DHT11 vs DHT22: Wiring, Code, and Why Your Reads Fail" (specific, differentiating, implies problems solved)
- BAD: "Introduction to GPIO in Embedded Systems" (no promise)
- GOOD: "GPIO in Embedded Systems: Registers, Modes, and Control" (specific structure)
- Do NOT use "Guide", "Tutorial", "Introduction", "Overview" alone — they signal generic content

## Meta description rules
- MUST be 145-155 characters
- Lead with the problem the reader is trying to solve — not the topic
- Include the primary keyword
- End with a specific promise of what they'll learn or be able to do
- BAD: "Learn about DHT11 and DHT22 sensors and how they work with Arduino."
- GOOD: "Understand how DHT sensors measure humidity and temperature, compare DHT11 vs DHT22 specs, and fix common checksum errors in your Arduino code."

## High bounce rate signal
- If bounce rate is above 60%: the title/meta is attracting the wrong audience OR content doesn't match the promise
- In this case, the title rewrite must better match what the content ACTUALLY delivers

## Technical rules
- Do NOT change the WordPress H1 — only the Yoast SEO title (shown in Google search results)
- Keep the site's technical, educational tone
- Only propose intro_add if the keyword is genuinely absent from the intro
- NEVER invent content specifics — if you do not see it in the content outline, do not reference it
"""

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "The post slug"},
        "target_keyword": {"type": "string", "description": "Primary keyword to target"},
        "yoast_title": {
            "type": "string",
            "description": "New Yoast SEO title, max 60 chars. Must contain target keyword."
        },
        "yoast_metadesc": {
            "type": "string",
            "description": "New meta description, 145-155 chars. Must contain target keyword."
        },
        "intro_add": {
            "type": ["string", "null"],
            "description": "A single sentence to add before the intro paragraph. Set null if keyword already in intro."
        },
        "ctr_diagnosis": {
            "type": "string",
            "description": "One sentence explaining why the current CTR is low"
        },
        "content_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Subtopics that expand the post's EXISTING subject area, suggested by keyword data. Must stay within the same topic — do NOT suggest pivoting to different technologies, chip families, or subjects not already covered by the post."
        },
        "reasoning": {
            "type": "string",
            "description": "Brief explanation of why the rewrite will outperform the current title"
        }
    },
    "required": ["slug", "target_keyword", "yoast_title", "yoast_metadesc", "intro_add", "reasoning"]
}


def _format_outline(outline):
    """Format content outline for Claude prompt."""
    if not outline:
        return '(no outline available)'
    if isinstance(outline[0], str):
        # Old format: plain H2 list
        return '\n'.join(f'  H2: {h}' for h in outline)
    lines = []
    for item in outline:
        indent = '  ' if item.get('level') == 'h2' else '    '
        tag = item.get('level', 'h2').upper()
        lines.append(f"{indent}{tag}: {item['heading']}")
        if item.get('first_sentence'):
            lines.append(f"{indent}     → {item['first_sentence']}")
    return '\n'.join(lines)


def select_target_keyword(keywords):
    """
    Pick the best keyword to optimize for using position-tiered scoring.

    Tier 1 (pos 5-15): hanging fruit — page is already visible, CTR improvement
                        has immediate measurable impact. Highest priority.
    Tier 2 (pos 15-25): near-miss — could reach page 1 with content + CTR work.
    Tier 3 (pos <5):    already winning — CTR improvement still helps but post
                        doesn't need a rewrite.
    Tier 4 (pos >25):   far out — informational only, never use as primary target.

    Within each tier, sort by impressions (more eyes = more upside).
    Returns (query_string, position, tier_number).
    """
    if not keywords:
        return '', 0, 4

    # Gate 2: drop junk/garbled/low-signal queries before picking a target
    keywords = seo_quality.filter_keywords(keywords)
    if not keywords:
        return '', 0, 4

    # Opportunity-based: pick the highest-DEMAND keyword the page actually ranks
    # for (impressions = real search volume), not the rigid position tier. This
    # stops a low-volume pos-6 query being chosen over a high-volume pos-4 one.
    rankable = [k for k in keywords if k['position'] <= 25] or keywords
    best = max(rankable, key=lambda k: k.get('impressions', 0))
    pos = best['position']
    tier = 1 if 5 <= pos <= 15 else (2 if 15 < pos <= 25 else (3 if pos < 5 else 4))
    return best['query'], pos, tier


def get_position_strategy(tier, position):
    """
    Return the optimization strategy instruction based on keyword tier.
    Tier 1 (pos 5-15): CTR-only — no content gaps, just better advertisement.
    Tier 2 (pos 15-25): CTR + content gaps that deepen existing coverage.
    Tier 3 (pos <5):   CTR improvement only — already ranking, don't risk it.
    Tier 4 (pos >25):  CTR focus; content gaps only if they extend the core topic.
    """
    if tier == 1:
        return (
            f"This keyword is at position {position:.1f} — HANGING FRUIT. "
            f"The page is already visible on page 1. Users are seeing it but not clicking. "
            f"Your ONLY job here is to make the title and meta more compelling. "
            f"Do NOT suggest content gaps — the content is already good enough to rank. "
            f"Set content_gaps to an empty array []."
        )
    elif tier == 2:
        return (
            f"This keyword is at position {position:.1f} — NEAR MISS. "
            f"The page is on page 2 or the bottom of page 1. "
            f"Improve CTR with a better title/meta AND suggest 2-3 content gaps "
            f"that would deepen the post's existing coverage within its primary topic. "
            f"Content gaps must extend what's already there — not pivot to a new subject."
        )
    elif tier == 3:
        return (
            f"This keyword is at position {position:.1f} — already ranking well. "
            f"Focus on CTR improvement. Do not suggest content changes that could "
            f"disrupt the existing ranking. Set content_gaps to []."
        )
    else:
        return (
            f"This keyword is at position {position:.1f} — the page barely ranks here. "
            f"Do NOT use this keyword to suggest a content pivot. "
            f"Focus on CTR improvement for the page's primary audience. "
            f"If you suggest content gaps, they must strictly extend the post's core topic, "
            f"not chase this distant keyword."
        )


def analyze_page_with_claude(client, page_data, model=None):
    """
    Send a single page's data to Claude for SEO analysis.
    Returns proposed fix dict.
    """
    slug = page_data['slug']
    keywords = page_data['keywords']
    top_kw, top_kw_pos, top_kw_tier = select_target_keyword(keywords)
    track = page_data.get('track', 'ctr')

    # Calculate page-level CTR from keyword data
    total_impr = sum(k.get('impressions', 0) for k in keywords)
    total_clicks = sum(k.get('clicks', 0) for k in keywords)
    page_ctr = round(total_clicks / total_impr * 100, 2) if total_impr > 0 else 0
    avg_pos = round(sum(k['position'] * k.get('impressions', 1) for k in keywords) / max(total_impr, 1), 1)

    # Opportunity floor: no real keyword AND barely any impressions -> nothing for
    # a title/meta rewrite to act on. Skip rather than propose a weak change.
    if not top_kw and total_impr < globals().get('MIN_PAGE_IMPRESSIONS', 25):
        print(f"  [SKIP] {slug} — no keyword opportunity ({total_impr} impr)")
        return None

    # CTR benchmark assessment
    expected_ctr = {1: 28, 2: 15, 3: 10, 4: 8, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2.5, 10: 2}
    pos_bucket = min(10, max(1, round(avg_pos)))
    expected = expected_ctr.get(pos_bucket, 2)
    ctr_gap = round(expected - page_ctr, 1)
    if ctr_gap > 3:
        ctr_verdict = f"SEVERELY underperforming (expected ~{expected}% at position {avg_pos}, actual {page_ctr}% — gap of {ctr_gap}pp). Title is almost certainly the problem."
    elif ctr_gap > 1:
        ctr_verdict = f"Underperforming (expected ~{expected}% at position {avg_pos}, actual {page_ctr}%). Title needs differentiation."
    else:
        ctr_verdict = f"Reasonable CTR ({page_ctr}% vs ~{expected}% expected at position {avg_pos})."

    # GA4 metrics
    bounce_rate = page_data.get('bounce_rate')
    sessions = page_data.get('sessions')
    avg_duration = page_data.get('avg_session_duration')
    ga4_section = ''
    if bounce_rate is not None:
        dur_str = f"{int(avg_duration//60)}m {int(avg_duration%60)}s" if avg_duration else 'N/A'
        bounce_verdict = 'HIGH — title may be attracting wrong audience, or content does not match promise' if bounce_rate >= 60 else ('MODERATE' if bounce_rate >= 45 else 'ACCEPTABLE')
        ga4_section = f"""
## User Engagement (Google Analytics 4)
- Sessions: {sessions} | Avg Session Duration: {dur_str}
- Bounce Rate: {bounce_rate}% ({bounce_verdict})
"""

    # Previous fix history
    prev_fix = page_data.get('previous_fix', {})
    history_section = ''
    if prev_fix:
        days_ago = prev_fix.get('days_ago', '?')
        prev_title = prev_fix.get('yoast_title', 'N/A')
        prev_ctr = prev_fix.get('ctr_at_fix', 'N/A')
        history_section = f"""
## Previous Fix History
- Last fix applied: {days_ago} days ago
- Previous title set to: "{prev_title}"
- CTR at time of fix: {prev_ctr}%
- Current CTR: {page_ctr}%
- {"CTR has NOT improved since last fix — the previous rewrite did not work. Take a fundamentally different approach." if prev_ctr != 'N/A' and page_ctr <= prev_ctr else "CTR has improved since last fix."}
"""

    if track == 'ranking':
        track_directive = (
            "This page is in STRIKING DISTANCE (ranks ~8-30) — its click-through is "
            "already fine for its position, so the goal is to rank HIGHER, not to chase "
            "clicks. Make the title and H1-aligned meta match the target keyword's intent "
            "more precisely (relevance, not clickbait). In 'reasoning', name the 1-2 "
            "internal-link or content-depth moves most likely to lift its ranking. "
            "Do NOT keyword-stuff or over-promise.")
    else:
        track_directive = ("This page is visible in Google but under-earns clicks for its "
                           "position. Goal: a title + meta that makes people CHOOSE this page "
                           "over competitors, based ONLY on what the page actually covers.")

    prompt = f"""You are optimizing a page's SEO title and meta description.
{track_directive}

## Page Performance Summary
- URL: {page_data['url']}
- Page-level CTR: {page_ctr}% | Total Impressions: {total_impr} | Total Clicks: {total_clicks}
- Average Position: {avg_pos}
- CTR Assessment: {ctr_verdict}

## Current Title & Meta
- Title: {page_data.get('title', 'N/A')} ({page_data.get('title_length', '?')} chars)
- Meta Description: {page_data.get('meta_description', 'N/A')} ({page_data.get('meta_description_length', '?')} chars)
- H1: {page_data.get('h1', 'N/A')}
- Word Count: {page_data.get('word_count', 'N/A')} | Internal Links: {page_data.get('internal_links', 'N/A')}
- First Paragraph: {page_data.get('intro_preview', '')[:500]}

## What This Post ACTUALLY Covers (scraped from live page — use ONLY this, never invent)
{_format_outline(page_data.get('content_outline', page_data.get('h2s', [])))}
{ga4_section}{history_section}
## Keywords This Page Ranks For
| Keyword | Position | Impressions | Clicks | CTR | Source |
|---------|----------|-------------|--------|-----|--------|
"""
    for kw in keywords[:10]:
        prompt += f"| {kw['query']} | {kw['position']} | {kw['impressions']} | {kw['clicks']} | {kw.get('ctr', 0)}% | {kw.get('source', 'gsc')} |\n"

    prompt += f"""
## SEO Issues Detected
{json.dumps(page_data.get('issues', []), indent=2, ensure_ascii=False)}

## Post Primary Topic
The post's primary topic is defined by its title and H1:
- Title: {page_data.get('title', 'N/A')}
- H1: {page_data.get('h1', 'N/A')}

A keyword ranking for this page does NOT mean the post is about that keyword.
It means users with that intent found this page. Optimize for users who want the PRIMARY topic.
If the target keyword is a specific sub-topic (e.g., a chip family, a brand, a narrower concept)
that the post MENTIONS but does not FOCUS on — treat the post's primary topic as the real target,
and note the mismatch in your ctr_diagnosis.

## Optimization Strategy
Target keyword: "{top_kw}" (position {top_kw_pos:.1f})
{get_position_strategy(top_kw_tier, top_kw_pos)}

Set "target_keyword" in your JSON to EXACTLY: "{top_kw}" — do not substitute a different keyword.

CRITICAL RULES:
1. Title, meta, and intro MUST only reference topics explicitly shown in the content outline above. Never invent technologies, chip families, or specifics the post does not cover.
2. A keyword in the table does NOT mean the post should pivot to it. Serve the primary audience.
3. Content gaps (when applicable) must extend the post's EXISTING subject area — not chase a ranking keyword that represents a different topic.

Write a title and meta description that:
1. Identifies WHY the current CTR is low
2. Rewrites with a SPECIFIC promise based ONLY on what the post actually covers
3. Counts characters exactly — title ≤60 chars, meta 145-155 chars

Return ONLY this JSON object (no markdown, no extra text):
{{
  "slug": "{slug}",
  "target_keyword": "{top_kw}",
  "yoast_title": "new title (≤60 chars, specific, based only on actual content)",
  "yoast_metadesc": "new meta description (145-155 chars, leads with reader problem)",
  "intro_add": "sentence to prepend to intro, or null",
  "ctr_diagnosis": "one sentence explaining why current CTR is low",
  "content_gaps": [],
  "reasoning": "why your rewrite will outperform the current title"
}}
"""

    # Retry with backoff for overloaded/rate-limit errors
    for attempt in range(4):
        try:
            response = client.messages.create(
                model=model or DIRECT_MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )
            break
        except (anthropic._exceptions.OverloadedError, anthropic.RateLimitError) as e:
            wait = (attempt + 1) * 15
            print(f"    [RETRY] {e.__class__.__name__}, waiting {wait}s (attempt {attempt+1}/4)")
            time.sleep(wait)
    else:
        print(f"    [ERROR] All retries failed for {slug}")
        return None

    # Parse response
    text = response.content[0].text.strip()

    # Strip markdown fences if present
    if text.startswith('```'):
        text = text.split('\n', 1)[1]  # Remove first line
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()

    try:
        fix = json.loads(text)
    except json.JSONDecodeError:
        print(f"  [WARN] Failed to parse Claude response for {slug}")
        print(f"  Response: {text[:200]}")
        return None

    # Enforce target keyword — Claude must not override it
    if fix.get('target_keyword', '').lower() != top_kw.lower():
        print(f"  [WARN] Claude changed target_keyword from '{top_kw}' to '{fix.get('target_keyword')}' — overriding")
        fix['target_keyword'] = top_kw

    # Store tier metadata in fix for dashboard display
    tier_labels = {1: 'hanging fruit', 2: 'near-miss', 3: 'already ranking', 4: 'far out'}
    fix['keyword_tier'] = top_kw_tier
    fix['keyword_tier_label'] = tier_labels.get(top_kw_tier, 'unknown')
    fix['keyword_position'] = round(top_kw_pos, 1)
    print(f"  [TIER {top_kw_tier}] {tier_labels.get(top_kw_tier, '?')} — pos {top_kw_pos:.1f}: '{top_kw}'")

    # Gate 1: validate generated text. Salvage slightly-long output by trimming
    # whole words; never emit a truncated/dangling title (e.g. "...Intimacy with").
    warnings = []
    title = (fix.get('yoast_title') or '').strip()
    meta = (fix.get('yoast_metadesc') or '').strip()

    if len(title) > 60:
        salvaged = seo_quality.safe_shorten_title(title, 60)
        if salvaged:
            print(f"  [FIX] Title {len(title)}c -> {len(salvaged)}c: {salvaged}")
            title = salvaged
        else:
            title = ''
    ok_t, why_t = seo_quality.validate_title(title, keyword=top_kw)
    if ok_t:
        fix['yoast_title'] = title
    else:
        warnings.append(f"title rejected ({why_t})")
        fix['yoast_title'] = ''
        print(f"  [REJECT] title for {slug} — {why_t}: {title!r}")

    if len(meta) > 160:
        salvaged = seo_quality.safe_shorten_meta(meta, 160)
        meta = salvaged or ''
    ok_m, why_m = seo_quality.validate_metadesc(meta, keyword=top_kw)
    if ok_m:
        fix['yoast_metadesc'] = meta
    else:
        warnings.append(f"meta rejected ({why_m})")
        fix['yoast_metadesc'] = ''
        print(f"  [REJECT] meta for {slug} — {why_m}")

    if warnings:
        fix['quality_warning'] = '; '.join(warnings)
    if not fix.get('yoast_title') and not fix.get('yoast_metadesc') and not fix.get('intro_add'):
        print(f"  [SKIP] {slug} — no usable change after quality gate")
        return None
    elif len(meta) < 140:
        print(f"  [WARN] Meta too short ({len(meta)}c): {meta[:60]}...")

    # Tag with track / opportunity / linking data for the dashboard + history
    fix['track'] = track
    if page_data.get('opportunity_score') is not None:
        fix['opportunity_score'] = page_data['opportunity_score']
    if track == 'ranking' and page_data.get('link_from'):
        fix['link_from'] = page_data['link_from']

    # Engagement-collapse flag: very high bounce + near-zero dwell time is a
    # content/intent problem a title rewrite will NOT fix — flag, don't pretend.
    if (bounce_rate is not None and bounce_rate >= 70
            and avg_duration is not None and avg_duration <= 10):
        fix['engagement_warning'] = (
            f"High bounce ({bounce_rate}%) + {int(avg_duration)}s avg time — likely a "
            f"content/intent problem; a title rewrite probably won't help. Review the page.")

    return fix


def run_claude_analysis(analysis_path=None, opportunities_path=None, output_path=None):
    """
    Main function. Reads analysis + opportunities, calls Claude, outputs fixes.
    Can be called from Lambda handler or CLI.
    """
    # Resolve paths
    base_dir = os.environ.get('OUTPUT_DIR', os.path.join(os.path.dirname(__file__), 'output'))

    if not analysis_path:
        analysis_path = os.path.join(base_dir, 'analysis.json')
    if not opportunities_path:
        # Prefer merged (GSC + Bing) over GSC-only
        merged = os.path.join(base_dir, 'merged-opportunities.json')
        opportunities_path = merged if os.path.exists(merged) else os.path.join(base_dir, 'opportunities.json')
    if not output_path:
        output_path = os.path.join(base_dir, 'proposed-fixes.json')

    # Load data
    with open(analysis_path, 'r', encoding='utf-8') as f:
        analysis = json.load(f)

    with open(opportunities_path, 'r', encoding='utf-8') as f:
        opportunities = json.load(f)

    # Merge: add keyword data from opportunities into analysis
    opp_by_slug = {}
    for opp in opportunities:
        opp_by_slug[opp['slug']] = opp

    for page in analysis:
        slug = page['slug']
        if slug in opp_by_slug:
            opp = opp_by_slug[slug]
            page['keywords'] = opp['keywords']
            # Attach GA4 metrics if available
            if opp.get('bounce_rate') is not None:
                page['bounce_rate'] = opp['bounce_rate']
                page['sessions'] = opp.get('sessions', 0)
                page['avg_session_duration'] = opp.get('avg_session_duration', 0)
        # Add intro preview from first paragraph if available
        if 'intro_preview' not in page:
            page['intro_preview'] = ''

    # Filter to pages that need fixes. Include either:
    #   (a) seo_score < 80 — technical SEO problems exist
    #   (b) significant CTR gap at decent position — title/meta is failing as advertisement
    # WITHOUT (b), pages with perfect SEO basics but boring titles get silently dropped
    # even when they rank #5 with 1% CTR (~5pp below expected). Those are the highest-
    # leverage rewrites on the whole site — they're already visible, just need a better title.
    def _has_ctr_gap(p, min_impr=30, min_gap_pp=2.0):
        kws = p.get('keywords', [])
        if not kws:
            return False
        impr = sum(k.get('impressions', 0) for k in kws)
        if impr < min_impr:
            return False
        clicks = sum(k.get('clicks', 0) for k in kws)
        ctr = (clicks / impr * 100) if impr else 0
        pos = sum(k.get('position', 0) * k.get('impressions', 1) for k in kws) / max(impr, 1)
        if pos > 20 or pos < 1:
            return False
        # Use the default CTR curve directly to avoid building one twice; will be re-evaluated by should_rewrite later
        from seo_quality import expected_ctr
        return (expected_ctr(pos) - ctr) >= min_gap_pp

    pages_to_fix = [
        p for p in analysis
        if p.get('keywords') and (p.get('seo_score', 100) < 80 or _has_ctr_gap(p))
    ]
    ctr_only_n = sum(1 for p in pages_to_fix if p.get('seo_score', 100) >= 80)
    if ctr_only_n:
        print(f"[CLAUDE] Including {ctr_only_n} pages with score>=80 but significant CTR gap (high-leverage rewrites)")

    # Skip pages that were recently fixed (cooldown period)
    COOLDOWN_DAYS = 45  # GSC takes ~28 days to reflect changes + buffer
    state_path = os.path.join(base_dir, 'review-state.json')
    recently_fixed = set()
    if os.path.exists(state_path):
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                state_data = json.load(f)
            applied_dates = state_data.get('applied_dates', {})
            statuses = state_data.get('statuses', {})
            from datetime import datetime, timedelta
            cutoff = datetime.now() - timedelta(days=COOLDOWN_DAYS)
            for slug, date_str in applied_dates.items():
                try:
                    applied_dt = datetime.fromisoformat(date_str)
                    if applied_dt > cutoff:
                        recently_fixed.add(slug)
                except (ValueError, TypeError):
                    pass
            # Also skip rejected pages (user explicitly said no)
            for slug, status in statuses.items():
                if status == 'rejected':
                    recently_fixed.add(slug)
        except Exception:
            pass

    if recently_fixed:
        before = len(pages_to_fix)
        pages_to_fix = [p for p in pages_to_fix if p['slug'] not in recently_fixed]
        skipped = before - len(pages_to_fix)
        if skipped:
            print(f"[CLAUDE] Skipped {skipped} pages (fixed within last {COOLDOWN_DAYS} days or rejected)")

    # Build 1: closed-loop feedback — don't churn pages whose last fix worked.
    try:
        from feedback import evaluate_outcomes
        _, outcomes = evaluate_outcomes(base_dir)
    except Exception:
        outcomes = {}
    keep_winners = {s for s, r in outcomes.items() if r.get('verdict') == 'improved'}
    regressed = [s for s, r in outcomes.items() if r.get('verdict') == 'regressed']
    if keep_winners:
        pages_to_fix = [p for p in pages_to_fix if p['slug'] not in keep_winners]
        print(f"[CLAUDE] Left {len(keep_winners)} winning pages untouched (last fix improved them)")
    if regressed:
        print(f"[CLAUDE] {len(regressed)} pages regressed after a prior fix — "
              f"see outcome-report.json (revertable: history stores pre-fix values)")

    # Builds 2+3: decide HOW to treat each page, then prioritize by opportunity.
    #   ctr     — ranks well + under-earns clicks -> title/meta rewrite (CTR play)
    #   ranking — striking distance (pos ~8-30), CTR already fine -> relevance +
    #             internal links + content depth (the lever that moves RANK)
    #   skip    — already winning, or barely ranks (needs backlinks/big content)
    ctr_curve = seo_quality.build_ctr_curve(
        [k for p in analysis for k in p.get('keywords', [])])

    def _page_metrics(p):
        kws = p.get('keywords', [])
        impr = sum(k.get('impressions', 0) for k in kws)
        clicks = sum(k.get('clicks', 0) for k in kws)
        ctr = round(clicks / impr * 100, 2) if impr > 0 else 0
        pos = round(sum(k.get('position', 0) * k.get('impressions', 1) for k in kws)
                    / max(impr, 1), 1) if kws else None
        return pos, ctr, impr

    keep = []
    for p in pages_to_fix:
        pos, ctr, impr = _page_metrics(p)
        do_rewrite, reason = seo_quality.should_rewrite(pos, ctr, ctr_curve)
        if do_rewrite:
            # Page ranks decently (pos 1-10ish) but CTR is below curve — title/meta fix
            p['track'] = 'ctr'          # back-compat — old code reads this
            p['bucket'] = 'sleeping_giant'
            p['opportunity_score'] = round(impr * max(seo_quality.ctr_gap(pos, ctr, ctr_curve), 0.1), 1)
            keep.append(p)
        elif pos is not None and 8 <= pos <= 30:
            # Striking distance (page 2) — push RANK with depth + internal links
            p['track'] = 'ranking'      # back-compat
            p['bucket'] = 'almost_there'
            p['link_from'] = seo_quality.linking_candidates(
                p, analysis,
                brand_tokens=globals().get('SITE_BRAND_TOKENS', []))
            proximity = max(0.05, (31 - pos) / 30.0)   # closer to page 1 = bigger upside
            p['opportunity_score'] = round(max(impr, 1) * proximity, 1)
            p['skip_reason'] = reason
            keep.append(p)
        else:
            print(f"  [SKIP] {p['slug']} — {reason}")

    # Side-track: classify ALL analyzed pages into universal buckets
    # (sleeping_giant / almost_there / converter / dead_weight) for the
    # dashboard's Bucket panel — even pages we won't rewrite this run.
    _classify_all_pages_to_buckets(analysis, ctr_curve, output_dir=base_dir)

    # Build 3: highest-ROI first, and cap per run so each batch stays attributable
    # when the feedback loop grades it later.
    keep.sort(key=lambda p: p.get('opportunity_score', 0), reverse=True)
    cap = globals().get('MAX_FIXES_PER_RUN', 25)
    if len(keep) > cap:
        print(f"[CLAUDE] Capped to top {cap} by opportunity score "
              f"({len(keep) - cap} deferred to next run)")
        keep = keep[:cap]
    n_ctr = sum(1 for p in keep if p.get('track') == 'ctr')
    n_rank = sum(1 for p in keep if p.get('track') == 'ranking')
    print(f"[CLAUDE] {len(keep)} pages selected — {n_ctr} CTR-track, {n_rank} ranking-track")
    pages_to_fix = keep

    # Internal-linking worksheet for the ranking-track pages
    link_suggestions = [
        {'slug': p['slug'], 'title': p.get('title', ''), 'track': 'ranking',
         'avg_position': _page_metrics(p)[0],
         'opportunity_score': p.get('opportunity_score'),
         'reason': p.get('skip_reason', ''), 'link_from': p.get('link_from', [])}
        for p in keep if p.get('track') == 'ranking' and p.get('link_from')
    ]
    if link_suggestions:
        link_path = os.path.join(base_dir, 'linking-suggestions.json')
        with open(link_path, 'w', encoding='utf-8') as f:
            json.dump(link_suggestions, f, indent=2, ensure_ascii=False)
        print(f"[CLAUDE] {len(link_suggestions)} ranking-track pages -> "
              f"internal-linking worksheet {link_path}")

    # Load fix history (metrics at time of last fix) for feedback loop
    fix_history = {}
    history_path = os.path.join(base_dir, 'fix-history.json')
    if os.path.exists(history_path):
        try:
            with open(history_path, 'r', encoding='utf-8') as f:
                fix_history = json.load(f)
        except Exception:
            pass

    # Attach previous fix history to each page
    from datetime import datetime
    today = datetime.now()
    for page in pages_to_fix:
        slug = page['slug']
        if slug in fix_history:
            hist = fix_history[slug]
            try:
                applied_dt = datetime.fromisoformat(hist['applied_date'])
                days_ago = (today - applied_dt).days
            except Exception:
                days_ago = '?'
            page['previous_fix'] = {
                'days_ago': days_ago,
                'yoast_title': hist.get('yoast_title', ''),
                'ctr_at_fix': hist.get('ctr_at_fix', 'N/A'),
            }

    # Initialize Claude client — Bedrock or direct API
    if USE_BEDROCK:
        MODEL = BEDROCK_MODEL
        client = anthropic.AnthropicBedrock(aws_region=AWS_REGION)
        print(f"[CLAUDE] Using AWS Bedrock ({AWS_REGION})")
    else:
        MODEL = DIRECT_MODEL
        if not ANTHROPIC_API_KEY:
            print("[ERROR] Set ANTHROPIC_API_KEY in config.py or environment")
            return []
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        print(f"[CLAUDE] Using direct Anthropic API")

    print(f"[CLAUDE] Analyzing {len(pages_to_fix)} pages — Model: {MODEL}\n")

    # Each analysis is a fresh RUN: stamp a run_id, and regenerate rather than
    # silently re-showing a previous run's unapplied proposals. The only time we
    # keep prior results is to RESUME a run that was interrupted (e.g. API died)
    # within the last few hours.
    from datetime import datetime as _dt
    run_marker = os.path.join(base_dir, 'analysis-run.json')
    prev = {}
    if os.path.exists(run_marker):
        try:
            with open(run_marker, 'r', encoding='utf-8') as f:
                prev = json.load(f)
        except Exception:
            prev = {}
    resume = False
    if prev.get('status') == 'running' and os.path.exists(output_path):
        try:
            age_h = (_dt.now() - _dt.fromisoformat(prev['started'])).total_seconds() / 3600
            resume = age_h < 3
        except Exception:
            resume = False

    fixes, already_done = [], set()
    if resume:
        run_id = prev.get('run_id')
        started = prev.get('started')
        with open(output_path, 'r', encoding='utf-8') as f:
            fixes = json.load(f)
        already_done = {f['slug'] for f in fixes}
        print(f"  Resuming interrupted run {run_id} — {len(already_done)} pages already done\n")
    else:
        run_id = _dt.now().strftime('%Y-%m-%d %H:%M:%S')
        started = _dt.now().isoformat()
        if os.path.exists(output_path):
            arch = os.path.join(base_dir, 'archive')
            os.makedirs(arch, exist_ok=True)
            ts = _dt.now().strftime('%Y%m%d-%H%M%S')
            try:
                os.replace(output_path,
                           os.path.join(arch, f'proposed-fixes-{ts}.json'))
                print(f"  Archived previous run's proposals to {arch}")
            except Exception:
                pass
        print(f"  Fresh analysis run {run_id}\n")

    def _write_marker(status):
        try:
            with open(run_marker, 'w', encoding='utf-8') as f:
                json.dump({'run_id': run_id, 'started': started,
                           'status': status, 'done': len(fixes)}, f)
        except Exception:
            pass
    _write_marker('running')

    for i, page in enumerate(pages_to_fix):
        slug = page['slug']
        if slug in already_done:
            print(f"  [{i+1}/{len(pages_to_fix)}] {slug} (cached)")
            continue

        print(f"  [{i+1}/{len(pages_to_fix)}] {slug}")

        fix = analyze_page_with_claude(client, page, model=MODEL)
        if fix:
            fix['run_id'] = run_id
            fix['analyzed_at'] = run_id
            fixes.append(fix)
            title_len = len(fix.get('yoast_title', ''))
            desc_len = len(fix.get('yoast_metadesc', ''))
            print(f"    Title ({title_len}c): {fix['yoast_title']}")
            print(f"    Meta ({desc_len}c): {fix['yoast_metadesc'][:70]}...")
            print(f"    Intro: {'ADD' if fix.get('intro_add') else 'no change'}")
            print(f"    Why: {fix.get('reasoning', '')[:80]}")
            print()

            # Save after each successful page (partial progress)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(fixes, f, indent=2, ensure_ascii=False)

        # Rate limiting
        if i < len(pages_to_fix) - 1:
            time.sleep(2)

    # Save proposed fixes
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(fixes, f, indent=2, ensure_ascii=False)
    _write_marker('complete')

    print(f"\n[CLAUDE] {len(fixes)} fixes proposed (run {run_id}). Saved to {output_path}")

    # Also generate a human-readable report
    report_path = output_path.replace('.json', '-report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("SEO FIX PROPOSALS — Generated by Claude\n")
        f.write("=" * 70 + "\n\n")
        for fix in fixes:
            f.write(f"Page: /{fix['slug']}/\n")
            f.write(f"Target Keyword: \"{fix['target_keyword']}\"\n")
            f.write(f"New Title: {fix['yoast_title']} ({len(fix['yoast_title'])}c)\n")
            f.write(f"New Meta: {fix['yoast_metadesc']} ({len(fix['yoast_metadesc'])}c)\n")
            if fix.get('intro_add'):
                f.write(f"Add to Intro: {fix['intro_add']}\n")
            f.write(f"Reasoning: {fix.get('reasoning', '')}\n")
            f.write("-" * 70 + "\n\n")

    print(f"[CLAUDE] Human-readable report: {report_path}")

    # NOTE (OSS Edition): the live install also calls generate_post_ideas() here
    # to suggest new posts to write. That feature is not included in the public
    # OSS release — it stays with the original author as a content moat.

    return fixes


# NOTE (OSS Edition): the live install includes three helper functions here —
# _post_idea_tokens, _existing_post_index, _best_existing_match — used only by
# the generate_post_ideas() flow. That feature is private to the original author
# and not part of the OSS release. The helpers are removed too since they have
# no callers without generate_post_ideas.


def _classify_page_bucket(impr, clicks, ctr, position):
    """Universal page classifier — returns one of:
      converter        — high CTR + decent click volume (PROTECT — don't rewrite, use as link source)
      sleeping_giant   — high impressions + low CTR + ranks well (title/meta fix wins)
      almost_there     — page 2 (pos 11-20) + decent impressions (depth + links push to page 1)
      dead_weight      — low everything (kill, consolidate, or improve)
      unclassified     — middle-ground page that doesn't fit any pattern
    """
    if impr == 0:
        return 'no_data'
    # Converter: high CTR + at least 10 clicks (real volume, not random spikes)
    if ctr >= 5.0 and clicks >= 10:
        return 'converter'
    # Sleeping giant: ranks well but underperforms expected CTR
    if position and 1 <= position <= 10 and impr >= 500 and ctr < 2.0 and clicks > 0:
        return 'sleeping_giant'
    # Almost there: page 2 (pos 11-20) with at least some traffic
    if position and 11 <= position <= 20 and impr >= 100:
        return 'almost_there'
    # Dead weight: barely ranks + no clicks + low impressions
    if impr < 100 and clicks == 0:
        return 'dead_weight'
    return 'unclassified'


def _classify_all_pages_to_buckets(analysis, ctr_curve, output_dir):
    """Walk every analyzed page, classify into universal buckets, write JSON."""
    buckets = {'converter': [], 'sleeping_giant': [], 'almost_there': [],
               'dead_weight': [], 'unclassified': [], 'no_data': []}
    for p in analysis:
        kws = p.get('keywords', [])
        if not kws:
            buckets['no_data'].append({'slug': p.get('slug', ''), 'title': p.get('title', '')})
            continue
        impr = sum(k.get('impressions', 0) for k in kws)
        clicks = sum(k.get('clicks', 0) for k in kws)
        ctr = round(clicks / impr * 100, 2) if impr > 0 else 0
        pos = round(sum(k.get('position', 0) * k.get('impressions', 1) for k in kws)
                    / max(impr, 1), 1) if kws else None
        bucket = _classify_page_bucket(impr, clicks, ctr, pos)
        buckets[bucket].append({
            'slug': p.get('slug', ''),
            'title': p.get('title', ''),
            'impressions': impr,
            'clicks': clicks,
            'ctr': ctr,
            'position': pos,
        })
    # Sort each bucket by impressions desc (most important first)
    for b in buckets.values():
        b.sort(key=lambda x: -(x.get('impressions') or 0))

    summary = {k: len(v) for k, v in buckets.items()}
    out = {'generated_at': datetime.now().isoformat() if 'datetime' in globals() else '',
           'summary': summary, 'buckets': buckets}
    # datetime import — make defensive
    try:
        from datetime import datetime as _dt
        out['generated_at'] = _dt.now().isoformat()
    except Exception:
        pass
    path = os.path.join(output_dir, 'page-buckets.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"[BUCKETS] {len(analysis)} pages classified: "
          + ' · '.join(f"{k}={v}" for k, v in summary.items() if v))


# NOTE (OSS Edition): generate_post_ideas() lives in the private install only.
# That function uses Claude to suggest NEW blog post topics from your keyword data —
# it's the original author's content moat and is not part of the public release.
# The OSS pipeline focuses on AUDITING + FIX PROPOSALS for existing pages.


# --- AWS Lambda Handler ---

def lambda_handler(event, context):
    """
    AWS Lambda entry point.
    Expects event with optional paths or S3 references.
    Environment variables: ANTHROPIC_API_KEY, OUTPUT_DIR
    """
    output_dir = os.environ.get('OUTPUT_DIR', '/tmp')

    analysis_path = event.get('analysis_path', os.path.join(output_dir, 'analysis.json'))
    opportunities_path = event.get('opportunities_path', os.path.join(output_dir, 'opportunities.json'))
    output_path = os.path.join(output_dir, 'proposed-fixes.json')

    fixes = run_claude_analysis(analysis_path, opportunities_path, output_path)

    return {
        'statusCode': 200,
        'fixes_count': len(fixes),
        'output_path': output_path,
        'fixes': fixes
    }


if __name__ == '__main__':
    run_claude_analysis()
