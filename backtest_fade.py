#!/usr/bin/env python3
"""Backtest of v2_attention_fade vs v2_news_momentum (research only; 2026-11-17, requested by Francesco:
"cambiamo strategia: backtest di attention_fade"). Read-only on the DB, zero X calls, Alpaca MARKET-DATA host only
(IEX bars via alpaca_client; never the trading API). Bars are cached under data/backtest/cache (gitignored).

Usage: backtest_fade.py [--db PATH] [--slippage-bps 2,5,10] [--json PATH] [--cache DIR]

Event sets (US watchlist only; Swiss observe-only names are never included):
  fade_logged        the v2_attention_fade decisions paper_trader logged (evaluate.py's n=23), FADE direction
  fade_old_gate      every historical trigger (sentiment_shock/composite follow, flagged core volume spikes) that the
                     OLD news gate (pre-2026-11-17) found attention-only -> fade
  fade_new_gate      same, classified by the NEW gate (publish-time freshness at decision time, per-company direction,
                     headline quality) using only headlines fetched by the decision time
  news_logged        the 46 v2_news_momentum trades actually placed (trigger direction)
  news_new_gate      historical triggers the NEW gate backs with news (trigger direction)
Rules (same as live): entry = open of the first 1-min IEX bar >= decision time (else trigger time) + 1 min, 09:30-15:30
  ET only (no entries after 15:30 ET); max chase 1 x ATR(14, 1h) past the signal price in the trade direction -> skip;
  stop 1.5 x ATR clamped 0.4-4%, take-profit 2 x stop; 0.5% of equity risk per trade, max 5%/trade, 20% total;
  one position per ticker; opposite-direction cooldown per ticker per session; loss-streak breaker: after 3 stop-losses
  in one session no further entries that session (fade setups have no news, so none is 'top conviction').
Exit variants: bracket_60m (live rule: stop/TP + 60-min time exit, flat by 15:45 ET), bracket_eod (stop/TP, flat 15:45),
  bracket_1d (stop/TP, held to the same clock time next session, overnight gap fills at the open), and pure time exits
  time_1h / time_eod / time_1d (no stop/TP). A bar touching stop and target counts as the stop.
Costs: slippage S bps on EVERY fill (entry and exit, incl. limit take-profits: conservative); Alpaca commission $0.
"""
import argparse, json, math, os, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
VARIANTS = ("bracket_60m", "bracket_eod", "bracket_1d", "time_1h", "time_eod", "time_1d")


def et_dt(day, hh, mm):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).astimezone(timezone.utc)


class DiskBars:
    """1-min bars per (ticker, ET day) and 1-hour/1-day bars per ticker, from Alpaca IEX, cached on disk as JSON."""
    def __init__(self, client, cache_dir):
        self.c, self.dir, self.mem = client, cache_dir, {}
        os.makedirs(cache_dir, exist_ok=True)

    def _get(self, key, fetch):
        if key in self.mem:
            return self.mem[key]
        p = os.path.join(self.dir, key + ".json")
        if os.path.exists(p):
            v = json.load(open(p))
        else:
            v = fetch()
            json.dump(v, open(p, "w"))
        for b in v:
            b["_t"] = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        self.mem[key] = v
        return v

    def minute(self, sym, day):
        if day >= datetime.now(ET).date():
            return []
        d0 = et_dt(day, 9, 30)
        return self._get(f"{sym}_1m_{day}", lambda: self.c.bars(sym, "1Min", start=d0, end=d0 + timedelta(hours=6, minutes=30)).get(sym, []))

    def hourly(self, sym, start, end):
        return self._get(f"{sym}_1h_{start:%Y%m%d}_{end:%Y%m%d}", lambda: self.c.bars(sym, "1Hour", start=start, end=end).get(sym, []))

    def daily(self, sym, start, end):
        return self._get(f"{sym}_1d_{start:%Y%m%d}_{end:%Y%m%d}", lambda: self.c.bars(sym, "1Day", start=start, end=end).get(sym, []))


def next_session_bars(bars, sym, day):
    for k in range(1, 6):
        d = day + timedelta(days=k)
        if d.weekday() < 5:
            m = bars.minute(sym, d)
            if m:
                return d, m
    return None, []


def walk(bs, long, stop, target, t_exit):
    """-> (exit_px, exit_ts, kind). stop/target None = pure time exit."""
    for b in bs:
        t = b["_t"]
        if t >= t_exit:
            return b["o"], t, "time"
        if stop is not None:
            if (b["l"] <= stop) if long else (b["h"] >= stop):
                return (min(b["o"], stop) if long else max(b["o"], stop)), t, "stop_loss"
            if (b["h"] >= target) if long else (b["l"] <= target):
                return (max(b["o"], target) if long else min(b["o"], target)), t, "take_profit"
    if bs:
        return bs[-1]["c"], bs[-1]["_t"], "end_of_data"
    return None, None, None


def simulate_set(events, bars, variant, slip_bps, P, atr, bracket_levels, size_qty, chase_atr):
    """Sequential portfolio simulation of one event set. -> (trades, skipped counts)."""
    eq0 = P["equity"]
    trades, skipped = [], {}
    k = slip_bps / 10000

    def skip(r):
        skipped[r] = skipped.get(r, 0) + 1

    for ev in sorted(events, key=lambda e: e["t_entry"]):
        t_entry = ev["t_entry"]
        et = t_entry.astimezone(ET)
        if et.weekday() >= 5 or et.strftime("%H:%M") < "09:30" or et.strftime("%H:%M") >= "15:30":
            skip("outside 09:30-15:30 ET"); continue
        closed = [t for t in trades if t["exit_ts"] <= t_entry]
        open_pos = [t for t in trades if t["exit_ts"] > t_entry]
        if any(t["ticker"] == ev["ticker"] for t in open_pos):
            skip("position already open in ticker"); continue
        day = et.date()
        if any(t["day"] == day and t["ticker"] == ev["ticker"] and t["dir"] != ev["dir"] for t in trades):
            skip("opposite-direction cooldown"); continue
        run = 0
        for t in sorted([t for t in closed if t["day"] == day], key=lambda t: t["exit_ts"]):
            run = run + 1 if t["kind"] == "stop_loss" else 0
        if run >= P["loss_streak_stops"]:
            skip("loss-streak breaker"); continue
        bs = [b for b in bars.minute(ev["ticker"], day) if b["_t"] >= t_entry]
        if not bs:
            skip("no entry bar"); continue
        hb = [b for b in bars.hourly(ev["ticker"], P["h_start"], P["h_end"])
              if b["_t"] + timedelta(hours=1) <= t_entry and b["_t"] >= t_entry - timedelta(days=7)]
        e0 = bs[0]
        entry = float(e0["o"])
        a = atr(hb, 14) if len(hb) >= 2 else None
        side = "buy" if ev["dir"] == "long" else "sell"
        stop, tp, dist = bracket_levels(side, entry, a, P["atr_mult_stop"], P["tp_mult"], P["min_stop_pct"], P["max_stop_pct"])
        ch = chase_atr(ev["dir"], ev.get("price"), entry, a or dist / P["atr_mult_stop"])
        if ch is not None and ch > P["max_chase_atr"]:
            skip("max chase > 1 ATR"); continue
        equity = eq0 + sum(t["net"] for t in closed)
        qty = size_qty(equity, sum(t["notional"] for t in open_pos), entry, P["max_trade_pct"], P["max_total_pct"], dist,
                       P["risk_per_trade_pct"])
        if qty < 1:
            skip("size 0 (exposure cap)"); continue
        long = side == "buy"
        close_cut = et_dt(day, 15, 45)
        path = bs
        if variant in ("bracket_60m", "time_1h"):
            t_exit = min(e0["_t"] + timedelta(minutes=60), close_cut)
        elif variant in ("bracket_eod", "time_eod"):
            t_exit = close_cut
        else:
            nd, nm = next_session_bars(bars, ev["ticker"], day)
            if not nm:
                skip("no next-session bars"); continue
            t_exit = datetime.combine(nd, e0["_t"].astimezone(ET).time(), ET).astimezone(timezone.utc)
            path = bs + nm
        br = variant.startswith("bracket")
        px, xts, kind = walk(path, long, stop if br else None, tp if br else None, t_exit)
        if px is None:
            skip("no exit bar"); continue
        sgn = 1 if long else -1
        gross = (px - entry) * qty * sgn
        net = ((px * (1 - k * sgn)) - entry * (1 + k * sgn)) * qty * sgn
        trades.append({"set": ev["set"], "ticker": ev["ticker"], "dir": ev["dir"], "src": ev["src"], "day": day,
                       "entry_ts": e0["_t"], "exit_ts": xts, "entry": entry, "exit": px, "qty": qty, "notional": qty * entry,
                       "stop": stop, "tp": tp, "dist": dist, "kind": kind, "gross": gross, "net": net,
                       "r": net / (dist * qty) if dist else None, "chase_atr": ch})
    return trades, skipped


def stats(trades, eq0=100_000):
    n = len(trades)
    if not n:
        return {"n": 0}
    net = [t["net"] for t in trades]
    gp, gl = sum(x for x in net if x > 0), -sum(x for x in net if x < 0)
    cur = peak = mdd = 0.0
    for t in sorted(trades, key=lambda t: t["exit_ts"]):
        cur += t["net"]; peak = max(peak, cur); mdd = max(mdd, peak - cur)
    by, kinds = {}, {}
    for t in trades:
        b = by.setdefault(t["ticker"], {"n": 0, "net": 0.0, "wins": 0})
        b["n"] += 1; b["net"] += t["net"]; b["wins"] += t["net"] > 0
        kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
    mean = sum(net) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in net) / (n - 1)) if n > 1 else None
    return {"n": n, "days": len({t["day"] for t in trades}), "win_rate": sum(1 for x in net if x > 0) / n, "avg_net": mean,
            "avg_net_bps": sum(t["net"] / t["notional"] * 1e4 for t in trades) / n, "total_net": sum(net),
            "total_pct": sum(net) / eq0 * 100, "max_dd": mdd, "pf": gp / gl if gl else None,
            "t_stat": (mean / (sd / math.sqrt(n))) if sd else None, "avg_r": sum(t["r"] or 0 for t in trades) / n,
            "by_ticker": by, "exits": kinds}


def load_events(con, db, newsgate, backtest):
    """-> dict set_name -> list of events {set, ticker, dir, t_entry, price, src}."""
    trig = backtest.load_triggers(con)
    by_key = {(t["type"], t["id"]): t for t in trig}
    cfg = {"news_window_min": 60, "news_novelty_min": 0.5, "news_min_tier": 2, **db.load_config().get("paper_trader", {})}
    sets = {k: [] for k in ("fade_logged", "fade_old_gate", "fade_new_gate", "news_logged", "news_new_gate")}
    for d in con.execute("SELECT * FROM decisions WHERE rule='v2_attention_fade' OR (rule='v2_news_momentum' AND action='trade')"):
        t = by_key.get((d["trigger_type"], d["trigger_id"]))
        if not t:
            continue
        name = "fade_logged" if d["rule"] == "v2_attention_fade" else "news_logged"
        sets[name].append({"set": name, "ticker": d["ticker"], "dir": d["direction"], "price": t["price"], "src": f"decision {d['id']}",
                           "t_entry": db.parse_ts(d["ts"]) + timedelta(minutes=1)})
    for t in trig:
        t_dec = db.parse_ts(t["decision_ts"]) if t.get("decision_ts") else db.parse_ts(t["ts"])
        con.execute("DROP TABLE IF EXISTS temp.media"); con.execute("DROP TABLE IF EXISTS temp.news")
        con.execute("CREATE TEMP TABLE media AS SELECT * FROM main.media WHERE ticker=? AND (fetched_at IS NULL OR fetched_at <= ?)",
                    (t["ticker"], db.iso(t_dec)))
        con.execute("CREATE TEMP TABLE news AS SELECT * FROM main.news WHERE ticker=? AND (fetched_at IS NULL OR fetched_at <= ?)",
                    (t["ticker"], db.iso(t_dec)))
        args = (con, t["ticker"], t["ts"], t["direction"], cfg["news_window_min"], cfg["news_novelty_min"], cfg["news_min_tier"])
        old = newsgate.hard_news(*args, quality=False)
        new = newsgate.hard_news(*args, now_iso=db.iso(t_dec))
        flip = "short" if t["direction"] == "long" else "long"
        base = {"ticker": t["ticker"], "price": t["price"], "src": f"{t['type']} {t['id']}", "t_entry": t_dec + timedelta(minutes=1)}
        if not old:
            sets["fade_old_gate"].append({**base, "set": "fade_old_gate", "dir": flip})
        if not new:
            sets["fade_new_gate"].append({**base, "set": "fade_new_gate", "dir": flip})
        else:
            sets["news_new_gate"].append({**base, "set": "news_new_gate", "dir": t["direction"]})
    con.execute("DROP TABLE IF EXISTS temp.media"); con.execute("DROP TABLE IF EXISTS temp.news")
    return sets


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="DB copy to read (default: live DB, read-only)"); ap.add_argument("--slippage-bps", default="2,5,10")
    ap.add_argument("--json"); ap.add_argument("--cache")
    a = ap.parse_args()
    if a.db:
        os.environ["STOCK_SENT_DB"] = a.db
    import db, newsgate, backtest
    from paper_trader import DEFAULTS, RISK_DEFAULTS, atr, bracket_levels, size_qty, chase_atr
    from alpaca_client import AlpacaPaper
    con = db.connect(a.db) if a.db else db.connect_ro()
    sets = load_events(con, db, newsgate, backtest)
    c = db.load_config()
    P = {**DEFAULTS, **c.get("paper_trader", {}), **RISK_DEFAULTS,
         **{k: v for k, v in c.get("risk", {}).items() if not k.startswith("_")}, "equity": 100_000}
    allt = [e["t_entry"] for s in sets.values() for e in s]
    P["h_start"] = (min(allt) - timedelta(days=10)).replace(hour=0, minute=0, second=0, microsecond=0)
    P["h_end"] = (max(allt) + timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)
    bars = DiskBars(AlpacaPaper(), a.cache or os.path.join(db.HERE, "data", "backtest", "cache"))
    out = {"generated": db.iso(db.utcnow()), "window": [db.iso(min(allt)), db.iso(max(allt))],
           "events": {k: len(v) for k, v in sets.items()}, "results": {}, "skipped": {}, "trades": {}}
    for slip in [float(x) for x in a.slippage_bps.split(",")]:
        for name, evs in sets.items():
            for v in VARIANTS:
                tr, sk = simulate_set(evs, bars, v, slip, P, atr, bracket_levels, size_qty, chase_atr)
                key = f"{name}|{v}|{slip:g}"
                out["results"][key], out["skipped"][key] = stats(tr), sk
                if slip == 5:
                    out["trades"][f"{name}|{v}"] = [{k2: (str(x) if k2 in ("day", "entry_ts", "exit_ts") else x)
                                                     for k2, x in t.items()} for t in tr]
    bh = {}
    d0, d1 = min(allt).astimezone(ET).date(), max(allt).astimezone(ET).date()
    for sym in ("SPY", "QQQ"):
        dl = [b for b in bars.daily(sym, P["h_start"], P["h_end"]) if d0 <= b["_t"].astimezone(ET).date() <= d1]
        if dl:
            bh[sym] = {"from": str(dl[0]["_t"].date()), "to": str(dl[-1]["_t"].date()), "ret_pct": (dl[-1]["c"] / dl[0]["o"] - 1) * 100}
    out["buy_hold"] = bh
    f = lambda v, p=2: "n/a" if v is None else f"{v:+.{p}f}"
    print(f"window {out['window'][0]} .. {out['window'][1]}; events {out['events']}")
    print("buy&hold: " + ", ".join(f"{k} {v['ret_pct']:+.2f}% ({v['from']}..{v['to']})" for k, v in bh.items()))
    for key, s in out["results"].items():
        if not s.get("n"):
            print(f"  {key:34} n=0  skipped {out['skipped'][key]}"); continue
        print(f"  {key:34} n={s['n']:3d} days={s['days']:2d} win={s['win_rate']*100:4.0f}% avg=${s['avg_net']:+7.2f} "
              f"({s['avg_net_bps']:+6.1f} bps) total=${s['total_net']:+8.2f} ({s['total_pct']:+.3f}%) maxDD=${s['max_dd']:7.2f} "
              f"PF={f(s['pf'])} t={f(s['t_stat'])} exits={s['exits']}")
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1, default=str)
        print(f"json: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
