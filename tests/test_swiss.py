#!/usr/bin/env python3
"""Swiss observe-only arm: config, hard no-trade guarantee (paper_trader + alpaca_client), SIX move math, headline
rules, feed handling (fake feeds, blocked feed skipped), observation rules, outcome evaluation with 3 bps slippage,
X off by default. No network. Run: .venv/bin/python -m unittest tests/test_swiss.py -v"""
import os, re, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("STOCK_SENT_DB", os.path.join(tempfile.mkdtemp(), "sw.db"))
import db, swiss, swiss_news, collector
from alpaca_client import AlpacaPaper, ObserveOnlyError
from paper_trader import Trader, ObserveOnlyRefused, assert_tradable

CONF = db.load_config()
SO = CONF["swiss_observe"]
SWISS = ["NVS", "UBS", "LOGI", "ONON", "ALC"]


def fresh_con():
    return db.connect(os.path.join(tempfile.mkdtemp(), "s.db"))


class Config(unittest.TestCase):
    def test_watchlist_and_flags(self):
        self.assertTrue(SO["observe_only"])
        self.assertEqual(sorted(SO["names"]), sorted(SWISS))
        m = {t: v["six"] for t, v in SO["names"].items()}
        self.assertEqual(m, {"NVS": "NOVN.SW", "UBS": "UBSG.SW", "LOGI": "LOGN.SW", "ONON": None, "ALC": "ALC.SW"})
        for t in SWISS:
            self.assertTrue(SO["names"][t]["observe_only"])
        self.assertIn("On Running", SO["names"]["ONON"]["aliases"])
        self.assertFalse(SO["x_enabled"])                       # X off by default

    def test_not_in_trading_watchlists(self):
        for t in SWISS:
            self.assertNotIn(t, CONF["watchlist_core"] + CONF["watchlist_extra"])

    def test_observe_only_symbols(self):
        s = db.observe_only_symbols()
        self.assertTrue(set(SWISS) <= s)
        self.assertNotIn("TSLA", s)
        # even if someone flips a per-name flag, the section-level flag keeps it blocked
        c = {"swiss_observe": {"observe_only": True, "names": {"NVS": {"observe_only": False}}}}
        self.assertIn("NVS", db.observe_only_symbols(c))


class FakeClient:
    def __init__(self):
        self.placed = []
    def account(self): return {"equity": "100000", "cash": "100000"}
    def clock(self): return {"is_open": True, "next_close": "2099-01-01T20:00:00Z"}
    def positions(self): return []
    def orders(self, status="open", **kw): return []
    def latest_quote(self, s): return {"bp": 99.95, "ap": 100.05}
    def latest_trade(self, s): return {"p": 100.0}
    def asset(self, s): return {"shortable": True, "easy_to_borrow": True}
    def bars(self, s, tf, start=None, **kw): return {s: [{"h": 101, "l": 99, "c": 100}] * 20}
    def calendar(self, a, b): return []
    def place_bracket_order(self, *a, **k):
        self.placed.append((a, k)); return {"id": "o1", "status": "new"}
    def place_limit_order(self, *a, **k):
        self.placed.append((a, k)); return {"id": "o1", "status": "new"}


class NoTradeGuarantee(unittest.TestCase):
    def setUp(self):
        self.con = fresh_con()
        self.now = db.utcnow().replace(microsecond=0)

    def test_assert_tradable(self):
        for t in SWISS:
            with self.assertRaises(ObserveOnlyRefused):
                assert_tradable(t, db.observe_only_symbols())
        assert_tradable("TSLA", db.observe_only_symbols())

    def test_trader_refuses_swiss_even_if_listed_as_core(self):
        # flagged spike + fresh novel tier-1 news + composite follow signal on a Swiss name: would trade a US name
        for t in ("NVS", "UBS"):
            self.con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,flagged)
                                VALUES (?,?,?,?,?,?,?,1)""", (db.iso(self.now - timedelta(minutes=10)), t, "5Min",
                                                             db.iso(self.now - timedelta(minutes=4)), 5.0, 0.9, "up"))
            self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,sentiment) VALUES (?,?,?,?,?,?,?)",
                             ("m" + t, t, db.iso(self.now - timedelta(minutes=15)), "Reuters", 1, f"{t} wins approval for new drug", 0.7))
            self.con.execute("INSERT INTO signals(ts,ticker,type,variant,zscore,volume_ratio,direction,price) VALUES (?,?,?,?,?,?,?,?)",
                             (db.iso(self.now - timedelta(minutes=5)), t, "composite", "follow", 2.5, 4.0, "long", 100))
        self.con.commit()
        fc = FakeClient()
        tr = Trader(fc, self.con, {}, live=True, now=self.now, kill_path="/nonexistent/KILL", core=["NVS", "UBS", "TSLA"])
        tr.run()
        self.assertEqual(fc.placed, [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM decisions WHERE ticker IN ('NVS','UBS')").fetchone()[0], 0)

    def test_place_raises_before_client(self):
        fc = FakeClient()
        tr = Trader(fc, self.con, {}, live=True, now=self.now, kill_path="/nonexistent/KILL", core=["TSLA"])
        with self.assertRaises(ObserveOnlyRefused):
            tr.place("LOGI", "buy", 10, 100, 98, 104, None, None, "test")
        self.assertEqual(fc.placed, [])

    def test_us_name_still_trades(self):
        # control: the same setup on TSLA still reaches the client (guard is specific, not a global kill)
        self.con.execute("""INSERT INTO volume_spikes(ts,ticker,timeframe,detected_at,ratio,ret_pct,direction,flagged)
                            VALUES (?,?,?,?,?,?,?,1)""", (db.iso(self.now - timedelta(minutes=10)), "TSLA", "5Min",
                                                         db.iso(self.now - timedelta(minutes=4)), 5.0, 0.9, "up"))
        self.con.execute("INSERT INTO media(id,ticker,created_at,outlet,tier,title,sentiment) VALUES (?,?,?,?,?,?,?)",
                         ("mT", "TSLA", db.iso(self.now - timedelta(minutes=15)), "Reuters", 1, "Tesla raises delivery guidance", 0.7))
        self.con.commit()
        fc = FakeClient()
        Trader(fc, self.con, {}, live=True, now=self.now, kill_path="/nonexistent/KILL", core=["TSLA"]).run()
        self.assertEqual(len(fc.placed), 1)

    def test_alpaca_client_refuses_without_socket(self):
        c = AlpacaPaper("k", "s")
        with mock.patch("urllib.request.urlopen") as uo:
            for t in SWISS:
                with self.assertRaises(ObserveOnlyError):
                    c.place_bracket_order(t, 1, "buy", 100, 103, 98)
                with self.assertRaises(ObserveOnlyError):
                    c.place_limit_order(t.lower(), 1, "buy", 100)
            uo.assert_not_called()

    def test_swiss_modules_have_no_order_or_x_code(self):
        for f in ("swiss.py", "swiss_news.py"):
            src = open(os.path.join(HERE, f)).read()
            for bad in ("place_bracket_order", "place_limit_order", "close_position", "cancel_order", "import paper_trader",
                        "from paper_trader",
                        "api.x.com", "api.twitter.com", "x.com/i/api"):
                self.assertNotIn(bad, src, f"{bad} in {f}")


class SixMove(unittest.TestCase):
    def test_compute_move(self):
        chg, imp, gap = swiss.compute_move(126.66, 126.50, 1.212, 153.64)
        self.assertAlmostEqual(chg, (126.66 / 126.50 - 1) * 100)
        self.assertAlmostEqual(imp, 126.66 * 1.212)
        self.assertAlmostEqual(gap, (126.66 * 1.212 / 153.64 - 1) * 100)
        self.assertEqual(swiss.compute_move(None, 1, 1, 1), (None, None, None))

    def test_windows(self):
        z = lambda hm: datetime.strptime(f"2026-10-21 {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=swiss.ZRH)
        self.assertEqual(swiss.window_of(z("07:00")), "pre_0730_adhoc")
        self.assertEqual(swiss.window_of(z("10:00")), "six_session")
        self.assertEqual(swiss.window_of(z("18:00")), "post_1740_us_only")
        self.assertEqual(swiss.six_state(z("09:00")), "open")
        self.assertEqual(swiss.six_state(z("17:30")), "closed")
        self.assertTrue(collector.swiss_window(z("07:00")))
        self.assertFalse(collector.swiss_window(z("20:00")))

    def test_next_us_open(self):
        fri_eve = datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc)           # Fri 17:00 ET
        self.assertEqual(swiss.next_us_open(fri_eve).astimezone(swiss.ET).strftime("%a %H:%M"), "Mon 09:30")
        pre = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)               # Wed 08:00 ET
        self.assertEqual(swiss.next_us_open(pre).astimezone(swiss.ET).strftime("%a %d %H:%M"), "Wed 21 09:30")


def rss(items):
    body = "".join(f"<item><title>{t}</title><link>https://ex.com/{i}</link><description>{d}</description>"
                   f"<pubDate>{format_datetime(ts)}</pubDate></item>" for i, (t, d, ts) in enumerate(items))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'


class Headlines(unittest.TestCase):
    def test_relevance_filters(self):
        n = SO["names"]
        self.assertFalse(swiss_news.relevant("UBS", "UBS hebt Kursziel für Kroger auf 66 USD an", SO, n))
        self.assertFalse(swiss_news.relevant("UBS", "Starbucks' Turnaround dürfte den Umsatz stützen, so UBS", SO, n))
        self.assertFalse(swiss_news.relevant("UBS", "Nvidia Turbo L 177.07 open end (UBS) | Kurs, Chart, Analysen", SO, n))
        self.assertTrue(swiss_news.relevant("UBS", "UBS Aktie: Bundesgericht bestätigt Bonus-Streichung", SO, n))
        self.assertTrue(swiss_news.relevant("UBS", "UBS will Deutsche Bank-CEO", SO, n))
        self.assertTrue(swiss_news.relevant("NVS", "UBS bestätigt Halten für die Novartis-Aktie", SO, n))
        self.assertFalse(swiss_news.relevant("ALC", "Alcon Aktie News: Alcon steigt am Mittwochmittag", SO, n))

    def test_other_company_is_subject_rejected(self):
        # 2026-10-21 false positive: UBS the broker commenting on FMC created a UBS short observation
        n = SO["names"]
        for h in ["FMC-Aktie leichter: UBS warnt vor Gefahr durch US-Reformpapier",
                  "SAP Aktie: UBS und Goldman sehen robuste Cloud-Nachfrage",
                  "Siemens Energy-Aktie: UBS bestätigt Kaufen",
                  "Kroger stock: UBS raises target",
                  "UBS senkt Ziel für Nestlé-Aktie auf 90 Franken",
                  "UBS stuft Roche auf Buy hoch",
                  "UBS warnt vor Risiken für die Nike-Aktie",
                  "UBS streicht Kaufempfehlung für Lonza",
                  "UBS sees upside for Nvidia as AI demand grows",
                  "UBS raises price target on Apple to $280",
                  "UBS Cuts Tesla To Sell",
                  "UBS initiates coverage on Palantir with Neutral",
                  "UBS Downgrades Deere",
                  "Nike shares slip after UBS downgrade"]:
            self.assertFalse(swiss_news.relevant("UBS", h, SO, n), h)
            self.assertTrue(swiss_news.other_subject("UBS", h, n) or not swiss_news.relevant("UBS", h, SO, n), h)

    def test_genuine_self_headlines_still_pass(self):
        n = SO["names"]
        for t, h in [("UBS", "UBS Aktie: Bundesgericht bestätigt Bonus-Streichung"),
                     ("UBS", "UBS will Deutsche Bank-CEO"),
                     ("UBS", "UBS Group: AlphaValue/Baader Europe hält an seiner Add-Empfehlung fest und senkt das Kursziel"),
                     ("UBS", "UBS warnt vor Gewinnrückgang im vierten Quartal"),
                     ("UBS", "UBS warnt vor Gefahr durch US-Reformpapier"),          # no other company named
                     ("UBS", "UBS streicht 3000 Stellen in der Schweiz"),
                     ("UBS", "UBS meldet Quartalsgewinn über Erwartungen"),
                     ("UBS", "JPMorgan hebt Kursziel für UBS auf 35 Franken"),
                     ("UBS", "UBS shares rise after strong quarter"),
                     ("UBS", "UBS beats Q3 profit estimates on wealth management strength"),
                     ("NVS", "UBS bestätigt Halten für die Novartis-Aktie"),          # Novartis is the rated stock
                     ("NVS", "Novartis senkt Prognose für Entresto"),
                     ("LOGI", "Logitech cuts outlook on weak demand"),
                     ("LOGI", "Logitech raises outlook for fiscal year"),
                     ("ALC", "Alcon bleibt auf 'Buy' - Berenberg verweist auf Dynamik bei Produkteinführungen"),
                     ("ALC", "Alcon reiterates guidance for 2026"),
                     ("ONON", "On Holding shares jump after strong quarter")]:
            self.assertTrue(swiss_news.relevant(t, h, SO, n), f"{t}: {h}")
            self.assertFalse(swiss_news.other_subject(t, h, n), f"{t}: {h}")

    def test_marketscreener_other_company_colon_rejected(self):
        n = SO["names"]
        for h in ["Netflix, Inc. : UBS bleibt bei seiner Kaufempfehlung",
                  "Netflix, Inc. : UBS belässt das Rating auf Buy",
                  "Siemens AG : UBS bleibt bei Neutral",
                  "Shell plc : UBS senkt das Kursziel",
                  "Microsoft Corp. : UBS bleibt bei seiner Empfehlung",
                  "Adidas : UBS bleibt bei Sell"]:
            self.assertTrue(swiss_news.other_subject("UBS", h, n), h)
            self.assertFalse(swiss_news.relevant("UBS", h, SO, n), h)
        for t, h in [("UBS", "UBS Group: AlphaValue/Baader Europe hält an seiner Add-Empfehlung fest und senkt das Kursziel"),
                     ("UBS", "UBS Aktie: Bundesgericht bestätigt Bonus-Streichung"),
                     ("UBS", "UBS Group AG : UBS bleibt bei Rückkaufprogramm"),
                     ("ALC", "Alcon bleibt auf 'Buy' - Berenberg verweist auf Dynamik bei Produkteinführungen")]:
            self.assertFalse(swiss_news.other_subject(t, h, n), f"{t}: {h}")

    def test_broker_reiteration_verbs_rejected(self):
        # 2026-10-21 miss: 'UBS bekräftigt Kaufempfehlung für Equifax-Aktie' created a UBS observation
        n = SO["names"]
        for h in ["UBS bekräftigt Kaufempfehlung für Equifax-Aktie nach Zahlen",
                  "UBS bestätigt Kaufempfehlung für Equifax",
                  "UBS empfiehlt Equifax zum Kauf",
                  "UBS nimmt Abdeckung von Equifax mit Buy auf",
                  "UBS startet Abdeckung für Equifax mit Neutral",
                  "UBS bleibt bei Kaufempfehlung für Equifax",
                  "UBS reiterates Buy on Equifax",
                  "UBS reaffirms Equifax rating",
                  "UBS maintains Neutral on Equifax",
                  "UBS initiates Equifax at Buy",
                  "UBS keeps Buy rating on Equifax",
                  "UBS sieht Chancen für Equifax-Aktie",
                  "UBS positive for Equifax stock"]:
            self.assertTrue(swiss_news.other_subject("UBS", h, n), h)
            self.assertFalse(swiss_news.relevant("UBS", h, SO, n), h)
        for t, h in [("UBS", "UBS bekräftigt Jahresziele"), ("UBS", "UBS bestätigt Stellenabbau in der Schweiz"),
                     ("NVS", "UBS bekräftigt Kaufempfehlung für Novartis-Aktie"),
                     ("UBS", "UBS keeps dividend unchanged")]:
            self.assertFalse(swiss_news.other_subject(t, h, n), f"{t}: {h}")

    def test_subject_check_generic_for_other_names(self):
        n = SO["names"]
        self.assertFalse(swiss_news.relevant("NVS", "Roche-Aktie fester: Novartis-Chef sieht Chancen im Markt", SO, n))
        self.assertFalse(swiss_news.relevant("LOGI", "Apple-Aktie: Logitech warnt vor Zöllen für Apple-Zubehör", SO, n))

    def test_fetch_drops_fmc_headline(self):
        import feedparser
        con = fresh_con()
        now = db.utcnow().replace(microsecond=0)
        t = now - timedelta(minutes=20)
        body = rss([("FMC-Aktie leichter: UBS warnt vor Gefahr durch US-Reformpapier - finanzen.at", "", t),
                    ("UBS Aktie: Bundesgericht bestätigt Bonus-Streichung - cash.ch", "", t)])
        jobs = [({"name": "gn_ch_UBS", "outlet": "(per item)", "kind": "news", "tier": 2, "url": "https://news.google.com/x"}, "UBS")]
        with mock.patch("finsent.score_texts", lambda texts: ([0.0] * len(texts), "vader")):
            new, _ = swiss_news.fetch(con, CONF, now, jobs=jobs, getter=lambda u: (feedparser.parse(body), 200, None, False),
                                      benzinga=False)
        self.assertEqual([r["title"] for r in new], ["UBS Aktie: Bundesgericht bestätigt Bonus-Streichung"])

    def test_lang_and_lexicon(self):
        self.assertEqual(swiss_news.lang_of("Novartis kauft britisches Biotech für 1,5 Mrd. Dollar"), "de")
        self.assertEqual(swiss_news.lang_of("Novartis wins FDA approval for new drug"), "en")
        self.assertGreater(swiss_news.lex_de("Alcon erhielt FDA-Zulassung"), 0)
        self.assertLess(swiss_news.lex_de("Logitech mit Gewinnwarnung, Aktie bricht ein"), 0)
        self.assertEqual(swiss_news.ser_sentiment("ser_mgmt", "Purchase of 1,000 securities"), 0.5)
        self.assertEqual(swiss_news.ser_sentiment("ser_mgmt", "Sale of 1,000 securities"), -0.3)
        self.assertEqual(swiss_news.ser_sentiment("ser_sig", "Disclosure of shareholdings in UBS Group AG"), 0.0)

    def test_fetch_name_rule_dedupe_ser_blocked(self):
        import feedparser
        con = fresh_con()
        now = db.utcnow().replace(microsecond=0)
        t = now - timedelta(minutes=30)
        feeds = {
            "https://gen": rss([("Novartis erhält FDA-Zulassung für neues Medikament", "", t),
                                ("SMI im Plus, Pharma gefragt", "Novartis und Roche fester", t),          # name only in body -> drop
                                ("Logitech gewinnt Marktanteile", "", t)]),
            "https://ser": rss([("UBS Group AG", "Purchase of 5,000 securities amounting to CHF 200,000 by a board member", t),
                                ("Kuros Biosciences Ltd.", "Purchase of 59,496 securities", t)]),
        }
        jobs = [({"name": "gen", "outlet": "cash.ch", "kind": "news", "tier": 2, "url": "https://gen"}, None),
                ({"name": "ser_mgmt", "outlet": "SER", "kind": "ser_mgmt", "tier": 1, "url": "https://ser"}, None),
                ({"name": "blocked", "outlet": "X", "kind": "news", "tier": 2, "url": "https://blocked"}, None)]

        def getter(url):
            if url == "https://blocked":
                return None, 403, "blocked", True
            return feedparser.parse(feeds[url]), 200, None, False
        with mock.patch("finsent.score_texts", lambda texts: ([0.0] * len(texts), "vader")):
            new, status = swiss_news.fetch(con, CONF, now, jobs=jobs, getter=getter, benzinga=False)
        got = sorted((r["ticker"], r["feed"]) for r in new)
        self.assertEqual(got, [("LOGI", "gen"), ("NVS", "gen"), ("UBS", "ser_mgmt")])
        ser = [r for r in new if r["feed"] == "ser_mgmt"][0]
        self.assertEqual(ser["sentiment"], 0.5)
        self.assertTrue(ser["title"].startswith("UBS Group AG: Purchase"))
        nvs = [r for r in new if r["ticker"] == "NVS"][0]
        self.assertGreater(nvs["sentiment"], 0)               # German lexicon: Zulassung
        self.assertEqual(nvs["novelty"], 1.0)
        self.assertTrue(status["blocked"]["blocked"] and not status["blocked"]["ok"])
        row = con.execute("SELECT feed, lang FROM media WHERE ticker='NVS'").fetchone()
        self.assertEqual((row[0], row[1]), ("swiss:gen", "de"))
        with mock.patch("finsent.score_texts", lambda texts: ([0.0] * len(texts), "vader")):
            new2, _ = swiss_news.fetch(con, CONF, now, jobs=jobs, getter=getter, benzinga=False)
        self.assertEqual(new2, [])                            # dedupe


class Observations(unittest.TestCase):
    def setUp(self):
        self.con = fresh_con()

    def six_row(self, gap, t="NVS"):
        return {"ticker": t, "six_symbol": "NOVN.SW", "six_chg_pct": gap, "implied_gap_pct": gap, "us_prev_close": 150.0,
                "six_bar_ts": "2026-10-21T13:00:00Z", "delay_min": 15}

    def test_six_move_gap_preopen_only(self):
        pre = datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc)      # 09:00 ET (EDT) -> pre-open window
        n = swiss.observe(self.con, SO, pre, six_rows=[self.six_row(1.6), self.six_row(0.4, "UBS")], price_fn=lambda t: None)
        self.assertEqual(n, 1)
        o = self.con.execute("SELECT * FROM swiss_observations").fetchone()
        self.assertEqual((o["rule"], o["ticker"], o["direction"], o["entry_basis"], o["observe_only"]),
                         ("six_move_gap", "NVS", "long", "next_us_open", 1))
        self.assertEqual(swiss.observe(self.con, SO, pre + timedelta(minutes=5), six_rows=[self.six_row(2.0)], price_fn=lambda t: None), 0)
        mid = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)       # 04:00 ET: outside the window
        c2 = fresh_con()
        self.assertEqual(swiss.observe(c2, SO, mid, six_rows=[self.six_row(3.0)], price_fn=lambda t: None), 0)

    def test_news_and_spike_rules(self):
        now = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)      # 12:00 ET: US open -> IEX entry
        news = [{"id": "a", "ticker": "ALC", "created_at": swiss.db.iso(now - timedelta(minutes=10)), "title": "Alcon erhält FDA-Zulassung",
                 "feed": "gn_ch_ALC", "kind": "news", "tier": 2, "novelty": 0.9, "sentiment": 0.5},
                {"id": "b", "ticker": "LOGI", "created_at": swiss.db.iso(now), "title": "Logitech: neue Maus", "feed": "cash_ch",
                 "kind": "news", "tier": 2, "novelty": 0.9, "sentiment": 0.05}]                           # too neutral
        spikes = [{"id": 7, "ticker": "UBS", "tf": "5Min", "ts": swiss.db.iso(now - timedelta(minutes=5)), "ratio": 4.0, "ret": 0.8, "price": 47.0}]
        n = swiss.observe(self.con, SO, now, new_news=news, new_spikes=spikes, price_fn=lambda t: 50.0)
        rows = {r["rule"]: dict(r) for r in self.con.execute("SELECT * FROM swiss_observations")}
        self.assertEqual(n, 2)
        self.assertEqual((rows["swiss_news"]["ticker"], rows["swiss_news"]["direction"], rows["swiss_news"]["entry_basis"],
                          rows["swiss_news"]["entry_price"]), ("ALC", "long", "iex_mid_now", 50.0))
        self.assertEqual((rows["swiss_spike_fade"]["ticker"], rows["swiss_spike_fade"]["direction"]), ("UBS", "short"))

    def test_backfill_creates_no_news_obs(self):
        now = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
        news = [{"id": "a", "ticker": "ALC", "created_at": swiss.db.iso(now), "title": "Alcon erhält FDA-Zulassung", "feed": "x",
                 "kind": "news", "tier": 2, "novelty": 1.0, "sentiment": 0.5}]
        self.assertEqual(swiss.observe(self.con, SO, now, new_news=news, price_fn=lambda t: 1.0, backfill=True), 0)

    def test_invalidate_keeps_row_excludes_from_eval_and_quarantines_headline(self):
        now = datetime(2026, 10, 21, 13, 15, tzinfo=timezone.utc)
        con = self.con
        con.execute("""INSERT INTO media(id,ticker,created_at,outlet,tier,title,feed,sentiment,scorer,novelty,lang)
                       VALUES ('swFMC','UBS',?,'finanzen.at',2,'FMC-Aktie leichter: UBS warnt vor Gefahr durch US-Reformpapier',
                       'swiss:gn_ch_UBS',-0.2,'lex_de',0.86,'de')""", (db.iso(now - timedelta(minutes=50)),))
        con.commit()
        news = [{"id": "swFMC", "ticker": "UBS", "created_at": db.iso(now - timedelta(minutes=50)),
                 "title": "FMC-Aktie leichter: UBS warnt vor Gefahr durch US-Reformpapier", "feed": "gn_ch_UBS", "kind": "news",
                 "tier": 2, "novelty": 0.86, "sentiment": -0.2}]
        self.assertEqual(swiss.observe(con, SO, now, new_news=news, price_fn=lambda t: None), 1)
        oid = con.execute("SELECT id FROM swiss_observations WHERE rule='swiss_news'").fetchone()[0]
        con.execute("INSERT INTO swiss_obs_returns(obs_id,horizon,net_signed) VALUES (?,?,?)", (oid, "1d", 0.01))
        con.commit()
        self.assertTrue(swiss.invalidate(con, oid, "false positive: UBS as broker on FMC", now))
        o = con.execute("SELECT * FROM swiss_observations WHERE id=?", (oid,)).fetchone()
        self.assertEqual((o["status"], o["observe_only"]), ("invalid", 1))                 # kept, flagged
        self.assertIn("FMC", o["excluded_reason"])
        self.assertIsNone(con.execute("SELECT 1 FROM media WHERE id='swFMC'").fetchone())
        self.assertEqual(con.execute("SELECT reason FROM media_rejected WHERE id='swFMC'").fetchone()[0],
                         "false positive: UBS as broker on FMC")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM swiss_obs_returns WHERE obs_id=?", (oid,)).fetchone()[0], 0)
        self.assertEqual(swiss.summary(con, SO), [])
        # evaluate() skips it (would otherwise try to fill the next-US-open entry)
        self.assertEqual(swiss.evaluate(con, SO, now + timedelta(days=5), bars=lambda *a: (_ for _ in ()).throw(AssertionError)), 0)
        # an invalid observation does not debounce the next genuine UBS headline
        news2 = [dict(news[0], id="swOK", title="UBS warnt vor Gewinnrückgang im vierten Quartal", sentiment=-0.5,
                      created_at=db.iso(now + timedelta(minutes=5)))]
        self.assertEqual(swiss.observe(con, SO, now + timedelta(minutes=10), new_news=news2, price_fn=lambda t: None), 1)

    def test_zcombine(self):
        self.assertIsNone(swiss.zcombine(None, None, 3))
        self.assertAlmostEqual(swiss.zcombine(2.0, 1.0, 2.0), (1.5 + 1.0) / 1.25 ** 0.5)
        self.assertEqual(swiss.zcombine(0.1, None, 5), 0.1)


class Evaluation(unittest.TestCase):
    def test_outcomes_slippage_and_open_fill(self):
        import pandas as pd
        con = fresh_con()
        t_obs = datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc)    # Wed 07:00 ET -> entry at 09:30 ET open
        con.execute("""INSERT INTO swiss_observations(ts,ticker,rule,trigger_type,trigger_ref,direction,reason,entry_basis)
                       VALUES (?,?,?,?,?,?,?,?)""", (db.iso(t_obs), "NVS", "six_move_gap", "six_preopen", "NVS:x", "long", "t", "next_us_open"))
        con.commit()
        idx = pd.date_range("2026-10-14 13:30", "2026-10-20 20:00", freq="5min", tz="UTC")

        def bars(sym, interval, start, end):
            base = 100.0 if sym == "NVS" else 500.0
            step = 0.0001 if sym == "NVS" else 0.0
            close = [base * (1 + step * i) for i in range(len(idx))]
            return pd.DataFrame({"Open": close, "Close": close}, index=idx)
        now = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
        n = swiss.evaluate(con, SO, now, bars=bars)
        self.assertEqual(n, 3)
        o = con.execute("SELECT * FROM swiss_observations").fetchone()
        self.assertEqual(o["entry_ts"], "2026-10-14T13:30:00Z")
        self.assertAlmostEqual(o["entry_price"], 100.0)
        r = {x["horizon"]: dict(x) for x in con.execute("SELECT * FROM swiss_obs_returns")}
        self.assertAlmostEqual(r["30m"]["ret"], 100 * (1 + 0.0001 * 6) / 100 - 1, places=9)
        self.assertAlmostEqual(r["30m"]["net_signed"], r["30m"]["ret"] - 0.0006, places=9)     # 3 bps per side
        self.assertAlmostEqual(r["30m"]["spy_ret"], 0.0)
        self.assertGreater(con.execute("SELECT COUNT(*) FROM swiss_random").fetchone()[0], 0)
        s = swiss.summary(con, SO)
        self.assertTrue(all(not d["ready"] for d in s))                                        # n < 100
        self.assertEqual(swiss.net_signed(0.01, "short"), -0.01 - 0.0006)

    def test_open_tests(self):
        import pandas as pd
        con = fresh_con()
        con.execute("""INSERT INTO six_moves(ts,ticker,six_symbol,six_bar_ts,six_chg_pct,implied_gap_pct,us_open)
                       VALUES ('2026-10-14T12:30:00Z','NVS','NOVN.SW','2026-10-14T12:15:00Z',1.5,1.2,0)""")
        con.commit()
        daily = pd.DataFrame({"Open": [100.0, 101.0], "Close": [100.0, 102.0]},
                             index=pd.to_datetime(["2026-10-13", "2026-10-14"]).tz_localize("America/New_York"))
        n = swiss.open_tests(con, datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc), daily=lambda t: daily)
        self.assertEqual(n, 1)
        r = con.execute("SELECT * FROM six_open_tests").fetchone()
        self.assertAlmostEqual(r["open_gap_pct"], 1.0)
        self.assertAlmostEqual(r["open_to_close_pct"], (102 / 101 - 1) * 100)


class XOff(unittest.TestCase):
    def test_x_check_disabled(self):
        self.assertEqual(swiss.x_check({"x_enabled": False}), 3)

    def test_x_check_enabled_uses_budget(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=2, stdout="over budget")
            self.assertEqual(swiss.x_check({"x_enabled": True, "names": SO["names"]}), 2)
            self.assertIn("budget.py", " ".join(run.call_args[0][0]))


if __name__ == "__main__":
    unittest.main()
