"""Tests for agent/wayland_portal.py that run anywhere (no Wayland needed).

- Input: the Notify* calls a Portal sends (member, signature, body, flags),
  their order, the scroll-axis flip, and queueing before the session is up,
  through a fake D-Bus connection.
- Frames: PortalFrames' appsink/BGRx handling, with a GStreamer test source
  in place of the PipeWire stream.

Run: agent's venv python -I scripts/test_wayland_portal.py
The end-to-end path (dialog, PipeWire, real input) needs a Wayland session.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))
import wayland_portal as wp  # noqa: E402

from dbus_fast import MessageFlag  # noqa: E402


class FakeBus:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return None


def notify_calls(bus):
    return [(m.member, m.signature, m.body[2:], m.flags) for m in bus.sent]


def started_portal():
    p = wp.Portal("/nonexistent/portal_token")
    p._bus = FakeBus()
    p._session = "/org/freedesktop/portal/desktop/session/1_1/t"
    p._node = 42
    p.rect = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    return p


def test_input_messages():
    p = started_portal()
    p.move_rel(0.01, -0.02)
    p.move_abs(0.5, 1.0)
    p.click("right")
    p.button("left", True)
    p.button("left", False)
    p.scroll(0, 1)    # pynput: dy > 0 = up -> Wayland steps -1
    p.scroll(2, 0)
    p.key("enter")
    p.text("é1")
    calls = notify_calls(p._bus)
    assert calls[0][:2] == ("NotifyPointerMotion", "oa{sv}dd")
    assert [round(v, 6) for v in calls[0][2]] == [19.2, -21.6]
    assert calls[1] == ("NotifyPointerMotionAbsolute", "oa{sv}udd", [42, 959.5, 1079.0],
                        MessageFlag.NO_REPLY_EXPECTED)
    assert [c[2] for c in calls[2:4]] == [[0x111, 1], [0x111, 0]]
    assert [c[2] for c in calls[4:6]] == [[0x110, 1], [0x110, 0]]
    assert calls[6][:3] == ("NotifyPointerAxisDiscrete", "oa{sv}ui", [0, -1])
    assert calls[7][2] == [1, 2]
    assert calls[8][2] == [0xff0d, 1] and calls[9][2] == [0xff0d, 0]
    assert [c[2] for c in calls[10:14]] == [[0xe9, 1], [0xe9, 0], [ord("1"), 1], [ord("1"), 0]]
    for m in p._bus.sent:
        assert m.body[0] == p._session and m.body[1] == {}
        assert m.flags == MessageFlag.NO_REPLY_EXPECTED
    print("input messages: ok")


def test_keysyms():
    assert wp.char_keysym("a") == 0x61 and wp.char_keysym("\n") == 0xff0d
    assert wp.char_keysym("€") == 0x1000000 + 0x20ac
    try:
        started_portal().key("nosuchkey")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown key accepted")
    print("keysyms: ok")


def test_queue_until_started():
    async def run():
        p = wp.Portal("/nonexistent/portal_token")
        p._bus = FakeBus()
        started = asyncio.Event()

        async def fake_ensure():
            p._session, p._node = "/s", 7
            p.rect = {"left": 0, "top": 0, "width": 1000, "height": 500}
            for send in p._pending:
                send()
            p._pending = []
            started.set()
            return p
        p.ensure = fake_ensure
        p.move_rel(0.1, 0.1)   # before the session: queued, starts it
        p.key("tab")
        assert p._bus.sent == [] and len(p._pending) == 3
        await asyncio.wait_for(started.wait(), 1)
        calls = notify_calls(p._bus)
        assert [c[0] for c in calls] == ["NotifyPointerMotion", "NotifyKeyboardKeysym",
                                         "NotifyKeyboardKeysym"]
        assert [round(v, 6) for v in calls[0][2]] == [100.0, 50.0]  # rect read at send time
    asyncio.run(run())
    print("queue until started: ok")


def test_frames_from_test_source():
    Gst = wp._gst()
    orig = wp._pipeline
    # Odd width: a padded stride must not leak into the image.
    wp._pipeline = lambda fd, node: (
        "videotestsrc pattern=smpte num-buffers=30 ! video/x-raw,width=1366,height=769 "
        "! videoconvert ! video/x-raw,format=BGRx ! appsink name=sink max-buffers=1 drop=true sync=false")
    r, w = os.pipe()
    try:
        f = wp.PortalFrames(r, 1)
        img = None
        for _ in range(20):
            img = f.grab(0.5)
            if img is not None:
                break
        assert img is not None, "no frame"
        assert img.shape == (769, 1366, 4) and img.dtype.name == "uint8"
        assert img.flags["C_CONTIGUOUS"]
        assert img[:, :, :3].std() > 10  # a real picture, not zeros
        f.close()
        assert f._fd is None
    finally:
        wp._pipeline = orig
        os.close(w)
        for fd in (r,):
            try:
                os.close(fd)
            except OSError:
                pass
    print(f"frames from a test source: ok (GStreamer {Gst.version_string()})")


if __name__ == "__main__":
    test_input_messages()
    test_keysyms()
    test_queue_until_started()
    test_frames_from_test_source()
    print("all wayland_portal tests passed")
