"""collector.py liveness (2026-10-22): the box VM restarted ~16:06-16:09 UTC+8 and killed the loop silently; nothing
restarted it for ~5 h. Heartbeat JSON + stale detection in `ensure`, single-instance flock, loop survives exceptions."""
import os, sys, json, tempfile, unittest
from datetime import timedelta
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("STOCK_SENT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))   # never the live DB
import db, collector

# 2026-10-22: never run against the live DB (an import-order slip once let test_trader.reset() wipe data/sentiment.db)
if os.path.realpath(db.DB_PATH) == os.path.realpath(os.path.join(db.HERE, "data", "sentiment.db")):
    raise RuntimeError("tests resolved STOCK_SENT_DB to the live data/sentiment.db; refusing to run")


class Liveness(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.p = [mock.patch.object(collector, "HEART", os.path.join(d, "hb")),
                  mock.patch.object(collector, "PID", os.path.join(d, "pid")),
                  mock.patch.object(collector, "LOCK", os.path.join(d, "lock")),
                  mock.patch.object(collector, "LOGDIR", d)]
        for x in self.p:
            x.start()
        self.logs = []
        self.lp = mock.patch.object(collector, "log", lambda m: self.logs.append(m)); self.lp.start()

    def tearDown(self):
        for x in self.p + [self.lp]:
            x.stop()

    def write_hb(self, age_s, pid=4242):
        json.dump({"ts": db.iso(db.utcnow() - timedelta(seconds=age_s)), "pid": pid, "phase": "sleep"}, open(collector.HEART, "w"))

    def test_heartbeat_roundtrip_and_old_format(self):
        collector.beat("sleep", tick=3)
        h = collector.read_heart()
        self.assertEqual((h["pid"], h["phase"], h["tick"]), (os.getpid(), "sleep", 3))
        self.assertLess(collector.heart_age(h), 5)
        open(collector.HEART, "w").write("2026-10-22T08:05:35Z")        # pre-2026-10-22 plain ISO heartbeat
        self.assertEqual(collector.read_heart()["last_tick"], "2026-10-22T08:05:35Z")
        os.remove(collector.HEART)
        self.assertIsNone(collector.read_heart()); self.assertIsNone(collector.heart_age())

    def test_lock_is_exclusive(self):
        a = collector.acquire_lock()
        self.assertIsNotNone(a)
        self.assertIsNone(collector.acquire_lock())
        a.close()
        b = collector.acquire_lock(); self.assertIsNotNone(b); b.close()

    def run_ensure(self, alive_pids, age):
        open(collector.PID, "w").write("4242")
        self.write_hb(age)
        calls = {"kill": [], "start": 0}
        live = set(alive_pids)
        def kill(pid, why):
            calls["kill"].append(pid); live.discard(pid)
        def start():
            calls["start"] += 1; return 0
        with mock.patch.object(collector, "alive", lambda pid: pid in live), \
             mock.patch.object(collector, "kill", kill), mock.patch.object(collector, "start", start), \
             mock.patch.object(collector, "collector_pids", lambda: sorted(live)), \
             mock.patch.object(collector, "status", lambda: 0):
            collector.ensure()
        return calls

    def test_ensure_fresh_alive_does_nothing(self):
        self.assertEqual(self.run_ensure({4242}, 30), {"kill": [], "start": 0})

    def test_ensure_restarts_on_stale_heartbeat_even_if_pid_alive(self):
        c = self.run_ensure({4242}, collector.STALE_S + 60)
        self.assertEqual(c, {"kill": [4242], "start": 1})

    def test_ensure_restarts_dead_pid(self):
        self.assertEqual(self.run_ensure(set(), 30), {"kill": [], "start": 1})
        self.assertTrue(any("not running -> starting" in m and "box booted" in m for m in self.logs))

    def test_ensure_adopts_orphan_instead_of_duplicating(self):
        open(collector.PID, "w").write("1")
        self.write_hb(20, pid=777)
        started = []
        with mock.patch.object(collector, "alive", lambda pid: pid == 777), \
             mock.patch.object(collector, "start", lambda: started.append(1) or 0), \
             mock.patch.object(collector, "collector_pids", lambda: [777]), \
             mock.patch.object(collector, "status", lambda: 0):
            collector.ensure()
        self.assertEqual(started, []); self.assertEqual(collector.read_pid(), 777)

    def test_loop_survives_iteration_exception(self):
        n = {"i": 0}
        def it(k, last):
            n["i"] += 1
            if n["i"] == 1:
                raise RuntimeError("database is locked")
            collector._stop = True
            return k + 1, last
        with mock.patch.object(collector, "iteration", it), mock.patch.object(collector, "sleep_until", lambda e, d: None), \
             mock.patch.object(collector.signal, "signal", lambda *a: None):
            collector._stop = False
            collector.loop()
        collector._stop = False
        self.assertEqual(n["i"], 2)
        self.assertTrue(any("loop error" in m and "database is locked" in m for m in self.logs))
        self.assertEqual(collector.read_heart()["phase"], "stopped")

    def test_step_never_raises(self):
        with mock.patch.object(collector.subprocess, "run", side_effect=OSError("fork failed")):
            self.assertEqual(collector.step(["news.py", "fetch"]), -1)
            self.assertEqual(collector.exits_step(), -1)


if __name__ == "__main__":
    unittest.main()
