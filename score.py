#!/usr/bin/env python3
"""Store sentiment scores for posts.

The primary scorer is the AGENT itself: it reads each post and judges sentiment toward
the ticker (-1 very bearish .. 0 neutral/irrelevant .. +1 very bullish) and optionally
novelty (0 = rehash/boilerplate .. 1 = genuinely new information). A cheap VADER lexicon
baseline is available for comparison (scorer='vader').

Usage:
  score.py export TICKER [--batch B|latest] [--all] [--scorer agent]
      -> prints JSON list of posts still lacking a score from that scorer (quality=1 only;
         --all includes low-quality too). Default batch: latest for the ticker.
  score.py apply FILE [--scorer agent]
      FILE = JSON list: [{"post_id": "2112...", "ticker": "TSLA", "sentiment": 0.6,
                          "novelty": 0.8}, ...]     (novelty optional -> auto-computed)
  score.py vader TICKER [--batch B|latest]
      -> lexicon baseline for every post in the batch (needs .venv with vaderSentiment)
  score.py show TICKER [--batch B|latest]
      -> table of posts with all scores

Auto novelty = 1 - max word-set Jaccard similarity vs earlier posts of the same ticker (7 days).
"""
import argparse, json, re, sys
import db


def latest_batch(con, ticker):
    r = con.execute("SELECT MAX(batch) FROM post_batches WHERE ticker=? AND batch NOT LIKE 'x-%'", (ticker,)).fetchone()
    return r[0]


def batch_posts(con, ticker, batch, include_lowq=False):
    q = """SELECT p.* FROM posts p JOIN post_batches b ON b.post_id=p.id AND b.ticker=p.ticker
           WHERE p.ticker=? AND b.batch=?""" + ("" if include_lowq else " AND p.quality=1")
    return con.execute(q + " ORDER BY p.created_at", (ticker, batch)).fetchall()


def words(t):
    return set(re.findall(r"[a-z0-9$]{3,}", re.sub(r"https?://\S+", "", t.lower())))


def auto_novelty(con, post_id, ticker):
    row = con.execute("SELECT text, created_at FROM posts WHERE id=? AND ticker=?", (post_id, ticker)).fetchone()
    if not row:
        return None
    w = words(row["text"])
    if not w:
        return 0.0
    best = 0.0
    for r in con.execute("""SELECT text FROM posts WHERE ticker=? AND id<>? AND created_at<=?
                            AND created_at >= datetime(?, '-7 days')""",
                         (ticker, post_id, row["created_at"], row["created_at"].replace("T", " ").rstrip("Z"))):
        o = words(r["text"])
        if o:
            best = max(best, len(w & o) / len(w | o))
    return round(1 - best, 3)


def store(con, post_id, ticker, sentiment, novelty, scorer):
    s = max(-1.0, min(1.0, float(sentiment)))
    n = auto_novelty(con, post_id, ticker) if novelty is None else max(0.0, min(1.0, float(novelty)))
    con.execute("""INSERT INTO scores(post_id, ticker, sentiment, novelty, scorer, scored_at) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(post_id, ticker, scorer) DO UPDATE SET sentiment=excluded.sentiment,
                   novelty=excluded.novelty, scored_at=excluded.scored_at""",
                (post_id, ticker, s, n, scorer, db.iso(db.utcnow())))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["export", "apply", "vader", "show"])
    ap.add_argument("arg", help="TICKER, or FILE for apply")
    ap.add_argument("--batch", default="latest")
    ap.add_argument("--scorer", default="agent")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    con = db.connect()

    if a.cmd == "apply":
        items = json.load(open(a.arg))
        n = 0
        for it in items:
            t = it["ticker"].upper()
            if not con.execute("SELECT 1 FROM posts WHERE id=? AND ticker=?", (str(it["post_id"]), t)).fetchone():
                print(f"  [skip] unknown post {it['post_id']} {t}")
                continue
            store(con, str(it["post_id"]), t, it["sentiment"], it.get("novelty"), it.get("scorer", a.scorer))
            n += 1
        con.commit()
        print(f"stored {n} scores (scorer={a.scorer})")
        return 0

    ticker = a.arg.upper()
    batch = latest_batch(con, ticker) if a.batch == "latest" else a.batch
    if not batch:
        print(f"no batches for {ticker}"); return 1

    if a.cmd == "export":
        out = []
        for p in batch_posts(con, ticker, batch, a.all):
            if con.execute("SELECT 1 FROM scores WHERE post_id=? AND ticker=? AND scorer=?",
                           (p["id"], ticker, a.scorer)).fetchone():
                continue
            out.append({"post_id": p["id"], "ticker": ticker, "author": p["author"],
                        "followers": p["followers"], "created_at": p["created_at"], "text": p["text"]})
        print(json.dumps(out, ensure_ascii=False, indent=1))
    elif a.cmd == "vader":
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        sia = SentimentIntensityAnalyzer()
        sia.lexicon.update({"bullish": 2.5, "bearish": -2.5, "moon": 2.0, "pump": 1.5, "dump": -2.0,
                            "short": -1.0, "puts": -1.5, "calls": 1.0, "beat": 1.5, "miss": -1.5,
                            "rally": 2.0, "crash": -3.0, "squeeze": 1.0, "overvalued": -2.0, "undervalued": 2.0})
        ps = batch_posts(con, ticker, batch, True)
        for p in ps:
            store(con, p["id"], ticker, sia.polarity_scores(p["text"])["compound"], None, "vader")
        con.commit()
        print(f"vader scored {len(ps)} posts for {ticker} batch {batch}")
    elif a.cmd == "show":
        for p in batch_posts(con, ticker, batch, True):
            sc = {r["scorer"]: (r["sentiment"], r["novelty"]) for r in con.execute(
                "SELECT * FROM scores WHERE post_id=? AND ticker=?", (p["id"], ticker))}
            ss = " ".join(f"{k}={v[0]:+.2f}/n{v[1]:.2f}" for k, v in sc.items())
            print(f"{'  ' if p['quality'] else 'x '}{p['followers']:>7} @{(p['author'] or '?')[:15]:15} {ss:32} {p['text'][:70]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
