#!/bin/bash
# Daily skilled-wallet refresh, cache-backed:
#   1) discover  — enumerate traders of markets resolved in the last ~14 days,
#                  merged into the accumulating candidate pool
#   2) freshen   — force a re-pull of the current watchlists (skilled + sharps,
#                  for forward tracking), then top up the cache (collect re-pulls
#                  only new + >14d-stale wallets, so it's cheap after the first run)
#   3) re-score  — the 5-gate skill funnel, instant from cache -> watch_skilled.json
#   4) sharps    — conviction-profile scan + last-minute timing gate ->
#                  conviction_wallets.json + watch_sharps.json (the set the live
#                  trading dashboard reads via raw.githubusercontent)
#   5) dashboard — regenerate + snapshot for auditable forward history
#   6) publish   — commit + push the refreshed outputs so the live dashboard
#                  (jaxperro.com/trading) picks up the new sharp list
#
# Schedule with launchd/cron (Mac must be awake). Logs to daily.log.
set -u
cd "$(dirname "$0")"
echo "[daily] $(date '+%F %T') 1/6 discover (enumerate last 14d)"
python3 enumerate.py 14
echo "[daily] $(date '+%F %T') 2/6 freshen cache (watchlists forced + new wallets)"
python3 -c "import json,os,cache
wl=[]
for f in ('watch_skilled.json','watch_sharps.json'):
    if os.path.exists(f):
        wl += [w['wallet'] for w in json.load(open(f))]
if wl:
    cache.invalidate(wl)" 2>/dev/null || true
python3 collect.py
echo "[daily] $(date '+%F %T') 3/6 re-score skilled (cache-backed, instant)"
python3 skill.py
echo "[daily] $(date '+%F %T') 4/6 sharps: conviction scan + last-minute timing gate"
python3 conviction_scan.py
python3 validate_timing.py
echo "[daily] $(date '+%F %T') 5/6 dashboard"
python3 dashboard.py
mkdir -p history && cp watch_skilled.json "history/watch_$(date '+%Y%m%d').json" 2>/dev/null
echo "[daily] $(date '+%F %T') 6/6 publish (commit + push refreshed outputs)"
PUBLISH="no changes"
git add watch_skilled.json watch_sharps.json conviction_wallets.json dashboard.html 2>/dev/null
if git diff --cached --quiet 2>/dev/null; then
    echo "[daily] no output changes to publish"
elif git commit -q -m "live: daily refresh — skilled + sharp wallets [skip ci]"; then
    # sync first so a diverged remote (e.g. a manual commit) doesn't wedge the
    # auto-push permanently; abort a conflicting rebase and retry next run.
    git pull --rebase -q origin main 2>/dev/null || git rebase --abort 2>/dev/null || true
    if git push -q origin main; then
        echo "[daily] pushed refreshed sharp list"; PUBLISH="pushed"
    else
        echo "[daily] push failed — committed locally, will retry next run"; PUBLISH="push failed (committed locally)"
    fi
fi
echo "[daily] $(date '+%F %T') done -> watch_sharps.json + dashboard.html"

# ping Discord (webhook kept in gitignored ../config.json -> daily_webhook)
PUBLISH="$PUBLISH" python3 - <<'PY'
import json, os, ssl, time, urllib.request
try:                                   # cwd is live/ (daily.sh cd's there); config is repo-root
    hook = json.load(open("../config.json")).get("daily_webhook")
except Exception:
    hook = None
if hook:
    try:
        n = len(json.load(open("watch_sharps.json")))
    except Exception:
        n = "?"
    msg = (f"✅ Sharp pipeline finished {time.strftime('%Y-%m-%d %H:%M')} — "
           f"{n} copyable sharps · feed {os.environ.get('PUBLISH','?')}")
    data = json.dumps({"content": msg}).encode()
    req = urllib.request.Request(hook, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})  # Discord 403s w/o UA
    try:
        urllib.request.urlopen(req, timeout=15, context=ssl._create_unverified_context())
        print("[daily] discord pinged")
    except Exception as e:
        print("[daily] discord ping failed:", e)
PY
