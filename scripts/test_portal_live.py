"""The Wayland portal for real: starts the agent's portal session on the
running desktop, reads frames from PipeWire and checks the picture; with
--virtual-input also creates the uinput mouse/keyboard and checks the
kernel lists it. CI runs it in a headless Sway session (wlroots: the
portal has ScreenCast but no RemoteDesktop, so the agent must fall back),
see scripts/ci_wlroots.sh.

Run: agent's venv python -I scripts/test_portal_live.py OUT_DIR [--expect-no-remote-desktop] [--virtual-input]
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))
import wayland_portal as wp  # noqa: E402


async def main():
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    assert wp.available(), "portal pieces missing (dbus-fast, PyGObject + GStreamer)"
    portal = wp.Portal(os.path.join(out_dir, "portal_token"))
    await asyncio.wait_for(portal.ensure(), 60)
    print(f"session up: rect {portal.rect}, remote desktop: {portal.remote_desktop}")
    if "--expect-no-remote-desktop" in sys.argv:
        assert portal.remote_desktop is False, "expected the ScreenCast-only fallback"
    img = None
    for attempt in range(3):
        frames = await asyncio.to_thread(portal.open_frames)
        try:
            # A real picture within 10 s (a fresh stream may first hand
            # over an empty, black buffer; the phone just gets the next).
            for _ in range(20):
                got = await asyncio.to_thread(frames.grab, 0.5)
                if got is not None:
                    img = got
                    if int(img[:, :, :3].max()) > 16:
                        break
            break
        except wp.PortalError as e:
            # What the agent does (ScreenStreamer._grab_portal): a dead
            # stream ends the session and the next grab starts a new one.
            print(f"stream died ({e}): new session, as the agent does")
            frames.close()
            await portal.close()
            await asyncio.wait_for(portal.ensure(), 60)
    try:
        assert img is not None, "no frame from PipeWire"
        h, w, _ = img.shape
        top = int(img[:, :, :3].max())
        print(f"frame: {w}x{h}, brightest {top}")
        from PIL import Image
        Image.frombuffer("RGB", (w, h), img.tobytes(), "raw", "BGRX", 0, 1) \
            .save(os.path.join(out_dir, "portal-frame.png"))
        assert top > 16, "the portal frame is black"
    finally:
        frames.close()
    if "--virtual-input" in sys.argv:
        portal.move_rel(0.05, 0.05)
        portal.click("left")
        portal.key("enter")
        with open("/proc/bus/input/devices") as f:
            listed = "Rem remote input" in f.read()
        print(f"virtual input device listed by the kernel: {listed}")
        assert listed, "the uinput device did not appear"
        portal._virtual().close()
    await portal.close()
    print("PASS")


if __name__ == "__main__":
    asyncio.run(main())
