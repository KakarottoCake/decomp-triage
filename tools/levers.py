"""Read the mwcceppc lever catalogue (tools/levers.jsonl) and select what applies.

The catalogue is a distilled INDEX of compiler behaviours that have been used to turn a
non-matching function into a matching one. The evidence lives where each entry's `source`
points; this file is the lookup table, not the record.

Why a catalogue at all: most of what makes a function match is folklore about one specific
compiler, and it is currently spread across commit messages, Discord and people's heads.
Writing it down in a machine-readable form means a person can grep it by symptom and a model
can be handed exactly the subset that applies, instead of a wall of general advice.

Schema-compatible with tangosdev/sm64ds-decomp notes/levers.jsonl, deliberately: that
catalogue covers mwccarm on the DS, this one covers mwcceppc on the GameCube, and the same
reader can load either. To read a sibling project's catalogue:

    python tools/levers.py --repo ../sm64ds-decomp --catalogue notes/levers.jsonl --arch arm

Beware across the two: GC/1.x and mwccarm differ on real mechanics (mwccarm reclaims dead
stack slots, GC/1.x never does), so a lever proven on one is a hypothesis on the other until
someone reproduces it. `compiler_version` is there to keep that honest.

jsonl and stdlib json on purpose - a reader must not need anything installed.

Usage:
  python tools/levers.py                            # human-readable table
  python tools/levers.py --match "stack"            # filter by symptom or lever text
  python tools/levers.py --id inline-pass-ladder    # one entry, in full
  python tools/levers.py --format prompt            # the block to hand a model
  python tools/levers.py --format json              # machine-readable passthrough
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import textwrap

REPO = pathlib.Path(__file__).resolve().parent.parent
CATALOGUE = "tools/levers.jsonl"


def load(repo: str | None = None, catalogue: str | None = None) -> list[dict]:
    """Every lever in a catalogue. `repo` points at another checkout that carries one."""
    root = pathlib.Path(repo).resolve() if repo else REPO
    path = root / (catalogue or CATALOGUE)
    if not path.is_file():
        sys.exit(f"no lever catalogue at {path}")
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError as exc:
            sys.exit(f"{path}:{n}: unparseable line: {exc}")
        if "_comment" in rec:  # the header documents the schema; it is not a lever
            continue
        out.append(rec)
    return out


def select(
    levers: list[dict],
    arch: str | None = None,
    version: str | None = None,
    match: str | None = None,
    proven_only: bool = False,
) -> list[dict]:
    """Levers applicable to an arch and compiler version, optionally filtered by text.

    Version filtering matters more here than arch does: every entry is PowerPC, but GC/1.x
    and GC/2.x+ disagree about things as basic as whether dead stack is reclaimed. An entry
    that does not name the version you asked for is dropped rather than offered as a guess.
    """
    out = []
    for lv in levers:
        if arch and arch not in (lv.get("arch") or []):
            continue
        if version:
            known = lv.get("compiler_version") or []
            if known and version not in known:
                continue
        if proven_only and lv.get("confidence") != "proven":
            continue
        if match:
            hay = " ".join(
                str(lv.get(k, "")) for k in ("id", "symptom", "lever", "note", "tooling")
            ).lower()
            if match.lower() not in hay:
                continue
        out.append(lv)
    return out


def render_table(levers: list[dict]) -> str:
    rows = []
    for lv in levers:
        rows.append(f"  {lv['id']}   [{lv.get('confidence', '?')}]")
        rows.append(textwrap.fill(lv["symptom"], 92, initial_indent="      ", subsequent_indent="      "))
        rows.append("")
    return "\n".join(rows)


def render_full(levers: list[dict]) -> str:
    out = []
    for lv in levers:
        out.append(f"{lv['id']}   [{lv.get('confidence', '?')}]")
        ver = ", ".join(lv.get("compiler_version") or []) or "unspecified"
        out.append(f"  compiler   {lv.get('compiler', '?')} {ver}")
        for field in ("symptom", "lever", "diagnosis", "worked_example", "note", "tooling", "source"):
            if lv.get(field):
                body = textwrap.fill(
                    lv[field], 92, initial_indent="", subsequent_indent=" " * 13
                )
                out.append(f"  {field:<9}  {body}")
        out.append("")
    return "\n".join(out)


def render_prompt(levers: list[dict]) -> str:
    """The block a driver injects into a model's context.

    Kept terse and symptom-first: the reader is matching a specific failing function and
    needs to recognise its situation, not read a manual.
    """
    out = [
        "Known mwcceppc (GameCube PowerPC) matching levers. Each is a symptom you may be",
        "looking at, and what actually causes it in this compiler. These were established on",
        "this target; do not generalise them to another compiler without checking.",
        "",
    ]
    for lv in levers:
        out.append(f"- {lv['id']} [{lv.get('confidence', '?')}]")
        out.append(f"  symptom: {lv['symptom']}")
        out.append(f"  cause/fix: {lv['lever']}")
        if lv.get("diagnosis"):
            out.append(f"  how to diagnose: {lv['diagnosis']}")
        if lv.get("worked_example"):
            out.append(f"  worked example: {lv['worked_example']}")
        if lv.get("note"):
            out.append(f"  caveat: {lv['note']}")
        if lv.get("tooling"):
            out.append(f"  tooling: {lv['tooling']}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", help="read the catalogue from another checkout")
    ap.add_argument("--catalogue", help=f"path within the repo (default {CATALOGUE})")
    ap.add_argument("--arch", help="filter to an ISA, e.g. ppc")
    ap.add_argument("--version", help="filter to a compiler version, e.g. GC/1.2.5")
    ap.add_argument("--match", help="filter by text in the symptom, lever, note or tooling")
    ap.add_argument("--id", help="show one lever in full")
    ap.add_argument("--proven-only", action="store_true", help="drop anything not reproduced")
    ap.add_argument(
        "--format",
        choices=("table", "full", "prompt", "json"),
        default="table",
        help="table (default), full, prompt (for a model), or json",
    )
    args = ap.parse_args(argv)

    levers = load(args.repo, args.catalogue)
    if args.id:
        levers = [lv for lv in levers if lv["id"] == args.id]
        if not levers:
            sys.exit(f"no lever with id {args.id!r}")
        print(render_full(levers))
        return 0

    levers = select(levers, args.arch, args.version, args.match, args.proven_only)
    if not levers:
        print("no levers match that selection", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(levers, indent=1))
    elif args.format == "prompt":
        print(render_prompt(levers))
    elif args.format == "full":
        print(render_full(levers))
    else:
        print(f"\n{len(levers)} lever(s)\n")
        print(render_table(levers))
        print("  --format full for detail, --id <id> for one, --format prompt for a model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
