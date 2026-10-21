#!/usr/bin/env python3
"""Forward returns of logged signals vs SPY (research only; prices via yfinance, unofficial).

Usage: evaluate.py [--recompute] [--signal ID]

Part 1 (signals): sentiment_shock and composite (hypothetical) signals, follow/fade, vs SPY.
Part 2 (decisions, v2): every paper_trader decision (rules v2_news_momentum, v2_attention_fade, v1_follow_spike,
earnings_drift; traded or hypothetical) at 15m/1h/1d, net of 3 bps slippage per side (config evaluation.slippage_bps),
excess vs SPY and QQQ, against a random-entry null arm (same ticker/direction/horizon, 5 random minutes in the same
session), plus SPY/QQQ buy-and-hold since the paper start vs paper equity. n and distinct days are shown: results
cluster by day, and ~100 trades per arm are needed before reading anything into them.

For every signal and horizon in 15m, 1h, 1d, 5d whose horizon has elapsed:
  entry  = first 1m bar close at/after the signal ts (prepost incl.); fallback = logged price
  exit   = last bar close at/before ts + horizon (15m/1h from 1m bars, which Yahoo keeps ~7 days;
           1d/5d = +1/+5 business days from 1h bars, prepost incl.)
  ret = exit/entry - 1 for the ticker and SPY over the same window
  excess_signed = (ret - spy_ret) * (+1 long / -1 short)
Results go to signal_returns; a summary by variant x horizon is printed (n, mean, hit rate).
NOTE: 1m bars are only available for ~7 days, so run this at least daily or 15m/1h
evaluations for older signals fall back to 1h bars (coarser).
"""
import argparse, sys
from datetime import datetime, timedelta, timezone
import pandas as pd
import db

HORIZONS = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "1d": None, "5d": None}
_cache = {}


def bars(sym, interval, start, end):
    key = (sym, interval, start.date(), end.date())
    if key not in _cache:
        import yfinance as yf
        h = yf.Ticker(sym).history(start=start.date(), end=(end + timedelta(days=1)).date(),
                                   interval=interval, prepost=True)
        if not h.empty:
            h.index = h.index.tz_convert("UTC")
        _cache[key] = h
    return _cache[key]


def px_at(sym, ts, after, interval):
    h = bars(sym, interval, ts - timedelta(days=1), ts + timedelta(days=1))
    if h.empty:
        return None
    t = pd.Timestamp(ts)
    sel = h[h.index >= t] if after else h[h.index <= t]
    if sel.empty:
        return None
    return float(sel["Close"].iloc[0] if after else sel["Close"].iloc[-1])


def window_ret(sym, t0, t1, interval):
    a, b = px_at(sym, t0, True, interval), px_at(sym, t1, False, interval)
    return (a, b, b / a - 1) if a and b else (a, b, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recompute", action="store_true")
    ap.add_argument("--signal", type=int)
    a = ap.parse_args()
    con = db.connect()
    now = db.utcnow()
    q = "SELECT * FROM signals" + (" WHERE id=?" if a.signal else "")
    sigs = con.execute(q, (a.signal,) if a.signal else ()).fetchall()
    done = 0
    for s in sigs:
        t0 = db.parse_ts(s["ts"])
        for hz, delta in HORIZONS.items():
            if not a.recompute and con.execute("SELECT 1 FROM signal_returns WHERE signal_id=? AND horizon=?",
                                               (s["id"], hz)).fetchone():
                continue
            t1 = t0 + delta if delta else (pd.Timestamp(t0) + pd.offsets.BDay(1 if hz == "1d" else 5)).to_pydatetime()
            if t1 > now:
                continue
            interval = "1m" if delta and now - t0 < timedelta(days=7) else "1h"
            if hz == "15m" and interval != "1m":
                continue  # 1m bars gone (>7d); 15m on hourly bars is meaningless
            try:
                e, x, r = window_ret(s["ticker"], t0, t1, interval)
                _, _, sr = window_ret("SPY", t0, t1, interval)
            except Exception as ex:
                print(f"  [warn] {s['ticker']} #{s['id']} {hz}: {ex}")
                continue
            if r is None or sr is None:
                continue
            e = e or s["price"]
            sign = 1 if s["direction"] == "long" else -1
            con.execute("""INSERT OR REPLACE INTO signal_returns(signal_id, horizon, entry_price, exit_price, ret,
                           spy_ret, excess_signed, computed_at) VALUES (?,?,?,?,?,?,?,?)""",
                        (s["id"], hz, e, x, r, sr, sign * (r - sr), db.iso(now)))
            done += 1
    con.commit()
    print(f"evaluated {done} new (signal, horizon) pairs; {len(sigs)} signals total")
    rows = con.execute("""SELECT s.variant, r.horizon, COUNT(*) n, AVG(r.excess_signed) m,
                          AVG(CASE WHEN r.excess_signed>0 THEN 1.0 ELSE 0 END) hit
                          FROM signal_returns r JOIN signals s ON s.id=r.signal_id
                          GROUP BY s.variant, r.horizon ORDER BY s.variant, r.horizon""").fetchall()
    for r in rows:
        print(f"  {r['variant']:6} {r['horizon']:>3}: n={r['n']:3d} mean_excess={r['m']*100:+.2f}% hit={r['hit']*100:.0f}%")
    if not rows:
        print("  (no evaluated signals yet)")
    return 0


if __name__ == "__main__":
    main()


# ======================= v2: rule decisions, slippage, random-entry and buy-and-hold baselines ==============
DEC_H = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "1d": None}


def _t1(t0, hz):
    d = DEC_H[hz]
    return t0 + d if d else (pd.Timestamp(t0) + pd.offsets.BDay(1)).to_pydatetime()


def _interval(t0, hz, now):
    return "1m" if now - t0 < timedelta(days=7) else ("1h" if hz != "15m" else None)


def evaluate_decisions(con, now, cfg):
    import random
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    slip = 2 * cfg.get("slippage_bps", 3) / 10000          # entry + exit
    k_rand = cfg.get("random_entries_per_decision", 5)
    rng = random.Random(cfg.get("random_seed", 42))
    n = 0
    for d in con.execute("SELECT * FROM decisions").fetchall():
        t0 = db.parse_ts(d["ts"])
        sign = 1 if d["direction"] == "long" else -1
        for hz in DEC_H:
            t1 = _t1(t0, hz)
            iv = _interval(t0, hz, now)
            if t1 > now or iv is None or con.execute("SELECT 1 FROM decision_returns WHERE decision_id=? AND horizon=?",
                                                     (d["id"], hz)).fetchone():
                continue
            try:
                _, _, r = window_ret(d["ticker"], t0, t1, iv)
                _, _, sr = window_ret("SPY", t0, t1, iv)
                _, _, qr = window_ret("QQQ", t0, t1, iv)
            except Exception as ex:
                print(f"  [warn] decision {d['id']} {hz}: {ex}"); continue
            if r is None:
                continue
            con.execute("""INSERT OR REPLACE INTO decision_returns(decision_id, horizon, ret, net_signed, spy_ret, qqq_ret,
                           excess_spy, excess_qqq, computed_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (d["id"], hz, r, sign * r - slip, sr, qr,
                         sign * (r - sr) - slip if sr is not None else None,
                         sign * (r - qr) - slip if qr is not None else None, db.iso(now)))
            n += 1
            # random-entry null arm: same ticker + direction + horizon, random minute in the same ET session
            day = t0.astimezone(et).date()
            for k in range(k_rand):
                lo = datetime.combine(day, datetime.strptime("09:45", "%H:%M").time(), et)
                span = (6 * 60 - (60 if hz != "1d" else 0)) if hz != "15m" else 6 * 60
                rt = (lo + timedelta(minutes=rng.randrange(0, max(1, span)))).astimezone(timezone.utc)
                rt1 = _t1(rt, hz)
                if rt1 > now:
                    continue
                try:
                    _, _, rr = window_ret(d["ticker"], rt, rt1, iv)
                except Exception:
                    rr = None
                if rr is not None:
                    con.execute("INSERT OR REPLACE INTO random_baseline(decision_id, k, ts, horizon, net_signed) VALUES (?,?,?,?,?)",
                                (d["id"], k, db.iso(rt), hz, sign * rr - slip))
    con.commit()
    return n


def decision_summary(con):
    rows = con.execute("""SELECT d.rule, d.action, r.horizon, COUNT(*) n, COUNT(DISTINCT substr(d.ts,1,10)) days,
                          AVG(r.net_signed) m, AVG(CASE WHEN r.net_signed>0 THEN 1.0 ELSE 0 END) hit,
                          AVG(r.excess_spy) xs, AVG(r.excess_qqq) xq,
                          (SELECT AVG(b.net_signed) FROM random_baseline b JOIN decisions d2 ON d2.id=b.decision_id
                             WHERE d2.rule=d.rule AND b.horizon=r.horizon) rnd
                          FROM decision_returns r JOIN decisions d ON d.id=r.decision_id
                          GROUP BY d.rule, d.action, r.horizon ORDER BY d.rule, d.action, r.horizon""").fetchall()
    return [dict(r) for r in rows]


def buy_and_hold(con, now, start_iso="2026-10-21T13:30:00Z"):
    """SPY / QQQ buy-and-hold since the paper start vs paper equity (Alpaca)."""
    out = {}
    t0 = db.parse_ts(start_iso)
    if now <= t0:
        return out
    for sym in ("SPY", "QQQ"):
        try:
            _, _, r = window_ret(sym, t0, now, "1h" if now - t0 > timedelta(days=6) else "1m")
            out[sym] = r
        except Exception:
            out[sym] = None
    try:
        from alpaca_client import AlpacaPaper
        out["paper_equity"] = float(AlpacaPaper().account()["equity"]) / 100000 - 1
    except Exception:
        out["paper_equity"] = None
    return out


def main_v2():
    con = db.connect()
    now = db.utcnow()
    cfg = db.load_config().get("evaluation", {})
    n = evaluate_decisions(con, now, cfg)
    print(f"decisions: evaluated {n} new (decision, horizon) pairs; slippage {cfg.get('slippage_bps', 3)} bps/side")
    for r in decision_summary(con):
        f = lambda v: "   n/a" if v is None else f"{v*100:+.2f}%"
        print(f"  {r['rule']:18} {r['action']:12} {r['horizon']:>3}: n={r['n']:3d} days={r['days']:2d} net={f(r['m'])} "
              f"hit={r['hit']*100:3.0f}% random={f(r['rnd'])} exSPY={f(r['xs'])} exQQQ={f(r['xq'])}")
    bh = buy_and_hold(con, now)
    if bh:
        print("  buy&hold since paper start: " + ", ".join(f"{k} {v*100:+.2f}%" if v is not None else f"{k} n/a" for k, v in bh.items()))


if __name__ == "__main__" and len(sys.argv) > 0:
    main_v2()
