#!/usr/bin/env python3
"""Minimal Alpaca REST client, PAPER ONLY (stdlib urllib).

Keys come from env ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET_KEY and are never printed or logged.
Trading base is hardcoded to https://paper-api.alpaca.markets/v2; any request to another host
(e.g. live api.alpaca.markets) raises PaperOnlyError before a socket is opened.
Market data: https://data.alpaca.markets/v2 with feed=iex (free; IEX is a small slice of volume).

CLI (read-only): alpaca_client.py account | positions | orders [status] | clock | quote SYM | bars SYM [5Min] [N]
"""
import json, os, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

PAPER_TRADING = "https://paper-api.alpaca.markets/v2"
DATA = "https://data.alpaca.markets/v2"
NEWS = "https://data.alpaca.markets/v1beta1/news"
ALLOWED_PREFIXES = (PAPER_TRADING + "/", DATA + "/", NEWS + "?", NEWS + "/")
TIMEFRAMES = ("1Min", "5Min", "15Min", "1Hour", "1Day")


class PaperOnlyError(RuntimeError):
    pass


class ObserveOnlyError(PaperOnlyError):
    pass


def observe_only_symbols():
    """Symbols flagged observe_only in config.json (swiss_observe); orders in them are refused (defence in depth,
    paper_trader.py checks first)."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        c = json.load(open(os.path.join(here, "config.json")))
    except Exception:
        return set()
    out = {s.upper() for s in c.get("observe_only_symbols", [])}
    so = c.get("swiss_observe") or {}
    for sym, v in (so.get("names") or {}).items():
        if so.get("observe_only", True) or (v or {}).get("observe_only", True):
            out.add(sym.upper())
    return out


def guard_symbol(symbol):
    if str(symbol).upper() in observe_only_symbols():
        raise ObserveOnlyError(f"refusing order in observe-only symbol {symbol!r}")


class AlpacaError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"Alpaca HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


def guard_url(url):
    """Raise unless url is on the paper trading host or the market-data host."""
    if not isinstance(url, str) or not url.startswith(ALLOWED_PREFIXES):
        raise PaperOnlyError(f"refusing non-paper Alpaca URL: {url!r}")
    host = urllib.parse.urlparse(url).netloc
    if host not in ("paper-api.alpaca.markets", "data.alpaca.markets"):
        raise PaperOnlyError(f"refusing host {host!r}")
    return url


class AlpacaPaper:
    def __init__(self, key_id=None, secret=None, trading_base=PAPER_TRADING, data_base=DATA, timeout=20):
        if trading_base != PAPER_TRADING:
            raise PaperOnlyError(f"trading base must be {PAPER_TRADING}, got {trading_base!r}")
        if data_base != DATA:
            raise PaperOnlyError(f"data base must be {DATA}, got {data_base!r}")
        self._key = key_id or os.environ.get("ALPACA_PAPER_KEY_ID")
        self._secret = secret or os.environ.get("ALPACA_PAPER_SECRET_KEY")
        if not self._key or not self._secret:
            raise RuntimeError("ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET_KEY not set")
        self.timeout = timeout

    def __repr__(self):  # never leak keys
        return "AlpacaPaper(paper-api.alpaca.markets, keys=***)"

    # ---- transport ----
    def _request(self, method, url, params=None, body=None):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        guard_url(url)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret,
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            raise AlpacaError(e.code, e.read().decode(errors="replace")) from None

    def _t(self, method, path, **kw):
        return self._request(method, PAPER_TRADING + path, **kw)

    def _d(self, path, params=None):
        return self._request("GET", DATA + path, params=params)

    # ---- trading (paper) ----
    def account(self):
        return self._t("GET", "/account")

    def clock(self):
        return self._t("GET", "/clock")

    def asset(self, symbol):
        return self._t("GET", f"/assets/{symbol}")

    def portfolio_history(self, period="1M", timeframe="1D"):
        return self._t("GET", "/account/portfolio/history", params={"period": period, "timeframe": timeframe})

    def positions(self):
        return self._t("GET", "/positions")

    def orders(self, status="open", limit=100, symbols=None):
        return self._t("GET", "/orders", params={"status": status, "limit": limit, "nested": "true",
                                                  "symbols": ",".join(symbols) if symbols else None})

    def get_order(self, order_id):
        return self._t("GET", f"/orders/{order_id}")

    def place_limit_order(self, symbol, qty, side, limit_price, tif="day", client_order_id=None, extended_hours=False):
        guard_symbol(symbol)
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        q = int(qty)
        if q <= 0:
            raise ValueError("qty must be a positive whole number")
        body = {"symbol": symbol, "qty": str(q), "side": side, "type": "limit", "time_in_force": tif,
                "limit_price": f"{float(limit_price):.2f}", "extended_hours": bool(extended_hours)}
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._t("POST", "/orders", body=body)

    def place_bracket_order(self, symbol, qty, side, limit_price, take_profit, stop_loss, tif="day", client_order_id=None):
        """Limit entry + server-side take-profit (limit) and stop-loss (stop) legs (order_class=bracket)."""
        guard_symbol(symbol)
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        q = int(qty)
        if q <= 0:
            raise ValueError("qty must be a positive whole number")
        lp, tp, sl = float(limit_price), float(take_profit), float(stop_loss)
        if side == "buy" and not (sl < lp < tp) or side == "sell" and not (tp < lp < sl):
            raise ValueError(f"bracket prices inconsistent for {side}: stop {sl} limit {lp} tp {tp}")
        body = {"symbol": symbol, "qty": str(q), "side": side, "type": "limit", "time_in_force": tif,
                "limit_price": f"{lp:.2f}", "order_class": "bracket",
                "take_profit": {"limit_price": f"{tp:.2f}"}, "stop_loss": {"stop_price": f"{sl:.2f}"}}
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._t("POST", "/orders", body=body)

    def close_position(self, symbol):
        """Market-close a position (Alpaca DELETE /positions/{symbol}); cancel its open bracket legs first."""
        for o in self.orders("open", symbols=[symbol]):
            try:
                self.cancel_order(o["id"])
            except AlpacaError:
                pass
        return self._t("DELETE", f"/positions/{symbol}")

    def calendar(self, start, end):
        return self._t("GET", "/calendar", params={"start": start, "end": end})

    def cancel_order(self, order_id):
        return self._t("DELETE", f"/orders/{order_id}")

    # ---- market data (IEX) ----
    def latest_quote(self, symbol):
        return self._d(f"/stocks/{symbol}/quotes/latest", {"feed": "iex"}).get("quote", {})

    def latest_trade(self, symbol):
        return self._d(f"/stocks/{symbol}/trades/latest", {"feed": "iex"}).get("trade", {})

    def news(self, symbols, start=None, limit=50, include_content=False):
        """Benzinga headlines via Alpaca News (free). Returns list of articles, newest first."""
        if isinstance(symbols, str):
            symbols = [symbols]
        iso = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(d, datetime) else d
        r = self._request("GET", NEWS, params={"symbols": ",".join(symbols), "start": iso(start) if start else None,
                                                "limit": limit, "sort": "desc",
                                                "include_content": str(include_content).lower()})
        return r.get("news", [])

    def bars(self, symbols, timeframe="5Min", start=None, end=None, limit=10000):
        """Return {symbol: [bar,...]} (bar keys t,o,h,l,c,v,n,vw), paginating. start/end: datetime or ISO."""
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
        if isinstance(symbols, str):
            symbols = [symbols]
        iso = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(d, datetime) else d
        start = start or datetime.now(timezone.utc) - timedelta(days=3)
        out, token = {s: [] for s in symbols}, None
        while True:
            r = self._d("/stocks/bars", {"symbols": ",".join(symbols), "timeframe": timeframe, "start": iso(start),
                                         "end": iso(end) if end else None, "feed": "iex", "limit": limit,
                                         "adjustment": "raw", "sort": "asc", "page_token": token})
            for s, bs in (r.get("bars") or {}).items():
                out.setdefault(s, []).extend(bs)
            token = r.get("next_page_token")
            if not token:
                return out


def _main(argv):
    if len(argv) < 2:
        print(__doc__); return 1
    c = AlpacaPaper()
    cmd = argv[1]
    if cmd == "account":
        a = c.account()
        print(json.dumps({k: a.get(k) for k in ("status", "equity", "cash", "buying_power", "long_market_value",
                                                "short_market_value", "pattern_day_trader", "shorting_enabled",
                                                "multiplier", "currency")}, indent=1))
    elif cmd == "positions":
        print(json.dumps(c.positions(), indent=1))
    elif cmd == "orders":
        print(json.dumps(c.orders(argv[2] if len(argv) > 2 else "open"), indent=1))
    elif cmd == "clock":
        print(json.dumps(c.clock(), indent=1))
    elif cmd == "quote":
        print(json.dumps(c.latest_quote(argv[2]), indent=1))
    elif cmd == "bars":
        tf = argv[3] if len(argv) > 3 else "5Min"
        n = int(argv[4]) if len(argv) > 4 else 5
        b = c.bars(argv[2], tf, start=datetime.now(timezone.utc) - timedelta(days=5))[argv[2]]
        print(json.dumps(b[-n:], indent=1))
    else:
        print(__doc__); return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
