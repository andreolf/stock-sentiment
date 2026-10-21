#!/usr/bin/env python3
"""Compute per-ticker sentiment/volume state for the latest batch and emit signals.

Usage: signals.py [TICKER ...] [--dry-run] [--no-price] [--min-history 5]

Per ticker:
  * Sample = quality posts in the ticker's latest ingest batch, scored by 'agent'
    (falls back to 'vader' if the agent has not scored that batch).
  * Author-reach-weighted sentiment:  w_i = log10(10 + followers_i) * (0.5 + 0.5*novelty_i)
        w_sentiment = sum(w_i * s_i) / sum(w_i)
  * Volume: current hourly rate = latest counts bucket scaled to 1h (partial bucket used only
    if it spans >= 20 min, else the last complete hour). Baseline = same UTC hour-of-day on the
    prior days in the counts table (up to 7), so US-session vs overnight seasonality cancels.
        volume_ratio = cur_rate / mean(baseline);  volume_z = (cur_rate - mean)/std
  * Sentiment z-score = (w_sentiment - mean(prior runs)) / max(std(prior runs), 0.05) over this
    ticker's previous runs (last 7 days, same scorer). Needs >= --min-history prior runs;
    until then status = 'warmup' and no signal can fire.
  * Signal 'sentiment_shock' when |sent_z| > 2 AND volume_ratio > 3. Two rows are logged:
    variant 'follow' (direction = sign of z) and 'fade' (opposite). Debounce: no new shock for the
    same ticker within 2h of the previous one. Price at signal via yfinance (incl. pre/post).
Every run (signal or not) is appended to the `runs` table; that is the z-score history.
Output: one summary line per ticker; lines starting with 'SIGNAL' mean something fired.
Exit code 0 always (10 if any signal fired, so wrappers can branch).
"""
import argparse, math, statistics as st, sys
from datetime import timedelta
import db

Z_TH, VOL_TH, DEBOUNCE_H = 2.0, 3.0, 2


def batch_sample(con, ticker):
    batch = con.execute("SELECT MAX(batch) FROM post_batches WHERE ticker=? AND batch NOT LIKE 'x-%'",
                        (ticker,)).fetchone()[0]  # 'x-...' = extra spike-narrative pulls, not a regular sample
    if not batch:
        return None, None, []
    for scorer in ("agent", "vader"):
        rows = con.execute("""SELECT p.followers, s.sentiment, s.novelty FROM posts p
            JOIN post_batches b ON b.post_id=p.id AND b.ticker=p.ticker
            JOIN scores s ON s.post_id=p.id AND s.ticker=p.ticker AND s.scorer=?
            WHERE p.ticker=? AND b.batch=? AND p.quality=1""", (scorer, ticker, batch)).fetchall()
        if rows:
            return batch, scorer, rows
    return batch, None, []


def volume_state(con, ticker):
    rows = con.execute("SELECT window_start, window_end, post_count FROM counts WHERE ticker=? ORDER BY window_start",
                       (ticker,)).fetchall()
    if not rows:
        return None, None, None, None
    full = {}
    cur = cur_start = None
    for r in rows:
        s, e = db.parse_ts(r["window_start"]), db.parse_ts(r["window_end"])
        dur = (e - s).total_seconds()
        if abs(dur - 3600) < 1:
            full[s] = r["post_count"]
    last = rows[-1]
    s, e = db.parse_ts(last["window_start"]), db.parse_ts(last["window_end"])
    dur = (e - s).total_seconds()
    if dur >= 1200:
        cur, cur_start = last["post_count"] * 3600 / dur, s
    if cur is None and full:
        cur_start = max(full)
        cur = full[cur_start]
    base = [full[t] for t in full if t.hour == cur_start.hour and t < cur_start - timedelta(minutes=30)][-7:]
    if not base:
        return cur, None, None, cur_start
    m = st.mean(base)
    sd = st.pstdev(base) if len(base) > 1 else 0
    ratio = cur / m if m else None
    z = (cur - m) / sd if sd else None
    return cur, ratio, z, cur_start


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", default=db.TICKERS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-price", action="store_true")
    ap.add_argument("--min-history", type=int, default=5)
    a = ap.parse_args()
    con = db.connect()
    now = db.utcnow()
    fired = 0
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    et_today = _dt.now(_Z("America/New_York")).date()
    earn = {r[0]: f"{r[1]} {r[2] or ''}".strip() for r in con.execute(
        "SELECT ticker, date, time_of_day FROM earnings WHERE date BETWEEN ? AND ?",
        (str(et_today - timedelta(days=1)), str(et_today + timedelta(days=1))))}
    print(f"signals @ {db.local(now)}" + (f" | earnings window: {earn}" if earn else ""))
    for t in [x.upper() for x in a.tickers]:
        batch, scorer, rows = batch_sample(con, t)
        cur, vratio, vz, cur_start = volume_state(con, t)
        if not rows:
            print(f"{t}: no scored quality posts (batch={batch}); vol rate={cur}")
            continue
        sents = [r["sentiment"] for r in rows]
        ws = [math.log10(10 + (r["followers"] or 0)) * (0.5 + 0.5 * (r["novelty"] if r["novelty"] is not None else 0.5))
              for r in rows]
        wsent = sum(w * s for w, s in zip(ws, sents)) / sum(ws)
        avg = st.mean(sents)
        hist = [r[0] for r in con.execute("""SELECT w_sentiment FROM runs WHERE id IN (SELECT MAX(id) FROM runs
                    WHERE ticker=? AND scorer=? AND batch<>? AND ts >= ? GROUP BY batch)""",
                    (t, scorer, batch, db.iso(now - timedelta(days=7))))]
        if len(hist) >= a.min_history:
            sz = (wsent - st.mean(hist)) / max(st.pstdev(hist), 0.05)
            status = "ok"
        else:
            sz, status = None, f"warmup {len(hist)}/{a.min_history}"
        price = None if a.no_price else db.latest_price(t)
        shock = sz is not None and vratio is not None and abs(sz) > Z_TH and vratio > VOL_TH
        if shock:
            last = con.execute("SELECT MAX(ts) FROM signals WHERE ticker=? AND type='sentiment_shock'", (t,)).fetchone()[0]
            if last and db.parse_ts(last) > now - timedelta(hours=DEBOUNCE_H):
                shock, status = False, "shock debounced"
        run_id = None
        if not a.dry_run:
            cur_ins = con.execute("""INSERT INTO runs(ts, ticker, batch, scorer, n_posts, avg_sentiment, w_sentiment,
                sent_z, cur_rate, volume_ratio, volume_z, price, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (db.iso(now), t, batch, scorer, len(rows), avg, wsent, sz, cur, vratio, vz, price, status))
            run_id = cur_ins.lastrowid
        f = lambda v, p=2: "n/a" if v is None else f"{v:.{p}f}"
        print(f"{t}: n={len(rows)} ({scorer}) avg={avg:+.3f} wsent={wsent:+.3f} sent_z={f(sz)} "
              f"vol={f(cur,0)}/h ratio={f(vratio)} vol_z={f(vz)} price={f(price)} [{status}]")
        if shock:
            fired += 1
            d = "long" if sz > 0 else "short"
            for variant, direction in (("follow", d), ("fade", "short" if d == "long" else "long")):
                notes = f"wsent={wsent:+.3f} n={len(rows)} scorer={scorer} batch={batch} vol_z={f(vz)}"
                if t in earn:
                    notes += f" EARNINGS({earn[t]})"
                if not a.dry_run:
                    con.execute("""INSERT INTO signals(ts, ticker, type, variant, zscore, volume_ratio, direction, price,
                                   notes, run_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                (db.iso(now), t, "sentiment_shock", variant, sz, vratio, direction, price, notes, run_id))
            print(f"SIGNAL sentiment_shock {t}: z={sz:+.2f} vol_ratio={vratio:.2f} follow={d} "
                  f"fade={'short' if d == 'long' else 'long'} price={f(price)}")
    if not a.dry_run:
        con.commit()
    if not fired:
        print("no signal")
    return 10 if fired else 0


if __name__ == "__main__":
    sys.exit(main())
