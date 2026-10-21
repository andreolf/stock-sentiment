"""Shared SQLite helpers for stock-sentiment. All stored timestamps are ISO-8601 UTC ('...Z')."""
import json, os, sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("STOCK_SENT_DB") or os.path.join(HERE, "data", "sentiment.db")
LOCAL_TZ = ZoneInfo("Asia/Makassar")
TICKERS = ["TSLA", "NVDA", "PLTR"]


def watchlist(core_only=False):
    c = load_config()
    core = c.get("watchlist_core", TICKERS)
    return core if core_only or not c.get("use_extra") else core + c.get("watchlist_extra", [])

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id TEXT NOT NULL, ticker TEXT NOT NULL, author TEXT, followers INTEGER, text TEXT,
  created_at TEXT, url TEXT, fetched_at TEXT,
  author_id TEXT, likes INTEGER, impressions INTEGER,
  quality INTEGER DEFAULT 1, quality_reason TEXT,
  PRIMARY KEY (id, ticker));
-- which ingest batch(es) a post was seen in; signals use the latest batch per ticker
CREATE TABLE IF NOT EXISTS post_batches (
  post_id TEXT NOT NULL, ticker TEXT NOT NULL, batch TEXT NOT NULL,
  PRIMARY KEY (post_id, ticker, batch));
CREATE TABLE IF NOT EXISTS scores (
  post_id TEXT NOT NULL, ticker TEXT NOT NULL, sentiment REAL CHECK (sentiment BETWEEN -1 AND 1),
  novelty REAL CHECK (novelty BETWEEN 0 AND 1), scorer TEXT NOT NULL, scored_at TEXT,
  PRIMARY KEY (post_id, ticker, scorer));
CREATE TABLE IF NOT EXISTS counts (
  ticker TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
  post_count INTEGER, fetched_at TEXT, query TEXT,
  PRIMARY KEY (ticker, window_start, window_end));
-- one row per signals.py run per ticker (history for the sentiment z-score)
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, batch TEXT, scorer TEXT,
  n_posts INTEGER, avg_sentiment REAL, w_sentiment REAL, sent_z REAL,
  cur_rate REAL, volume_ratio REAL, volume_z REAL, price REAL, status TEXT);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, type TEXT, variant TEXT,
  zscore REAL, volume_ratio REAL, direction TEXT, price REAL, notes TEXT, run_id INTEGER);
CREATE TABLE IF NOT EXISTS signal_returns (
  signal_id INTEGER, horizon TEXT, entry_price REAL, exit_price REAL, ret REAL,
  spy_ret REAL, excess_signed REAL, computed_at TEXT,
  PRIMARY KEY (signal_id, horizon));
CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, side TEXT, qty REAL,
  limit_price REAL, status TEXT, alpaca_order_id TEXT, signal_id INTEGER);
CREATE TABLE IF NOT EXISTS volume_spikes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ticker TEXT NOT NULL, timeframe TEXT NOT NULL,
  detected_at TEXT, session_date TEXT, volume REAL, baseline_avg REAL, baseline_n INTEGER, ratio REAL,
  ret_pct REAL, direction TEXT, price REAL, flagged INTEGER, notes TEXT,
  UNIQUE (ticker, timeframe, ts));
CREATE TABLE IF NOT EXISTS news (
  id TEXT NOT NULL, ticker TEXT NOT NULL, created_at TEXT, headline TEXT, summary TEXT, source TEXT,
  author TEXT, url TEXT, symbols TEXT, fetched_at TEXT, sentiment REAL, scorer TEXT,
  PRIMARY KEY (id, ticker));
CREATE TABLE IF NOT EXISTS reddit_posts (
  id TEXT NOT NULL, ticker TEXT NOT NULL, subreddit TEXT, title TEXT, selftext TEXT, author TEXT,
  score INTEGER, num_comments INTEGER, created_at TEXT, url TEXT, fetched_at TEXT,
  sentiment REAL, scorer TEXT, PRIMARY KEY (id, ticker));
CREATE TABLE IF NOT EXISTS reddit_counts (
  ts TEXT NOT NULL, ticker TEXT NOT NULL, posts_scanned INTEGER, mentions INTEGER,
  mentions_24h INTEGER, avg_vader REAL, PRIMARY KEY (ts, ticker));
CREATE TABLE IF NOT EXISTS media (
  id TEXT NOT NULL, ticker TEXT NOT NULL, created_at TEXT, outlet TEXT, tier INTEGER, title TEXT, url TEXT,
  feed TEXT, title_norm TEXT, fetched_at TEXT, sentiment REAL, scorer TEXT, agent_sentiment REAL,
  PRIMARY KEY (id, ticker));
CREATE INDEX IF NOT EXISTS media_tn ON media(ticker, title_norm);
-- latest volume ratio per ticker/timeframe from volume_alerts (spike or not), for the composite
CREATE TABLE IF NOT EXISTS volume_state (
  ticker TEXT NOT NULL, timeframe TEXT NOT NULL, ts TEXT, ratio REAL, ret_pct REAL, updated_at TEXT,
  PRIMARY KEY (ticker, timeframe));
CREATE TABLE IF NOT EXISTS composite (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, source TEXT,
  x_sent REAL, x_vol REAL, reddit_chg REAL, media_sent REAL, media_cnt_chg REAL, price_vol REAL,
  z_x_sent REAL, z_x_vol REAL, z_reddit_chg REAL, z_media_sent REAL, z_media_cnt_chg REAL, z_price_vol REAL,
  sent_dir REAL, composite REAL, n_media INTEGER, zmode TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS sec_filings (
  accession TEXT PRIMARY KEY, ticker TEXT, cik TEXT, form TEXT, filed_at TEXT, items TEXT, url TEXT,
  insider TEXT, buy_shares REAL, buy_value REAL, sell_value REAL, fetched_at TEXT, notes TEXT);
-- every trading-rule decision (traded or hypothetical), for apples-to-apples evaluation across rules
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, rule TEXT, trigger_type TEXT, trigger_id INTEGER,
  direction TEXT, action TEXT, reason TEXT, price REAL, news_ref TEXT, paper_trade_id INTEGER,
  UNIQUE (rule, trigger_type, trigger_id));
CREATE TABLE IF NOT EXISTS decision_returns (
  decision_id INTEGER, horizon TEXT, ret REAL, net_signed REAL, spy_ret REAL, qqq_ret REAL,
  excess_spy REAL, excess_qqq REAL, computed_at TEXT, PRIMARY KEY (decision_id, horizon));
CREATE TABLE IF NOT EXISTS random_baseline (
  decision_id INTEGER, k INTEGER, ts TEXT, horizon TEXT, net_signed REAL, PRIMARY KEY (decision_id, k, horizon));
CREATE TABLE IF NOT EXISTS earnings_events (
  ticker TEXT NOT NULL, report_date TEXT NOT NULL, time_of_day TEXT, eps_actual REAL, eps_estimate REAL,
  surprise_pct REAL, reaction_date TEXT, prev_close REAL, open_price REAL, gap_pct REAL, news_sent REAL,
  n_news INTEGER, direction TEXT, agree INTEGER, source TEXT, computed_at TEXT, notes TEXT,
  PRIMARY KEY (ticker, report_date));
-- ===== Swiss observe-only arm (swiss.py / swiss_news.py): never traded =====
-- SIX home-line snapshot per collector tick (yfinance .SW, ~15-20 min delayed) and the implied gap vs the US line
CREATE TABLE IF NOT EXISTS six_moves (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ticker TEXT NOT NULL, six_symbol TEXT, zrh_date TEXT,
  six_last REAL, six_bar_ts TEXT, delay_min REAL, six_prev_close REAL, six_chg_pct REAL, fx_chfusd REAL,
  us_prev_close REAL, us_prev_date TEXT, implied_us REAL, implied_gap_pct REAL, six_state TEXT, us_open INTEGER, notes TEXT);
CREATE INDEX IF NOT EXISTS six_moves_t ON six_moves(ticker, ts);
-- 'SIX move -> US open' test: one row per US session and ticker (pre-open implied gap vs actual open gap)
CREATE TABLE IF NOT EXISTS six_open_tests (
  us_date TEXT NOT NULL, ticker TEXT NOT NULL, snap_ts TEXT, six_chg_pct REAL, implied_gap_pct REAL,
  us_prev_close REAL, us_open REAL, open_gap_pct REAL, us_close REAL, open_to_close_pct REAL, computed_at TEXT,
  PRIMARY KEY (us_date, ticker));
CREATE TABLE IF NOT EXISTS swiss_iex (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ticker TEXT NOT NULL, timeframe TEXT NOT NULL, detected_at TEXT,
  volume REAL, baseline_avg REAL, ratio REAL, ret_pct REAL, price REAL, flagged INTEGER, UNIQUE (ticker, timeframe, ts));
-- hypothetical observations (direction, entry, reason); observe_only is always 1: nothing here is ever an order
CREATE TABLE IF NOT EXISTS swiss_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ticker TEXT NOT NULL, six_symbol TEXT, rule TEXT NOT NULL,
  trigger_type TEXT NOT NULL, trigger_ref TEXT NOT NULL, direction TEXT, reason TEXT, news_ref TEXT, window TEXT,
  entry_basis TEXT, entry_ts TEXT, entry_price REAL, six_chg_pct REAL, implied_gap_pct REAL,
  observe_only INTEGER NOT NULL DEFAULT 1 CHECK (observe_only = 1),
  UNIQUE (rule, trigger_type, trigger_ref));
CREATE TABLE IF NOT EXISTS swiss_obs_returns (
  obs_id INTEGER NOT NULL, horizon TEXT NOT NULL, entry_price REAL, exit_price REAL, ret REAL, net_signed REAL,
  spy_ret REAL, excess_spy REAL, computed_at TEXT, PRIMARY KEY (obs_id, horizon));
CREATE TABLE IF NOT EXISTS swiss_random (
  obs_id INTEGER NOT NULL, k INTEGER NOT NULL, horizon TEXT NOT NULL, ts TEXT, net_signed REAL,
  PRIMARY KEY (obs_id, k, horizon));
-- headlines removed from `media` as false positives (copied here verbatim with a reason; never deleted outright)
CREATE TABLE IF NOT EXISTS media_rejected (
  id TEXT NOT NULL, ticker TEXT NOT NULL, created_at TEXT, outlet TEXT, tier INTEGER, title TEXT, url TEXT, feed TEXT,
  sentiment REAL, scorer TEXT, novelty REAL, lang TEXT, fetched_at TEXT, reason TEXT, rejected_at TEXT,
  PRIMARY KEY (id, ticker));
CREATE TABLE IF NOT EXISTS earnings (
  ticker TEXT NOT NULL, date TEXT NOT NULL, time_of_day TEXT, eps_estimate REAL, source TEXT,
  fetched_at TEXT, PRIMARY KEY (ticker, date));
"""

# columns added after the first schema version (applied idempotently in connect())
MIGRATIONS = {
    "paper_trades": [("purpose", "TEXT"), ("parent_id", "INTEGER"), ("client_order_id", "TEXT"),
                     ("filled_qty", "REAL"), ("filled_avg_price", "REAL"), ("exit_due", "TEXT"),
                     ("closed_at", "TEXT"), ("mode", "TEXT"), ("notes", "TEXT"), ("updated_at", "TEXT")],
    "reddit_counts": [("source", "TEXT"), ("upvotes", "INTEGER"), ("rank", "INTEGER")],
    "media": [("novelty", "REAL"), ("lang", "TEXT")],
    # status NULL/'valid' = counts; 'invalid' = false positive, kept for audit but excluded from evaluation
    "swiss_observations": [("status", "TEXT"), ("excluded_reason", "TEXT"), ("excluded_at", "TEXT")],
}


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    con.commit()
    return con


def load_config():
    p = os.path.join(HERE, "config.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def observe_only_symbols(conf=None):
    """US symbols that must NEVER be traded (config swiss_observe.names with observe_only, plus an optional
    top-level 'observe_only_symbols' list). Default for a listed Swiss name is observe-only."""
    c = load_config() if conf is None else conf
    out = {s.upper() for s in c.get("observe_only_symbols", [])}
    so = c.get("swiss_observe") or {}
    for sym, v in (so.get("names") or {}).items():
        if so.get("observe_only", True) or (v or {}).get("observe_only", True):
            out.add(sym.upper())
    return out


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def local(s_or_dt):
    dt = parse_ts(s_or_dt) if isinstance(s_or_dt, str) else s_or_dt
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M UTC+8")


def load_x_json(path):
    """Load an X tool result. Accepts plain JSON, or the agent-tools file format
    (JSON object wrapped in other text, e.g. untrusted-data fences and a trailing
    'X reads left...' line). Returns the first top-level JSON object found."""
    s = open(path, encoding="utf-8").read()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    i = s.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(s[i:])
            if isinstance(obj, dict) and ("data" in obj or "meta" in obj):
                return obj
        except json.JSONDecodeError:
            pass
        i = s.find("{", i + 1)
    raise ValueError(f"no X JSON object found in {path}")


def latest_price(ticker):
    """Last traded price (incl. pre/post market) via yfinance; None on failure."""
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True)
        if h is None or h.empty:
            h = yf.Ticker(ticker).history(period="5d", interval="1d")
        return float(h["Close"].dropna().iloc[-1]) if not h.empty else None
    except Exception as e:  # network/yahoo hiccups must not kill a run
        print(f"  [warn] price fetch failed for {ticker}: {e}")
        return None
