#!/usr/bin/env python3
"""Render dashboard/index.html (single self-contained file; Chart.js from CDN) and dashboard/latest.png.

Usage: dashboard.py [--no-png] [--no-alpaca]
Times are shown in Europe/Zurich (labelled). Run at the end of every routine run. Zero X calls.
Sections: Alpaca paper equity & P/L, positions, recent orders, X budget today vs 150, per-ticker charts
(X posts/hour, weighted sentiment per run, IEX price), volume spikes, signals + forward returns,
paper_trades log, missed calls (signals skipped by a filter), news headlines, Reddit mentions, upcoming earnings, and the 'Switzerland (observe-only)'
panel (SIX move vs US line, Swiss headlines, observe-only observations; never traded).
"""
import argparse, html, json, os, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import db

ZRH = ZoneInfo("Europe/Zurich")
OUT = os.path.join(db.HERE, "dashboard")
CAP = 150


def zt(s, fmt="%d %b %H:%M"):
    if not s:
        return ""
    dt = db.parse_ts(s) if isinstance(s, str) else s
    return dt.astimezone(ZRH).strftime(fmt)


def e(x):
    return html.escape("" if x is None else str(x))


def fnum(v, p=2, pct=False, sign=False):
    if v is None or v == "":
        return "–"
    v = float(v)
    s = f"{v:+.{p}f}" if sign else f"{v:,.{p}f}"
    return s + ("%" if pct else "")


def cls(v):
    return "" if v in (None, "") else ("pos" if float(v) > 0 else "neg" if float(v) < 0 else "")


def table(headers, rows, empty="nothing yet"):
    if not rows:
        return f'<div class="empty">{e(empty)}</div>'
    h = "".join(f"<th>{e(x)}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tw"><table style="--cols:{len(headers)}"><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>'


def alpaca_section(no_alpaca):
    if no_alpaca:
        return None
    try:
        from alpaca_client import AlpacaPaper
        c = AlpacaPaper()
        return {"account": c.account(), "positions": c.positions(), "orders": c.orders("all", limit=15),
                "history": c.portfolio_history("1M", "1D"), "clock": c.clock(), "client": c}
    except Exception as ex:
        print(f"  [warn] Alpaca unavailable: {ex}")
        return None


def price_series(alp, tickers):
    out = {t: [] for t in tickers}
    if not alp:
        return out
    try:
        bars = alp["client"].bars(tickers, "1Hour", start=db.utcnow() - timedelta(days=8))
        for t in tickers:
            out[t] = [[int(db.parse_ts(b["t"]).timestamp() * 1000), b["c"]] for b in bars.get(t, [])]
    except Exception as ex:
        print(f"  [warn] bars failed: {ex}")
    return out


def swiss_panel(con, now):
    """'Switzerland (observe-only)': SIX move vs US line, Swiss headlines, observe-only observations + outcomes."""
    try:
        so = db.load_config().get("swiss_observe") or {}
        if not so.get("enabled"):
            return ""
        import swiss, swiss_news
        names = so.get("names", {})
        six = {r["ticker"]: r for r in con.execute("SELECT * FROM six_moves WHERE id IN (SELECT MAX(id) FROM six_moves GROUP BY ticker)")}
        pc = lambda v: f'<span class="{cls(v)}">{fnum(v, 2, True, True)}</span>' if v is not None else "–"
        six_rows = []
        for t, v in names.items():
            r = six.get(t)
            if not v.get("six"):
                six_rows.append([e(t), "–", "no SIX line (NYSE primary)", "–", "–", "–", "–", "–"])
            elif r:
                six_rows.append([e(t), e(r["six_symbol"]), f'{fnum(r["six_last"])} CHF',
                                 (zt(r["six_bar_ts"], "%H:%M") + f' <span class="mut">(~{r["delay_min"]:.0f} min delay)</span>') if r["six_bar_ts"] else "no trades today",
                                 pc(r["six_chg_pct"]), f'{fnum(r["implied_us"])} $', f'{fnum(r["us_prev_close"])} $ <span class="mut">({e(r["us_prev_date"])})</span>',
                                 f'<b>{pc(r["implied_gap_pct"])}</b>'])
        hl_rows = [[zt(r["created_at"]), e(r["ticker"]), e((r["feed"] or "")[6:]), e(r["outlet"]),
                    f'<a href="{e(r["url"])}" target="_blank" rel="noopener">{e(r["title"][:150])}</a>',
                    fnum(r["novelty"], 2), f'<span class="{cls(r["s"])}">{fnum(r["s"], 2, sign=True)}</span>']
                   for r in con.execute("""SELECT *, COALESCE(agent_sentiment, sentiment) s FROM media WHERE feed LIKE 'swiss:%'
                                           AND created_at>=? ORDER BY created_at DESC LIMIT 18""", (db.iso(now - timedelta(hours=36)),))]
        hl_rows += [[zt(r["created_at"]), e(r["ticker"]), "benzinga", e(r["source"]),
                     f'<a href="{e(r["url"])}" target="_blank" rel="noopener">{e((r["headline"] or "")[:150])}</a>', "–",
                     fnum(r["sentiment"], 2, sign=True) if r["sentiment"] is not None else "–"]
                    for r in con.execute("SELECT * FROM news WHERE ticker IN (%s) AND created_at>=? ORDER BY created_at DESC LIMIT 6"
                                         % ",".join("?" * len(names)), (*names, db.iso(now - timedelta(hours=36))))]
        hl_rows.sort(key=lambda x: x[0], reverse=True)
        rets = {}
        for r in con.execute("SELECT * FROM swiss_obs_returns"):
            rets.setdefault(r["obs_id"], {})[r["horizon"]] = r["net_signed"]
        obs_rows = []
        for o in con.execute("SELECT * FROM swiss_observations ORDER BY id DESC LIMIT 15"):
            rr = rets.get(o["id"], {})
            inv = o["status"] == "invalid"
            obs_rows.append([zt(o["ts"]), e(o["ticker"]), e(o["rule"]) + (' <span class="neg">[invalid]</span>' if inv else ""),
                             e(o["direction"]), e(o["window"]), e(o["entry_basis"]),
                             fnum(o["entry_price"]), (f'<s>{e(o["reason"][:110])}</s> <span class="mut">({e(o["excluded_reason"] or "")[:60]})</span>'
                                                      if inv else e(o["reason"][:110]))] +
                            [pc(rr[h] * 100) if rr.get(h) is not None else "–" for h in ("30m", "1d", "3d")])
        ev_rows = [[e(d["rule"]), e(d["horizon"]), e(d["n"]), pc(d["m"] * 100), fnum(d["hit"] * 100, 0, True),
                    pc(d["rnd"] * 100) if d["rnd"] is not None else "–", pc(d["xs"] * 100) if d["xs"] is not None else "–",
                    fnum(d["t"], 2, sign=True) if d["t"] is not None else "–", "yes" if d["ready"] else "no (n<100)"]
                   for d in swiss.summary(con, so)]
        fs = json.load(open(swiss_news.STATUS)) if os.path.exists(swiss_news.STATUS) else {}
        feeds = " · ".join(f'{e(k)} <span class="{"pos" if v.get("ok") else "neg"}">{"ok" if v.get("ok") else "blocked/skipped"}</span>'
                           for k, v in fs.items())
        nobs = con.execute("SELECT COUNT(*) FROM swiss_observations WHERE COALESCE(status,'valid')<>'invalid'").fetchone()[0]
        z = now.astimezone(ZRH).strftime("%H:%M")
        return f"""<section class="card" style="border-color:#d29922"><h2>Switzerland (observe-only) <span class="pill" style="background:#d2992233;color:#d29922">OBSERVE ONLY · no orders</span></h2>
<div class="mut" style="margin-bottom:8px">NVS↔NOVN.SW · UBS↔UBSG.SW · LOGI↔LOGN.SW · ALC↔ALC.SW · ONON (NYSE only). Times Europe/Zurich (now {z} Zurich).
SIX 09:00–17:30; ad hoc news before 07:30 or after 17:40; US lines 15:30–22:00. SIX prices via yfinance, delayed ~15–20 min.
Implied gap = SIX price × CHFUSD ÷ US line's previous close − 1. paper_trader.py refuses these symbols.</div>
<div class="grid2"><div>{table(["US","SIX","SIX last","Bar (Zurich)","SIX chg","Implied US","Prev US close","Implied gap"], six_rows, "no SIX data yet")}
<h2 style="margin-top:14px">Hypothetical observations ({nobs} total; 100+ per rule needed)</h2>
{table(["Time (Zurich)","US","Rule","Dir","Window","Entry","Price","Reason","+30m","+1d","+3d"], obs_rows, "no observations yet")}
{table(["Rule","Horizon","n","Net","Hit","Random","Excess SPY","t","Readable"], ev_rows, "no evaluated outcomes yet (3 bps/side)")}</div>
<div>{table(["Time (Zurich)","US","Feed","Outlet","Headline","Novelty","Sent."], hl_rows[:20], "no Swiss headlines (36h)")}
<div class="mut" style="margin-top:8px">Feeds: {feeds or "–"}</div></div></div></section>"""
    except Exception as ex:
        print(f"  [warn] swiss panel: {ex}")
        return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--no-alpaca", action="store_true")
    a = ap.parse_args()
    con = db.connect()
    now = db.utcnow()
    core = db.watchlist(core_only=True)
    allw = db.watchlist()
    alp = alpaca_section(a.no_alpaca)

    # ---- KPIs ----
    import budget
    led = json.load(open(budget.LEDGER)) if os.path.exists(budget.LEDGER) else {}
    used = led.get(budget.today(), {}).get("used", 0)
    if alp:
        acct = alp["account"]
        eq, last_eq = float(acct["equity"]), float(acct.get("last_equity") or acct["equity"])
        day_pl = eq - last_eq
        tot_pl = eq - 100_000
        hist = alp["history"]
        eq_series = [[ts * 1000, v] for ts, v in zip(hist.get("timestamp", []), hist.get("equity", [])) if v]
        market = "OPEN" if alp["clock"].get("is_open") else "closed"
    else:
        eq = day_pl = tot_pl = None; eq_series = []; market = "?"

    # ---- per ticker series ----
    since = db.iso(now - timedelta(days=7))
    px = price_series(alp, core)
    charts = {}
    for t in core:
        counts = [[int(db.parse_ts(r[0]).timestamp() * 1000), r[1] * 3600 / max(60, (db.parse_ts(r[2]) - db.parse_ts(r[0])).total_seconds())]
                  for r in con.execute("SELECT window_start, post_count, window_end FROM counts WHERE ticker=? AND window_start>=? ORDER BY window_start", (t, since))]
        sent = [[int(db.parse_ts(r[0]).timestamp() * 1000), round(r[1], 3)]
                for r in con.execute("SELECT ts, w_sentiment FROM runs WHERE ticker=? AND ts>=? AND id IN (SELECT MAX(id) FROM runs GROUP BY ticker, batch) ORDER BY ts", (t, since))]
        sigs = [[int(db.parse_ts(r[0]).timestamp() * 1000), r[1]] for r in con.execute(
            "SELECT ts, direction FROM signals WHERE ticker=? AND variant='follow' AND ts>=?", (t, since))]
        comp_rows = con.execute("SELECT * FROM composite WHERE ticker=? AND ts>=? ORDER BY ts", (t, since)).fetchall()
        comp = {k: [[int(db.parse_ts(r["ts"]).timestamp() * 1000), round(r[k], 3)] for r in comp_rows if r[k] is not None]
                for k in ("composite", "z_x_sent", "z_x_vol", "z_reddit_chg", "z_media_sent", "z_media_cnt_chg", "z_price_vol")}
        last = con.execute("SELECT * FROM runs WHERE ticker=? ORDER BY id DESC LIMIT 1", (t,)).fetchone()
        charts[t] = {"counts": counts, "sent": sent, "price": px.get(t, []), "signals": sigs,
                     "last": dict(last) if last else None, "comp": comp}

    # ---- tables ----
    pos_rows = [[e(p["symbol"]), e(p["side"]), e(p["qty"]), fnum(p["avg_entry_price"]), fnum(p.get("current_price")),
                 fnum(p["market_value"], 0), f'<span class="{cls(p["unrealized_pl"])}">{fnum(p["unrealized_pl"], 2, sign=True)} '
                 f'({fnum(float(p["unrealized_plpc"])*100, 2, True, True)})</span>'] for p in (alp["positions"] if alp else [])]
    ord_rows = [[zt(o.get("submitted_at") or o.get("created_at")), e(o["symbol"]), e(o["side"]), e(o["qty"]), e(o.get("type")),
                 fnum(o.get("limit_price")), e(o["status"]), e(o.get("filled_qty")), fnum(o.get("filled_avg_price"))]
                for o in (alp["orders"] if alp else [])]
    spikes = [[zt(r["ts"]), e(r["ticker"]), e(r["timeframe"]), f'<b>x{r["ratio"]:.1f}</b>',
               f'<span class="{cls(r["ret_pct"])}">{fnum(r["ret_pct"], 2, True, True)}</span>', fnum(r["price"]),
               "yes" if r["flagged"] else "ratio only", e(r["notes"])]
              for r in con.execute("SELECT * FROM volume_spikes ORDER BY ts DESC LIMIT 25")]
    ret = {}
    for r in con.execute("SELECT * FROM signal_returns"):
        ret.setdefault(r["signal_id"], {})[r["horizon"]] = r["excess_signed"]
    sig_rows = []
    for r in con.execute("SELECT * FROM signals ORDER BY ts DESC LIMIT 30"):
        rr = ret.get(r["id"], {})
        sig_rows.append([zt(r["ts"]), e(r["ticker"]), e(r["type"]) + (" (hyp.)" if r["type"] == "composite" else ""), e(r["variant"]), e(r["direction"]), fnum(r["zscore"], 2, sign=True),
                         fnum(r["volume_ratio"]), fnum(r["price"])] +
                        [f'<span class="{cls(rr.get(h))}">{fnum(rr[h]*100, 2, True, True) if h in rr else "–"}</span>' for h in ("15m", "1h", "1d", "5d")])
    pt_rows = [[zt(r["ts"]), e(r["ticker"]), e(r["purpose"]), e(r["side"]), fnum(r["qty"], 0), fnum(r["limit_price"]),
                e(r["status"]), fnum(r["filled_avg_price"]), e(r["notes"])]
               for r in con.execute("SELECT * FROM paper_trades ORDER BY id DESC LIMIT 20")]
    news_rows = [[zt(r["created_at"]), e(r["ticker"]), f'<a href="{e(r["url"])}" target="_blank" rel="noopener">{e(r["headline"])}</a>',
                  fnum(r["sentiment"], 1, sign=True) if r["sentiment"] is not None else "–"]
                 for r in con.execute("SELECT * FROM news WHERE created_at>=? ORDER BY created_at DESC LIMIT 25",
                                      (db.iso(now - timedelta(hours=48)),))]
    rc = con.execute("SELECT MAX(ts) FROM reddit_counts WHERE source='apewisdom'").fetchone()[0]
    red_rows = []
    if rc:
        for r in con.execute("SELECT * FROM reddit_counts WHERE ts=? AND source='apewisdom' ORDER BY mentions DESC", (rc,)):
            d = None if r["mentions_24h"] in (None, 0) else (r["mentions"] / r["mentions_24h"] - 1) * 100
            nn = con.execute("SELECT COUNT(*) FROM news WHERE ticker=? AND created_at>=?", (r["ticker"], db.iso(now - timedelta(hours=24)))).fetchone()[0]
            red_rows.append([e(r["ticker"]), e(r["mentions"]), e(r["mentions_24h"]),
                             f'<span class="{cls(d)}">{fnum(d, 0, True, True)}</span>', e(r["upvotes"]), e(r["rank"]), e(nn)])
    earn_rows = [[e(r["date"]), e(r["ticker"]), e(r["time_of_day"] or "?"), fnum(r["eps_estimate"]), e(r["source"])]
                 for r in con.execute("SELECT * FROM earnings WHERE date>=? ORDER BY date LIMIT 12",
                                      (str((now - timedelta(days=1)).date()),))]
    run_rows = []
    for t in core:
        l = charts[t]["last"]
        if l:
            run_rows.append([e(t), zt(l["ts"]), e(l["n_posts"]), fnum(l["w_sentiment"], 3, sign=True), fnum(l["sent_z"], 2, sign=True),
                             fnum(l["volume_ratio"]), fnum(l["price"]), e(l["status"])])

    import media as _media
    COMPN = [("z_x_sent", "X sentiment"), ("z_x_vol", "X volume"), ("z_reddit_chg", "Reddit Δ"),
             ("z_media_sent", "Media sentiment"), ("z_media_cnt_chg", "Media count Δ"), ("z_price_vol", "Price volume")]
    drive_rows = []
    for t in core:
        r = con.execute("SELECT * FROM composite WHERE ticker=? ORDER BY id DESC LIMIT 1", (t,)).fetchone()
        if not r:
            continue
        zs = [(lab, r[k]) for k, lab in COMPN if r[k] is not None]
        top = ", ".join(f"{lab} {v:+.1f}" for lab, v in sorted(zs, key=lambda x: -abs(x[1]))[:3])
        drive_rows.append([e(t), zt(r["ts"]), f'<b class="{cls(r["composite"])}">{fnum(r["composite"], 2, sign=True)}</b>'] +
                          [f'<span class="{cls(r[k])}">{fnum(r[k], 1, sign=True)}</span>' for k, _ in COMPN] +
                          [e(top), e(r["zmode"]), e(r["notes"])])
    msnap = _media.snapshot(con, core, 24, now)
    msnap_rows = [[e(t), e(v["n"]), e(v["n_tier1"]), f'<span class="{cls(v["wsent"])}">{fnum(v["wsent"], 2, sign=True)}</span>']
                  for t, v in msnap.items()]
    media_rows = [[zt(r["created_at"]), e(r["ticker"]), e(r["outlet"]), f'T{r["tier"]}',
                   f'<a href="{e(r["url"])}" target="_blank" rel="noopener">{e(r["title"])}</a>',
                   f'<span class="{cls(r["s"])}">{fnum(r["s"], 2, sign=True)}</span>' + (" ✎" if r["agent_sentiment"] is not None else "")]
                  for r in con.execute("""SELECT *, COALESCE(agent_sentiment, sentiment) s FROM media WHERE created_at>=? AND ticker IN (%s)
                                          ORDER BY tier, created_at DESC LIMIT 30""" % ",".join("?" * len(core)),
                                       (db.iso(now - timedelta(hours=24)), *core))]
    import evaluate as _ev
    eval_rows = []
    try:
        for r in _ev.decision_summary(con):
            pc = lambda v: f'<span class="{cls(v)}">{fnum(v*100, 2, True, True)}</span>' if v is not None else "–"
            eval_rows.append([e(r["rule"]), e(r["action"]), e(r["horizon"]), e(r["n"]), e(r["days"]), pc(r["m"]),
                              fnum(r["hit"]*100, 0, True), pc(r["rnd"]), pc(r["xs"]), pc(r["xq"])])
    except Exception as ex:
        print(f"  [warn] eval summary: {ex}")
    bh = _ev.buy_and_hold(con, now) if alp else {}
    bh_txt = " · ".join(f"{k} {v*100:+.2f}%" for k, v in bh.items() if v is not None) or "available after the first session"
    dec_rows = [[zt(r["ts"]), e(r["ticker"]), e(r["rule"]), e(r["trigger_type"]), e(r["direction"]),
                 f'<b>{e(r["action"])}</b>' if r["action"] == "trade" else e(r["action"]), e(r["reason"]), e(r["news_ref"])]
                for r in con.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 25")]
    missed_rows, missed_counts = [], ""
    try:
        missed_rows = [[zt(r["ts"]), e(r["ticker"]), e(r["direction"]), fnum(r["signal_price"]), fnum(r["live_price"]),
                        f'<span class="pill">{e(r["filter"])}</span>', e((r["reason"] or "")[:140])]
                       for r in con.execute("SELECT * FROM missed_calls ORDER BY ts DESC, id DESC LIMIT 20")]
        missed_counts = " · ".join(f"{e(r[0])} {r[1]}" for r in con.execute(
            "SELECT filter, COUNT(*) FROM missed_calls WHERE ts>=? GROUP BY filter ORDER BY 2 DESC", (since,)))
    except Exception as ex:
        print(f"  [warn] missed calls: {ex}")
    ev_rows = [[e(r["ticker"]), e(r["report_date"]), e(r["time_of_day"]), e(r["reaction_date"]),
                f'{fnum(r["eps_actual"])} vs {fnum(r["eps_estimate"])}', f'<span class="{cls(r["surprise_pct"])}">{fnum(r["surprise_pct"], 1, True, True)}</span>',
                f'<span class="{cls(r["gap_pct"])}">{fnum(r["gap_pct"], 2, True, True)}</span>', fnum(r["news_sent"], 2, sign=True),
                "<b>yes</b>" if r["agree"] else "no", e(r["source"]), e(r["notes"])]
               for r in con.execute("SELECT * FROM earnings_events ORDER BY report_date DESC, ticker LIMIT 12")]
    sec_rows = [[zt(r["filed_at"]), e(r["ticker"]), e(r["form"]),
                 e(r["items"]) if r["form"].startswith("8-K") else e(f"{r['insider']}: buy ${r['buy_value'] or 0:,.0f} / sell ${r['sell_value'] or 0:,.0f}"),
                 f'<a href="{e(r["url"])}" target="_blank" rel="noopener">link</a>']
                for r in con.execute("SELECT * FROM sec_filings ORDER BY filed_at DESC LIMIT 15")]
    kpi = lambda label, val, sub="", c="": f'<div class="kpi"><div class="kl">{e(label)}</div><div class="kv {c}">{val}</div><div class="ks">{sub}</div></div>'
    bpct = min(100, used / CAP * 100)
    bcol = "#3fb950" if used < 100 else "#d29922" if used < 140 else "#f85149"
    kpis = "".join([
        kpi("Paper equity", f"${eq:,.2f}" if eq is not None else "–", f"market {market}"),
        kpi("Day P/L", fnum(day_pl, 2, sign=True) if day_pl is not None else "–", "vs last close", cls(day_pl)),
        kpi("Total P/L", fnum(tot_pl, 2, sign=True) if tot_pl is not None else "–", "vs $100k start", cls(tot_pl)),
        f'<div class="kpi"><div class="kl">X budget today (UTC+8 day)</div><div class="kv">{used} <span class="ks">/ {CAP}</span></div>'
        f'<div class="bar"><div style="width:{bpct:.1f}%;background:{bcol}"></div></div></div>',
        kpi("Signals (7d)", e(con.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND variant='follow'", (since,)).fetchone()[0]),
            "sentiment_shock"),
        kpi("Volume spikes (7d)", e(con.execute("SELECT COUNT(*) FROM volume_spikes WHERE ts>=? AND flagged=1", (since,)).fetchone()[0]),
            "flagged, IEX"),
    ])
    chart_divs = "".join(f'<div class="card"><h3>{e(t)}</h3><div class="ch"><canvas id="c_{e(t)}"></canvas></div></div>' for t in core)
    comp_divs = "".join(f'<div class="card"><h3>{e(t)} composite and components (z)</h3><div class="ch"><canvas id="k_{e(t)}"></canvas></div></div>' for t in core)
    data = {"charts": charts, "equity": eq_series}
    gen = now.astimezone(ZRH).strftime("%a %d %b %Y %H:%M")
    swiss_html = swiss_panel(con, now)

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>X-sentiment paper desk</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--mut:#8b949e;--pos:#3fb950;--neg:#f85149;--acc:#58a6ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}}
header{{padding:18px 26px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px}}
h1{{font-size:19px;margin:0;font-weight:600}}h2{{font-size:15px;margin:0 0 10px;font-weight:600;color:var(--fg)}}h3{{margin:0 0 8px;font-size:14px;color:var(--acc)}}
.mut{{color:var(--mut);font-size:12px}}main{{padding:18px 26px;display:grid;gap:16px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}}
.kpi,.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.kl{{color:var(--mut);font-size:12px}}.kv{{font-size:22px;font-weight:600;margin-top:2px}}.ks{{color:var(--mut);font-size:12px}}
.bar{{height:8px;background:#21262d;border-radius:4px;margin-top:8px;overflow:hidden}}.bar div{{height:100%}}
.grid3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:16px}}.grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(520px,1fr));gap:16px}}
.ch{{height:250px}}.tw{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th{{text-align:left;color:var(--mut);font-weight:500;border-bottom:1px solid var(--line);padding:6px 8px;white-space:nowrap}}
td{{border-bottom:1px solid #21262d;padding:6px 8px;vertical-align:top}}tr:hover td{{background:#1c2128}}
.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}.empty{{color:var(--mut);font-style:italic;padding:6px 0}}a{{color:var(--fg);text-decoration:none}}a:hover{{color:var(--acc)}}
footer{{padding:16px 26px;border-top:1px solid var(--line);color:var(--mut);font-size:13px;text-align:center}}a.credit{{color:var(--acc)}}a.credit:hover{{text-decoration:underline}}
.pill{{display:inline-block;padding:1px 8px;border-radius:10px;background:#1f6feb33;color:var(--acc);font-size:12px}}
/* responsive: fluid grids never exceed the viewport; grid children may shrink so wide tables scroll inside .tw */
html{{-webkit-text-size-adjust:100%;text-size-adjust:100%}}
.grid3{{grid-template-columns:repeat(auto-fit,minmax(min(380px,100%),1fr))}}.grid2{{grid-template-columns:repeat(auto-fit,minmax(min(520px,100%),1fr))}}
main>*,.grid2>*,.grid3>*,.kpis>*,header>*{{min-width:0}}.tw{{max-width:100%;-webkit-overflow-scrolling:touch}}
img,video{{max-width:100%;height:auto}}canvas{{max-width:100%}}h1,h2,h3{{overflow-wrap:anywhere}}
@media (max-width:700px){{
body{{font-size:15px}}header{{padding:14px 14px;display:block}}header>div+div{{margin-top:6px}}main{{padding:12px;gap:12px}}
footer{{padding:16px 14px;font-size:14px;line-height:2}}h1{{font-size:18px}}h2{{font-size:16px}}h3{{font-size:15px}}
.mut,.ks,.kl{{font-size:13px}}.kv{{font-size:20px}}.kpis{{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}
.grid2,.grid3{{grid-template-columns:minmax(0,1fr);gap:12px}}.kpi,.card{{padding:12px}}.ch{{height:230px}}
table{{font-size:13px}}.tw table{{min-width:calc(var(--cols,4) * 80px)}}th,td{{padding:7px 8px}}td a{{display:inline-block;padding:4px 0}}
a.credit{{padding:8px 2px}}
}}
@media (max-width:360px){{.kpis{{grid-template-columns:minmax(0,1fr)}}}}
</style></head><body>
<header><div><h1>X-sentiment paper desk <span class="pill">PAPER ONLY</span></h1>
<div class="mut">TSLA · NVDA · PLTR core, IEX volume watchlist: {e(", ".join(allw))}</div></div>
<div class="mut">Built with ❤️ by Francesco Andreoli (<a class="credit" href="https://x.com/francescoswiss" target="_blank" rel="noopener">@francescoswiss</a>)</div>
<div class="mut">Generated {gen} (Europe/Zurich). All times on this page are Zurich time.</div></header>
<main>
<section class="kpis">{kpis}</section>
<section class="grid3">{chart_divs}</section>
<section class="card"><h2>What's driving it: holistic composite (latest)</h2>{table(["Ticker","Time","Composite","X sent.","X vol","Reddit Δ","Media sent.","Media cnt Δ","Price vol","Top drivers","z mode","Notes"], drive_rows, "no composite yet")}</section>
<section class="grid3">{comp_divs}</section>
<section class="card"><h2>Latest X run per ticker</h2>{table(["Ticker","Run (Zurich)","Posts","Weighted sent.","Sent. z","Vol ratio","Price","Status"], run_rows)}</section>
{swiss_html}
<section class="grid2">
<div class="card"><h2>Paper equity (1M, daily)</h2><div class="ch"><canvas id="c_eq"></canvas></div></div>
<div class="card"><h2>Open positions</h2>{table(["Symbol","Side","Qty","Avg entry","Last","Mkt value","Unrealized P/L"], pos_rows, "flat: no open positions")}
<h2 style="margin-top:16px">Recent Alpaca orders</h2>{table(["Submitted","Sym","Side","Qty","Type","Limit","Status","Filled","Avg fill"], ord_rows, "no orders yet")}</div>
</section>
<section class="card"><h2>Rule evaluation (net of 3 bps/side slippage) vs random entry and SPY/QQQ</h2>
<div class="mut" style="margin-bottom:8px">Buy-and-hold since paper start: {e(bh_txt)}. Rules: v2_news_momentum trades only with fresh novel news; v2_attention_fade = paper-only fade log; v1_follow_spike = retired rule (log); earnings_drift = days 1-3 when surprise, gap and news agree.</div>
{table(["Rule","Action","Horizon","n","Days","Net mean","Hit","Random entry","Excess SPY","Excess QQQ"], eval_rows, "no evaluated decisions yet")}</section>
<section class="card"><h2>Trading decisions (latest)</h2>{table(["Time","Ticker","Rule","Trigger","Dir","Action","Reason","News ref"], dec_rows, "no decisions yet")}</section>
<section class="card"><h2>Missed calls (signals skipped by a filter)</h2>
<div class="mut" style="margin-bottom:8px">Every US signal blocked by the news gate, threshold, debounce, earnings pause, max chase (live price &gt; 1 ATR past the signal), loss-streak breaker (3 stop-losses in a session → top-conviction only) and similar. Last 7 days: {missed_counts or "none"}.</div>
{table(["Time","Ticker","Dir","Signal price","Live","Filter","Reason"], missed_rows, "no missed calls logged yet")}</section>
<section class="grid2">
<div class="card"><h2>Earnings events (surprise, gap, news agreement)</h2>{table(["Ticker","Report (ET)","When","Reaction","EPS act vs est","Surprise","Gap","News","Agree","Src","Notes"], ev_rows, "no recent reports")}</div>
<div class="card"><h2>SEC filings (8-K items, Form 4)</h2>{table(["Filed","Ticker","Form","Items / insider","Doc"], sec_rows, "none: SEC currently blocks this box (HTTP 403); Benzinga covers filings")}</div>
</section>
<section class="card"><h2>Signals (sentiment_shock; composite = hypothetical) and forward excess return vs SPY, signed</h2>{table(["Time","Ticker","Type","Variant","Dir","z","Vol ratio","Price","15m","1h","1d","5d"], sig_rows, "no signals yet (each ticker needs 5 warm-up runs)")}</section>
<section class="grid2">
<div class="card"><h2>Volume spikes (IEX, vs same time of day over prior 10 sessions)</h2>{table(["Bucket start","Ticker","TF","Ratio","Move","Price","Flagged","Notes"], spikes, "no spikes")}</div>
<div class="card"><h2>Paper trade log (incl. hypotheticals)</h2>{table(["Time","Ticker","Purpose","Side","Qty","Limit","Status","Fill","Notes"], pt_rows, "no paper trades yet")}</div>
</section>
<section class="grid2">
<div class="card"><h2>Reddit mentions (24h, ApeWisdom) and news count</h2>{table(["Ticker","Mentions 24h","24h ago","Δ","Upvotes","Rank","News 24h"], red_rows, "no Reddit data")}
<h2 style="margin-top:16px">Upcoming earnings</h2>{table(["Date (ET)","Ticker","When","EPS est.","Source"], earn_rows, "none")}</div>
<div class="card"><h2>Media sentiment, 24h (tier-1 x2, FinBERT/agent)</h2>{table(["Ticker","Headlines","Tier-1","Weighted sent."], msnap_rows)}
<h2 style="margin-top:16px">Top-outlet headlines (tier 1 first, 24h)</h2>{table(["Time","Ticker","Outlet","Tier","Headline","Sent."], media_rows, "no media headlines")}</div>
<div class="card"><h2>News headlines (Benzinga via Alpaca, 48h)</h2>{table(["Time","Ticker","Headline","Sent."], news_rows, "no headlines")}</div>
</section>
<div class="mut">Research and paper trading only. Prices: Alpaca IEX (thin slice of volume) and yfinance (unofficial). X counts: keyword queries, retweets excluded.</div>
</main>
<footer>Built with ❤️ by Francesco Andreoli (<a class="credit" href="https://x.com/francescoswiss" target="_blank" rel="noopener">@francescoswiss</a>) · <a class="credit" href="https://github.com/andreolf/stock-sentiment" target="_blank" rel="noopener">GitHub repo</a></footer>
<script>
const D={json.dumps(data)};
const fmt=new Intl.DateTimeFormat('en-GB',{{timeZone:'Europe/Zurich',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}});
Chart.defaults.color='#8b949e';Chart.defaults.borderColor='#21262d';Chart.defaults.font.size=11;const NARROW=innerWidth<700;
const xs={{type:'linear',ticks:{{callback:v=>fmt.format(new Date(v)),maxTicksLimit:NARROW?4:6,maxRotation:0}}}};
// narrow screens: pin the x range to the data so fewer ticks do not widen the axis
const xr=arrs=>{{if(!NARROW) return xs;const v=[].concat(...arrs.map(a=>a||[])).map(p=>p[0]);return v.length?{{...xs,min:Math.min(...v),max:Math.max(...v)}}:xs}};
const pts=a=>a.map(p=>({{x:p[0],y:p[1]}}));
for(const [t,c] of Object.entries(D.charts)){{
  const el=document.getElementById('c_'+t); if(!el) continue;
  new Chart(el,{{data:{{datasets:[
    {{type:'bar',label:'X posts/h',data:pts(c.counts),backgroundColor:'#1f6feb55',yAxisID:'yv',barPercentage:1,categoryPercentage:1}},
    {{type:'line',label:'Price (IEX 1h)',data:pts(c.price),borderColor:'#e6edf3',borderWidth:1.4,pointRadius:0,yAxisID:'yp'}},
    {{type:'line',label:'Weighted sentiment',data:pts(c.sent),borderColor:'#3fb950',backgroundColor:'#3fb950',pointRadius:3,showLine:true,yAxisID:'ys'}}
  ]}},options:{{maintainAspectRatio:false,animation:false,interaction:{{mode:'nearest',intersect:false}},
    plugins:{{legend:{{labels:{{boxWidth:10}}}},tooltip:{{callbacks:{{title:i=>fmt.format(new Date(i[0].parsed.x))+' Zurich'}}}}}},
    scales:{{x:xr([c.counts,c.price,c.sent]),yv:{{position:'left',grid:{{display:false}},title:{{display:true,text:'posts/h'}}}},
      yp:{{position:'right',title:{{display:true,text:'$'}}}},ys:{{position:'right',min:-1,max:1,display:false}}}}}}}});
}}
const CC={{composite:['Composite','#e6edf3',2.5],z_x_sent:['X sent','#3fb950',1],z_x_vol:['X vol','#1f6feb',1],z_reddit_chg:['Reddit Δ','#ff7b72',1],
  z_media_sent:['Media sent','#d2a8ff',1],z_media_cnt_chg:['Media cnt Δ','#a371f7',1],z_price_vol:['Price vol','#d29922',1]}};
for(const [t,c] of Object.entries(D.charts)){{
  const el=document.getElementById('k_'+t); if(!el) continue;
  const ds=Object.entries(CC).filter(([k])=>(c.comp[k]||[]).length).map(([k,[l,col,w]])=>({{label:l,data:pts(c.comp[k]),borderColor:col,backgroundColor:col,borderWidth:w,pointRadius:w>2?2:0,tension:.2}}));
  new Chart(el,{{type:'line',data:{{datasets:ds}},options:{{maintainAspectRatio:false,animation:false,plugins:{{legend:{{labels:{{boxWidth:10}}}}}},
    scales:{{x:xr(Object.values(c.comp||{{}})),y:{{suggestedMin:-3,suggestedMax:3}}}}}}}});
}}
if(D.equity.length) new Chart(document.getElementById('c_eq'),{{type:'line',data:{{datasets:[{{label:'Equity $',data:pts(D.equity),borderColor:'#58a6ff',pointRadius:2,fill:true,backgroundColor:'#58a6ff22'}}]}},
  options:{{maintainAspectRatio:false,animation:false,scales:{{x:{{type:'linear',ticks:{{callback:v=>new Intl.DateTimeFormat('en-GB',{{timeZone:'Europe/Zurich',day:'2-digit',month:'short'}}).format(new Date(v)),maxTicksLimit:NARROW?4:6}}}}}}}}}});
</script></body></html>"""
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "index.html")
    open(path, "w").write(page)
    print(f"dashboard: {path} ({len(page)//1024} KB)")
    if not a.no_png:
        chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
        if chrome:
            png = os.path.join(OUT, "latest.png")
            r = subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                                "--virtual-time-budget=8000", "--window-size=1440,4900", f"--screenshot={png}",
                                "file://" + path], capture_output=True, text=True, timeout=90)
            print(f"screenshot: {png}" if os.path.exists(png) and r.returncode == 0 else f"  [warn] screenshot failed: {r.stderr[-300:]}")
        else:
            print("  [warn] no chrome/chromium for screenshot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
