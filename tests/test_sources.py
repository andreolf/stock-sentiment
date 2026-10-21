#!/usr/bin/env python3
"""Unit tests for media parsing helpers, composite combination, Form 4 parsing, earnings session logic. No network."""
import os, sys, tempfile, unittest
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
