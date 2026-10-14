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
    def __init__(self, rid, pattern, message, fix, source, advisory=False, skip=(),
                 raw=False, only=()):
        self.id = rid
        self.re = re.compile(pattern, re.M)
        self.message = message
        self.fix = fix
        self.source = source
        self.advisory = advisory
        self.skip = tuple(skip)
        # raw: match with comments kept (strings still blanked), for rules about
        # comments themselves. only: file suffixes the rule applies to.
        self.raw = raw
        self.only = tuple(only)


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
                            rec.get("skip", ()), rec.get("raw", False),
                            rec.get("only", ())))
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
            # Decode explicitly. `text=True` uses the locale codec, which on
            # Windows is cp1252 and cannot decode the Japanese string literals
            # this tree contains -- git then hands back None and the caller
            # dies on `None.splitlines()` with no hint as to why.
            cwd=root_dir, capture_output=True, check=True,
        ).stdout.decode("utf-8", errors="replace")
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
    return re.sub(r"//.*$", "", strip_strings(line))


def strip_strings(line):
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)


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


FIX_INVENTED = {
    "dead": "check mario.MAP before touching it. A symbol present there is real "
            "retail code that was dead-stripped -- it must stay, unreferenced or "
            "not. Absent from the MAP and added by you, it is an inline the "
            "compiler never sees: a claim about the original that nothing supports, "
            "so delete it",
    "single": "a one-use helper usually belongs at its call site, or is a method on "
              "the class it operates on. Fold it unless the boundary is proven",
}


def invented_helpers(text, wanted, path):
    """File-local helper functions that no longer earn their place.

    Built in rather than configurable, because it is not a project convention --
    it is the signature of source invented to reach a match. A reviewer reads a
    `static` helper in a .cpp as a claim that the original had an inline function
    there. Two shapes make that claim badly:

      * never referenced -- the claim is not even used. PR #152 shipped two of
        these; the reviewer caught one and missed the other.
      * one call site -- the abstraction earns nothing, and a reviewer will ask
        why it exists at all.

    Headers are skipped: an inline in a class header is that class's API, not a
    guess about a translation unit. Only the definition line is reported, so this
    stays quiet unless the helper itself is part of the change.
    """
    if not path.endswith((".c", ".cpp")):
        return []

    hits = []
    for m in re.finditer(
        r"^[ 	]*static\s+(?:inline\s+)?[\w:<>,&*\s]+?[\s*&](\w+)\s*\([^;{]*\)\s*\{",
        text, re.M,
    ):
        name = m.group(1)
        lineno = text.count(chr(10), 0, m.start()) + 1
        if wanted is not None and lineno not in wanted:
            continue
        if re.fullmatch(r"dummy_?\d*", name):
            continue  # project idiom: an uncalled static that forces section order
        # Any mention of the name, not just `name(` -- a callback handed to the
        # engine is referenced by address and never "called". Counting only call
        # syntax reported 153 dead helpers across the tree, nearly all of them
        # legitimate callbacks.
        uses = len(re.findall(r"\b" + re.escape(name) + r"\b", text)) - 1
        if uses <= 0:
            hits.append((lineno, name, "dead",
                         f"{name}() is defined here and never referenced"))
        elif uses == 1:
            hits.append((lineno, name, "single",
                         f"{name}() has exactly one call site"))
    return hits


DECL = re.compile(
    r"^[ \t]+(?:const\s+)?(?:unsigned\s+|signed\s+)?[A-Za-z_][\w:<>]*[\s*&]+"
    r"\w+(?:\s*,\s*[*&]?\s*\w+)*\s*;\s*(?://.*)?$")


def top_declared_locals(text, wanted):
    """Functions whose body opens with a block of uninitialised declarations.

    That is Ghidra's layout, not the original authors'. The maintainer wants locals
    declared where they are first used. Three in a row is the threshold: one or
    two at the top is often just where they are first needed.
    """
    hits = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line != "{" or i == 0 or not re.search(r"\)\s*(?:const\s*)?$", lines[i - 1]):
            continue
        j = i + 1
        while j < len(lines) and DECL.match(lines[j]) and not re.match(r"^\s*return\b", lines[j]):
            j += 1
        if j - (i + 1) < 3:
            continue
        lineno = i + 2
        if wanted is not None and not (set(range(lineno, j + 1)) & wanted):
            continue
        hits.append((lineno, j - (i + 1)))
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
        with_comments = strip_strings(raw)
        for rule in rules:
            if any(s in path for s in SELF_DEFINING) or any(s in path for s in rule.skip):
                continue
            if rule.only and not path.endswith(rule.only):
                continue
            if f"review-lint: ignore {rule.id}" in raw:
                continue
            if rule.re.search(with_comments if rule.raw else line):
                findings.append((n, rule.id, rule.message, rule.fix, rule.source,
                                 raw.strip(), rule.advisory))

    for lineno, name, size in write_only_arrays(text, wanted):
        findings.append((
            lineno, "write-only-array",
            f"{name}[{size}] is declared but only {name}[0] is ever read",
            "if it exists to move the stack frame, delete it and leave the frame "
            "unmatched. Locals that only pad the frame are banned for AI work",
            "built in; maintainer rule 2026-10-13: AI matches up to stack frame size, no "
            "padding locals",
            f"{name}[{size}]", False,
        ))

    if path.endswith((".c", ".cpp")):
        for lineno, count in top_declared_locals(text, wanted):
            findings.append((
                lineno, "top-declared-locals",
                f"function opens with {count} uninitialised declarations",
                "declare each local where it is first assigned; the devs did not "
                "write C89-style blocks, Ghidra does",
                "maintainer: few temporaries, locals declared where used",
                text.splitlines()[lineno - 1].strip(), True,
            ))

    for lineno, name, kind, msg in invented_helpers(text, wanted, path):
        findings.append((
            lineno, f"invented-helper-{kind}", msg, FIX_INVENTED[kind],
            "a maintainer review: 'It is probably best to avoid this inline "
            "until it can be proven / disproven'",
            name, kind != "dead",
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
    # The Windows console codec is cp1252; a Shift-JIS string in a snippet would
    # otherwise kill the run halfway through the file list.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.list:
        for r in load_rules(args.rules):
            tag = "  [advisory]" if r.advisory else ""
            print(f"{r.id}{tag}\n  flags  {r.message}\n  fix    {r.fix}\n  from   {r.source}\n")
        print("write-only-array\n  flags  a local array where only [0] is read\n"
              "  fix    delete it; frame-only padding is banned for AI work\n"
              "  from   PR #134 getSlotResult; maintainer rule 2026-10-13\n")
        print("top-declared-locals  [advisory]\n  flags  a function that opens with "
              "three or more uninitialised declarations\n"
              "  fix    declare each local where it is first assigned\n"
              "  from   maintainer: locals are declared where used, not C-style\n")
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
