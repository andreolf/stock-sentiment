#!/usr/bin/env python3
"""Unit tests for media parsing helpers, composite combination, Form 4 parsing, earnings session logic. No network."""
import math, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_SENT_DB"] = os.path.join(tempfile.mkdtemp(), "s.db")
import media, composite, edgar, earnings


class Media(unittest.TestCase):
    def test_norm_and_tier(self):
        self.assertEqual(media.norm_title("Tesla Q3 earnings preview - Electrek"), "tesla q3 earnings preview")
        t1 = ["Reuters", "Bloomberg", "WSJ", "Financial Times", "CNBC", "The Wall Street Journal"]
        self.assertEqual(media.tier_of("Bloomberg.com", t1), 1)
        self.assertEqual(media.tier_of("The Wall Street Journal", t1), 1)
        self.assertEqual(media.tier_of("Yahoo Finance", t1), 2)

    def test_classify_source_quality(self):
        import json
        cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")))["media"]
        c = lambda o, t="Nvidia shares rise after earnings beat", url=None, google=True: media.classify(o, t, url, cfg, google)[0]
        for o in ("Reuters", "Bloomberg.com", "The Wall Street Journal", "CNBC", "Financial Times", "BNN Bloomberg"):
            self.assertEqual(c(o), 1, o)
        for o in ("Yahoo Finance", "Yahoo Finance UK", "MarketWatch", "Barron's", "Benzinga", "24/7 Wall St.", "Investopedia",
                  "Associated Press", "AP News", "Investor's Business Daily", "Forbes", "Morningstar", "Investing.com India",
                  "cash.ch (AWP)", "finews.ch", "NZZ"):
            self.assertEqual(c(o), 2, o)
        for o in ("Moomoo", "\u5bcc\u9014\u725b\u725b", "Stocktwits", "TradingView", "Reddit", "r/wallstreetbets", "Webull", "Tiger Trade",
                  "X", "Twitter", "YouTube", "Medium", "someone.substack.com", "InvestorsHub", "HotCopper"):
            self.assertEqual(c(o), media.TIER_UGC, o)
        for o in ("shattered.io", "Startup Fortune", "AD HOC NEWS", "Some Random Blog", "", "(per item)"):
            self.assertEqual(c(o), media.TIER_UNVETTED, o)
        self.assertEqual(c("Yahoo Finance", url="https://stocktwits.com/news-articles/x", google=False), media.TIER_UGC)
        self.assertEqual(c("Yahoo Finance", url="https://finance.yahoo.com/news/x.html", google=False), 2)
        self.assertEqual(c("Teslarati", google=False), 2)                      # directly configured feed
        self.assertEqual(c("Moomoo", "Tesla Stock Edges Higher After Company Reports Results -- WSJ"), 2)   # syndicated copy
        self.assertEqual(c("Reuters", "$NVIDIA (NVDA.US)$ haiyaaaa... uncle shaking\u00b2 hand look at it."), media.TIER_UGC)

    def test_headline_style(self):
        bad = ["$NVIDIA (NVDA.US)$ haiyaaaa... uncle shaking\u00b2 hand look at it. itchy hand want to click buy.",
               "Can Tesla (TSLA.US) shares hit $4,400?", "$NVDA to the moon", "$TSLA $NVDA $PLTR all green today",
               "lol nvda gonna rip", "haiyaaaa nvda again..."]
        good = ["Nvidia shares fall after US widens AI chip export curbs", "Watching $PLTR; Hearing SwingTrader Buys Stock",
                "Tesla Stock At $4,400? Cathie Wood's ARK Pins Hopes On Robotaxis", "Nvidia rises from $180 to $200 after deal",
                "Palantir stock jumps 5% on new federal cloud pa...", "Musk: 'We are gonna win' on robotaxi, Tesla says",
                "Tesla shares rise as Elon Musk reveals new clues regarding a merger", "Why Is Nvidia Stock Falling Today?"]
        for h in bad:
            self.assertIsNotNone(media.headline_style(h), h)
        for h in good:
            self.assertIsNone(media.headline_style(h), h)

    def test_matchers(self):
        m = media.matchers({"match": {"META": ["Meta Platforms", "META"], "TSLA": ["Tesla", "TSLA"]}}, ["META", "TSLA"])
        self.assertTrue(m["META"].search("Meta Platforms beats estimates"))
        self.assertFalse(m["META"].search("the meta narrative of AI"))      # bare ticker is case-sensitive
        self.assertTrue(m["TSLA"].search("why tesla fell"))


class Composite(unittest.TestCase):
    def test_combine(self):
        z = {"x_sent": 2.0, "media_sent": 1.0, "x_vol": 3.0, "reddit_chg": 1.0, "media_cnt_chg": None, "price_vol": 2.0}
        S, c = composite.combine(z)
        self.assertAlmostEqual(S, 1.5)
        self.assertAlmostEqual(c, (1.5 + 2.0 * 0.5) / 1.25 ** 0.5)
        S, c = composite.combine({**z, "x_sent": -2.0, "media_sent": -1.0})
        self.assertLess(c, S)                                   # attention amplifies the bearish direction
        self.assertEqual(composite.combine({**z, "x_sent": 0.1, "media_sent": 0.0})[1], 0.05)   # weak S: attention ignored
        self.assertEqual(composite.combine({"x_sent": None, "media_sent": None, "x_vol": 5})[1], None)


class CompositeZ(unittest.TestCase):
    """2026-10-21: X z vs prior X runs (not repeated collector rows), std floors, >=5-run warmup, winsorize +-4."""
    def setUp(self):
        import db
        from datetime import timedelta
        self.db, self.td = db, timedelta
        self.con = db.connect(os.path.join(tempfile.mkdtemp(), "cz.db"))   # own DB: other modules hold open txns
        self.now = db.utcnow().replace(microsecond=0)

    def tearDown(self):
        self.con.close()

    def add_run(self, i, ws, vr):
        ts = self.db.iso(self.now - self.td(minutes=30 * (10 - i)))
        self.con.execute("INSERT INTO runs(ts,ticker,batch,scorer,n_posts,w_sentiment,volume_ratio,status) VALUES (?,?,?,?,?,?,?,?)",
                         (ts, "NVDA", f"b{i}", "agent", 25, ws, vr, "ok"))

    def raw(self, ws, vr, batch="cur"):
        r = {k: None for k in composite.COMPS}
        r.update(x_sent=ws, x_vol=math.log(vr), _x_run=(batch, self.now, "agent"))
        return r

    def test_x_warmup_weight_zero(self):
        for i, ws in enumerate([0.12, 0.18, 0.17, 0.19]):
            self.add_run(i, ws, 1.5)
        notes = []
        z, _ = composite.zscores(self.con, "NVDA", self.raw(0.095, 1.6), self.now, notes)
        self.assertIsNone(z["x_sent"]); self.assertIsNone(z["x_vol"])
        self.assertTrue(any("x_sent warmup (4/5" in n for n in notes))
        self.assertIsNone(composite.combine(z)[1])          # nothing else available -> no composite at all

    def test_nvda_2026_10_21_regression(self):
        for i, (ws, vr) in enumerate([(0.125, 1.072), (0.184, 1.783), (0.172, 1.940), (0.189, 2.043), (0.165, 1.745)]):
            self.add_run(i, ws, vr)
        # 20 collector rows that all repeat the same X value (what produced z_x_sent -8.3)
        for m in range(20):
            self.con.execute("INSERT INTO composite(ts,ticker,source,x_sent,x_vol) VALUES (?,?,?,?,?)",
                             (self.db.iso(self.now - self.td(minutes=5 * (m + 1))), "NVDA", "collector", 0.165, 0.557))
        z, _ = composite.zscores(self.con, "NVDA", self.raw(0.0954, 1.596), self.now, [])
        self.assertAlmostEqual(z["x_sent"], (0.0954 - 0.167) / 0.05, delta=0.05)   # std 0.023 floored at 0.05 -> ~-1.43
        self.assertGreater(z["x_vol"], -1.0)

    def test_floor_and_winsorize(self):
        for i in range(6):
            self.add_run(i, 0.20, 2.0)                       # zero-variance baseline
        z, _ = composite.zscores(self.con, "NVDA", self.raw(-0.9, 40.0), self.now, [])
        self.assertEqual(z["x_sent"], -composite.Z_CAP)      # (-1.1/0.05 = -22) clipped to -4
        self.assertEqual(z["x_vol"], composite.Z_CAP)
        z, _ = composite.zscores(self.con, "NVDA", self.raw(0.22, 2.0), self.now, [])
        self.assertAlmostEqual(z["x_sent"], 0.4)             # 0.02 / floor 0.05, not /1e-9
        r = {k: None for k in composite.COMPS}; r["media_sent"] = 0.9
        self.assertEqual(composite.zscores(self.con, "NVDA", r, self.now, [])[0]["media_sent"], composite.Z_CAP)  # warmup scale clipped too


class Form4(unittest.TestCase):
    def test_parse(self):
        xml = b"""<ownershipDocument><reportingOwner><reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId></reportingOwner>
        <nonDerivativeTable>
         <nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>1000</value></transactionShares><transactionPricePerShare><value>50</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
         <nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>200</value></transactionShares><transactionPricePerShare><value>55</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
        </nonDerivativeTable></ownershipDocument>"""
        self.assertEqual(edgar.parse_form4(xml), ("Doe Jane", 1000.0, 50000.0, 11000.0))
        self.assertIn("results of operations", edgar.describe_items("2.02,9.01"))


class EarningsSessions(unittest.TestCase):
    TD = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27"]

    def test_reaction_date(self):
        self.assertEqual(earnings.reaction_date("2026-10-21", "after-hours", self.TD), "2026-10-22")
        self.assertEqual(earnings.reaction_date("2026-10-21", "pre-market", self.TD), "2026-10-21")
        self.assertEqual(earnings.reaction_date("2026-10-23", "after-hours", self.TD), "2026-10-26")   # weekend

    def test_session_index(self):
        self.assertEqual(earnings.session_index("2026-10-22", "2026-10-22", self.TD), 1)
        self.assertEqual(earnings.session_index("2026-10-22", "2026-10-26", self.TD), 3)
        self.assertIsNone(earnings.session_index("2026-10-22", "2026-10-27", self.TD))
        self.assertIsNone(earnings.session_index("2026-10-22", "2026-10-21", self.TD))


if __name__ == "__main__":
    unittest.main(verbosity=2)
