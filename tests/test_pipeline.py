#!/usr/bin/env python3
"""End-to-end smoke test on a throwaway DB + ledger (never touches data/).
Run: .venv/bin/python tests/test_pipeline.py   (needs network for yfinance in the evaluate step)"""
import json, os, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
tmp = tempfile.mkdtemp()
env = dict(os.environ, STOCK_SENT_DB=os.path.join(tmp, "t.db"))


def run(*args, ok=(0,)):
    p = subprocess.run([PY, *args], cwd=HERE, env=env, capture_output=True, text=True)
    if p.returncode not in ok:
        print(p.stdout, p.stderr); raise SystemExit(f"FAIL: {args} -> {p.returncode}")
    return p.stdout


def iso(d): return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
# counts: flat 100/h for 7 days, last full hour 500 -> ratio 5
buckets = [{"start": iso(now - timedelta(hours=h + 1)), "end": iso(now - timedelta(hours=h)),
            "tweet_count": 500 if h == 0 else 100} for h in range(168, -1, -1)]
cf = os.path.join(tmp, "counts.txt")
open(cf, "w").write("<fence>\n" + json.dumps({"data": buckets, "meta": {"total_tweet_count": 1}}) + "\nX reads left...\n</fence>")
print(run("ingest.py", "TEST", cf, "--query", "test"))


def posts_file(i, sent_word):
    data = [{"id": f"{i}{k:03d}", "author_id": f"u{k}", "created_at": iso(now), "text": f"batch {i} post {k} TEST {sent_word} {k*i}",
             "public_metrics": {"like_count": 1, "impression_count": 10}} for k in range(6)]
    users = [{"id": f"u{k}", "username": f"user{k}", "public_metrics": {"followers_count": 10 ** k}} for k in range(6)]
    f = os.path.join(tmp, f"p{i}.json"); json.dump({"data": data, "includes": {"users": users}, "meta": {}}, open(f, "w"))
    return f


import random
random.seed(1)
for i in range(1, 8):  # 6 calm batches then one shock batch
    shock = i == 7
    out = run("ingest.py", "TEST", posts_file(i, "great" if shock else "ok"), "--batch", f"2026-01-01T00:0{i}Z")
    exp = json.loads(run("score.py", "export", "TEST"))
    assert len(exp) == 6, exp
    scores = [{"post_id": e["post_id"], "ticker": "TEST",
               "sentiment": 0.9 if shock else round(random.uniform(-0.1, 0.1), 3), "novelty": 0.5} for e in exp]
    sf = os.path.join(tmp, f"s{i}.json"); json.dump(scores, open(sf, "w"))
    run("score.py", "apply", sf)
    run("score.py", "vader", "TEST")
    out = run("signals.py", "TEST", "--no-price", ok=(0, 10))
    print(out.strip().splitlines()[1])
assert "SIGNAL sentiment_shock TEST" in out, out
# debounce: re-running on the same shock batch should not fire again
out2 = run("signals.py", "TEST", "--no-price", ok=(0,))
assert "debounced" in out2, out2

# evaluate: insert a real-ticker signal 8 business days ago and price it
import sqlite3
con = sqlite3.connect(env["STOCK_SENT_DB"])
import pandas as pd
ts = (pd.Timestamp(now.date()) - pd.offsets.BDay(8)).strftime("%Y-%m-%dT15:00:00Z")  # weekday, 11:00 ET
con.execute("INSERT INTO signals(ts,ticker,type,variant,zscore,volume_ratio,direction,price,notes) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, "TSLA", "sentiment_shock", "follow", 2.5, 4, "long", None, "synthetic test"))
con.commit()
ev = run("evaluate.py")
print(ev)
n = con.execute("SELECT COUNT(*) FROM signal_returns").fetchone()[0]
assert n >= 3, f"expected >=3 (1h,1d,5d) evaluated horizons, got {n}"
print("ALL TESTS PASSED")
