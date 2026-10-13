#!/usr/bin/env python3
"""Full regression runner for the Rent a Biker app smoke suites.

Runs every tests/smoke_*.py sequentially (stdlib only; each suite spawns its
own throwaway server), prints per-suite results and a grand total, and exits
nonzero if anything fails.

Usage:
    python3 tests/run_all.py                  # everything
    python3 tests/run_all.py --only smoke_pwa   # substring filter
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TALLY_RE = re.compile(r"(\d+)/(\d+)\s+checks passed", re.IGNORECASE)


def run_suite(path):
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=1200, cwd=HERE,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "passed": 0, "total": 0, "secs": time.time() - t0,
                "note": "TIMEOUT (>1200s)"}
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    tallies = TALLY_RE.findall(out)
    passed, total = (int(tallies[-1][0]), int(tallies[-1][1])) if tallies else (0, 0)
    ok = p.returncode == 0 and tallies and passed == total and total > 0
    note = ""
    if not tallies:
        last = [ln for ln in out.splitlines() if ln.strip()][-3:]
        note = "NO TALLY; tail: " + " | ".join(last)
    return {"ok": ok, "passed": passed, "total": total, "secs": time.time() - t0,
            "note": note}


def main():
    ap = argparse.ArgumentParser(description="Run all Rent a Biker smoke suites.")
    ap.add_argument("--only", default="", help="only run suites whose filename contains this substring")
    args = ap.parse_args()
    suites = sorted(glob.glob(os.path.join(HERE, "smoke_*.py")))
    if args.only:
        suites = [s for s in suites if args.only in os.path.basename(s)]
    if not suites:
        print("no suites matched", file=sys.stderr)
        return 2
    grand_passed = grand_total = 0
    failed = []
    print(f"running {len(suites)} suites…\n")
    for path in suites:
        name = os.path.basename(path)
        r = run_suite(path)
        grand_passed += r["passed"]
        grand_total += r["total"]
        flag = "PASS" if r["ok"] else "FAIL"
        extra = f" {r['note']}" if r["note"] else ""
        print(f"{flag} {r['passed']}/{r['total']} {r['secs']:.1f}s {name}{extra}")
        if not r["ok"]:
            failed.append(name)
    print(f"\n{'ALL GREEN' if not failed else 'FAILURES: ' + ', '.join(failed)}: "
          f"{grand_passed}/{grand_total} checks across {len(suites)} suites")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
