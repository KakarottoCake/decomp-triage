#!/usr/bin/env python3

"""Catch the review comments you already know you're going to get.

A rule here is not a style opinion -- it is a review comment a maintainer already
made, written down so nobody has to make it twice. On a decompilation project the
maintainer's attention is the scarce resource, and a round trip spent on "use the
accessor" is a round trip not spent on anything that needed a human.

Because rules are per-project by nature, they live in tools/review-rules.jsonl.
What ships is an example set from one GameCube project; replace it with your own
project's review history.

Diff-scoped by default: only lines your branch added or changed are checked, so
turning it on part-way through a project doesn't bury you in pre-existing hits
(the example rules match a few hundred lines of already-merged code). Pass --all
to scan whole files anyway.

Each finding cites where the rule came from, so you can argue with it. Some are
wrong in context -- suppress one line with a trailing comment:

    something->unk4->unk20;  // review-lint: ignore chained-unk

Exit status:
  0  no findings
  1  at least one finding

Usage:
  python tools/review-lint.py                      # changed lines vs origin/main
  python tools/review-lint.py --base HEAD~3
  python tools/review-lint.py --all src/Enemy/bosstelesa.cpp
  python tools/review-lint.py --list               # explain every rule
  python tools/review-lint.py --strict             # include advisory rules
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

script_dir = os.path.dirname(os.path.realpath(__file__))
root_dir = os.path.abspath(os.path.join(script_dir, ".."))
RULES_FILE = "review-rules.jsonl"

# Paths where a rule must never fire, loaded from the rule file's header. These
# are the files that DEFINE the accessor a rule tells you to use -- the accessor
# has to touch the raw field, or it could not exist.
SELF_DEFINING: tuple = ()


class Rule:
    def __init__(self, rid, pattern, message, fix, source, advisory=False, skip=()):
        self.id = rid
        self.re = re.compile(pattern, re.M)
        self.message = message
        self.fix = fix
        self.source = source
        self.advisory = advisory
        self.skip = tuple(skip)


def load_rules(path=None):
    """Rules come from a data file, because they are per-project by nature.

    A lint rule here is not a style opinion -- it is a review comment somebody
    already made, encoded so nobody has to make it twice. That makes the rule set
    specific to one codebase and its maintainers, so it lives in
    tools/review-rules.jsonl rather than in this file. What ships is an example
    set from a GameCube project; replace it with your own.
    """
    path = pathlib.Path(path or (pathlib.Path(__file__).resolve().parent / RULES_FILE))
    if not path.is_file():
        sys.exit(f"no rule file at {path} -- see review-rules.jsonl for the format")
    global SELF_DEFINING
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError as exc:
            sys.exit(f"{path}:{n}: unparseable: {exc}")
        if "_comment" in rec:
            SELF_DEFINING = tuple(rec.get("skip_paths", ()))
            continue
        try:
            out.append(Rule(rec["id"], rec["pattern"], rec["message"], rec["fix"],
                            rec.get("source", ""), rec.get("advisory", False),
                            rec.get("skip", ())))
        except (KeyError, re.error) as exc:
            sys.exit(f"{path}:{n}: bad rule: {exc}")
    return out


def norm(p):
    return p.replace("\\", "/").lstrip("./")


def changed_lines(base):
    """{path: {lineno, ...}} for lines this branch adds or modifies.

    Uses the merge base (three-dot) to match what CI reviews: lines that arrived
    on upstream after we branched are not ours to clean up.
    """
    try:
        diff = subprocess.run(
            ["git", "diff", "-U0", f"{base}...HEAD", "--", "*.cpp", "*.hpp", "*.h"],
            cwd=root_dir, capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        sys.exit(f"git diff against {base!r} failed: {exc.stderr.strip()}")

    out, path = {}, None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = norm(line[6:])
            out.setdefault(path, set())
        elif line.startswith("@@") and path:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = 1 if m.group(2) is None else int(m.group(2))
                out[path].update(range(start, start + count))
    return {p: ls for p, ls in out.items() if ls}


def strip_noise(line):
    """Blank out string literals and comments so rules don't fire inside them."""
    line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
    line = re.sub(r"//.*$", "", line)
    return line


def write_only_arrays(text, wanted):
    """Local arrays whose only element ever read back is [0].

    Built in rather than configurable, because it is not a project convention --
    it is the signature of a match bought with source nobody would write. A
    three-element array read only at [0] usually exists to move the stack frame.
    Sometimes that is the right call; either way the commit message should say so
    rather than leaving a reviewer to notice.

    Deliberately narrow -- literal indices only, no loops, no address-of -- so it
    stays a real signal instead of noise.
    """
    hits = []
    for m in re.finditer(r"^\s*(?:u8|s8|u16|s16|u32|s32|int|f32|f64|double)\s+(\w+)\[(\d+)\]\s*;", text, re.M):
        name, size = m.group(1), int(m.group(2))
        if size < 2:
            continue
        lineno = text.count("\n", 0, m.start()) + 1
        if wanted is not None and lineno not in wanted:
            continue
        body = text[m.end():]
        end = body.find("\n}")
        body = body[: end if end != -1 else len(body)]
        if re.search(r"[&*]\s*" + re.escape(name) + r"\b", body):
            continue  # address taken; the whole object is live
        idx = set(re.findall(re.escape(name) + r"\[(\w+)\]", body))
        if not idx or idx - {"0"}:
            continue  # nothing found, or something other than [0] is touched
        hits.append((lineno, name, size))
    return hits


def scan(path, wanted, rules):
    full = os.path.join(root_dir, path)
    if not os.path.isfile(full):
        return []
    with open(full, encoding="utf-8", errors="replace") as f:
        text = f.read()

    findings = []
    for n, raw in enumerate(text.splitlines(), 1):
        if wanted is not None and n not in wanted:
            continue
        line = strip_noise(raw)
        for rule in rules:
            if any(s in path for s in SELF_DEFINING) or any(s in path for s in rule.skip):
                continue
            if f"review-lint: ignore {rule.id}" in raw:
                continue
            if rule.re.search(line):
                findings.append((n, rule.id, rule.message, rule.fix, rule.source,
                                 raw.strip(), rule.advisory))

    for lineno, name, size in write_only_arrays(text, wanted):
        findings.append((
            lineno, "write-only-array",
            f"{name}[{size}] is declared but only {name}[0] is ever read",
            "if this exists to move the stack frame, say so in the commit message "
            "-- it reads as fitted to the compiler, and readable source usually "
            "outranks a match",
            "built in: the signature of a match bought with unnatural source",
            f"{name}[{size}]", False,
        ))

    return sorted(findings)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="files to scan (default: changed files)")
    ap.add_argument("--base", default="origin/main", help="diff base (default origin/main)")
    ap.add_argument("--all", action="store_true", help="scan whole files, not just changed lines")
    ap.add_argument("--strict", action="store_true", help="include advisory rules")
    ap.add_argument("--list", action="store_true", help="explain every rule and exit")
    ap.add_argument("--rules", help=f"rule file (default tools/{RULES_FILE})")
    args = ap.parse_args(argv)

    if args.list:
        for r in load_rules(args.rules):
            tag = "  [advisory]" if r.advisory else ""
            print(f"{r.id}{tag}\n  flags  {r.message}\n  fix    {r.fix}\n  from   {r.source}\n")
        print("write-only-array\n  flags  a local array where only [0] is read\n"
              "  fix    keep it only if the commit message explains why\n"
              "  from   PR #134 getSlotResult\n")
        return 0

    all_rules = load_rules(args.rules)
    rules = all_rules if args.strict else [r for r in all_rules if not r.advisory]

    if args.paths:
        targets = {norm(p): None for p in args.paths}
    else:
        targets = changed_lines(args.base)
        if args.all:
            targets = {p: None for p in targets}
        if not targets:
            print(f"no changed sources vs {args.base}")
            return 0

    total, advisory_only = 0, 0
    for path in sorted(targets):
        findings = scan(path, targets[path], rules)
        if not findings:
            continue
        print(f"\n{path}")
        for lineno, rid, msg, fix, source, snippet, adv in findings:
            mark = "note" if adv else "warn"
            print(f"  {path}:{lineno}: [{mark}] {rid}: {msg}")
            print(f"      {snippet}")
            print(f"      -> {fix}")
            print(f"      ({source})")
            total += 1
            advisory_only += adv

    real = total - advisory_only
    print()
    if not total:
        scope = "changed lines" if not args.all else "whole files"
        print(f"review-lint: clean ({scope}, {len(targets)} file(s))")
        return 0
    print(f"review-lint: {total} finding(s), {real} worth acting on")
    if not args.strict:
        print("(--strict adds advisory rules, --list explains each one)")
    return 1 if real else 0


if __name__ == "__main__":
    sys.exit(main())
