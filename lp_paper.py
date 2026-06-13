#!/usr/bin/env python3
"""Paper liquidity-provision loop — measures NET reward yield without real money.

It simulates posting two-sided limit orders near the midpoint on the screener's
top markets, against the LIVE order book, and tracks:

    net P&L = rewards accrued  -  adverse-selection / inventory P&L

Reward model (per poll, per market):
    your_share = your_notional / (your_notional + competing_notional_near_mid)
    rewards   += daily_pool * your_share * (seconds_elapsed / 86400)
  (matches Polymarket's score-share mechanic; assumes ~equal price-quality, which
   is conservative since we quote tight to mid.)

Fill / adverse-selection model:
    We rest a bid at mid-tick and an ask at mid+tick. Between polls, if the
    midpoint crosses a quote, that quote is assumed FILLED at its price, and we
    take on inventory marked at the NEW mid — so a price that runs through us
    books an immediate loss. This is the bleed that must stay below rewards.
    Each poll we "cancel and re-quote" around the new mid (a bot requoting every
    poll interval). Shorter --poll = less bleed, fewer missed requotes.

This is an APPROXIMATION (ignores queue position, partial fills, requote
latency), deliberately a bit pessimistic on fills. Good enough to answer the
one question that gates real money: is net positive, and how big?

    python3 lp_paper.py --capital 1000 --markets 6 --poll 20
"""

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from lp_screener import get, reward_markets, daily_rate, hours_to_end, realized_vol_cents, CLOB
from copytrade import post_discord, load_json

STATE_PATH = "lp_paper_state.json"


def screen_targets(n, min_rate, capital, max_vol):
    """Pick the top-N low-volatility reward markets to make on."""
    mkts = [m for m in reward_markets()
            if m.get("active") and not m.get("closed") and daily_rate(m) >= min_rate]
    scored = []
    def assess(m):
        r = m.get("rewards") or {}
        ms = r.get("max_spread", 0) / 100.0
        toks = m.get("tokens") or []
        if not toks or ms <= 0:
            return None
        tok = toks[0]["token_id"]
        try:
            bk = get(f"{CLOB}/book?token_id={tok}")
        except Exception:
            return None
        bids = [(float(o["price"]), float(o["size"])) for o in bk.get("bids", [])]
        asks = [(float(o["price"]), float(o["size"])) for o in bk.get("asks", [])]
        if not bids or not asks:
            return None
        mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2
        comp = min(sum(p * s for p, s in bids if p >= mid - ms),
                   sum((1 - p) * s for p, s in asks if p <= mid + ms))
        vol = realized_vol_cents(tok)
        hrs = hours_to_end(m)
        if vol is None or vol > max_vol:        # skip toxic / unknown-vol
            return None
        if hrs is not None and hrs < 24:         # skip imminent/live
            return None
        return {
            "token": tok, "question": m.get("question", "?")[:50],
            "pool": daily_rate(m), "max_spread": ms,
            "min_size": r.get("min_size", 0),
            "tick": float(bk.get("tick_size", 0.01)),
            "comp": comp, "mid": mid, "vol": vol,
        }
    with ThreadPoolExecutor(max_workers=16) as ex:
        for res in ex.map(assess, mkts):
            if res:
                scored.append(res)
    # rank by reward-yield / competition, low vol
    scored.sort(key=lambda x: x["pool"] / (x["comp"] + x["pool"]) / (1 + x["vol"]),
                reverse=True)
    return scored[:n]


def fresh_market_state(t, per_market):
    # cash = cumulative cash flow from simulated trades (buys negative, sells
    # positive); inv = signed share position. Net trading P&L = cash + inv*mid.
    return {**t, "notional": per_market / 2, "bid": None, "ask": None,
            "inv": 0.0, "cash": 0.0, "rewards": 0.0, "fills": 0,
            "last_t": time.time()}


def poll_market(s, max_inv_shares):
    """One observe-fill-accrue-requote step against the live book."""
    try:
        bk = get(f"{CLOB}/book?token_id={s['token']}")
    except Exception:
        return
    bids = [(float(o["price"]), float(o["size"])) for o in bk.get("bids", [])]
    asks = [(float(o["price"]), float(o["size"])) for o in bk.get("asks", [])]
    if not bids or not asks:
        return
    mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2
    now = time.time()
    dt = now - s["last_t"]
    s["last_t"] = now
    size = s["notional"] / mid if mid > 0 else 0

    # 1) fills: did the mid cross our resting quotes since last poll?
    #    cash + mark-to-market captures the adverse-selection loss directly.
    if s["bid"] is not None and mid <= s["bid"]:           # bought at our bid
        s["inv"] += size
        s["cash"] -= size * s["bid"]
        s["fills"] += 1
    if s["ask"] is not None and mid >= s["ask"]:           # sold at our ask
        s["inv"] -= size
        s["cash"] += size * s["ask"]
        s["fills"] += 1

    # 2) accrue rewards for the elapsed time
    comp = s["comp"]
    share = s["notional"] / (s["notional"] + comp) if (s["notional"] + comp) > 0 else 0
    s["rewards"] += s["pool"] * share * (dt / 86400.0)

    # 3) re-quote around the new mid (within max_spread), respecting inventory cap
    s["mid"] = mid
    s["bid"] = mid - s["tick"] if s["inv"] < max_inv_shares else None   # stop adding if long
    s["ask"] = mid + s["tick"] if s["inv"] > -max_inv_shares else None
    ms = s["max_spread"]
    s["comp"] = min(sum(p * sz for p, sz in bids if p >= mid - ms),
                    sum((1 - p) * sz for p, sz in asks if p <= mid + ms))


def net_pnl(s):
    return s["rewards"] + s["cash"] + s["inv"] * s["mid"]


def summary(states, started, capital, retired):
    rew = retired["rewards"] + sum(s["rewards"] for s in states)
    trading = retired["trading"] + sum(s["cash"] + s["inv"] * s["mid"] for s in states)
    net = rew + trading
    hrs = (time.time() - started) / 3600 or 1e-9
    apr = net / capital / (hrs / 24) * 365 * 100 if capital else 0
    lines = [
        f"⏱  {hrs:.1f}h · capital ${capital:,.0f}",
        f"   rewards accrued  : +${rew:,.2f}",
        f"   trading/inventory: {trading:+,.2f}  (adverse-selection bleed)",
        f"   ── NET           : {net:+,.2f}   (~{apr:,.0f}% APR if it holds)",
    ]
    return net, "\n".join(lines)


def run(args):
    cfg = load_json("config.json", {})
    webhook = cfg.get("discord_webhook", "")
    per_market = args.capital / args.markets
    print(f"[{time.strftime('%H:%M:%S')}] screening for {args.markets} low-vol markets...",
          flush=True)
    targets = screen_targets(args.markets, args.min_rate, args.capital, args.max_vol)
    if not targets:
        print("No suitable low-vol markets found right now.")
        return
    states = [fresh_market_state(t, per_market) for t in targets]
    started = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] making markets on {len(states)} markets "
          f"(${per_market:,.0f} each, ${args.capital:,.0f} total):", flush=True)
    for s in states:
        print(f"    ${s['pool']:>4.0f}/day  vol {s['vol']:.1f}c  comp ${s['comp']:,.0f}"
              f"  {s['question']}", flush=True)
    if webhook:
        post_discord(webhook, f"📊 **Paper LP started** · {len(states)} markets · "
                              f"${args.capital:,.0f} capital. Tracking net = rewards − bleed.")

    max_inv_shares = (per_market / 2) / 0.5 * args.max_inv   # rough share cap per market
    # P&L from markets that have rotated out (resolved/expired) is banked here
    # so cumulative net survives rotation.
    retired = {"rewards": 0.0, "trading": 0.0}

    def retire(s):
        retired["rewards"] += s["rewards"]
        retired["trading"] += s["cash"] + s["inv"] * s["mid"]

    last_report = started
    next_rescreen = started + args.refresh
    try:
        while True:
            for s in states:
                poll_market(s, max_inv_shares)
            now = time.time()

            # rotate: drop markets that fell out of the fresh screen (resolved /
            # vol spiked / out-competed), bank their P&L, add fresh ones.
            if now >= next_rescreen:
                fresh = screen_targets(args.markets, args.min_rate, args.capital, args.max_vol)
                fresh_toks = {t["token"] for t in fresh}
                kept = []
                for s in states:
                    if s["token"] in fresh_toks:
                        kept.append(s)
                    else:
                        retire(s)
                states = kept
                held = {s["token"] for s in states}
                for t in fresh:
                    if len(states) >= args.markets:
                        break
                    if t["token"] not in held:
                        states.append(fresh_market_state(t, per_market))
                next_rescreen = now + args.refresh
                print(f"[{time.strftime('%H:%M:%S')}] re-screened · {len(states)} active "
                      f"· banked net so far ${retired['rewards'] + retired['trading']:,.2f}",
                      flush=True)

            save_state(states, started, args.capital, retired)
            if now - last_report >= args.report:
                net, txt = summary(states, started, args.capital, retired)
                print(f"\n[{time.strftime('%H:%M:%S')}]\n{txt}", flush=True)
                if webhook:
                    post_discord(webhook, "📊 **Paper LP update**\n" + txt)
                last_report = now
            if args.duration and (now - started) >= args.duration * 3600:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        pass
    net, txt = summary(states, started, args.capital, retired)
    print(f"\n=== FINAL ===\n{txt}")
    print("\nPer-market:")
    for s in sorted(states, key=net_pnl, reverse=True):
        print(f"  net {net_pnl(s):+8.2f} | rew +{s['rewards']:6.2f} | "
              f"fills {s['fills']:3d} | inv {s['inv']:+8.1f} | {s['question']}")


def save_state(states, started, capital, retired):
    slim = [{"question": s["question"], "pool": s["pool"],
             "rewards": round(s["rewards"], 2), "trading": round(s["cash"] + s["inv"] * s["mid"], 2),
             "inv": round(s["inv"], 1), "fills": s["fills"],
             "net": round(net_pnl(s), 2)} for s in states]
    net, _ = summary(states, started, capital, retired)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"started": started, "capital": capital, "net": round(net, 2),
                   "retired": {k: round(v, 2) for k, v in retired.items()},
                   "markets": slim}, f, indent=2)
    os.replace(tmp, STATE_PATH)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, default=1000)
    ap.add_argument("--markets", type=int, default=6)
    ap.add_argument("--poll", type=int, default=20, help="seconds between requotes")
    ap.add_argument("--report", type=int, default=900, help="seconds between summaries")
    ap.add_argument("--refresh", type=int, default=3600, help="seconds between re-screens")
    ap.add_argument("--min-rate", type=float, default=50)
    ap.add_argument("--max-vol", type=float, default=1.5, help="max 24h vol (cents) to qualify")
    ap.add_argument("--max-inv", type=float, default=1.0, help="inventory cap multiple")
    ap.add_argument("--duration", type=float, default=0, help="hours to run (0 = until killed)")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
