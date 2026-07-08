"""
run.py — CLI orchestrator for KittyRank (OSS Edition).

Usage:
  python run.py                          # Full pipeline: fetch → analyze → audit → report
  python run.py fetch                    # Pull GSC + Bing data
  python run.py analyze                  # Page analysis + Claude fix proposals
  python run.py audit                    # All 4 audits (technical, trend, backlink, cornerstone)
  python run.py audit technical          # Single audit
  python run.py audit trend
  python run.py audit backlink
  python run.py audit cornerstone
  python run.py submit <url> [<url> ...] # Submit URL(s) to Bing
  python run.py report                   # Generate markdown + HTML report
  python run.py help                     # Show this help

OSS edition. Does NOT auto-apply changes to WordPress — generates proposals,
you apply manually in Yoast (or upgrade to Pro for one-click apply).
See README.md for the premium feature list.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ─── Encoding-safe print ────────────────────────────────────────────────────
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


def _ensure_utf8_stdout():
    if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
        try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception: pass


def _check_config():
    """Make sure config.py exists. If not, show a friendly error pointing to the template."""
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = os.path.join(here, 'config.py')
    if not os.path.exists(cfg):
        print('ERROR: config.py not found.')
        print('')
        print('  Copy the template:  cp config.example.py config.py')
        print('  Then edit it with your site domain + API keys.')
        sys.exit(1)


def cmd_fetch():
    """Run all data-fetching steps: GSC + Bing + optional GA4 + merge."""
    print('━━ STEP 1/4: Fetching GSC data ━━')
    try:
        from fetch import fetch_gsc_data
        fetch_gsc_data()
    except Exception as e:
        print(f'  [GSC] failed: {e}')
    print('')
    print('━━ STEP 2/4: Fetching Bing data ━━')
    try:
        from fetch_bing import fetch_bing_data
        fetch_bing_data()
    except Exception as e:
        print(f'  [BING] failed: {e}')
    print('')
    print('━━ STEP 3/4: Fetching GA4 data (optional) ━━')
    try:
        from fetch_ga4 import fetch_ga4_data
        fetch_ga4_data()
    except ImportError:
        print('  [GA4] not configured — skipping (set GA4_PROPERTY_ID in config to enable)')
    except Exception as e:
        print(f'  [GA4] failed (skipping): {e}')
    print('')
    print('━━ STEP 4/4: Merging GSC + Bing keyword data ━━')
    from merge_data import merge_opportunities
    merge_opportunities()


def cmd_analyze():
    """Run the page analysis + Claude fix-proposal step."""
    print('━━ Running page analysis ━━')
    try:
        from analyze import analyze_all
    except ImportError:
        # The OSS release expects analyze.py to exist OR we use claude_analyze directly.
        # If analyze.py is part of the live pipeline only, skip the analyze step here
        # and just run claude_analyze (which reads merged-opportunities.json directly).
        print('  [analyze] analyze.py not present in this build — going straight to Claude')
    else:
        analyze_all()
    print('')
    print('━━ Running Claude fix-proposal generation ━━')
    from claude_analyze import run_claude_analysis
    run_claude_analysis()


def cmd_audit(which=None):
    """Run one or all audits."""
    if which is None or which == 'all':
        modules = ['technical_audit', 'trend_analysis', 'backlink_audit',
                   'cornerstone_audit', 'cannibalization_audit']
    elif which == 'technical':
        modules = ['technical_audit']
    elif which == 'trend':
        modules = ['trend_analysis']
    elif which == 'backlink':
        modules = ['backlink_audit']
    elif which == 'cornerstone':
        modules = ['cornerstone_audit']
    elif which == 'cannibal':
        modules = ['cannibalization_audit']
    else:
        print(f'Unknown audit: {which}')
        print('Available: technical, trend, backlink, cornerstone, cannibal')
        sys.exit(1)

    for i, mod_name in enumerate(modules, 1):
        print(f'━━ AUDIT {i}/{len(modules)}: {mod_name} ━━')
        mod = __import__(mod_name)
        # Each audit module exposes a run_* function — find it
        for fn_name in ('run_audit', 'run_trend_analysis', 'run'):
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                try:
                    fn()
                except Exception as e:
                    print(f'  [{mod_name}] failed: {e}')
                break
        else:
            print(f'  [{mod_name}] no run-function found')
        print('')


def cmd_submit(urls):
    """Submit URLs to Bing."""
    if not urls:
        print('Usage: python run.py submit <url> [<url> ...]')
        sys.exit(1)
    from submit_urls import submit_url, submit_urls_batch
    if len(urls) == 1:
        submit_url(urls[0])
    else:
        submit_urls_batch(urls)


def cmd_report():
    """Generate markdown + HTML report from JSON outputs."""
    from report_generator import generate
    generate()


def cmd_help():
    print(__doc__)


def cmd_all():
    """Full pipeline: fetch → analyze → audit → report."""
    cmd_fetch()
    print('')
    cmd_analyze()
    print('')
    cmd_audit('all')
    print('')
    cmd_report()


def main():
    _ensure_utf8_stdout()
    args = sys.argv[1:]
    if not args:
        cmd_all()
        return
    cmd = args[0]
    rest = args[1:]
    if cmd in ('-h', '--help', 'help'):
        cmd_help()
    elif cmd == 'fetch':
        _check_config(); cmd_fetch()
    elif cmd == 'analyze':
        _check_config(); cmd_analyze()
    elif cmd == 'audit':
        _check_config(); cmd_audit(rest[0] if rest else None)
    elif cmd == 'submit':
        _check_config(); cmd_submit(rest)
    elif cmd == 'report':
        _check_config(); cmd_report()
    elif cmd == 'all':
        _check_config(); cmd_all()
    else:
        print(f'Unknown command: {cmd}')
        cmd_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
