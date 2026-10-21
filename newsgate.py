"""'Real news' gate: is a move backed by a fresh, novel headline or an SEC filing?

hard_news(con, ticker, at_iso, direction=None, window_min=60, novelty_min=0.5, max_tier=2)
  -> dict(kind, ref, title, created_at, novelty, sentiment, tier) or None
Candidates inside [at - window, at + 5 min]:
  * media rows with tier <= max_tier (tier 1 Reuters/Bloomberg/WSJ/FT/CNBC, tier 2 others) and Benzinga `news` rows
    (tier 2), whose title is NOVEL: 1 - max word-Jaccard vs the ticker's headlines from 72 h to 60 min before it
    >= novelty_min (a rewrite of an older story is stale news, which tends to reverse: Tetlock 2011);
  * sec_filings: any 8-K/8-K/A, or a Form 4 with an open-market purchase (buy_value > 0).
Company focus (2026-10-21, after the NVDA short backed by 'Cyclicals Top Sectors In Wednesday Trading...'):
  a headline only qualifies if it NAMES the company (see about_company): ticker as a word / $cashtag or an alias
  from config.json newsgate.aliases. Generic market/sector recaps and movers lists (newsgate.generic_patterns) never
  qualify; index/basket headlines ('Dow ...', 'Chip Stocks ...', newsgate.index_patterns) qualify only if the
  headline itself names the company. The Benzinga `symbols` tag alone is never enough. SEC filings are exempt.
Headlines whose effective sentiment clearly contradicts the trade direction (s * dir < -0.1) are rejected.
Best = SEC filing first, then lower tier, then higher novelty.
"""
import re
from datetime import timedelta
import db

_CFG = None


def gate_cfg():
    global _CFG
    if _CFG is None:
        c = db.load_config()
        g = dict(c.get("newsgate") or {})
        al = {k.upper(): list(v) for k, v in (g.get("aliases") or {}).items()}
        for k, v in ((c.get("media") or {}).get("names") or {}).items():   # display names are aliases too
            al.setdefault(k.upper(), [])
            if v and v not in al[k.upper()]:
                al[k.upper()].append(v)
        for k, v in (((c.get("swiss_observe") or {}).get("names")) or {}).items():
            al.setdefault(k.upper(), [])
            al[k.upper()] += [a for a in [v.get("name")] + list(v.get("aliases") or []) if a and a not in al[k.upper()]]
        g["aliases"] = al
        g["generic"] = [re.compile(p, re.I) for p in g.get("generic_patterns", [])]
        g["index"] = [re.compile(p, re.I) for p in g.get("index_patterns", [])]
        _CFG = g
    return _CFG


def names_company(ticker, text, aliases=None):
    """True if `text` names the company: ticker as an upper-case word or $cashtag, or an alias (case-insensitive,
    word boundaries, possessive/hyphen suffixes allowed: "Nvidia's", "Nvidia-Based")."""
    if not text:
        return False
    t = ticker.upper()
    if re.search(rf"(?<![A-Za-z0-9])\${re.escape(t)}\b", text, re.I) or re.search(rf"(?<![A-Za-z0-9$]){re.escape(t)}\b", text):
        return True
    al = gate_cfg()["aliases"].get(t, []) if aliases is None else aliases
    return any(re.search(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", text, re.I) for a in al if a)


def about_company(ticker, headline, teaser=None, aliases=None):
    """(ok, reason): is this headline about `ticker` (not a generic recap / basket list)?"""
    g = gate_cfg()
    h = re.sub(r"\s+-\s+[^-]{2,60}$", "", headline or "")   # drop ' - Outlet' suffix
    if any(p.search(h) for p in g["generic"]):
        return False, "generic market/sector recap or movers list"
    in_head = names_company(ticker, h, aliases)
    if any(p.search(h) for p in g["index"]):
        return (True, "named in headline") if in_head else (False, "index/basket headline without company focus")
    if in_head:
        return True, "named in headline"
    if re.search(r"\b(stock|shares)\b", h, re.I):
        return False, "headline is about another company's stock"   # 'Why Is Dell Stock Surging?' for SMCI
    if names_company(ticker, teaser, aliases):
        return True, "named in teaser"
    return False, "company not named (symbols tag only)"


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


def hard_news(con, ticker, at_iso, direction=None, window_min=60, novelty_min=0.5, max_tier=2, rejected=None):
    """`rejected`: optional list that collects (title, reason) for candidates dropped by the company-focus check."""
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
        cands.append(("media", r["id"], r["title"], None, r["created_at"], r["tier"], r["s"]))
    for r in con.execute("SELECT id, headline, summary, created_at, sentiment FROM news WHERE ticker=? AND created_at BETWEEN ? AND ?",
                         (ticker, lo, hi)):
        cands.append(("benzinga", r["id"], r["headline"], r["summary"], r["created_at"], 2, r["sentiment"]))
    best = None
    for kind, ref, title, teaser, ts, tier, s in cands:
        ok, why = about_company(ticker, title, teaser)
        if not ok:
            if rejected is not None:
                rejected.append((title, why))
            continue
        if s is not None and sign and s * sign < -0.1:
            continue
        nv = novelty(con, ticker, title, ts)
        if nv < novelty_min:
            continue
        item = {"kind": kind, "ref": str(ref), "title": title, "created_at": ts, "novelty": nv, "sentiment": s, "tier": tier}
        if best is None or (tier, -nv) < (best["tier"], -best["novelty"]):
            best = item
    return best
