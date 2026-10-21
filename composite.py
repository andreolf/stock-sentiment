#!/usr/bin/env python3
"""Holistic composite score per ticker (zero X calls; uses whatever the other collectors stored).

Usage: composite.py [TICKER ...] [--source routine|collector] [--dry-run]

Raw components (latest values at run time):
  x_sent        X author-reach-weighted sentiment of the latest regular batch (runs.w_sentiment, <= 2h old)
  x_vol         log(X volume ratio) from the same run (same-hour-of-day baseline)
  reddit_chg    log((mentions+1)/(mentions_24h_ago+1)) from ApeWisdom (latest row)
  media_sent    tier-weighted headline sentiment, last 6h (tier-1 x2; media + Benzinga; agent overrides win)
  media_cnt_chg log((headlines last 6h + 1)/(mean 6h count over the prior 7 days + 1)); skipped until we
                have collected media for >= 3 days (feeds only expose their latest items)
  price_vol     log(max IEX volume ratio over 5Min/15Min/1Hour) from volume_alerts (<= 90 min old)
Each component is z-scored against this ticker's own composite history (last 14 days, >= 20 rows); until
then a fixed warm-up scale is used (zmode='warmup'): x_sent/0.15, media_sent/0.15, log-ratios/0.5.
Composite (signed): sentiment direction  S = mean(z_x_sent, z_media_sent) (available ones)
    attention A = mean(z_x_vol, z_reddit_chg, z_media_cnt_chg, z_price_vol) (available ones)
    composite = (S*1.0 + sign(S)*A*0.5) / sqrt(1 + 0.25)   if |S| >= 0.25, else S   (attention amplifies,
    never sets, the direction). Components missing -> skipped (logged in notes).
If |composite| > 2: a HYPOTHETICAL signal type='composite' (follow + fade rows) is logged for evaluate.py.
paper_trader.py ignores type='composite' (it only trades sentiment_shock). Debounce 2h per ticker.
"""
import argparse, math, statistics as st, sys
from datetime import timedelta
import db, media

SCALE = {"x_sent": 0.15, "x_vol": 0.5, "reddit_chg": 0.5, "media_sent": 0.15, "media_cnt_chg": 0.5, "price_vol": 0.5}
COMPS = list(SCALE)
SENT, ATT = ("x_sent", "media_sent"), ("x_vol", "reddit_chg", "media_cnt_chg", "price_vol")


def raw_components(con, t, now):
    c, notes = {}, []
    r = con.execute("SELECT * FROM runs WHERE ticker=? ORDER BY id DESC LIMIT 1", (t,)).fetchone()
    if r and db.parse_ts(r["ts"]) >= now - timedelta(hours=2):
        c["x_sent"] = r["w_sentiment"]
        c["x_vol"] = math.log(r["volume_ratio"]) if r["volume_ratio"] else None
    else:
        notes.append("X stale")
    rc = con.execute("SELECT * FROM reddit_counts WHERE ticker=? AND source='apewisdom' ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
    if rc and db.parse_ts(rc["ts"]) >= now - timedelta(hours=3) and rc["mentions_24h"] is not None:
        c["reddit_chg"] = math.log((rc["mentions"] + 1) / (rc["mentions_24h"] + 1))
    snap = media.snapshot(con, [t], 6, now)[t]
    c["media_sent"] = snap["wsent"]
    n6 = snap["n"]
    tot7 = con.execute("SELECT COUNT(*) FROM media WHERE ticker=? AND created_at BETWEEN ? AND ?",
                       (t, db.iso(now - timedelta(days=7, hours=6)), db.iso(now - timedelta(hours=6)))).fetchone()[0]
    tot7 += con.execute("SELECT COUNT(*) FROM news WHERE ticker=? AND created_at BETWEEN ? AND ?",
                        (t, db.iso(now - timedelta(days=7, hours=6)), db.iso(now - timedelta(hours=6)))).fetchone()[0]
    first = con.execute("SELECT MIN(fetched_at) FROM media WHERE ticker=?", (t,)).fetchone()[0]
    # feeds only expose their latest N items, so the count baseline is only fair once we have collected
    # for >= 3 days ourselves; before that the component is skipped
    if first and now - db.parse_ts(first) >= timedelta(days=3):
        span_h = min(168.0, (now - db.parse_ts(first)).total_seconds() / 3600 - 6)
        c["media_cnt_chg"] = math.log((n6 + 1) / (tot7 / (span_h / 6) + 1))
    else:
        notes.append("media count warmup (<3d collected)")
    vs = con.execute("SELECT MAX(ratio), MAX(updated_at) FROM volume_state WHERE ticker=?", (t,)).fetchone()
    if vs[0] and db.parse_ts(vs[1]) >= now - timedelta(minutes=90):
        c["price_vol"] = math.log(vs[0])
    else:
        notes.append("price vol stale")
    return {k: c.get(k) for k in COMPS}, n6, notes


def zscores(con, t, raw, now):
    hist = con.execute("SELECT * FROM composite WHERE ticker=? AND ts>=?", (t, db.iso(now - timedelta(days=14)))).fetchall()
    z, mode = {}, "history"
    for k in COMPS:
        v = raw[k]
        if v is None:
            z[k] = None; continue
        h = [r[k] for r in hist if r[k] is not None]
        if len(h) >= 20 and st.pstdev(h) > 1e-9:
            z[k] = (v - st.mean(h)) / st.pstdev(h)
        else:
            z[k] = v / SCALE[k]; mode = "warmup"
    return z, mode


def combine(z):
    s = [z[k] for k in SENT if z.get(k) is not None]
    a = [z[k] for k in ATT if z.get(k) is not None]
    if not s:
        return None, None
    S = st.mean(s)
    if abs(S) < 0.25 or not a:
        return S, S
    return S, (S + math.copysign(1, S) * st.mean(a) * 0.5) / math.sqrt(1.25)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--source", default="routine")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    con = db.connect()
    now = db.utcnow()
    tickers = [t.upper() for t in a.tickers] or db.watchlist(True)
    print(f"composite @ {db.local(now)} ({a.source})")
    for t in tickers:
        raw, n6, notes = raw_components(con, t, now)
        z, mode = zscores(con, t, raw, now)
        S, comp = combine(z)
        f = lambda v: "  n/a" if v is None else f"{v:+5.2f}"
        drivers = sorted([(k, v) for k, v in z.items() if v is not None], key=lambda kv: -abs(kv[1]))[:3]
        print(f"{t}: composite {f(comp)} | " + " ".join(f"{k}={f(z[k])}" for k in COMPS) +
              f" | top: {', '.join(f'{k} {v:+.1f}' for k, v in drivers)} [{mode}]" + (f" ({'; '.join(notes)})" if notes else ""))
        if a.dry_run:
            continue
        cur = con.execute(f"""INSERT INTO composite(ts, ticker, source, {', '.join(COMPS)}, {', '.join('z_' + k for k in COMPS)},
                          sent_dir, composite, n_media, zmode, notes) VALUES ({','.join('?' * (5 + 2 * len(COMPS) + 3))})""",
                          (db.iso(now), t, a.source, *[raw[k] for k in COMPS], *[z[k] for k in COMPS], S, comp, n6, mode,
                           "; ".join(notes)))
        if comp is not None and abs(comp) > 2:
            last = con.execute("SELECT MAX(ts) FROM signals WHERE ticker=? AND type='composite'", (t,)).fetchone()[0]
            if last and db.parse_ts(last) > now - timedelta(hours=2):
                print(f"  composite |z|>2 for {t} but debounced"); continue
            price = db.latest_price(t)
            d = "long" if comp > 0 else "short"
            for variant, direction in (("follow", d), ("fade", "short" if d == "long" else "long")):
                con.execute("""INSERT INTO signals(ts, ticker, type, variant, zscore, volume_ratio, direction, price, notes, run_id)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (db.iso(now), t, "composite", variant, comp,
                             math.exp(raw["price_vol"]) if raw["price_vol"] is not None else None, direction, price,
                             "hypothetical composite; top: " + ", ".join(f"{k} {v:+.1f}" for k, v in drivers), cur.lastrowid))
            print(f"COMPOSITE-SIGNAL (hypothetical) {t}: composite {comp:+.2f} -> follow {d}, price {price}")
    con.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
