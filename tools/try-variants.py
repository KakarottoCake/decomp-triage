#!/usr/bin/env python3

"""Compile several candidate bodies for one function and report what each produces.

This is the loop that actually closes frame-size mismatches, and it is worth a
real tool because doing it by hand is both slow and easy to get wrong: the
source has to be patched, one object rebuilt, the target object read back, and
the original restored even when something throws halfway through.

The output is the only thing that decides anything -- frame size against the
target's, byte-identity, and code length. A variant that reaches the right frame
but the wrong length told you the shape is plausible and the body is not.

It rebuilds a single object, not the tree, so a sweep of half a dozen variants
takes seconds.

Variants file format -- a name line starting with '===', then the body:

    === scalar result
    	int result = getResultFromAng(unk13C[0]);
    	for (int i = 1; i < 3; ++i) {
    		if (getResultFromAng(unk13C[i]) != result)
    			return -1;
    	}
    	return result;

    === three-element array
    	int results[3];
    	...

The body replaces everything between the function's opening and closing brace.
Indent with tabs; this writes the file that clang-format will later check.

The original file is restored on success, on failure, and on Ctrl-C. If the
process is killed hard, a .try-variants.bak sits next to the source.

Usage:
  python tools/try-variants.py src/Enemy/bosstelesa.cpp \\
      'int TTelesaSlot::getSlotResult()' variants.txt
  python tools/try-variants.py --keep BEST src/... 'sig' variants.txt
"""

import argparse
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

script_dir = Path(__file__).resolve().parent
root_dir = script_dir.parent


def load_sfd():
    """Reuse stack-frame-diff's ELF reader rather than carrying a second copy."""
    spec = importlib.util.spec_from_file_location("sfd", script_dir / "stack-frame-diff.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sfd"] = mod  # dataclasses need the module importable while it execs
    argv, sys.argv = sys.argv, ["stack-frame-diff.py"]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


def unit_for(source):
    """(target_object, our_object) for the unit that builds this source file."""
    source = source.replace("\\", "/").lstrip("./")
    units = json.loads((root_dir / "objdiff.json").read_text(encoding="utf-8"))["units"]
    for u in units:
        sp = (u.get("metadata") or {}).get("source_path")
        if sp and sp.replace("\\", "/").lstrip("./") == source:
            tp, bp = u.get("target_path"), u.get("base_path")
            if not tp or not bp:
                sys.exit(f"{source}: unit has no target/base object path")
            return root_dir / tp, root_dir / bp
    sys.exit(f"{source}: no unit in objdiff.json builds this file")


def parse_variants(path):
    out, name, body = [], None, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("==="):
            if name is not None:
                out.append((name, "\n".join(body).rstrip()))
            name, body = line[3:].strip() or f"variant {len(out) + 1}", []
        elif name is not None:
            body.append(line)
    if name is not None:
        out.append((name, "\n".join(body).rstrip()))
    if not out:
        sys.exit(f"{path}: no variants found (expected lines starting with ===)")
    return out


def find_function(text, signature):
    """(start, end) of the whole definition, and the body's slice inside it.

    Brace-counted rather than regex'd -- a body containing a nested block or a
    brace inside a string is normal and must not truncate the span.
    """
    start = text.find(signature)
    if start == -1:
        sys.exit(f"signature not found: {signature!r}")
    open_brace = text.find("{", start)
    if open_brace == -1:
        sys.exit(f"no opening brace after {signature!r}")
    depth, i, in_str = 0, open_brace, None
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return open_brace + 1, i
        i += 1
    sys.exit(f"unbalanced braces in {signature!r}")


def build(obj_rel, env):
    r = subprocess.run(["ninja", obj_rel], cwd=root_dir, capture_output=True, text=True, env=env)
    if r.returncode == 0:
        return None
    for line in (r.stdout + r.stderr).splitlines():
        if "rror" in line and "ninja:" not in line:
            return line.strip()
    return "build failed"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", help="the .cpp to patch, e.g. src/Enemy/bosstelesa.cpp")
    ap.add_argument("signature", help="the definition line, e.g. 'int TFoo::bar()'")
    ap.add_argument("variants", help="the variants file")
    ap.add_argument("--symbol", help="mangled symbol (default: inferred from the objects)")
    ap.add_argument("--keep", help="leave this variant applied instead of restoring")
    args = ap.parse_args(argv)

    sfd = load_sfd()
    src = root_dir / args.source
    if not src.is_file():
        sys.exit(f"no such file: {src}")
    target_obj, our_obj = unit_for(args.source)
    obj_rel = str(our_obj.relative_to(root_dir)).replace("\\", "/")

    original = io.open(src, encoding="utf-8", newline="").read()
    lo, hi = find_function(original, args.signature)
    nl = "\r\n" if "\r\n" in original[lo:hi] else "\n"
    variants = parse_variants(args.variants)

    env = dict(os.environ)
    # Some setups need a specific ninja ahead of whatever is on PATH -- on Windows
    # the MSYS2 ninja runs build rules through /bin/sh and eats backslashes. Point
    # DECOMP_NINJA_DIR at the directory holding the ninja you want.
    extra = os.environ.get("DECOMP_NINJA_DIR")
    if extra and os.path.isdir(extra):
        env["PATH"] = extra + os.pathsep + env["PATH"]

    backup = src.with_suffix(src.suffix + ".try-variants.bak")
    shutil.copyfile(src, backup)

    # Establish the target's frame from the extracted object once. If --symbol
    # wasn't given, infer it by rebuilding the file untouched and taking the one
    # symbol whose code span covers this definition -- but only when the file
    # defines exactly one candidate, otherwise ask rather than guess.
    target_funcs = sfd.read_functions(target_obj)
    symbol = args.symbol
    if not symbol:
        err = build(obj_rel, env)
        if err:
            shutil.move(backup, src)
            sys.exit(f"baseline build failed: {err}")
        ours = sfd.read_functions(our_obj)
        method = args.signature.split("(")[0].split("::")[-1].split()[-1]
        cands = [s for s in ours if s.startswith(method + "__") and s in target_funcs]
        if len(cands) != 1:
            shutil.move(backup, src)
            sys.exit(f"could not infer the symbol for {method!r} "
                     f"({len(cands)} candidates); pass --symbol")
        symbol = cands[0]

    tgt = target_funcs.get(symbol)
    if tgt is None:
        shutil.move(backup, src)
        sys.exit(f"{symbol} is not in the target object {target_obj}")
    tframe, tlen = sfd.frame_size(tgt), len(tgt)

    print(f"\n{symbol}")
    print(f"target: frame 0x{tframe:x}, {tlen} bytes\n")

    keep, width = None, max(len(n) for n, _ in variants)
    try:
        for name, body in variants:
            patched = original[:lo] + nl + body.replace("\n", nl) + nl + original[hi:]
            io.open(src, "w", encoding="utf-8", newline="").write(patched)
            err = build(obj_rel, env)
            if err:
                print(f"  {name:<{width}}  BUILD FAILED  {err}")
                continue
            code = sfd.read_functions(our_obj).get(symbol)
            if code is None:
                print(f"  {name:<{width}}  symbol vanished (inlined away?)")
                continue
            frame = sfd.frame_size(code)
            if code == tgt:
                verdict = "*** BYTE IDENTICAL ***"
            elif frame == tframe:
                verdict = "frame OK, code differs"
            else:
                verdict = f"frame {frame - tframe:+d}"
            print(f"  {name:<{width}}  frame 0x{frame:<4x} {len(code):>4}/{tlen} bytes   {verdict}")
            if args.keep and name == args.keep:
                keep = patched
    finally:
        io.open(src, "w", encoding="utf-8", newline="").write(keep if keep else original)
        build(obj_rel, env)
        backup.unlink(missing_ok=True)
        if keep:
            print(f"\nkept: {args.keep}")
        else:
            print(f"\nrestored {args.source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
