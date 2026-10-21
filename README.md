# stock-sentiment: X + media + Reddit + volume sentiment, news-gated, paper trading only

**Live dashboard:** https://andreolf.github.io/stock-sentiment/ (times in Europe/Zurich, refreshed about every 15 min)

A research desk that asks one question: *does a burst of attention and sentiment around a stock tell you anything
about the next minutes, hours or days?* It collects free signals, turns them into a composite score, and tests
trading rules on an **Alpaca paper account only**. There is no live-money path in the code.

> **Disclaimer.** This is a personal research project, **not financial advice**. It trades **paper money only**
> (Alpaca paper API; the client refuses any other host). Results so far are a handful of events and mean
> nothing statistically. Don't trade on anything you see here.

## What it watches

- **Core tickers (X + paper trading):** TSLA, NVDA, PLTR
- **Volume watchlist:** AMD, MSTR, HOOD, COIN, META, SMCI
- **Swiss observe-only arm:** NVS (Novartis), UBS, LOGI (Logitech), ALC (Alcon), ONON (On Holding). Signals are
  logged and evaluated, but **never traded**: the trader and the API client both refuse these symbols.

## Signals

| Source | What | Module |
|---|---|---|
| X (Twitter) | hourly post counts + recent posts, scored for sentiment toward the stock (agent judgment, VADER baseline), weighted by author reach and novelty | `ingest.py`, `score.py`, `signals.py` |
| Media | RSS from Reuters, Bloomberg, WSJ, FT, CNBC, MarketWatch, Barron's, Yahoo, Seeking Alpha (+ Electrek/Teslarati for TSLA), scored with FinBERT | `media.py`, `finsent.py` |
| News | Benzinga headlines via Alpaca News | `news.py` |
| Reddit | mention counts via the ApeWisdom public API | `reddit.py` |
| Volume | Alpaca IEX bars: 5m/15m/1h volume vs the same time of day over the prior 10 sessions | `volume_alerts.py` |
| Filings / earnings | SEC 8-K + Form 4 (when reachable), Nasdaq earnings calendar, EPS surprise + opening gap | `edgar.py`, `earnings.py` |
| Swiss | SIX moves (yfinance `.SW`, delayed), SER insider RSS, cash.ch, finews, NZZ, Google News CH | `swiss.py`, `swiss_news.py` |

`composite.py` z-scores X sentiment, X volume, Reddit change, media sentiment, media count change and price volume:
sentiment sets the direction, attention amplifies it.

## Trading rules (paper)

- **v2_news_momentum**: trade *with* a sentiment shock / composite / volume spike only if a fresh (≤ 60 min), novel
  tier-1/2 or Benzinga headline (or an 8-K / insider buy) backs it (`newsgate.py`).
- **v2_attention_fade**: attention without news → logged as a paper-only fade hypothetical, never an order.
- **earnings_drift**: days 1–3 after a report, only if EPS surprise, opening gap and post-release news agree.
- Orders are Alpaca **paper** brackets: limit entry, stop = 1.5 × ATR(14) on 1h bars (0.4–4%), take-profit = 2 × stop.
  Risk 0.5% of equity per trade, caps 5% per trade and 20% total. Kill switch: `touch data/KILL`.
- Every decision (traded or hypothetical) is evaluated net of 3 bps/side vs a random-entry null and SPY/QQQ (`evaluate.py`).

## Architecture

```
X (agent-called MCP tools, 150 calls/day budget) ──► ingest.py ─► score.py ─► signals.py ─┐
Media / Benzinga / Reddit / IEX volume / SEC / earnings (collector.py, every 5 min) ───────┼─► composite.py
                                                                                          ▼
                                                     paper_trader.py (news gate, brackets, Alpaca PAPER)
                                                                                          ▼
                                     SQLite data/sentiment.db ─► evaluate.py ─► dashboard.py ─► docs/ (GitHub Pages)
```

- Scripts never call X; X data comes from an agent run that respects a hard daily call budget (`budget.py`).
- `collector.py` is a free background loop (no X calls, never trades) that refreshes the free sources, renders the
  dashboard and publishes it with `publish_dashboard.sh`.
- `RUNBOOK.md` is the full per-run procedure.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install yfinance pandas vaderSentiment transformers torch feedparser requests
export ALPACA_PAPER_KEY_ID=...        # paper keys only, from env; never committed
export ALPACA_PAPER_SECRET_KEY=...
.venv/bin/python collector.py start   # free 5-min loop (sources, composite, dashboard, publish)
.venv/bin/python dashboard.py         # render dashboard/index.html + dashboard/latest.png
.venv/bin/python paper_trader.py      # dry run by default; --live-paper places PAPER orders
.venv/bin/python -m unittest discover -s tests
./publish_dashboard.sh                # render, copy into docs/, commit + push only if changed
```

## What is not in this repo

The SQLite database, raw X results, agent score files, logs, the X budget ledger and any `.env` live in `data/`
and are git-ignored. No API keys are stored anywhere in the code: Alpaca paper keys come only from the environment.
