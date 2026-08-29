#!/usr/bin/env python3

"""Compare the DATA laid out by each file against retail, not the code.

Matching work concentrates on instructions, but a file also emits data --
arrays, string literals, and constants the compiler invents for itself. If our
data sits at different offsets than retail's, every instruction that reaches it
looks wrong even when the code is perfect. That failure is invisible in a code
diff and it is what this reports.

The case that motivated it: src/System/RenderModeObj.cpp had four functions
that looked like a wrong struct definition. The struct was fine. Retail's data
section simply began with 40 bytes of constants the compiler generated and then
optimised the uses of -- constants we never emitted, so everything after them
sat 40 bytes too early. Supplying equivalent bytes made the data byte-identical
and reduced every remaining difference in that file to stack-frame size.

Compiler-generated constants are the ones named `@NNNN`. They are dead weight
that retail carries and we usually do not, because MWCC emits them even after
the code that used them is gone. When this reports a missing `@NNNN`, the fix
is not to fabricate padding -- it is to find the source construct that made the
compiler create it.

Usage:
  python tools/data-pool-diff.py                 # every unit with a data mismatch
  python tools/data-pool-diff.py Enemy/          # units matching a substring
  python tools/data-pool-diff.py --detail        # list every symbol, both sides
  python tools/data-pool-diff.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
from pathlib import Path

script_dir = Path(__file__).resolve().parent
root_dir = script_dir.parent

SHT_SYMTAB = 2
SHF_EXECINSTR = 0x4
STT_OBJECT = 1

# Sections whose ordering can shift an instruction's displacement.
DATA_SECTIONS = (".data", ".rodata", ".sdata", ".sdata2", ".bss", ".sbss", ".sbss2")


def _u32(b, off):
    return struct.unpack_from(">I", b, off)[0]


def _u16(b, off):
    return struct.unpack_from(">H", b, off)[0]


def read_section_bytes(path: Path) -> dict[str, bytes]:
    """{section: raw contents}. The authoritative check -- symbol names are not.

    Our build and retail routinely disagree about symbol NAMES for identical
    data: retail's compiler-generated constants are anonymous `@NNNN` while the
    stand-ins written by hand here are named (`dummy1`). Comparing names calls
    a byte-perfect file broken, so content is compared first and names are only
    used to describe a difference that is already known to be real.
    """
    b = path.read_bytes()
    e_shoff, e_shentsize, e_shnum = _u32(b, 0x20), _u16(b, 0x2E), _u16(b, 0x30)
    e_shstrndx = _u16(b, 0x32)
    hdrs = []
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        hdrs.append((_u32(b, o + 0x00), _u32(b, o + 0x04),
                     _u32(b, o + 0x10), _u32(b, o + 0x14)))
    shstr = hdrs[e_shstrndx][2]
    out = {}
    for name_off, stype, off, size in hdrs:
        end = b.index(b"\0", shstr + name_off)
        name = b[shstr + name_off:end].decode("utf-8", "replace")
        if name in DATA_SECTIONS:
            out[name] = b"" if stype == 8 else b[off:off + size]
    return out


def read_data_symbols(path: Path) -> dict[str, list[dict]]:
    """{section: [{name, offset, size}, ...]} for data objects, in address order."""
    b = path.read_bytes()
    if b[:6] != b"\x7fELF\x01\x02":
        raise ValueError(f"{path}: not a 32-bit big-endian ELF")

    e_shoff, e_shentsize, e_shnum = _u32(b, 0x20), _u16(b, 0x2E), _u16(b, 0x30)
    e_shstrndx = _u16(b, 0x32)
    sections = []
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        sections.append({
            "name_off": _u32(b, o + 0x00), "type": _u32(b, o + 0x04),
            "flags": _u32(b, o + 0x08), "addr": _u32(b, o + 0x0C),
            "offset": _u32(b, o + 0x10), "size": _u32(b, o + 0x14),
            "link": _u32(b, o + 0x18), "entsize": _u32(b, o + 0x24),
        })

    shstr = sections[e_shstrndx]["offset"]
    for sec in sections:
        end = b.index(b"\0", shstr + sec["name_off"])
        sec["name"] = b[shstr + sec["name_off"]:end].decode("utf-8", "replace")

    out: dict[str, list[dict]] = {}
    for sec in sections:
        if sec["type"] != SHT_SYMTAB:
            continue
        strtab = sections[sec["link"]]
        entsize = sec["entsize"] or 16
        for off in range(sec["offset"], sec["offset"] + sec["size"], entsize):
            st_name, st_value = _u32(b, off + 0x00), _u32(b, off + 0x04)
            st_size, st_info = _u32(b, off + 0x08), b[off + 0x0C]
            st_shndx = _u16(b, off + 0x0E)
            if (st_info & 0xF) != STT_OBJECT or st_shndx == 0 or st_shndx >= len(sections):
                continue
            host = sections[st_shndx]
            if host["name"] not in DATA_SECTIONS:
                continue
            end = b.index(b"\0", strtab["offset"] + st_name)
            name = b[strtab["offset"] + st_name:end].decode("utf-8", "replace")
            if not name:
                continue
            off_in_sec = st_value - host["addr"]
            # SHT_NOBITS (.bss) occupies no file space, so it has no content to
            # compare -- only a size.
            body = b"" if host["type"] == 8 else b[
                host["offset"] + off_in_sec:host["offset"] + off_in_sec + st_size]
            out.setdefault(host["name"], []).append(
                {"name": name, "offset": off_in_sec, "size": st_size, "body": body})
    for syms in out.values():
        syms.sort(key=lambda s: (s["offset"], s["name"]))
    return out


VERSION = os.environ.get("DECOMP_VERSION", "GMSJ01")
RETAIL_MAP = Path(os.environ.get(
    "DECOMP_MAP", str(root_dir / "orig" / VERSION / "files" / "mario.MAP")))
_map_index = None


def retail_map_symbols(source: str):
    """Symbol names the linker map records for one source file, or None.

    The extracted objects you diff against are what the ORIGINAL LINKER KEPT.
    Anything the original build dead-stripped is missing from them while still
    having been real, and the link map still lists it -- with a `........`
    address and an UNUSED marker.

    So "we emit a symbol the target does not" is only half a sentence. Read from
    the objects alone it is indistinguishable from "we invented this", and the
    two want opposite fixes: keep it, or delete it. Deleting a dead-stripped
    symbol removes correct source, and when it came from a widely included
    header that can cost matched data across every unit that shares it.

    Set DECOMP_MAP if your map is not at orig/$DECOMP_VERSION/files/mario.MAP.
    Without a map this returns None and the tool behaves as it did before.
    """
    global _map_index
    if _map_index is None:
        _map_index = {}
        if not RETAIL_MAP.exists():
            return None
        text = RETAIL_MAP.read_text(encoding="utf-8", errors="replace")
        # `  UNUSED   000010 ........ SomeSymbol Lib.a File.cpp`
        # The linked form ends the same way: <symbol> <lib>.a <source file>
        for m in re.finditer(r"(\S+)\s+\S+\.a\s+(\S+\.(?:cpp|c|s))\s*$",
                             text, re.MULTILINE):
            _map_index.setdefault(m.group(2), set()).add(m.group(1))
    if not _map_index:
        return None
    return _map_index.get(Path(source).name, set())


def compare_section(tsyms: list[dict], osyms: list[dict],
                    source: str = "") -> dict | None:
    """What differs between one section in retail and the same section in ours."""
    # `@NNNN` names are a per-compilation counter, not an identity: the same
    # literal is @1490 in one build and @597 in another. Comparing those by name
    # reports every file as broken. They are compared by content instead, and
    # only the leftovers -- content retail has that we never emit -- are real.
    tgen = [s for s in tsyms if s["name"].startswith("@")]
    # Match retail's anonymous constants against EVERY symbol of ours, not just
    # our anonymous ones. The accepted workaround in this repo is a named
    # stand-in (`static Vec dummy1 = {1,1,1};`) for data the original build got
    # from its precompiled header, so the correct counterpart to an `@NNNN` is
    # very often a named symbol.
    ogen = list(osyms)
    # .bss holds no bytes, so every symbol there has an empty body and content
    # matching would collapse them all into one. Match those by size instead.
    zero_filled = all(not s["body"] for s in tgen + ogen) and bool(tgen or ogen)
    key = (lambda s: s["size"]) if zero_filled else (lambda s: s["body"])
    avail: dict = {}
    for s in ogen:
        avail[key(s)] = avail.get(key(s), 0) + 1
    missing_generated = []
    for s in tgen:
        if avail.get(key(s), 0) > 0:
            avail[key(s)] -= 1
        else:
            missing_generated.append(s)

    tnamed = {s["name"] for s in tsyms if not s["name"].startswith("@")}
    onamed = {s["name"] for s in osyms if not s["name"].startswith("@")}
    tnames, onames = tnamed, onamed
    missing = [s for s in tsyms
               if not s["name"].startswith("@") and s["name"] not in onames]
    extra = [s for s in osyms
             if not s["name"].startswith("@") and s["name"] not in tnames]

    # Split "extra" against the link map. A symbol the map records for this
    # source file is one the original build HAD and stripped, so its presence is
    # evidence the source is right. Only the remainder is genuinely ours.
    known = retail_map_symbols(source) if source else None
    dead_stripped = []
    if known:
        dead_stripped = [s for s in extra if s["name"] in known]
        extra = [s for s in extra if s["name"] not in known]

    tpos = {s["name"]: s["offset"] for s in tsyms}
    opos = {s["name"]: s["offset"] for s in osyms}
    tsize = {s["name"]: s["size"] for s in tsyms}
    osize = {s["name"]: s["size"] for s in osyms}
    shared = sorted(tnames & onames, key=lambda n: tpos[n])

    moved = [{"name": n, "target": tpos[n], "ours": opos[n]}
             for n in shared if tpos[n] != opos[n]]
    resized = [{"name": n, "target": tsize[n], "ours": osize[n]}
               for n in shared if tsize[n] != osize[n]]
    if not (missing or extra or moved or resized or missing_generated
            or dead_stripped):
        return None

    # A single shift shared by every moved symbol means the pool head is wrong,
    # which is one fix rather than one per symbol.
    deltas = {m["target"] - m["ours"] for m in moved}
    head_shift = deltas.pop() if len(deltas) == 1 and moved else None
    return {"missing": missing, "extra": extra, "moved": moved, "resized": resized,
            "dead_stripped": dead_stripped,
            "head_shift": head_shift,
            "missing_generated": [
                {k: v for k, v in s.items() if k != "body"} for s in missing_generated],
            "missing_bytes": sum(s["size"] for s in missing_generated)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("filters", nargs="*")
    ap.add_argument("--detail", action="store_true", help="list every differing symbol")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)

    units = json.loads((root_dir / "objdiff.json").read_text(encoding="utf-8"))["units"]
    rows, clean = [], 0

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
            tdata, odata = read_data_symbols(tf), read_data_symbols(bf)
            tbytes, obytes = read_section_bytes(tf), read_section_bytes(bf)
        except (ValueError, IndexError, struct.error) as exc:
            print(f"{name}: skipped ({exc})", file=sys.stderr)
            continue
        for sec in sorted(set(tdata) | set(odata)):
            # Content is the verdict. Identical bytes means this section is done,
            # whatever the two sides happen to call their symbols.
            tb, ob = tbytes.get(sec), obytes.get(sec)
            if tb is not None and tb == ob:
                clean += 1
                continue
            src = (unit.get("metadata") or {}).get("source_path", "")
            diff = compare_section(tdata.get(sec, []), odata.get(sec, []), src)
            if diff is None:
                if tb is not None and ob is not None and len(tb) != len(ob):
                    diff = {"missing": [], "extra": [], "moved": [], "resized": [],
                            "dead_stripped": [],
                            "head_shift": None, "missing_generated": [],
                            "missing_bytes": len(tb) - len(ob),
                            "size_only": (len(tb), len(ob))}
                else:
                    clean += 1
                    continue
            diff.setdefault("size_only", None)
            if tb is not None and ob is not None:
                diff["byte_delta"] = len(tb) - len(ob)
            diff.update(unit=name, section=sec,
                        source=(unit.get("metadata") or {}).get("source_path", ""))
            rows.append(diff)

    # Units missing compiler-generated constants first: those are the ones with a
    # concrete, already-understood cause.
    rows.sort(key=lambda r: (not r["missing_generated"], -r["missing_bytes"]))

    shown = 0
    for r in rows:
        if not args.detail and shown >= 25:
            break
        shown += 1
        print(f"\n{r['unit']}  [{r['section']}]")
        if r["source"]:
            print(f"  {r['source']}")
        if r["missing_generated"]:
            total = sum(s["size"] for s in r["missing_generated"])
            print(f"  MISSING {len(r['missing_generated'])} compiler-generated constant(s), "
                  f"{total} bytes -- retail emits these and we do not")
            for s in r["missing_generated"][:6]:
                print(f"      {s['name']:<12} {s['size']:>4} bytes at 0x{s['offset']:x}")
            print("      -> find the source construct that made the compiler create them; "
                  "do not fabricate padding")
        other_missing = r["missing"]
        if other_missing:
            print(f"  missing {len(other_missing)} named symbol(s): "
                  f"{', '.join(s['name'] for s in other_missing[:5])}")
        if r["extra"]:
            print(f"  we emit {len(r['extra'])} symbol(s) retail does not: "
                  f"{', '.join(s['name'] for s in r['extra'][:5])}")
        if r.get("dead_stripped"):
            print(f"  {len(r['dead_stripped'])} symbol(s) look extra but the link map "
                  f"lists them for this file: "
                  f"{', '.join(s['name'] for s in r['dead_stripped'][:5])}")
            print("      -> the original build HAD these and dead-stripped them, so they "
                  "are absent from the extracted object only. Keep them")
        if r["resized"]:
            for s in r["resized"][:4]:
                print(f"  size differs: {s['name']} is {s['ours']} bytes, "
                      f"retail has {s['target']}")
        if r["head_shift"] is not None:
            print(f"  every shared symbol is offset by a uniform {r['head_shift']:+d} bytes "
                  "-- one fix at the head of the section")
        elif r["moved"]:
            print(f"  {len(r['moved'])} symbol(s) at different offsets (ordering differs)")
            if args.detail:
                for s in r["moved"][:10]:
                    print(f"      {s['name']:<28} retail 0x{s['target']:<5x} ours 0x{s['ours']:x}")

    print("\n" + "-" * 72)
    print(f"  {clean} section(s) match, {len(rows)} differ")
    gen = [r for r in rows if r["missing_generated"]]
    if gen:
        total = sum(sum(s["size"] for s in r["missing_generated"]) for r in gen)
        print(f"  {len(gen)} section(s) are missing compiler-generated constants "
              f"({total} bytes total)")
        print("  These have a known cause and are the ones to work first.")
    if shown < len(rows):
        print(f"  showing {shown} of {len(rows)}; --detail for all")

    if args.json_out:
        # Symbol payloads are raw `bytes`, which json cannot encode. Hex is the
        # useful form anyway -- it is what you paste back when comparing two
        # units' constants by content rather than by size.
        def encode(o):
            if isinstance(o, (bytes, bytearray)):
                return o.hex()
            raise TypeError(f"cannot serialise {o.__class__.__name__}")

        Path(args.json_out).write_text(
            json.dumps(rows, indent=1, default=encode), encoding="utf-8"
        )
        print(f"  wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
