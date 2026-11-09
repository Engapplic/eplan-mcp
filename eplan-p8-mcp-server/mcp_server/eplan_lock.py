"""
One caller at a time per EPLAN instance, across processes.

Every Claude session starts its own MCP server process, and all of them drive
the same EPLAN through its remote API. Nothing serialised them: on 2026-11-09
EPLAN hung twice while one session exported pages and another regenerated
connections in the same seconds (P26009, actions.jsonl 21:45 and 22:22).

This module is a cross-process lock on a small file next to the other MCP
temp files, keyed by EPLAN host and port. EplanConnectionManager.execute_action
takes it around the remote call, so every action, export and ExecuteScript of
every session queues behind the one already running. Ping does not take it:
it must answer even while EPLAN is busy or wedged.

- Re-entrant within a process (an action wrapped in a generated script calls
  execute_action again for its ExecuteScript).
- The OS releases the lock when the holding process dies: no stale locks.
- A caller that waits longer than the timeout gets EplanBusy naming who holds
  EPLAN, what it is doing and since when - instead of piling onto a hung EPLAN.
"""

import json
import os
import re
import threading
import time

try:  # Windows (where EPLAN runs)
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - other platforms (tests on CI)
    _msvcrt = None
    import fcntl as _fcntl

LOCK_DIR = os.environ.get("EPLAN_MCP_LOCK_DIR") or os.path.join(
    os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~"), "eplan_mcp_locks")


def default_timeout():
    try:
        return float(os.environ.get("EPLAN_MCP_LOCK_TIMEOUT", "180"))
    except ValueError:
        return 180.0


class EplanBusy(Exception):
    def __init__(self, message, owner=None):
        super().__init__(message)
        self.owner = owner or {}


def _safe(key):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key or "eplan")


class _ProcessLock:
    """Process-wide state: one OS lock per key, re-entrant across nested calls."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks = {}  # key -> {"rlock", "depth", "fh"}

    def _entry(self, key):
        with self._guard:
            e = self._locks.get(key)
            if e is None:
                e = {"rlock": threading.RLock(), "depth": 0, "fh": None}
                self._locks[key] = e
            return e

    @staticmethod
    def _try_os_lock(fh):
        fh.seek(0)
        if _msvcrt is not None:
            _msvcrt.locking(fh.fileno(), _msvcrt.LK_NBLCK, 1)
        else:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)

    @staticmethod
    def _os_unlock(fh):
        fh.seek(0)
        if _msvcrt is not None:
            _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
        else:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)

    @staticmethod
    def owner_of(key):
        try:
            with open(os.path.join(LOCK_DIR, _safe(key) + ".owner"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def acquire(self, key, label="", timeout=None):
        timeout = default_timeout() if timeout is None else float(timeout)
        e = self._entry(key)
        deadline = time.time() + timeout
        # threads of this process: wait on the in-process lock first
        if not e["rlock"].acquire(timeout=max(0.0, timeout)):
            raise EplanBusy(self._busy_message(key, timeout), self.owner_of(key))
        if e["depth"] > 0:  # nested call of the holder
            e["depth"] += 1
            return
        os.makedirs(LOCK_DIR, exist_ok=True)
        path = os.path.join(LOCK_DIR, _safe(key) + ".lock")
        fh = open(path, "a+b")
        try:
            if os.path.getsize(path) < 1:
                fh.write(b"\0"); fh.flush()
            while True:
                try:
                    self._try_os_lock(fh)
                    break
                except OSError:
                    if time.time() >= deadline:
                        raise EplanBusy(self._busy_message(key, timeout), self.owner_of(key))
                    time.sleep(0.1)
        except BaseException:
            fh.close()
            e["rlock"].release()
            raise
        e["fh"] = fh
        e["depth"] = 1
        try:
            with open(os.path.join(LOCK_DIR, _safe(key) + ".owner"), "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "action": (label or "")[:160],
                           "since": time.strftime("%H:%M:%S")}, f)
        except OSError:
            pass

    def release(self, key):
        e = self._entry(key)
        if e["depth"] <= 0:
            return
        e["depth"] -= 1
        if e["depth"] == 0 and e["fh"] is not None:
            try:
                self._os_unlock(e["fh"])
            except OSError:
                pass
            e["fh"].close()
            e["fh"] = None
        e["rlock"].release()

    def _busy_message(self, key, timeout):
        o = self.owner_of(key)
        who = ""
        if o:
            who = " Held by MCP server pid %s since %s running: %s." % (o.get("pid"), o.get("since"), o.get("action"))
        return ("EPLAN is busy with another session's call and did not come free within %gs.%s "
                "Only one session drives EPLAN at a time (eplan_lock); wait and retry, or check "
                "with eplan_ping whether EPLAN itself is hung." % (timeout, who))


_LOCK = _ProcessLock()


class hold:
    """with hold(key, label): ... - one caller at a time on that EPLAN."""

    def __init__(self, key, label="", timeout=None):
        self.key, self.label, self.timeout = key, label, timeout

    def __enter__(self):
        _LOCK.acquire(self.key, self.label, self.timeout)
        return self

    def __exit__(self, *exc):
        _LOCK.release(self.key)
        return False


def owner(key):
    return _LOCK.owner_of(key)
