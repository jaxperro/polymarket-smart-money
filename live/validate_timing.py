#!/usr/bin/env python3
"""Last-minute-vs-sharp check on the standout conviction wallets.

This is a COPYABILITY heuristic, not proof of inside information: a near-100%
win rate is only useful to us if we can actually mirror it. The tell is entry->
resolution lead time on their WINNING conviction bets:
  * mostly < 1h before resolution  -> last-minute, you can't follow it in time
  * hours-to-days of lead          -> a sharp you could actually mirror
A short lead can mean a genuine insider OR just someone who trades fast-resolving
markets (live sports, hourly) well — we can't tell which, and for copy purposes
it doesn't matter: either way the window is too tight to mirror.
"""

import json
import os
import statistics as st
from concurrent.futures import ThreadPoolExecutor

import cache
import smart_money as sm

HERE = os.path.dirname(__file__)
COPYABLE_MED_LEAD = 24.0     # median lead (h) on winning conviction bets to count as copyable


def _bet_pnl(b):
    """Resolved (outcome) P&L of one bet: a $size stake bought at avg price p pays
    size/p if it resolved a win, else $0 — so P&L = size·(1−p)/p if won else −size.
    The cache's `won` already unions redeemed + resolved-unredeemed, so this is
    survivorship-correct."""
    p = max(0.001, min(0.999, b["p"] or 0))
    return b["size"] * ((1 - p) / p if b["won"] else -1)


def display_stats(w):
    """Everything the dashboard's sharp table renders, precomputed so the page makes
    ZERO per-wallet data-api calls (it just reads the feed). Computed from the cache
    (every resolved bet over the 180d window, survivorship-correct):

      conv win%/record/P&L : over ALL of the wallet's conviction bets (top-20% stake)
      realized P&L         : over the most recent 500 resolved bets (any size)
      name / last-bet      : one /activity pull (record `name`; latest BUY >= trade p80)
    """
    bets = [b for b in cache.get_bets(w) if (b["size"] or 0) > 0]
    thr = cache.conv_cutoff(b["size"] for b in bets)
    conv = [b for b in bets if b["size"] >= thr]                      # ALL conviction bets
    won = sum(1 for b in conv if b["won"])
    recent = sorted(bets, key=lambda b: b["res_t"] or 0, reverse=True)[:500]   # last 500 resolved
    out = {
        "conv_win": round(100 * won / len(conv), 1) if conv else None,
        "conv_won": won, "conv_lost": len(conv) - won,
        "conv_pnl": round(sum(_bet_pnl(b) for b in conv)),
        "realized_pnl": round(sum(_bet_pnl(b) for b in recent)),
        "name": None, "last_trade": 0, "last_conv_bet": 0,
    }
    a = sm.get_json("/activity", {"user": w, "type": "TRADE", "limit": 300}) or []
    if a:
        out["last_trade"] = a[0].get("timestamp", 0)
        out["name"] = next((t.get("name") for t in a if t.get("name")), None)
        tthr = cache.conv_cutoff(t.get("usdcSize", 0) for t in a if t.get("side") == "BUY")
        for t in a:
            if t.get("side") == "BUY" and (t.get("usdcSize", 0) or 0) >= tthr:
                out["last_conv_bet"] = t.get("timestamp", 0)
                break
    return out


def lead_profile(w):
    ent = cache.get_entries(w)
    bets = cache.get_bets(w)
    cut = cache.conv_cutoff(b["size"] for b in bets)   # this wallet's top-20% stake cutoff
    leads = [(b["res_t"] - ent[b["cond"]]) / 3600.0 for b in bets
             if b["won"] and (b["size"] or 0) >= cut and b["cond"] in ent
             and b["res_t"] and b["res_t"] >= ent[b["cond"]]]
    if not leads:
        return None
    med = st.median(leads)
    u6 = sum(1 for l in leads if l < 6) / len(leads)
    verdict = ("last-minute" if (med < 6 or sum(1 for l in leads if l < 1) / len(leads) > 0.5)
               else "borderline" if med < COPYABLE_MED_LEAD else "sharp")
    return dict(n=len(leads), med=med, u6=u6, verdict=verdict)


def main():
    conv = json.load(open(os.path.join(HERE, "conviction_wallets.json")))
    print(f"validating timing on {len(conv)} conviction wallets…\n", flush=True)
    with ThreadPoolExecutor(max_workers=10) as ex:
        profs = list(ex.map(lambda c: (c, lead_profile(c["wallet"])), conv))

    sharps = []
    for c, p in profs:
        if p:
            c["med_lead_h"] = round(p["med"], 1)
            c["timing"] = p["verdict"]
            if p["verdict"] == "sharp":
                sharps.append(c)
    # enrich the sharps with the exact stats the dashboard renders, so it reads them
    # straight from the feed (1 request) instead of 3 data-api calls per wallet.
    with ThreadPoolExecutor(max_workers=8) as ex:
        for c, ds in zip(sharps, ex.map(lambda c: display_stats(c["wallet"]), sharps)):
            c.update(ds)
            if ds.get("name"):
                c["name"] = ds["name"]          # real Polymarket username (else keep prefix)

    sharps.sort(key=lambda c: (c["fwd_conv_roi"] is not None, c.get("fwd_conv_roi") or -9,
                               c["train_conv_roi"]), reverse=True)

    counts = {}
    for c, p in profs:
        counts[p["verdict"] if p else "no-data"] = counts.get(p["verdict"] if p else "no-data", 0) + 1
    print(f"timing breakdown: {counts}")
    print(f"COPYABLE SHARPS (median lead >= {COPYABLE_MED_LEAD:.0f}h): {len(sharps)}\n")

    h = (f"{'tr_win':>7}{'tr_roi':>7}{'medLeadH':>9}{'fw_win':>7}{'fw_roi':>7}{'fw_n':>5}  wallet")
    print(h); print("-" * len(h))
    for c in sharps[:30]:
        fw = f"{c['fwd_win']:.0f}%" if c["fwd_win"] is not None else "—"
        fr = f"{c['fwd_conv_roi']:+.0%}" if c["fwd_conv_roi"] is not None else "—"
        print(f"{c['train_win']:>6.0f}%{c['train_conv_roi']:>+6.0%}{c['med_lead_h']:>9.0f}"
              f"{fw:>7}{fr:>7}{c['fwd_n']:>5}  {c['wallet']}")

    json.dump(sharps, open(os.path.join(HERE, "watch_sharps.json"), "w"), indent=2)
    print(f"\n-> watch_sharps.json ({len(sharps)} copyable sharps, last-minute wallets filtered out)")


if __name__ == "__main__":
    main()
