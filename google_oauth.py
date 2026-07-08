"""
Google login (OAuth) — optional, friction-free replacement for the
service-account JSON.

Instead of: create GCP project -> enable APIs -> make service account ->
download JSON -> add its email as a GSC owner, the user clicks "Connect
Google" once, consents in the browser, and picks their property.

Design:
  - The one-time consent uses the desktop "installed app" loopback flow
    (google-auth-oauthlib). It opens a browser, catches the redirect on a
    local port, and saves a REFRESH TOKEN locally (output/google-oauth-token.json,
    gitignored). The token never leaves the machine.
  - Every pipeline run calls get_credentials(scope), which returns a live,
    auto-refreshing Credentials object built from that refresh token — OR,
    if no OAuth token exists, falls back to the classic service-account JSON.
    So existing setups keep working untouched.

Setup (once, by you): create an OAuth client of type "Desktop app" in a
Google Cloud project with the three APIs enabled, and put its id/secret in
config as GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET. See
docs/GOOGLE-LOGIN.md.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import *

SCOPES = {
    'gsc':      'https://www.googleapis.com/auth/webmasters.readonly',
    'ga4':      'https://www.googleapis.com/auth/analytics.readonly',
}
ALL_SCOPES = list(SCOPES.values())
TOKEN_URI = 'https://oauth2.googleapis.com/token'
TOKEN_PATH = os.path.join(OUTPUT_DIR, 'google-oauth-token.json')


def _client():
    cid = globals().get('GOOGLE_OAUTH_CLIENT_ID', '') or os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
    csec = globals().get('GOOGLE_OAUTH_CLIENT_SECRET', '') or os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')
    if not cid:
        return None, None
    return cid, csec


def is_connected():
    return os.path.exists(TOKEN_PATH)


def status():
    """Lightweight state for the wizard: connected?, email, chosen property."""
    if not is_connected():
        cid, _ = _client()
        return {'connected': False, 'configured': bool(cid)}
    try:
        with open(TOKEN_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return {'connected': True, 'email': d.get('email', ''),
                'property': d.get('property', ''), 'scopes': d.get('scopes', [])}
    except Exception:
        return {'connected': False, 'configured': True}


def _save(creds, email='', prop=''):
    data = {
        'refresh_token': creds.refresh_token,
        'scopes': list(creds.scopes or ALL_SCOPES),
        'email': email,
        'property': prop,
    }
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except Exception:
        pass


def _email_of(creds):
    try:
        from googleapiclient.discovery import build
        svc = build('oauth2', 'v2', credentials=creds)
        return svc.userinfo().get().execute().get('email', '')
    except Exception:
        return ''


def start_oauth_flow(open_browser=True):
    """Interactive one-time consent. Opens a browser, saves the refresh token.
    Returns {'success', 'email', 'properties'} or {'success': False, 'error'}."""
    cid, csec = _client()
    if not cid:
        return {'success': False, 'error':
                'Google OAuth client not configured. Set GOOGLE_OAUTH_CLIENT_ID / '
                'GOOGLE_OAUTH_CLIENT_SECRET in config.py — see docs/GOOGLE-LOGIN.md.'}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        return {'success': False, 'error':
                'Missing dependency — run: pip install google-auth-oauthlib'}
    cfg = {'installed': {
        'client_id': cid, 'client_secret': csec,
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
        'token_uri': TOKEN_URI,
        'redirect_uris': ['http://localhost'],
    }}
    try:
        flow = InstalledAppFlow.from_client_config(cfg, scopes=ALL_SCOPES + [
            'https://www.googleapis.com/auth/userinfo.email', 'openid'])
        creds = flow.run_local_server(port=0, prompt='consent',
                                      open_browser=open_browser, access_type='offline')
    except Exception as e:
        return {'success': False, 'error': f'OAuth flow failed: {e}'}
    if not creds.refresh_token:
        return {'success': False, 'error':
                'No refresh token returned — revoke prior access at '
                'myaccount.google.com/permissions and reconnect.'}
    email = _email_of(creds)
    _save(creds, email=email)
    try:
        props = list_properties(creds)
    except Exception:
        props = []
    return {'success': True, 'email': email, 'properties': props}


def set_property(site_url):
    """Persist the GSC property the user picked."""
    if not is_connected():
        return {'success': False, 'error': 'Not connected.'}
    with open(TOKEN_PATH, encoding='utf-8') as f:
        d = json.load(f)
    d['property'] = site_url
    with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=2)
    return {'success': True, 'property': site_url}


def get_credentials(scope='gsc'):
    """Runtime credential source. OAuth refresh token if present, else the
    service-account JSON fallback. Returns a Credentials object or None."""
    if os.path.exists(TOKEN_PATH):
        try:
            from google.oauth2.credentials import Credentials
            with open(TOKEN_PATH, encoding='utf-8') as f:
                d = json.load(f)
            cid, csec = _client()
            creds = Credentials(
                token=None, refresh_token=d.get('refresh_token'),
                client_id=cid, client_secret=csec, token_uri=TOKEN_URI,
                scopes=d.get('scopes', ALL_SCOPES))
            return creds  # google-api client refreshes automatically
        except Exception as e:
            print(f'[google_oauth] token load failed ({e}); trying service account', flush=True)
    # Fallback — classic service-account JSON
    gsc = globals().get('GSC_CREDENTIALS', '') or ''
    if scope == 'ga4':
        gsc = globals().get('GA4_CREDENTIALS', '') or gsc
    if gsc and os.path.exists(gsc):
        try:
            from google.oauth2 import service_account
            return service_account.Credentials.from_service_account_file(
                gsc, scopes=[SCOPES.get(scope, SCOPES['gsc'])])
        except Exception as e:
            print(f'[google_oauth] service-account load failed: {e}', flush=True)
    return None


def list_properties(creds):
    """GSC properties this account can access (verified only)."""
    from googleapiclient.discovery import build
    svc = build('searchconsole', 'v1', credentials=creds)
    entries = svc.sites().list().execute().get('siteEntry', [])
    return [e['siteUrl'] for e in entries
            if e.get('permissionLevel') != 'siteUnverifiedUser']


def disconnect():
    if os.path.exists(TOKEN_PATH):
        os.remove(TOKEN_PATH)
    return {'success': True}


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    if '--connect' in sys.argv:
        print(json.dumps(start_oauth_flow(), indent=2))
    else:
        print(json.dumps(status(), indent=2))
