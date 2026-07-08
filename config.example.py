"""
Configuration template for KittyRank (OSS Edition).

Copy this to config.py and fill in your own values. NEVER commit config.py.

Most values can be set via environment variables instead — useful for
production deployments and CI/CD.
"""

import os

# ─── Site Identity ──────────────────────────────────────────────────────────
SITE_DOMAIN = 'https://example.com'
SITE_NAME = 'Example Site'
SITE_DESCRIPTION = 'a brief description of your site (used in Claude prompts)'

# Brand-name words that appear in nearly every post title via the WP site-name
# suffix (e.g. "Tutorial X - Example Site"). Stripped from internal-link
# overlap calculations so they don't inflate suggestions. Tune per site.
SITE_BRAND_TOKENS = ['example', 'site']


# ─── Cornerstone Posts (optional — leave empty if you're not using yet) ─────
# Two-tier classification for the cornerstone-link auditor:
#   'A' = Content Pillar (HIGH priority — body links carry strong signal)
#   'B' = Architectural Hub (LOWER — banner mu-plugin already links it)
CORNERSTONE_SLUGS = {
    # 'your-pillar-slug':            {'type': 'A'},
    # 'your-architectural-hub-slug': {'type': 'B'},
}


# ─── Google Search Console ──────────────────────────────────────────────────
# Path to your GCP service account JSON. Get one at:
#   Google Cloud Console → IAM & Admin → Service Accounts → Keys
# Then enable: the Search Console API for that service account.
# Grant the service account email read access to your GSC property.
GSC_CREDENTIALS = os.environ.get(
    'GSC_CREDENTIALS',
    r'PATH-TO-YOUR-SERVICE-ACCOUNT.json',
)


# ─── Bing Webmaster Tools ───────────────────────────────────────────────────
# Get your API key from: Bing Webmaster Tools → Settings → API Access
BING_API_KEY = os.environ.get('BING_API_KEY', 'YOUR-BING-API-KEY')
BING_SITE_URL = os.environ.get('BING_SITE_URL', SITE_DOMAIN + '/')


# ─── Anthropic (Claude) — for AI fix proposals ──────────────────────────────
# Get your key from: console.anthropic.com → Settings → API Keys (starts sk-ant-)
# Used to generate title/meta rewrites for sleeping-giant pages.
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', 'YOUR-ANTHROPIC-API-KEY')

# Claude model. claude-sonnet-4-6 is the recommended balance.
# claude-opus-4-7 = stronger, slower, more expensive.
# claude-haiku-4-5-20251001 = cheapest, fastest, less nuanced output.
DIRECT_MODEL = 'claude-sonnet-4-6'
USE_BEDROCK = False              # set True to use AWS Bedrock instead of direct API
AWS_REGION = 'us-east-1'         # only relevant if USE_BEDROCK = True
BEDROCK_MODEL = 'anthropic.claude-sonnet-4-6:0'   # only relevant if USE_BEDROCK = True


# ─── Google Analytics 4 (optional — for bounce rate signals) ────────────────
# Property ID from GA4 admin → Property → Property Details
GA4_PROPERTY_ID = os.environ.get('GA4_PROPERTY_ID', '')
GA4_CREDENTIALS = os.environ.get('GA4_CREDENTIALS', GSC_CREDENTIALS)


# ─── WordPress (only needed if you want to push fixes — OSS is read-only) ───
# OSS edition generates fix proposals but does NOT push to WordPress.
# The Pro tier adds auto-apply. Leave these blank for OSS use.
WP_LIVE_URL = os.environ.get('WP_LIVE_URL', SITE_DOMAIN)
WP_LIVE_USER = os.environ.get('WP_LIVE_USER', '')
WP_LIVE_PASS = os.environ.get('WP_LIVE_PASS', '')


# ─── Pipeline tunables ──────────────────────────────────────────────────────
MAX_PAGES_TO_ANALYZE = 20           # top N pages to deeply analyze per run
POSITION_RANGE = (1, 50)            # GSC keyword position filter (only fetch in this range)
MIN_PAGE_IMPRESSIONS = 25           # filter floor: pages with fewer impressions are noise
BING_KEYWORD_FETCH_LIMIT = 200      # max pages to fetch keyword data for from Bing

# --- Google login (OAuth) — optional, replaces the service-account JSON ---
# Create an OAuth client of type 'Desktop app' in a Google Cloud project with
# Search Console + Indexing + Analytics Data APIs enabled. See docs/GOOGLE-LOGIN.md.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)
