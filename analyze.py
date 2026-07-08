"""
Step 2: Analyze live pages against their target keywords.
Reads each page, checks SEO factors, outputs a report.
"""

import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from config import *


def fetch_live_page(url):
    """Fetch rendered HTML of a live page."""
    try:
        resp = requests.get(url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; NerdySEOBot/1.0)'
        })
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"    [ERROR] Failed to fetch {url}: {e}")
        return None


def fetch_wp_post(slug):
    """Fetch post data via WordPress REST API."""
    try:
        url = f"{SITE_DOMAIN}/wp-json/wp/v2/posts?slug={slug}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        posts = resp.json()
        if posts:
            return posts[0]
    except Exception as e:
        print(f"    [ERROR] WP API failed for {slug}: {e}")
    return None


def analyze_page(page_data):
    """
    Analyze a single page against its target keywords.
    Returns a dict with findings and recommendations.
    """
    url = page_data['page']
    slug = page_data['slug']
    keywords = page_data['keywords']
    top_keyword = keywords[0]['query']
    all_keyword_terms = set()
    for kw in keywords:
        for word in kw['query'].lower().split():
            if len(word) > 2:
                all_keyword_terms.add(word)

    print(f"  Analyzing: {slug}")

    # Fetch live rendered page
    html = fetch_live_page(url)
    if not html:
        return None

    soup = BeautifulSoup(html, 'html.parser')

    # Fetch WP post data for raw content
    wp_post = fetch_wp_post(slug)

    # --- SEO Checks ---
    findings = {
        'url': url,
        'slug': slug,
        'top_keyword': top_keyword,
        'all_keywords': [kw['query'] for kw in keywords],
        'total_impressions': page_data['total_impressions'],
        'issues': [],
        'recommendations': []
    }

    # 1. Title tag
    title_tag = soup.find('title')
    title_text = title_tag.get_text().strip() if title_tag else ''
    findings['title'] = title_text
    findings['title_length'] = len(title_text)

    if top_keyword.lower() not in title_text.lower():
        findings['issues'].append(f"Title does not contain top keyword \"{top_keyword}\"")
        findings['recommendations'].append({
            'type': 'title',
            'issue': f"Title missing keyword: \"{top_keyword}\"",
            'current': title_text,
            'suggestion': f"Add \"{top_keyword}\" to the title tag"
        })

    if len(title_text) > 60:
        findings['issues'].append(f"Title too long ({len(title_text)} chars, max 60)")
    elif len(title_text) < 30:
        findings['issues'].append(f"Title too short ({len(title_text)} chars)")

    # 2. Meta description
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    desc_text = meta_desc['content'].strip() if meta_desc and meta_desc.get('content') else ''
    findings['meta_description'] = desc_text
    findings['meta_description_length'] = len(desc_text)

    if not desc_text:
        findings['issues'].append("No meta description found")
        findings['recommendations'].append({
            'type': 'meta_description',
            'issue': 'Missing meta description',
            'current': '',
            'suggestion': f"Add a meta description containing \"{top_keyword}\" (150-155 chars)"
        })
    elif top_keyword.lower() not in desc_text.lower():
        findings['issues'].append(f"Meta description does not contain \"{top_keyword}\"")
        findings['recommendations'].append({
            'type': 'meta_description',
            'issue': f"Meta description missing keyword: \"{top_keyword}\"",
            'current': desc_text,
            'suggestion': f"Rewrite meta description to include \"{top_keyword}\""
        })

    if desc_text and len(desc_text) > 160:
        findings['issues'].append(f"Meta description too long ({len(desc_text)} chars, max 160)")
    elif desc_text and len(desc_text) < 120:
        findings['issues'].append(f"Meta description short ({len(desc_text)} chars, aim 150-155)")

    # 3. H1 tag
    h1 = soup.find('h1')
    h1_text = h1.get_text().strip() if h1 else ''
    findings['h1'] = h1_text

    if h1_text and top_keyword.lower() not in h1_text.lower():
        # Check partial match (most keyword words present)
        kw_words = set(top_keyword.lower().split())
        h1_words = set(h1_text.lower().split())
        overlap = kw_words & h1_words
        if len(overlap) < len(kw_words) * 0.5:
            findings['issues'].append(f"H1 does not contain keyword \"{top_keyword}\"")
            findings['recommendations'].append({
                'type': 'h1',
                'issue': f"H1 missing keyword terms",
                'current': h1_text,
                'suggestion': f"Adjust H1 to include \"{top_keyword}\" or its key terms"
            })

    # 4. H2 headings — check if keyword terms appear in subheadings
    h2_tags = soup.find_all('h2')
    h2s = [h.get_text().strip() for h in h2_tags]
    findings['h2_count'] = len(h2s)
    findings['h2s'] = h2s[:10]  # first 10

    # Build structured content outline: each H2/H3 with first sentence of its section
    outline = []
    all_headings = soup.find_all(['h2', 'h3'])
    for heading in all_headings[:15]:
        heading_text = heading.get_text().strip()
        # Get first paragraph after this heading
        next_el = heading.find_next_sibling()
        first_sentence = ''
        while next_el and next_el.name not in ['h2', 'h3']:
            if next_el.name == 'p':
                text = next_el.get_text().strip()
                if text:
                    # Take first sentence only (up to 150 chars)
                    sentence_end = text.find('. ')
                    first_sentence = text[:sentence_end + 1] if sentence_end > 0 else text[:150]
                    break
            next_el = next_el.find_next_sibling()
        outline.append({'heading': heading_text, 'level': heading.name,
                        'first_sentence': first_sentence})
    findings['content_outline'] = outline

    h2_text_combined = ' '.join(h2s).lower()
    kw_in_h2 = any(word in h2_text_combined for word in all_keyword_terms)
    if not kw_in_h2 and h2s:
        findings['issues'].append("No H2 headings contain keyword-related terms")

    # 5. Content analysis
    # Get main content area (skip sidebar, footer, nav)
    article = soup.find('article') or soup.find('div', class_='entry-content') or soup.find('main')
    if article:
        # Remove script, style, nav elements
        for tag in article.find_all(['script', 'style', 'nav', 'aside']):
            tag.decompose()
        content_text = article.get_text(separator=' ', strip=True)
    else:
        content_text = soup.get_text(separator=' ', strip=True)

    word_count = len(content_text.split())
    findings['word_count'] = word_count

    if word_count < 800:
        findings['issues'].append(f"Thin content ({word_count} words, aim for 1500+)")
        findings['recommendations'].append({
            'type': 'content_length',
            'issue': f"Only {word_count} words",
            'current': f"{word_count} words",
            'suggestion': "Expand content to 1500+ words with more depth on the topic"
        })

    # 6. Keyword density in content
    content_lower = content_text.lower()
    kw_count = content_lower.count(top_keyword.lower())
    kw_density = (kw_count / word_count * 100) if word_count > 0 else 0
    findings['keyword_count'] = kw_count
    findings['keyword_density'] = round(kw_density, 2)

    if kw_count == 0:
        findings['issues'].append(f"Exact keyword \"{top_keyword}\" not found in content")
        findings['recommendations'].append({
            'type': 'keyword_usage',
            'issue': f"Keyword \"{top_keyword}\" absent from content",
            'current': '0 occurrences',
            'suggestion': f"Naturally include \"{top_keyword}\" 3-5 times in the content"
        })
    elif kw_density < 0.3:
        findings['issues'].append(f"Low keyword density ({kw_density:.1f}%, aim 0.5-1.5%)")

    # 7. First paragraph check
    first_p = article.find('p') if article else soup.find('p')
    if first_p:
        findings['intro_preview'] = first_p.get_text().strip()[:500]
        first_p_text = first_p.get_text().strip().lower()
        if top_keyword.lower() not in first_p_text:
            # Check partial match
            kw_words = top_keyword.lower().split()
            if not any(w in first_p_text for w in kw_words if len(w) > 3):
                findings['issues'].append("Keyword not mentioned in first paragraph")
                findings['recommendations'].append({
                    'type': 'intro',
                    'issue': 'Keyword missing from introduction',
                    'current': first_p_text[:150],
                    'suggestion': f"Mention \"{top_keyword}\" in the first paragraph"
                })

    # 8. Images — check alt text
    images = article.find_all('img') if article else soup.find_all('img')
    # Exclude lazy-load placeholders: data: URIs and images with no real src
    real_images = [img for img in images if not img.get('src', '').startswith('data:')]
    imgs_without_alt = [img.get('src', '')[:60] for img in real_images if not img.get('alt', '').strip()]
    findings['total_images'] = len(real_images)
    findings['images_missing_alt'] = len(imgs_without_alt)

    if imgs_without_alt:
        findings['issues'].append(f"{len(imgs_without_alt)} images missing alt text")

    # 9. Internal links check
    internal_links = []
    if article:
        for a in article.find_all('a', href=True):
            href = a['href']
            site_host = SITE_DOMAIN.replace('https://', '').replace('http://', '')
            if site_host in href or href.startswith('/'):
                internal_links.append(href)
    findings['internal_links'] = len(internal_links)

    if len(internal_links) < 3:
        findings['issues'].append(f"Only {len(internal_links)} internal links (aim for 5+)")

    # 10. Check for secondary keywords that could be targeted
    secondary_kw_missing = []
    for kw in keywords[1:5]:  # top 5 secondary keywords
        if kw['query'].lower() not in content_lower:
            secondary_kw_missing.append({
                'query': kw['query'],
                'impressions': kw['impressions'],
                'position': kw['position']
            })

    if secondary_kw_missing:
        findings['recommendations'].append({
            'type': 'secondary_keywords',
            'issue': 'Secondary ranking keywords missing from content',
            'missing': secondary_kw_missing,
            'suggestion': 'Add sections or mentions covering these related queries'
        })

    # Score the page (0-100)
    score = 100
    score -= len(findings['issues']) * 8
    score = max(0, score)
    findings['seo_score'] = score

    return findings


def analyze_all():
    """Analyze top opportunity pages."""
    # Prefer the merged GSC + Bing file (built by merge_data.py) so the live-page
    # analyzer sees Bing keywords too. Fall back to GSC-only opportunities.json
    # if the merge step never ran.
    merged_path = os.path.join(OUTPUT_DIR, 'merged-opportunities.json')
    opp_path    = os.path.join(OUTPUT_DIR, 'opportunities.json')
    use_path = merged_path if os.path.exists(merged_path) else opp_path
    if not os.path.exists(use_path):
        print("[ANALYZE] No opportunities file found. Run fetch + merge first.")
        return []
    print(f"[ANALYZE] Reading {os.path.basename(use_path)}")

    with open(use_path, 'r', encoding='utf-8') as f:
        opportunities = json.load(f)

    print(f"[ANALYZE] Analyzing top {MAX_PAGES_TO_ANALYZE} pages...")
    results = []

    for page_data in opportunities[:MAX_PAGES_TO_ANALYZE]:
        result = analyze_page(page_data)
        if result:
            results.append(result)
        time.sleep(1)  # Be nice to the server

    # Sort by number of issues (worst first)
    results.sort(key=lambda x: x['seo_score'])

    # Save results
    results_path = os.path.join(OUTPUT_DIR, 'analysis.json')
    with open(results_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # Print summary report
    print(f"\n{'='*90}")
    print(f"SEO ANALYSIS REPORT — {len(results)} pages analyzed")
    print(f"{'='*90}\n")

    for r in results:
        print(f"[Score: {r['seo_score']:>3}] {r['slug']}")
        print(f"         Top keyword: \"{r['top_keyword']}\" ({r['total_impressions']} impr)")
        print(f"         Words: {r['word_count']} | Title: {r['title_length']} chars | H2s: {r['h2_count']}")
        if r['issues']:
            for issue in r['issues'][:3]:
                print(f"         - {issue}")
        if r['recommendations']:
            print(f"         >> {len(r['recommendations'])} recommendations")
        print()

    return results


if __name__ == '__main__':
    analyze_all()
