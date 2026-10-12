"""Benchmark the JPEG screen pipeline on THIS PC: capture -> resize -> encode.

The screen presets (saver / balanced / hd / max) each have a frame-time
budget (1 / fps). This measures what every stage really costs here so the
presets can pick the sharpest settings that still fit:

  grab     mss.grab() with a fresh mss instance per frame (the old streamer)
           vs one persistent instance (the new capture thread)
  convert  shot.rgb + Image.frombytes (old) vs Image.frombuffer("BGRX")
  resize   PIL BILINEAR / BICUBIC / LANCZOS, PIL reduce() box pre-pass +
           filter, and OpenCV INTER_AREA / INTER_LINEAR / INTER_CUBIC
  encode   JPEG quality x subsampling (4:2:0 vs 4:4:4), optimize off

Each resize option reports its median time plus JPEG size and an
end-to-end PSNR at a reference encode (q80 4:2:0): the decoded JPEG is
upscaled back to native with one fixed BICUBIC filter and compared against
the native capture — higher = sharper text on the phone. The fixed upscale
keeps the metric neutral about which downscale filter is "right".

The last table prices each preset as the streamer runs it: grab on the
capture thread, resize+encode on the encode thread (the two overlap), so
throughput is bounded by the slower stage and latency by their sum.

Read-only: it only captures the screen, never injects input. Timings are
medians but still move with whatever else the PC is doing.

Usage:
    python scripts/bench_capture.py [--widths 960,1280,1600,native]
                                    [--iters 12] [--monitor 1]
"""

import argparse
import io
import statistics
import time

import mss
import numpy as np
from PIL import Image

try:
    import cv2
except ImportError:  # the agent falls back to PIL resizing without OpenCV
    cv2 = None

# Reference encode used to compare resize filters on equal terms.
REF_Q, REF_SUB = 80, 2

# (quality, subsampling) sweep — PIL subsampling 2 = 4:2:0, 0 = 4:4:4.
ENCODES = [(55, 2), (65, 2), (70, 2), (75, 2), (80, 2), (85, 2), (90, 2),
           (80, 0), (85, 0), (90, 0)]

# The shipped presets: (name, fps, width or None = native, quality, sub).
PRESETS = [("saver", 12, 960, 55, 2), ("balanced", 20, 1280, 70, 2),
           ("hd", 24, None, 80, 2), ("max", 30, None, 90, 0)]


def new_mss():
    # mss >= 10.2 renamed the factory to MSS (mss.mss warns); keep 9.x working.
    return (getattr(mss, "MSS", None) or mss.mss)()


def median_ms(fn, iters):
    times = []
    out = None
    for _ in range(iters):
        t0 = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times), out


def psnr(a, b):
    diff = np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32)
    mse = float(np.mean(diff * diff))
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def encode(img, quality, subsampling):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, subsampling=subsampling,
             optimize=False)
    return buf.getvalue()


def score(jpeg, native_arr, native_size):
    decoded = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if decoded.size != native_size:
        decoded = decoded.resize(native_size, Image.BICUBIC)
    return psnr(np.asarray(decoded), native_arr)


def resizers(native, bgra, width, height):
    """name -> zero-arg callable returning the resized PIL RGB image."""
    out = {
        "pil bilinear": lambda: native.resize((width, height), Image.BILINEAR),
        "pil bicubic": lambda: native.resize((width, height), Image.BICUBIC),
        "pil lanczos": lambda: native.resize((width, height), Image.LANCZOS),
    }
    k = native.width // width
    if k >= 2:
        # reduce(k) is a k x k box average (cheap); the filter then only
        # covers the small remaining ratio.
        def reduce_then(flt):
            small = native.reduce(k)
            return small if small.size == (width, height) \
                else small.resize((width, height), flt)
        out["pil reduce+bilinear"] = lambda: reduce_then(Image.BILINEAR)
        out["pil reduce+bicubic"] = lambda: reduce_then(Image.BICUBIC)
    if cv2 is not None:
        def cv(interp):
            small = cv2.resize(bgra, (width, height), interpolation=interp)
            return Image.frombuffer("RGB", (width, height), small,
                                    "raw", "BGRX", 0, 1)
        out["cv2 area"] = lambda: cv(cv2.INTER_AREA)
        out["cv2 linear"] = lambda: cv(cv2.INTER_LINEAR)
        out["cv2 cubic"] = lambda: cv(cv2.INTER_CUBIC)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--widths", default="960,1280,1600,native")
    ap.add_argument("--iters", type=int, default=12)
    ap.add_argument("--monitor", type=int, default=1,
                    help="index into mss().monitors (1 = first monitor)")
    args = ap.parse_args()

    with new_mss() as sct:
        mon = sct.monitors[args.monitor]
    print(f"monitor {args.monitor}: {mon['width']}x{mon['height']} "
          f"at ({mon['left']},{mon['top']})   opencv: "
          f"{cv2.__version__ if cv2 else 'missing'}")

    # ---- grab: fresh instance per frame (old) vs persistent (new)
    def grab_fresh():
        with new_mss() as s:
            return s.grab(mon)
    t_fresh, _ = median_ms(grab_fresh, args.iters)
    sct = new_mss()
    sct.grab(mon)  # warm the DIB section
    t_grab, shot = median_ms(lambda: sct.grab(mon), args.iters)
    print(f"\ngrab      fresh mss per frame {t_fresh:6.1f} ms   "
          f"persistent {t_grab:6.1f} ms")

    # ---- convert BGRA -> PIL RGB (grab excluded: same shot every time)
    t_rgb, _ = median_ms(
        lambda: Image.frombytes("RGB", shot.size, bytes(shot.rgb)), args.iters)
    t_buf, native = median_ms(
        lambda: Image.frombuffer("RGB", shot.size, shot.raw,
                                 "raw", "BGRX", 0, 1).copy(), args.iters)
    print(f"convert   .rgb + frombytes {t_rgb:6.1f} ms   "
          f"frombuffer(BGRX) {t_buf:6.1f} ms")
    sct.close()
    bgra = np.frombuffer(shot.raw, np.uint8).reshape(
        shot.height, shot.width, 4)
    native_arr = np.asarray(native)

    widths = []
    for w in args.widths.split(","):
        w = w.strip()
        widths.append(native.width if w == "native" else int(w))

    best = {}  # width -> (resize name, resize ms)
    for width in widths:
        height = round(native.height * width / native.width)
        print(f"\n=== resize {native.width}x{native.height} -> "
              f"{width}x{height}  (scored at q{REF_Q} 4:2:0) ===")
        print(f"  {'filter':22} {'resize':>8} {'KB':>7} {'PSNR':>7}")
        if width == native.width:
            best[width] = ("none", 0.0)
            jpeg = encode(native, REF_Q, REF_SUB)
            print(f"  {'none (native)':22} {0.0:6.1f}ms "
                  f"{len(jpeg) / 1024:7.1f} "
                  f"{score(jpeg, native_arr, native.size):7.2f}")
            continue
        rows = []
        for name, fn in resizers(native, bgra, width, height).items():
            t, small = median_ms(fn, args.iters)
            jpeg = encode(small, REF_Q, REF_SUB)
            s = score(jpeg, native_arr, native.size)
            rows.append((name, t, len(jpeg), s))
            print(f"  {name:22} {t:6.1f}ms {len(jpeg) / 1024:7.1f} {s:7.2f}")
        # "Best" = sharpest filter among those at most 2 ms slower than the
        # fastest one: past that the extra resize time buys little.
        fastest = min(r[1] for r in rows)
        pick = max((r for r in rows if r[1] <= fastest + 2.0),
                   key=lambda r: r[3])
        best[width] = (pick[0], pick[1])
        print(f"  -> pick: {pick[0]}")

    print("\n=== encode sweep (on each width's picked filter) ===")
    print(f"  {'width':>5} {'q/sub':>10} {'encode':>8} {'KB':>7} {'PSNR':>7}")
    for width in widths:
        height = round(native.height * width / native.width)
        name, _ = best[width]
        if name == "none":
            small = native
        else:
            small = resizers(native, bgra, width, height)[name]()
        for q, sub in ENCODES:
            t, jpeg = median_ms(lambda: encode(small, q, sub), args.iters)
            s = score(jpeg, native_arr, native.size)
            print(f"  {width:5} q{q:<3}{'4:2:0' if sub == 2 else '4:4:4':>6} "
                  f"{t:6.1f}ms {len(jpeg) / 1024:7.1f} {s:7.2f}")

    print("\n=== presets: grab thread || resize+encode thread ===")
    print(f"  {'preset':9} {'fps':>4} {'budget':>8} {'grab':>7} "
          f"{'process':>8} {'max fps':>8} {'latency':>8} {'KB':>7} "
          f"{'PSNR':>6}")
    for pname, fps, width, q, sub in PRESETS:
        width = width or native.width
        height = round(native.height * width / native.width)
        rname = best.get(width, ("cv2 area" if cv2 else "pil bilinear",))[0]

        def process():
            small = native if rname == "none" else \
                resizers(native, bgra, width, height)[rname]()
            return encode(small, q, sub)
        t_proc, jpeg = median_ms(process, args.iters)
        t_proc += t_buf
        stage = max(t_grab, t_proc)
        print(f"  {pname:9} {fps:4} {1000 / fps:6.1f}ms {t_grab:5.1f}ms "
              f"{t_proc:6.1f}ms {1000 / stage:8.1f} "
              f"{t_grab + t_proc:6.1f}ms {len(jpeg) / 1024:7.1f} "
              f"{score(jpeg, native_arr, native.size):6.2f}")


if __name__ == "__main__":
    main()
