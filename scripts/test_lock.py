"""Test whether injected keys reach the Windows lock screen."""

import subprocess
import time

import mss
from PIL import Image
from pynput.keyboard import Controller, Key


def snap(name):
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    img.thumbnail((960, 960))
    img.save(f"scripts/{name}.jpg", quality=70)
    print(f"saved scripts/{name}.jpg")


kb = Controller()
print("locking...")
subprocess.run("rundll32.exe user32.dll,LockWorkStation", shell=True)
time.sleep(3)
snap("lock1")

print("sending space...")
kb.press(Key.space)
kb.release(Key.space)
time.sleep(1.5)
snap("lock2")

print("typing 1234...")
kb.type("1234")
time.sleep(0.8)
snap("lock3")
print("done")
