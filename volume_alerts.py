#!/usr/bin/env python3
"""Volume-spike alerts on Alpaca IEX bars (zero X calls).

Usage: volume_alerts.py [--core-only] [--scan] [--tickers A,B] [--no-store]

* Pulls 5Min IEX bars (regular hours 09:30-16:00 ET) for the watchlist in config.json for the last
  ~20 calendar days, and resamples to 15Min and 1Hour buckets (ET clock-aligned; the 09:00 hourly
  bucket covers 09:30-10:00).
* For the latest COMPLETE bucket of each timeframe in the latest session (or every bucket of the
  latest session with --scan), compares its volume with the mean volume of the same ET time-of-day
  bucket over the prior `baseline_sessions` (default 10) sessions (a missing IEX bar = 0 volume).
  IEX is a small slice of consolidated volume, so only ratios are meaningful.
* Price move = bucket close vs previous bucket close (first bucket of a session: vs its open).
* SPIKE (flagged=1) when ratio >= ratio_threshold (3) AND |move| >= min_move_pct[timeframe]
  (0.30% / 0.50% / 0.80%). Ratio-only hits are stored with flagged=0.
* Rows go to `volume_spikes` (unique per ticker/timeframe/bucket, so re-runs don't duplicate).
Output lines starting with 'SPIKE' are flagged; exit 10 if a NEW flagged spike on a core ticker
was stored (the runbook may then spend <=2 budget-checked X calls on that ticker).
"""
import argparse, statistics as st, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import db
from alpaca_client import AlpacaPaper

ET = ZoneInfo("America/New_York")
ZRH = ZoneInfo("Europe/Zurich")
TF_MIN = {"5Min": 5, "15Min": 15, "1Hour": 60}


def bucket_start(t_et, tf):
    m = TF_MIN[tf]
    if m == 60:
        return t_et.replace(minute=0, second=0, microsecond=0)
    return t_et.replace(minute=(t_et.minute // m) * m, second=0, microsecond=0)


def regular(t_et):
    hm = t_et.hour * 60 + t_et.minute
    return t_et.weekday() < 5 and 570 <= hm < 960


def build_buckets(bars):
    """bars: list of Alpaca 5Min bars -> {tf: {session_date: {bucket_time(HH:MM): dict(o,c,v,start)}}}"""
    out = {tf: defaultdict(dict) for tf in TF_MIN}
    for b in bars:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        if not regular(t):
            continue
        for tf in TF_MIN:
            bs = bucket_start(t, tf)
            key = bs.strftime("%H:%M")
            d = out[tf][t.date()].get(key)
            if d is None:
                out[tf][t.date()][key] = {"o": b["o"], "c": b["c"], "v": b["v"], "start": bs}
            else:
                d["c"] = b["c"]; d["v"] += b["v"]
    return out


def evaluate(buckets, tf, now, cfg, scan=False):
    """Yield dict rows for the latest session's bucket(s)."""
    sessions = sorted(buckets[tf])
    if not sessions:
        return []
    last = sessions[-1]
    prior = sessions[:-1][-cfg.get("baseline_sessions", 10):]
    day = buckets[tf][last]
    keys = sorted(day)
    complete = [k for k in keys if day[k]["start"] + timedelta(minutes=TF_MIN[tf]) <= now.astimezone(ET)
                or last < now.astimezone(ET).date()]
    if not complete:
        return []
    targets = complete if scan else complete[-1:]
    rows = []
    for k in targets:
        cur = day[k]
        base = [buckets[tf][s].get(k, {"v": 0})["v"] for s in prior]
        if len(base) < cfg.get("min_baseline_n", 5):
            continue
        m = st.mean(base)
        ratio = cur["v"] / m if m > 0 else None
        i = keys.index(k)
        ref = day[keys[i - 1]]["c"] if i > 0 else cur["o"]
        ret = (cur["c"] / ref - 1) * 100 if ref else 0.0
        rows.append({"start": cur["start"], "key": k, "session": last, "volume": cur["v"], "base": m,
                     "n": len(base), "ratio": ratio, "ret": ret, "price": cur["c"]})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--core-only", action="store_true")
    ap.add_argument("--scan", action="store_true", help="evaluate every bucket of the latest session")
    ap.add_argument("--tickers")
    ap.add_argument("--no-store", action="store_true")
    a = ap.parse_args()
    conf = db.load_config()
    cfg = conf.get("volume_alerts", {})
    core = conf.get("watchlist_core", db.TICKERS)
    tickers = a.tickers.split(",") if a.tickers else core + ([] if a.core_only or not conf.get("use_extra") else
                                                            conf.get("watchlist_extra", []))
    th = cfg.get("ratio_threshold", 3.0)
    mm = cfg.get("min_move_pct", {"5Min": 0.3, "15Min": 0.5, "1Hour": 0.8})
    now = db.utcnow()
    bars = AlpacaPaper().bars(tickers, "5Min", start=now - timedelta(days=20))
    con = db.connect()
    et_today = now.astimezone(ET).date()
    earn = {r[0] for r in con.execute("SELECT ticker FROM earnings WHERE date BETWEEN ? AND ?",
                                      (str(et_today - timedelta(days=1)), str(et_today + timedelta(days=1))))}
    new_core = 0
    print(f"volume_alerts @ {now.astimezone(ZRH):%Y-%m-%d %H:%M} Zurich | {len(tickers)} tickers | IEX 5Min, "
          f"baseline = same ET time, prior {cfg.get('baseline_sessions', 10)} sessions")
    for t in tickers:
        bk = build_buckets(bars.get(t, []))
        parts = []
        for tf in TF_MIN:
            for r in evaluate(bk, tf, now, cfg, a.scan):
                flagged = r["ratio"] is not None and r["ratio"] >= th and abs(r["ret"]) >= mm.get(tf, 0.5)
                hit = r["ratio"] is not None and r["ratio"] >= th
                tag = f"{tf} {r['key']}ET x{r['ratio']:.1f} {r['ret']:+.2f}%" if r["ratio"] else f"{tf} {r['key']}ET n/a"
                if not a.scan:
                    parts.append(tag)
                    if not a.no_store:
                        con.execute("""INSERT OR REPLACE INTO volume_state(ticker, timeframe, ts, ratio, ret_pct, updated_at)
                                       VALUES (?,?,?,?,?,?)""", (t, tf, db.iso(r["start"]), r["ratio"], r["ret"], db.iso(now)))
                if hit and not a.no_store:
                    cur = con.execute("""INSERT OR IGNORE INTO volume_spikes(ts, ticker, timeframe, detected_at,
                        session_date, volume, baseline_avg, baseline_n, ratio, ret_pct, direction, price, flagged, notes)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (db.iso(r["start"]), t, tf, db.iso(now), str(r["session"]), r["volume"], r["base"], r["n"],
                         r["ratio"], r["ret"], "up" if r["ret"] > 0 else "down", r["price"], int(flagged),
                         ("IEX volume; scan" if a.scan else "IEX volume") + ("; EARNINGS window" if t in earn else "")))
                    if flagged and cur.rowcount and t in core:
                        new_core += 1
                if flagged:
                    zt = r["start"].astimezone(ZRH)
                    print(f"SPIKE{' [earnings]' if t in earn else ''} {t} {tf} bucket {r['session']} {r['key']} ET ({zt:%H:%M} Zurich): vol {r['volume']:.0f} "
                          f"vs avg {r['base']:.0f} = x{r['ratio']:.1f}, move {r['ret']:+.2f}%, px {r['price']:.2f}")
        if parts:
            print(f"  {t:5} " + " | ".join(parts))
    con.commit()
    if not new_core:
        print("no new core-ticker spike")
    return 10 if new_core else 0


if __name__ == "__main__":
    sys.exit(main())
