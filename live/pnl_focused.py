#!/usr/bin/env python3
"""Does a FOCUSED copy strategy clear where the broad 10-wallet basket didn't?

Same $1000 capital-constrained engine + missed-trade accounting (pnl_basket.sim),
but on narrower signal sets: fewer wallets, and/or only the wallet's higher-
conviction (larger-stake) bets — so $1000 isn't spread across 1,210 markets.
"""

import time
import cache
import pnl_basket as pb

JUN1 = time.mktime(time.strptime("2026-06-01", "%Y-%m-%d"))
E8 = "0xe8ca3f758c93f44f3ec210542ab78afb7c0bcccb"
A0 = "0x0a7aaf83341b52df34e8ffef52aa295538d6df1b"


def gather(wallets, size_min=None):
    """One signal per market. size_min filters to the wallet's larger-stake
    (higher-conviction) bets; in that mode we only use resolved bets (open ones
    have no known stake to filter on)."""
    pos = {}
    for w in wallets:
        ent = cache.get_entries(w)
        resolved = {b["cond"]: b for b in cache.get_bets(w)}
        for cond, ets in ent.items():
            if ets < JUN1:
                continue
            b = resolved.get(cond)
            if b:
                if size_min and (b["size"] or 0) < size_min:
                    continue
                rec = dict(ets=ets, p=max(0.001, min(0.999, b["p"])),
                           won=b["won"], res_t=b["res_t"])
            else:
                if size_min:
                    continue
                rec = dict(ets=ets, p=None, won=None, res_t=None)
            if cond not in pos or ets < pos[cond]["ets"]:
                pos[cond] = rec
    return sorted(pos.values(), key=lambda r: r["ets"])


def run(label, wallets, size_min=None):
    ev = gather(wallets, size_min)
    res = sum(1 for e in ev if e["res_t"] is not None)
    print(f"\n### {label} — {len(ev)} markets ({res} resolved)")
    h = f"{'stake':>6}{'entered':>8}{'missed':>7}{'open':>5}{'realized':>11}{'equity':>9}"
    print(h)
    for s in (50, 100, 200):
        r = pb.sim(ev, s)
        print(f"${s:>4}{r['entered']:>8}{r['missed']:>7}{r['open_left']:>5}"
              f"{r['realized']:>+10,.0f}{r['equity']:>9,.0f}")


def main():
    run("0xe8 only — all June+ entries", [E8])
    run("0xe8 only — conviction (their bets >= $200)", [E8], size_min=200)
    run("0xe8 only — conviction (their bets >= $1000)", [E8], size_min=1000)
    run("0xe8 + 0x0a — all June+ entries", [E8, A0])
    run("0xe8 + 0x0a — conviction (>= $200)", [E8, A0], size_min=200)
    print("\nrealized = settled-bet P&L · equity = $1000 + realized (open at cost)")


if __name__ == "__main__":
    main()
