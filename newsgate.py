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
Source quality (2026-10-22, after the NVDA short 17:04 Zurich backed by the Moomoo community post
  '$NVIDIA (NVDA.US)$ haiyaaaa... uncle shaking² hand look at it. itchy hand want to click buy.' via Google News):
  every media row is re-classified at gate time with media.classify(outlet, title, url) (stored tiers are only
  demoted, never promoted): social/community/forum/UGC sources (Moomoo/Futu, Stocktwits, Reddit, TradingView, Webull,
  Tiger, X, YouTube, ...) and post-style headlines ('$NAME (TICKER.US)$', leading or multiple cashtags, chatter) -> tier
  4; Google News items from outlets not on config media.tier2_outlets -> tier 3. Benzinga headlines get the same
  headline-style check. Tier > max_tier never qualifies (listed in `rejected`).
Headlines whose effective sentiment clearly contradicts the trade direction (s * dir < -0.1) are rejected.
Best = SEC filing first, then lower tier, then higher novelty.
Headline quality (2026-11-17, approved by Francesco: "sistema i filtri"; after #118 'AMD upgraded because it will
  benefit from Nvidia's leadership vacuum', #120/#122 stale Google News items, #124 Benzinga opinion piece):
  * freshness is measured by the article PUBLISH time (media/news created_at) against the DECISION time (`now_iso`):
    older than max_age_min (newsgate.max_age_min, 60) -> rejected, even if it fell inside the trigger window (a 1-hour
    volume-spike bar starts up to an hour before it is detected, and Google News items arrive with lag). Google News rows
    without a publish time (created_at == fetched_at) are rejected (age unverifiable).
  * headline_issue(): opinion/commentary (URL /opinion/, opinion authors, Cramer, 'should you buy'...), generic market
    wraps even when they name the company ('Stocks Climb ... Despite Nvidia Woes', '...; Gold Falls Below $4000'),
    basket / stock-list articles ('NVIDIA, Caterpillar & Nike: Three Corporate Events'), analyst-as-subject for banks
    (newsgate.analyst_tickers, e.g. 'UBS bleibt bei Buy' for voestalpine is not UBS news), another company as the
    headline subject ('AMD upgraded ... Nvidia's ...') and the company only in a subordinate clause.
  * per-company direction: the headline must name the company AND its (adjusted) sentiment must point the trade's way:
    s * dir >= min_direction_sentiment (0.1). Unscored headlines never back a trade. Teaser-only mentions no longer count.
  * adjust_sentiment(): neutral clinical-trial / study-results headlines without explicit failure words are not
    negative (FinBERT reads 'Phase III ... Untersuchung' as bearish).
"""
import re
from datetime import timedelta
import db, media

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
        for k, d in DEFAULT_PATTERNS.items():
            g[k] = [re.compile(p, re.I) for p in (g.get(k + "_patterns") or d)]
        known = {k: list(v) for k, v in al.items()}
        for n in g.get("other_companies") or DEFAULT_OTHER_COMPANIES:
            known.setdefault("~" + n, [n])
        g["known"] = known
        g["analyst_tickers"] = {t.upper() for t in g.get("analyst_tickers", ["UBS"])}
        g["opinion_authors"] = [a.lower() for a in g.get("opinion_authors", DEFAULT_OPINION_AUTHORS)]
        _CFG = g
    return _CFG

# ---- headline quality (2026-11-17) ----
DEFAULT_OTHER_COMPANIES = [
    "Apple", "Microsoft", "Amazon", "Alphabet", "Google", "Intel", "Broadcom", "TSMC", "Taiwan Semiconductor", "Micron",
    "Qualcomm", "Arm Holdings", "Oracle", "Dell", "HPE", "Hewlett Packard", "IBM", "Cisco", "Salesforce", "Adobe",
    "Netflix", "Samsung", "SK Hynix", "ASML", "Marvell", "CoreWeave", "OpenAI", "Anthropic", "xAI", "SpaceX", "Rivian",
    "Lucid", "BYD", "Ford", "GM", "General Motors", "Toyota", "Volkswagen", "Uber", "Lyft", "Waymo", "Snowflake",
    "CrowdStrike", "Okta", "OKTA", "ServiceNow", "Shopify", "PayPal", "Block", "Visa", "Mastercard", "JPMorgan",
    "Goldman Sachs", "Morgan Stanley", "Citigroup", "Bank of America", "Wells Fargo", "BlackRock", "Berkshire",
    "Caterpillar", "Nike", "Walmart", "Costco", "Boeing", "Lockheed", "Amkor", "Nokia", "Ericsson", "Foxconn",
    "Lam Research", "Applied Materials", "KLA", "Synopsys", "Cadence", "Arista", "Vertiv", "C3.ai", "BigBear.ai",
    "SoundHound", "Roche", "Nestle", "Nestlé", "ABB", "Zurich Insurance", "Swiss Re", "Richemont", "Holcim", "Sika",
    "Lonza", "Givaudan", "Julius Baer", "Partners Group", "Credit Suisse", "voestalpine", "Technoprobe", "BioMarin",
    "Apollo", "Lloyds", "Standard Chartered", "Elanco", "Deutsche Börse", "Deutsche Bank", "Ströer", "AMD"]
DEFAULT_OPINION_AUTHORS = ["the arora report", "jim cramer", "cramer"]
_V = r"(?:secures?|teams?|hires?|signs?|wins?|launch(?:es)?|unveils?|posts?|reports?|raises?|cuts?|lifts?|lowers?|" \
     r"upgraded|downgraded|upgrades?|downgrades?|stock|shares|says|plans?|buys?|sells?|acquires?|agrees?|partners?|" \
     r"beats?|misses?|surges?|jumps?|falls?|drops?|slides?|soars?|rises?|gains?|climbs?|sinks?|tumbles?|rall(?:y|ies)|" \
     r"expands?|invests?|announces?|benefits?|joins?|names?|picks?|bets?|is|are|to|will|could|may|gets?|faces?)"
DEFAULT_PATTERNS = {
    "opinion": [
        r"/opinion/", r"/op-?eds?/", r"/commentary/", r"/columnists?/",
        r"^(?!.*\b(CHMP|EMA|EFSA|regulators?|positive opinion|negative opinion|scientific opinion)\b).*\bopinion\b", r"\bop-ed\b", r"\bcommentary\b",
        r"\bcolumn\b", r"\bCramer\b", r"\bJosh Brown\b", r"\bLive On CNBC\b", r"\bHosts Segment\b", r"^(why|how) I\b",
        r"\bshould you (buy|sell)\b", r"\b(is it )?time to (buy|sell)\b", r"\bbuy the dip\b", r"\bmy (top|favorite)\b",
        r"\b(could|might|may) move markets\b", r"\b(I|we) (like|love|hate|think)\b", r"\bpodcast\b",
        r"\b(bull|bear) case\b", r"\bhere'?s why\b", r"\bmust[- ]own\b", r"\bmillionaire\b", r"^prediction:",
        r"^(why|this is|here is|here's|what|is it|should|can|will)\b", r"^watch\b", r"\bvideo\b",
        r"^(seeing reports|hearing|watching|rumou?r)\b", r"\b(great|better|best) alternative\b"],
    "wrap": [
        r"^(U\.?S\.? |European |Europe's |Asian |Asia |Global |World |Swiss )?(stocks|shares|markets?|equities|futures|"
        r"wall st(reet)?\.?|dow( jones)?|s&p( 500)?|nasdaq( composite)?|tech stocks|chip stocks|stock market)\s+"
        r"(rise|rises|rose|climb|climbs|gain|gains|fall|falls|fell|slide|slides|slip|slips|drop|drops|rally|rallies|surge|"
        r"surges|sink|sinks|tumble|tumbles|jump|jumps|edge|edges|end|ends|close|closes|open|opens|mixed|higher|lower|steady|"
        r"rebound|rebounds|retreat|retreats|extend|extends|hit|hits|set|sets|wobble|wobbles|waver|waver|struggle|struggles)\b",
        r";\s*(gold|oil|bitcoin|silver|treasur\w*|dollar|yields?|crude)\b", r"\bstock market (today|news)\b",
        r"\b(morning|evening|midday|closing) (bid|wrap|briefing|bell)\b", r"\bmarkets? wrap\b", r"\btrading day\b",
        r"\bwhat'?s moving (the )?markets?\b", r"\bfutures (rise|fall|climb|slip|edge|point)\b", r"\bB\u00f6rse (aktuell|am Mittag|am Abend)\b",
        r"\bSMI (steigt|f\u00e4llt|schliesst|legt zu|gibt nach)\b"],
    "basket": [
        r"\b(two|three|four|five|six|seven|eight|nine|ten|\d+) (other |top |big |hot |best )?(other )?(ai |tech |chip )?(stocks|companies|names|tickers|picks|"
        r"corporate events|events|chip stocks|ai stocks|tech stocks|aktien|titel)\b", r"\bbest stocks\b", r"\bstocks? update\b",
        r"\btop picks?\b", r"\bwatch ?list\b", r"\bstocks to (buy|watch|own)\b", r"\bvs\.?\s"],
    "analyst": [
        r"\b{A}('s)?\s+(analysts?|strategists?|economists?|research)\b",
        r"\b{A}\s+(raises|lifts|cuts|lowers|trims|boosts|adjusts|upgrades|downgrades|initiates|reiterates|maintains|keeps|"
        r"resumes|names|picks|sees|expects|rates|starts|ends|bleibt|hebt|senkt|erh\u00f6ht|stuft|ernennt|empfiehlt|sieht|r\u00e4t|"
        r"bel\u00e4sst|best\u00e4tigt)\b",
        r"\b(by|from|at|per|according to|via|says|laut|von|durch|\u00fcberzeugt)\s+{A}\b(?![- ](Aktie|Group|shares|stock))",
        r"\b{A}[- ]([\w\u00c0-\u017f-]+[- ]){0,3}(conference|konferenz|summit|forum|event|tagung)\b",
        r"\b{A}[- ]?(lohnumfrage|umfrage|studie|survey|index|konsumindikator|prognose|wealth report|global wealth)\b",
        r"\b(lohnrunde|l\u00f6hne|lohn)\b", r"\bdiscloses change in major holdings\b", r"\b(verkaufs|kauf|empfehlungs)liste der {A}\b",
        r"\b{A}('s)? (price target|rating|top[- ]pick)\b"],
}
TRIAL = re.compile(r"\b(phase[- ]?(i{1,3}|[123])\w*|trials?|studie\w*|study|studies|readout|endpoints?|endpunkt\w*|chmp|"
                   r"clinical|klinisch\w*|patients?|patienten)\b", re.I)
TRIAL_NEG = re.compile(r"\b(fail\w*|miss\w*|halt\w*|discontinu\w*|setback|did not meet|does not meet|not meet|verfehlt|"
                       r"scheiter\w*|gestoppt|stopp\w*|r\u00fcckschlag|abgebrochen|safety (concern|signal)|death|deaths|"
                       r"todesf\u00e4ll\w*|reject\w*|abgelehnt|complete response letter|crl|negative)\b", re.I)
SUBORD = re.compile(r"\s(because|despite|amid|as|after|while|following|ahead of|trotz|w\u00e4hrend|wegen)\s", re.I)


def adjust_sentiment(title, s):
    """Neutral trial-result / study-data headlines are not bearish: clamp a negative score to 0 unless the headline has an
    explicit failure word. Positive scores and non-trial headlines are unchanged."""
    if s is None or s >= 0 or not title:
        return s
    if TRIAL.search(title) and not TRIAL_NEG.search(title):
        return 0.0
    return s


def _alias_rx(a):
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", 0 if (a.isupper() and len(a) <= 5) else re.I)


def _first_pos(text, aliases):
    pos = None
    for a in aliases:
        if not a:
            continue
        m = _alias_rx(a).search(text)
        if m and (pos is None or m.start() < pos):
            pos = m.start()
    return pos


def _own_aliases(ticker):
    return [ticker.upper()] + gate_cfg()["aliases"].get(ticker.upper(), [])


def other_companies(ticker, text):
    """[(position, name)] of other known companies named in text (watchlist/Swiss aliases + newsgate.other_companies)."""
    g, t, out = gate_cfg(), ticker.upper(), []
    own = {a.lower() for a in _own_aliases(t)}
    for k, al in g["known"].items():
        if k == t:
            continue
        al2 = [a for a in al + ([] if k.startswith("~") else [k]) if a and a.lower() not in own]
        p = _first_pos(text, al2)
        if p is not None:
            out.append((p, k.lstrip("~")))
    return sorted(out)


def headline_issue(ticker, headline, url=None, author=None):
    """-> reason string if the headline must not back a trade/observation for `ticker`, else None. Assumes the company is
    named (about_company); checks opinion, market wrap, basket, analyst-as-subject, other-company subject, subordinate."""
    g = gate_cfg()
    h = re.sub(r"\s+-\s+[^-]{2,60}$", "", headline or "").strip().strip("'\"\u2018\u2019\u201c\u201d")
    t = ticker.upper()
    if author and author.lower() in g["opinion_authors"]:
        return f"opinion piece (author {author})"
    if url and any(p.pattern.startswith("/") and p.search(url) for p in g["opinion"]):
        return "opinion / commentary piece (URL)"
    if any(p.search(h) for p in g["basket"]):
        return "basket / stock-list article"
    if any(p.search(h) for p in g["opinion"]):
        return "opinion / commentary piece"
    if any(p.search(h) for p in g["wrap"]):
        return "generic market wrap"
    others = other_companies(t, h)
    if len({n for _, n in others}) >= 2:
        return "basket / stock-list article (names %d other companies)" % len({n for _, n in others})
    if t in g["analyst_tickers"]:
        for a in _own_aliases(t):
            for pat in DEFAULT_PATTERNS["analyst"] if not g.get("analyst_patterns") else g["analyst_patterns"]:
                if re.search(pat.replace("{A}", re.escape(a)), h, re.I):
                    return f"{t} is the analyst/host/holder, not the subject"
    own = _first_pos(h, _own_aliases(t))
    if own is None:
        return None
    # leading subject: text before the first verb or colon
    m = re.search(rf"(:|\s{_V}\b)", h, re.I)
    lead = h[:m.start()] if m else h
    if others and others[0][0] < own:
        p0, name = others[0]
        pre = h[max(0, p0 - 12):p0].lower()
        if not re.search(r"\b(ex|former|ex-)\s*$", pre) and (p0 < len(lead) or own >= len(lead)):
            return f"another company ({name}) is the headline subject"
    sm = SUBORD.search(h)
    if sm and own > sm.start() and _first_pos(h[:sm.start()], _own_aliases(t)) is None and sm.start() > 0:
        return "company only in a subordinate clause"
    return None


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


def source_tier(stored_tier, outlet, title, url=None, feed=None):
    """(effective tier, reason): media.classify() can only demote the stored tier (never promote it)."""
    ct, why = media.classify(outlet, title, url, google=media.is_google_feed(feed))
    st = stored_tier if stored_tier is not None else ct
    return (st, why) if ct <= 2 else (max(st, ct), why)


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


def hard_news(con, ticker, at_iso, direction=None, window_min=60, novelty_min=0.5, max_tier=2, rejected=None, now_iso=None,
              max_age_min=None, quality=True):
    """`rejected`: optional list that collects (title, reason) for candidates dropped by the source-quality check
    (stored tier <= max_tier but re-classified above it) or the company-focus check.
    now_iso: decision time; headlines published more than max_age_min (default newsgate.max_age_min, 60) before it are
    stale. quality=False reproduces the pre-2026-11-17 gate (used only to compare old vs new decisions)."""
    at = db.parse_ts(at_iso)
    g = gate_cfg()
    now = db.parse_ts(now_iso) if now_iso else None
    max_age = max_age_min if max_age_min is not None else g.get("max_age_min", 60)
    min_dir = g.get("min_direction_sentiment", 0.1)
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
    for r in con.execute("""SELECT id, title, created_at, fetched_at, tier, outlet, url, feed, COALESCE(agent_sentiment, sentiment) s
                            FROM media WHERE ticker=? AND created_at BETWEEN ? AND ?""", (ticker, lo, hi)):
        if quality and media.is_google_feed(r["feed"]) and r["fetched_at"] and r["created_at"] == r["fetched_at"]:
            if rejected is not None:
                rejected.append((r["title"], "Google News item without publish time (age unverifiable)"))
            continue
        tier, why = source_tier(r["tier"], r["outlet"], r["title"], r["url"], r["feed"])
        if tier > max_tier:
            if rejected is not None and (r["tier"] or 99) <= max_tier:   # newly rejected by the source check
                rejected.append((r["title"], f"source T{tier}: {why}"))
            continue
        cands.append(("media", r["id"], r["title"], None, r["created_at"], tier, r["s"], r["url"], None))
    for r in con.execute("""SELECT id, headline, summary, created_at, sentiment, url, author FROM news WHERE ticker=?
                            AND created_at BETWEEN ? AND ?""", (ticker, lo, hi)):
        hs = media.headline_style(r["headline"])
        if hs:
            if rejected is not None:
                rejected.append((r["headline"], f"source T{media.TIER_UGC}: {hs}"))
            continue
        cands.append(("benzinga", r["id"], r["headline"], r["summary"], r["created_at"], 2, r["sentiment"], r["url"], r["author"]))
    best = None
    for kind, ref, title, teaser, ts, tier, s, url, author in cands:
        ok, why = about_company(ticker, title, teaser)
        if ok and quality and why == "named in teaser" and not g.get("allow_teaser_mention", False):
            ok, why = False, "company named only in the teaser (no per-company headline)"
        if ok and quality:
            age = (now - db.parse_ts(ts)).total_seconds() / 60 if now else None
            iss = headline_issue(ticker, title, url, author)
            if age is not None and age > max_age:
                ok, why = False, f"stale: published {age:.0f} min before the decision (max {max_age})"
            elif iss:
                ok, why = False, iss
        if not ok:
            if rejected is not None:
                rejected.append((title, why))
            continue
        if quality:
            s = adjust_sentiment(title, s)
            if sign and (s is None or s * sign < min_dir):
                if rejected is not None:
                    rejected.append((title, f"direction not confirmed for {ticker}: sentiment "
                                            f"{'n/a' if s is None else f'{s:+.2f}'} vs {direction}"))
                continue
        elif s is not None and sign and s * sign < -0.1:
            continue
        nv = novelty(con, ticker, title, ts)
        if nv < novelty_min:
            continue
        item = {"kind": kind, "ref": str(ref), "title": title, "created_at": ts, "novelty": nv, "sentiment": s, "tier": tier}
        if now:
            item["age_min"] = round((now - db.parse_ts(ts)).total_seconds() / 60, 1)
        if best is None or (tier, -nv) < (best["tier"], -best["novelty"]):
            best = item
    return best
