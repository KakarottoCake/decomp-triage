#!/usr/bin/env python3
"""Find functions whose only error is the size of their stack frame.

Why this works
--------------
On Metrowerks CodeWarrior GC/1.0 - GC/1.2.5 the compiler does *not* reclaim
unused stack space. Anything that never gets a register still gets a slot,
even if every instruction that touched it was optimized away, and the frame
also covers the outgoing-argument area of the largest call the function makes.
So the frame size is a quantitative fingerprint of the *shape of the original
source*, visible even when the generated code is otherwise identical.

What actually occupies these frames is mostly *compiler temporaries* -- one
slot per intermediate value, created per source line, largely by inlined calls.
So a frame delta is rarely "a missing variable"; it is a missing chain of
intermediate values, which almost always means the original source expression
was longer, or inlined something we do not.

    ours smaller than target -> the original produced more intermediate values
                                here: usually an inline callee we are not
                                inlining, sometimes a longer expression chain
    ours larger than target  -> we produce more than the original did

The FRAME-ONLY list is functions where every instruction agrees once you
account for the uniform shift in stack offsets. objdiff cannot single them out,
because a frame shift shows up there as a wall of unrelated-looking offset diffs
spread across the whole function.

These are NOT easy wins, and the tool used to say they were. They are cheap to
*diagnose* and among the hardest things in the tree to *fix*. The code is
already instruction-perfect, so there is no gradient to climb: objdiff is
saturated, decomp-permuter has nothing to optimise toward, and the only feedback
is one number that is either right or wrong. You need a source edit that adds
intermediate values while generating identical instructions, which is a narrow
needle, and finding it means reconstructing how the original author wrote the
expression. That is source archaeology, not iteration.

Two things make a frame-only case more tractable, and both are worth sorting on:

  * a LARGE delta. +608 bytes names a specific object -- an array of a knowable
    size. +8 is one intermediate value and could be produced a hundred ways.
  * CLUSTERING. Eight copy-paste siblings that all want the same delta are one
    insight, not eight problems.

If you want cheap matches, look at the "other" bucket instead: frame already
correct, code differs. Those are register allocation, scheduling and expression
order -- they have a gradient, and the usual tools work on them.

To find out what is actually in a frame and which inlined call put it there,
run mwcc-izer's `stack -w` on the function; this script exists to tell you
*which* functions are worth pointing it at, across the whole tree at once.

Usage
-----
    python tools/stack-frame-diff.py                 # summary + frame-only list
    python tools/stack-frame-diff.py Enemy/          # only units matching a substring
    python tools/stack-frame-diff.py --by-idiom       # group by method, not by file
    python tools/stack-frame-diff.py --json out.json # machine-readable worklist
"""

from __future__ import annotations

import json
import struct
import sys
from collections import Counter
from pathlib import Path

SHT_SYMTAB = 2
SHF_EXECINSTR = 0x4
STT_FUNC = 2


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def _u16(b: bytes, off: int) -> int:
    return struct.unpack_from(">H", b, off)[0]


def read_functions(path: Path) -> dict[str, bytes]:
    """{symbol: code bytes} for every STT_FUNC in an executable section.

    Stdlib only, big-endian 32-bit ELF -- which is all a GameCube object ever
    is. Staying dependency-free means this runs anywhere the build runs.
    """
    b = path.read_bytes()
    if b[:6] != b"\x7fELF\x01\x02":
        raise ValueError(f"{path}: not a 32-bit big-endian ELF")

    e_shoff, e_shentsize, e_shnum = _u32(b, 0x20), _u16(b, 0x2E), _u16(b, 0x30)
    sections = []
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        sections.append(
            {
                "type": _u32(b, o + 0x04),
                "flags": _u32(b, o + 0x08),
                "addr": _u32(b, o + 0x0C),
                "offset": _u32(b, o + 0x10),
                "size": _u32(b, o + 0x14),
                "link": _u32(b, o + 0x18),
                "entsize": _u32(b, o + 0x24),
            }
        )

    out: dict[str, bytes] = {}
    for sec in sections:
        if sec["type"] != SHT_SYMTAB:
            continue
        strtab = sections[sec["link"]]
        entsize = sec["entsize"] or 16
        for off in range(sec["offset"], sec["offset"] + sec["size"], entsize):
            st_name, st_value = _u32(b, off + 0x00), _u32(b, off + 0x04)
            st_size, st_info = _u32(b, off + 0x08), b[off + 0x0C]
            st_shndx = _u16(b, off + 0x0E)
            if (st_info & 0xF) != STT_FUNC or st_size == 0:
                continue
            if st_shndx == 0 or st_shndx >= len(sections):
                continue
            host = sections[st_shndx]
            if not host["flags"] & SHF_EXECINSTR:
                continue
            end = b.index(b"\0", strtab["offset"] + st_name)
            name = b[strtab["offset"] + st_name : end].decode("utf-8", "replace")
            if name:
                # st_value is section-relative in a relocatable object and
                # absolute in a linked one; subtracting sh_addr covers both.
                start = host["offset"] + (st_value - host["addr"])
                out[name] = b[start : start + st_size]
    return out


def frame_size(code: bytes) -> int:
    """Bytes reserved by the prologue, or 0 for a leaf function.

    MWCC opens every non-leaf prologue with `stwu r1, -N(r1)`. A few put
    `mflr` first, so scan a little way in before giving up.
    """
    for i in range(0, min(len(code), 24), 4):
        word = _u32(code, i)
        if word & 0xFFFF0000 == 0x94210000:  # stwu r1, d(r1)
            d = word & 0xFFFF
            return 0x10000 - d if d & 0x8000 else 0
    return 0


def explained_by_frame(a: bytes, b: bytes, delta: int) -> bool:
    """True if every difference between a and b is the stack shift `delta`.

    A frame change rewrites the displacement of every stack access by a
    constant. Instructions differing in nothing but that displacement are
    therefore expected; anything else is a genuine mismatch.
    """
    if len(a) != len(b):
        return False
    for i in range(0, len(a), 4):
        wa, wb = _u32(a, i), _u32(b, i)
        if wa == wb:
            continue
        if (wa >> 16) != (wb >> 16):
            return False
        da, db = wa & 0xFFFF, wb & 0xFFFF
        sa = da - 0x10000 if da & 0x8000 else da
        sb = db - 0x10000 if db & 0x8000 else db
        if sa - sb not in (delta, -delta):
            return False
    return True


# PowerPC instruction forms, only as much as is needed to blank register fields.
# D-form  : OPCD(0-5) rS(6-10) rA(11-15) d(16-31)      -> registers are bits 6-15
# X-form  : OPCD(0-5) rS(6-10) rA(11-15) rB(16-20) ... -> registers are bits 6-20
# Anything else is compared exactly rather than guessed at.
_XFORM_OPCODES = frozenset({31, 59, 63})
_DFORM_OPCODES = frozenset(
    {14, 15, 24, 25, 26, 27, 28, 29, 32, 33, 34, 35, 36, 37, 38, 39,
     40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55}
)


def _blank_registers(word: int) -> int | None:
    """The instruction with its register operands zeroed, or None if unknown form."""
    opcd = word >> 26
    if opcd in _XFORM_OPCODES:
        return word & ~0x03FFC000  # bits 6-20
    if opcd in _DFORM_OPCODES:
        return word & ~0x03FF0000  # bits 6-15
    return None


def register_permutation_only(a: bytes, b: bytes) -> bool:
    """True if the two bodies differ ONLY in which registers were chosen.

    sm64ds-decomp's experience with the same compiler family is that this case is a
    wall rather than a task: the allocator is non-local, so the colour a temp receives
    depends on whole-function pressure, and there is no source-level knob that reliably
    forces one register over another. Their advice is to recognise it and move on. That
    is only useful if you can recognise it mechanically, hence this check.
    """
    if len(a) != len(b) or a == b:
        return False
    saw_difference = False
    for i in range(0, len(a), 4):
        wa, wb = _u32(a, i), _u32(b, i)
        if wa == wb:
            continue
        if (wa >> 26) != (wb >> 26):
            return False  # different instruction entirely
        ma, mb = _blank_registers(wa), _blank_registers(wb)
        if ma is None or ma != mb:
            return False
        saw_difference = True
    return saw_difference


def shared_cause_hint(rows: list[dict]) -> str:
    """Report the modal delta for a unit, and flag it when it means something.

    An inlined call contributes temporaries to its caller, so a helper we fail
    to inline shows up as the *same* delta repeated across a file.

    But +-8 is the minimum quantum -- the frame is 8-byte aligned, so a single
    4-byte local already costs 8 -- which makes +8 the most common delta for
    purely arithmetic reasons. Treating that as evidence of a shared cause
    would be reading meaning into a floor. So +8 is reported plainly, and
    only a larger repeated delta is called out as a likely single root cause.
    """
    if len(rows) < 3:
        return ""
    counts = Counter(r["delta"] for r in rows)
    delta, n = counts.most_common(1)[0]
    if n < 3 or n / len(rows) < 0.55:
        return ""
    if abs(delta) == 8:
        return (
            "%d of %d differ by %+d, the smallest possible step: one or two "
            "intermediate values each" % (n, len(rows), delta)
        )
    return (
        "%d of %d want the same %+d bytes -- too specific to be coincidence, "
        "suspect one shared inline helper rather than %d separate bugs"
        % (n, len(rows), delta, len(rows))
    )


def report_by_idiom(frame_only: list[dict]) -> None:
    """Group frame-only functions by method name across every class that has one.

    Grouping by unit answers "which file is closest to done". Grouping by method name
    answers a better question: which *idiom* are we writing wrong. SMS is a rushed,
    copy-pasted codebase, so the same virtual is implemented dozens of times in slightly
    different classes; when forty of them are short on stack, that is one reconstruction
    problem repeated, not forty problems. The method name is just the mangled prefix, so
    this costs nothing and needs no demangler.
    """
    groups: dict[str, list[dict]] = {}
    for r in frame_only:
        name = r["symbol"].split("__", 1)[0]
        if name:
            groups.setdefault(name, []).append(r)

    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    print()
    print("functions grouped by method name -- shared idioms, not shared files")
    print()
    print("  %-24s %6s %8s   most common deltas" % ("method", "count", "classes"))
    for name, rs in ranked:
        if len(rs) < 3:
            continue
        classes = len({r["unit"] for r in rs})
        top = Counter(r["delta"] for r in rs).most_common(4)
        deltas = ", ".join("%+dx%d" % (d, n) for d, n in top)
        lead = Counter(r["delta"] for r in rs).most_common(1)[0]
        flag = "  <-- %d/%d agree" % (lead[1], len(rs)) if lead[1] / len(rs) >= 0.7 else ""
        print("  %-24s %6d %8d   %s%s" % (name, len(rs), classes, deltas, flag))


def main(argv: list[str]) -> int:
    json_out = None
    if "--json" in argv:
        json_out = argv[argv.index("--json") + 1]
        argv = [a for a in argv if a != json_out]
    by_idiom = "--by-idiom" in argv
    filters = [a for a in argv if not a.startswith("--")]

    root = Path(__file__).resolve().parent.parent
    units = json.loads((root / "objdiff.json").read_text())["units"]

    frame_only: list[dict] = []
    mixed = other = regperm = identical = compared = 0

    for unit in units:
        name = unit["name"]
        if filters and not any(f in name for f in filters):
            continue
        tp, bp = unit.get("target_path"), unit.get("base_path")
        if not tp or not bp:
            continue
        tf, bf = root / tp, root / bp
        if not tf.exists() or not bf.exists():
            continue
        try:
            target, ours = read_functions(tf), read_functions(bf)
        except (ValueError, IndexError, struct.error) as exc:
            print(f"{name}: skipped ({exc})", file=sys.stderr)
            continue

        for sym, tcode in target.items():
            ocode = ours.get(sym)
            if ocode is None:
                continue
            compared += 1
            if tcode == ocode:
                identical += 1
                continue
            delta = frame_size(tcode) - frame_size(ocode)
            if delta and explained_by_frame(tcode, ocode, delta):
                frame_only.append(
                    {
                        "unit": name,
                        "symbol": sym,
                        "source": unit.get("metadata", {}).get("source_path", ""),
                        "target_frame": frame_size(tcode),
                        "our_frame": frame_size(ocode),
                        "delta": delta,
                    }
                )
            elif delta:
                mixed += 1
            elif register_permutation_only(tcode, ocode):
                regperm += 1
            else:
                other += 1

    frame_only.sort(key=lambda r: (r["unit"], r["symbol"]))
    if by_idiom:
        report_by_idiom(frame_only)
        print()
        print("%d frame-only of %d failing" % (len(frame_only), len(frame_only) + mixed + other + regperm))
        return 0
    by_unit: dict[str, list[dict]] = {}
    for r in frame_only:
        by_unit.setdefault(r["unit"], []).append(r)

    for unit, rows in by_unit.items():
        print("\n%s   (%s)" % (unit, rows[0]["source"]))
        for r in rows:
            need = "short" if r["delta"] > 0 else "over"
            print(
                "    %-58s target 0x%-4x ours 0x%-4x  %s by %d bytes of stack"
                % (r["symbol"], r["target_frame"], r["our_frame"], need, abs(r["delta"]))
            )
        hint = shared_cause_hint(rows)
        if hint:
            print("    -> %s" % hint)

    failing = len(frame_only) + mixed + other + regperm
    print("\n" + "-" * 72)
    print("  %d functions compared, %d byte-identical, %d failing" % (compared, identical, failing))
    print("  FRAME-ONLY  %5d   only the frame size is wrong -- hard: no gradient to climb" % len(frame_only))
    print("  frame+other %5d   frame differs and so does the code" % mixed)
    print("  regperm     %5d   identical but for register choice -- allocator wall, skip these" % regperm)
    print("  other       %5d   frame agrees, structural differences -- start here" % other)
    if frame_only:
        top = Counter(r["delta"] for r in frame_only).most_common(6)
        print("  common deltas: " + ", ".join("%+dB x%d" % (d, n) for d, n in top))

    if json_out:
        Path(json_out).write_text(json.dumps(frame_only, indent=1))
        print("  wrote %s" % json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
