#!/usr/bin/env python3
"""OBSERVE-ONLY Swiss arm: SIX home-line move, IEX spikes, Swiss news -> hypothetical observations + evaluation.
NEVER places orders (no order code in this module; paper_trader.py/alpaca_client.py refuse these symbols anyway).
Zero X calls by default.

Usage:
  swiss.py collect [--no-news] [--no-observe]  one tick: swiss_news fetch -> SIX move -> IEX spikes (US hours)
                                               -> observations. The collector runs this every 5 min.
  swiss.py six                                 SIX move snapshot only (prints table)
  swiss.py observe                             derive observations from what is stored
  swiss.py evaluate                            fill hypothetical outcomes (+30m, +1d, +3d), random-entry null,
                                               SPY excess, 3 bps/side slippage; 'SIX move -> US open' test; summary
  swiss.py status                              latest SIX moves, observations, feed status
  swiss.py invalidate ID --reason TEXT         mark a false-positive observation status='invalid' (kept, excluded
                                               from evaluate/summary/debounce) and move its headline to media_rejected
  swiss.py x-check                             X is OFF by default (swiss_observe.x_enabled=false). When enabled,
                                               checks budget.py and prints the query; the AGENT then makes the calls
                                               with the built-in x MCP tools and records them with budget.py.

Names (config swiss_observe.names): NVS<->NOVN.SW, UBS<->UBSG.SW, LOGI<->LOGN.SW, ALC<->ALC.SW, ONON (no SIX line).
SIX move (yfinance, unofficial, ~15-20 min DELAYED; delay_min is stored per snapshot):
  six_chg_pct     = SIX last (latest 1m bar today, Zurich date) / SIX previous close - 1
  implied_us      = SIX last * CHFUSD * adr_ratio   (all five are 1:1 lines)
  implied_gap_pct = implied_us / US line's last regular close - 1   (the gap the US line 'should' open with)
Windows (Zurich): SIX continuous trading 09:00-17:30; ad hoc news for primary listings comes before 07:30 or
after 17:40; US lines trade 15:30-22:00 Zurich, so 17:40-22:00 is the window where Swiss news hits only the US line.
Observation rules (all hypothetical, swiss_observations.observe_only = 1):
  six_move_gap      ET 08:45-09:30 on a US session day, |implied_gap| >= 1.0% -> direction = sign(gap),
                    entry = next US open (filled by evaluate from yfinance). One per ticker per US session.
  swiss_news        new, novel (>= 0.5) Swiss/Benzinga headline with |sentiment| >= 0.2 (SER purchase +0.5, sale -0.3)
                    -> direction = sign(sentiment); entry = IEX mid now if US open else next US open. Debounce 60 min.
  swiss_spike_news  flagged IEX volume spike (same thresholds as volume_alerts) on the US line, backed by
                    newsgate.hard_news() -> direction = move (news momentum)
  swiss_spike_fade  flagged IEX spike without fresh novel news -> fade (attention-only)
  swiss_composite   S = mean(z media sentiment 6h [/0.15], z SIX move [six_chg/1%]); A = z IEX volume [log ratio/0.5];
                    composite.py formula (S + sign(S)*A/2)/sqrt(1.25); |c| > 2 -> sign(c). Needs headline sentiment
                    (a SIX move alone is the six_move_gap rule). Debounce 2 h.
"""
import argparse, json, math, os, random, statistics as st, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import db

ET = ZoneInfo("America/New_York")
ZRH = ZoneInfo("Europe/Zurich")
HZ = {"30m": timedelta(minutes=30), "1d": 1, "3d": 3}


def cfg(conf=None):
    so = (conf or db.load_config()).get("swiss_observe", {})
    if not so.get("observe_only", True):
        raise SystemExit("swiss_observe.observe_only must stay true (Francesco approved observe-only)")
    return so


def zrh(s):
    return (db.parse_ts(s) if isinstance(s, str) else s).astimezone(ZRH)


def window_of(ts):
    """Zurich-clock window label for an event time."""
    z = zrh(ts)
    hm = z.strftime("%H:%M")
    if z.weekday() >= 5:
        return "weekend"
    if hm < "07:30":
        return "pre_0730_adhoc"
    if hm < "09:00":
        return "pre_six_open"
    if hm < "17:30":
        return "six_session"
    if hm < "17:40":
        return "six_close_auction"
    if hm < "22:00":
        return "post_1740_us_only"
    return "late"


def six_state(now):
    z = now.astimezone(ZRH)
    if z.weekday() >= 5:
        return "closed"
    hm = z.strftime("%H:%M")
    return "open" if "09:00" <= hm < "17:30" else ("pre" if hm < "09:00" else "closed")


def us_regular_open(now):
    t = now.astimezone(ET)
    hm = t.hour * 60 + t.minute
    return t.weekday() < 5 and 570 <= hm < 960


# ---------------- SIX move ----------------
def compute_move(six_last, six_prev, fx, us_prev, ratio=1.0):
    """Pure helper -> (six_chg_pct, implied_us, implied_gap_pct)."""
    chg = (six_last / six_prev - 1) * 100 if six_last and six_prev else None
    imp = six_last * fx * ratio if six_last and fx else None
    gap = (imp / us_prev - 1) * 100 if imp and us_prev else None
    return chg, imp, gap


def _hist(sym, **kw):
    import yfinance as yf
    h = yf.Ticker(sym).history(**kw)
    return h if h is not None else None


def six_snapshot(con, so, now, store=True):
    rows = []
    try:
        fxh = _hist(so.get("fx_symbol", "CHFUSD=X"), period="1d", interval="1m")
        fx = float(fxh["Close"].dropna().iloc[-1]) if fxh is not None and not fxh.empty else None
    except Exception as ex:
        print(f"  [warn] fx: {ex}"); fx = None
    today_z = now.astimezone(ZRH).date()
    for t, v in so.get("names", {}).items():
        six = v.get("six")
        if not six:
            continue
        try:
            d = _hist(six, period="10d", interval="1d")
            m = _hist(six, period="1d", interval="1m")
            u = _hist(t, period="10d", interval="1d")
        except Exception as ex:
            print(f"  [warn] {t}/{six}: {ex}"); continue
        if d is None or d.empty or u is None or u.empty:
            continue
        d = d.dropna(subset=["Close"])
        dd = [x.date() for x in d.index]
        if m is not None and not m.empty and m.index[-1].astimezone(ZRH).date() == today_z:
            last, bar_ts = float(m["Close"].dropna().iloc[-1]), m.index[-1].to_pydatetime().astimezone(timezone.utc)
            prev = float(d["Close"].iloc[-2]) if dd[-1] == today_z and len(d) > 1 else float(d["Close"].iloc[-1])
        else:  # SIX has not traded today yet (pre-open/weekend): last close vs the close before
            last, bar_ts = float(d["Close"].iloc[-1]), None
            prev = float(d["Close"].iloc[-2]) if len(d) > 1 else None
        # US line: last COMPLETED regular session close
        u = u.dropna(subset=["Close"])
        et_today = now.astimezone(ET).date()
        ud = [x.date() for x in u.index]
        if ud[-1] == et_today and (us_regular_open(now) or now.astimezone(ET).hour < 16):
            us_prev, us_prev_date = float(u["Close"].iloc[-2]), str(ud[-2])
        else:
            us_prev, us_prev_date = float(u["Close"].iloc[-1]), str(ud[-1])
        chg, imp, gap = compute_move(last, prev, fx, us_prev, v.get("adr_ratio", 1.0))
        delay = (now - bar_ts).total_seconds() / 60 if bar_ts else None
        r = {"ts": db.iso(now), "ticker": t, "six_symbol": six, "zrh_date": str(today_z), "six_last": last,
             "six_bar_ts": db.iso(bar_ts) if bar_ts else None, "delay_min": delay, "six_prev_close": prev,
             "six_chg_pct": chg, "fx_chfusd": fx, "us_prev_close": us_prev, "us_prev_date": us_prev_date,
             "implied_us": imp, "implied_gap_pct": gap, "six_state": six_state(now), "us_open": int(us_regular_open(now)),
             "notes": "yfinance .SW ~15-20 min delayed" + ("" if bar_ts else "; no SIX trade today yet (last close)")}
        rows.append(r)
        if store:
            con.execute(f"INSERT INTO six_moves({','.join(r)}) VALUES ({','.join('?' * len(r))})", tuple(r.values()))
    if store:
        con.commit()
    return rows


# ---------------- IEX spikes on the US lines ----------------
def iex_spikes(con, so, now):
    import volume_alerts as va
    from alpaca_client import AlpacaPaper
    vcfg = db.load_config().get("volume_alerts", {})
    mm = vcfg.get("min_move_pct", {"5Min": 0.3, "15Min": 0.5, "1Hour": 0.8})
    th = vcfg.get("ratio_threshold", 3.0)
    tick = list(so.get("names", {}))
    bars = AlpacaPaper().bars(tick, "5Min", start=now - timedelta(days=20))
    new = []
    for t in tick:
        bk = va.build_buckets(bars.get(t, []))
        for tf in va.TF_MIN:
            for r in va.evaluate(bk, tf, now, vcfg):
                flagged = int(r["ratio"] is not None and r["ratio"] >= th and abs(r["ret"]) >= mm[tf])
                cur = con.execute("""INSERT OR IGNORE INTO swiss_iex(ts, ticker, timeframe, detected_at, volume, baseline_avg,
                                     ratio, ret_pct, price, flagged) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                  (db.iso(r["start"]), t, tf, db.iso(now), r["volume"], r["base"], r["ratio"], r["ret"],
                                   r["price"], flagged))
                if cur.rowcount and flagged:
                    new.append({"id": cur.lastrowid, "ticker": t, "tf": tf, "ts": db.iso(r["start"]), "ratio": r["ratio"],
                                "ret": r["ret"], "price": r["price"]})
                    print(f"SWISS-SPIKE {t} {tf} x{r['ratio']:.1f} {r['ret']:+.2f}% @ {r['price']}")
    con.commit()
    return new


def iex_mid(t):
    try:
        from alpaca_client import AlpacaPaper
        c = AlpacaPaper()
        q = c.latest_quote(t) or {}
        if q.get("bp") and q.get("ap") and q["ap"] >= q["bp"] > 0:
            return round((q["bp"] + q["ap"]) / 2, 4)
        return float((c.latest_trade(t) or {}).get("p") or 0) or None
    except Exception:
        return None


# ---------------- observations ----------------
def log_obs(con, now, t, so, rule, ttype, ref, direction, reason, news_ref=None, event_ts=None, price_fn=iex_mid, extra=None):
    if con.execute("SELECT 1 FROM swiss_observations WHERE rule=? AND trigger_type=? AND trigger_ref=?",
                   (rule, ttype, str(ref))).fetchone():
        return None
    if us_regular_open(now):
        basis, ets, px = "iex_mid_now", db.iso(now), price_fn(t)
    else:
        basis, ets, px = "next_us_open", None, None
    six = so.get("names", {}).get(t, {}).get("six")
    sm = con.execute("SELECT six_chg_pct, implied_gap_pct FROM six_moves WHERE ticker=? ORDER BY id DESC LIMIT 1", (t,)).fetchone()
    cur = con.execute("""INSERT INTO swiss_observations(ts, ticker, six_symbol, rule, trigger_type, trigger_ref, direction, reason,
                         news_ref, window, entry_basis, entry_ts, entry_price, six_chg_pct, implied_gap_pct, observe_only)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                      (db.iso(now), t, six, rule, ttype, str(ref), direction, reason, news_ref, window_of(event_ts or now),
                       basis, ets, px, sm[0] if sm else None, sm[1] if sm else None))
    print(f"SWISS-OBS (observe-only) {rule} {t} {direction} entry={basis}{'' if px is None else f' {px}'} | {reason[:110]}")
    return cur.lastrowid


def reject_headline(con, media_id, ticker, reason, now):
    """Move a false-positive headline from `media` to `media_rejected` (copy + reason, then delete from media so it no
    longer feeds media sentiment / newsgate / composite). Returns True if a row was moved."""
    r = con.execute("SELECT * FROM media WHERE id=? AND ticker=?", (media_id, ticker)).fetchone()
    if not r:
        return False
    con.execute("""INSERT OR REPLACE INTO media_rejected(id, ticker, created_at, outlet, tier, title, url, feed, sentiment, scorer,
                   novelty, lang, fetched_at, reason, rejected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["id"], r["ticker"], r["created_at"], r["outlet"], r["tier"], r["title"], r["url"], r["feed"], r["sentiment"],
                 r["scorer"], r["novelty"], r["lang"], r["fetched_at"], reason, db.iso(now)))
    con.execute("DELETE FROM media WHERE id=? AND ticker=?", (media_id, ticker))
    return True


def invalidate(con, obs_id, reason, now=None):
    """Mark an observation status='invalid' (row kept for audit; excluded from evaluate/summary/debounce), drop its
    outcomes/random-null rows if any, and quarantine its trigger headline. Returns whether a headline was moved."""
    now = now or db.utcnow()
    o = con.execute("SELECT * FROM swiss_observations WHERE id=?", (obs_id,)).fetchone()
    if not o:
        raise SystemExit(f"no observation {obs_id}")
    con.execute("UPDATE swiss_observations SET status='invalid', excluded_reason=?, excluded_at=? WHERE id=?",
                (reason, db.iso(now), obs_id))
    con.execute("DELETE FROM swiss_obs_returns WHERE obs_id=?", (obs_id,))
    con.execute("DELETE FROM swiss_random WHERE obs_id=?", (obs_id,))
    moved = False
    if o["rule"] == "swiss_news" and o["trigger_type"] != "benzinga":
        moved = reject_headline(con, o["trigger_ref"], o["ticker"], reason, now)
    con.commit()
    return moved


def zcombine(z_media, z_six, z_vol):
    s = [x for x in (z_media, z_six) if x is not None]
    if not s:
        return None
    S = st.mean(s)
    if abs(S) < 0.25 or z_vol is None:
        return S
    return (S + math.copysign(1, S) * z_vol * 0.5) / math.sqrt(1.25)


def observe(con, so, now, new_news=(), new_spikes=(), six_rows=(), price_fn=iex_mid, backfill=False):
    import newsgate
    n = 0
    thr = so.get("six_move_threshold_pct", 1.0)
    et = now.astimezone(ET)
    lo, hi = so.get("preopen_window_et", ["08:45", "09:30"])
    # 1) SIX move -> US open
    if et.weekday() < 5 and lo <= et.strftime("%H:%M") < hi:
        for r in six_rows:
            g = r.get("implied_gap_pct")
            if g is not None and abs(g) >= thr and r.get("six_bar_ts"):
                d = "long" if g > 0 else "short"
                n += bool(log_obs(con, now, r["ticker"], so, "six_move_gap", "six_preopen", f"{r['ticker']}:{et.date()}", d,
                                  f"SIX {r['six_symbol']} {r['six_chg_pct']:+.2f}% vs prev close (delayed ~{(r['delay_min'] or 0):.0f} min); "
                                  f"implied US gap {g:+.2f}% vs {r['us_prev_close']:.2f}", price_fn=price_fn))
    # 2) news
    deb = timedelta(minutes=so.get("news_debounce_min", 60))
    for r in ([] if backfill else new_news):
        s, nov = r.get("sentiment"), r.get("novelty") or 0
        if s is None or abs(s) < so.get("news_min_abs_sentiment", 0.2) or nov < so.get("news_novelty_min", 0.5):
            continue
        if r.get("kind") == "ser_sig":
            continue
        if db.parse_ts(r["created_at"]) < now - timedelta(hours=24):
            continue   # old item seen for the first time (feed backlog): not an event
        last = con.execute("SELECT MAX(ts) FROM swiss_observations WHERE ticker=? AND rule='swiss_news' AND COALESCE(status,'valid')<>'invalid'",
                           (r["ticker"],)).fetchone()[0]
        if last and db.parse_ts(last) > now - deb:
            continue
        d = "long" if s > 0 else "short"
        n += bool(log_obs(con, now, r["ticker"], so, "swiss_news", r["kind"], r["id"], d,
                          f"{r['feed']} T{r['tier']} nov {nov:.2f} s {s:+.2f}: {r['title'][:90]}",
                          news_ref=f"{r['feed']}:{r['id']}", event_ts=r["created_at"], price_fn=price_fn))
    # 3) IEX spikes -> news gate (same newsgate as the US rules)
    for sp in new_spikes:
        d = "long" if sp["ret"] > 0 else "short"
        news = newsgate.hard_news(con, sp["ticker"], sp["ts"], d, so.get("news_window_min", 60), so.get("news_novelty_min", 0.5), 2)
        if news:
            n += bool(log_obs(con, now, sp["ticker"], so, "swiss_spike_news", "iex_spike", sp["id"], d,
                              f"{sp['tf']} IEX x{sp['ratio']:.1f} {sp['ret']:+.2f}% backed by {news['kind']} T{news['tier']} "
                              f"nov {news['novelty']:.2f}: {news['title'][:70]}", news_ref=f"{news['kind']}:{news['ref']}",
                              event_ts=sp["ts"], price_fn=price_fn))
        else:
            n += bool(log_obs(con, now, sp["ticker"], so, "swiss_spike_fade", "iex_spike", sp["id"], "short" if d == "long" else "long",
                              f"{sp['tf']} IEX x{sp['ratio']:.1f} {sp['ret']:+.2f}% attention-only -> fade", event_ts=sp["ts"],
                              price_fn=price_fn))
    # 4) composite
    import media
    snap = media.snapshot(con, list(so.get("names", {})), 6, now)
    six_by = {r["ticker"]: r for r in six_rows}
    for t in so.get("names", {}):
        ms = snap[t]["wsent"]
        zm = ms / 0.15 if ms is not None else None
        sr = six_by.get(t)
        zs = sr["six_chg_pct"] / 1.0 if sr and sr.get("six_chg_pct") is not None and sr.get("six_bar_ts") else None
        vr = con.execute("SELECT MAX(ratio) FROM swiss_iex WHERE ticker=? AND detected_at>=?", (t, db.iso(now - timedelta(minutes=90)))).fetchone()[0]
        zv = math.log(vr) / 0.5 if vr and vr > 0 else None
        c = zcombine(zm, zs, zv) if zm is not None else None   # needs headline sentiment; SIX move alone = six_move_gap
        if c is not None and abs(c) > so.get("composite_threshold", 2.0) and not backfill:
            last = con.execute("SELECT MAX(ts) FROM swiss_observations WHERE ticker=? AND rule='swiss_composite' AND COALESCE(status,'valid')<>'invalid'",
                               (t,)).fetchone()[0]
            if last and db.parse_ts(last) > now - timedelta(hours=2):
                continue
            f = lambda v: "n/a" if v is None else f"{v:+.2f}"
            n += bool(log_obs(con, now, t, so, "swiss_composite", "composite", f"{t}:{db.iso(now)}", "long" if c > 0 else "short",
                              f"composite {c:+.2f} (z media {f(zm)}, z SIX {f(zs)}, z IEX vol {f(zv)})", price_fn=price_fn))
    con.commit()
    return n


# ---------------- evaluation ----------------
_cache = {}


def yf_bars(sym, interval, start, end):
    key = (sym, interval, start.date(), end.date())
    if key not in _cache:
        import yfinance as yf
        h = yf.Ticker(sym).history(start=start.date(), end=(end + timedelta(days=1)).date(), interval=interval, prepost=False)
        if h is not None and not h.empty:
            h.index = h.index.tz_convert("UTC")
        _cache[key] = h
    return _cache[key]


def interval_for(t0, now):
    age = now - t0
    return "1m" if age < timedelta(days=6) else ("5m" if age < timedelta(days=55) else "1h")


def px(sym, ts, after, interval, bars=yf_bars, field="Close"):
    """First bar at/after ts (after=True) or last bar at/before ts -> (price, bar time). field='Open' for entries at the open."""
    import pandas as pd
    h = bars(sym, interval, ts - timedelta(days=4), ts + timedelta(days=4))
    if h is None or h.empty:
        return None, None
    t = pd.Timestamp(ts)
    sel = h[h.index >= t] if after else h[h.index <= t]
    if sel.empty:
        return None, None
    i = 0 if after else -1
    return float(sel[field].iloc[i]), sel.index[i].to_pydatetime()


def next_us_open(ts):
    t = ts.astimezone(ET)
    d = t.date()
    if t.hour * 60 + t.minute >= 570:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, datetime.strptime("09:30", "%H:%M").time(), ET).astimezone(timezone.utc)


def exit_time(t0, hz):
    import pandas as pd
    d = HZ[hz]
    if isinstance(d, timedelta):
        return t0 + d
    return (pd.Timestamp(t0) + pd.offsets.BDay(d)).to_pydatetime()


def net_signed(ret, direction, slip_bps=3):
    sign = {"long": 1, "short": -1}[direction]
    return sign * ret - 2 * slip_bps / 10000


def evaluate(con, so, now, bars=yf_bars):
    ec = so.get("evaluation", {})
    slip = ec.get("slippage_bps", 3)
    rng = random.Random(ec.get("random_seed", 7))
    k_rand = ec.get("random_entries_per_obs", 5)
    n = 0
    for o in con.execute("SELECT * FROM swiss_observations WHERE COALESCE(status,'valid')<>'invalid'").fetchall():
        # fill a pending 'next US open' entry
        if o["entry_ts"] is None:
            t_open = next_us_open(db.parse_ts(o["ts"]))
            if t_open + timedelta(minutes=2) > now:
                continue
            p, at = px(o["ticker"], t_open, True, interval_for(t_open, now), bars, field="Open")
            if p is None or at - t_open > timedelta(hours=7):
                continue   # holiday / no data yet
            con.execute("UPDATE swiss_observations SET entry_ts=?, entry_price=? WHERE id=?", (db.iso(at), p, o["id"]))
            o = con.execute("SELECT * FROM swiss_observations WHERE id=?", (o["id"],)).fetchone()
        t0 = db.parse_ts(o["entry_ts"])
        for hz in HZ:
            if con.execute("SELECT 1 FROM swiss_obs_returns WHERE obs_id=? AND horizon=?", (o["id"], hz)).fetchone():
                continue
            t1 = exit_time(t0, hz)
            if t1 > now:
                continue
            iv = interval_for(t0, now)
            e = o["entry_price"] or px(o["ticker"], t0, True, iv, bars)[0]
            x, _ = px(o["ticker"], t1, False, iv, bars)
            s0, _ = px("SPY", t0, True, iv, bars)
            s1, _ = px("SPY", t1, False, iv, bars)
            if not e or not x:
                continue
            r = x / e - 1
            sr = s1 / s0 - 1 if s0 and s1 else None
            ns = net_signed(r, o["direction"], slip) if o["direction"] in ("long", "short") else None
            xs = (net_signed(r - sr, o["direction"], slip) if sr is not None and ns is not None else None)
            con.execute("""INSERT OR REPLACE INTO swiss_obs_returns(obs_id, horizon, entry_price, exit_price, ret, net_signed,
                           spy_ret, excess_spy, computed_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (o["id"], hz, e, x, r, ns, sr, xs, db.iso(now)))
            n += 1
            if ns is None:
                continue
            day = t0.astimezone(ET).date()
            for k in range(k_rand):   # random-entry null arm: same ticker/direction/horizon, random minute that session
                lo = datetime.combine(day, datetime.strptime("09:31", "%H:%M").time(), ET)
                rt = (lo + timedelta(minutes=rng.randrange(0, 360 if hz != "30m" else 359 - 30))).astimezone(timezone.utc)
                rt1 = exit_time(rt, hz)
                if rt1 > now:
                    continue
                a, _ = px(o["ticker"], rt, True, iv, bars)
                b, _ = px(o["ticker"], rt1, False, iv, bars)
                if a and b:
                    con.execute("INSERT OR REPLACE INTO swiss_random(obs_id, k, horizon, ts, net_signed) VALUES (?,?,?,?,?)",
                                (o["id"], k, hz, db.iso(rt), net_signed(b / a - 1, o["direction"], slip)))
    con.commit()
    return n


def open_tests(con, now, daily=None):
    """'SIX move -> US open': last pre-open snapshot per (US date, ticker) vs the real opening gap and open->close."""
    import pandas as pd
    n = 0
    rows = con.execute("SELECT * FROM six_moves WHERE six_bar_ts IS NOT NULL AND us_open=0 ORDER BY ts").fetchall()
    last = {}
    for r in rows:
        ts = db.parse_ts(r["ts"])
        d = next_us_open(ts)
        if d - ts > timedelta(hours=8):
            continue    # only snapshots in the 8 h before a US open
        last[(str(d.astimezone(ET).date()), r["ticker"])] = r
    for (ud, t), r in last.items():
        if con.execute("SELECT 1 FROM six_open_tests WHERE us_date=? AND ticker=? AND us_close IS NOT NULL", (ud, t)).fetchone():
            continue
        h = daily(t) if daily else yf_bars(t, "1d", now - timedelta(days=30), now)
        if h is None or h.empty:
            continue
        idx = [str(x.astimezone(ET).date()) if hasattr(x, "astimezone") else str(x)[:10] for x in h.index]
        if ud not in idx:
            continue
        i = idx.index(ud)
        if i == 0:
            continue
        o, c, pc = float(h["Open"].iloc[i]), float(h["Close"].iloc[i]), float(h["Close"].iloc[i - 1])
        done = ud < str(now.astimezone(ET).date()) or now.astimezone(ET).hour >= 16
        con.execute("""INSERT OR REPLACE INTO six_open_tests(us_date, ticker, snap_ts, six_chg_pct, implied_gap_pct, us_prev_close,
                       us_open, open_gap_pct, us_close, open_to_close_pct, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (ud, t, r["ts"], r["six_chg_pct"], r["implied_gap_pct"], pc, o, (o / pc - 1) * 100,
                     c if done else None, (c / o - 1) * 100 if done else None, db.iso(now)))
        n += 1
    con.commit()
    return n


def summary(con, so):
    min_n = so.get("evaluation", {}).get("min_events", 100)
    out = []
    for r in con.execute("""SELECT o.rule, r.horizon, COUNT(r.net_signed) n, COUNT(DISTINCT substr(o.ts,1,10)) days,
                            AVG(r.net_signed) m, AVG(CASE WHEN r.net_signed>0 THEN 1.0 ELSE 0 END) hit, AVG(r.excess_spy) xs,
                            (SELECT AVG(b.net_signed) FROM swiss_random b JOIN swiss_observations o2 ON o2.id=b.obs_id
                               WHERE o2.rule=o.rule AND b.horizon=r.horizon AND COALESCE(o2.status,'valid')<>'invalid') rnd
                            FROM swiss_obs_returns r JOIN swiss_observations o ON o.id=r.obs_id
                            WHERE r.net_signed IS NOT NULL AND COALESCE(o.status,'valid')<>'invalid' GROUP BY o.rule, r.horizon ORDER BY o.rule, r.horizon"""):
        d = dict(r)
        vals = [x[0] for x in con.execute("""SELECT r.net_signed FROM swiss_obs_returns r JOIN swiss_observations o ON o.id=r.obs_id
                                             WHERE o.rule=? AND r.horizon=? AND r.net_signed IS NOT NULL
                                             AND COALESCE(o.status,'valid')<>'invalid'""", (d["rule"], d["horizon"]))]
        d["t"] = (st.mean(vals) / (st.stdev(vals) / math.sqrt(len(vals)))) if len(vals) > 2 and st.stdev(vals) > 0 else None
        d["ready"] = d["n"] >= min_n
        out.append(d)
    return out


def open_test_summary(con):
    rows = con.execute("SELECT * FROM six_open_tests WHERE implied_gap_pct IS NOT NULL AND open_gap_pct IS NOT NULL").fetchall()
    if len(rows) < 3:
        return {"n": len(rows)}
    x = [r["implied_gap_pct"] for r in rows]; y = [r["open_gap_pct"] for r in rows]
    mx, my = st.mean(x), st.mean(y)
    vx = sum((a - mx) ** 2 for a in x)
    beta = sum((a - mx) * (b - my) for a, b in zip(x, y)) / vx if vx else None
    corr = st.correlation(x, y) if st.pstdev(x) and st.pstdev(y) else None
    sign = st.mean([1.0 if a * b > 0 else 0.0 for a, b in zip(x, y) if abs(a) >= 0.3])  if any(abs(a) >= 0.3 for a in x) else None
    cont = [r for r in rows if r["open_to_close_pct"] is not None and abs(r["implied_gap_pct"]) >= 1.0]
    cont_m = st.mean([math.copysign(1, r["implied_gap_pct"]) * r["open_to_close_pct"] for r in cont]) if cont else None
    return {"n": len(rows), "beta": beta, "corr": corr, "sign_hit": sign, "n_big": len(cont), "open_to_close_signed": cont_m}


# ---------------- X (off by default) ----------------
def x_check(so):
    if not so.get("x_enabled", False):
        print("X for Swiss names is OFF (swiss_observe.x_enabled=false). No X calls."); return 3
    import subprocess
    n = int(so.get("x_max_calls_per_run", 2))
    r = subprocess.run([sys.executable, os.path.join(db.HERE, "budget.py"), "check", str(n)], capture_output=True, text=True)
    print(r.stdout.strip())
    if r.returncode != 0:
        print("over the shared 150/day budget -> skip Swiss X"); return 2
    q = " OR ".join(f'"{v.get("name", t)}"' for t, v in so.get("names", {}).items())
    print(f"agent may make <= {n} built-in x MCP calls, e.g. x.get_posts_counts_recent query: ({q}) lang:en -is:retweet ; "
          f"then: python3 budget.py record {n} \"swiss x\"")
    return 0


# ---------------- CLI ----------------
def fmt(v, p=2, sign=True):
    return "n/a" if v is None else (f"{v:+.{p}f}" if sign else f"{v:.{p}f}")


def print_six(rows):
    print(f"SIX move (yfinance, ~15-20 min delayed) @ {datetime.now(ZRH):%Y-%m-%d %H:%M} Zurich")
    for r in rows:
        bt = zrh(r["six_bar_ts"]).strftime("%H:%M") if r["six_bar_ts"] else "no trade today"
        print(f"  {r['ticker']:4} {r['six_symbol']:8} last {fmt(r['six_last'], 2, False)} CHF (bar {bt} Zurich, delay "
              f"{fmt(r['delay_min'], 0, False)} min) chg {fmt(r['six_chg_pct'])}% | implied US {fmt(r['implied_us'], 2, False)} "
              f"vs {r['ticker']} close {fmt(r['us_prev_close'], 2, False)} ({r['us_prev_date']}) -> gap {fmt(r['implied_gap_pct'])}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["collect", "six", "observe", "evaluate", "status", "x-check", "invalidate"])
    ap.add_argument("obs_id", nargs="?", type=int)
    ap.add_argument("--reason", default="")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--no-observe", action="store_true")
    a = ap.parse_args()
    so = cfg()
    if not so.get("enabled", True):
        print("swiss_observe disabled"); return 0
    con = db.connect()
    now = db.utcnow()
    if a.cmd == "collect":
        new_news = []
        backfill = con.execute("SELECT COUNT(*) FROM media WHERE feed LIKE 'swiss:%'").fetchone()[0] == 0
        if not a.no_news:
            import swiss_news
            new_news, status = swiss_news.fetch(con, now=now)
            swiss_news.save_status(status)
            bad = {k: (v.get("error") or v.get("http")) for k, v in status.items() if not v["ok"]}
            print(f"swiss-news: {len(status)} sources, {len(new_news)} new" + (" (first run: backlog, no news observations)" if backfill else "")
                  + (f"; skipped: {bad}" if bad else ""))
        rows = six_snapshot(con, so, now)
        print_six(rows)
        spikes = []
        if us_regular_open(now):
            try:
                spikes = iex_spikes(con, so, now)
            except Exception as ex:
                print(f"  [warn] swiss IEX spikes: {ex}")
        if not a.no_observe:
            k = observe(con, so, now, new_news, spikes, rows, backfill=backfill)
            print(f"swiss observations logged this tick: {k} (observe-only, no orders)")
    elif a.cmd == "six":
        print_six(six_snapshot(con, so, now))
    elif a.cmd == "observe":
        rows = [dict(r) for r in con.execute("""SELECT * FROM six_moves WHERE id IN (SELECT MAX(id) FROM six_moves GROUP BY ticker)""")]
        print(f"observations: {observe(con, so, now, (), (), rows)}")
    elif a.cmd == "evaluate":
        n = evaluate(con, so, now)
        k = open_tests(con, now)
        print(f"swiss evaluate: {n} new (obs, horizon) outcomes, {k} SIX->US-open rows; slippage "
              f"{so.get('evaluation', {}).get('slippage_bps', 3)} bps/side; horizons +30m/+1d/+3d")
        tot = con.execute("SELECT COUNT(*) FROM swiss_observations WHERE COALESCE(status,'valid')<>'invalid'").fetchone()[0]
        bad = con.execute("SELECT COUNT(*) FROM swiss_observations WHERE status='invalid'").fetchone()[0]
        print(f"  observations total: {tot} valid, {bad} invalid/excluded (need >= {so.get('evaluation', {}).get('min_events', 100)} per rule before reading edge)")
        for d in summary(con, so):
            print(f"  {d['rule']:17} {d['horizon']:>3}: n={d['n']:3d} days={d['days']:2d} net={d['m']*100:+.2f}% hit={d['hit']*100:3.0f}% "
                  f"random={fmt(d['rnd'] and d['rnd']*100)}% exSPY={fmt(d['xs'] and d['xs']*100)}% t={fmt(d['t'])}"
                  + ("" if d["ready"] else " [n<min: not readable yet]"))
        ot = open_test_summary(con)
        print(f"  SIX->US open: n={ot['n']}" + (f" beta={fmt(ot.get('beta'))} corr={fmt(ot.get('corr'))} sign_hit={fmt(ot.get('sign_hit'))} "
                                              f"|gap|>=1% n={ot.get('n_big')} open->close signed={fmt(ot.get('open_to_close_signed'))}%" if ot["n"] >= 3 else " (need >= 3 sessions)"))
    elif a.cmd == "status":
        rows = [dict(r) for r in con.execute("SELECT * FROM six_moves WHERE id IN (SELECT MAX(id) FROM six_moves GROUP BY ticker)")]
        print_six(rows)
        for o in con.execute("SELECT * FROM swiss_observations ORDER BY id DESC LIMIT 10"):
            inv = " [INVALID, excluded: " + (o["excluded_reason"] or "")[:60] + "]" if o["status"] == "invalid" else ""
            print(f"  OBS {zrh(o['ts']):%d %b %H:%M} Zurich {o['rule']} {o['ticker']} {o['direction']} {o['entry_basis']} "
                  f"{fmt(o['entry_price'], 2, False)} | {o['reason'][:90]}{inv}")
        import swiss_news
        if os.path.exists(swiss_news.STATUS):
            for k, v in json.load(open(swiss_news.STATUS)).items():
                print(f"  feed {k:16} {'ok' if v['ok'] else 'SKIPPED'} http={v.get('http')} items={v.get('items')} {v.get('error') or ''}")
    elif a.cmd == "x-check":
        return x_check(so)
    elif a.cmd == "invalidate":
        if not a.obs_id or not a.reason:
            print("usage: swiss.py invalidate ID --reason TEXT"); return 2
        r = invalidate(con, a.obs_id, a.reason, now)
        print(f"observation {a.obs_id}: status=invalid ({a.reason}); headline moved to media_rejected: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
