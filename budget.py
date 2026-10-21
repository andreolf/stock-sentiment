#!/usr/bin/env python3
"""X call budget ledger for the stock-sentiment project.

Hard cap: 150 X MCP calls per box-local day (Asia/Makassar, UTC+8).
Ledger: data/x_budget.json  {"YYYY-MM-DD": {"used": int, "entries": [{"ts","n","note"}]}}

Usage:
  budget.py check N          exit 0 if used+N <= cap, else exit 2 (prints remaining)
  budget.py record N note... add N calls with a note (refuses, exit 2, if it would exceed cap
                             unless --force; the calls already happened, so --force still records)
  budget.py status [--days K] show today's usage (and last K days)
"""
import json, os, sys, fcntl
from datetime import datetime
from zoneinfo import ZoneInfo

CAP = 150
TZ = ZoneInfo("Asia/Makassar")
HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.environ.get("STOCK_SENT_BUDGET") or os.path.join(HERE, "data", "x_budget.json")


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _load(f):
    f.seek(0)
    raw = f.read()
    return json.loads(raw) if raw.strip() else {}


def _open():
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    f = open(LEDGER, "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def used_today(data):
    return data.get(today(), {}).get("used", 0)


def main(argv):
    if len(argv) < 2 or argv[1] not in ("check", "record", "status"):
        print(__doc__); return 1
    cmd = argv[1]
    f = _open()
    try:
        data = _load(f)
        used = used_today(data)
        if cmd == "check":
            n = int(argv[2]) if len(argv) > 2 else 1
            ok = used + n <= CAP
            print(f"{'OK' if ok else 'OVER'}: {today()} used {used}/{CAP}, request {n}, remaining {CAP-used}")
            return 0 if ok else 2
        if cmd == "record":
            if len(argv) < 3:
                print("usage: record N note"); return 1
            force = "--force" in argv
            args = [a for a in argv[2:] if a != "--force"]
            n = int(args[0]); note = " ".join(args[1:]) or "-"
            if used + n > CAP and not force:
                print(f"REFUSED: would make {used+n}/{CAP}. If the calls already happened, re-run with --force so the ledger stays truthful.")
                return 2
            day = data.setdefault(today(), {"used": 0, "entries": []})
            day["used"] += n
            day["entries"].append({"ts": datetime.now(TZ).isoformat(timespec="seconds"), "n": n, "note": note})
            f.seek(0); f.truncate(); json.dump(data, f, indent=1); f.flush()
            print(f"recorded {n}: {today()} used {day['used']}/{CAP}, remaining {CAP-day['used']}")
            return 0
        if cmd == "status":
            k = int(argv[argv.index("--days") + 1]) if "--days" in argv else 1
            for d in sorted(data)[-k:] if k > 1 else [today()]:
                e = data.get(d, {"used": 0, "entries": []})
                print(f"{d}: used {e['used']}/{CAP}, remaining {CAP-e['used']}, {len(e['entries'])} entries")
                if k == 1:
                    for x in e["entries"]:
                        print(f"  {x['ts']}  +{x['n']}  {x['note']}")
            return 0
    finally:
        fcntl.flock(f, fcntl.LOCK_UN); f.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
