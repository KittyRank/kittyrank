# Google Search Console — service-account setup (no OAuth)

KittyRank can read Search Console two ways:

- **Connect with Google** (OAuth) — one click in the setup wizard. See
  [GOOGLE-LOGIN.md](GOOGLE-LOGIN.md). Easiest for most people.
- **Service-account JSON** (this guide) — a key file you generate once. Best for
  headless/server use, CI, or if you'd rather not run the OAuth flow.

Either works. This guide covers the service-account path end to end.

## 1. Create a Google Cloud project
1. Go to <https://console.cloud.google.com/>
2. Project dropdown (top-left) → **New Project** → name it (e.g. "KittyRank") → **Create**
3. Make sure the new project is selected.

## 2. Enable the APIs
**APIs & Services → Library**, then **Enable** each (all in the same project):
- **Google Search Console API**
- **Google Analytics Data API** — only if you connect GA4

## 3. Create a service account + JSON key
1. **IAM & Admin → Service Accounts → + Create Service Account**
2. Name it (e.g. "kittyrank") → **Create and Continue**
3. Skip the optional role/access steps → **Done**
4. Click the new service-account email → **Keys** tab → **Add Key → Create new key**
5. Choose **JSON** → **Create** — a `.json` file downloads automatically.
6. Move it somewhere **outside this repo** (e.g. `C:\keys\gsc.json`). Put that path in
   `config.py` as `GSC_CREDENTIALS`, or paste it in the setup wizard.
7. Note the **`client_email`** in the JSON (looks like
   `kittyrank@your-project.iam.gserviceaccount.com`) — you need it in the next step.

> **Check you have the right file.** A real service-account JSON has top-level keys
> `type`, `project_id`, `private_key`, `client_email`. If yours has `clientIP` or
> `rayName`, that's a Cloudflare event log, not a service account.

## 4. Grant the service account access in Search Console
1. <https://search.google.com/search-console/> → select your property (add it if needed)
2. **Settings → Users and permissions → Add User**
3. Paste the service account's **`client_email`**
4. Permission: **Full** (or Owner) — enough to read Search Console performance data.
5. **Add**

## 5. GSC site URL format
`GSC_SITE_URL` in `config.py` depends on your property type
(Search Console → **Settings → Property type**):

| Property type | `GSC_SITE_URL` |
|---|---|
| Domain property | `sc-domain:yoursite.com` |
| URL-prefix property | `https://yoursite.com/` (trailing slash required) |

## GA4 (optional)
GA4 reuses this **same** service-account JSON. Enable the **Google Analytics Data API**
(step 2), then in **GA4 Admin → Property Access Management** add the service-account email
as a **Viewer**. Put your GA4 **Property ID** in `config.py` (`GA4_PROPERTY_ID`).

## Done
Set `GSC_CREDENTIALS` to the JSON path (or paste it in the wizard) and you're ready to
fetch. Prefer one click instead? Use OAuth — see [GOOGLE-LOGIN.md](GOOGLE-LOGIN.md).
For Bing, see [BING-SETUP.md](BING-SETUP.md).
