#!/usr/bin/env python3
"""Earnings calendar for the watchlist (zero X calls).

Usage: earnings.py fetch [--days 21] [--force]  |  earnings.py today  |  earnings.py show
       earnings.py events            compute/refresh earnings_events for reports in the last 5 days (surprise, gap, news)
       earnings.py set-actual TICKER EPS [--date YYYY-MM-DD] [--estimate X]   manual override (agent read the release)
* Primary: Nasdaq public calendar API (api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD), one request
  per weekday from 3 days back to --days ahead (~18 requests), at most once per box day unless --force.
  Gives time of day (before open / after hours) and consensus EPS.
* Fallback for tickers Nasdaq didn't list in the window: yfinance Ticker.calendar (date only).
* `events` (EPS surprise module): for each watchlist report in the last 5 ET days:
    - reaction session = next trading day after an after-hours report, the report day for a pre-market report
      (unknown time -> treated as after-hours); trading days from Alpaca /v2/calendar
    - EPS actual/estimate/surprise from yfinance get_earnings_dates (same EPS basis for both); a manual
      `set-actual` wins. Nasdaq's consensus can use a different EPS basis, so it is not mixed in.
    - gap = reaction-session open vs previous close (yfinance daily). Before the open, an INDICATIVE gap from
      the latest Alpaca IEX trade (pre-market) is stored and tagged in notes.
    - news_sent = tier-weighted media + Benzinga sentiment for the ticker since the release.
    - agree = 1 when sign(surprise) == sign(gap) == sign(news) with |surprise| >= 2%, |gap| >= 1%, |news| >= 0.05.
  paper_trader.py trades `earnings_drift` on reaction days 1-3 only when agree = 1 (gap must be the real open,
  not indicative), and pauses other sentiment trades for that ticker on reaction day 1.
* `today` prints watchlist tickers reporting today or the next US session (ET), used to tag signals and
  spikes as scheduled-event noise.
"""
import argparse, json, sys, time, urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import db

ET = ZoneInfo("America/New_York")


def nasdaq_day(d):
    req = urllib.request.Request(f"https://api.nasdaq.com/api/calendar/earnings?date={d:%Y-%m-%d}",
                                 headers={"User-Agent": "Mozilla/5.0 (stock-sentiment research)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return (json.loads(r.read().decode()).get("data") or {}).get("rows") or []


def trading_days(start, end):
    from alpaca_client import AlpacaPaper
    return [d["date"] for d in AlpacaPaper().calendar(str(start), str(end))]


def reaction_date(report_date, tod, tdays):
    rd = str(report_date)
    if tod and ("pre" in tod or "before" in tod):
        return rd if rd in tdays else next((d for d in tdays if d > rd), None)
    return next((d for d in tdays if d > rd), None)


def session_index(event_reaction_date, today, tdays):
    """1 on the reaction session, 2 and 3 on the next sessions; None otherwise."""
    if not event_reaction_date or event_reaction_date not in tdays or str(today) not in tdays:
        return None
    k = tdays.index(str(today)) - tdays.index(event_reaction_date) + 1
    return k if 1 <= k <= 3 else None


def sgn(x, th):
    return 0 if x is None or abs(x) < th else (1 if x > 0 else -1)


def compute_events(con, now):
    import media
    today = datetime.now(ET).date()
    tdays = trading_days(today - timedelta(days=12), today + timedelta(days=10))
    rows = con.execute("SELECT * FROM earnings WHERE date BETWEEN ? AND ?", (str(today - timedelta(days=5)), str(today))).fetchall()
    out = []
    for r in rows:
        t, rdate, tod = r["ticker"], r["date"], r["time_of_day"] or ""
        ev = con.execute("SELECT * FROM earnings_events WHERE ticker=? AND report_date=?", (t, rdate)).fetchone()
        rx = reaction_date(rdate, tod, tdays)
        notes = []
        actual = est = surprise = None
        src = "yfinance"
        if ev and ev["source"] == "manual":
            actual, est, surprise, src = ev["eps_actual"], ev["eps_estimate"], ev["surprise_pct"], "manual"
        else:
            try:
                import yfinance as yf
                d = yf.Ticker(t).get_earnings_dates(limit=8)
                for idx, row in d.iterrows():
                    if abs((idx.date() - date.fromisoformat(rdate)).days) <= 1:
                        est = None if row["EPS Estimate"] != row["EPS Estimate"] else float(row["EPS Estimate"])
                        actual = None if row["Reported EPS"] != row["Reported EPS"] else float(row["Reported EPS"])
                        if actual is not None and est:
                            surprise = (actual - est) / abs(est) * 100
                        break
            except Exception as e:
                notes.append(f"yfinance eps: {type(e).__name__}")
        if actual is None:
            notes.append("EPS actual not yet available (use set-actual)")
        # gap
        prev_close = open_px = gap = None
        if rx:
            try:
                import yfinance as yf
                h = yf.Ticker(t).history(start=str(date.fromisoformat(rx) - timedelta(days=7)),
                                         end=str(date.fromisoformat(rx) + timedelta(days=1)), interval="1d")
                h.index = [i.date() for i in h.index]
                before = [i for i in h.index if str(i) < rx]
                if before:
                    prev_close = float(h.loc[before[-1], "Close"])
                opened = str(today) > rx or (str(today) == rx and datetime.now(ET).strftime("%H:%M") >= "09:31")
                if date.fromisoformat(rx) in h.index and opened:
                    open_px = float(h.loc[date.fromisoformat(rx), "Open"])
            except Exception as e:
                notes.append(f"yfinance px: {type(e).__name__}")
            if open_px is None and prev_close and str(today) <= rx:
                try:
                    from alpaca_client import AlpacaPaper
                    lt = AlpacaPaper().latest_trade(t)
                    if lt.get("p") and lt.get("t", "") > db.iso(now - timedelta(hours=14)):
                        gap = (float(lt["p"]) / prev_close - 1) * 100
                        notes.append("gap INDICATIVE (latest IEX trade, pre-open)")
                except Exception:
                    pass
            elif open_px and prev_close:
                gap = (open_px / prev_close - 1) * 100
        # news since release
        rel = datetime.combine(date.fromisoformat(rdate), datetime.strptime("16:00" if "pre" not in tod else "06:00", "%H:%M").time(), ET)
        hours = max(1.0, (now - rel.astimezone(timezone.utc)).total_seconds() / 3600)
        snap = media.snapshot(con, [t], hours, now)[t] if now > rel.astimezone(timezone.utc) else {"wsent": None, "n": 0}
        ns = snap["wsent"]
        ss, gs, nsg = sgn(surprise, 2.0), sgn(gap, 1.0), sgn(ns, 0.05)
        agree = int(ss != 0 and ss == gs == nsg and open_px is not None)
        direction = {1: "long", -1: "short"}.get(ss)
        con.execute("""INSERT OR REPLACE INTO earnings_events(ticker, report_date, time_of_day, eps_actual, eps_estimate,
                       surprise_pct, reaction_date, prev_close, open_price, gap_pct, news_sent, n_news, direction, agree,
                       source, computed_at, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t, rdate, tod, actual, est, surprise, rx, prev_close, open_px, gap, ns, snap["n"], direction, agree,
                     src, db.iso(now), "; ".join(notes)))
        out.append((t, rdate, tod, rx, actual, est, surprise, gap, ns, agree, notes))
    con.commit()
    return out


def upcoming(con, days_ahead=1):
    today = datetime.now(ET).date()
    return con.execute("SELECT * FROM earnings WHERE date BETWEEN ? AND ? ORDER BY date",
                       (str(today), str(today + timedelta(days=days_ahead)))).fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fetch", "today", "show", "events", "set-actual"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--date")
    ap.add_argument("--estimate", type=float)
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    con = db.connect()
    tickers = db.watchlist()
    now = db.utcnow()
    if a.cmd == "events":
        f = lambda v, p=2: "n/a" if v is None else f"{v:+.{p}f}"
        for t, rdate, tod, rx, act, est, sur, gap, ns, agree, notes in compute_events(con, now):
            print(f"  {t:5} report {rdate} {tod or '?':11} reaction {rx} | EPS {act} vs {est} surprise {f(sur, 1)}% | "
                  f"gap {f(gap)}% | news {f(ns)} | agree={agree}" + (f" ({'; '.join(notes)})" if notes else ""))
        return 0
    if a.cmd == "set-actual":
        t, eps = a.args[0].upper(), float(a.args[1])
        rdate = a.date or con.execute("SELECT MAX(date) FROM earnings WHERE ticker=? AND date<=?",
                                      (t, str(datetime.now(ET).date()))).fetchone()[0]
        ev = con.execute("SELECT * FROM earnings_events WHERE ticker=? AND report_date=?", (t, rdate)).fetchone()
        est = a.estimate if a.estimate is not None else (ev["eps_estimate"] if ev else None)
        sur = (eps - est) / abs(est) * 100 if est else None
        con.execute("""INSERT INTO earnings_events(ticker, report_date, eps_actual, eps_estimate, surprise_pct, source, computed_at)
                       VALUES (?,?,?,?,?,?,?) ON CONFLICT(ticker, report_date) DO UPDATE SET eps_actual=excluded.eps_actual,
                       eps_estimate=excluded.eps_estimate, surprise_pct=excluded.surprise_pct, source='manual'""",
                    (t, rdate, eps, est, sur, "manual", db.iso(now)))
        con.commit()
        print(f"{t} {rdate}: EPS actual {eps} vs est {est} -> surprise {sur}% (manual); run `earnings.py events` next")
        return 0
    if a.cmd == "fetch":
        last = con.execute("SELECT MAX(fetched_at) FROM earnings").fetchone()[0]
        if last and not a.force and db.local(last)[:10] == db.local(now)[:10]:
            print(f"earnings: already fetched today ({db.local(last)}); use --force"); return 0
        today = datetime.now(ET).date()
        found = set()
        d = today - timedelta(days=3)
        while d <= today + timedelta(days=a.days):
            if d.weekday() < 5:
                try:
                    for r in nasdaq_day(d):
                        if r.get("symbol") in tickers:
                            eps = (r.get("epsForecast") or "").replace("$", "").replace("(", "-").replace(")", "")
                            con.execute("""INSERT OR REPLACE INTO earnings(ticker, date, time_of_day, eps_estimate, source,
                                           fetched_at) VALUES (?,?,?,?,?,?)""",
                                        (r["symbol"], str(d), (r.get("time") or "").replace("time-", ""),
                                         float(eps) if eps.replace(".", "").replace("-", "").isdigit() else None,
                                         "nasdaq", db.iso(now)))
                            found.add(r["symbol"])
                except Exception as e:
                    print(f"  [warn] nasdaq {d}: {e}")
                time.sleep(0.4)
            d += timedelta(days=1)
        missing = [t for t in tickers if t not in found]
        if missing:
            import yfinance as yf
            for t in missing:
                try:
                    for ed in (yf.Ticker(t).calendar or {}).get("Earnings Date", [])[:1]:
                        con.execute("""INSERT OR IGNORE INTO earnings(ticker, date, time_of_day, eps_estimate, source, fetched_at)
                                       VALUES (?,?,?,?,?,?)""", (t, str(ed), None, None, "yfinance", db.iso(now)))
                except Exception as e:
                    print(f"  [warn] yfinance {t}: {e}")
        con.commit()
        print(f"earnings: nasdaq found {sorted(found)}; yfinance fallback for {missing}")
    rows = upcoming(con, 1) if a.cmd == "today" else con.execute(
        "SELECT * FROM earnings WHERE date >= ? ORDER BY date", (str(datetime.now(ET).date() - timedelta(days=3)),)).fetchall()
    for r in rows:
        print(f"  {r['date']} {r['ticker']:5} {r['time_of_day'] or '?':12} EPS est {r['eps_estimate']} ({r['source']})")
    if a.cmd == "today" and not rows:
        print("  no watchlist earnings today/next session")
    return 0


if __name__ == "__main__":
    sys.exit(main())
