"""Security checks for the agent (v2.4.1, from Corey Klass's report).

1. An outside relay in config.json (the removed relay/ server) is refused:
   load_config falls back to the built-in relay, which has the Allow
   prompt and tokens, and says so in the log.
2. Two pairing requests at once: the Allow/Deny prompt shows the first,
   keeps the second queued (no busy loop freezing the Tk thread), then
   shows the second once the first is answered.

No display needed: the Tk window is stubbed out.

Usage: python -I scripts/test_security.py
"""

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

passed, failed = 0, 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")


def test_outside_relay_refused():
    import agent
    print("outside relay in config.json")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        old_path = agent.CONFIG_PATH
        agent.CONFIG_PATH = path
        try:
            for relay in ("wss://relay.example.com", "ws://192.168.1.5:8080",
                          "ws://0.0.0.0:8080"):
                path.write_text(json.dumps({"relay": relay, "code": "ABC123",
                                            "device_id": "x", "name": "pc"}))
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cfg = agent.load_config()
                check(f"{relay} -> built-in relay",
                      cfg["relay"] == agent.DEFAULT_RELAY, cfg["relay"])
                check(f"{relay} -> logged", "SECURITY" in out.getvalue())
            # a local relay on another port is kept as it is
            path.write_text(json.dumps({"relay": "ws://127.0.0.1:8090",
                                        "code": "ABC123", "device_id": "x",
                                        "name": "pc"}))
            with contextlib.redirect_stdout(io.StringIO()):
                cfg = agent.load_config()
            check("local relay kept", cfg["relay"] == "ws://127.0.0.1:8090",
                  cfg["relay"])
            os.environ["RELAY_URL"] = "wss://relay.example.com"
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    cfg = agent.load_config()
            finally:
                del os.environ["RELAY_URL"]
            check("RELAY_URL ignored", cfg["relay"] == "ws://127.0.0.1:8090",
                  cfg["relay"])
        finally:
            agent.CONFIG_PATH = old_path
    check("no outbound relay path left",
          not hasattr(agent.Agent, "_run_outbound"))


class FakeRoot:
    def after(self, ms, fn):
        pass  # the test calls _poll itself, one tick at a time


def test_overlapping_approvals():
    from approval import ApprovalManager
    print("two phones pairing at once")
    loop = asyncio.new_event_loop()
    m = ApprovalManager()
    m._loop = loop
    m._root = FakeRoot()
    shown = []

    def fake_show(desc, fut):
        shown.append(desc)
        m._dialog = object()  # a dialog is now on screen

    m._show = fake_show
    fut_a = loop.create_future()
    fut_b = loop.create_future()
    m._requests.put(("phone A", fut_a))
    m._requests.put(("phone B", fut_b))

    def tick():
        t = threading.Thread(target=m._poll, daemon=True)
        t.start()
        t.join(2)
        return not t.is_alive()

    check("first tick returns", tick())
    check("first tick shows phone A", shown == ["phone A"], str(shown))
    check("tick with a dialog open returns", tick())
    check("phone B waits while A is open", shown == ["phone A"], str(shown))
    check("phone B still queued", m._requests.qsize() == 1,
          f"qsize={m._requests.qsize()}")
    m._dialog = None  # phone A answered
    check("tick after A returns", tick())
    check("then phone B is shown", shown == ["phone A", "phone B"],
          str(shown))

    # a prompt that fails to open denies that phone and keeps polling
    m._dialog = None
    fut_c = loop.create_future()
    m._requests.put(("phone C", fut_c))

    def broken_show(desc, fut):
        raise RuntimeError("no display")

    m._show = broken_show
    with contextlib.redirect_stdout(io.StringIO()):
        check("broken prompt tick returns", tick())
    loop.run_until_complete(asyncio.sleep(0))
    check("broken prompt denies", fut_c.done() and fut_c.result() is False)
    loop.close()


if __name__ == "__main__":
    test_outside_relay_refused()
    test_overlapping_approvals()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
