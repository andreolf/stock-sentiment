#!/usr/bin/env python3
"""Backtest harness: replay logged US triggers against Alpaca IEX historical bars with the CURRENT exit logic, net of
costs. Read-only on the DB, zero X calls, never touches the trading API (market data only: data.alpaca.markets, feed=iex).

RULE (2026-10-25): a backtest NET OF COSTS (this script) is required before any discussion of live trading.

Usage: backtest.py [--since ISO] [--until ISO] [--slippage-pct 0.05] [--commission-per-share 0] [--commission-min 0]
                   [--no-partial] [--no-chase] [--verbose] [--json PATH]

Triggers replayed (the same ones paper_trader.py considers; Swiss observe-only names are never included):
  sentiment_shock / composite 'follow' signals (time = signal ts, signal price = logged price) and flagged IEX volume
  spikes on core tickers (time = detected_at, direction = price move, signal price = spike bar price).
Simulation (per trigger, one open position per ticker at a time):
  entry   open of the first 1-min IEX bar at/after T + entry_delay_min (1), regular hours only (09:30-15:45 ET);
          T = the time paper_trader actually evaluated the trigger (decisions.ts) when it did, else the trigger time
          (--entry-at trigger forces the trigger time); fill = open +/- slippage.
  stop    paper_trader.bracket_levels: atr_mult_stop (1.5) x ATR(14) of completed 1-hour IEX bars, clamped 0.4-4%.
  target  tp_mult (2) x stop distance.   time exit: hold_minutes (60), or 15 min before the close if earlier.
  size    paper_trader.size_qty: risk_per_trade_pct (0.5%) of equity / stop distance, capped at 5% of equity.
  fills   a bar touching both stop and target counts as the STOP (conservative); a gap through the stop fills at the
          bar open; time exits fill at the next bar open. Slippage is charged on BOTH sides of every fill (incl. the
          take-profit limit, conservative); commission = max(commission_min, per_share x qty) per side.
Reported separately:
  * all triggers (unfiltered), * the subset paper_trader actually traded (decisions action='trade'),
  * all triggers after the max-chase filter (risk.max_chase_atr), and
  * TEST-ONLY partial take-profit + runner variant (backtest.partial_tp: 50% off at 1R, runner stop -> breakeven,
    runner target 3R, same time exit). It exists only here and never changes live behaviour.
"""
import argparse, json, math, os, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import db
from paper_trader import DEFAULTS as PT_DEFAULTS, RISK_DEFAULTS, atr, bracket_levels, size_qty, chase_atr

ET = ZoneInfo("America/New_York")
BT_DEFAULTS = {"slippage_pct_per_side": 0.05, "commission_per_share": 0.0, "commission_min_per_order": 0.0,
               "equity": 100_000, "entry_delay_min": 1,
               "partial_tp": {"fraction": 0.5, "tp1_r": 1.0, "runner_r": 3.0, "runner_stop": "breakeven"}}


def ts_of(b):
    return db.parse_ts(b["t"]) if isinstance(b["t"], str) else b["t"]


# ---------- pure simulation (unit-tested) ----------
def walk(bars, side, entry_px, legs, time_exit):
    """Walk 1-min bars from the entry bar. legs = list of dicts {qty, stop, target, be_after_tp1(bool)} sharing the same
    entry. Returns list of (qty, exit_px, exit_ts, kind) fills. Rules: stop before target inside one bar; a gap through a
    level fills at the bar open; at/after time_exit everything still open exits at that bar's open; a leg with
    be_after_tp1 moves its stop to the entry price from the bar AFTER the first target fill of another leg."""
    long = side == "buy"
    open_legs = [dict(l) for l in legs]
    fills, tp1_hit_at = [], None
    for i, b in enumerate(bars):
        t = ts_of(b)
        if t >= time_exit:
            for l in open_legs:
                fills.append((l["qty"], b["o"], t, "time"))
            return fills
        if tp1_hit_at is not None and i > tp1_hit_at:
            for l in open_legs:
                if l.get("be_after_tp1") and not l.get("_be"):
                    l["stop"], l["_be"] = entry_px, True
        still = []
        for l in open_legs:
            hit_stop = b["l"] <= l["stop"] if long else b["h"] >= l["stop"]
            hit_tp = b["h"] >= l["target"] if long else b["l"] <= l["target"]
            if hit_stop:
                px = min(b["o"], l["stop"]) if long else max(b["o"], l["stop"])
                fills.append((l["qty"], px, t, "breakeven" if l.get("_be") else "stop_loss"))
            elif hit_tp:
                px = max(b["o"], l["target"]) if long else min(b["o"], l["target"])
                fills.append((l["qty"], px, t, "take_profit"))
                if tp1_hit_at is None:
                    tp1_hit_at = i
            else:
                still.append(l)
        open_legs = still
        if not open_legs:
            return fills
    if open_legs and bars:                         # data ran out: exit at the last close
        for l in open_legs:
            fills.append((l["qty"], bars[-1]["c"], ts_of(bars[-1]), "end_of_data"))
    return fills


def costs_and_pnl(side, entry_ref, fills, qty, slip_pct, comm_ps, comm_min):
    """-> (gross $, net $, cost $). Slippage slip_pct % on both sides of each fill, commission per side per order."""
    sgn = 1 if side == "buy" else -1
    k = slip_pct / 100
    entry_fill = entry_ref * (1 + k * sgn)
    gross = sum((px - entry_ref) * q * sgn for q, px, _, _ in fills)
    net = sum(((px * (1 - k * sgn)) - entry_fill) * q * sgn for q, px, _, _ in fills)
    comm = max(comm_min, comm_ps * qty) + sum(max(comm_min, comm_ps * q) for q, _, _, _ in fills) if (comm_ps or comm_min) else 0.0
    net -= comm
    return gross, net, gross - net


def simulate(bars_1m, hour_bars, side, t_entry, params, partial=None):
    """One trade. bars_1m: 1-min bars from the entry time on (asc); hour_bars: completed 1-hour bars before entry.
    -> dict or None (no entry bar)."""
    p = params
    bars = [b for b in bars_1m if ts_of(b) >= t_entry]
    if not bars:
        return None
    eb = bars[0]
    t0 = ts_of(eb)
    entry = float(eb["o"])
    a = atr(hour_bars, 14) if len(hour_bars) >= 2 else None
    stop, tp, dist = bracket_levels(side, entry, a, p["atr_mult_stop"], p["tp_mult"], p["min_stop_pct"], p["max_stop_pct"])
    qty = size_qty(p["equity"], 0, entry, p["max_trade_pct"], p["max_total_pct"], dist, p["risk_per_trade_pct"])
    if qty < 1:
        return None
    close_et = t0.astimezone(ET).replace(hour=16, minute=0, second=0, microsecond=0)
    t_exit = min(t0 + timedelta(minutes=p["hold_minutes"]), close_et - timedelta(minutes=p["flat_before_close_min"]))
    sgn = 1 if side == "buy" else -1
    if partial:
        q1 = int(math.floor(qty * partial["fraction"]))
        q2 = qty - q1
        legs = [{"qty": q1, "stop": stop, "target": entry + sgn * partial["tp1_r"] * dist}] if q1 else []
        if q2:
            legs.append({"qty": q2, "stop": stop, "target": entry + sgn * partial["runner_r"] * dist,
                         "be_after_tp1": partial.get("runner_stop", "breakeven") == "breakeven"})
    else:
        legs = [{"qty": qty, "stop": stop, "target": tp}]
    fills = walk(bars, side, entry, legs, t_exit)
    gross, net, cost = costs_and_pnl(side, entry, fills, qty, p["slippage_pct_per_side"], p["commission_per_share"],
                                     p["commission_min_per_order"])
    kinds = sorted({f[3] for f in fills})
    return {"entry_ts": db.iso(t0), "entry": entry, "atr": a, "stop": stop, "tp": tp, "dist": dist, "qty": qty,
            "exit_ts": db.iso(max(f[2] for f in fills)), "exit_kinds": kinds,
            "exit": sum(f[0] * f[1] for f in fills) / qty, "gross": gross, "net": net, "cost": cost,
            "r_net": net / (dist * qty) if dist else None}


def summarize(trades, equity):
    n = len(trades)
    if not n:
        return {"trades": 0}
    wins = [t for t in trades if t["net"] > 0]
    gp = sum(t["net"] for t in trades if t["net"] > 0)
    gl = -sum(t["net"] for t in trades if t["net"] < 0)
    eq, peak, mdd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        eq += t["net"]; peak = max(peak, eq); mdd = max(mdd, peak - eq)
    kinds = {}
    for t in trades:
        for k in t["exit_kinds"]:
            kinds[k] = kinds.get(k, 0) + 1
    return {"trades": n, "wins": len(wins), "win_rate": len(wins) / n, "gross_pl": sum(t["gross"] for t in trades),
            "costs": sum(t["cost"] for t in trades), "net_pl": sum(t["net"] for t in trades),
            "net_pct_equity": sum(t["net"] for t in trades) / equity * 100,
            "avg_r_net": sum(t["r_net"] or 0 for t in trades) / n, "profit_factor": (gp / gl) if gl else None,
            "max_drawdown": mdd, "exits": kinds,
            "days": len({t["entry_ts"][:10] for t in trades})}


# ---------- triggers from the DB (read-only) ----------
def load_triggers(con, since=None, until=None, core=None, blocked=None):
    core = set(core or db.watchlist(True))
    blocked = blocked if blocked is not None else db.observe_only_symbols()
    since, until = since or "0000", until or "9999"
    out = []
    for s in con.execute("""SELECT * FROM signals WHERE type IN ('sentiment_shock','composite') AND variant='follow'
                            AND ts BETWEEN ? AND ? ORDER BY ts""", (since, until)):
        out.append({"type": s["type"], "id": s["id"], "ticker": s["ticker"], "ts": s["ts"], "direction": s["direction"],
                    "price": s["price"], "desc": f"{s['type']} z={s['zscore']:+.2f}"})
    for v in con.execute("""SELECT * FROM volume_spikes WHERE flagged=1 AND COALESCE(detected_at, ts) BETWEEN ? AND ?
                            ORDER BY ts""", (since, until)):
        if v["ticker"] in core:
            out.append({"type": "volume_spike", "id": v["id"], "ticker": v["ticker"], "ts": v["detected_at"] or v["ts"],
                        "direction": "long" if v["direction"] == "up" else "short", "price": v["price"],
                        "desc": f"{v['timeframe']} spike x{v['ratio']:.1f} {v['ret_pct']:+.2f}%"})
    traded = {(r["trigger_type"], r["trigger_id"]) for r in con.execute(
        "SELECT trigger_type, trigger_id FROM decisions WHERE action='trade' AND rule='v2_news_momentum'")}
    dts = {}
    for r in con.execute("""SELECT trigger_type, trigger_id, MIN(ts) ts FROM decisions
                            WHERE rule IN ('v2_news_momentum','v2_attention_fade') GROUP BY 1, 2"""):
        dts[(r["trigger_type"], r["trigger_id"])] = r["ts"]
    out = [t for t in out if t["ticker"].upper() not in blocked]
    for t in out:
        t["traded_live"] = (t["type"], t["id"]) in traded
        t["decision_ts"] = dts.get((t["type"], t["id"]))
    return sorted(out, key=lambda t: t["ts"])


class BarCache:
    """Alpaca IEX bars (market-data host only), cached per ticker/day (1Min) and per ticker (1Hour)."""
    def __init__(self, client):
        self.c, self.m, self.h = client, {}, {}

    def minute(self, sym, day):
        k = (sym, day)
        if k not in self.m:
            d0 = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30)
            self.m[k] = self.c.bars(sym, "1Min", start=d0, end=d0 + timedelta(hours=6, minutes=30)).get(sym, [])
        return self.m[k]

    def hourly(self, sym, start, end):
        if sym not in self.h:
            self.h[sym] = self.c.bars(sym, "1Hour", start=start, end=end).get(sym, [])
        return self.h[sym]


def run(con, client, params, since=None, until=None, partial=None, max_chase=None, verbose=False, triggers=None,
        entry_at="decision"):
    trigs = triggers if triggers is not None else load_triggers(con, since, until)
    if not trigs:
        return {"triggers": 0, "results": [], "skipped": {}}
    cache = BarCache(client)
    first = db.parse_ts(trigs[0]["ts"]) - timedelta(days=10)
    last = db.parse_ts(trigs[-1]["ts"]) + timedelta(days=1)
    busy_until, skipped, results = {}, {}, []
    for t in trigs:
        t0 = t["decision_ts"] if entry_at == "decision" and t.get("decision_ts") else t["ts"]
        t_entry = db.parse_ts(t0) + timedelta(minutes=params["entry_delay_min"])
        et = t_entry.astimezone(ET)
        if et.weekday() >= 5 or et.strftime("%H:%M") < "09:30" or et.strftime("%H:%M") >= "15:45":
            skipped["outside regular hours"] = skipped.get("outside regular hours", 0) + 1; continue
        if busy_until.get(t["ticker"]) and t_entry < busy_until[t["ticker"]]:
            skipped["position already open in ticker"] = skipped.get("position already open in ticker", 0) + 1; continue
        try:
            m = cache.minute(t["ticker"], et.date())
            hb = [b for b in cache.hourly(t["ticker"], first, last) if ts_of(b) + timedelta(hours=1) <= t_entry
                  and ts_of(b) >= t_entry - timedelta(days=7)]
        except Exception as ex:
            skipped[f"bars error: {type(ex).__name__}"] = skipped.get(f"bars error: {type(ex).__name__}", 0) + 1; continue
        side = "buy" if t["direction"] == "long" else "sell"
        base = simulate(m, hb, side, t_entry, params)
        if not base:
            skipped["no entry bar / size 0"] = skipped.get("no entry bar / size 0", 0) + 1; continue
        busy_until[t["ticker"]] = db.parse_ts(base["exit_ts"]) + timedelta(minutes=1)
        a_eff = base["atr"] or base["dist"] / params["atr_mult_stop"]
        ch = chase_atr(t["direction"], t.get("price"), base["entry"], a_eff)
        row = {**t, "base": base, "chase_atr": ch,
               "chase_skip": bool(max_chase and ch is not None and ch > max_chase),
               "partial": simulate(m, hb, side, t_entry, params, partial) if partial else None}
        results.append(row)
        if verbose:
            print(f"  {db.local(t['ts'])} {t['ticker']} {t['direction']:5} {t['desc'][:28]:28} entry {base['entry']:.2f} "
                  f"stop {base['stop']:.2f} tp {base['tp']:.2f} qty {base['qty']} -> {','.join(base['exit_kinds'])} "
                  f"net ${base['net']:+.2f}" + (f" chase {ch:+.2f} ATR" if ch is not None else "")
                  + (" [traded live]" if t["traded_live"] else ""))
    return {"triggers": len(trigs), "results": results, "skipped": skipped}


def report(out, params, partial):
    eq = params["equity"]
    res = out["results"]
    sets = {"all triggers": [r["base"] for r in res],
            "traded live (v2_news_momentum)": [r["base"] for r in res if r["traded_live"]],
            "all triggers after max-chase filter": [r["base"] for r in res if not r["chase_skip"]]}
    if partial:
        sets["TEST-ONLY partial TP + runner (all triggers)"] = [r["partial"] for r in res if r["partial"]]
        sets["TEST-ONLY partial TP + runner (traded live)"] = [r["partial"] for r in res if r["partial"] and r["traded_live"]]
    summ = {k: summarize(v, eq) for k, v in sets.items()}
    print(f"backtest: {out['triggers']} logged triggers, {len(res)} simulated; skipped: "
          + (", ".join(f"{k} {v}" for k, v in out["skipped"].items()) or "none"))
    print(f"costs: slippage {params['slippage_pct_per_side']}%/side, commission ${params['commission_per_share']}/share "
          f"(min ${params['commission_min_per_order']}); exits: stop {params['atr_mult_stop']}x ATR1h, tp {params['tp_mult']}x stop, "
          f"time {params['hold_minutes']} min; risk {params['risk_per_trade_pct']}% of ${eq:,.0f}")
    for k, s in summ.items():
        if not s["trades"]:
            print(f"  {k}: 0 trades"); continue
        pf = "n/a" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
        print(f"  {k}: {s['trades']} trades on {s['days']} days, win rate {s['win_rate']*100:.1f}%, gross ${s['gross_pl']:+,.2f}, "
              f"costs ${s['costs']:,.2f}, NET ${s['net_pl']:+,.2f} ({s['net_pct_equity']:+.3f}% of equity), avg {s['avg_r_net']:+.3f} R, "
              f"PF {pf}, max DD ${s['max_drawdown']:,.2f}, exits {s['exits']}")
    print("NOTE: research only. A backtest net of costs is required before any live-trading discussion; small n is not evidence.")
    return summ


def params_from_config(conf=None, **over):
    c = db.load_config() if conf is None else conf
    p = {**PT_DEFAULTS, **c.get("paper_trader", {}), **{k: v for k, v in BT_DEFAULTS.items() if k != "partial_tp"},
         **{k: v for k, v in (c.get("backtest") or {}).items() if not k.startswith("_") and k != "partial_tp"}}
    p.update({k: v for k, v in over.items() if v is not None})
    partial = {**BT_DEFAULTS["partial_tp"], **((c.get("backtest") or {}).get("partial_tp") or {})}
    chase = {**RISK_DEFAULTS, **(c.get("risk") or {})}["max_chase_atr"]
    return p, partial, chase


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since"); ap.add_argument("--until")
    ap.add_argument("--slippage-pct", type=float, dest="slippage_pct_per_side")
    ap.add_argument("--commission-per-share", type=float); ap.add_argument("--commission-min", type=float, dest="commission_min_per_order")
    ap.add_argument("--no-partial", action="store_true"); ap.add_argument("--no-chase", action="store_true")
    ap.add_argument("--verbose", action="store_true"); ap.add_argument("--json")
    ap.add_argument("--entry-at", choices=("decision", "trigger"), default="decision",
                    help="decision (default): enter when paper_trader actually evaluated the trigger (decisions.ts) if it "
                         "did, else at the trigger time; trigger: always at the trigger time")
    a = ap.parse_args()
    params, partial, chase = params_from_config(slippage_pct_per_side=a.slippage_pct_per_side,
                                                commission_per_share=a.commission_per_share,
                                                commission_min_per_order=a.commission_min_per_order)
    con = db.connect_ro()
    from alpaca_client import AlpacaPaper
    out = run(con, AlpacaPaper(), params, a.since, a.until, None if a.no_partial else partial,
              None if a.no_chase else chase, a.verbose, entry_at=a.entry_at)
    print(f"entry time: {a.entry_at}")
    summ = report(out, params, None if a.no_partial else partial)
    if a.json:
        json.dump({"generated": db.iso(db.utcnow()), "params": params, "partial_tp": partial, "max_chase_atr": chase,
                   "skipped": out["skipped"], "summary": summ,
                   "trades": [{k: v for k, v in r.items()} for r in out["results"]]}, open(a.json, "w"), indent=1, default=str)
        print(f"json: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
