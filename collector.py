#!/usr/bin/env python3
"""Free fast loop: every 5 min during US regular hours run the zero-X-cost collectors. Never opens trades, never calls X.
EXIT ENFORCEMENT (since 2026-10-21): every open tick first runs `paper_trader.py --live-paper --exits-only
--flat-before-close` (reconcile + time exits + earnings-after-close flat-by-15:40 ET + flat 15 min before the close;
no entries, no X). Between ticks the loop also wakes at the earliest pending exit_due (+3 s) and runs that step, so
60-min time exits fire within seconds instead of waiting for the next 30-min routine.

Usage: collector.py start | stop | status | ensure | run | once
  start   launch the loop in the background (nohup-style, own session), pidfile data/collector.pid
  ensure  start it if the pidfile is missing/stale (the 30-min routine calls this first) -> prints status
  stop    SIGTERM the loop;  status  print pid / alive / last tick;  run  foreground loop;  once  one tick now

Tick (market open per Alpaca /v2/clock, America/New_York session):
  volume_alerts.py -> news.py fetch -> every 3rd tick (15 min): media.py fetch, reddit.py fetch, edgar.py fetch,
  earnings.py events -> composite.py --source collector -> dashboard.py -> publish_dashboard.sh --skip-render
  (GitHub Pages; rate-limited to 15 min inside the script; disable with `touch data/NO_PUBLISH`)
Market closed: one slow tick every 30 min (news + media + earnings events + dashboard) and sleep until min(next_open, 30 min).
Swiss observe-only arm (swiss.py collect: Swiss RSS + Benzinga for NVS/UBS/LOGI/ONON/ALC, SIX move, IEX spikes, observations;
zero X calls, never trades) runs on EVERY tick. While the US market is closed but the Swiss window is active
(weekdays 06:30-18:30 Zurich: pre-07:30 ad hoc news, SIX session 09:00-17:30, post-17:40 ad hoc), the loop wakes every
5 min for a light Swiss-only tick and does the full slow tick only every 30 min. swiss.py evaluate runs on slow ticks.
Logs: data/logs/collector-YYYY-MM-DD.log (box-local date). Each step is a subprocess with a timeout, so one
failing step never kills the loop. Needs ALPACA_PAPER_KEY_ID/SECRET in the environment that starts it.
"""
import os, signal, subprocess, sys, time
from datetime import datetime, timedelta, timezone
import db

PID = os.path.join(db.HERE, "data", "collector.pid")
LOGDIR = os.path.join(db.HERE, "data", "logs")
HEART = os.path.join(db.HERE, "data", "collector.heartbeat")
PY = os.path.join(db.HERE, ".venv", "bin", "python")
TICK = 300
_stop = False


def logpath():
    return os.path.join(LOGDIR, f"collector-{datetime.now(db.LOCAL_TZ):%Y-%m-%d}.log")


def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    with open(logpath(), "a") as f:
        f.write(f"{datetime.now(db.LOCAL_TZ):%Y-%m-%d %H:%M:%S} UTC+8 | {msg}\n")


def step(args, timeout=240):
    t0 = time.time()
    try:
        p = subprocess.run([PY, *args], cwd=db.HERE, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip().splitlines()
        keep = [l for l in out if l.startswith(("SPIKE", "COMPOSITE", "media:", "news:", "reddit", "composite", "[warn]", "  [warn]", "edgar", "  TSLA", "  META",
                                                 "Traceback", "dashboard:", "swiss", "SWISS", "  NVS", "  UBS", "  LOGI", "  ALC")) or "Error" in l][:12]
        log(f"{args[0]} rc={p.returncode} {time.time()-t0:.1f}s " + " / ".join(keep))
        return p.returncode
    except subprocess.TimeoutExpired:
        log(f"{args[0]} TIMEOUT after {timeout}s"); return -1


def publish():
    """Push the freshly rendered dashboard to GitHub Pages (publish_dashboard.sh rate-limits itself to one
    publish per 15 min, PNG per 6 h). Fail-safe: any error or timeout is logged and ignored."""
    sh = os.path.join(db.HERE, "publish_dashboard.sh")
    if not os.path.exists(sh) or os.path.exists(os.path.join(db.HERE, "data", "NO_PUBLISH")):
        return
    t0 = time.time()
    try:
        p = subprocess.run(["bash", sh, "--skip-render"], cwd=db.HERE, capture_output=True, text=True, timeout=150)
        msg = " / ".join((p.stdout + p.stderr).strip().splitlines()[-3:])
        log(f"publish_dashboard.sh rc={p.returncode} {time.time()-t0:.1f}s {msg}")
    except Exception as e:
        log(f"publish_dashboard.sh error: {e}")


def clock():
    try:
        from alpaca_client import AlpacaPaper
        return AlpacaPaper().clock()
    except Exception as e:
        log(f"clock error: {e}"); return None


def swiss_window(now=None):
    from zoneinfo import ZoneInfo
    z = (now or db.utcnow()).astimezone(ZoneInfo("Europe/Zurich"))
    return z.weekday() < 5 and "06:30" <= z.strftime("%H:%M") < "18:30"


def swiss_enabled():
    return bool((db.load_config().get("swiss_observe") or {}).get("enabled"))


EXITS = ["paper_trader.py", "--live-paper", "--exits-only", "--flat-before-close"]


def exits_step():
    """Paper-only exit enforcement; logged with the CLOSE/reconcile lines."""
    t0 = time.time()
    try:
        p = subprocess.run([PY, *EXITS], cwd=db.HERE, capture_output=True, text=True, timeout=120)
        out = [l.strip() for l in (p.stdout + p.stderr).strip().splitlines()
               if any(k in l for k in ("CLOSE", "reconcile", "exit", "FAILED", "Traceback", "Error", "lock", "closed"))][:8]
        log(f"exits rc={p.returncode} {time.time()-t0:.1f}s " + " / ".join(out))
        return p.returncode
    except subprocess.TimeoutExpired:
        log("exits TIMEOUT after 120s"); return -1


def next_due():
    """Earliest pending exit wake-up (UTC datetime) or None; never raises."""
    try:
        import paper_trader
        con = db.connect()
        try:
            return paper_trader.next_exit_due(con)
        finally:
            con.close()
    except Exception as e:
        log(f"next_due error: {e}"); return None


def tick(n, is_open, light=False):
    if light:   # US closed, Swiss window: Swiss arm only
        if swiss_enabled():
            step(["swiss.py", "collect"], timeout=240)
        with open(HEART, "w") as f:
            f.write(db.iso(db.utcnow()))
        return
    if is_open:
        exits_step()                       # first: exits must never wait behind slow collectors
        step(["volume_alerts.py"])
        step(["news.py", "fetch"])
        if n % 3 == 0:
            step(["media.py", "fetch"])
            step(["reddit.py", "fetch"])
            step(["edgar.py", "fetch"])
            step(["earnings.py", "events"])
        step(["composite.py", "--source", "collector"])
        if swiss_enabled():
            step(["swiss.py", "collect"], timeout=240)
    else:
        step(["news.py", "fetch"])
        step(["media.py", "fetch"])
        step(["earnings.py", "events"])
        if swiss_enabled():
            step(["swiss.py", "collect"], timeout=240)
            step(["swiss.py", "evaluate"], timeout=240)
    step(["dashboard.py"], timeout=120)
    publish()
    with open(HEART, "w") as f:
        f.write(db.iso(db.utcnow()))


def loop():
    global _stop
    def h(*_):
        global _stop
        _stop = True
    signal.signal(signal.SIGTERM, h); signal.signal(signal.SIGINT, h)
    log(f"collector loop started pid={os.getpid()}")
    n = 0
    last_slow = 0.0
    while not _stop:
        c = clock()
        is_open = bool(c and c.get("is_open"))
        light = (not is_open) and time.time() - last_slow < 1800 - 30
        try:
            tick(n, is_open, light=light)
        except Exception as e:
            log(f"tick error: {e}")
        if not is_open and not light:
            last_slow = time.time()
        n += 1
        now = db.utcnow()
        if is_open:
            nxt = (now.timestamp() // TICK + 1) * TICK + 5          # next 5-min boundary (+5 s)
            wait = nxt - now.timestamp()
        else:
            wait = 1800
            if c and c.get("next_open"):
                wait = max(30, min(1800, (db.parse_ts(c["next_open"]) - now).total_seconds() + 10))
            if swiss_window(now) and swiss_enabled():
                wait = min(wait, (now.timestamp() // TICK + 1) * TICK + 5 - now.timestamp())
        log(f"tick {n} done (market {'open' if is_open else 'closed'}{', swiss-only' if light else ''}); sleeping {wait:.0f}s")
        end = time.time() + wait
        due = next_due() if is_open else None
        while not _stop and time.time() < end:
            if due is not None and db.utcnow().timestamp() >= due.timestamp() + 3:
                log(f"exit wake-up for due {db.iso(due)}")
                exits_step()
                nd = next_due()
                due = nd if nd is not None and nd > due else None   # no busy-loop on a stuck exit; next tick retries
                continue
            time.sleep(max(0.1, min(5, end - time.time())))
    log("collector loop stopped")
    try:
        if open(PID).read().strip() == str(os.getpid()):
            os.remove(PID)
    except OSError:
        pass


def read_pid():
    try:
        return int(open(PID).read().strip())
    except (OSError, ValueError):
        return None


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # guard against pid reuse
        return "collector.py" in open(f"/proc/{pid}/cmdline").read()
    except OSError:
        return True


def start():
    pid = read_pid()
    if alive(pid):
        print(f"already running pid={pid}"); return 0
    if not (os.environ.get("ALPACA_PAPER_KEY_ID") and os.environ.get("ALPACA_PAPER_SECRET_KEY")):
        print("ALPACA_PAPER_* env vars missing; not starting"); return 2
    os.makedirs(LOGDIR, exist_ok=True)
    out = open(os.path.join(LOGDIR, "collector.stdout.log"), "a")
    p = subprocess.Popen(["nohup", PY, os.path.abspath(__file__), "run"], cwd=db.HERE, stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    open(PID, "w").write(str(p.pid))
    time.sleep(1.5)
    print(f"started pid={p.pid} alive={alive(p.pid)} log={logpath()}")
    return 0 if alive(p.pid) else 1


def status():
    pid = read_pid()
    hb = open(HEART).read().strip() if os.path.exists(HEART) else None
    print(f"collector pid={pid} alive={alive(pid)} last_tick={db.local(hb) if hb else 'never'} log={logpath()}")
    return 0 if alive(pid) else 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        open(PID, "w").write(str(os.getpid())); loop(); return 0
    if cmd == "once":
        c = clock(); tick(0, bool(c and c.get("is_open"))); print(open(logpath()).read().splitlines()[-1]); return 0
    if cmd == "start":
        return start()
    if cmd == "ensure":
        if not alive(read_pid()):
            log("ensure: collector not running -> starting")
            r = start()
            if r:
                return r
        return status()
    if cmd == "stop":
        pid = read_pid()
        if alive(pid):
            os.kill(pid, signal.SIGTERM); print(f"sent SIGTERM to {pid}")
        else:
            print("not running")
        return 0
    if cmd == "status":
        return status()
    print(__doc__); return 1


if __name__ == "__main__":
    sys.exit(main())
