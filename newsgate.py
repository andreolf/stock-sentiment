"""'Real news' gate: is a move backed by a fresh, novel headline or an SEC filing?

hard_news(con, ticker, at_iso, direction=None, window_min=60, novelty_min=0.5, max_tier=2)
  -> dict(kind, ref, title, created_at, novelty, sentiment, tier) or None
Candidates inside [at - window, at + 5 min]:
  * media rows with tier <= max_tier (tier 1 Reuters/Bloomberg/WSJ/FT/CNBC, tier 2 others) and Benzinga `news` rows
    (tier 2), whose title is NOVEL: 1 - max word-Jaccard vs the ticker's headlines from 72 h to 60 min before it
    >= novelty_min (a rewrite of an older story is stale news, which tends to reverse: Tetlock 2011);
  * sec_filings: any 8-K/8-K/A, or a Form 4 with an open-market purchase (buy_value > 0).
Headlines whose effective sentiment clearly contradicts the trade direction (s * dir < -0.1) are rejected.
Best = SEC filing first, then lower tier, then higher novelty.
"""
import re
from datetime import timedelta
import db


def words(t):
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", t or "")
    return set(w for w in re.findall(r"[a-z0-9$]+", t.lower()) if len(w) > 2)


def novelty(con, ticker, title, created_at):
    w = words(title)
    if not w:
        return 0.0
    c = db.parse_ts(created_at)
    lo, hi = db.iso(c - timedelta(hours=72)), db.iso(c - timedelta(minutes=60))
    best = 0.0
    for (t,) in list(con.execute("SELECT title FROM media WHERE ticker=? AND created_at BETWEEN ? AND ?", (ticker, lo, hi))) + \
            list(con.execute("SELECT headline FROM news WHERE ticker=? AND created_at BETWEEN ? AND ?", (ticker, lo, hi))):
        o = words(t)
        if o:
            best = max(best, len(w & o) / len(w | o))
    return round(1 - best, 3)


def hard_news(con, ticker, at_iso, direction=None, window_min=60, novelty_min=0.5, max_tier=2):
    at = db.parse_ts(at_iso)
    lo, hi = db.iso(at - timedelta(minutes=window_min)), db.iso(at + timedelta(minutes=5))
    sign = {"long": 1, "short": -1}.get(direction, 0)
    for r in con.execute("""SELECT * FROM sec_filings WHERE ticker=? AND filed_at BETWEEN ? AND ?
                            AND (form LIKE '8-K%' OR (form='4' AND COALESCE(buy_value,0)>0)) ORDER BY filed_at DESC""",
                         (ticker, lo, hi)):
        if r["form"] == "4" and sign < 0:
            continue  # an insider buy does not back a short
        return {"kind": "sec", "ref": r["accession"], "title": f"{r['form']} {r['items'] or ''}".strip(),
                "created_at": r["filed_at"], "novelty": 1.0, "sentiment": None, "tier": 0}
    cands = []
    for r in con.execute("""SELECT id, title, created_at, tier, COALESCE(agent_sentiment, sentiment) s FROM media
                            WHERE ticker=? AND tier<=? AND created_at BETWEEN ? AND ?""", (ticker, max_tier, lo, hi)):
        cands.append(("media", r["id"], r["title"], r["created_at"], r["tier"], r["s"]))
    for r in con.execute("SELECT id, headline, created_at, sentiment FROM news WHERE ticker=? AND created_at BETWEEN ? AND ?",
                         (ticker, lo, hi)):
        cands.append(("benzinga", r["id"], r["headline"], r["created_at"], 2, r["sentiment"]))
    best = None
    for kind, ref, title, ts, tier, s in cands:
        if s is not None and sign and s * sign < -0.1:
            continue
        nv = novelty(con, ticker, title, ts)
        if nv < novelty_min:
            continue
        item = {"kind": kind, "ref": str(ref), "title": title, "created_at": ts, "novelty": nv, "sentiment": s, "tier": tier}
        if best is None or (tier, -nv) < (best["tier"], -best["novelty"]):
            best = item
    return best
