#!/usr/bin/env python3
"""Reddit as a second sentiment source (zero X calls).

Usage: reddit.py fetch [--core-only]  |  reddit.py show [TICKER]

1) Mention counts (always): ApeWisdom public API (https://apewisdom.io/api/, aggregates mentions across
   r/wallstreetbets, r/stocks, r/investing, r/options, ...): mentions + upvotes in the last 24h and the
   24h-ago value. 1-3 requests per run. Stored in reddit_counts (source='apewisdom').
2) Posts (when Reddit lets us in): r/wallstreetbets, r/stocks, r/teslainvestorsclub new+hot JSON with a
   polite User-Agent, at most 6 requests, 1.5 s apart. Stores posts mentioning a watchlist ticker
   (cashtag, bare ticker or alias like 'tesla') in reddit_posts with a VADER score, plus a
   reddit_counts row (source='reddit_json').
   NOTE (tested 2026-10-21): reddit.com returns HTTP 403 to unauthenticated requests from this box, so this
   step is skipped automatically. If Francesco creates a Reddit "script" app, set REDDIT_CLIENT_ID and
   REDDIT_CLIENT_SECRET and the same step runs through oauth.reddit.com (app-only token, read-only).
"""
import argparse, base64, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
import db


def get(url, ua, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": ua, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def apewisdom(con, tickers, ua, now):
    want, found = set(tickers), {}
    for page in range(1, 4):
        d = get(f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}", ua)
        for r in d.get("results", []):
            if r["ticker"] in want:
                found[r["ticker"]] = r
        if want <= set(found) or page >= d.get("pages", 1):
            break
    for t in tickers:
        r = found.get(t, {})
        con.execute("""INSERT OR REPLACE INTO reddit_counts(ts, ticker, posts_scanned, mentions, mentions_24h, avg_vader,
                       source, upvotes, rank) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (db.iso(now), t, None, r.get("mentions", 0), r.get("mentions_24h_ago"), None, "apewisdom",
                     r.get("upvotes"), r.get("rank")))
    return found


def reddit_token(ua):
    cid, sec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and sec):
        return None
    auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    d = get("https://www.reddit.com/api/v1/access_token", ua, {"Authorization": f"Basic {auth}"},
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode())
    return d.get("access_token")


def reddit_posts(con, tickers, cfg, ua, now):
    tok = reddit_token(ua)
    base, hdr = ("https://oauth.reddit.com", {"Authorization": f"Bearer {tok}"}) if tok else ("https://www.reddit.com", {})
    aliases = cfg.get("aliases", {})
    # bare ticker/cashtag: case-sensitive (avoids 'meta', 'coin', 'hood' as ordinary words); aliases: any case
    pats = {t: re.compile(rf"\$?\b{t}\b") for t in tickers}
    alias_pats = {t: re.compile("|".join(rf"\b{re.escape(a)}\b" for a in aliases.get(t, [])), re.I)
                  for t in tickers if aliases.get(t)}
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        sia = SentimentIntensityAnalyzer()
    except Exception:
        sia = None
    scanned, mentions, sents = 0, {t: 0 for t in tickers}, {t: [] for t in tickers}
    n = 0
    for sub in cfg.get("subreddits", []):
        for lst in cfg.get("listings", ["new", "hot"]):
            if n:
                time.sleep(1.5)
            n += 1
            url = f"{base}/r/{sub}/{lst}{'' if tok else '.json'}?limit=100&raw_json=1"
            try:
                d = get(url, ua, hdr)
            except urllib.error.HTTPError as e:
                print(f"  reddit {sub}/{lst}: HTTP {e.code} -> skipping direct Reddit posts this run")
                return None
            for ch in d.get("data", {}).get("children", []):
                p = ch["data"]; scanned += 1
                text = f"{p.get('title', '')}\n{p.get('selftext', '')}"
                for t in tickers:
                    if pats[t].search(text) or (t in alias_pats and alias_pats[t].search(text)):
                        mentions[t] += 1
                        s = sia.polarity_scores(text[:2000])["compound"] if sia else None
                        if s is not None:
                            sents[t].append(s)
                        con.execute("""INSERT OR REPLACE INTO reddit_posts(id, ticker, subreddit, title, selftext, author,
                                       score, num_comments, created_at, url, fetched_at, sentiment, scorer)
                                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (p["id"], t, sub, p.get("title"), (p.get("selftext") or "")[:4000], p.get("author"),
                                     p.get("score"), p.get("num_comments"),
                                     db.iso(datetime.fromtimestamp(p["created_utc"], timezone.utc)),
                                     "https://www.reddit.com" + p.get("permalink", ""), db.iso(now), s, "vader"))
    for t in tickers:
        con.execute("""INSERT OR REPLACE INTO reddit_counts(ts, ticker, posts_scanned, mentions, mentions_24h, avg_vader,
                       source) VALUES (?,?,?,?,?,?,?)""",
                    (db.iso(now), t, scanned, mentions[t], None,
                     sum(sents[t]) / len(sents[t]) if sents[t] else None, "reddit_json"))
    return scanned, mentions


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fetch", "show"])
    ap.add_argument("ticker", nargs="?")
    ap.add_argument("--core-only", action="store_true")
    a = ap.parse_args()
    conf = db.load_config()
    cfg = conf.get("reddit", {})
    ua = cfg.get("user_agent", "linux:stock-sentiment-research:0.1")
    con = db.connect()
    now = db.utcnow()
    tickers = db.watchlist(a.core_only)
    if a.cmd == "fetch":
        try:
            f = apewisdom(con, tickers, ua, now)
            print("reddit mentions 24h (ApeWisdom): " + ", ".join(
                f"{t} {f[t]['mentions']} (24h ago {f[t]['mentions_24h_ago']}, #{f[t]['rank']})" if t in f else f"{t} 0"
                for t in tickers))
        except Exception as e:
            print(f"  [warn] ApeWisdom failed: {e}")
        r = reddit_posts(con, tickers, cfg, ua, now)
        if r:
            print(f"reddit posts: scanned {r[0]}, mentions " + ", ".join(f"{t} {m}" for t, m in r[1].items() if m))
        con.commit()
    else:
        q = "SELECT * FROM reddit_counts WHERE ts=(SELECT MAX(ts) FROM reddit_counts WHERE source='apewisdom') AND source='apewisdom'"
        for r in con.execute(q):
            if not a.ticker or r["ticker"] == a.ticker.upper():
                print(f"{r['ticker']:5} mentions {r['mentions']} (24h ago {r['mentions_24h']}) upvotes {r['upvotes']} rank {r['rank']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
