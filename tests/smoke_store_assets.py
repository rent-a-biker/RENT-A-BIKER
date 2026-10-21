#!/usr/bin/env python3
"""Smoke test: M9 store assets — feature graphic + store icon contract.

No server needed; pure file checks over hidden_files/store-prep/assets/.

  1. assets/feature-graphic.png exists, is a real PNG, and is exactly
     1024x500 RGB — the Play Console feature-graphic spec.
  2. the graphic is real artwork: charcoal background, wheel orange, and
     bright title text all present (not a blank/flat placeholder).
  3. build-feature-graphic.py regenerates the file deterministically
     (run it twice, sha256 must match) — rebuildable, not a one-off.
  4. assets/icon-1024.png exists, 1024x1024, RGB with NO alpha channel
     (App Store rejects icons with transparency).
  5. play-console.md points the feature-graphic row at the shipped file and
     its short-description tagline matches the copy baked into the graphic.
  6. branding scan clean (no ride-service names) across the builder script,
     the new play-console row, and the asset filenames.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_store_assets.py
"""
import hashlib
import os
import re
import struct
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/
STORE_PREP = os.path.normpath(os.path.join(HERE, "..", "store-prep"))
ASSETS = os.path.join(STORE_PREP, "assets")
GRAPHIC = os.path.join(ASSETS, "feature-graphic.png")
BUILDER = os.path.join(STORE_PREP, "build-feature-graphic.py")
ICON = os.path.join(ASSETS, "icon-1024.png")
PLAY_MD = os.path.join(STORE_PREP, "play-console.md")

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail else ""))


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def read_png(path):
    """Parse PNG IHDR + decoded RGB pixels. Returns (width, height, colortype, pixels|None)."""
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(PNG_SIG):
        return None
    pos = len(PNG_SIG)
    width = height = bitdepth = colortype = None
    idat = b""
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            width, height, bitdepth, colortype = struct.unpack(">IIBB", chunk[:10])
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if width is None:
        return None
    pixels = None
    if colortype == 2 and bitdepth == 8:
        try:
            raw = zlib.decompress(idat)
            stride = width * 3
            rows = []
            prev = bytearray(stride)
            p = 0
            for _ in range(height):
                ftype = raw[p]
                p += 1
                line = bytearray(raw[p:p + stride])
                p += stride
                if ftype == 1:  # Sub
                    for i in range(3, stride):
                        line[i] = (line[i] + line[i - 3]) & 0xFF
                elif ftype == 2:  # Up
                    for i in range(stride):
                        line[i] = (line[i] + prev[i]) & 0xFF
                elif ftype == 3:  # Average
                    for i in range(stride):
                        a = line[i - 3] if i >= 3 else 0
                        line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
                elif ftype == 4:  # Paeth
                    for i in range(stride):
                        a = line[i - 3] if i >= 3 else 0
                        b = prev[i]
                        c = prev[i - 3] if i >= 3 else 0
                        pp = a + b - c
                        pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                        pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                        line[i] = (line[i] + pr) & 0xFF
                rows.append(bytes(line))
                prev = line
            pixels = b"".join(rows)
        except Exception:
            pixels = None
    return (width, height, colortype, pixels)


def near(pixel, target, tol=40):
    return all(abs(pixel[i] - target[i]) <= tol for i in range(3))


def main():
    # --- feature graphic exists + PNG contract ---
    g_exists = os.path.isfile(GRAPHIC)
    check("feature-graphic.png exists", g_exists, GRAPHIC)
    g_size = os.path.getsize(GRAPHIC) if g_exists else 0
    check("graphic is a real file (not a stub)", g_size > 10_000, f"{g_size} bytes")

    parsed = read_png(GRAPHIC) if g_exists else None
    check("graphic parses as PNG with IHDR", parsed is not None)
    if parsed is None:
        return finish()
    width, height, colortype, pixels = parsed
    check("graphic is exactly 1024 wide", width == 1024, str(width))
    check("graphic is exactly 500 tall", height == 500, str(height))
    check("graphic is RGB (no alpha) PNG", colortype == 2, f"color type {colortype}")

    if pixels is not None:
        check("graphic is not a solid flat fill",
              min(pixels) < 100 < max(pixels))
        charcoal = near(pixels[(20 * 1024 + 20) * 3:(20 * 1024 + 20) * 3 + 3], (20, 20, 20))
        check("charcoal brand background present", charcoal)
        hub = pixels[(250 * 1024 + 270) * 3:(250 * 1024 + 270) * 3 + 3]  # wheel hub
        orange = near(hub, (204, 102, 30), tol=60)
        check("wheel orange brand mark present", orange, f"hub rgb={tuple(hub)}")
        bright = sum(1 for i in range(0, len(pixels), 3)
                     if pixels[i] > 200 and pixels[i + 1] > 200 and pixels[i + 2] > 200)
        check("bright title text present", bright > 1000, f"{bright} bright px")
    else:
        check("graphic pixel decode (zlib)", False, "could not decode IDAT")

    # --- deterministic rebuild ---
    h1 = hashlib.sha256(open(GRAPHIC, "rb").read()).hexdigest()
    p = subprocess.run([sys.executable, BUILDER], cwd=STORE_PREP,
                       capture_output=True, text=True, timeout=120)
    h2 = hashlib.sha256(open(GRAPHIC, "rb").read()).hexdigest()
    check("builder regenerates graphic deterministically",
          p.returncode == 0 and h1 == h2,
          f"rc={p.returncode} sha={h2[:12]}")

    # --- store icon contract ---
    i_exists = os.path.isfile(ICON)
    check("icon-1024.png exists", i_exists, ICON)
    iparsed = read_png(ICON) if i_exists else None
    check("icon parses as PNG with IHDR", iparsed is not None)
    if iparsed is not None:
        iw, ih, ict, _ = iparsed
        check("icon is exactly 1024x1024", iw == 1024 and ih == 1024, f"{iw}x{ih}")
        check("icon has no alpha channel (App Store rule)", ict == 2, f"color type {ict}")

    # --- play-console.md references ---
    md = open(PLAY_MD, encoding="utf-8").read() if os.path.isfile(PLAY_MD) else ""
    check("play-console.md references the shipped feature graphic",
          "assets/feature-graphic.png" in md)
    tagline = "Book real motorcycle rides with trusted local bikers."
    in_md = tagline in md
    builder_src = open(BUILDER, encoding="utf-8").read() if os.path.isfile(BUILDER) else ""
    in_builder = ("Book real motorcycle rides" in builder_src and
                  "with trusted local bikers." in builder_src)
    check("graphic tagline matches the Play short description",
          in_md and in_builder, "tagline present in md+builder" if in_md and in_builder else "MISMATCH")

    # --- branding scan ---
    banned = ["uber", "lyft"]
    hay = (builder_src + "\n" + md + "\n" + " ".join(os.listdir(ASSETS))).lower()
    hits = sorted({b for b in banned if b in hay})
    check("no ride-service names in store-asset surfaces", not hits,
          f"hits: {hits}" if hits else "clean")

    finish()


def finish():
    passed = sum(1 for _, c, _ in CHECKS if c)
    total = len(CHECKS)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


main()
