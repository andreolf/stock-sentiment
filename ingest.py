#!/usr/bin/env python3
"""Ingest X MCP results (namespace `x`) into data/sentiment.db.

Usage:
  ingest.py TICKER FILE [FILE ...] [--batch ISO] [--query "q"]

Each FILE is one raw result of an X tool call, saved as returned (CallDynamicTool
with write_to_file=true is fine; surrounding fence text / 'X reads left' lines are
stripped automatically). The kind is auto-detected per file:

1) get_posts_counts_recent  ->  counts
   {"data": [{"start": "2026-10-14T08:00:00.000Z", "end": "2026-10-14T09:00:00.000Z",
              "tweet_count": 81}, ...],
    "meta": {"total_tweet_count": 61103}}

2) search_posts_all (with expansions=author_id and user.fields incl. public_metrics) -> posts
   {"data": [{"id": "2112813684741513518", "author_id": "2314301454",
              "created_at": "2026-10-21T07:50:11.000Z", "text": "...",
              "public_metrics": {"like_count": 0, "impression_count": 1, ...},
              "url": "https://x.com/<user>/status/<id>"}, ...],
    "includes": {"users": [{"id": "2314301454", "username": "NovaTwentyOne",
                            "public_metrics": {"followers_count": 444, ...}}, ...]},
    "meta": {"result_count": 25, "next_token": "..."}}
   Posts without includes.users still ingest (author/followers become NULL/0).

Quality flags (posts.quality=0 + quality_reason, kept but ignored by signals.py):
  spam phrases (WhatsApp groups, "#1 stock pick" campaigns, paid-signal promos),
  duplicate normalized text within the ticker, more than 2 posts by one author per batch.
All posts of this call get the same batch id (default: now, UTC, minute precision).
"""
import argparse, re, sys
from collections import Counter
import db

SPAM = re.compile(r"whatsapp|telegram group|stock pick|top pick|#1 pick|deserve a closer look|"
                  r"premium members|profit achieved|join (my|our) (group|discord)|dm me|"
                  r"are in focus|keep evs in play|uid: \d+", re.I)


def norm(t):
    t = re.sub(r"https?://\S+", "", t.lower())
    return re.sub(r"[^a-z0-9$ ]+", "", re.sub(r"\s+", " ", t)).strip()


def ingest_counts(con, ticker, obj, fetched_at, query):
    n = 0
    for b in obj.get("data", []):
        con.execute("""INSERT INTO counts(ticker, window_start, window_end, post_count, fetched_at, query)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(ticker, window_start, window_end) DO UPDATE SET
                       post_count=excluded.post_count, fetched_at=excluded.fetched_at,
                       query=COALESCE(excluded.query, counts.query)""",
                    (ticker, b["start"].replace(".000Z", "Z"), b["end"].replace(".000Z", "Z"),
                     int(b["tweet_count"]), fetched_at, query))
        n += 1
    return n


def ingest_posts(con, ticker, obj, fetched_at, batch):
    users = {u["id"]: u for u in obj.get("includes", {}).get("users", [])}
    seen = {norm(r["text"]) for r in con.execute(
        "SELECT text FROM posts WHERE ticker=? AND quality=1", (ticker,))}
    author_n = Counter({r[0]: r[1] for r in con.execute(
        """SELECT p.author, COUNT(*) FROM posts p JOIN post_batches b ON b.post_id=p.id AND b.ticker=p.ticker
           WHERE p.ticker=? AND b.batch=? AND p.quality=1 GROUP BY p.author""", (ticker, batch))})
    n = flagged = 0
    for p in obj.get("data", []):
        u = users.get(p.get("author_id"), {})
        pm = p.get("public_metrics", {})
        author = u.get("username")
        followers = u.get("public_metrics", {}).get("followers_count", 0)
        exists = con.execute("SELECT quality FROM posts WHERE id=? AND ticker=?", (p["id"], ticker)).fetchone()
        reason = None
        if exists is None:
            nt = norm(p["text"])
            author_n[author] += 1
            if SPAM.search(p["text"]):
                reason = "spam_phrase"
            elif nt in seen:
                reason = "duplicate_text"
            elif author and author_n[author] > 2:
                reason = "author_cap"
            seen.add(nt)
        url = p.get("url") or (f"https://x.com/{author}/status/{p['id']}" if author else None)
        con.execute("""INSERT INTO posts(id, ticker, author, followers, text, created_at, url, fetched_at,
                                         author_id, likes, impressions, quality, quality_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id, ticker) DO UPDATE SET likes=excluded.likes,
                         impressions=excluded.impressions, followers=excluded.followers""",
                    (p["id"], ticker, author, followers, p["text"], p.get("created_at", "").replace(".000Z", "Z"),
                     url, fetched_at, p.get("author_id"), pm.get("like_count"), pm.get("impression_count"),
                     0 if reason else 1, reason))
        con.execute("INSERT OR IGNORE INTO post_batches(post_id, ticker, batch) VALUES (?,?,?)",
                    (p["id"], ticker, batch))
        n += 1
        flagged += bool(reason)
    return n, flagged


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--batch", help="batch id (ISO UTC); default now")
    ap.add_argument("--query", help="X query used (stored with counts)")
    a = ap.parse_args()
    ticker = a.ticker.upper()
    now = db.utcnow()
    fetched_at = db.iso(now)
    batch = a.batch or now.strftime("%Y-%m-%dT%H:%MZ")
    con = db.connect()
    for f in a.files:
        obj = db.load_x_json(f)
        data = obj.get("data", [])
        if data and "tweet_count" in data[0]:
            n = ingest_counts(con, ticker, obj, fetched_at, a.query)
            print(f"{ticker}: {n} count buckets from {f} (total {obj.get('meta', {}).get('total_tweet_count')})")
        elif data and "text" in data[0]:
            n, fl = ingest_posts(con, ticker, obj, fetched_at, batch)
            print(f"{ticker}: {n} posts from {f} (batch {batch}, {fl} new posts flagged low-quality)")
        else:
            print(f"{ticker}: no data in {f} (meta={obj.get('meta')})")
    con.commit()
    print(f"batch={batch}")


if __name__ == "__main__":
    sys.exit(main())
