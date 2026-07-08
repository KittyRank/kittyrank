# KittyRank Free — Setup Guide

KittyRank analyzes your WordPress site's search performance (Google Search
Console + Bing + optional GA4), finds striking-distance keywords (rank 3–20),
and uses Claude to propose better titles and meta descriptions. The Free
edition is read-only toward WordPress: it tells you exactly what to change,
you paste the changes into your SEO plugin.

## Prerequisites

- Python 3.10+ (3.13 recommended)
- A WordPress site verified in Google Search Console and Bing Webmaster Tools
- ~15 minutes for API-key setup (one time)

## 1. Install

```bash
git clone <this-repo>
cd kittyrank
pip install -r requirements.txt
```

## 2. Get your API credentials

### Google Search Console (required)

> **Full walkthrough:** [GSC-SERVICE-ACCOUNT.md](GSC-SERVICE-ACCOUNT.md) (service account) &middot; or one-click [OAuth](GOOGLE-LOGIN.md).

1. Go to [Google Cloud Console → Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Create a project (or reuse one) → **Create Service Account** → any name → Done
3. Open the account → **Keys → Add Key → JSON** — a `.json` file downloads.
   Save it somewhere OUTSIDE this repo (e.g. `C:\keys\gsc.json`)
4. Enable the **Search Console API** for the project
   (APIs & Services → Library → search → Enable)
5. In [Search Console](https://search.google.com/search-console) → Settings →
   Users and permissions → **Add user** → paste the service account's email
   (`...@...iam.gserviceaccount.com`) → permission **Owner** (Full for URL submission)

> **Check you have the right file:** a real service-account JSON contains the
> top-level keys `type, project_id, private_key, client_email`. If yours has
> keys like `clientIP` or `rayName`, you grabbed a different download.

### Bing Webmaster Tools (required)

> **Full walkthrough:** [BING-SETUP.md](BING-SETUP.md).

1. [bing.com/webmasters](https://www.bing.com/webmasters) → verify your site
   (import from GSC is one click)
2. Settings (gear icon) → **API access** → copy the API key (32-char hex)

### Anthropic / Claude (required for fix proposals)

1. [console.anthropic.com](https://console.anthropic.com/settings/keys) →
   API Keys → Create Key (starts with `sk-ant-`)
2. Proposals use Claude Sonnet by default — a full analyze run on ~20 pages
   costs well under $1.

### Google Analytics 4 (optional)

Adds bounce-rate / engagement signals to the audits.
GA4 Admin → Property Settings → copy the numeric **Property ID**. Uses the
same service-account JSON as GSC (add it as a viewer on the GA4 property:
Admin → Property Access Management).

## 3. First run

```bash
python review.py
```

The dashboard opens at `http://localhost:8090`. With no config it redirects
to the **setup wizard** (`/setup`) — paste in the values from step 2 and Save.
Re-open the wizard anytime via the **⚙ Settings** button.

Everything lives in `config.py` (gitignored) — you can also edit it by hand.

## 4. Run the pipeline

In the dashboard's **Pipeline** tab:

1. **Fetch Search Data** — pulls GSC + Bing (+ GA4), merges keyword data
   (~2–5 min depending on site size and lookback window)
2. **Upload CrawlyCat report** *(optional)* — crawl-issue data if you use it
3. **Analyze & Generate Fixes** — per-page SEO diagnosis + Claude title/meta
   proposals (~2–5 min for 20 pages)

Or from the command line:

```bash
python run.py fetch        # pull data
python run.py analyze      # diagnose + Claude proposals
python run.py audit        # all 5 audits
python run.py audit cannibal   # just one (technical/trend/backlink/cornerstone/cannibal)
python run.py report       # static markdown + HTML report in output/
python run.py all          # everything
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No such file or directory: ...json` on Fetch | GSC_CREDENTIALS path wrong, or the file isn't a service-account JSON (see check above) |
| GSC returns 403 | Service-account email not added as Owner in Search Console |
| Bing fetch empty | API key wrong, or site not verified in Bing Webmaster |
| `name 'GSC_DAYS_BACK' is not defined` | Your config.py predates this version — re-save via the Settings wizard |
| Dashboard port in use | `python review.py --port 8091` |

## What's in Free vs Pro

Free: all data fetching, all 5 audits (technical, trend, backlink,
cornerstone, **cannibalization**), Claude fix proposals, static reports,
copy-to-clipboard + Mark Applied workflow, URL submission to Bing.

Pro adds the one-click action layer: Approve & Apply to WordPress, bulk
apply, revert, internal-link insertion, Claude full-content rewrite,
URL removal, **page consolidation (site-wide link rewrite + 301)**, and the
fix-outcome history / cooldown feedback loop.

See `docs/USER-GUIDE.md` for the day-to-day workflow.
