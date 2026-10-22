#!/usr/bin/env python3
"""Unit tests: sizing (incl. volatility sizing), bracket levels/ATR, paper-only URL guard, news gate, v2 trading
rules (fake Alpaca client, temp DB), earnings pause/drift, evaluation slippage + random baseline.
Run: .venv/bin/python -m unittest tests/test_trader.py -v   (no network, never touches Alpaca)"""
import os, sys, tempfile, unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_SENT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import db, newsgate, evaluate

# 2026-10-22: never run against the live DB (an import-order slip once let test_trader.reset() wipe data/sentiment.db)
if os.path.realpath(db.DB_PATH) == os.path.realpath(os.path.join(db.HERE, "data", "sentiment.db")):
    raise RuntimeError("tests resolved STOCK_SENT_DB to the live data/sentiment.db; refusing to run")
from alpaca_client import AlpacaPaper, PaperOnlyError, guard_url
from paper_trader import Trader, size_qty, limit_price, exposure_of, atr, bracket_levels


class Sizing(unittest.TestCase):
    def test_max_5pct_per_trade(self):
        self.assertEqual(size_qty(100_000, 0, 100), 50)
        self.assertLessEqual(size_qty(100_000, 0, 380.45) * 380.45, 5_000)

    def test_max_20pct_total(self):
        self.assertEqual(size_qty(100_000, 18_000, 100), 20)
        self.assertEqual(size_qty(100_000, 20_000, 100), 0)
        self.assertEqual(size_qty(100_000, 25_000, 100), 0)
        self.assertEqual(size_qty(100_000, -18_000, 100), 20)

    def test_volatility_sizing(self):
        # risk 0.5% of 100k = $500; stop $2 -> 250 sh, but 5% cap ($5k @ $100) -> 50
        self.assertEqual(size_qty(100_000, 0, 100, stop_dist=2, risk_pct=0.5), 50)
        # wide stop $25 -> 20 sh (risk binds, below the 5% cap)
        self.assertEqual(size_qty(100_000, 0, 100, stop_dist=25, risk_pct=0.5), 20)
        # risk sizing never exceeds the 20% total cap
        self.assertEqual(size_qty(100_000, 19_500, 100, stop_dist=0.5, risk_pct=0.5), 5)

    def test_degenerate(self):
        for args in [(0, 0, 100), (100_000, 0, 0), (100_000, 0, None), (-5, 0, 10)]:
            self.assertEqual(size_qty(*args), 0)
        self.assertEqual(size_qty(100_000, 0, 6000), 0)

    def test_exposure_ignores_bracket_legs_of_held_symbols(self):
        pos = [{"symbol": "TSLA", "market_value": "4000"}, {"symbol": "NVDA", "market_value": "-3000"}]
        orders = [{"symbol": "PLTR", "type": "limit", "qty": "10", "filled_qty": "4", "limit_price": "100"},
                  {"symbol": "TSLA", "type": "limit", "qty": "10", "filled_qty": "0", "limit_price": "420"}]
        self.assertAlmostEqual(exposure_of(pos, orders), 7_600)


class Levels(unittest.TestCase):
    def test_mid_offset(self):
        self.assertEqual(limit_price("buy", 99.9, 100.1, offset_bps=5), 100.05)
        self.assertEqual(limit_price("sell", 99.9, 100.1, offset_bps=5), 99.95)
        self.assertEqual(limit_price("buy", 0, 0, last=200, offset_bps=0), 200)
        self.assertEqual(limit_price("buy", 90, 110, last=100, offset_bps=0), 100)
        self.assertIsNone(limit_price("buy", None, None, None))

    def test_atr(self):
        bars = [{"h": 101, "l": 99, "c": 100}] * 20
        self.assertAlmostEqual(atr(bars), 2.0)
        self.assertIsNone(atr(bars[:1]))

    def test_bracket_levels(self):
        sl, tp, d = bracket_levels("buy", 100, 1.0)              # 1.5 x 1.0 = 1.5 -> within 0.4%..4%
        self.assertEqual((sl, tp, d), (98.5, 103.0, 1.5))
        sl, tp, d = bracket_levels("sell", 100, 1.0)
        self.assertEqual((sl, tp), (101.5, 97.0))
        self.assertEqual(bracket_levels("buy", 100, 0.01)[2], 0.4)   # min clamp 0.4%
        self.assertEqual(bracket_levels("buy", 100, 10)[2], 4.0)     # max clamp 4%
        self.assertEqual(bracket_levels("buy", 100, None)[2], 0.4)


class PaperGuard(unittest.TestCase):
    def setUp(self):
        self.c = AlpacaPaper("k", "s")

    def test_guard_url(self):
        guard_url("https://paper-api.alpaca.markets/v2/orders")
        guard_url("https://data.alpaca.markets/v2/stocks/bars?x=1")
        guard_url("https://data.alpaca.markets/v1beta1/news?symbols=TSLA")
        for bad in ["https://api.alpaca.markets/v2/orders", "http://paper-api.alpaca.markets/v2/orders",
                    "https://paper-api.alpaca.markets.evil.com/v2/orders", "https://paper-api.alpaca.markets@evil.com/v2/",
                    "https://broker-api.alpaca.markets/v1/", "https://data.alpaca.markets/v1beta3/crypto", None]:
            with self.assertRaises(PaperOnlyError, msg=bad):
                guard_url(bad)

    def test_constructor_rejects_live(self):
        with self.assertRaises(PaperOnlyError):
            AlpacaPaper("k", "s", trading_base="https://api.alpaca.markets/v2")
        with self.assertRaises(PaperOnlyError):
            AlpacaPaper("k", "s", data_base="https://example.com/v2")

    def test_request_never_opens_socket_for_live(self):
        with mock.patch("urllib.request.urlopen") as uo:
            with self.assertRaises(PaperOnlyError):
                self.c._request("POST", "https://api.alpaca.markets/v2/orders", body={"symbol": "TSLA"})
            uo.assert_not_called()

    def _capture(self, fn):
        seen = []
        def fake(req, timeout=None):
            seen.append((req.full_url, req.data))
            class R:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def read(s): return b'{"id":"x","status":"new"}'
            return R()
        with mock.patch("urllib.request.urlopen", fake):
            fn()
        return seen

    def test_orders_go_to_paper_host(self):
        seen = self._capture(lambda: self.c.place_limit_order("TSLA", 3, "buy", 100.123))
        self.assertTrue(seen[0][0].startswith("https://paper-api.alpaca.markets/v2/orders"))

    def test_bracket_order_body(self):
        import json
        seen = self._capture(lambda: self.c.place_bracket_order("TSLA", 3, "buy", 100, 103, 98.5))
        self.assertTrue(seen[0][0].startswith("https://paper-api.alpaca.markets/v2/orders"))
        b = json.loads(seen[0][1])
        self.assertEqual((b["order_class"], b["take_profit"]["limit_price"], b["stop_loss"]["stop_price"]), ("bracket", "103.00", "98.50"))
        with self.assertRaises(ValueError):
            self.c.place_bracket_order("TSLA", 3, "buy", 100, 98, 103)      # stop above entry for a buy
        with self.assertRaises(ValueError):
            self.c.place_bracket_order("TSLA", 3, "sell", 100, 103, 98.5)   # tp above entry for a sell

    def test_repr_hides_keys(self):
        self.assertNotIn("SECRET456", repr(AlpacaPaper("KEYID123", "SECRET456")))
        self.assertNotIn("KEYID123", repr(AlpacaPaper("KEYID123", "SECRET456")))

    def test_bad_order_args(self):
        with self.assertRaises(ValueError):
            self.c.place_limit_order("TSLA", 0, "buy", 100)
        with self.assertRaises(ValueError):
            self.c.place_limit_order("TSLA", 1, "short", 100)


CON = db.connect()   # one shared connection: separate ones would deadlock on uncommitted test inserts


def reset(con):
    con.rollback()
    for t in ("signals", "volume_spikes", "paper_trades", "decisions", "media", "news", "sec_filings", "earnings_events",
              "decision_returns", "random_baseline", "earnings"):
        con.execute(f"DELETE FROM {t}")
    con.commit()


class NewsGate(unittest.TestCase):
    def setUp(self):
        self.con = CON; reset(self.con)
        self.now = db.utcnow().replace(microsecond=0)

    def add_media(self, title, ago_min, tier=1, s=0.5, mid=None):
        self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,sentiment) VALUES (?,?,?,?,?,?,?)",
                         (mid or title[:12], "TSLA", db.iso(self.now - timedelta(minutes=ago_min)), "Reuters", tier, title, s))

    def test_fresh_novel_headline(self):
        self.add_media("Tesla recalls 200,000 vehicles over brake software", 20, s=-0.6)
        n = newsgate.hard_news(self.con, "TSLA", db.iso(self.now), "short")
        self.assertEqual(n["kind"], "media")

    def test_stale_repeat_rejected(self):
        self.add_media("Tesla and SpaceX explore merger, Musk says", 600, mid="old")
        self.add_media("Musk says Tesla and SpaceX explore merger", 15, mid="new")
        self.assertIsNone(newsgate.hard_news(self.con, "TSLA", db.iso(self.now), "long"))

    def test_too_old_and_contradicting(self):
        self.add_media("Tesla wins giant robotaxi contract in Texas", 120, s=0.8)       # outside 60 min
        self.assertIsNone(newsgate.hard_news(self.con, "TSLA", db.iso(self.now), "long"))
        self.add_media("Tesla shares plunge after guidance cut", 10, s=-0.8)
        self.assertIsNone(newsgate.hard_news(self.con, "TSLA", db.iso(self.now), "long"))  # contradicts long
        self.assertIsNotNone(newsgate.hard_news(self.con, "TSLA", db.iso(self.now), "short"))

    def test_sec(self):
        self.con.execute("INSERT INTO sec_filings(accession,ticker,form,filed_at,items) VALUES (?,?,?,?,?)",
                         ("a1", "TSLA", "8-K", db.iso(self.now - timedelta(minutes=5)), "2.02,9.01"))
        self.con.execute("INSERT INTO sec_filings(accession,ticker,form,filed_at,buy_value) VALUES (?,?,?,?,?)",
                         ("a2", "PLTR", "4", db.iso(self.now - timedelta(minutes=5)), 250000))
        self.assertEqual(newsgate.hard_news(self.con, "TSLA", db.iso(self.now), "short")["kind"], "sec")
        self.assertEqual(newsgate.hard_news(self.con, "PLTR", db.iso(self.now), "long")["kind"], "sec")
        self.assertIsNone(newsgate.hard_news(self.con, "PLTR", db.iso(self.now), "short"))   # insider buy != short


class NewsGateCompanyFocus(unittest.TestCase):
    """2026-10-21: a qualifying headline must name the company; generic recaps / basket lists never back a trade."""
    def setUp(self):
        self.con = CON; reset(self.con)
        self.now = db.utcnow().replace(microsecond=0)

    def add_bz(self, nid, ticker, headline, summary, symbols, ago_min=10, s=None):
        self.con.execute("INSERT INTO news(id,ticker,created_at,headline,summary,source,symbols,sentiment) VALUES (?,?,?,?,?,?,?,?)",
                         (nid, ticker, db.iso(self.now - timedelta(minutes=ago_min)), headline, summary, "benzinga", symbols, s))

    def test_cyclicals_sector_recap_rejected_for_nvda(self):
        self.add_bz("62499965", "NVDA", "Cyclicals Top Sectors In Wednesday Trading As Defensives Trail",
                    "Five sectors are higher and six are lower in Wednesday's regular session, with cyclical sectors holding "
                    "two of the top three positions alongside Technology.", "AAPL,AVGO,NVDA,QQQ,SPY,XLK")
        rej = []
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short", rejected=rej))
        self.assertEqual(len(rej), 1)

    def test_spacex_chip_stocks_rejected_for_tsla(self):
        h = "SpaceX Lifts All Boats: Chip Stocks Claw Back $274 Billion After a $1.6 Trillion Rout"
        sm = "SpaceX earnings lift chip stocks as 26 AI hardware names recoup $274 billion premarket. SOXX, SMCI in focus."
        for t in ("TSLA", "NVDA", "SMCI"):
            self.add_bz("62494484", t, h, sm, "META,NVDA,SMCI,SOXX,SPCX,TSLA")
        self.assertIsNone(newsgate.hard_news(self.con, "TSLA", db.iso(self.now), "long"))
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "long"))
        self.assertIsNone(newsgate.hard_news(self.con, "SMCI", db.iso(self.now), "long"))   # teaser mention, basket headline
        # the same basket headline would count for TSLA only if it named Tesla
        self.assertTrue(newsgate.about_company("TSLA", "SpaceX, Tesla Lift All Boats: Chip Stocks Claw Back $274 Billion")[0])

    def test_genuine_headlines_pass(self):
        self.add_bz("n1", "NVDA", "Amkor Secures $1.3 Billion NVIDIA Deal To Expand AI Packaging", "", "AMKR,NVDA")
        self.assertEqual(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "long")["ref"], "n1")
        self.add_bz("n2", "TSLA", "Elon Musk's Tesla-SpaceX Mega-Merger Could Create 'Most Unique Company'", "", "SPCX,TSLA")
        self.assertEqual(newsgate.hard_news(self.con, "TSLA", db.iso(self.now), "long")["ref"], "n2")
        self.add_bz("n3", "PLTR", "Watching $PLTR; Hearing SwingTrader Buys Stock", "", "PLTR")
        self.assertEqual(newsgate.hard_news(self.con, "PLTR", db.iso(self.now), "long")["ref"], "n3")
        ok = newsgate.about_company
        self.assertTrue(ok("META", "Facebook parent faces EU fine over ad targeting")[0])
        self.assertTrue(ok("META", "Meta's Zuckerberg unveils new glasses")[0])
        self.assertTrue(ok("NVDA", "Nokia Teams Up With Partners On Nvidia-Based AI-RAN")[0])
        self.assertTrue(ok("TSLA", "Elon Musk Hints at Chip Breakthroughs", "Tesla CEO said the AI5 chip ...")[0])   # teaser names it

    def test_generic_and_basket_headlines_rejected(self):
        ok = newsgate.about_company
        for h in ("12 Information Technology Stocks Moving In Wednesday's Pre-Market Session",
                  "Winnebago Posts Upbeat Q4 Earnings, Joins SpaceX, Super Micro Computer And Other Big Stocks Moving",
                  "5 Stocks To Watch This Week", "Stock Market Wrap: Dow Gains 300 Points", "Midday movers: Dell, Nvidia",
                  "Here Are Top 5 Initiations For Wednesday"):
            self.assertFalse(ok("SMCI" if "Super" in h else "NVDA", h)[0], h)
        self.assertFalse(ok("NVDA", "Nasdaq Climbs As Tech Rebounds", "Nvidia led gains")[0])      # index headline, teaser only
        self.assertTrue(ok("NVDA", "Nasdaq Climbs As Nvidia Surges 5% On China Deal")[0])          # index headline naming it
        self.assertFalse(ok("NVDA", "Why Is Dell Stock Surging Wednesday?", "Dell, which partners with Nvidia ...")[0])
        self.assertFalse(ok("NVDA", "Ken Griffin Calls AI a Bubble", "")[0])                       # symbols tag only
        self.assertFalse(ok("COIN", "Bitcoin coin prices jump")[0])                                 # ticker word is case-sensitive

    def test_media_rows_need_company_too(self):
        self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,sentiment) VALUES (?,?,?,?,?,?,?)",
                         ("m1", "NVDA", db.iso(self.now - timedelta(minutes=5)), "CNBC", 1, "Stock market today: Dow, S&P 500 slip", -0.3))
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short"))

    def test_sec_filing_still_counts_without_headline(self):
        self.add_bz("g1", "NVDA", "Cyclicals Top Sectors In Wednesday Trading As Defensives Trail", "", "NVDA,SPY")
        self.con.execute("INSERT INTO sec_filings(accession,ticker,form,filed_at,items) VALUES (?,?,?,?,?)",
                         ("a9", "NVDA", "8-K", db.iso(self.now - timedelta(minutes=5)), "8.01"))
        self.assertEqual(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short")["kind"], "sec")


class NewsGateSourceQuality(unittest.TestCase):
    """2026-10-22: NVDA short 17:04 Zurich was backed by a Moomoo community post via Google News. Social/community/UGC
    sources, post-style headlines and unvetted Google News outlets never back a trade; real outlets still do."""
    MOOMOO = "$NVIDIA (NVDA.US)$ haiyaaaa... uncle shaking\u00b2 hand look at it. itchy hand want to click buy."

    def setUp(self):
        self.con = CON; reset(self.con)
        self.now = db.utcnow().replace(microsecond=0)

    def add(self, mid, outlet, title, feed="gn_NVDA", tier=2, s=-0.3, ago=10, ticker="NVDA", url=None):
        self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,url,feed,sentiment) VALUES (?,?,?,?,?,?,?,?,?)",
                         (mid, ticker, db.iso(self.now - timedelta(minutes=ago)), outlet, tier, title, url, feed, s))

    def test_moomoo_post_rejected(self):
        self.add("f065c8e3e8117ec1", "Moomoo", self.MOOMOO, s=-0.2)   # stored as tier 2 by the old classifier
        rej = []
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short", rejected=rej))
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "long"))
        self.assertEqual(len(rej), 1)
        self.assertIn("T4", rej[0][1])

    def test_post_style_headline_rejected_even_from_real_outlet_and_benzinga(self):
        self.add("p1", "Reuters", self.MOOMOO, feed="reuters_gn", tier=1)
        self.con.execute("INSERT INTO news(id,ticker,created_at,headline,summary,source,symbols,sentiment) VALUES (?,?,?,?,?,?,?,?)",
                         ("b1", "NVDA", db.iso(self.now - timedelta(minutes=5)), "$NVDA $AMD lol gonna rip", "", "benzinga", "NVDA", 0.4))
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "long"))

    def test_social_sources_rejected(self):
        for i, o in enumerate(["Stocktwits", "TradingView", "Reddit", "Webull", "Tiger Brokers", "X", "YouTube", "\u5bcc\u9014\u725b\u725b",
                               "Futu", "Seeking Alpha Comments", "Some Investor Community"]):
            self.add(f"s{i}", o, f"Nvidia unveils new AI chip number {i}")
        self.add("y1", "Yahoo Finance", "Nvidia unveils a brand new robotics platform", feed="yahoo_NVDA",
                 url="https://stocktwits.com/news-articles/markets/equity/nvidia-x")             # Stocktwits copy in a direct feed
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short"))

    def test_unvetted_google_news_outlet_rejected(self):
        self.add("u1", "shattered.io", "Nvidia Stock Slides 3% After China Export Report")
        self.add("u2", "", "Nvidia shares slide on China export report")
        self.assertIsNone(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short"))

    def test_real_reuters_and_cnbc_headlines_still_pass(self):
        self.add("m1", "Moomoo", self.MOOMOO, s=-0.2, ago=12)
        self.add("r1", "Reuters", "Nvidia shares fall after US widens AI chip export curbs to China", feed="gn_tier1_NVDA", tier=1, ago=8)
        n = newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short")
        self.assertEqual((n["ref"], n["tier"]), ("r1", 1))
        reset(self.con)
        self.add("c1", "CNBC", "Nvidia stock drops as U.S. weighs new chip export limits", feed="cnbc_tech", tier=1)
        self.assertEqual(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short")["ref"], "c1")

    def test_whitelisted_tier2_outlets_pass(self):
        for i, o in enumerate(["Yahoo Finance", "MarketWatch", "Barron's", "Benzinga", "24/7 Wall St.", "Investopedia",
                               "Associated Press", "Investor's Business Daily", "Yahoo Finance UK"]):
            reset(self.con)
            self.add(f"w{i}", o, f"Nvidia shares slip as export curbs widen to {i} more countries")
            n = newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short")
            self.assertIsNotNone(n, o)
            self.assertEqual(n["tier"], 2, o)

    def test_moomoo_syndicated_dow_jones_copy_passes_as_tier2(self):
        self.add("d1", "Moomoo", "Nvidia Gets a China Boost but the Stock Is Still Falling -- Barrons.com")
        self.assertEqual(newsgate.hard_news(self.con, "NVDA", db.iso(self.now), "short")["tier"], 2)


class FakeClient:
    def __init__(self, positions=None, is_open=True, equity=100_000):
        self.placed, self.cancelled, self.closed = [], [], []
        self._pos = positions or []
        self.is_open, self.equity = is_open, equity
        self.orders_by_id = {}
    def account(self): return {"equity": str(self.equity), "cash": str(self.equity)}
    def clock(self): return {"is_open": self.is_open, "next_close": "2099-01-01T20:00:00Z"}
    def positions(self): return self._pos
    def orders(self, status="open", **kw): return [o for o in self.orders_by_id.values() if o["status"] == "new"]
    def latest_quote(self, s): return {"bp": 99.95, "ap": 100.05}
    def latest_trade(self, s): return {"p": 100.0}
    def asset(self, s): return {"shortable": True, "easy_to_borrow": True}
    def bars(self, s, tf, start=None, **kw): return {s: [{"h": 101, "l": 99, "c": 100}] * 20}   # ATR 2 -> stop 3%
    def calendar(self, a, b):
        import pandas as pd
        return [{"date": str(d.date())} for d in pd.bdate_range(a, b)]
    def get_order(self, oid): return self.orders_by_id[oid]
    def cancel_order(self, oid): self.cancelled.append(oid)
    def close_position(self, sym):
        self.closed.append(sym); self._pos = [p for p in self._pos if p["symbol"] != sym]
        o = {"id": f"c{len(self.closed)}", "symbol": sym, "status": "filled", "type": "market", "filled_qty": "49",
             "filled_avg_price": "100.5"}
        self.orders_by_id[o["id"]] = o
        return {**o, "status": "new"}
    def place_bracket_order(self, sym, qty, side, lp, tp, sl, client_order_id=None, **kw):
        oid = f"o{len(self.placed)+1}"
        o = {"id": oid, "symbol": sym, "qty": str(qty), "side": side, "status": "new", "type": "limit",
             "limit_price": str(lp), "tp": tp, "sl": sl, "filled_qty": "0"}
        self.orders_by_id[oid] = o; self.placed.append(o); return o


class TraderV2(unittest.TestCase):
    def setUp(self):
        self.con = CON; reset(self.con)
        self.now = db.utcnow().replace(microsecond=0)

    def spike(self, direction="up", ticker="TSLA"):
        self.con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,flagged)
                            VALUES (?,?,?,?,?,?,?,1)""", (db.iso(self.now - timedelta(minutes=10)), ticker, "5Min",
                                                         db.iso(self.now - timedelta(minutes=4)), 4.2,
                                                         0.6 if direction == "up" else -0.6, direction))

    def shock(self, d="long"):
        for variant, dd in (("follow", d), ("fade", "short" if d == "long" else "long")):
            self.con.execute("INSERT INTO signals(ts,ticker,type,variant,zscore,volume_ratio,direction,price) VALUES (?,?,?,?,?,?,?,?)",
                             (db.iso(self.now - timedelta(minutes=5)), "TSLA", "sentiment_shock", variant, 2.6, 4.0, dd, 100))

    def news(self, title="Tesla raises delivery guidance after record China sales", s=0.6):
        self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,sentiment) VALUES (?,?,?,?,?,?,?)",
                         ("n1", "TSLA", db.iso(self.now - timedelta(minutes=20)), "Reuters", 1, title, s))

    def run_t(self, fc, live=True, now=None):
        Trader(fc, self.con, {}, live=live, now=now or self.now, kill_path="/nonexistent/KILL", core=["TSLA", "NVDA", "PLTR"]).run()
        self.con.commit()

    def rules(self):
        return {(r["rule"], r["trigger_type"]): (r["action"], r["direction"]) for r in self.con.execute("SELECT * FROM decisions")}

    def test_spike_with_fresh_news_trades_bracket(self):
        self.spike("up"); self.news(); self.con.commit()
        fc = FakeClient(); self.run_t(fc)
        self.assertEqual(len(fc.placed), 1)
        o = fc.placed[0]
        # entry 100.05, ATR 2 x1.5 = 3.0 -> stop 97.05, tp 106.05; risk $500/3 = 166 sh but 5% cap -> 49
        self.assertEqual((o["side"], o["qty"], o["limit_price"], o["sl"], o["tp"]), ("buy", "49", "100.05", 97.05, 106.05))
        self.assertEqual(self.rules()[("v2_news_momentum", "volume_spike")], ("trade", "long"))
        pt = self.con.execute("SELECT * FROM paper_trades").fetchone()
        self.assertEqual(pt["alpaca_order_id"], "o1")
        self.run_t(fc)                                   # idempotent
        self.assertEqual(len(fc.placed), 1)

    def test_attention_only_is_fade_hypothetical(self):
        self.spike("up"); self.con.commit()
        fc = FakeClient(); self.run_t(fc)
        self.assertEqual(fc.placed, [])
        self.assertEqual(self.rules()[("v2_attention_fade", "volume_spike")], ("hypothetical", "short"))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 0)

    def test_stale_news_does_not_count(self):
        self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,sentiment) VALUES (?,?,?,?,?,?,?)",
                         ("old", "TSLA", db.iso(self.now - timedelta(hours=8)), "CNBC", 1,
                          "Tesla raises delivery guidance after record China sales", 0.6))
        self.spike("up"); self.news(); self.con.commit()          # same story again 20 min ago
        fc = FakeClient(); self.run_t(fc)
        self.assertEqual(fc.placed, [])

    def test_v1_logged_for_comparison(self):
        self.shock("long"); self.spike("up"); self.con.commit()
        fc = FakeClient(); self.run_t(fc)
        r = self.rules()
        self.assertEqual(r[("v1_follow_spike", "sentiment_shock")][0], "hypothetical")
        self.assertIn("WOULD trade", self.con.execute("SELECT reason FROM decisions WHERE rule='v1_follow_spike'").fetchone()[0])
        self.assertEqual(fc.placed, [])                  # no news -> v2 does not trade

    def test_dry_run_writes_nothing(self):
        self.spike("up"); self.news(); self.con.commit()
        fc = FakeClient(); self.run_t(fc, live=False)
        self.assertEqual(fc.placed, [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_market_closed_and_cap(self):
        self.spike("up"); self.news(); self.con.commit()
        fc = FakeClient(is_open=False); self.run_t(fc)
        self.assertEqual(fc.placed, [])
        self.assertIn("market closed", self.con.execute("SELECT reason FROM decisions").fetchone()[0])
        reset(self.con); self.spike("up"); self.news(); self.con.commit()
        fc = FakeClient(positions=[{"symbol": "NVDA", "qty": "80", "market_value": "18000", "avg_entry_price": "225", "unrealized_plpc": "0"}])
        self.run_t(fc)
        self.assertEqual(fc.placed[0]["qty"], "19")

    def test_time_exit_closes_position(self):
        self.spike("up"); self.news(); self.con.commit()
        fc = FakeClient(); self.run_t(fc)
        fc.orders_by_id["o1"].update(status="filled", filled_qty="49", filled_avg_price="100.02")
        fc._pos = [{"symbol": "TSLA", "qty": "49", "market_value": "4900", "avg_entry_price": "100.02", "unrealized_plpc": "0.001"}]
        self.run_t(fc, now=self.now + timedelta(minutes=61))
        self.assertEqual(fc.closed, ["TSLA"])
        self.run_t(fc, now=self.now + timedelta(minutes=66))   # position gone -> entry closed
        self.assertIsNotNone(self.con.execute("SELECT closed_at FROM paper_trades WHERE purpose='entry'").fetchone()[0])

    def _event(self, agree, rx_offset_days=0):
        import pandas as pd
        rx = (pd.Timestamp(self.now.astimezone(db.ZoneInfo("America/New_York")).date()) - pd.offsets.BDay(rx_offset_days)).date()
        self.con.execute("""INSERT INTO earnings_events(ticker, report_date, time_of_day, eps_actual, eps_estimate, surprise_pct,
                            reaction_date, gap_pct, news_sent, direction, agree) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         ("TSLA", str(rx - timedelta(days=1)), "after-hours", 0.55, 0.45, 22.2, str(rx), 3.1, 0.3, "long", agree))

    def test_earnings_pause_blocks_sentiment_trade(self):
        self._event(agree=0)
        self.spike("up"); self.news(); self.con.commit()
        fc = FakeClient(); self.run_t(fc)
        self.assertEqual(fc.placed, [])
        self.assertIn("earnings_pause", self.con.execute("SELECT reason FROM decisions").fetchone()[0])

    def test_earnings_drift_trades_when_agree(self):
        self._event(agree=1, rx_offset_days=1)          # today = reaction day 2 -> no pause, drift allowed
        self.con.commit()
        et = db.ZoneInfo("America/New_York")
        at = self.now.astimezone(et).replace(hour=11, minute=0).astimezone(db.timezone.utc)
        fc = FakeClient(); self.run_t(fc, now=at)
        self.assertEqual(len(fc.placed), 1)
        self.assertEqual(self.rules()[("earnings_drift", "earnings")], ("trade", "long"))
        due = self.con.execute("SELECT exit_due FROM paper_trades").fetchone()[0]
        self.assertEqual(db.parse_ts(due).astimezone(et).strftime("%H:%M"), "15:45")


class ExitEnforcement(unittest.TestCase):
    """2026-10-21 incident: TSLA time exit due 11:08 ET was missed because exits only ran inside the 30-min routine
    (last run 13 s before exit_due). Exits now run from the 5-min collector (--exits-only) and at each exit_due;
    plus the hard rule: flat before 15:45 ET when the ticker reports after today's close."""
    ET = db.ZoneInfo("America/New_York")

    def setUp(self):
        self.con = CON; reset(self.con)
        # a fixed trading day, 11:00 ET
        self.day = db.datetime(2026, 10, 21, 11, 0, tzinfo=self.ET).astimezone(db.timezone.utc)

    def at(self, hh, mm, ss=0):
        return db.datetime(2026, 10, 21, hh, mm, ss, tzinfo=self.ET).astimezone(db.timezone.utc)

    def entry(self, ticker="TSLA", side="buy", qty=12, due=None, ts=None):
        cur = self.con.execute("""INSERT INTO paper_trades(ts,ticker,side,qty,limit_price,status,alpaca_order_id,purpose,
                                  filled_qty,filled_avg_price,exit_due,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (db.iso(ts or self.at(10, 8, 8)), ticker, side, qty, 386.0, "filled", "e1", "entry", qty,
                                385.955, db.iso(due) if due else None, "live-paper"))
        self.con.commit(); return cur.lastrowid

    def earnings(self, ticker="TSLA", tod="after-hours", date="2026-10-21"):
        self.con.execute("INSERT INTO earnings(ticker,date,time_of_day) VALUES (?,?,?)", (ticker, date, tod)); self.con.commit()

    def fc(self, positions):
        fc = FakeClient(positions=positions)
        fc.orders_by_id["e1"] = {"id": "e1", "symbol": "TSLA", "status": "filled", "filled_qty": "12", "filled_avg_price": "385.955"}
        return fc

    def pos(self, sym="TSLA", qty="12"):
        return {"symbol": sym, "qty": qty, "market_value": "4600", "avg_entry_price": "385.955", "unrealized_plpc": "0"}

    def trader(self, fc, now, flat=False):
        return Trader(fc, self.con, {}, live=True, now=now, kill_path="/nonexistent/KILL", core=["TSLA", "NVDA", "PLTR"],
                      flat_before_close=flat)

    def test_exits_only_fires_time_exit_and_never_enters(self):
        self.entry(due=self.at(11, 8, 8))
        TraderV2.spike(type("S", (), {"con": self.con, "now": self.at(11, 9)})(), "up"); self.con.commit()
        fc = self.fc([self.pos()])
        self.trader(fc, self.at(11, 8, 0)).run_exits(); self.con.commit()
        self.assertEqual(fc.closed, [])                         # 8 s early: not yet
        self.trader(fc, self.at(11, 8, 11)).run_exits(); self.con.commit()
        self.assertEqual(fc.closed, ["TSLA"])
        self.assertEqual(fc.placed, [])                          # exits-only never opens trades
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)
        ex = self.con.execute("SELECT * FROM paper_trades WHERE purpose='exit'").fetchone()
        self.assertEqual((ex["side"], ex["qty"], ex["notes"]), ("sell", 12, "time exit"))
        self.trader(fc, self.at(11, 9)).run_exits(); self.con.commit()
        self.assertEqual(fc.closed, ["TSLA"])                   # no double close

    def test_short_time_exit_buys_back(self):
        self.entry("NVDA", "sell", 22, due=self.at(12, 7, 55), ts=self.at(11, 7, 55))
        fc = self.fc([self.pos("NVDA", "-22")])
        self.trader(fc, self.at(12, 8, 0)).run_exits(); self.con.commit()
        self.assertEqual(fc.closed, ["NVDA"])
        self.assertEqual(self.con.execute("SELECT side FROM paper_trades WHERE purpose='exit'").fetchone()[0], "buy")

    def test_earnings_after_close_flattens_before_1545(self):
        self.earnings("TSLA", "after-hours")
        self.entry(due=self.at(15, 59))                          # exit_due later than the cut
        fc = self.fc([self.pos()])
        self.trader(fc, self.at(15, 39)).run_exits(); self.con.commit()
        self.assertEqual(fc.closed, [])
        self.trader(fc, self.at(15, 40, 5)).run_exits(); self.con.commit()
        self.assertEqual(fc.closed, ["TSLA"])
        self.assertIn("earnings", self.con.execute("SELECT notes FROM paper_trades WHERE purpose='exit'").fetchone()[0])

    def test_earnings_rule_covers_untracked_and_unknown_time_not_premarket(self):
        self.earnings("TSLA", None)                              # unknown time -> treated as after close
        self.earnings("PLTR", "pre-market")                      # pre-market report: already out, no flatten
        fc = FakeClient(positions=[self.pos("TSLA"), self.pos("PLTR", "5")])
        self.trader(fc, self.at(15, 41)).run_exits(); self.con.commit()
        self.assertEqual(fc.closed, ["TSLA"])
        self.assertIsNone(self.con.execute("SELECT parent_id FROM paper_trades WHERE purpose='exit'").fetchone()[0])

    def test_earnings_other_day_ignored(self):
        self.earnings("TSLA", "after-hours", "2026-10-22")
        fc = FakeClient(positions=[self.pos("TSLA")])
        self.trader(fc, self.at(15, 44)).run_exits()
        self.assertEqual(fc.closed, [])

    def test_entry_exit_due_capped_and_blocked_after_cut(self):
        self.earnings("TSLA", "after-hours")
        # 14:50 ET: trades, but its 60-min exit (15:50) would fall into the report -> capped to 15:40.
        # 15:00 ET and later: no new entries at all in a ticker reporting after today's close.
        for when, expect_trade in ((self.at(14, 50), True), (self.at(14, 59, 59), True), (self.at(15, 0), False),
                                   (self.at(15, 20), False), (self.at(15, 42), False)):
            reset(self.con); self.earnings("TSLA", "after-hours")
            self.con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,flagged)
                                VALUES (?,?,?,?,?,?,?,1)""", (db.iso(when - timedelta(minutes=10)), "TSLA", "5Min",
                                                             db.iso(when - timedelta(minutes=4)), 4.2, 0.6, "up"))
            self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,sentiment) VALUES (?,?,?,?,?,?,?)",
                             ("n1", "TSLA", db.iso(when - timedelta(minutes=20)), "Reuters", 1,
                              "Tesla raises delivery guidance after record China sales", 0.6))
            self.con.commit()
            fc = FakeClient(); self.trader(fc, when).run(); self.con.commit()
            self.assertEqual(len(fc.placed), 1 if expect_trade else 0)
            if expect_trade:
                due = self.con.execute("SELECT exit_due FROM paper_trades").fetchone()[0]
                self.assertEqual(db.parse_ts(due).astimezone(self.ET).strftime("%H:%M"), "15:40")
            else:
                self.assertIn("earnings after close", self.con.execute(
                    "SELECT reason FROM decisions WHERE action='hypothetical' AND rule='v2_news_momentum'").fetchone()[0])

    def test_entry_cutoff_only_for_earnings_ticker(self):
        # NVDA has no report today: still tradable at 15:20 ET (its own 60-min exit is enforced by flat-before-close)
        self.earnings("TSLA", "after-hours")
        when = self.at(15, 20)
        self.con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,flagged)
                            VALUES (?,?,?,?,?,?,?,1)""", (db.iso(when - timedelta(minutes=10)), "NVDA", "5Min",
                                                         db.iso(when - timedelta(minutes=4)), 4.2, 0.6, "up"))
        self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,sentiment) VALUES (?,?,?,?,?,?,?)",
                         ("n2", "NVDA", db.iso(when - timedelta(minutes=20)), "Reuters", 1,
                          "Nvidia raises guidance after record data center sales", 0.6))
        self.con.commit()
        fc = FakeClient(); self.trader(fc, when).run(); self.con.commit()
        self.assertEqual(len(fc.placed), 1)

    def test_next_exit_due(self):
        import paper_trader
        self.assertIsNone(paper_trader.next_exit_due(self.con, self.at(11, 0)))
        self.entry("NVDA", "sell", 22, due=self.at(12, 7, 55))
        self.assertEqual(paper_trader.next_exit_due(self.con, self.at(11, 0)), self.at(12, 7, 55))
        self.entry("TSLA", due=self.at(15, 59)); self.earnings("TSLA")
        self.con.execute("UPDATE paper_trades SET exit_due=NULL WHERE ticker='NVDA'"); self.con.commit()
        self.assertEqual(paper_trader.next_exit_due(self.con, self.at(11, 0)), self.at(15, 40))

    def test_lock_is_exclusive(self):
        import paper_trader
        p = os.path.join(tempfile.mkdtemp(), "pt.lock")
        a = paper_trader.lock_file(p)
        self.assertIsNotNone(a)
        self.assertIsNone(paper_trader.lock_file(p))
        a.close()
        self.assertIsNotNone(paper_trader.lock_file(p))

    def test_collector_runs_exits_first_on_open_tick(self):
        import collector
        calls = []
        with mock.patch.object(collector, "exits_step", lambda: calls.append("exits")), \
             mock.patch.object(collector, "step", lambda a, timeout=240: calls.append(a[0])), \
             mock.patch.object(collector, "publish", lambda: None), \
             mock.patch.object(collector, "swiss_enabled", lambda: False), \
             mock.patch.object(collector, "HEART", os.path.join(tempfile.mkdtemp(), "hb")):
            collector.tick(1, True)
            self.assertEqual(calls[0], "exits")
            calls.clear(); collector.tick(1, False)
            self.assertNotIn("exits", calls)
        self.assertIn("--exits-only", collector.EXITS)
        self.assertNotIn("--live", [a for a in collector.EXITS if a != "--live-paper"])


class Evaluation(unittest.TestCase):
    def test_slippage_and_random_baseline(self):
        con = CON; reset(con)
        ts = db.iso(db.utcnow() - timedelta(days=3))
        con.execute("INSERT INTO decisions(ts,ticker,rule,trigger_type,trigger_id,direction,action) VALUES (?,?,?,?,?,?,?)",
                    (ts, "TSLA", "v2_news_momentum", "volume_spike", 1, "short", "trade"))
        con.commit()
        rets = {"TSLA": -0.010, "SPY": 0.002, "QQQ": 0.004}
        with mock.patch.object(evaluate, "window_ret", lambda sym, a, b, iv: (100, 100 * (1 + rets[sym]), rets[sym])):
            n = evaluate.evaluate_decisions(con, db.utcnow(), {"slippage_bps": 3, "random_entries_per_decision": 3})
        self.assertGreaterEqual(n, 2)
        r = con.execute("SELECT * FROM decision_returns WHERE horizon='1h'").fetchone()
        self.assertAlmostEqual(r["net_signed"], 0.010 - 0.0006)              # short gains 1% minus 2 x 3 bps
        self.assertAlmostEqual(r["excess_spy"], 0.012 - 0.0006)
        self.assertAlmostEqual(r["excess_qqq"], 0.014 - 0.0006)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM random_baseline WHERE horizon='1h'").fetchone()[0], 3)
        s = evaluate.decision_summary(con)
        self.assertTrue(any(x["rule"] == "v2_news_momentum" and x["rnd"] is not None for x in s))


if __name__ == "__main__":
    unittest.main(verbosity=2)
