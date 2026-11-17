#!/usr/bin/env python3
"""Filter fixes approved by Francesco 2026-11-17 ("sistema i filtri"): news-gate headline quality, publish-time
freshness, per-company direction, trial-headline sentiment, late-entry cutoff, opposite-direction cooldown, earnings_drift
re-entry block, fail-safe exits, Alpaca retries, attention_fade flag, Swiss cooldown/headline filter, earnings reconcile.
Temp DB only (test_trader sets STOCK_SENT_DB); no network, never touches Alpaca."""
import io, json, os, sys, unittest, urllib.error
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_trader import CON, reset, FakeClient, session_now   # sets the temp STOCK_SENT_DB first
import db, newsgate, earnings, swiss
import paper_trader
from paper_trader import Trader, has_live_protection
from alpaca_client import AlpacaPaper, AlpacaError

if os.path.realpath(db.DB_PATH) == os.path.realpath(os.path.join(db.HERE, "data", "sentiment.db")):
    raise RuntimeError("tests resolved STOCK_SENT_DB to the live data/sentiment.db; refusing to run")
ET = db.ZoneInfo("America/New_York")
RISK = {"max_chase_atr": 1.0, "loss_streak_stops": 3, "no_entry_after_et": "15:30", "opposite_cooldown": "session"}


class HeadlineQuality(unittest.TestCase):
    def issue(self, t, h, url=None, author=None):
        return newsgate.headline_issue(t, h, url, author)

    def test_past_false_positives_rejected(self):
        self.assertIn("AMD", self.issue("NVDA", "AMD upgraded because it will benefit from Nvidia's 'leadership vacuum'"))  # #118
        self.assertIn("opinion", self.issue("NVDA", "Jim Cramer on interim Nvidia CEO: 'I like Colette very much'"))       # #120
        self.assertIn("opinion", self.issue(                                                                               # #124
            "NVDA", "Nvidia Crisis \u2013 Visionary Huang\u2019s Resignation Puts America's AI Advantage at Risk",
            "https://www.benzinga.com/opinion/26/11/63213045/nvidia-crisis", "The Arora Report"))
        self.assertIn("wrap", self.issue("NVDA", "Stocks Climb as Buyers Step In Despite Nvidia Woes"))                   # #100
        self.assertIn("basket", self.issue("NVDA", "NVIDIA, Caterpillar & Nike: Three Corporate Events That Could Move Markets"))
        self.assertIn("basket", self.issue("PLTR", "Josh's Best Stocks Update: OKTA, Palantir Technologies; Hewlett Packard"))

    def test_ubs_as_analyst_or_host_rejected(self):
        for h in ("voestalpine-Aktie steigt: UBS bleibt nach Zahlenvorlage bei \u201cBuy\u201d",
                  "UBS ernennt Apollo Global Management zum Top-Pick", "UBS Adjusts Price Target on TWFG to $36 From $34",
                  "Lloyds Banking Group auf der UBS London European Conference: Wachstum mit Disziplin",
                  "UBS Group AG Discloses Change in Major Holdings of Str\u00f6er SE", "UBS-Lohnumfrage: Schweizer L\u00f6hne steigen"):
            self.assertIsNotNone(self.issue("UBS", h), h)

    def test_genuine_company_news_passes(self):
        for t, h in (("UBS", "UBS Announces Workforce Reductions in London"),
                     ("UBS", "AlphaValue/Baader Europe Downgrades UBS Group AG to Reduce"),
                     ("UBS", "US Federal Reserve Fines UBS $268.5 Million Over Credit Suisse Archegos Malpractice"),
                     ("NVDA", "Law Firm Wachtell Lipton to Lead Nvidia Internal Investigation"),
                     ("NVDA", "Nvidia Hires Wachtell Lipton to Investigate CEO"), ("TSLA", "Musk says Tesla and SpaceX explore merger"),
                     ("TSLA", "Tesla shares plunge after guidance cut"), ("NVDA", "Nvidia's China revenue exposure: What New Street estimates"),
                     ("ONON", "Keybanc Maintains Overweight on On Holding, Raises Price Target to $42"),
                     ("NVS", "Novartis Wins Positive CHMP Opinion For Cosentyx In Adolescent Hidradenitis")):
            self.assertIsNone(self.issue(t, h), h)

    def test_other_company_subject_and_subordinate(self):
        self.assertIsNotNone(self.issue("NVDA", "Amkor Secures $1.3 Billion NVIDIA Deal To Expand AI Packaging"))
        self.assertIsNotNone(self.issue("NVDA", "Wall Street on edge ahead of Nvidia earnings"))
        self.assertIsNone(self.issue("NVDA", "Trump says ex Apple CEO Cook should replace Huang at Nvidia"))   # 'ex Apple' = descriptor

    def test_trial_headlines_not_negative(self):
        a = newsgate.adjust_sentiment
        self.assertEqual(a("Novartis gibt Ergebnisse nach 52 Wochen aus dem Phase-III-Programm RemIND zur Untersuchung", -0.3), 0.0)
        self.assertEqual(a("Novartis Phase III trial fails primary endpoint", -0.8), -0.8)
        self.assertEqual(a("Nvidia data center sales fall", -0.6), -0.6)          # not a trial headline
        self.assertEqual(a("Novartis Phase III data positive", 0.7), 0.7)


class GateFreshnessDirection(unittest.TestCase):
    def setUp(self):
        self.con = CON; reset(self.con)
        self.now = session_now()

    def media(self, mid, title, pub_ago, s=0.6, feed=None, fetched_ago=None, outlet="Reuters", ticker="NVDA"):
        self.con.execute("""INSERT INTO media(id,ticker,created_at,fetched_at,outlet,tier,title,sentiment,feed)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (mid, ticker, db.iso(self.now - timedelta(minutes=pub_ago)),
                          db.iso(self.now - timedelta(minutes=fetched_ago if fetched_ago is not None else pub_ago - 1)),
                          outlet, 1, title, s, feed))
        self.con.commit()

    def test_age_by_publish_time_vs_decision_time(self):
        # #120 pattern: 1-hour spike bar started 80 min ago (window covers the headline), decided now -> 75 min old: stale
        self.media("m1", "Nvidia wins record government AI contract", 75, fetched_ago=5, feed="gn_tier1_NVDA", outlet="CNBC")
        trig = db.iso(self.now - timedelta(minutes=80))
        self.assertIsNotNone(newsgate.hard_news(self.con, "NVDA", trig, "long", quality=False))
        rej = []
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", trig, "long", now_iso=db.iso(self.now), rejected=rej))
        self.assertTrue(any("stale" in r for _, r in rej))
        reset(self.con); self.media("m2", "Nvidia wins record government AI contract", 30)
        self.assertIsNotNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now - timedelta(minutes=25)), "long",
                                                now_iso=db.iso(self.now)))

    def test_google_item_without_publish_time_rejected(self):
        self.media("g1", "Nvidia wins record government AI contract", 10, fetched_ago=10, feed="gn_tier1_NVDA", outlet="CNBC")
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "long", now_iso=db.iso(self.now)))

    def test_direction_must_match_per_company(self):
        self.media("d1", "Nvidia names interim CEO", 10, s=0.02)                        # neutral: does not confirm long
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "long", now_iso=db.iso(self.now)))
        reset(self.con); self.media("d2", "Nvidia names interim CEO", 10, s=None)
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "long", now_iso=db.iso(self.now)))
        reset(self.con)
        self.media("d3", "AMD upgraded because it will benefit from Nvidia's 'leadership vacuum'", 10, s=0.91, outlet="CNBC")
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "long", now_iso=db.iso(self.now)))
        reset(self.con); self.media("d4", "Nvidia shares slide as board weighs CEO search", 10, s=-0.7)
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "long", now_iso=db.iso(self.now)))
        self.assertIsNotNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short", now_iso=db.iso(self.now)))


class TraderRules(unittest.TestCase):
    def setUp(self):
        self.con = CON; reset(self.con)

    def at(self, hh, mm):
        return session_now().astimezone(ET).replace(hour=hh, minute=mm).astimezone(db.timezone.utc)

    def setup_trigger(self, now, direction="up", title="Tesla raises delivery guidance after record China sales", s=0.6):
        self.con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,flagged,price)
                            VALUES (?,?,?,?,?,?,?,1,?)""", (db.iso(now - timedelta(minutes=10)), "TSLA", "5Min",
                                                           db.iso(now - timedelta(minutes=4)), 4.2,
                                                           0.6 if direction == "up" else -0.6, direction, 100.0))
        if title:
            self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,sentiment) VALUES (?,?,?,?,?,?,?)",
                             (title[:10] + str(now), "TSLA", db.iso(now - timedelta(minutes=20)), "Reuters", 1, title, s))
        self.con.commit()

    def run_t(self, now, fc=None, cfg=None):
        fc = fc or FakeClient()
        Trader(fc, self.con, cfg or {}, live=True, now=now, kill_path="/nonexistent/KILL", core=["TSLA"], blocked=set(),
               risk=RISK).run()
        self.con.commit()
        return fc

    def reason(self):
        r = self.con.execute("SELECT reason FROM decisions WHERE rule='v2_news_momentum' ORDER BY id DESC").fetchone()
        return r[0] if r else None

    def test_no_entries_after_1530_et(self):
        self.setup_trigger(self.at(15, 25))
        self.assertEqual(len(self.run_t(self.at(15, 25)).placed), 1)
        reset(self.con); self.setup_trigger(self.at(15, 31))
        self.assertEqual(self.run_t(self.at(15, 31)).placed, [])
        self.assertIn("no new entries after 15:30", self.reason())
        self.assertEqual(self.con.execute("SELECT filter FROM missed_calls").fetchone()[0], "late_entry")

    def test_opposite_direction_cooldown_same_session(self):
        now = self.at(11, 0)
        self.con.execute("""INSERT INTO decisions(ts,ticker,rule,trigger_type,trigger_id,direction,action,reason)
                            VALUES (?,?,?,?,?,?,?,?)""", (db.iso(now - timedelta(hours=1)), "TSLA", "v2_news_momentum",
                                                          "composite", 999, "long", "trade", "x"))
        self.setup_trigger(now, "down", "Tesla shares slide after recall of 200,000 vehicles", -0.7)
        self.assertEqual(self.run_t(now).placed, [])
        self.assertIn("opposite-direction cooldown", self.reason())
        # a long trade yesterday does not block today's short
        reset(self.con)
        self.con.execute("""INSERT INTO decisions(ts,ticker,rule,trigger_type,trigger_id,direction,action,reason)
                            VALUES (?,?,?,?,?,?,?,?)""", (db.iso(now - timedelta(days=1)), "TSLA", "v2_news_momentum",
                                                          "composite", 999, "long", "trade", "x"))
        self.setup_trigger(now, "down", "Tesla shares slide after recall of 200,000 vehicles", -0.7)
        self.assertEqual(len(self.run_t(now).placed), 1)

    def test_stopped_today_helper(self):
        now = self.at(13, 0)
        self.con.execute("INSERT INTO paper_trades(ts,ticker,side,qty,purpose,exit_reason,closed_at) VALUES (?,?,?,?,?,?,?)",
                         (db.iso(now - timedelta(hours=2)), "MSTR", "buy", 5, "entry", "stop_loss", db.iso(now - timedelta(hours=1))))
        self.con.commit()
        self.assertTrue(paper_trader.stopped_today(self.con, "MSTR", now))
        self.assertFalse(paper_trader.stopped_today(self.con, "MSTR", now + timedelta(days=1)))
        self.assertFalse(paper_trader.stopped_today(self.con, "TSLA", now))

    def test_attention_fade_flag(self):
        now = self.at(11, 0)
        self.setup_trigger(now, title=None)
        self.assertEqual(self.run_t(now).placed, [])                                 # default: hypothetical only
        self.assertEqual(self.con.execute("SELECT action, direction FROM decisions WHERE rule='v2_attention_fade'").fetchone()[:],
                         ("hypothetical", "short"))
        reset(self.con); self.setup_trigger(now, title=None)
        fc = self.run_t(now, cfg={"attention_fade_trading": True})
        self.assertEqual(len(fc.placed), 1); self.assertEqual(fc.placed[0]["side"], "sell")   # fades the up-spike

    def test_failsafe_flattens_naked_position(self):
        now = self.at(12, 0)
        self.con.execute("""INSERT INTO paper_trades(ts,ticker,side,qty,status,alpaca_order_id,purpose,filled_qty,exit_due)
                            VALUES (?,?,?,?,?,?,?,?,?)""", (db.iso(now - timedelta(minutes=10)), "TSLA", "buy", 10, "filled", "n1",
                                                          "entry", 10, db.iso(now + timedelta(minutes=50))))
        self.con.commit()
        fc = FakeClient(positions=[{"symbol": "TSLA", "qty": "10", "market_value": "1000"}])
        fc.orders_by_id["n1"] = {"id": "n1", "status": "filled", "filled_qty": "10", "type": "limit",
                                 "legs": [{"type": "limit", "status": "canceled"}, {"type": "stop", "status": "rejected"}]}
        Trader(fc, self.con, {}, live=True, now=now, kill_path="/nonexistent/KILL", core=["TSLA"], blocked=set(), risk=RISK).run_exits()
        self.con.commit()
        self.assertEqual(fc.closed, ["TSLA"])
        self.assertEqual(self.con.execute("SELECT exit_reason FROM paper_trades WHERE alpaca_order_id='n1'").fetchone()[0], "failsafe")
        # protected entry is left alone
        self.assertTrue(has_live_protection({"legs": [{"type": "stop", "status": "held"}]}))
        self.assertFalse(has_live_protection({"legs": []}))

    def test_submit_error_recovers_existing_order(self):
        class Flaky(FakeClient):
            def place_bracket_order(self, sym, qty, side, lp, tp, sl, client_order_id=None, **kw):
                o = super().place_bracket_order(sym, qty, side, lp, tp, sl, client_order_id)
                o["client_order_id"] = client_order_id
                raise AlpacaError(0, "network error after 4 tries: timeout")
        now = self.at(11, 0); self.setup_trigger(now)
        fc = self.run_t(now, Flaky())
        r = self.con.execute("SELECT alpaca_order_id FROM paper_trades WHERE purpose='entry'").fetchone()
        self.assertEqual(r[0], "o1")                                               # recorded, managed, not resubmitted
        self.assertEqual(len(fc.placed), 1)

        class Dead(FakeClient):
            def place_bracket_order(self, *a, **k):
                raise AlpacaError(500, "boom")
        reset(self.con); self.setup_trigger(now)
        self.run_t(now, Dead())
        self.assertIsNone(self.con.execute("SELECT 1 FROM paper_trades WHERE purpose='entry'").fetchone())


class AlpacaRetries(unittest.TestCase):
    def client(self):
        c = AlpacaPaper(key_id="k", secret="s")
        c._sleep = lambda s: None
        c.retries = 3
        return c

    def resp(self, body):
        m = mock.MagicMock(); m.__enter__.return_value.read.return_value = json.dumps(body).encode(); return m

    def test_get_retries_transient_errors(self):
        c = self.client()
        seq = [urllib.error.URLError("timeout"), urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b"busy")),
               self.resp({"is_open": True})]
        with mock.patch("urllib.request.urlopen", side_effect=seq) as m:
            self.assertEqual(c.clock(), {"is_open": True})
            self.assertEqual(m.call_count, 3)

    def test_client_error_not_retried(self):
        c = self.client()
        with mock.patch("urllib.request.urlopen", side_effect=[urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b"no"))]) as m:
            with self.assertRaises(AlpacaError):
                c.account()
            self.assertEqual(m.call_count, 1)

    def test_order_without_client_id_not_retried_and_duplicate_id_recovered(self):
        c = self.client()
        with mock.patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("timeout")]) as m:
            with self.assertRaises(AlpacaError):
                c.place_bracket_order("TSLA", 1, "buy", 100, 104, 98)
            self.assertEqual(m.call_count, 1)
        seq = [urllib.error.URLError("timeout"),
               urllib.error.HTTPError("u", 422, "x", {}, io.BytesIO(b'{"message":"client_order_id must be unique"}')),
               self.resp({"id": "abc", "status": "new"})]
        with mock.patch("urllib.request.urlopen", side_effect=seq) as m:
            self.assertEqual(c.place_bracket_order("TSLA", 1, "buy", 100, 104, 98, client_order_id="ss-x")["id"], "abc")
            self.assertIn("by_client_order_id", m.call_args_list[-1][0][0].full_url)

    def test_retry_never_leaves_paper_host(self):
        c = self.client()
        with mock.patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("t")] * 4) as m:
            with self.assertRaises(AlpacaError):
                c.clock()
            for call in m.call_args_list:
                self.assertTrue(call[0][0].full_url.startswith("https://paper-api.alpaca.markets/v2/"))


class SwissFilters(unittest.TestCase):
    NOW = db.datetime(2026, 10, 21, 18, 0, tzinfo=db.timezone.utc)      # 14:00 ET

    def setUp(self):
        self.con = CON; reset(self.con)
        for t in ("swiss_observations", "swiss_obs_skipped"):
            self.con.execute(f"DELETE FROM {t}")
        self.con.commit()
        self.so = {**db.load_config()["swiss_observe"], "opposite_cooldown": "session"}

    def test_opposite_direction_cooldown(self):
        good = lambda t: {"price": 100.0, "basis": "iex_trade_now"}
        a = swiss.log_obs(self.con, self.NOW, "NVS", self.so, "swiss_news", "news", "a", "long", "x", price_fn=good)
        b = swiss.log_obs(self.con, self.NOW + timedelta(hours=1), "NVS", self.so, "swiss_spike_fade", "iex_spike", "b", "short",
                          "y", price_fn=good)
        self.assertIsNotNone(a); self.assertIsNone(b)
        self.assertIn("opposite-direction", self.con.execute("SELECT skip_reason FROM swiss_obs_skipped").fetchone()[0])
        c = swiss.log_obs(self.con, self.NOW + timedelta(days=1), "NVS", self.so, "swiss_spike_fade", "iex_spike", "c", "short",
                          "y", price_fn=good)
        self.assertIsNotNone(c)                                                      # next session: allowed

    def test_news_headline_filter_and_trial(self):
        rows = [{"id": "u1", "ticker": "UBS", "created_at": db.iso(self.NOW), "title": "UBS Adjusts Price Target on TWFG to $36",
                 "feed": "benzinga", "kind": "benzinga", "tier": 2, "sentiment": 0.78, "novelty": 0.9},
                {"id": "n1", "ticker": "NVS", "created_at": db.iso(self.NOW),
                 "title": "Novartis gibt Ergebnisse nach 52 Wochen aus dem Phase-III-Programm RemIND zur Untersuchung",
                 "feed": "gn_ch_NVS", "kind": "news", "tier": 2, "sentiment": -0.3, "novelty": 0.7}]
        good = lambda t: {"price": 100.0, "basis": "iex_trade_now"}
        swiss.observe(self.con, self.so, self.NOW, new_news=rows, price_fn=good)
        self.assertIsNone(self.con.execute("SELECT 1 FROM swiss_observations WHERE rule='swiss_news'").fetchone())
        self.assertIn("analyst", self.con.execute("SELECT skip_reason FROM swiss_obs_skipped WHERE ticker='UBS'").fetchone()[0])


class EarningsReconcile(unittest.TestCase):
    def test_canonical_reports(self):
        rows = [{"ticker": "NVDA", "date": "2026-11-17", "time_of_day": "after-hours", "source": "nasdaq"},
                {"ticker": "NVDA", "date": "2026-11-18", "time_of_day": "not-supplied", "source": "nasdaq"},
                {"ticker": "META", "date": "2026-10-21", "time_of_day": "not-supplied", "source": "nasdaq"},
                {"ticker": "META", "date": "2026-10-22", "time_of_day": None, "source": "yfinance"},
                {"ticker": "META", "date": "2027-01-28", "time_of_day": None, "source": "yfinance"}]
        keep, drop = earnings.canonical_reports(rows)
        self.assertEqual(sorted(drop), [("META", "2026-10-22"), ("NVDA", "2026-11-18")])
        self.assertIn(("META", "2027-01-28"), keep)

    def test_reconcile_deletes_duplicates(self):
        con = CON; reset(con)
        for t, d, tod, src in (("NVDA", "2026-11-17", "after-hours", "nasdaq"), ("NVDA", "2026-11-18", "not-supplied", "nasdaq")):
            con.execute("INSERT INTO earnings(ticker,date,time_of_day,source) VALUES (?,?,?,?)", (t, d, tod, src))
        con.execute("INSERT INTO earnings_events(ticker,report_date) VALUES ('NVDA','2026-11-18')"); con.commit()
        self.assertEqual(earnings.reconcile(con), [("NVDA", "2026-11-18")])
        self.assertEqual([r[0] for r in con.execute("SELECT date FROM earnings")], ["2026-11-17"])
        self.assertIsNone(con.execute("SELECT 1 FROM earnings_events").fetchone())


if __name__ == "__main__":
    unittest.main()
