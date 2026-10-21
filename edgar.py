#!/usr/bin/env python3
"""SEC EDGAR filings for the watchlist (free, no key): 8-K item numbers and Form 4 insider buys/sells.

Usage: edgar.py fetch [--days 14]  |  edgar.py show [TICKER] [--days 7]

* SEC fair-access rules: declared User-Agent "Name email" (config.json sec.user_agent), <= 10 requests/s
  (we sleep 0.15 s), only what we need. Ticker->CIK map cached in data/sec_company_tickers.json (refreshed weekly).
* Per ticker: data.sec.gov/submissions/CIK##########.json -> recent 8-K/8-K/A (with `items`, e.g. "2.02,9.01")
  and Form 4 (for new Form 4s the raw XML is fetched once to sum open-market purchases (code P) and sales (S)).
* Stored in sec_filings; used by paper_trader.hard_news() as a "real news" source (any 8-K, or a Form 4 with an
  open-market buy, accepted within the news window).
* 2026-10-21: sec.gov answers HTTP 403 "Undeclared Automated Tool" to this box's IP (network-level block, not the
  UA). The script then logs the block and exits 0; Benzinga/Alpaca news (which reports 8-Ks and insider trades)
  remains the hard-news source. If the block lifts, nothing needs to change.
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET
from datetime import timedelta
import db

ITEM_NAMES = {"1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy", "2.01": "acquisition/disposal",
              "2.02": "results of operations", "2.03": "new obligation", "2.05": "exit costs", "2.06": "impairment",
              "3.01": "delisting notice", "3.02": "unregistered equity sale", "4.01": "auditor change",
              "4.02": "non-reliance on financials", "5.02": "officer/director change", "5.03": "bylaws change",
              "5.07": "shareholder vote", "7.01": "Reg FD disclosure", "8.01": "other events", "9.01": "exhibits"}
CACHE = os.path.join(db.HERE, "data", "sec_company_tickers.json")


class Blocked(Exception):
    pass


def get(url, ua, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            b = r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 403:
            raise Blocked("HTTP 403" + (" Undeclared Automated Tool" if "Undeclared Automated Tool" in body else ""))
        raise
    finally:
        time.sleep(0.15)
    return b if raw else json.loads(b.decode())


def cik_map(ua):
    if os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < 7 * 86400:
        d = json.load(open(CACHE))
    else:
        d = get("https://www.sec.gov/files/company_tickers.json", ua)
        json.dump(d, open(CACHE, "w"))
    return {v["ticker"]: str(v["cik_str"]).zfill(10) for v in d.values()}


def parse_form4(xml_bytes):
    """-> (insider name, buy_shares, buy_value, sell_value) from a Form 4 XML (non-derivative table)."""
    root = ET.fromstring(xml_bytes)
    name = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "").strip()
    bs = bv = sv = 0.0
    for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = (tx.findtext(".//transactionCoding/transactionCode") or "").strip()
        sh = float(tx.findtext(".//transactionAmounts/transactionShares/value") or 0)
        px = float(tx.findtext(".//transactionAmounts/transactionPricePerShare/value") or 0)
        if code == "P":
            bs += sh; bv += sh * px
        elif code == "S":
            sv += sh * px
    return name, bs, bv, sv


def describe_items(items):
    return ", ".join(f"{i} {ITEM_NAMES.get(i, '')}".strip() for i in (items or "").split(",") if i)


def fetch(con, tickers, ua, days, now):
    m = cik_map(ua)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    new = 0
    for t in tickers:
        cik = m.get(t)
        if not cik:
            continue
        rec = get(f"https://data.sec.gov/submissions/CIK{cik}.json", ua)["filings"]["recent"]
        for i, form in enumerate(rec["form"]):
            if rec["filingDate"][i] < since:
                break
            if form not in ("8-K", "8-K/A", "4"):
                continue
            acc = rec["accessionNumber"][i]
            if con.execute("SELECT 1 FROM sec_filings WHERE accession=?", (acc,)).fetchone():
                continue
            doc = rec["primaryDocument"][i]
            base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"
            ins, bs, bv, sv, note = None, None, None, None, None
            if form == "4":
                try:
                    ins, bs, bv, sv = parse_form4(get(f"{base}/{re.sub(r'^xsl[^/]*/', '', doc)}", ua, raw=True))
                except Blocked:
                    raise
                except Exception as e:
                    note = f"form4 parse failed: {e}"
            acc_ts = rec["acceptanceDateTime"][i]  # e.g. 2026-10-21T16:05:12.000Z
            con.execute("""INSERT OR IGNORE INTO sec_filings(accession, ticker, cik, form, filed_at, items, url, insider,
                           buy_shares, buy_value, sell_value, fetched_at, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (acc, t, cik, form, acc_ts.replace(".000Z", "Z"), rec["items"][i] or None, f"{base}/{doc}", ins,
                         bs, bv, sv, db.iso(now), note))
            new += 1
    con.commit()
    return new


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fetch", "show"])
    ap.add_argument("ticker", nargs="?")
    ap.add_argument("--days", type=int, default=None)
    a = ap.parse_args()
    cfg = db.load_config().get("sec", {})
    ua = cfg.get("user_agent", "stock-sentiment research contact@example.com")
    con = db.connect()
    now = db.utcnow()
    if a.cmd == "fetch":
        try:
            n = fetch(con, db.watchlist(), ua, a.days or cfg.get("lookback_days", 14), now)
            print(f"edgar: {n} new filings")
        except Blocked as e:
            print(f"edgar: SEC blocked this box ({e}); skipped. Hard-news gate uses Benzinga/media only.")
        except Exception as e:
            print(f"edgar: [warn] {type(e).__name__}: {e}")
        return 0
    q = "SELECT * FROM sec_filings WHERE filed_at>=?" + (" AND ticker=?" if a.ticker else "") + " ORDER BY filed_at DESC"
    args = (db.iso(now - timedelta(days=a.days or 7)),) + ((a.ticker.upper(),) if a.ticker else ())
    for r in con.execute(q, args):
        extra = describe_items(r["items"]) if r["form"].startswith("8-K") else \
            f"{r['insider']}: buys ${r['buy_value'] or 0:,.0f}, sells ${r['sell_value'] or 0:,.0f}"
        print(f"{db.local(r['filed_at'])} {r['ticker']:5} {r['form']:5} {extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
