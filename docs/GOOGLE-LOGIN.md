# Google login (skip the service-account JSON)

KittyRank can authenticate to Google Search Console with a one-click **"Connect
Google"** instead of the old 5-step service-account setup. You (the app owner)
create one OAuth client once; your users then just click and consent.

The service-account JSON still works as a fallback — nothing breaks if you skip
this.

**Related setup guides:**
- Prefer a key file over OAuth? → [GSC-SERVICE-ACCOUNT.md](GSC-SERVICE-ACCOUNT.md)
- Bing Webmaster Tools API key → [BING-SETUP.md](BING-SETUP.md)

## One-time setup (you, the app owner)

1. **Google Cloud project** — [console.cloud.google.com](https://console.cloud.google.com) →
   create or pick a project.
2. **Enable APIs** (APIs & Services → Library → Enable each):
   - Search Console API
   - Google Analytics Data API *(only if you use GA4)*
3. **OAuth consent screen** (APIs & Services → OAuth consent screen):
   - User type: External
   - App name, support email, your logo
   - **App homepage:** `https://kittyrank.com`
   - **Privacy policy:** `https://kittyrank.com/privacy.html`
     *(these come from the landing site — deploy it first)*
   - Scopes: add `.../auth/webmasters.readonly`, `.../auth/analytics.readonly`
   - While unverified you can add up to 100 test users; that's fine for beta.
4. **OAuth client** (APIs & Services → Credentials → Create credentials →
   OAuth client ID):
   - Application type: **Desktop app**
   - Copy the **Client ID** and **Client secret**.
5. **Put them in config** (or set as env vars):
   ```python
   GOOGLE_OAUTH_CLIENT_ID = '....apps.googleusercontent.com'
   GOOGLE_OAUTH_CLIENT_SECRET = '....'
   ```
   For a desktop app the secret is not treated as confidential by Google
   (PKCE is the real protection), so shipping it with the app is fine.

## What the user experiences

Settings → **Connect Google** → browser opens → they pick their account and
click Allow → the app lists their Search Console properties → they pick one.
Done. No JSON file, no adding a robot email as owner.

The consent is stored as a **refresh token in `output/google-oauth-token.json`**
on the user's own machine (gitignored, `chmod 600`). It never leaves the
machine. Users can revoke anytime at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## Removing the "unverified app" warning

Until Google verifies your OAuth app, users see an "unverified app" screen and
you're capped at 100 users. Search Console + Analytics are **"sensitive"**
scopes — they need OAuth verification (privacy policy, homepage, scope
justification, a short demo video) but **not** the expensive annual CASA audit
that Gmail/Drive scopes require. Submit for verification once the landing site
is live; approval takes days to a couple of weeks and lifts both the warning
and the cap.

## URL submission is Bing-only

KittyRank does **not** use the Google Indexing API — Google restricts it to
JobPosting/BroadcastEvent pages and general URL submission is against its ToS. The tool
submits only to **Bing**. For Google, keep your sitemap fresh (WordPress auto-pings on
publish/update) and use Search Console → **Request Indexing** for priority pages.

## Dependencies

The one-time connect needs `google-auth-oauthlib` (in requirements). Daily runs
use only the already-required `google-auth` libs to refresh the token.
