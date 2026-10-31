"""Captures the main screen through the agent's own code and checks the
picture: the agent imports on this system, mss sees the monitors, and a
grab is a real picture, not black (CI on Windows, macOS and the Linux
distributions; see .github/workflows/agent-platforms.yml).

Run: python -I scripts/test_capture.py OUT_DIR [--black-first]

--black-first (Linux with Xvfb and xsetroot): the screen starts black, so
the agent must send its notice frame (explain_black); then the script
paints the screen and the agent must send the real picture.
Exit 0 when all checks pass; the JPEGs land in OUT_DIR.
"""
import io
import os
import platform
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))
import warnings  # noqa: E402
warnings.filterwarnings("error", message="mss.mss is deprecated")

import agent  # noqa: E402
import mss  # noqa: E402
from PIL import Image  # noqa: E402


def grab(out_dir, name):
    mon = agent.primary_monitor(agent.list_monitors(0))
    rect = agent.monitor_rect(mon)
    with agent.new_mss() as sct:
        shot = agent.explain_black(sct.grab(rect))
    jpeg, w, h = agent.encode_frame(shot, rect, None, 1280, 80, "420")
    with open(os.path.join(out_dir, f"{name}.jpg"), "wb") as f:
        f.write(jpeg)
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    top = max(hi for _, hi in img.getextrema())
    notice = isinstance(shot, agent.NoticeShot)
    print(f"{name}: screen {rect['width']}x{rect['height']} -> {w}x{h} JPEG "
          f"{len(jpeg)} bytes, brightest {top}, {'NOTICE' if notice else 'picture'}")
    return notice


def main():
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    print(f"{platform.system()} {platform.release()} ({platform.platform()}), "
          f"Python {platform.python_version()}, mss {mss.__version__}")
    monitors = agent.list_monitors(0)
    print(f"monitors: {monitors[1:]}")
    assert len(monitors) > 1, "mss found no monitor"
    if "--black-first" in sys.argv:
        subprocess.run(["xsetroot", "-solid", "black"], check=True)
        assert grab(out_dir, "1-black-screen"), "a black screen must give the notice"
        print("  reason: " + " ".join(agent.black_screen_reason()))
        subprocess.run(["xsetroot", "-solid", "#3060a0"], check=True)
        assert not grab(out_dir, "2-painted-screen"), "a painted screen must come through"
    elif grab(out_dir, "screen"):
        print("FAIL: the capture is black: " + " ".join(agent.black_screen_reason()))
        sys.exit(2)
    print("PASS")


if __name__ == "__main__":
    main()
