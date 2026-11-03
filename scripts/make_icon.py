"""Generate the Rem icons: a teal pointer with a tap ring on dark.

Writes app/assets/icon/icon.png (1024, the whole icon),
app/assets/icon/icon_foreground.png (the Android adaptive foreground: the
pointer and rings inside the safe zone, on transparent; the background is
pubspec.yaml's adaptive_icon_background) and agent/sudo.ico (Windows exes
and dialogs). Then `cd app && dart run flutter_launcher_icons`.

Run with the agent venv's python (it has Pillow):
    python -I scripts/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
S = 1024
K = 4                                   # supersampling
TEAL = (38, 196, 176)
DARK_TOP, DARK_BOT = (22, 28, 36), (8, 10, 14)   # bottom = pubspec background


def pointer(d, x, y, size):
    pts = [(0, 0), (0, 0.74), (0.19, 0.575), (0.31, 0.86), (0.43, 0.81),
           (0.31, 0.53), (0.55, 0.53)]
    p = [(x + px * size, y + py * size) for px, py in pts]
    d.polygon(p, fill=TEAL)
    d.line(p + [p[0]], fill=TEAL, width=int(size * 0.07), joint="curve")


def art(s):
    """The pointer and its tap rings on transparent, s x s."""
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    rings = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(rings)
    cx, cy = int(s * 0.40), int(s * 0.33)
    for r, a in ((0.20, 90), (0.13, 180)):
        rr = int(s * r)
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=TEAL + (a,),
                  width=int(s * 0.022))
    layer = Image.alpha_composite(layer, rings)
    pointer(ImageDraw.Draw(layer), cx, cy, int(s * 0.50))
    return layer


def background(s):
    grad = Image.new("RGB", (s, s))
    d = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / (s - 1)
        d.line([(0, y), (s, y)], fill=tuple(
            int(DARK_TOP[i] + (DARK_BOT[i] - DARK_TOP[i]) * t) for i in range(3)))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1),
                                           radius=int(s * 0.23), fill=255)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def main():
    s = S * K
    icon = Image.alpha_composite(background(s), art(s)).resize((S, S), Image.LANCZOS)
    out = ROOT / "app" / "assets" / "icon"
    out.mkdir(parents=True, exist_ok=True)
    icon.save(out / "icon.png")

    # Adaptive foreground: the art cropped to what it covers, fitted into
    # the middle 56% (Android masks to a 66% circle at most), centred.
    a = art(s)
    a = a.crop(a.getbbox())
    box = int(s * 0.56)
    scale = box / max(a.size)
    a = a.resize((int(a.width * scale), int(a.height * scale)), Image.LANCZOS)
    fg = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    fg.paste(a, ((s - a.width) // 2, (s - a.height) // 2), a)
    fg.resize((S, S), Image.LANCZOS).save(out / "icon_foreground.png")

    icon.save(ROOT / "agent" / "sudo.ico",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                     (128, 128), (256, 256)])
    print("icons written")


main()
