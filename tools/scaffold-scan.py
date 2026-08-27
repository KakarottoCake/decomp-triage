#!/usr/bin/env python3

"""Cancel each function's frame delta with a throwaway local, then count what is
left -- the `char trash[0x40];` diagnostic, run over a whole unit.

Why it exists: when our frame is the wrong size, every stack displacement in the
function shifts, so objdiff renders a wall of `~` lines and the real code
mismatch underneath is invisible. Sizing a throwaway local to the delta cancels
the shift and the diff becomes readable. A function that drops to 0-3 residual
differences is ONE real fix away; a function that stays at 40 has genuine
structural work left. That ranking is the whole point -- it tells you where to
spend the next hour.

The scaffold is a DIAGNOSTIC. This script always restores the file, on success,
on failure, and on Ctrl-C. It must never reach a commit: `review-lint.py`'s
`diagnostic-scaffold-left-in` rule exists to catch exactly that, and it must
stay at zero.

Usage:
  python tools/scaffold-scan.py src/GC2D/GCConsole2.cpp mario/GC2D/GCConsole2
  python tools/scaffold-scan.py src/GC2D/GCConsole2.cpp mario/GC2D/GCConsole2 \
      --only processAppearCoin startAppearBalloon
"""

import argparse
import io
import json
import os
import re
import signal
import subprocess
import sys

script_dir = os.path.dirname(os.path.realpath(__file__))
root_dir = os.path.abspath(os.path.join(script_dir, ".."))
DIFF = os.path.join(script_dir, "decomp-diff.py")
VERSION = os.environ.get("DECOMP_VERSION", "GMSJ01")
REPORT = os.path.join(root_dir, "build", VERSION, "report.json")

FRAME_RE = re.compile(r"stwu\s+r1,\s*\{?-(0x[0-9a-f]+)\}?\(r1\)")


def failing_functions(unit):
    with io.open(REPORT, encoding="utf-8") as f:
        report = json.load(f)
    for u in report["units"]:
        if u["name"] == unit:
            out = [(fn["name"], float(fn["fuzzy_match_percent"]))
                   for fn in u["functions"]
                   if float(fn["fuzzy_match_percent"]) < 100.0]
            out.sort(key=lambda r: -r[1])
            return out
    sys.exit(f"unit not found in report.json: {unit}")


def diff_text(unit, short):
    # Wide context on purpose: at -C 0 objdiff elides every matching line, so a
    # prologue whose frame already agrees is invisible and the frame cannot be
    # read at all. The diff count only looks at ~/</> lines, so context is free.
    r = subprocess.run([sys.executable, DIFF, "-u", unit, "-d", short, "-C", "500"],
                       cwd=root_dir, capture_output=True, text=True)
    return r.stdout


def parse_diff(text):
    """(target_frame, our_frame, residual_diff_count, percent).

    The frames come off the prologue's `stwu`. When objdiff brackets a value it
    means the two sides disagree, so the left/right split gives both at once;
    an unbracketed prologue means the frames already agree.
    """
    pct = None
    m = re.search(r"([0-9.]+)% match", text)
    if m:
        pct = float(m.group(1))
    target = ours = None
    ndiff = 0
    for line in text.splitlines():
        if line[:1] in "~<>":
            ndiff += 1
        if target is None and "stwu r1" in line:
            parts = line.split("|")
            if len(parts) >= 3:
                lm, rm = FRAME_RE.search(parts[1]), FRAME_RE.search(parts[2])
                if lm and rm:
                    target, ours = int(lm.group(1), 16), int(rm.group(1), 16)
            elif len(parts) == 2:
                lm = FRAME_RE.search(parts[1])
                if lm:
                    target = ours = int(lm.group(1), 16)
    return target, ours, ndiff, pct


BRACE = re.compile(r"\{([^}]*)\}")
REG = re.compile(r"^r\d+$")
SLOT = re.compile(r"^-?0x[0-9a-f]+$")


def classify(text):
    """Split residual diff lines into (real, regperm, slot).

    A residual count alone does not say whether there is work to do. objdiff
    brackets only the parts that disagree, so the bracket contents answer it:
    all register names means the allocator picked differently and the emitted
    logic is identical; all hex displacements off r1 means a stack slot moved.
    Anything else -- a different opcode, an instruction present on one side only,
    a changed immediate -- is a real code difference and IS worth working on.
    """
    real, regperm, slot = [], [], []
    for line in text.splitlines():
        tag = line[:1]
        # `"" in "~<>|"` is True in Python, so an empty line would fall through
        # here and be counted as a real difference. Check for it explicitly.
        if not tag or tag not in "~<>|":
            continue
        if tag in "<>|":
            real.append(line)
            continue
        parts = line.split("|")
        if len(parts) < 3:
            real.append(line)
            continue
        left, right = parts[1], parts[2]
        lb, rb = BRACE.findall(left), BRACE.findall(right)
        # Strip the disagreeing parts out; whatever is left must be identical,
        # or the two sides differ by more than the bracketed values.
        if BRACE.sub("", left).split() != BRACE.sub("", right).split():
            real.append(line)
        elif lb and all(REG.match(v) for v in lb + rb):
            regperm.append(line)
        elif lb and all(SLOT.match(v) for v in lb + rb):
            slot.append(line)
        else:
            real.append(line)
    return real, regperm, slot


def find_body_start(text, needle):
    """Offset just past the opening brace of the definition containing `needle`.

    Brace-counted rather than regex'd, and it skips any `::` declaration that is
    not a definition (a trailing `;` before the brace).
    """
    at = 0
    while True:
        start = text.find(needle, at)
        if start == -1:
            return None
        brace = text.find("{", start)
        semi = text.find(";", start)
        if brace != -1 and (semi == -1 or brace < semi):
            return brace + 1
        at = start + len(needle)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source")
    ap.add_argument("unit")
    ap.add_argument("--only", nargs="*", default=None,
                    help="restrict to these short function names")
    args = ap.parse_args()

    src = os.path.join(root_dir, args.source)
    obj_rel = ("build/" + VERSION + "/src/"
               + args.source[len("src/"):].rsplit(".", 1)[0] + ".o")

    original = io.open(src, encoding="utf-8", newline="").read()
    restored = [False]

    def restore(*_):
        if not restored[0]:
            io.open(src, "w", encoding="utf-8", newline="").write(original)
            restored[0] = True

    signal.signal(signal.SIGINT, lambda *a: (restore(), sys.exit(130)))

    try:
        rows = []
        for mangled, pct0 in failing_functions(args.unit):
            short = mangled.split("__")[0]
            if args.only and short not in args.only:
                continue

            target, ours, nd0, _ = parse_diff(diff_text(args.unit, short))
            if target is None:
                rows.append((short, pct0, None, nd0, None, "no prologue"))
                continue
            delta = target - ours
            if delta <= 0:
                note = "frames agree" if delta == 0 else f"ours is {-delta} too big"
                rows.append((short, pct0, delta, nd0, nd0, note))
                print(f"  {short:<26} delta {delta:+5d}  residual {nd0:<4} ({note})")
                continue

            body = find_body_start(original, f"::{short}(")
            if body is None:
                rows.append((short, pct0, delta, nd0, None, "definition not found"))
                continue

            patched = (original[:body]
                       + f"\r\n\tchar trash[{delta:#x}]; // scaffold-scan\r\n"
                       + original[body:])
            io.open(src, "w", encoding="utf-8", newline="").write(patched)
            restored[0] = False
            build = subprocess.run(["ninja", obj_rel], cwd=root_dir,
                                   capture_output=True, text=True)
            if build.returncode != 0:
                rows.append((short, pct0, delta, nd0, None, "build failed"))
                print(f"  {short:<26} delta {delta:+5d}  BUILD FAILED")
                continue
            t2, o2, nd1, pct1 = parse_diff(diff_text(args.unit, short))
            note = "frame cancelled" if t2 == o2 else f"still {t2 - o2:+d}"
            rows.append((short, pct0, delta, nd0, nd1, note))
            print(f"  {short:<26} delta {delta:+5d}  {nd0:>4} -> {nd1:<4} "
                  f"{pct0:6.2f}% -> {pct1 if pct1 is not None else 0:6.2f}%  ({note})")

        restore()
        subprocess.run(["ninja", obj_rel], cwd=root_dir, capture_output=True)

        print("\n" + "=" * 78)
        print("ONE FIX AWAY -- frame cancelled, almost nothing left underneath:")
        near = [r for r in rows if r[4] is not None and r[4] <= 6]
        near.sort(key=lambda r: (r[4], -(r[1] or 0)))
        for short, pct0, delta, nd0, nd1, note in near:
            print(f"  {short:<26} {nd1:>3} residual  (was {nd0:>3} at {pct0:.2f}%, "
                  f"frame {delta:+d})")
        if not near:
            print("  (none)")
        print("\nSTRUCTURAL -- real code work remains:")
        rest = [r for r in rows if r[4] is None or r[4] > 6]
        rest.sort(key=lambda r: (r[4] or 9999))
        for short, pct0, delta, nd0, nd1, note in rest:
            print(f"  {short:<26} {str(nd1):>4} residual  (was {nd0:>3} at "
                  f"{pct0:.2f}%, frame {delta if delta is not None else 0:+d}) {note}")
        print("\nThe scaffold is a diagnostic. review-lint's "
              "'diagnostic-scaffold-left-in' must stay at 0.")
    finally:
        restore()


if __name__ == "__main__":
    main()
