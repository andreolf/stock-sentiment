#!/usr/bin/env python3
"""Risk rules approved 2026-10-25 (US arm, paper only, exits unchanged): backtest harness (+ test-only partial TP),
max chase, loss-streak breaker, missed calls (DB table, migration, dashboard). Temp DB only; no network, no Alpaca.
Run: .venv/bin/python -m unittest tests/test_risk.py -v"""
import os, sqlite3, sys, tempfile, unittest
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
os.environ.setdefault("STOCK_SENT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))
import db
if os.path.realpath(db.DB_PATH) == os.path.realpath(os.path.join(db.HERE, "data", "sentiment.db")):
    raise RuntimeError("tests resolved STOCK_SENT_DB to the live data/sentiment.db; refusing to run")
from test_trader import CON, reset, FakeClient
import backtest, composite, paper_trader
from paper_trader import Trader, chase_atr, session_breaker, exit_kind_from_order

ET = db.ZoneInfo("America/New_York")
UTC = db.timezone.utc
RISK = {"max_chase_atr": 1.0, "loss_streak_stops": 3, "top_conviction_min_composite": 3.0, "top_conviction_news_tier": 1,
        "top_conviction_composite_lookback_min": 30, "log_missed_calls": True}


def at(hh, mm, day=21):
    return db.datetime(2026, 10, day, hh, mm, tzinfo=ET).astimezone(UTC)


def mbars(start, rows):
    """rows = [(o,h,l,c), ...] one per minute from `start`."""
    return [{"t": db.iso(start + timedelta(minutes=i)), "o": o, "h": h, "l": l, "c": c} for i, (o, h, l, c) in enumerate(rows)]


HOUR = [{"h": 101, "l": 99, "c": 100}] * 20          # ATR 2 -> stop 1.5 x 2 = 3.0, tp 6.0
P = backtest.params_from_config({}, equity=100_000)[0]


# ---------------- 1. backtest harness ----------------
class Backtest(unittest.TestCase):
    def test_stop_target_and_both_in_one_bar(self):
        t0 = at(10, 0)
        stop = mbars(t0, [(100, 100.5, 99.5, 100)] * 3 + [(99, 99.2, 96.5, 97)])
        r = backtest.simulate(stop, HOUR, "buy", t0, {**P, "slippage_pct_per_side": 0})
        self.assertEqual((r["stop"], r["tp"], r["exit_kinds"]), (97.0, 106.0, ["stop_loss"]))
        self.assertAlmostEqual(r["exit"], 97.0)
        tp = mbars(t0, [(100, 101, 99.5, 100.5), (101, 106.5, 100.8, 106)])
        self.assertEqual(backtest.simulate(tp, HOUR, "buy", t0, P)["exit_kinds"], ["take_profit"])
        both = mbars(t0, [(100, 107, 96, 100)])                     # touches both: conservative = stop
        self.assertEqual(backtest.simulate(both, HOUR, "buy", t0, P)["exit_kinds"], ["stop_loss"])
        gap = mbars(t0, [(100, 100, 99.5, 99.8), (95, 95.5, 94, 95)])   # gap through the stop fills at the open
        self.assertAlmostEqual(backtest.simulate(gap, HOUR, "buy", t0, {**P, "slippage_pct_per_side": 0})["exit"], 95)
        short = mbars(t0, [(100, 100.4, 99.6, 100), (100, 100.2, 93.5, 94)])
        self.assertEqual(backtest.simulate(short, HOUR, "sell", t0, P)["exit_kinds"], ["take_profit"])

    def test_time_exit_60min_and_before_close(self):
        t0 = at(10, 0)
        flat = mbars(t0, [(100, 100.3, 99.7, 100)] * 90)
        r = backtest.simulate(flat, HOUR, "buy", t0, P)
        self.assertEqual(r["exit_kinds"], ["time"])
        self.assertEqual(r["exit_ts"], db.iso(t0 + timedelta(minutes=60)))
        late = at(15, 20)
        r = backtest.simulate(mbars(late, [(100, 100.3, 99.7, 100)] * 40), HOUR, "buy", late, P)
        self.assertEqual(r["exit_ts"], db.iso(at(15, 45)))          # flat 15 min before the 16:00 close

    def test_costs_slippage_and_commission(self):
        t0 = at(10, 0)
        tp = mbars(t0, [(100, 106.5, 99.9, 106)])
        p = {**P, "slippage_pct_per_side": 0.05, "commission_per_share": 0.01, "commission_min_per_order": 1.0}
        r = backtest.simulate(tp, HOUR, "buy", t0, p)
        q = r["qty"]                                               # 0.5% risk / $3 stop -> 166, 5% cap -> 50
        self.assertEqual(q, 50)
        self.assertAlmostEqual(r["gross"], 6.0 * q)
        net = (106 * 0.9995 - 100 * 1.0005) * q - 2 * max(1.0, 0.01 * q)
        self.assertAlmostEqual(r["net"], net)
        self.assertAlmostEqual(r["cost"], r["gross"] - net)

    def test_partial_tp_runner_is_test_only(self):
        t0 = at(10, 0)
        # +1R (103) hit on bar 1, then back to entry -> runner stopped at breakeven
        rows = [(100, 100.5, 99.5, 100), (100.5, 103.2, 100.4, 103), (102, 102, 99.9, 100)]
        r = backtest.simulate(mbars(t0, rows), HOUR, "buy", t0, {**P, "slippage_pct_per_side": 0},
                              {"fraction": 0.5, "tp1_r": 1.0, "runner_r": 3.0, "runner_stop": "breakeven"})
        self.assertEqual(r["exit_kinds"], ["breakeven", "take_profit"])
        self.assertAlmostEqual(r["gross"], 25 * 3.0)               # half at +3, half at 0
        # the live trader has no partial-TP path: brackets stay single take-profit at 2x stop distance
        import inspect
        self.assertNotIn("partial", inspect.getsource(paper_trader.Trader).lower())

    def test_summary(self):
        trades = [{"net": 100.0, "gross": 110, "cost": 10, "r_net": 0.2, "entry_ts": "2026-10-21T14:00:00Z", "exit_kinds": ["take_profit"]},
                  {"net": -50.0, "gross": -45, "cost": 5, "r_net": -0.1, "entry_ts": "2026-10-22T14:00:00Z", "exit_kinds": ["stop_loss"]}]
        s = backtest.summarize(trades, 100_000)
        self.assertEqual((s["trades"], s["wins"], s["win_rate"], s["net_pl"], s["costs"], s["profit_factor"]), (2, 1, 0.5, 50.0, 15, 2.0))

    def test_replay_from_db_no_swiss_no_trading_calls(self):
        con = CON; reset(con)
        t = at(10, 0)
        con.execute("INSERT INTO signals(ts,ticker,type,variant,zscore,direction,price) VALUES (?,?,?,?,?,?,?)",
                    (db.iso(t), "NVDA", "composite", "follow", -3.1, "short", 100.0))
        con.execute("INSERT INTO signals(ts,ticker,type,variant,zscore,direction,price) VALUES (?,?,?,?,?,?,?)",
                    (db.iso(t), "UBS", "composite", "follow", 3.1, "long", 30.0))       # Swiss: never replayed
        con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,price,flagged)
                       VALUES (?,?,?,?,?,?,?,?,1)""", (db.iso(at(11, 0)), "TSLA", "5Min", db.iso(at(11, 6)), 4.0, 0.6, "up", 100.0))
        con.execute("""INSERT INTO decisions(ts,ticker,rule,trigger_type,trigger_id,direction,action) VALUES (?,?,?,?,?,?,?)""",
                    (db.iso(t), "TSLA", "v2_news_momentum", "volume_spike",
                     con.execute("SELECT id FROM volume_spikes").fetchone()[0], "long", "trade"))
        con.commit()
        trigs = backtest.load_triggers(con, core=["TSLA", "NVDA", "PLTR"])
        self.assertEqual(sorted(x["ticker"] for x in trigs), ["NVDA", "TSLA"])
        self.assertEqual([x["traded_live"] for x in trigs], [False, True])

        class BarsOnly:                                            # any trading call would raise AttributeError
            calls = []
            def bars(self, sym, tf, start=None, end=None, **kw):
                self.calls.append(tf)
                if tf == "1Hour":
                    return {sym: [{"t": db.iso(at(9, 0) - timedelta(hours=i)), "h": 101, "l": 99, "c": 100} for i in range(30, 1, -1)]}
                return {sym: mbars(at(9, 30), [(100, 100.3, 99.7, 100)] * 390)}
        bc = BarsOnly()
        out = backtest.run(con, bc, P, partial={"fraction": 0.5, "tp1_r": 1, "runner_r": 3}, max_chase=1.0, triggers=trigs)
        self.assertEqual(len(out["results"]), 2)
        self.assertTrue(all(r["base"]["exit_kinds"] == ["time"] for r in out["results"]))
        self.assertEqual(set(bc.calls), {"1Min", "1Hour"})
        with mock.patch("builtins.print"):
            s = backtest.report(out, P, {"fraction": 0.5})
        self.assertEqual(s["traded live (v2_news_momentum)"]["trades"], 1)
        self.assertIn("TEST-ONLY partial TP + runner (all triggers)", s)

    def test_backtest_never_makes_x_calls(self):
        src = open(os.path.join(db.HERE, "backtest.py")).read()
        for bad in ("budget", "x_api", "search_posts", "import x", "paper-api"):
            self.assertNotIn(bad, src)


# ---------------- 2. max chase ----------------
class _Base(unittest.TestCase):
    """Shared setup/helpers (no tests of its own)."""
    def setUp(self):
        self.con = CON; reset(self.con)
        self.now = at(11, 0)

    def tearDown(self):
        reset(self.con)

    def setup_trigger(self, price, direction="up", title="Tesla raises delivery guidance after record China sales", s=0.6):
        self.con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,price,flagged)
                            VALUES (?,?,?,?,?,?,?,?,1)""", (db.iso(self.now - timedelta(minutes=10)), "TSLA", "5Min",
                                                           db.iso(self.now - timedelta(minutes=4)), 4.2,
                                                           0.6 if direction == "up" else -0.6, direction, price))
        self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,sentiment) VALUES (?,?,?,?,?,?,?)",
                         ("n1", "TSLA", db.iso(self.now - timedelta(minutes=20)), "Reuters", 1, title, s))
        self.con.commit()

    def run_t(self, risk=RISK):
        fc = FakeClient()
        Trader(fc, self.con, {}, live=True, now=self.now, kill_path="/nonexistent/KILL", core=["TSLA"], blocked=set(), risk=risk).run()
        self.con.commit()
        return fc


class MaxChase(_Base):
    def test_chase_math(self):
        self.assertAlmostEqual(chase_atr("long", 96, 100, 2), 2.0)
        self.assertAlmostEqual(chase_atr("short", 104, 100, 2), 2.0)
        self.assertAlmostEqual(chase_atr("long", 104, 100, 2), -2.0)     # moved against: never a chase
        self.assertIsNone(chase_atr("long", None, 100, 2))

    def test_skip_when_price_ran_more_than_1_atr(self):
        self.setup_trigger(97.5)                     # live ~100.0, ATR 2 -> 1.25 ATR past the signal
        fc = self.run_t()
        self.assertEqual(fc.placed, [])
        d = self.con.execute("SELECT * FROM decisions WHERE rule='v2_news_momentum'").fetchone()
        self.assertEqual(d["action"], "hypothetical"); self.assertIn("max chase", d["reason"])
        m = self.con.execute("SELECT * FROM missed_calls").fetchone()
        self.assertEqual((m["filter"], m["ticker"], m["direction"], m["signal_price"]), ("max_chase", "TSLA", "long", 97.5))
        self.assertAlmostEqual(m["live_price"], 100.0, places=2)

    def test_trades_within_limit_and_short_side(self):
        self.setup_trigger(98.5)                     # 0.75 ATR -> fine
        self.assertEqual(len(self.run_t().placed), 1)
        reset(self.con)
        self.setup_trigger(102.5, "down", "Tesla cuts delivery guidance after weak China sales", -0.6)   # short chased
        self.assertEqual(self.run_t().placed, [])
        self.assertEqual(self.con.execute("SELECT filter FROM missed_calls").fetchone()[0], "max_chase")

    def test_configurable_and_off(self):
        self.setup_trigger(97.5)
        self.assertEqual(len(self.run_t({**RISK, "max_chase_atr": 1.5}).placed), 1)
        reset(self.con); self.setup_trigger(90)
        self.assertEqual(len(self.run_t({**RISK, "max_chase_atr": 0}).placed), 1)

    def test_config_json_defaults(self):
        r = db.load_config()["risk"]
        self.assertEqual((r["max_chase_atr"], r["loss_streak_stops"], r["top_conviction_min_composite"],
                          r["top_conviction_news_tier"]), (1.0, 3, 3.0, 1))
        b = db.load_config()["backtest"]
        self.assertEqual((b["slippage_pct_per_side"], b["commission_per_share"]), (0.05, 0.0))


# ---------------- 3. loss-streak breaker ----------------
class LossStreak(_Base):
    def test_breaker_math(self):
        self.assertFalse(session_breaker(["stop_loss", "stop_loss"], 3))
        self.assertTrue(session_breaker(["stop_loss"] * 3, 3))
        self.assertFalse(session_breaker(["stop_loss", "stop_loss", "time", "stop_loss"], 3))
        self.assertTrue(session_breaker(["stop_loss", "unknown", "stop_loss", "stop_loss"], 3))
        self.assertTrue(session_breaker(["stop_loss"] * 3 + ["take_profit"], 3))     # stays on for the session
        self.assertFalse(session_breaker(["stop_loss"] * 5, 0))                       # 0 = off

    def test_exit_kind_from_legs(self):
        legs = lambda st_stop, st_tp: {"legs": [{"type": "limit", "status": st_tp}, {"type": "stop", "status": st_stop}]}
        self.assertEqual(exit_kind_from_order(legs("filled", "canceled")), "stop_loss")
        self.assertEqual(exit_kind_from_order(legs("canceled", "filled")), "take_profit")
        self.assertEqual(exit_kind_from_order({"legs": []}), "unknown")

    def stops(self, n, day=21):
        for i in range(n):
            self.con.execute("""INSERT INTO paper_trades(ts,ticker,side,qty,status,purpose,filled_qty,closed_at,exit_reason)
                                VALUES (?,?,?,?,?,?,?,?,?)""", (db.iso(at(9, 40 + i, day)), "NVDA", "buy", 10, "filled", "entry",
                                                                10, db.iso(at(9, 50 + i, day)), "stop_loss"))
        self.con.commit()

    def test_three_stops_block_ordinary_setup(self):
        self.stops(3); self.setup_trigger(100)
        self.assertEqual(self.run_t().placed, [])
        m = self.con.execute("SELECT * FROM missed_calls").fetchone()
        self.assertEqual(m["filter"], "loss_streak"); self.assertIn("composite n/a", m["reason"])

    def test_top_conviction_still_trades(self):
        self.stops(3); self.setup_trigger(100)
        self.con.execute("INSERT INTO composite(ts,ticker,composite) VALUES (?,?,?)", (db.iso(self.now - timedelta(minutes=8)), "TSLA", 3.4))
        self.con.commit()
        self.assertEqual(len(self.run_t().placed), 1)

    def test_tier2_news_or_weak_composite_blocked(self):
        self.stops(3)
        self.con.execute("INSERT INTO composite(ts,ticker,composite) VALUES (?,?,?)", (db.iso(self.now - timedelta(minutes=8)), "TSLA", 2.6))
        self.setup_trigger(100)
        self.assertEqual(self.run_t().placed, [])                                  # composite 2.6 < 3.0
        reset(self.con); self.stops(3)
        self.con.execute("INSERT INTO composite(ts,ticker,composite) VALUES (?,?,?)", (db.iso(self.now - timedelta(minutes=8)), "TSLA", 3.4))
        self.setup_trigger(100); self.con.execute("UPDATE media SET tier=2, outlet='MarketWatch'"); self.con.commit()
        self.assertEqual(self.run_t().placed, [])                                  # tier-2 news is not top conviction
        reset(self.con); self.stops(3)
        self.con.execute("INSERT INTO composite(ts,ticker,composite) VALUES (?,?,?)", (db.iso(self.now - timedelta(minutes=8)), "TSLA", -3.4))
        self.setup_trigger(100)
        self.assertEqual(self.run_t().placed, [])                                  # composite against the direction

    def test_resets_next_session_and_two_stops_ok(self):
        self.stops(3, day=20); self.setup_trigger(100)                             # yesterday's streak
        self.assertEqual(len(self.run_t().placed), 1)
        reset(self.con); self.stops(2); self.setup_trigger(100)
        self.assertEqual(len(self.run_t().placed), 1)

    def test_manage_exits_records_stop_loss_from_legs(self):
        self.con.execute("""INSERT INTO paper_trades(ts,ticker,side,qty,status,alpaca_order_id,purpose,filled_qty,exit_due)
                            VALUES (?,?,?,?,?,?,?,?,?)""", (db.iso(self.now - timedelta(minutes=20)), "TSLA", "buy", 10, "filled",
                                                          "e9", "entry", 10, db.iso(self.now + timedelta(minutes=40))))
        self.con.commit()
        fc = FakeClient(positions=[])
        fc.orders_by_id["e9"] = {"id": "e9", "status": "filled", "filled_qty": "10", "filled_avg_price": "100",
                                 "legs": [{"type": "limit", "status": "canceled"}, {"type": "stop", "status": "filled"}]}
        Trader(fc, self.con, {}, live=True, now=self.now, kill_path="/nonexistent/KILL", core=["TSLA"], blocked=set(), risk=RISK).run_exits()
        self.con.commit()
        r = self.con.execute("SELECT exit_reason, closed_at FROM paper_trades WHERE id=(SELECT MAX(id) FROM paper_trades)").fetchone()
        self.assertEqual(r["exit_reason"], "stop_loss"); self.assertIsNotNone(r["closed_at"])

    def test_time_exit_sets_reason_time(self):
        self.con.execute("""INSERT INTO paper_trades(ts,ticker,side,qty,status,alpaca_order_id,purpose,filled_qty,exit_due)
                            VALUES (?,?,?,?,?,?,?,?,?)""", (db.iso(self.now - timedelta(minutes=61)), "TSLA", "buy", 10, "filled",
                                                          "e8", "entry", 10, db.iso(self.now - timedelta(minutes=1))))
        self.con.commit()
        fc = FakeClient(positions=[{"symbol": "TSLA", "qty": "10", "market_value": "1000"}])
        fc.orders_by_id["e8"] = {"id": "e8", "status": "filled", "filled_qty": "10"}
        Trader(fc, self.con, {}, live=True, now=self.now, kill_path="/nonexistent/KILL", core=["TSLA"], blocked=set(), risk=RISK).run_exits()
        self.con.commit()
        self.assertEqual(self.con.execute("SELECT exit_reason FROM paper_trades WHERE alpaca_order_id='e8'").fetchone()[0], "time")


# ---------------- 4. missed calls ----------------
class MissedCalls(_Base):
    def test_news_gate_and_earnings_pause_logged(self):
        self.con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,price,flagged)
                            VALUES (?,?,?,?,?,?,?,?,1)""", (db.iso(self.now - timedelta(minutes=10)), "TSLA", "5Min",
                                                           db.iso(self.now - timedelta(minutes=4)), 4.2, -0.6, "down", 101.2))
        self.con.commit()
        self.run_t(); self.run_t()                                                 # idempotent
        rows = self.con.execute("SELECT * FROM missed_calls").fetchall()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["ticker"], r["direction"], r["signal_price"], r["filter"], r["rule"]),
                         ("TSLA", "short", 101.2, "news_gate", "v2_attention_fade"))   # original direction, not the fade
        self.assertIsNotNone(r["signal_ts"]); self.assertIsNotNone(r["ts"])

    def test_market_closed_and_dry_run(self):
        self.setup_trigger(100)
        fc = FakeClient(is_open=False)
        Trader(fc, self.con, {}, live=False, now=self.now, kill_path="/nonexistent/KILL", core=["TSLA"], blocked=set(), risk=RISK).run()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM missed_calls").fetchone()[0], 0)   # dry run writes nothing
        Trader(fc, self.con, {}, live=True, now=self.now, kill_path="/nonexistent/KILL", core=["TSLA"], blocked=set(), risk=RISK).run()
        self.con.commit()
        self.assertEqual(self.con.execute("SELECT filter FROM missed_calls").fetchone()[0], "market_closed")

    def test_swiss_never_logged_or_traded(self):
        self.con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,price,flagged)
                            VALUES (?,?,?,?,?,?,?,?,1)""", (db.iso(self.now - timedelta(minutes=10)), "UBS", "5Min",
                                                           db.iso(self.now - timedelta(minutes=4)), 4.2, 0.6, "up", 30))
        self.con.commit()
        fc = FakeClient()
        Trader(fc, self.con, {}, live=True, now=self.now, kill_path="/nonexistent/KILL", core=["UBS"], blocked={"UBS"}, risk=RISK).run()
        self.assertEqual(fc.placed, [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM missed_calls").fetchone()[0], 0)

    def test_composite_threshold_and_debounce(self):
        now = self.now
        composite.near_miss(self.con, "NVDA", -1.7, now, 1, price_fn=lambda t: 180.0)
        composite.near_miss(self.con, "NVDA", -1.8, now + timedelta(minutes=5), 2, price_fn=lambda t: 180.0)   # once per 2h
        composite.near_miss(self.con, "PLTR", 1.2, now, 3, price_fn=lambda t: 180.0)                          # below the band
        composite.missed_once(self.con, "TSLA", 2.4, now, 4, "debounce", "debounced", price_fn=lambda t: 400.0)
        rows = self.con.execute("SELECT ticker, direction, filter, signal_price FROM missed_calls ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("NVDA", "short", "threshold", 180.0), ("TSLA", "long", "debounce", 400.0)])

    def test_backfill_from_old_decisions(self):
        self.con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,price,flagged)
                            VALUES (?,?,?,?,?,?,?,?,1)""", (db.iso(self.now), "TSLA", "5Min", db.iso(self.now), 4, 0.6, "up", 380.0))
        sid = self.con.execute("SELECT MAX(id) FROM volume_spikes").fetchone()[0]
        for rule, d, why in (("v2_news_momentum", "long", "earnings_pause (reaction session)"),
                             ("v2_attention_fade", "short", "attention-only, no fresh novel news -> fade (paper log only)"),
                             ("v1_follow_spike", "long", "old rule: no agreeing spike")):
            self.con.execute("INSERT INTO decisions(ts,ticker,rule,trigger_type,trigger_id,direction,action,reason) VALUES (?,?,?,?,?,?,?,?)",
                             (db.iso(self.now), "TSLA", rule, "volume_spike", sid if rule != "v2_attention_fade" else sid + 1, d, "hypothetical", why))
        self.con.commit()
        self.assertEqual(paper_trader.backfill_missed(self.con), 2)            # v1 (retired-rule log) is not a missed call
        self.assertEqual(paper_trader.backfill_missed(self.con), 0)            # idempotent
        rows = {r["filter"]: r for r in self.con.execute("SELECT * FROM missed_calls")}
        self.assertEqual(rows["earnings_pause"]["signal_price"], 380.0)
        self.assertEqual(rows["news_gate"]["direction"], "long")               # fade logged under the original direction

    def test_migration_on_old_db(self):
        p = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(p)
        c.execute("""CREATE TABLE paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, side TEXT, qty REAL,
                     limit_price REAL, status TEXT, alpaca_order_id TEXT, signal_id INTEGER)""")
        c.execute("INSERT INTO paper_trades(ts,ticker) VALUES ('2026-10-21T14:00:00Z','TSLA')"); c.commit(); c.close()
        con = db.connect(p); con = db.connect(p)                                    # idempotent
        self.assertIn("exit_reason", {r[1] for r in con.execute("PRAGMA table_info(paper_trades)")})
        self.assertEqual(con.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 1)    # data kept
        self.assertEqual(con.execute("SELECT COUNT(*) FROM missed_calls").fetchone()[0], 0)

    def test_dashboard_section_and_credits(self):
        import dashboard
        db.log_missed(self.con, "PLTR", "long", "max_chase", "max chase: live 205 is 1.4 ATR past signal 200",
                      ts=db.iso(self.now), signal_price=200.0, live_price=205.0, rule="v2_news_momentum",
                      trigger_type="composite", trigger_id=99)
        self.con.commit()
        out = tempfile.mkdtemp()
        with mock.patch.object(dashboard, "OUT", out), mock.patch.object(sys, "argv", ["dashboard.py", "--no-png", "--no-alpaca"]), \
             mock.patch("builtins.print"):
            dashboard.main()
        page = open(os.path.join(out, "index.html")).read()
        self.assertIn("Missed calls", page); self.assertIn("max_chase", page); self.assertIn("PLTR", page)
        self.assertEqual(page.count("Built with ❤️ by Francesco Andreoli"), 2)
        self.assertIn('name="viewport"', page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
