# Polymarket Smart Money

Finds Polymarket wallets that **win more than 75% of their resolved bets** and
**bet multiple times per week** — the "smart money" worth watching.

Zero dependencies. One file, Python 3 stdlib only.

## Run the dashboard

```bash
python3 smart_money.py
```

Open **http://localhost:8899**, hit **Scan**, and wait a minute or two.
Adjust the filters (win rate, bets/week, minimum resolved bets, candidate
pool size) and the table updates live while the scan runs. Click any trader
to see their recent resolved bets and a link to their Polymarket profile.

## Run in the terminal

```bash
python3 smart_money.py --scan            # default 150-wallet pool
python3 smart_money.py --scan --pool 300 # broader sweep
```

## How it works

1. **Candidates** — pulls the 7d, 30d, and all-time leaderboards from
   `data-api.polymarket.com/v1/leaderboard` and dedupes into a candidate pool
   (default 150 wallets).
2. **Win rate** — for each wallet, pages through `/closed-positions` (up to
   300 most recent resolved positions). A *win* is a resolved position with
   `realizedPnl > 0`.
3. **Frequency** — counts trades from `/activity` over the last 4 weeks;
   *bets/week* is the number of **distinct markets** traded per week, so 50
   fills on one order don't count as 50 bets.
4. **Filter** — keeps wallets with win rate ≥ 75%, ≥ 2 bets/week, and ≥ 10
   resolved bets (so a 3-for-3 fluke doesn't rank as a 100% winner).

## Copy-trading (`copytrade.py`)

Once you've found wallets worth following, `copytrade.py` watches them and
mirrors their trades onto your own account.

- **Sizing** — each fresh entry stakes a fixed **% of your bankroll** (default 2%).
- **Mirror** — copies **entries and exits**: when they add, it adds
  proportionally; when they sell part of a position, it sells the same
  fraction of yours.
- **Price guard** — skips a copy if the market has moved **>5%** from their
  fill price, so you don't chase.
- **No backfill** — only copies positions they open *after* you start
  watching; positions they already held are tracked (so exits still mirror)
  but never opened.

### ⚠️ Real money — read this

It runs in **PAPER mode by default** and places nothing — it just logs what it
*would* do. Live trading requires **all** of: `"mode": "live"` in the config,
the `--live` flag, typing a confirmation phrase, and `py-clob-client` with
valid credentials. Hard caps (per-trade, daily spend, total exposure, open
positions, price bounds) apply in both modes. In live mode this places real
orders with real money on your account — you own the config and the outcomes.

```bash
python3 copytrade.py --init      # write config.example.json
cp config.example.json config.json
#  ... edit config.json: add wallets to "watchlist", set bankroll & caps ...
python3 copytrade.py             # PAPER mode — safe, logs only
python3 copytrade.py --once      # single polling pass, then exit
```

Going live (only after you trust the paper output):

```bash
pip install py-clob-client
#  set "mode": "live" and fill in the "live" block (private_key, funder_address)
python3 copytrade.py --live      # prompts for a typed confirmation
```

`config.json` and `copytrade_state.json` are gitignored so your credentials
and runtime state never get committed.

## Caveats

- Candidates come from the leaderboards, so this surfaces *profitable* sharps.
  A high-win-rate wallet that has never cracked any leaderboard window won't
  appear — scanning every wallet on the platform isn't feasible via the
  public API.
- Win rate is measured over each wallet's most recent ~300 resolved
  positions, not their entire history.
- High win rate ≠ high EV: someone selling early for +$1 on every position
  counts as winning. Check the realized PnL column alongside the win rate.
