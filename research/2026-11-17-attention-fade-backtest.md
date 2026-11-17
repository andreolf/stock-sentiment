---
type: research
project: stock-sentiment
source: backtest_fade.py on a DB snapshot of 2026-11-17 (Alpaca IEX bars), paper only
---
# attention_fade backtest + news-gate replay (2026-11-17)

Requested by Francesco on 2026-11-17 ("cambiamo strategia: backtest di attention_fade e sistema i filtri").
Research only. Live paper trading has **not** switched: `paper_trader.attention_fade_trading` is `false`.

## Setup
- Window: 2026-10-21 → 2026-11-12 (all logged triggers; 16 sessions). US watchlist only (Swiss names are never traded).
- Live rules: entry at the next 1-min IEX bar after the decision, no entries after 15:30 ET, max chase 1×ATR(1h),
  stop 1.5×ATR, TP 2× stop, 0.5% risk per trade (max 5%/trade, 20% total), one position per ticker,
  opposite-direction cooldown, 3-stop loss-streak breaker. Every fill pays slippage, Alpaca commission is $0, start is $100k.
- Exit variants: bracket_60m (the live rule), bracket_eod, bracket_1d, plus pure time exits 1h/EOD/1d.
- Event sets:
  - fade_logged: the 23 logged v2_attention_fade decisions.
  - fade_old_gate: all 31 attention-only triggers under the old gate.
  - fade_new_gate: the 92 attention-only triggers under the new gate.
  - news_logged: the 46 v2_news_momentum trades.
  - news_new_gate: the 101 triggers the new gate backs with news.
- Buy & hold over the same window: SPY −1.82%, QQQ −4.72%.

## Results at 5 bps per fill (net $ on $100k)
| set | exits | n | win | avg/trade | total | max DD | t |
|---|---|---:|---:|---:|---:|---:|---:|
| fade_logged | bracket_60m (live) | 9 | 44% | −$16.63 (−34 bps) | −$150 | $186 | −1.1 |
| fade_logged | time_1d | 4 | 50% | −$61.03 | −$244 | $436 | −0.6 |
| fade_old_gate | bracket_60m | 11 | 55% | −$12.05 | −$133 | $175 | −1.0 |
| fade_old_gate | bracket_1d | 8 | 62% | +$19.79 | +$158 | $265 | +0.4 |
| fade_new_gate | bracket_60m | 32 | 41% | −$16.36 (−34 bps) | −$523 | $555 | −2.2 |
| fade_new_gate | bracket_eod | 27 | 33% | −$29.54 | −$798 | $825 | −2.9 |
| fade_new_gate | time_1d | 21 | 52% | −$52.44 | −$1,101 | $1,602 | −1.3 |
| news_logged | bracket_60m (live) | 35 | 54% | +$11.46 (+23 bps) | +$401 | $67 | +1.4 |
| news_logged | bracket_eod | 25 | 64% | +$31.89 | +$797 | $43 | +2.4 |
| news_new_gate | bracket_60m | 22 | 59% | +$14.28 (+29 bps) | +$314 | $117 | +1.2 |
| news_new_gate | bracket_eod | 10 | 70% | +$48.10 | +$481 | $125 | +1.6 |
| news_new_gate | bracket_1d | 10 | 30% | −$116.36 | −$1,164 | $1,525 | −1.5 |

Slippage sensitivity, total $ at 2 / 5 / 10 bps:
- fade_logged bracket_60m: −123 / −150 / −193
- fade_new_gate bracket_60m: −430 / −523 / −679
- news_logged bracket_60m: +504 / +401 / +231
- news_new_gate bracket_60m: +379 / +314 / +206

By ticker (bracket_60m, 5 bps):
- fade_new_gate: TSLA −$92 (n 7), NVDA −$201 (12), PLTR −$230 (13).
- news_logged: NVDA +$366 (20), PLTR +$149 (8), TSLA −$113 (7).

## Verdict
The fade does **not** survive realistic execution. evaluate.py's +1.04% at one day (n=23) was a mark-to-market
average with three weaknesses:
- **Duplicates:** the 23 decisions are mostly copies of each other. 18 fall on Nov 5, and up to 5 share one timestamp (one TSLA move counted 7 times). Only 9 independent trades remain.
- **No trading rules:** it had no stop, no max chase and no position limits.
- **Too few sessions:** it covers only 5 sessions.

With the live rules the fade loses in 17 of 18 set/exit combinations. The one positive result (old gate + bracket_1d, n=8, t=0.4) is noise. The larger, cleaner new-gate attention-only set (n=32) loses consistently (t ≈ −2).

News momentum stays positive after costs, but it is **not proven**: n=22–35, t ≈ 1.2–1.4, and most of the profit comes from NVDA during its CEO-scandal weeks. Holding to the close (bracket_eod) looks best in sample. It was picked after the fact from 6 variants, so treat it as a hypothesis to forward-test, not a result.

**Recommendation:** do not switch to attention_fade. Keep v2_news_momentum with the new filters, keep collecting data, and forward-test the end-of-day hold in paper before any change.

Caveats:
- The sample is small: 3–4 weeks and 3 tickers.
- IEX bars are thin, 1-minute opens are an approximation of fills, and ATR comes from 1-hour IEX bars.
- For triggers that were never evaluated live, the decision time is assumed to be the trigger time.
- Classifying triggers by the new gate uses only headlines fetched by the decision time, but FinBERT scores are today's.

## Past trades replayed through the new gate (realized paper P/L from Alpaca fills)
Of the 46 trades, 31 would have been blocked (realized −$45.48 together) and 15 kept (+$286.03). The 46 actually made +$240.55.

Block reasons:
- stale (published more than 60 minutes before the decision): 10
- basket or stock-list article: 5
- opinion piece: 4
- direction not confirmed: 4
- another company as the subject: 3
- opposite-direction cooldown: 2
- other: 3

Specific trades:
- #118 NVDA long (+$93.82): blocked. AMD is the subject ("AMD upgraded … Nvidia's leadership vacuum").
- #120 NVDA long (−$44.60): blocked. The Google News item was 128 minutes old at the decision.
- #122 NVDA short (−$42.00): no longer blocked as stale. With #118 and #120 gone it is backed by a fresh headline ("Nvidia's CEO Exit Sends Shockwaves Across ETF Markets").
- #124 NVDA short (+$18.28): the Benzinga /opinion/ piece is rejected. A Benzinga feature piece on Buffett and Nvidia's board still backs it (a known gap: a person as the subject of a feature).
- #104 (late entry), #109 (basket "Best Stocks Update"), #111 (opposite-direction cooldown): blocked.

Swiss observe arm, replaying 508 valid observations:
- 20 swiss_news observations would be filtered: UBS as analyst, conference host or holder (11); another company as the subject (7); a basket article (1); and the NVS Phase-III results headline, which is no longer read as negative (1).
- The opposite-direction cooldown would have suppressed 137 observations.
