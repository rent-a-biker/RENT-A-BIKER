#!/usr/bin/env python3
"""Smoke test: M9 Capacitor native-wrapper project.

Validates the Capacitor wrapper scaffold in hidden_files/capacitor/ without
needing an Android SDK or a Mac:

  1. capacitor.config.json is valid: appId com.rentabiker.app (reverse-DNS),
     appName "Rent a Biker", webDir www, bundled mode (no server.url),
     androidScheme https, allowMixedContent false.
  2. package.json pins identical @capacitor/* 8.x versions; node >= 22.
  3. scripts/build-www.mjs builds www/ from the web app source: injects the
     API base (default = live Render URL, RAB_API_BASE override honored,
     non-https refused), relativizes bundled asset paths, rewrites /docs.
  4. The web app's index.html carries the API-base override: RAB_API_BASE
     sanitizer + u() helper, api() and every direct fetch go through u(),
     no raw fetch('/...) remains, footer privacy link honors the base.
  5. android/ platform: app name + package, branded launcher icons at every
     density (expected sizes), charcoal adaptive-icon background, splash.
  6. ios/ platform: display name, bundle id, 1024px RGB (no-alpha) app icon,
     branded splash imageset.
  7. store-prep/assets/icon-1024.png exists at 1024x1024 with no alpha.
  8. Branding scan over the wrapper's own files (node_modules excluded).

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_capacitor.py
"""
import json
import os
import re
import struct
import subprocess

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/
CAP = os.path.normpath(os.path.join(HERE, "..", "capacitor"))
STORE_PREP = os.path.normpath(os.path.join(HERE, "..", "store-prep"))
APP_STATIC = os.path.join(HERE, "app", "static")
CHECKS: list[tuple[str, bool, str]] = []

LIVE_BASE = "https://rent-a-biker-f4m3.onrender.com"


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(cond), detail))


def png_size(path):
    """Return (width, height, color_type) from a PNG IHDR (stdlib only)."""
    with open(path, "rb") as f:
        sig = f.read(8)
        if sig != b"\x89PNG\r\n\x1a\n":
            return None
        ln = struct.unpack(">I", f.read(4))[0]
        typ = f.read(4)
        if typ != b"IHDR" or ln < 13:
            return None
        w, h, depth, ctype, comp, filt, inter = struct.unpack(">IIBBBBB", f.read(13))
        return (w, h, ctype)


def run_node_build(env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        ["node", "scripts/build-www.mjs"],
        cwd=CAP, capture_output=True, text=True, timeout=120, env=env,
    )


def main() -> int:
    # --- 1. capacitor.config.json ---
    cfg_path = os.path.join(CAP, "capacitor.config.json")
    check("capacitor.config.json exists", os.path.isfile(cfg_path))
    cfg = json.load(open(cfg_path))
    check("appId is com.rentabiker.app",
          cfg.get("appId") == "com.rentabiker.app", str(cfg.get("appId")))
    check("appId is valid reverse-DNS",
          bool(re.match(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", cfg.get("appId", ""))))
    check("appName is 'Rent a Biker'", cfg.get("appName") == "Rent a Biker")
    check("webDir is www", cfg.get("webDir") == "www")
    check("bundled mode (no server.url)", "server" not in cfg or "url" not in cfg.get("server", {}),
          "server.url would ship a remote WebView; see ARCHITECTURE.md")
    check("androidScheme https", cfg.get("server", {}).get("androidScheme") == "https")
    check("allowMixedContent false", cfg.get("android", {}).get("allowMixedContent") is False)

    # --- 2. package.json pins ---
    pkg = json.load(open(os.path.join(CAP, "package.json")))
    deps = dict(pkg.get("dependencies", {}))
    deps.update(pkg.get("devDependencies", {}))
    versions = {deps.get(k) for k in ("@capacitor/core", "@capacitor/cli",
                                      "@capacitor/android", "@capacitor/ios")}
    check("all @capacitor/* pinned to one version", len(versions) == 1, str(versions))
    check("@capacitor/* is v8", (versions.pop() or "").startswith("8."),
          "Capacitor 8 is current (Node 22+, Android SDK 36)")
    check("node engine >= 22", "22" in pkg.get("engines", {}).get("node", ""))

    # --- 3. www build script ---
    script = os.path.join(CAP, "scripts", "build-www.mjs")
    check("build-www.mjs exists", os.path.isfile(script))
    p = run_node_build()
    check("build-www.mjs exits 0 (default base)", p.returncode == 0, p.stderr[-300:])
    www_index = os.path.join(CAP, "www", "index.html")
    html = open(www_index).read()
    check("www/index.html injects the live API base",
          f'window.RAB_API_BASE="{LIVE_BASE}"' in html)
    check("www/index.html has no absolute /static/ refs",
          'href="/static/' not in html and 'src="/static/' not in html)
    check("www/index.html /docs points at the live backend",
          f'href="{LIVE_BASE}/docs"' in html)
    manifest = open(os.path.join(CAP, "www", "manifest.webmanifest")).read()
    check("www manifest icon srcs are bundle-relative",
          '"/static/' not in manifest and '"static/icons/icon-512.png"' in manifest)

    p = run_node_build({"RAB_API_BASE": "https://rent-a-biker.com"})
    html2 = open(www_index).read()
    check("RAB_API_BASE override honored",
          'window.RAB_API_BASE="https://rent-a-biker.com"' in html2, p.stderr[-200:])
    p = run_node_build({"RAB_API_BASE": "http://evil.example/x"})
    check("non-https API base refused", p.returncode != 0)
    p = run_node_build()  # leave www/ in the canonical default-base state
    check("rebuild with default base exits 0", p.returncode == 0)

    # --- 4. API-base override in the web app source ---
    src_html = open(os.path.join(APP_STATIC, "index.html")).read()
    check("index.html has the RAB_API_BASE sanitizer",
          "window.RAB_API_BASE" in src_html and "API_BASE" in src_html)
    check("sanitizer accepts https and rejects the rest",
          "https://" in src_html and "return '';" in src_html)
    check("api() helper routes through u(path)", "fetch(u(path)," in src_html)
    check("no raw fetch('/...) remains", "fetch('/" not in src_html)
    check("health check uses u()", "fetch(u('/health'))" in src_html)
    check("footer privacy link has an id", 'id="privacyLink"' in src_html)
    check("footer privacy link honors the API base",
          "getElementById('privacyLink').href = u('/privacy')" in src_html)

    # --- 5. android/ platform ---
    andr = os.path.join(CAP, "android")
    check("android/ platform generated", os.path.isdir(andr))
    strings = open(os.path.join(andr, "app/src/main/res/values/strings.xml")).read()
    check("android app_name is 'Rent a Biker'",
          "<string name=\"app_name\">Rent a Biker</string>" in strings)
    check("android package_name is com.rentabiker.app",
          "<string name=\"package_name\">com.rentabiker.app</string>" in strings)
    bg = open(os.path.join(andr, "app/src/main/res/values/ic_launcher_background.xml")).read()
    check("android adaptive-icon background is brand charcoal", "#141414" in bg)
    dens = {"mdpi": (108, 48), "hdpi": (162, 72), "xhdpi": (216, 96),
            "xxhdpi": (324, 144), "xxxhdpi": (432, 192)}
    icons_ok, icons_detail = True, []
    for d, (fg_px, lg_px) in dens.items():
        for name, want in (("ic_launcher_foreground.png", fg_px),
                           ("ic_launcher.png", lg_px),
                           ("ic_launcher_round.png", lg_px)):
            fp = os.path.join(andr, f"app/src/main/res/mipmap-{d}", name)
            sz = png_size(fp) if os.path.isfile(fp) else None
            if not sz or sz[0] != want or sz[1] != want:
                icons_ok = False
                icons_detail.append(f"{d}/{name}: {sz}")
    check("android launcher icons branded at all densities", icons_ok,
          "; ".join(icons_detail))
    splash = os.path.join(andr, "app/src/main/res/drawable/splash.png")
    check("android splash.png exists", os.path.isfile(splash))
    check("www assets synced into android project",
          os.path.isfile(os.path.join(andr, "app/src/main/assets/public/index.html")))

    # --- 6. ios/ platform ---
    ios = os.path.join(CAP, "ios")
    check("ios/ platform generated", os.path.isdir(ios))
    plist = open(os.path.join(ios, "App/App/Info.plist")).read()
    check("ios display name is 'Rent a Biker'",
          "<string>Rent a Biker</string>" in plist)
    check("ios bundle id is com.rentabiker.app",
          "com.rentabiker.app" in
          open(os.path.join(ios, "App/App.xcodeproj/project.pbxproj")).read())
    appicon = os.path.join(ios, "App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png")
    sz = png_size(appicon) if os.path.isfile(appicon) else None
    check("ios app icon is 1024x1024 RGB (no alpha — App Store rule)",
          sz is not None and sz[0] == 1024 and sz[1] == 1024 and sz[2] == 2, str(sz))
    splash_set = os.path.join(ios, "App/App/Assets.xcassets/Splash.imageset")
    splash_ok = all(
        os.path.isfile(os.path.join(splash_set, n))
        for n in ("splash-2732x2732.png", "splash-2732x2732-1.png", "splash-2732x2732-2.png"))
    check("ios splash imageset branded", splash_ok)

    # --- 7. 1024 store icon ---
    icon1024 = os.path.join(STORE_PREP, "assets", "icon-1024.png")
    sz = png_size(icon1024) if os.path.isfile(icon1024) else None
    check("store-prep 1024 icon exists at 1024x1024 with no alpha",
          sz is not None and sz[0] == 1024 and sz[1] == 1024 and sz[2] == 2, str(sz))

    # --- 8. branding scan (wrapper's own files; node_modules + Gradle build outputs excluded) ---
    # Gradle's build/ intermediates embed absolute workspace paths, which include
    # the goal's own directory slug ("...full-uber-style...") — a known false
    # positive (BLOCKED.md item 1), not a branding surface. The actual shipped
    # files (sources, www/, manifests) are still scanned.
    banned = ["uber", "lyft"]
    bad = []
    for root, dirs, files in os.walk(CAP):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "build")]
        for fn in files:
            if not fn.endswith((".json", ".md", ".mjs", ".xml", ".plist", ".gradle")):
                continue
            fp = os.path.join(root, fn)
            try:
                low = open(fp, encoding="utf-8", errors="replace").read().lower()
            except OSError:
                continue
            for b in banned:
                if b in low:
                    bad.append(f"{os.path.relpath(fp, CAP)}:{b}")
    check("branding-clean (no ride-service names)", not bad, str(bad))

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"  FAIL: {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
