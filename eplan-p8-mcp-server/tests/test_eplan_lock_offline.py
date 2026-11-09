"""The cross-process EPLAN lock (eplan_lock) - offline."""
import os
import subprocess
import sys
import textwrap
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(HERE, "..", "mcp_server")
sys.path.insert(0, SERVER_DIR)

import eplan_lock  # noqa: E402


@pytest.fixture
def lockdir(tmp_path, monkeypatch):
    monkeypatch.setattr(eplan_lock, "LOCK_DIR", str(tmp_path))
    return tmp_path


def _holder(tmp_path, key, seconds):
    code = textwrap.dedent("""
        import sys, time
        sys.path.insert(0, %r)
        import eplan_lock
        eplan_lock.LOCK_DIR = %r
        with eplan_lock.hold(%r, "export /TYPE:GRAPHICPAGE (other session)"):
            print("held", flush=True)
            time.sleep(%r)
    """) % (SERVER_DIR, str(tmp_path), key, seconds)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_a_second_process_waits_and_reports_the_holder(lockdir):
    p = _holder(lockdir, "eplan_localhost_49153", 3)
    try:
        t0 = time.time()
        with pytest.raises(eplan_lock.EplanBusy) as exc:
            with eplan_lock.hold("eplan_localhost_49153", "generate", timeout=0.5):
                pass
        assert 0.4 < time.time() - t0 < 2.5
        assert "export /TYPE:GRAPHICPAGE (other session)" in str(exc.value)
        assert exc.value.owner.get("pid") == p.pid
        # another EPLAN instance (other port) is not blocked
        with eplan_lock.hold("eplan_localhost_49152", "x", timeout=0.5):
            pass
        # once the holder is done, the call goes through
        t0 = time.time()
        with eplan_lock.hold("eplan_localhost_49153", "generate", timeout=10):
            waited = time.time() - t0
        assert waited < 5
    finally:
        p.wait(timeout=10)


def test_lock_is_released_when_the_holder_dies(lockdir):
    p = _holder(lockdir, "k", 60)
    p.kill(); p.wait(timeout=10)
    with eplan_lock.hold("k", "after crash", timeout=2):
        pass


def test_reentrant_within_one_process(lockdir):
    with eplan_lock.hold("k", "outer", timeout=1):
        with eplan_lock.hold("k", "inner (ExecuteScript of a wrapped action)", timeout=1):
            pass
        assert eplan_lock.owner("k").get("action") == "outer"
    # fully released: another process can take it at once
    code = "import sys; sys.path.insert(0, %r); import eplan_lock; eplan_lock.LOCK_DIR = %r\nwith eplan_lock.hold('k', 'y', timeout=1): print('ok')" % (SERVER_DIR, str(lockdir))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert out.stdout.strip() == "ok", out.stderr


def test_execute_action_takes_the_lock_and_reports_busy(lockdir, monkeypatch):
    import eplan_connection
    calls = []

    class FakeClient:
        SynchronousMode = False
        def ExecuteAction(self, action):
            calls.append((action, eplan_lock.owner(mgr.lock_key()).get("action")))

    mgr = eplan_connection.EPLANConnectionManager.__new__(eplan_connection.EPLANConnectionManager)
    mgr.connected, mgr.client, mgr.host, mgr.port = True, FakeClient(), "localhost", "49153"
    monkeypatch.setattr(mgr, "_log_action", lambda *a, **k: None, raising=False)
    r = mgr.execute_action("ExecuteScript /ScriptFile:x.cs")
    assert r["success"] and calls == [("ExecuteScript /ScriptFile:x.cs", "ExecuteScript /ScriptFile:x.cs")]
    p = _holder(lockdir, mgr.lock_key(), 3)
    try:
        monkeypatch.setenv("EPLAN_MCP_LOCK_TIMEOUT", "0.5")
        r = mgr.execute_action("generate /TYPE:CONNECTIONS")
        assert r["success"] is False and r["errorType"] == "EplanBusy" and "other session" in r["message"]
        assert len(calls) == 1, "the busy call never reached EPLAN"
    finally:
        p.wait(timeout=10)
