#!/usr/bin/env python3
"""Turn triggers into Alpaca PAPER bracket orders (v2 rules), and manage/reconcile them. Default is DRY RUN.

Usage: paper_trader.py [--live-paper] [--flat-before-close] [--now ISO]

Triggers (each considered once, if younger than signal_max_age_min = 45):
  sentiment_shock follow signals, composite follow signals, flagged IEX volume spikes on core tickers
  (direction = price move), and earnings events (earnings_drift).
Every decision is written to `decisions` (rule, action trade|hypothetical, reason) so rules can be compared:
  v2_news_momentum   trade WITH the trigger direction only if newsgate.hard_news() finds a fresh, novel tier-1/2 or
                     Benzinga headline (or an 8-K / insider-buy Form 4) within news_window_min (60) that does not
                     contradict the direction.
  v2_attention_fade  attention-only trigger (no fresh news): paper-only FADE hypothetical, never an order.
  v1_follow_spike    the previous rule (sentiment_shock follow + agreeing flagged volume spike within 60 min),
                     logged as a hypothetical so old and new rules stay comparable in evaluate.py.
  earnings_drift     reaction days 1-3 after a report, only if earnings_events.agree = 1 (surprise, real opening gap
                     and post-release news all point the same way), entered after 10:00 ET, held until 15:45 ET.
  earnings_pause     on reaction day 1 every non-earnings trigger for that ticker is logged hypothetical, no order.
Orders: bracket (limit entry at IEX mid +/- 5 bps, take-profit limit, stop-loss stop), DAY.
  stop distance = atr_mult_stop (1.5) x ATR(14) of 1-hour IEX bars, clamped to [min_stop_pct 0.4%, max_stop_pct 4%];
  take-profit = tp_mult (2) x stop distance.
  size = risk_per_trade_pct (0.5%) of equity / stop distance, capped at 5% of equity per trade and 20% total exposure
  (positions + open entry orders), whole shares.
Exits: bracket legs on Alpaca; time exit when exit_due passes (60 min, earnings: 15:45 ET) or with
  --flat-before-close <= 15 min before the close -> cancel the legs and market-close the position.
Unfilled entries are cancelled after entry_timeout_min (10). Every run first reconciles open paper orders.
Kill switch: data/KILL or trading_enabled=false -> no new entries (exits still managed).
DRY RUN (default): reads Alpaca, places/cancels nothing and writes nothing.
OBSERVE-ONLY GUARD: symbols flagged observe_only (config swiss_observe: NVS, UBS, LOGI, ONON, ALC) are refused
before any order call: triggers on them are dropped, try_trade logs nothing, place() raises ObserveOnlyRefused.
"""
import argparse, math, os, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import db, newsgate
from alpaca_client import AlpacaPaper, AlpacaError

ET = ZoneInfo("America/New_York")
OPEN = {"new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding", "pending_cancel",
        "pending_replace", "held", "calculated"}
DEFAULTS = {"trading_enabled": True, "max_trade_pct": 0.05, "max_total_pct": 0.20, "limit_offset_bps": 5,
            "max_spread_pct": 0.5, "hold_minutes": 60, "signal_max_age_min": 45, "spike_window_min": 60,
            "entry_timeout_min": 10, "allow_short": True, "flat_before_close_min": 15, "risk_per_trade_pct": 0.5,
            "atr_mult_stop": 1.5, "tp_mult": 2.0, "min_stop_pct": 0.4, "max_stop_pct": 4.0, "news_window_min": 60,
            "news_novelty_min": 0.5, "news_min_tier": 2, "earnings_hold_until_et": "15:45",
            "earnings_entry_after_et": "10:00", "bracket": True}


# ---------- pure helpers (unit-tested) ----------
def size_qty(equity, exposure, price, max_trade_pct=0.05, max_total_pct=0.20, stop_dist=None, risk_pct=None):
    """Whole shares: volatility-based (risk_pct% of equity / stop_dist) if given, always capped so the trade
    <= max_trade_pct*equity and exposure+trade <= max_total_pct*equity."""
    if not equity or equity <= 0 or not price or price <= 0:
        return 0
    room = max_total_pct * equity - abs(exposure)
    notional = min(max_trade_pct * equity, room)
    q = notional / price
    if stop_dist and stop_dist > 0 and risk_pct:
        q = min(q, (risk_pct / 100) * equity / stop_dist)
    return max(0, int(math.floor(q + 1e-9)))


def limit_price(side, bid=None, ask=None, last=None, offset_bps=5, max_spread_pct=0.5):
    ref = None
    if bid and ask and bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2
        if (ask - bid) / mid * 100 <= max_spread_pct:
            ref = mid
    if ref is None and last and last > 0:
        ref = last
    if ref is None:
        return None
    k = offset_bps / 10000
    return round(ref * (1 + k) if side == "buy" else ref * (1 - k), 2)


def atr(bars, n=14):
    """Wilder-free simple ATR over the last n true ranges of bars [{'h','l','c'}...]."""
    if len(bars) < 2:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    trs = trs[-n:]
    return sum(trs) / len(trs)


def bracket_levels(side, entry, atr_value, mult=1.5, tp_mult=2.0, min_pct=0.4, max_pct=4.0):
    """-> (stop_price, take_profit_price, stop_dist $). Stop distance clamped to [min_pct, max_pct] % of entry."""
    d = (atr_value or 0) * mult
    d = min(max(d, entry * min_pct / 100), entry * max_pct / 100)
    if side == "buy":
        return round(entry - d, 2), round(entry + tp_mult * d, 2), d
    return round(entry + d, 2), round(entry - tp_mult * d, 2), d


def exposure_of(positions, open_orders):
    """|market value| of positions + remaining notional of open limit orders in symbols without a position
    (orders in a held symbol are bracket exit legs, not new exposure)."""
    exp = sum(abs(float(p.get("market_value") or 0)) for p in positions)
    held = {p.get("symbol") for p in positions}
    for o in open_orders:
        if o.get("type") == "limit" and o.get("limit_price") and o.get("symbol") not in held:
            rem = float(o.get("qty") or 0) - float(o.get("filled_qty") or 0)
            exp += rem * float(o["limit_price"])
    return exp


class ObserveOnlyRefused(RuntimeError):
    """Raised when anything tries to place an order in an observe-only symbol."""


def assert_tradable(sym, blocked):
    if (sym or "").upper() in blocked:
        raise ObserveOnlyRefused(f"{sym} is flagged observe_only (Swiss observe arm): orders are refused")


def flip(d):
    return "short" if d == "long" else "long"


# ---------- trader ----------
class Trader:
    def __init__(self, client, con, cfg, live=False, now=None, flat_before_close=False, kill_path=None, core=None, blocked=None):
        self.c, self.con, self.cfg, self.live = client, con, {**DEFAULTS, **cfg}, live
        self.now = now or db.utcnow()
        self.flat = flat_before_close
        self.kill = kill_path or os.path.join(db.HERE, "data", "KILL")
        self.core = core or db.watchlist(True)
        self.blocked = db.observe_only_symbols() if blocked is None else {x.upper() for x in blocked}
        self.log = []

    def say(self, msg):
        self.log.append(msg); print(msg)

    def _write(self, sql, args=()):
        if self.live:
            return self.con.execute(sql, args)

    # ----- market data helpers -----
    def quote_limit(self, sym, side):
        q, last = {}, None
        try:
            q = self.c.latest_quote(sym) or {}
        except AlpacaError:
            pass
        try:
            last = float((self.c.latest_trade(sym) or {}).get("p") or 0) or None
        except AlpacaError:
            pass
        return limit_price(side, q.get("bp"), q.get("ap"), last, self.cfg["limit_offset_bps"], self.cfg["max_spread_pct"])

    def hourly_atr(self, sym):
        try:
            b = self.c.bars(sym, "1Hour", start=self.now - timedelta(days=7)).get(sym, [])
            return atr(b, 14)
        except Exception:
            return None

    # ----- 1) reconcile -----
    def reconcile(self):
        rows = self.con.execute("""SELECT * FROM paper_trades WHERE alpaca_order_id IS NOT NULL
                                   AND (status IS NULL OR status IN (%s))""" % ",".join("?" * len(OPEN)), tuple(OPEN)).fetchall()
        for r in rows:
            try:
                o = self.c.get_order(r["alpaca_order_id"])
            except AlpacaError as e:
                self.say(f"  [warn] order {r['id']} lookup failed: {e}"); continue
            st, fq = o.get("status"), float(o.get("filled_qty") or 0)
            fap = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
            self._write("UPDATE paper_trades SET status=?, filled_qty=?, filled_avg_price=?, updated_at=? WHERE id=?",
                        (st, fq, fap, db.iso(self.now), r["id"]))
            age = (self.now - db.parse_ts(r["ts"])).total_seconds() / 60
            if st in OPEN and fq == 0 and age > self.cfg["entry_timeout_min"]:
                self.say(f"  cancel stale {r['purpose']} #{r['id']} {r['ticker']} ({age:.0f} min unfilled)")
                if self.live:
                    try:
                        self.c.cancel_order(r["alpaca_order_id"])
                    except AlpacaError as e:
                        self.say(f"  [warn] cancel failed: {e}")
            if r["purpose"] == "exit" and st == "filled" and r["parent_id"]:
                self._write("UPDATE paper_trades SET closed_at=? WHERE id=?", (db.iso(self.now), r["parent_id"]))
            if st != r["status"]:
                self.say(f"  reconcile #{r['id']} {r['ticker']} {r['side']} {r['purpose']}: {r['status']} -> {st} (filled {fq:g})")

    # ----- 2) exits -----
    def manage_exits(self, positions, clock):
        pos = {p["symbol"]: p for p in positions}
        mins_to_close = None
        if clock.get("is_open") and clock.get("next_close"):
            mins_to_close = (db.parse_ts(clock["next_close"]) - self.now).total_seconds() / 60
        for e in self.con.execute("""SELECT * FROM paper_trades WHERE purpose='entry' AND closed_at IS NULL
                                     AND COALESCE(filled_qty,0) > 0""").fetchall():
            if self.con.execute("SELECT 1 FROM paper_trades WHERE parent_id=? AND purpose='exit' AND status IN (%s)"
                                % ",".join("?" * len(OPEN)), (e["id"], *OPEN)).fetchone():
                continue
            p = pos.get(e["ticker"])
            if p is None:
                self.say(f"  {e['ticker']} entry #{e['id']} has no open position (bracket leg or exit filled) -> closed")
                self._write("UPDATE paper_trades SET closed_at=? WHERE id=?", (db.iso(self.now), e["id"]))
                continue
            reason = None
            if e["exit_due"] and db.parse_ts(e["exit_due"]) <= self.now:
                reason = "time exit"
            elif self.flat and mins_to_close is not None and mins_to_close <= self.cfg["flat_before_close_min"]:
                reason = "flat before close"
            if not reason:
                continue
            if not clock.get("is_open"):
                self.say(f"  exit due for {e['ticker']} #{e['id']} ({reason}) but market closed; next run"); continue
            msg = f"{'CLOSE' if self.live else 'DRY-RUN would close'} {e['ticker']} {p['qty']} ({reason}; legs cancelled, market)"
            oid, status = None, "dry_run"
            if self.live:
                try:
                    o = self.c.close_position(e["ticker"]) or {}
                    oid, status = o.get("id"), o.get("status")
                except AlpacaError as ex:
                    self.say(f"  CLOSE FAILED {e['ticker']}: {ex}"); continue
            self.say("  " + msg)
            self._write("""INSERT INTO paper_trades(ts, ticker, side, qty, status, alpaca_order_id, signal_id, purpose, parent_id,
                           mode, notes, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (db.iso(self.now), e["ticker"], "sell" if e["side"] == "buy" else "buy", abs(float(p["qty"])), status,
                         oid, e["signal_id"], "exit", e["id"], "live-paper", reason, db.iso(self.now)))

    # ----- 3) triggers -----
    def triggers(self):
        since, now = db.iso(self.now - timedelta(minutes=self.cfg["signal_max_age_min"])), db.iso(self.now)
        out = []
        for s in self.con.execute("""SELECT * FROM signals WHERE type IN ('sentiment_shock','composite') AND variant='follow'
                                     AND ts BETWEEN ? AND ? ORDER BY ts""", (since, now)):
            out.append({"type": s["type"], "id": s["id"], "ticker": s["ticker"], "ts": s["ts"], "direction": s["direction"],
                        "desc": f"{s['type']} z={s['zscore']:+.2f}", "row": s})
        for v in self.con.execute("""SELECT * FROM volume_spikes WHERE flagged=1 AND detected_at BETWEEN ? AND ?
                                     AND ts >= ? ORDER BY ts""", (since, now, db.iso(self.now - timedelta(hours=2)))):
            if v["ticker"] in self.core:
                out.append({"type": "volume_spike", "id": v["id"], "ticker": v["ticker"], "ts": v["ts"],
                            "direction": "long" if v["direction"] == "up" else "short",
                            "desc": f"{v['timeframe']} spike x{v['ratio']:.1f} {v['ret_pct']:+.2f}%", "row": v})
        return [t for t in out if t["ticker"].upper() not in self.blocked]   # observe-only names never trigger

    def decided(self, rule, ttype, tid):
        return self.con.execute("SELECT 1 FROM decisions WHERE rule=? AND trigger_type=? AND trigger_id=?",
                                (rule, ttype, tid)).fetchone() is not None

    def decide(self, rule, trig, direction, action, reason, price=None, news=None, pt_id=None):
        self.say(f"  [{rule}] {trig['ticker']} {direction} {action}: {reason} ({trig['desc']})")
        self._write("""INSERT OR IGNORE INTO decisions(ts, ticker, rule, trigger_type, trigger_id, direction, action, reason, price,
                       news_ref, paper_trade_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (db.iso(self.now), trig["ticker"], rule, trig["type"], trig["id"], direction, action, reason, price,
                     news, pt_id))

    def earnings_state(self):
        """-> ({ticker: reaction-day index today}, [events with agree=1 and index 1..3])"""
        try:
            import earnings
            today = datetime.now(ET).date() if self.now is None else self.now.astimezone(ET).date()
            evs = self.con.execute("SELECT rowid AS rid, * FROM earnings_events WHERE reaction_date IS NOT NULL").fetchall()
            if not evs:
                return {}, []
            tdays = [d["date"] for d in self.c.calendar(str(today - timedelta(days=12)), str(today + timedelta(days=3)))]
            idx, tradable = {}, []
            for ev in evs:
                k = earnings.session_index(ev["reaction_date"], today, tdays)
                if k:
                    idx[ev["ticker"]] = k
                    if ev["agree"] == 1:
                        tradable.append((ev, k))
            return idx, tradable
        except Exception as ex:
            self.say(f"  [warn] earnings state: {ex}")
            return {}, []

    def entries(self, account, positions, open_orders, clock):
        equity = float(account.get("equity") or 0)
        state = {"exposure": exposure_of(positions, open_orders),
                 "held": {p["symbol"] for p in positions} | {o["symbol"] for o in open_orders}}
        killed = os.path.exists(self.kill) or not self.cfg["trading_enabled"]
        eidx, etrade = self.earnings_state()

        def try_trade(rule, trig, direction, news_ref, exit_due, note):
            side = "buy" if direction == "long" else "sell"
            why = None
            if trig["ticker"].upper() in self.blocked:
                self.say(f"  REFUSED {trig['ticker']}: observe_only symbol, no order, no decision row"); return
            if killed:
                why = "kill switch / trading disabled"
            elif not clock.get("is_open"):
                why = "market closed"
            elif trig["ticker"] in state["held"]:
                why = "already have position/order in ticker"
            elif side == "sell" and not self.shortable(trig["ticker"]):
                why = "not shortable / shorts disabled"
            lp = self.quote_limit(trig["ticker"], side) if not why else None
            if not why and not lp:
                why = "no usable price"
            if why:
                self.decide(rule, trig, direction, "hypothetical", f"would trade but {why}", lp, news_ref); return
            a = self.hourly_atr(trig["ticker"])
            stop, tp, dist = bracket_levels(side, lp, a, self.cfg["atr_mult_stop"], self.cfg["tp_mult"],
                                            self.cfg["min_stop_pct"], self.cfg["max_stop_pct"])
            qty = size_qty(equity, state["exposure"], lp, self.cfg["max_trade_pct"], self.cfg["max_total_pct"],
                           dist, self.cfg["risk_per_trade_pct"])
            if qty < 1:
                self.decide(rule, trig, direction, "hypothetical", "size 0 (exposure cap)", lp, news_ref); return
            pt = self.place(trig["ticker"], side, qty, lp, stop, tp, trig["id"] if trig["type"] != "earnings" else None,
                            exit_due, f"{rule}: {note}; ATR1h={a and round(a, 2)} stop {stop} tp {tp}")
            if pt is not False:
                state["exposure"] += qty * lp
                state["held"].add(trig["ticker"])
                self.decide(rule, trig, direction, "trade", note, lp, news_ref, pt)

        # earnings drift (days 1-3, agree only)
        et_now = self.now.astimezone(ET).strftime("%H:%M")
        for ev, k in etrade:
            trig = {"type": "earnings", "id": ev["rid"] * 10 + k, "ticker": ev["ticker"], "ts": db.iso(self.now),
                    "desc": f"day {k}, surprise {ev['surprise_pct']:+.1f}%, gap {ev['gap_pct']:+.2f}%, news {ev['news_sent']:+.2f}"}
            if self.decided("earnings_drift", "earnings", trig["id"]):
                continue
            if not (self.cfg["earnings_entry_after_et"] <= et_now < "15:30"):
                continue
            h, m = map(int, self.cfg["earnings_hold_until_et"].split(":"))
            due = db.iso(self.now.astimezone(ET).replace(hour=h, minute=m, second=0, microsecond=0))
            try_trade("earnings_drift", trig, ev["direction"], f"earnings {ev['report_date']}", due,
                      "surprise, gap and news agree")

        for trig in self.triggers():
            t, d = trig["ticker"], trig["direction"]
            # v1 comparability (sentiment_shock only)
            if trig["type"] == "sentiment_shock" and not self.decided("v1_follow_spike", trig["type"], trig["id"]):
                w = timedelta(minutes=self.cfg["spike_window_min"])
                st0 = db.parse_ts(trig["ts"])
                spike = self.con.execute("""SELECT id FROM volume_spikes WHERE ticker=? AND flagged=1 AND direction=?
                                            AND ts BETWEEN ? AND ?""", (t, "up" if d == "long" else "down",
                                                                         db.iso(st0 - w), db.iso(st0 + w))).fetchone()
                self.decide("v1_follow_spike", trig, d, "hypothetical",
                            "old rule WOULD trade (retired)" if spike else "old rule: no agreeing spike")
            if self.decided("v2_news_momentum", trig["type"], trig["id"]) or self.decided("v2_attention_fade", trig["type"], trig["id"]):
                continue
            if eidx.get(t) == 1:
                self.decide("v2_news_momentum", trig, d, "hypothetical", "earnings_pause (reaction session)"); continue
            news = newsgate.hard_news(self.con, t, trig["ts"], d, self.cfg["news_window_min"], self.cfg["news_novelty_min"],
                                      self.cfg["news_min_tier"])
            if news:
                ref = f"{news['kind']}:{news['ref']}"
                try_trade("v2_news_momentum", trig, d, ref, db.iso(self.now + timedelta(minutes=self.cfg["hold_minutes"])),
                          f"backed by {news['kind']} T{news['tier']} nov {news['novelty']:.2f}: {news['title'][:80]}")
            else:
                self.decide("v2_attention_fade", trig, flip(d), "hypothetical", "attention-only, no fresh novel news -> fade (paper log only)")

    def shortable(self, sym):
        if not self.cfg["allow_short"]:
            return False
        try:
            a = self.c.asset(sym)
            return bool(a.get("shortable") and a.get("easy_to_borrow"))
        except AlpacaError:
            return False

    def place(self, sym, side, qty, lp, stop, tp, signal_id, exit_due, note):
        assert_tradable(sym, self.blocked)          # hard guarantee: never reaches the client for observe-only names
        coid = f"ss-{sym}-{self.now:%Y%m%d%H%M%S}"
        msg = f"{'PLACE' if self.live else 'DRY-RUN would place'} bracket {side} {qty} {sym} limit {lp:.2f} stop {stop:.2f} tp {tp:.2f}"
        oid, status = None, "dry_run"
        if self.live:
            try:
                o = self.c.place_bracket_order(sym, qty, side, lp, tp, stop, client_order_id=coid)
                oid, status = o.get("id"), o.get("status")
            except (AlpacaError, ValueError) as e:
                self.say(f"  ORDER FAILED {sym}: {e}")
                return False
        self.say("  " + msg + (f" -> {status} {oid}" if self.live else ""))
        cur = self._write("""INSERT INTO paper_trades(ts, ticker, side, qty, limit_price, status, alpaca_order_id, signal_id,
                       purpose, client_order_id, exit_due, mode, notes, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (db.iso(self.now), sym, side, qty, lp, status, oid, signal_id, "entry", coid, exit_due,
                           "live-paper", note, db.iso(self.now)))
        return cur.lastrowid if cur else None

    def run(self):
        acct, clock = self.c.account(), self.c.clock()
        self.say(f"paper_trader v2 {'LIVE-PAPER' if self.live else 'DRY-RUN'} @ {db.local(self.now)} | equity "
                 f"${float(acct['equity']):,.2f} cash ${float(acct['cash']):,.2f} | market {'OPEN' if clock.get('is_open') else 'closed'}")
        self.reconcile()
        positions, open_orders = self.c.positions(), self.c.orders("open")
        self.manage_exits(positions, clock)
        positions, open_orders = self.c.positions(), self.c.orders("open")
        self.entries(acct, positions, open_orders, clock)
        for p in positions:
            self.say(f"  position {p['symbol']} {p['qty']} @ {float(p['avg_entry_price']):.2f} mv ${float(p['market_value']):,.0f} "
                     f"uPL {float(p['unrealized_plpc'])*100:+.2f}%")
        ex = exposure_of(positions, open_orders)
        self.say(f"  exposure ${ex:,.0f} ({ex/float(acct['equity'])*100:.1f}% of equity), {len(open_orders)} open orders")
        if self.live:
            self.con.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live-paper", action="store_true", help="actually send PAPER orders (default: dry run)")
    ap.add_argument("--flat-before-close", action="store_true")
    ap.add_argument("--now", help="override current time (ISO UTC), for testing")
    a = ap.parse_args()
    Trader(AlpacaPaper(), db.connect(), db.load_config().get("paper_trader", {}), live=a.live_paper,
           now=db.parse_ts(a.now) if a.now else None, flat_before_close=a.flat_before_close).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
