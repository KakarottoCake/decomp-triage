#!/usr/bin/env python3

"""Generate candidate respellings of a C++ function body for try-variants.py.

This tool extracts a function's body from source and generates semantically
identical C++ candidate respellings across several transform families. The
output is written in the variants-file format consumed by tools/try-variants.py.

This tool NEVER compiles, NEVER edits source in place, and NEVER decides anything.
It only proposes candidate variants for human inspection and automated testing.

Note on taxonomy and banned levers:
The transform families here were adapted from an external compiler taxonomy whose
success criterion had no readability gate. For this project, human readability
and maintainability are paramount. Accordingly:
  - BANNED: 'volatile' (no adding volatile locals or volatile reads to force stack slots)
  - BANNED: 'switch-padding' (no adding empty switch cases)
  - BANNED: 'pragma' (no wrapping in #pragma peephole / scheduling / fp_contract)
  - DELIBERATELY ABSENT: 'cast' and 'width' (sign/width changes alter semantics
    and require human domain knowledge).

Usage:
  python tools/gen-variants.py <source.cpp> '<signature>' [--families a,b,c] [--out FILE]
  python tools/gen-variants.py --list
"""

import argparse
import io
import itertools
import os
import re
import sys
from pathlib import Path

DEFAULT_FAMILIES = ["bool", "compare", "return", "decl-order", "local-form"]
ALL_FAMILIES = [
    "bool",
    "compare",
    "return",
    "decl-order",
    "local-form",
    "reassociate",
    "param-inversion",
    "bitfield",
]

FAMILY_DESCRIPTIONS = {
    "bool": "toggle between `x`, `x != 0`, `x != FALSE`, `x == TRUE` in conditions",
    "compare": "comparison aliases of the same test (a > b <-> b < a, !(a == b) <-> a != b)",
    "return": "`return TRUE;`/`return FALSE;` vs returning a variable already holding that value",
    "decl-order": "permute adjacent local declarations (capped at 6 permutations)",
    "local-form": "introduce named local for subexpression occurring >1 time, and the reverse",
    "reassociate": "permute and parenthesize 3-term integer additions (skips floats)",
    "param-inversion": "hoist call argument into temp declared immediately above call",
    "bitfield": "rewrite (x >> N) & M into equivalent form and back",
}


def print_family_list():
    print("Available families (default: bool,compare,return,decl-order,local-form):")
    for f in ALL_FAMILIES:
        print(f"  {f:<16} {FAMILY_DESCRIPTIONS[f]}")
    print("\nDeliberately absent (change semantics; must be human decisions):")
    print("  cast             type cast adjustments (e.g. (u32) vs (s32))")
    print("  width            integer sign/width swaps (e.g. u8 vs u32)")
    print("\nHard ban (produce unacceptable source nobody would write):")
    print("  volatile         no adding volatile locals or volatile reads")
    print("  switch-padding   no adding empty switch cases")
    print("  pragma           no wrapping in #pragma peephole / scheduling / fp_contract")


def find_function(text, signature):
    """(start, end) of the whole definition, and the body's slice inside it.

    Brace-counted rather than regex'd -- matches try-variants.py.
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


def extract_body(text, lo, hi):
    raw = text[lo:hi]
    lines = raw.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def infer_type_from_source(expr, source_text):
    """Try to infer the C++ type of an expression from declarations in source."""
    expr = expr.strip()
    m = re.search(r"(\w+)\s*\(", expr)
    if m:
        fn_name = m.group(1)
        pat = rf"(?:static\s+)?([A-Za-z0-9_]+(?:\s*\*|\s*&)?)\s+{fn_name}\s*\("
        m2 = re.search(pat, source_text)
        if m2:
            return m2.group(1).strip()

    if "getPane" in expr:
        return "J2DPane*"
    if "getPosition" in expr or "getPos" in expr:
        return "Vec"
    if "getHeight" in expr or "getWidth" in expr:
        return "f32"
    if "isVisible" in expr or "isEqual" in expr:
        return "bool"
    if "getStatus" in expr:
        return "u32"
    if re.search(r"\b\d+\.\d*f?\b|\b\d+f\b", expr):
        return "f32"
    return "int"


def find_matching_paren(text, start_idx):
    """Given index of '(', return index of matching ')'."""
    depth = 0
    in_str = None
    i = start_idx
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
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def split_top_level_compare(expr):
    """Split expr into (lhs, op, rhs) at depth 0 comparison operator."""
    depth = 0
    in_str = None
    ops = ["==", "!=", "<=", ">=", "<", ">"]
    for i in range(len(expr)):
        c = expr[i]
        if in_str:
            if c == "\\":
                continue
            if c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0:
            if i > 0 and expr[i - 1:i + 1] == "->":
                continue
            if expr[i:i + 2] in ("<<", ">>"):
                continue
            for op in ops:
                if expr[i:i + len(op)] == op:
                    lhs = expr[:i].strip()
                    rhs = expr[i + len(op):].strip()
                    if lhs and rhs:
                        return lhs, op, rhs
    return None


# -----------------------------------------------------------------------------
# Family 1: bool
# -----------------------------------------------------------------------------
def transform_bool(body, signature, source_text):
    variants = []
    seen = set()

    for m in re.finditer(r"\b(if|while)\s*\(", body):
        open_idx = m.end() - 1
        close_idx = find_matching_paren(body, open_idx)
        if close_idx == -1:
            continue

        cond = body[open_idx + 1:close_idx].strip()
        alts = []

        # Check existing condition form
        if cond.endswith("== 0"):
            inner = cond[:-4].strip()
            alts.extend([f"!({inner})" if " " in inner else f"!{inner}", f"{inner} == FALSE", f"{inner} != TRUE"])
        elif cond.endswith("== FALSE"):
            inner = cond[:-8].strip()
            alts.extend([f"!({inner})" if " " in inner else f"!{inner}", f"{inner} == 0", f"{inner} != TRUE"])
        elif cond.endswith("== false"):
            inner = cond[:-8].strip()
            alts.extend([f"!({inner})" if " " in inner else f"!{inner}", f"{inner} == 0", f"{inner} == FALSE"])
        elif cond.endswith("!= 0") or cond.endswith("!= 0U"):
            inner = cond[:-4].strip()
            alts.extend([inner, f"{inner} != FALSE", f"{inner} == TRUE"])
        elif cond.endswith("!= FALSE"):
            inner = cond[:-8].strip()
            alts.extend([inner, f"{inner} != 0", f"{inner} == TRUE"])
        elif cond.endswith("!= false"):
            inner = cond[:-8].strip()
            alts.extend([inner, f"{inner} != 0", f"{inner} == TRUE"])
        elif cond.endswith("== TRUE"):
            inner = cond[:-7].strip()
            alts.extend([inner, f"{inner} != 0", f"{inner} != FALSE"])
        elif cond.endswith("== true"):
            inner = cond[:-7].strip()
            alts.extend([inner, f"{inner} != 0", f"{inner} != FALSE"])
        elif cond.endswith("!= TRUE"):
            inner = cond[:-7].strip()
            alts.extend([f"!({inner})" if " " in inner else f"!{inner}", f"{inner} == 0", f"{inner} == FALSE"])
        elif cond.endswith("!= true"):
            inner = cond[:-7].strip()
            alts.extend([f"!({inner})" if " " in inner else f"!{inner}", f"{inner} == 0", f"{inner} == FALSE"])
        elif cond.startswith("!"):
            inner = cond[1:].strip()
            if inner.startswith("(") and find_matching_paren(inner, 0) == len(inner) - 1:
                inner = inner[1:-1].strip()
            alts.extend([f"{inner} == 0", f"{inner} == FALSE", f"{inner} != TRUE"])
        else:
            if split_top_level_compare(cond) is None and "&&" not in cond and "||" not in cond:
                alts.extend([f"{cond} != 0", f"{cond} != FALSE", f"{cond} == TRUE"])

        for alt in alts:
            if alt == cond:
                continue
            new_body = body[:open_idx + 1] + alt + body[close_idx:]
            if new_body != body and new_body not in seen:
                seen.add(new_body)
                variants.append((f"bool-{len(variants) + 1}", new_body))

    return variants


# -----------------------------------------------------------------------------
# Family 2: compare
# -----------------------------------------------------------------------------
def transform_compare(body, signature, source_text):
    variants = []
    seen = set()

    for m in re.finditer(r"\b(if|while|for)\s*\(", body):
        open_idx = m.end() - 1
        close_idx = find_matching_paren(body, open_idx)
        if close_idx == -1:
            continue

        clause = body[open_idx + 1:close_idx]
        conds = []
        if m.group(1) == "for":
            parts = clause.split(";")
            if len(parts) == 3:
                conds.append((parts[1], open_idx + 1 + len(parts[0]) + 1, open_idx + 1 + len(parts[0]) + 1 + len(parts[1])))
        else:
            conds.append((clause, open_idx + 1, close_idx))

        for cond_str, c_start, c_end in conds:
            cond_stripped = cond_str.strip()
            alts = []

            # Check if negated compare !(a == b), !(a < b), etc.
            if cond_stripped.startswith("!(") and cond_stripped.endswith(")"):
                inner = cond_stripped[2:-1].strip()
                res = split_top_level_compare(inner)
                if res:
                    lhs, op, rhs = res
                    inv_map = {
                        "==": "!=",
                        "!=": "==",
                        "<": ">=",
                        ">": "<=",
                        "<=": ">",
                        ">=": "<",
                    }
                    if op in inv_map:
                        alts.append(f"{lhs} {inv_map[op]} {rhs}")

            res = split_top_level_compare(cond_stripped)
            if res:
                lhs, op, rhs = res
                swap_map = {
                    "==": "==",
                    "!=": "!=",
                    "<": ">",
                    ">": "<",
                    "<=": ">=",
                    ">=": "<=",
                }
                if op in swap_map:
                    alts.append(f"{rhs} {swap_map[op]} {lhs}")

                inv_map = {
                    "==": f"!({lhs} != {rhs})",
                    "!=": f"!({lhs} == {rhs})",
                    "<": f"!({lhs} >= {rhs})",
                    ">": f"!({lhs} <= {rhs})",
                    "<=": f"!({lhs} > {rhs})",
                    ">=": f"!({lhs} < {rhs})",
                }
                if op in inv_map:
                    alts.append(inv_map[op])

                # Integer literal boundary adjustments
                if re.match(r"^-?\d+$", rhs):
                    val = int(rhs)
                    if op == "<":
                        alts.append(f"{lhs} <= {val - 1}")
                        alts.append(f"{lhs} != {val}")
                    elif op == "<=":
                        alts.append(f"{lhs} < {val + 1}")
                    elif op == ">":
                        alts.append(f"{lhs} >= {val + 1}")
                    elif op == ">=":
                        alts.append(f"{lhs} > {val - 1}")

            for alt in alts:
                if alt == cond_stripped:
                    continue
                leading_ws = cond_str[:len(cond_str) - len(cond_str.lstrip())]
                trailing_ws = cond_str[len(cond_str.rstrip()):]
                replacement = leading_ws + alt + trailing_ws
                new_body = body[:c_start] + replacement + body[c_end:]
                if new_body != body and new_body not in seen:
                    seen.add(new_body)
                    variants.append((f"compare-{len(variants) + 1}", new_body))

    return variants


# -----------------------------------------------------------------------------
# Family 3: return
# -----------------------------------------------------------------------------
def transform_return(body, signature, source_text):
    variants = []
    seen = set()

    sig_clean = signature.split("(")[0].strip()
    words = sig_clean.split()
    ret_type = " ".join(words[:-1]) if len(words) > 1 else ""

    rep_map = {
        r"\breturn\s+TRUE\s*;": ["return 1;", "return true;"],
        r"\breturn\s+FALSE\s*;": ["return 0;", "return false;"],
        r"\breturn\s+true\s*;": ["return TRUE;", "return 1;"],
        r"\breturn\s+false\s*;": ["return FALSE;", "return 0;"],
        r"\breturn\s+NULL\s*;": ["return 0;", "return nullptr;"],
        r"\breturn\s+nullptr\s*;": ["return NULL;", "return 0;"],
    }
    if ret_type and ret_type not in ("void", "void*"):
        rep_map[r"\breturn\s+1\s*;"] = ["return TRUE;", "return true;"]
        rep_map[r"\breturn\s+0\s*;"] = ["return FALSE;", "return false;"]

    for pattern, replacements in rep_map.items():
        for m in re.finditer(pattern, body):
            orig_match = m.group(0)
            for r in replacements:
                if r == orig_match:
                    continue
                new_body = body[:m.start()] + r + body[m.end():]
                if new_body != body and new_body not in seen:
                    seen.add(new_body)
                    variants.append((f"return-{len(variants) + 1}", new_body))

    # Pattern: if (cond) return TRUE; return FALSE; -> return cond;
    m_if_ret = re.search(
        r"(\t*)if\s*\((.*?)\)\s*\{\s*return\s+(TRUE|1|true)\s*;\s*\}\s*return\s+(FALSE|0|false)\s*;",
        body,
        re.DOTALL,
    )
    if m_if_ret:
        indent, cond = m_if_ret.group(1), m_if_ret.group(2).strip()
        alts = [
            f"{indent}return {cond};",
            f"{indent}return ({cond}) ? TRUE : FALSE;",
            f"{indent}return ({cond}) != 0;",
        ]
        for alt in alts:
            new_body = body[:m_if_ret.start()] + alt + body[m_if_ret.end():]
            if new_body != body and new_body not in seen:
                seen.add(new_body)
                variants.append((f"return-{len(variants) + 1}", new_body))

    # Pattern: if (cond) return FALSE; return TRUE; -> return !cond;
    m_if_ret_neg = re.search(
        r"(\t*)if\s*\((.*?)\)\s*\{\s*return\s+(FALSE|0|false)\s*;\s*\}\s*return\s+(TRUE|1|true)\s*;",
        body,
        re.DOTALL,
    )
    if m_if_ret_neg:
        indent, cond = m_if_ret_neg.group(1), m_if_ret_neg.group(2).strip()
        alts = [
            f"{indent}return !({cond});",
            f"{indent}return ({cond}) == 0;",
        ]
        for alt in alts:
            new_body = body[:m_if_ret_neg.start()] + alt + body[m_if_ret_neg.end():]
            if new_body != body and new_body not in seen:
                seen.add(new_body)
                variants.append((f"return-{len(variants) + 1}", new_body))

    # Pattern: hoisting/un-hoisting return variable
    if ret_type and ret_type != "void":
        lines = body.splitlines()
        if lines:
            last_line = lines[-1]
            m_ret_expr = re.match(r"^(\t*)return\s+([^;]+)\s*;$", last_line)
            if m_ret_expr:
                indent, expr = m_ret_expr.group(1), m_ret_expr.group(2).strip()
                if not re.match(r"^[A-Za-z0-9_]+$", expr) and not expr.isdigit():
                    res_lines = list(lines)
                    res_lines[-1] = f"{indent}{ret_type} res = {expr};\n{indent}return res;"
                    new_body = "\n".join(res_lines)
                    if new_body != body and new_body not in seen:
                        seen.add(new_body)
                        variants.append((f"return-{len(variants) + 1}", new_body))

    return variants


# -----------------------------------------------------------------------------
# Family 4: decl-order
# -----------------------------------------------------------------------------
def is_declaration_line(line):
    s = line.strip()
    if not s.endswith(";"):
        return False
    if re.match(r"^(if|else|while|for|do|switch|case|default|return|goto|break|continue|typedef|struct|class|enum|public|private|protected|namespace|#|//|/\*|\*)", s):
        return False
    if re.search(r"(->|\.|\[.*?\])\s*=", s):
        return False
    if "->" in s.split("=")[0] or "." in s.split("=")[0]:
        return False
    m = re.match(r"^(?:const\s+)?([A-Za-z0-9_<>:]+[\s*&]+)+([A-Za-z0-9_]+)(?:\s*=\s*([^;]+))?\s*;$", s)
    return m is not None


def extract_decl_info(line):
    s = line.strip()
    m = re.match(r"^(?:const\s+)?([A-Za-z0-9_<>:]+[\s*&]+)+([A-Za-z0-9_]+)(?:\s*=\s*([^;]+))?\s*;$", s)
    if m:
        var_name = m.group(2)
        init_expr = m.group(3) or ""
        return var_name, init_expr
    return None, None


def transform_decl_order(body, signature, source_text):
    variants = []
    seen = set()
    lines = body.splitlines()

    groups = []
    curr_group = []
    for i, line in enumerate(lines):
        if is_declaration_line(line):
            indent = line[:len(line) - len(line.lstrip())]
            if curr_group and curr_group[-1][2] != indent:
                if len(curr_group) >= 2:
                    groups.append(curr_group)
                curr_group = [(i, line, indent)]
            else:
                curr_group.append((i, line, indent))
        else:
            if len(curr_group) >= 2:
                groups.append(curr_group)
            curr_group = []
    if len(curr_group) >= 2:
        groups.append(curr_group)

    for group in groups:
        names = []
        inits = []
        for _, line, _ in group:
            vname, init = extract_decl_info(line)
            names.append(vname)
            inits.append(init)

        dep_found = False
        for idx, init in enumerate(inits):
            tokens = set(re.findall(r"\b[A-Za-z0-9_]+\b", init))
            other_names = [names[j] for j in range(len(names)) if j != idx and names[j]]
            if any(n in tokens for n in other_names):
                dep_found = True
                break

        if dep_found:
            continue

        n_decls = len(group)
        indices = list(range(n_decls))
        count = 0
        for p in itertools.permutations(indices):
            if list(p) == indices:
                continue
            count += 1
            if count > 6:
                break
            new_lines = list(lines)
            for new_pos, orig_idx in zip([g[0] for g in group], p):
                new_lines[new_pos] = group[orig_idx][1]
            new_body = "\n".join(new_lines)
            if new_body != body and new_body not in seen:
                seen.add(new_body)
                variants.append((f"decl-order-{len(variants) + 1}", new_body))

    return variants


# -----------------------------------------------------------------------------
# Family 5: local-form
# -----------------------------------------------------------------------------
def transform_local_form(body, signature, source_text):
    variants = []
    seen = set()

    # Direction 1: Introduce local for subexpression occurring >= 2 times
    candidates = re.findall(r"\b[A-Za-z0-9_]+->[A-Za-z0-9_]+\(\)", body)
    counts = {}
    for c in candidates:
        counts[c] = counts.get(c, 0) + 1

    for subexpr, cnt in counts.items():
        if cnt < 2:
            continue
        m_meth = re.search(r"->([A-Za-z0-9_]+)\(\)", subexpr)
        meth_name = m_meth.group(1) if m_meth else "sub"
        var_name = meth_name.lstrip("get")
        if not var_name:
            var_name = "pane" if "Pane" in meth_name else "temp"
        var_name = var_name[0].lower() + var_name[1:]
        if re.search(rf"\b{var_name}\b", body):
            var_name = f"{var_name}Val"

        inferred_type = infer_type_from_source(subexpr, source_text)

        lines = body.splitlines()
        first_line_idx = -1
        for idx, l in enumerate(lines):
            if subexpr in l and not is_declaration_line(l):
                first_line_idx = idx
                break

        if first_line_idx != -1:
            first_line = lines[first_line_idx]
            indent = first_line[:len(first_line) - len(first_line.lstrip())]
            decl = f"{indent}{inferred_type} {var_name} = {subexpr};"
            new_lines = []
            for idx, l in enumerate(lines):
                if idx == first_line_idx:
                    new_lines.append(decl)
                if idx >= first_line_idx:
                    new_lines.append(l.replace(subexpr, var_name))
                else:
                    new_lines.append(l)
            new_body = "\n".join(new_lines)
            if new_body != body and new_body not in seen:
                seen.add(new_body)
                variants.append((f"local-form-{len(variants) + 1}", new_body))

    # Direction 2: Inline a local variable used only in simple expressions
    lines = body.splitlines()
    for idx, l in enumerate(lines):
        if is_declaration_line(l):
            vname, init = extract_decl_info(l)
            if vname and init and not any(op in init for op in [";", "{", "}"]):
                # Ensure vname is never reassigned, compound-assigned, or address-taken
                rest = "\n".join(lines[idx + 1:])
                mutation_pat = rf"(&\s*{vname}\b|\+\+\s*{vname}\b|--\s*{vname}\b|\b{vname}\s*\+\+|\b{vname}\s*--|\b{vname}\s*(?:[\+\-\*/%&|\^]|<<|>>)?=|\b{vname}\s*\[.*?\]\s*=)"
                if not re.search(mutation_pat, rest):
                    # Replace vname with init in rest
                    rest_inlined = re.sub(rf"\b{vname}\b", init, rest)
                    new_lines = lines[:idx] + rest_inlined.splitlines()
                    new_body = "\n".join(new_lines)
                    if new_body != body and new_body not in seen:
                        seen.add(new_body)
                        variants.append((f"local-form-{len(variants) + 1}", new_body))

    return variants


# -----------------------------------------------------------------------------
# Family 6: reassociate
# -----------------------------------------------------------------------------
def transform_reassociate(body, signature, source_text):
    variants = []
    seen = set()

    float_indicators = ["f32", "float", "double", "f64", "getHeight", "getWidth", "getX", "getY", "getZ", "mPosition", "mBounds", "mScale"]

    pattern = r"([A-Za-z0-9_]+)\s*\+\s*([A-Za-z0-9_]+)\s*\+\s*([A-Za-z0-9_]+)"
    for m in re.finditer(pattern, body):
        a, b, c = m.group(1), m.group(2), m.group(3)
        terms = [a, b, c]
        if any(re.search(r"\b\d+\.\d*f?\b|\b\d+f\b", t) for t in terms):
            continue
        if any(any(ind in t for ind in float_indicators) for t in terms):
            continue

        alts = [
            f"({a} + {b}) + {c}",
            f"{a} + ({b} + {c})",
            f"({a} + {c}) + {b}",
            f"{b} + ({a} + {c})",
            f"({b} + {c}) + {a}",
            f"{c} + ({a} + {b})",
        ]
        for alt in alts:
            if alt == m.group(0):
                continue
            new_body = body[:m.start()] + alt + body[m.end():]
            if new_body != body and new_body not in seen:
                seen.add(new_body)
                variants.append((f"reassociate-{len(variants) + 1}", new_body))

    return variants


# -----------------------------------------------------------------------------
# Family 7: param-inversion
# -----------------------------------------------------------------------------
def transform_param_inversion(body, signature, source_text):
    variants = []
    seen = set()

    keywords = {"if", "while", "for", "switch", "return", "catch", "sizeof"}

    for m in re.finditer(r"\b([A-Za-z0-9_]+(?:->[A-Za-z0-9_]+)?)\s*\(", body):
        fn_full = m.group(1)
        base_name = fn_full.split("->")[-1]
        if base_name in keywords:
            continue

        open_idx = m.end() - 1
        close_idx = find_matching_paren(body, open_idx)
        if close_idx == -1:
            continue

        args_str = body[open_idx + 1:close_idx]
        if not args_str.strip():
            continue

        depth = 0
        in_str = None
        args = []
        cur = []
        for ch in args_str:
            if in_str:
                if ch == "\\":
                    cur.append(ch)
                    continue
                if ch == in_str:
                    in_str = None
                cur.append(ch)
            elif ch in "\"'":
                in_str = ch
                cur.append(ch)
            elif ch in "([":
                depth += 1
                cur.append(ch)
            elif ch in ")]":
                depth -= 1
                cur.append(ch)
            elif ch == "," and depth == 0:
                args.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if cur:
            args.append("".join(cur).strip())

        for arg_idx, arg in enumerate(args):
            is_call = bool(re.search(r"\b[A-Za-z0-9_]+\s*\(.*?\)", arg))
            is_math = bool(re.search(r"[+\*/|&^]", arg)) or (bool(re.search(r"-", arg)) and not arg.startswith("-"))
            is_member_chain = bool(re.search(r"->.*\.", arg))
            if not (is_call or is_math or is_member_chain):
                continue

            inferred_type = infer_type_from_source(arg, source_text)
            temp_name = "offset" if "Offset" in arg or "offset" in arg else "temp"
            if re.search(rf"\b{temp_name}\b", body):
                temp_name = f"{temp_name}Val"

            line_start = body.rfind("\n", 0, m.start())
            if line_start == -1:
                line_start = 0
            else:
                line_start += 1
            line_end = body.find("\n", close_idx)
            if line_end == -1:
                line_end = len(body)

            indent = ""
            for c in body[line_start:]:
                if c in ("\t", " "):
                    indent += c
                else:
                    break

            hoisted_decl = f"{indent}{inferred_type} {temp_name} = {arg};\n"

            new_args = list(args)
            new_args[arg_idx] = temp_name
            new_args_str = ", ".join(new_args)

            new_body = (
                body[:line_start]
                + hoisted_decl
                + body[line_start:open_idx + 1]
                + new_args_str
                + body[close_idx:]
            )

            if new_body != body and new_body not in seen:
                seen.add(new_body)
                variants.append((f"param-inversion-{len(variants) + 1}", new_body))

    return variants


# -----------------------------------------------------------------------------
# Family 8: bitfield
# -----------------------------------------------------------------------------
def transform_bitfield(body, signature, source_text):
    variants = []
    seen = set()

    # Pattern 1: (x >> N) & M
    for m in re.finditer(r"\(\s*([A-Za-z0-9_]+)\s*>>\s*(\d+)\s*\)\s*&\s*(0x[0-9A-Fa-f]+|\d+)", body):
        x, shift_str, mask_str = m.group(1), m.group(2), m.group(3)
        shift = int(shift_str)
        mask = int(mask_str, 16 if mask_str.startswith("0x") else 10)
        shifted_mask = mask << shift
        mask_fmt = f"0x{shifted_mask:X}" if mask_str.startswith("0x") else str(shifted_mask)

        alts = [
            f"({x} & {mask_fmt}) >> {shift}",
            f"(u32)({x} >> {shift}) & {mask_str}",
        ]
        for alt in alts:
            new_body = body[:m.start()] + alt + body[m.end():]
            if new_body != body and new_body not in seen:
                seen.add(new_body)
                variants.append((f"bitfield-{len(variants) + 1}", new_body))

    # Pattern 2: (x & M) >> N
    for m in re.finditer(r"\(\s*([A-Za-z0-9_]+)\s*&\s*(0x[0-9A-Fa-f]+|\d+)\s*\)\s*>>\s*(\d+)", body):
        x, mask_str, shift_str = m.group(1), m.group(2), m.group(3)
        shift = int(shift_str)
        mask = int(mask_str, 16 if mask_str.startswith("0x") else 10)
        if mask % (1 << shift) == 0:
            unmasked = mask >> shift
            mask_fmt = f"0x{unmasked:X}" if mask_str.startswith("0x") else str(unmasked)
            alt = f"({x} >> {shift}) & {mask_fmt}"
            new_body = body[:m.start()] + alt + body[m.end():]
            if new_body != body and new_body not in seen:
                seen.add(new_body)
                variants.append((f"bitfield-{len(variants) + 1}", new_body))

    # Pattern 3: (x >> N) & 1 <-> (x & (1 << N))
    for m in re.finditer(r"\(\s*([A-Za-z0-9_]+)\s*>>\s*(\d+)\s*\)\s*&\s*1\b", body):
        x, shift_str = m.group(1), m.group(2)
        shift = int(shift_str)
        mask_val = 1 << shift
        alt = f"{x} & 0x{mask_val:X}"
        new_body = body[:m.start()] + alt + body[m.end():]
        if new_body != body and new_body not in seen:
            seen.add(new_body)
            variants.append((f"bitfield-{len(variants) + 1}", new_body))

    return variants


# -----------------------------------------------------------------------------
# Main Driver
# -----------------------------------------------------------------------------
TRANSFORMERS = {
    "bool": transform_bool,
    "compare": transform_compare,
    "return": transform_return,
    "decl-order": transform_decl_order,
    "local-form": transform_local_form,
    "reassociate": transform_reassociate,
    "param-inversion": transform_param_inversion,
    "bitfield": transform_bitfield,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate candidate respellings of a C++ function body.",
        add_help=True,
    )
    parser.add_argument("source", nargs="?", help="source file path (e.g. src/GC2D/GCConsole2.cpp)")
    parser.add_argument("signature", nargs="?", help="function definition signature")
    parser.add_argument(
        "--families",
        default=",".join(DEFAULT_FAMILIES),
        help=f"comma-separated transform families (default: {','.join(DEFAULT_FAMILIES)})",
    )
    parser.add_argument("--out", help="output file path (default: stdout)")
    parser.add_argument("--list", action="store_true", help="print all families with descriptions and exit")

    args = parser.parse_args(argv)

    if args.list:
        print_family_list()
        return 0

    if not args.source or not args.signature:
        parser.error("both source and signature are required unless --list is specified")

    source_path = Path(args.source)
    if not source_path.is_file():
        sys.exit(f"source file not found: {source_path}")

    selected_families = [f.strip() for f in args.families.split(",") if f.strip()]
    for f in selected_families:
        if f not in TRANSFORMERS:
            sys.exit(f"unknown family: {f!r}. Available families: {', '.join(ALL_FAMILIES)}")

    source_text = io.open(source_path, encoding="utf-8", newline="").read()
    lo, hi = find_function(source_text, args.signature)
    baseline_body = extract_body(source_text, lo, hi)

    variants = []
    for fam_name in selected_families:
        fn = TRANSFORMERS[fam_name]
        fam_variants = fn(baseline_body, args.signature, source_text)
        variants.extend(fam_variants)

    out_lines = ["=== baseline", baseline_body]
    for name, vbody in variants:
        out_lines.append(f"\n=== {name}")
        out_lines.append(vbody)
    output_content = "\n".join(out_lines) + "\n"

    if args.out:
        out_file = Path(args.out)
        io.open(out_file, "w", encoding="utf-8", newline="").write(output_content)
    else:
        sys.stdout.write(output_content)

    return 0


if __name__ == "__main__":
    sys.exit(main())
