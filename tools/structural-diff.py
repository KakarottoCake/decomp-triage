#!/usr/bin/env python3

"""Say what kind of wrong a structurally-different function is.

stack-frame-diff.py sorts failures into buckets and its last bucket -- frame
agrees, code differs, not a register permutation -- is the one worth working,
but "structural" is not a diagnosis. This reads those functions instruction by
instruction and reports what actually differs, because the categories have very
different fixes and very different costs.

The categories came out of measuring all 405 of them, not from guessing:

    length differs   136    an operation is present in one and not the other
    unclassified     111    differences this tool still does not decode
    mixed             64    several kinds at once -- composition is reported
    registers         38    register fields only, near the allocator wall
    data addresses    36    a literal or static sits at a different offset
    field offsets      7    every difference is a struct displacement
    type/width         7    every difference is an opcode substitution
    branches           1    only the destination moved
    constants          1    every difference is a literal value

`unclassified` is deliberately named as a gap rather than a verdict. Folding it
into `registers` would label those functions "allocator wall, skip these" on no
evidence, which is the most expensive mistake a triage tool can make. Decoding
the X-form, branch and rotate families moved 44 functions out of it; what
remains needs a real disassembler.

Three rules keep the remaining categories trustworthy, and the middle one is the
one that matters:

  * Displacements off r1 are ignored -- that is the frame, and stack-frame-diff
    owns it.
  * A displacement is only called a struct field if its base register is an
    object pointer. MWCC addresses statics and string literals as
    `lis rX, sym@ha` + `addi rD, rX, sym@l`, which puts a data offset in exactly
    the field a struct displacement uses. Provenance is the only thing that
    separates them, so lis-derived bases are tracked and reported as data. This
    is not a detail: before it was added, 23 functions across 9 units were
    reported as a shared base-class bug when they were all data ordering.
  * `addi rD,rA,n` with a real rA is a member address, so its immediate is an
    offset, not a literal; with rA=0 it is `li` and genuinely is a literal.
    Without that split a Params constructor reports 66 wrong constants rather
    than one wrong pool.

Usage:
  python tools/structural-diff.py                    # every structural function
  python tools/structural-diff.py Enemy/             # units matching a substring
  python tools/structural-diff.py --category constants
  python tools/structural-diff.py --detail           # per-difference listing
  python tools/structural-diff.py --json out.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import struct
import sys
from collections import Counter
from pathlib import Path

script_dir = Path(__file__).resolve().parent
root_dir = script_dir.parent

# D-form memory ops. The displacement is the low 16 bits and the base register
# is bits 11-15, which is what lets a field offset be told from a constant.
MEM = {
    32: "lwz", 33: "lwzu", 34: "lbz", 35: "lbzu", 36: "stw", 37: "stwu",
    38: "stb", 39: "stbu", 40: "lhz", 41: "lhzu", 42: "lha", 43: "lhau",
    44: "sth", 45: "sthu", 46: "lmw", 47: "stmw", 48: "lfs", 49: "lfsu",
    50: "lfd", 51: "lfdu", 52: "stfs", 53: "stfsu", 54: "stfd", 55: "stfdu",
}
# D-form ops whose immediate is a value rather than an address displacement.
IMM = {
    7: "mulli", 8: "subfic", 10: "cmplwi", 11: "cmpwi", 12: "addic",
    13: "addic.", 14: "addi", 15: "addis", 24: "ori", 25: "oris",
    26: "xori", 27: "xoris", 28: "andi.", 29: "andis.",
}
# addi/addis are the ambiguous pair: `addi rD,0,n` is li, a literal, but
# `addi rD,rA,n` with a real rA is taking the address of a member and its
# immediate is a struct offset exactly like a load displacement. Splitting on
# rA is what stops a Params constructor's 66 member addresses from being
# reported as 66 wrong literals -- they are one wrong header.
ADDR_FORMING = {14, 15}
# Instruction families the classifier can now read. Everything outside these
# still lands in `unclassified`, which stays honest about being a gap.
BRANCH = {16, 18}            # bc, b
XFORM = {31, 59, 63}         # integer X-form, and float A/X-form
ROTATE = {20, 21, 23}        # rlwimi, rlwinm, rlwnm
WIDTH = {"lbz": 1, "stb": 1, "lhz": 2, "lha": 2, "sth": 2, "lwz": 4, "stw": 4,
         "lfs": 4, "stfs": 4, "lfd": 8, "stfd": 8}

# What a substitution between two opcode families usually means. Keyed by an
# unordered pair so direction doesn't matter to the lookup.
SUBST_MEANING = {
    frozenset(("lfs", "lfd")): "f32 vs f64 -- a float field or local has the wrong width",
    frozenset(("stfs", "stfd")): "f32 vs f64 -- a float field or local has the wrong width",
    frozenset(("op59", "op63")): "single vs double float arithmetic -- check for a stray "
                                 "double literal (1.0 vs 1.0f) or an f64 variable",
    frozenset(("lfs", "op59")): "a float is being computed where the other loads it, or vice versa",
    frozenset(("lwz", "lbz")): "integer width -- one side reads 4 bytes, the other 1",
    frozenset(("lwz", "lhz")): "integer width -- one side reads 4 bytes, the other 2",
    frozenset(("lhz", "lha")): "signedness -- lhz is unsigned, lha sign-extends; check u16 vs s16",
    frozenset(("stw", "stb")): "integer width on a store -- check the field's declared type",
    frozenset(("stw", "sth")): "integer width on a store -- check the field's declared type",
    frozenset(("lwz", "op31")): "displacement vs indexed addressing -- usually a[i] written "
                                "as a computed pointer, or a loop induction variable",
    frozenset(("addi", "op31")): "displacement vs indexed addressing, as above",
    frozenset(("addi", "lwz")): "one side computes an address, the other dereferences -- "
                                "a missing or extra level of indirection",
    frozenset(("stw", "addi")): "one side stores, the other only computes -- an assignment "
                                "is present on one side and not the other",
}


def load_sfd():
    spec = importlib.util.spec_from_file_location("sfd", script_dir / "stack-frame-diff.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sfd"] = mod
    argv, sys.argv = sys.argv, ["stack-frame-diff.py"]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


def words(code: bytes) -> list[int]:
    return [struct.unpack_from(">I", code, i)[0] for i in range(0, len(code) - 3, 4)]


def s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def mnemonic(op: int) -> str:
    return MEM.get(op) or IMM.get(op) or f"op{op}"


def data_base_registers(w: list[int]) -> set[int]:
    """Registers holding a relocated data address rather than an object pointer.

    This distinction is the whole difference between "your header is wrong" and
    "your data is ordered differently", and getting it wrong is not a small
    error -- it sends you editing a class definition when the class is fine.

    MWCC addresses a static, a string literal or a compiler-generated constant
    as `lis rX, sym@ha` followed by `addi rD, rX, sym@l`, so the low half lands
    in the same instruction field a struct displacement would. The only way to
    tell them apart is provenance: follow which registers descend from a `lis`
    (addis with rA=0) and treat their displacements as data offsets.

    Deliberately one-way -- a register is never un-marked once tainted. For
    triage that errs toward calling something data, which is the safe direction:
    a missed struct shift costs a lead, a phantom one costs a wrong edit.
    """
    tainted: set[int] = set()
    for x in w:
        op, rD, rA = x >> 26, (x >> 21) & 0x1F, (x >> 16) & 0x1F
        if op == 15 and rA == 0:          # lis rD, imm
            tainted.add(rD)
        elif op == 14 and rA in tainted:  # addi rD, <tainted>, imm
            tainted.add(rD)
        elif op == 31 and (x >> 1) & 0x3FF == 444 and rD in tainted:
            tainted.add(rA)               # mr rA, rD  (or rD)
    return tainted


def _branch_regs_equal(op: int, a: int, b: int) -> bool:
    """True if two branches differ only in destination, not in condition."""
    if op == 18:                      # b/bl: the whole payload is the target
        return True
    return (a ^ b) & 0x03FF0000 == 0  # bc: BO/BI unchanged


def classify(tcode: bytes, ocode: bytes) -> dict:
    """What differs between the target and ours, as counted components.

    Everything here is deliberately conservative: a difference that isn't
    confidently one of the named kinds lands in `other`, which keeps a function
    out of a clean category rather than mislabelling it.
    """
    tw, ow = words(tcode), words(ocode)
    if len(tw) != len(ow):
        return {"kind": "length", "delta_instr": len(ow) - len(tw),
                "target_instr": len(tw), "our_instr": len(ow)}

    fields, pools, consts, substs = [], [], [], Counter()
    regs = other = branches = masks = 0
    data_regs = data_base_registers(tw) | data_base_registers(ow)
    for idx, (a, b) in enumerate(zip(tw, ow)):
        if a == b:
            continue
        pa, pb = a >> 26, b >> 26
        if pa != pb:
            substs[frozenset((mnemonic(pa), mnemonic(pb)))] += 1
            continue
        same_hi = (a ^ b) & 0xFFFF0000 == 0
        same_lo = (a ^ b) & 0x0000FFFF == 0
        if (pa in MEM or pa in ADDR_FORMING) and same_hi:
            base = (a >> 16) & 0x1F
            entry = {"at": idx * 4, "op": mnemonic(pa), "target": s16(a & 0xFFFF),
                     "ours": s16(b & 0xFFFF)}
            if pa in ADDR_FORMING and base == 0:
                consts.append(entry)    # addi rD,0,n is li -- a real literal
            elif base == 1:
                other += 1              # the frame; stack-frame-diff owns it
            elif base in (2, 13) or base in data_regs:
                pools.append(entry)     # a data address: sdata, or lis-derived
            else:
                fields.append(entry)
        elif pa in IMM and same_hi:
            consts.append({"at": idx * 4, "op": IMM[pa], "target": s16(a & 0xFFFF),
                           "ours": s16(b & 0xFFFF)})
        elif same_lo:
            regs += 1
        elif pa in BRANCH:
            # A branch whose only difference is where it points means the code
            # around it moved, not that this instruction is wrong.
            if _branch_regs_equal(pa, a, b):
                branches += 1
            else:
                other += 1
        elif pa in XFORM:
            # X/A-form: rD 6-10, rA 11-15, rB 16-20, then the extended opcode.
            if (a ^ b) & 0x000007FF:
                substs[frozenset((f"{mnemonic(pa)}:xo{(a >> 1) & 0x3FF}",
                                  f"{mnemonic(pb)}:xo{(b >> 1) & 0x3FF}"))] += 1
            else:
                regs += 1
        elif pa in ROTATE:
            # rlwinm-family: differing SH/MB/ME is a different mask or shift,
            # which in source terms is a different field width or bit position.
            if (a ^ b) & 0x0000FFFE and not (a ^ b) & 0x03FF0000:
                masks += 1
            else:
                regs += 1
        else:
            other += 1

    out = {"kind": None, "fields": fields, "pools": pools, "consts": consts,
           "branches": branches, "masks": masks,
           # joined rather than tupled so the whole row stays JSON-serialisable
           "substs": {" <-> ".join(sorted(k)): v for k, v in substs.items()},
           "regs": regs, "other": other}

    named = (len(fields) + len(pools) + len(consts) + sum(substs.values())
             + branches + masks)
    if fields and not (pools or consts or substs or regs or other):
        out["kind"] = "fields"
    elif pools and not (fields or consts or substs or regs or other):
        out["kind"] = "pools"
    elif consts and not (fields or pools or substs or regs or other):
        out["kind"] = "constants"
    elif substs and not (fields or pools or consts or regs or other or branches or masks):
        out["kind"] = "types"
    elif masks and not (fields or pools or consts or substs or regs or other or branches):
        out["kind"] = "masks"
    elif branches and not (fields or pools or consts or substs or regs or other or masks):
        out["kind"] = "branches"
    elif named:
        out["kind"] = "mixed"
    elif regs and not other:
        out["kind"] = "registers"
    else:
        # Nothing recognised. This must NOT be folded into "registers": that
        # label reads as "allocator wall, skip these", and skipping a function
        # the tool merely failed to decode is the worst outcome it can cause.
        out["kind"] = "unclassified"

    if len(pools) >= 2:
        counts = Counter(p["target"] - p["ours"] for p in pools)
        shift, n = counts.most_common(1)[0]
        if n >= 2 and n / len(pools) >= 0.8 and abs(shift) >= 4:
            out["pool_shift"] = shift
            out["pool_outliers"] = len(pools) - n
            out["pool_from"] = min(p["target"] for p in pools
                                   if p["target"] - p["ours"] == shift)

    if len(fields) >= 2:
        # Modal rather than unanimous: a 60-member constructor can have one
        # offset that is genuinely wrong on top of a layout shift, and demanding
        # unanimity would hide the shift behind the outlier.
        counts = Counter(f["target"] - f["ours"] for f in fields)
        shift, n = counts.most_common(1)[0]
        # Require a 4-byte-aligned shift. Anything smaller is far more often two
        # literals that happen to differ by the same amount than a real layout
        # change -- `addi 1->2` twice was being reported as a "-1 byte struct
        # shift". A layout moved only by u8/u16 members can be missed this way;
        # that is the right trade for a category that is meant to be trusted.
        if n >= 2 and n / len(fields) >= 0.8 and shift % 4 == 0 and abs(shift) >= 4:
            out["uniform_shift"] = shift
            out["shift_outliers"] = len(fields) - n
            out["shift_from"] = min(f["target"] for f in fields
                                    if f["target"] - f["ours"] == shift)
    return out


def describe(c: dict) -> list[str]:
    """The lines printed under a function. Each says what to go and change."""
    lines = []
    if c["kind"] == "length":
        d = c["delta_instr"]
        lines.append(f"{c['target_instr']} instructions in retail, {c['our_instr']} in ours ({d:+d})")
        if abs(d) <= 3:
            lines.append("small: usually one operation, one inlined call, or a "
                         "condition written the other way round" if d else "")
        else:
            lines.append("large: the two are doing genuinely different work -- "
                         "read the target rather than editing ours")
        return [x for x in lines if x]

    if c.get("uniform_shift") is not None:
        sh, out = c["uniform_shift"], c.get("shift_outliers", 0)
        tail = f", plus {out} that shift by something else" if out else ""
        lines.append(
            f"{len(c['fields']) - out} of {len(c['fields'])} member offsets shifted by a "
            f"uniform {sh:+d} from 0x{c['shift_from']:x}{tail}"
        )
        lines.append(
            f"-> this is a header bug, not a function bug: our layout is {abs(sh)} bytes "
            f"{'short' if sh > 0 else 'long'} before 0x{c['shift_from']:x}. "
            "Fixing it moves every function that touches this type."
        )
    elif c["fields"]:
        shown = ", ".join(f"{f['op']} 0x{f['ours']:x}->0x{f['target']:x}" for f in c["fields"][:4])
        lines.append(f"{len(c['fields'])} struct field offsets ({shown}"
                     f"{', ...' if len(c['fields']) > 4 else ''})")
        widths = {WIDTH.get(f["op"]) for f in c["fields"]} - {None}
        if len(widths) == 1:
            lines.append(f"-> all {widths.pop()}-byte accesses; check the field order and "
                         "any missing member in the declaration")

    if c.get("pool_shift") is not None:
        sh, out = c["pool_shift"], c.get("pool_outliers", 0)
        tail = f", plus {out} that shift by something else" if out else ""
        lines.append(f"{len(c['pools']) - out} of {len(c['pools'])} data addresses shifted by "
                     f"a uniform {sh:+d} from 0x{c['pool_from']:x}{tail}")
        lines.append(
            f"-> data ordering, not code and not a header: this unit emits {abs(sh)} bytes "
            f"{'too much' if sh < 0 else 'too little'} before that point in its pool. "
            "Usually compiler-generated constants from an inline, or a static defined in "
            "the wrong place. The function itself may already be correct."
        )
    elif c["pools"]:
        lines.append(f"{len(c['pools'])} data address displacement(s) "
                     f"(string literals, statics, or small-data)")
        lines.append("-> data layout, not code: a literal or static is ordered differently "
                     "in the pool. Usually fixed by moving a definition, not the function.")

    if c["consts"]:
        shown = ", ".join(f"{k['op']} {k['ours']}->{k['target']}" for k in c["consts"][:5])
        lines.append(f"{len(c['consts'])} immediate(s): {shown}"
                     f"{', ...' if len(c['consts']) > 5 else ''}")
        lines.append("-> a literal, enum value, array stride or field count is wrong. "
                     "Cheapest category in the project; just read the numbers.")

    if c["substs"]:
        for pair, n in sorted(c["substs"].items(), key=lambda kv: -kv[1]):
            meaning = SUBST_MEANING.get(frozenset(pair.split(" <-> ")))
            tail = f"  -- {meaning}" if meaning else ""
            lines.append(f"{n}x {pair}{tail}")

    if c.get("masks"):
        lines.append(f"{c['masks']} rotate/mask difference(s) (rlwinm SH/MB/ME)")
        lines.append("-> a different bit position or field width -- check the declared "
                     "type of a bitfield or a u8/u16/s16 member, or a shift constant")
    if c.get("branches"):
        lines.append(f"{c['branches']} branch(es) pointing elsewhere")
        lines.append("-> control flow is the same shape but the code around it moved; "
                     "usually a consequence of another difference, not a cause")
    if c["regs"]:
        lines.append(f"{c['regs']} register-only difference(s)")
    if c["other"]:
        lines.append(f"{c['other']} difference(s) not confidently classified")
    return lines


HEADLINE = {
    "constants": "only numbers are wrong -- cheapest thing in the bucket",
    "fields": "only struct displacements -- fix the header, not the function",
    "pools": "only data addresses -- literal/static ordering, not the code",
    "types": "only opcode substitutions -- a declared type is wrong",
    "length": "instruction counts differ -- something is genuinely present or absent",
    "mixed": "several kinds at once -- see the composition",
    "registers": "register fields only, but not a clean permutation -- allocator wall",
    "masks": "only rotate/mask fields -- a bitfield or integer width is wrong",
    "branches": "only branch targets -- the code moved, this is a symptom not a cause",
    "unclassified": "this tool could not read the differences -- not a verdict, a gap",
}
ORDER = ["constants", "fields", "types", "masks", "pools", "mixed", "length",
         "branches", "registers", "unclassified"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("filters", nargs="*", help="only units whose name contains one of these")
    ap.add_argument("--category", choices=ORDER, help="show only one category")
    ap.add_argument("--detail", action="store_true", help="list every difference, not a summary")
    ap.add_argument("--json", dest="json_out", help="write the classified worklist")
    args = ap.parse_args(argv)

    sfd = load_sfd()
    units = json.loads((root_dir / "objdiff.json").read_text(encoding="utf-8"))["units"]

    rows: list[dict] = []
    for unit in units:
        name = unit["name"]
        if args.filters and not any(f in name for f in args.filters):
            continue
        tp, bp = unit.get("target_path"), unit.get("base_path")
        if not tp or not bp:
            continue
        tf, bf = root_dir / tp, root_dir / bp
        if not tf.exists() or not bf.exists():
            continue
        try:
            target, ours = sfd.read_functions(tf), sfd.read_functions(bf)
        except (ValueError, IndexError, struct.error) as exc:
            print(f"{name}: skipped ({exc})", file=sys.stderr)
            continue
        for sym, tcode in target.items():
            ocode = ours.get(sym)
            if ocode is None or ocode == tcode:
                continue
            if sfd.frame_size(tcode) != sfd.frame_size(ocode):
                continue          # frame bucket; stack-frame-diff owns it
            if sfd.register_permutation_only(tcode, ocode):
                continue          # clean permutation; nothing to say
            c = classify(tcode, ocode)
            c.update(unit=name, symbol=sym,
                     source=(unit.get("metadata") or {}).get("source_path", ""))
            rows.append(c)

    if args.category:
        rows = [r for r in rows if r["kind"] == args.category]
    if not rows:
        print("nothing structural matches that selection")
        return 0

    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)

    for kind in ORDER:
        group = by_kind.get(kind)
        if not group:
            continue
        print(f"\n=== {kind}  ({len(group)})  {HEADLINE[kind]}")
        # Uniform struct shifts first: one header fix can settle several functions.
        group.sort(key=lambda r: (r.get("uniform_shift") is None, r["unit"], r["symbol"]))
        limit = len(group) if (args.detail or args.category) else 12
        for r in group[:limit]:
            print(f"\n  {r['symbol']}")
            print(f"    {r['unit']}")
            for line in describe(r):
                print(f"    {line}")
        if limit < len(group):
            print(f"\n  ... and {len(group) - limit} more "
                  f"(--category {kind} for all of them)")

    print("\n" + "-" * 72)
    print(f"  {len(rows)} structural function(s)")
    for kind in ORDER:
        if by_kind.get(kind):
            print(f"    {kind:<11} {len(by_kind[kind]):>4}   {HEADLINE[kind]}")
    report_shifts(rows, "uniform_shift", "shift_from", "member-offset",
                  "one wrong type definition", "in our headers")
    report_shifts(rows, "pool_shift", "pool_from", "data-address",
                  "one wrong data pool", "emitted before that point")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"\n  wrote {args.json_out}")
    return 0


def report_shifts(rows, key, from_key, label, cause, where) -> None:
    """Group uniform shifts across functions.

    A delta shared by unrelated units is the whole point of aggregating: separate
    classes cannot independently be wrong by the same amount.
    """
    shifts = [r for r in rows if r.get(key) is not None]
    if shifts:
        print(f"\n  {len(shifts)} function(s) show a uniform {label} shift.")
        groups: dict[int, list[dict]] = {}
        for r in shifts:
            groups.setdefault(r[key], []).append(r)
        for delta, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            units_hit = sorted({g["unit"].split("/")[-1] for g in group})
            print(f"\n    {delta:+d} bytes -- {len(group)} function(s) "
                  f"across {len(units_hit)} unit(s)")
            if len(units_hit) >= 3:
                print(f"      ** {len(units_hit)} unrelated units share this exact delta -- "
                      f"suspect {cause},\n         {abs(delta)} bytes "
                      f"{'too much' if delta < 0 else 'too little'} {where}.")
                print(f"      units: {', '.join(units_hit)}")
            elif len(group) > 1:
                print(f"      shared by {len(group)} function(s) in {len(units_hit)} unit(s)")
            for g in group[:8]:
                print(f"        from 0x{g[from_key]:<5x} {g['symbol'][:62]}")
            if len(group) > 8:
                print(f"        ... and {len(group) - 8} more")


if __name__ == "__main__":
    sys.exit(main())
