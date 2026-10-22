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

Liveness (2026-10-22, after a ~5 h silent outage when the box VM restarted at ~16:06-16:09 UTC+8 and killed the loop):
  * single instance: `run` holds an exclusive flock on data/collector.lock for its whole life (a second `run` exits);
  * heartbeat data/collector.heartbeat = JSON {ts, pid, phase, tick, last_tick, next_wake}, rewritten before every
    step and at least every 60 s while sleeping; `ensure` restarts when the pid is dead OR the heartbeat is older than
    STALE_S (a hung loop is SIGTERMed, then SIGKILLed), and logs the box boot time so a VM restart is visible;
  * every loop iteration is wrapped in try/except (traceback logged, retry after 60 s); log() never raises;
    SIGTERM/SIGINT/SIGHUP are logged; faulthandler dumps stacks to collector.stdout.log on SIGUSR1 / fatal errors.
"""
import fcntl, faulthandler, json, os, signal, subprocess, sys, time, traceback
from datetime import datetime, timedelta, timezone
import db

PID = os.path.join(db.HERE, "data", "collector.pid")
LOGDIR = os.path.join(db.HERE, "data", "logs")
HEART = os.path.join(db.HERE, "data", "collector.heartbeat")
LOCK = os.path.join(db.HERE, "data", "collector.lock")
STALE_S = 900          # heartbeat older than this (s) = hung/dead loop; max legit gap is one step timeout (240 s) + slack
PY = os.path.join(db.HERE, ".venv", "bin", "python")
TICK = 300
_stop = False


def logpath():
    return os.path.join(LOGDIR, f"collector-{datetime.now(db.LOCAL_TZ):%Y-%m-%d}.log")


def log(msg):
    """Append to the day's log; never raises (a full disk or bad path must not kill the loop)."""
    line = f"{datetime.now(db.LOCAL_TZ):%Y-%m-%d %H:%M:%S} UTC+8 | {msg}\n"
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(logpath(), "a") as f:
            f.write(line)
    except Exception:
        try:
            sys.stderr.write(line); sys.stderr.flush()
        except Exception:
            pass


_hb = {"pid": None, "tick": 0, "last_tick": None, "next_wake": None, "phase": "start"}


def beat(phase=None, **kw):
    """Rewrite the heartbeat (atomic replace); never raises."""
    try:
        if phase:
            _hb["phase"] = phase
        _hb.update(kw)
        _hb["pid"] = os.getpid()
        _hb["ts"] = db.iso(db.utcnow())
        tmp = HEART + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_hb, f)
        os.replace(tmp, HEART)
    except Exception:
        pass


def read_heart():
    """-> dict (ts, pid, phase, last_tick, ...) or None. Accepts the old plain-ISO format (ts = last tick)."""
    try:
        raw = open(HEART).read().strip()
    except OSError:
        return None
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) and d.get("ts") else None
    except ValueError:
        return {"ts": raw, "last_tick": raw, "pid": None} if raw else None


def heart_age(h=None, now=None):
    h = h if h is not None else read_heart()
    if not h:
        return None
    try:
        return ((now or db.utcnow()) - db.parse_ts(h["ts"])).total_seconds()
    except Exception:
        return None


def boot_time():
    try:
        for l in open("/proc/stat"):
            if l.startswith("btime "):
                return datetime.fromtimestamp(int(l.split()[1]), timezone.utc)
    except OSError:
        pass
    return None


def step(args, timeout=240):
    beat("step " + " ".join(args[:2]))
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
    except Exception as e:
        log(f"{args[0]} step error: {type(e).__name__}: {e}"); return -1


def publish():
    """Push the freshly rendered dashboard to GitHub Pages (publish_dashboard.sh rate-limits itself to one
    publish per 15 min, PNG per 6 h). Fail-safe: any error or timeout is logged and ignored."""
    sh = os.path.join(db.HERE, "publish_dashboard.sh")
    if not os.path.exists(sh) or os.path.exists(os.path.join(db.HERE, "data", "NO_PUBLISH")):
        return
    t0 = time.time()
    beat("publish")
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
    beat("exits")
    try:
        p = subprocess.run([PY, *EXITS], cwd=db.HERE, capture_output=True, text=True, timeout=120)
        out = [l.strip() for l in (p.stdout + p.stderr).strip().splitlines()
               if any(k in l for k in ("CLOSE", "reconcile", "exit", "FAILED", "Traceback", "Error", "lock", "closed"))][:8]
        log(f"exits rc={p.returncode} {time.time()-t0:.1f}s " + " / ".join(out))
        return p.returncode
    except subprocess.TimeoutExpired:
        log("exits TIMEOUT after 120s"); return -1
    except Exception as e:
        log(f"exits step error: {type(e).__name__}: {e}"); return -1


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
        beat("tick done", last_tick=db.iso(db.utcnow()))
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
    beat("tick done", last_tick=db.iso(db.utcnow()))


def loop():
    global _stop
    def h(signum, *_):
        global _stop
        _stop = True
        log(f"stop requested by signal {signum}")
    for sg in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sg, h)
    log(f"collector loop started pid={os.getpid()}")
    beat("start", tick=0)
    n = 0
    last_slow = 0.0
    while not _stop:
        try:
            n, last_slow = iteration(n, last_slow)
        except Exception as e:   # anything outside the per-step guards: log + retry, never die silently
            log(f"loop error (retry in 60s): {type(e).__name__}: {e} | " +
                " / ".join(traceback.format_exc().strip().splitlines()[-4:]))
            n += 1
            sleep_until(time.time() + 60, None)
    log("collector loop stopped")
    beat("stopped")
    try:
        if open(PID).read().strip() == str(os.getpid()):
            os.remove(PID)
    except OSError:
        pass


def iteration(n, last_slow):
    """One tick + the sleep after it. Returns (n, last_slow)."""
    c = clock()
    is_open = bool(c and c.get("is_open"))
    light = (not is_open) and time.time() - last_slow < 1800 - 30
    try:
        tick(n, is_open, light=light)
    except Exception as e:
        log(f"tick error: {type(e).__name__}: {e} | " + " / ".join(traceback.format_exc().strip().splitlines()[-3:]))
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
    beat("sleep", tick=n, next_wake=db.iso(db.utcnow() + timedelta(seconds=wait)))
    sleep_until(end, next_due() if is_open else None)
    return n, last_slow


def sleep_until(end, due):
    """Sleep until `end` (epoch s), waking for exit_due wake-ups; refreshes the heartbeat every <= 60 s."""
    last_beat = time.time()
    while not _stop and time.time() < end:
        if due is not None and db.utcnow().timestamp() >= due.timestamp() + 3:
            log(f"exit wake-up for due {db.iso(due)}")
            exits_step()
            nd = next_due()
            due = nd if nd is not None and nd > due else None   # no busy-loop on a stuck exit; next tick retries
            beat("sleep")
            continue
        if time.time() - last_beat >= 60:
            beat("sleep"); last_beat = time.time()
        time.sleep(max(0.1, min(5, end - time.time())))


def acquire_lock():
    """Exclusive non-blocking flock on data/collector.lock; returns the open file (keep it) or None if held."""
    f = open(LOCK, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def run():
    lk = acquire_lock()
    if lk is None:
        log(f"run: another collector holds {os.path.basename(LOCK)} (pidfile {read_pid()}); pid={os.getpid()} exiting")
        print("another collector is running (lock held); exiting"); return 3
    open(PID, "w").write(str(os.getpid()))
    try:
        out = open(os.path.join(LOGDIR, "collector.stdout.log"), "a")
        faulthandler.enable(file=out)
        faulthandler.register(signal.SIGUSR1, file=out, all_threads=True)
    except Exception:
        pass
    try:
        loop()
    except BaseException as e:   # should never happen (loop guards itself); make the death visible
        log(f"collector loop FATAL {type(e).__name__}: {e} | " + " / ".join(traceback.format_exc().strip().splitlines()[-4:]))
        raise
    finally:
        lk.close()
    return 0


def collector_pids():
    """All live `collector.py run` processes (pid list), from /proc."""
    me, out = os.getpid(), []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == me:
            continue
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"collector.py") for a in cmd) and b"run" in cmd and not any(a.endswith(b"nohup") for a in cmd[:1]):
            out.append(int(d))
    return sorted(out)


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
        return False


def start():
    pid = read_pid()
    if alive(pid):
        print(f"already running pid={pid}"); return 0
    if not (os.environ.get("ALPACA_PAPER_KEY_ID") and os.environ.get("ALPACA_PAPER_SECRET_KEY")):
        print("ALPACA_PAPER_* env vars missing; not starting"); return 2
    os.makedirs(LOGDIR, exist_ok=True)
    out = open(os.path.join(LOGDIR, "collector.stdout.log"), "a")
    p = subprocess.Popen(["nohup", PY, os.path.abspath(__file__), "run"], cwd=db.HERE, stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    for _ in range(50):          # `run` writes the pidfile once it holds the lock
        time.sleep(0.2)
        if (read_pid() == p.pid and (read_heart() or {}).get("pid") == p.pid) or p.poll() is not None:
            break
    ok = read_pid() == p.pid and alive(p.pid)
    print(f"started pid={p.pid} alive={ok} log={logpath()}")
    return 0 if ok else 1


def kill(pid, why):
    """SIGTERM a (hung) collector, SIGKILL after 20 s. Only pids whose cmdline is collector.py."""
    if not alive(pid):
        return
    log(f"ensure: stopping pid={pid} ({why})")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(100):
        time.sleep(0.2)
        if not alive(pid):
            return
    log(f"ensure: pid={pid} ignored SIGTERM for 20s -> SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.5)


def ensure():
    """Start the loop if it is dead OR its heartbeat is stale (> STALE_S); never creates a second loop."""
    pid, h = read_pid(), read_heart()
    age = heart_age(h)
    up = alive(pid)
    others = [q for q in collector_pids() if q != pid]
    if up and age is not None and age > STALE_S:
        kill(pid, f"heartbeat stale {age:.0f}s > {STALE_S}s, phase={h.get('phase')}")
        up = False
    if not up:
        for q in others:          # an orphan loop that lost its pidfile: it holds the lock; let it be the one
            if h and h.get("pid") == q and age is not None and age <= STALE_S and alive(q):
                open(PID, "w").write(str(q))
                log(f"ensure: adopted running collector pid={q} (pidfile was {pid})")
                return status()
            kill(q, "orphan collector with stale heartbeat")
        bt = boot_time()
        log(f"ensure: collector not running -> starting (pidfile {pid}, last heartbeat "
            f"{db.local(h['ts']) if h else 'never'}, box booted {db.local(bt) if bt else '?'}"
            f"{' = box restarted since the last heartbeat' if bt and h and bt > db.parse_ts(h['ts']) else ''})")
        r = start()
        if r:
            return r
    return status()


def status():
    pid, h = read_pid(), read_heart()
    age = heart_age(h)
    up = alive(pid)
    lt = (h or {}).get("last_tick")
    n = len(collector_pids())
    print(f"collector pid={pid} alive={up} heartbeat_age={'n/a' if age is None else f'{age:.0f}s'}"
          f"{' STALE' if age is not None and age > STALE_S else ''} phase={(h or {}).get('phase')} "
          f"last_tick={db.local(lt) if lt else 'never'} processes={n} log={logpath()}")
    return 0 if up and age is not None and age <= STALE_S else 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        return run()
    if cmd == "once":
        c = clock(); tick(0, bool(c and c.get("is_open"))); print(open(logpath()).read().splitlines()[-1]); return 0
    if cmd == "start":
        return start()
    if cmd == "ensure":
        return ensure()
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
