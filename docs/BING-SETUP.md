# Bing Webmaster Tools — API key setup

Bing is optional — KittyRank works with Google alone. If configured, it fetches Bing
search data and merges it with Search Console for a fuller picture, and submits URLs to
Bing for re-indexing after fixes. For technical / how-to content Bing is often a bigger
traffic source than Google, so it's worth adding.

## 1. Add & verify your site
1. Go to <https://www.bing.com/webmasters/> and sign in with a Microsoft account.
2. **Add a Site** → enter your site URL (e.g. `https://yoursite.com`).
3. Verify with any method:
   - **Import from Google Search Console** — fastest if you're already verified there.
   - **XML file** — download and upload to your site root.
   - **Meta tag** — add to your site's `<head>`.
   - **CNAME / DNS record**.
4. Click **Verify**.

## 2. Generate the API key
1. In Bing Webmaster Tools → **Settings** (gear icon, top-right) → **API Access**.
2. Click **Generate** → copy the key (a 32-character hex string).
3. Put it in `config.py` as `BING_API_KEY` (or paste it in the setup wizard).
4. Set `BING_SITE_URL` to your verified site URL (e.g. `https://yoursite.com/`).

> The same key both fetches Bing search data and submits URLs for re-indexing. To skip
> Bing entirely, leave `BING_API_KEY` empty — the pipeline runs Google-only.

For Google Search Console credentials, see [GSC-SERVICE-ACCOUNT.md](GSC-SERVICE-ACCOUNT.md)
(service account) or [GOOGLE-LOGIN.md](GOOGLE-LOGIN.md) (one-click OAuth).
