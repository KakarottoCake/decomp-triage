# frame-signal

Triage tools for GameCube / Wii matching decompilation with Metrowerks CodeWarrior,
plus a machine-readable catalogue of `mwcceppc` compiler behaviours.

Built against [doldecomp/sms](https://github.com/doldecomp/sms) (Super Mario Sunshine,
GMSJ01, MWCC `GC/1.2.5`), but nothing here is Sunshine-specific — any dtk-based project
with an `objdiff.json` can run it.

---

## The finding

On MWCC **GC/1.0 – GC/1.2.5** the compiler does **not** reclaim unused stack space.
Anything that never wins a register still gets a slot, even when every instruction that
touched it was optimised away, and the frame also covers the outgoing-argument area of
the widest call the function makes. Later CodeWarrior versions tidy this up. Ours does not.

That makes `stwu r1,-N(r1)` a quantitative fingerprint of the shape of the original
source, readable even when the generated code is otherwise byte-for-byte correct.

Measured across Sunshine's whole tree — 10,091 functions compared against retail:

| bucket | count | meaning |
| --- | ---: | --- |
| byte-identical | 7,901 | matching |
| **frame-only** | **605** | every instruction agrees; only the frame size is wrong |
| frame + code | 1,160 | frame differs and so does the code |
| regperm | 20 | identical but for register choice |
| structural | 405 | frame agrees, code differs |

**Four out of five remaining failures involve a wrong stack frame.** objdiff cannot single
these out, because a frame shift renders there as a wall of unrelated-looking offset diffs
spread through the whole function.

### These are the *hardest* functions, not the cheapest

This is worth stating plainly, because the tool's early output said the opposite and it was
wrong. Frame-only functions are cheap to **diagnose** and brutal to **fix**:

- objdiff is already saturated — every instruction matches, so there is no percentage that
  creeps upward as you get warmer. One number, right or wrong.
- `decomp-permuter` has nothing to optimise toward; it searches for source producing the
  target *instructions*, and those are already correct.
- You need an edit that adds intermediate values while generating identical code. Most edits
  break the match you already have.
- Half the population is the worst case: 309 of the 605 differ by the minimum ±8.

Closing one means reconstructing how the original author wrote an expression. That is source
archaeology, not iteration. If you want matches rather than archaeology, work the
**structural** bucket instead.

### What is actually in these frames

Mostly **compiler temporaries** — one slot per intermediate value, created per source line,
largely by inlined calls. A delta is rarely "a missing variable"; it is a missing chain of
intermediate values, which almost always means the original inlined something you do not.

Frames also carry alignment padding, so a single added 4-byte temporary is absorbed and does
not move the frame at all. **A `+8` delta means two more intermediate values, not one.**

---

## Tools

Both are stdlib-only. `stack-frame-diff.py` carries its own big-endian ELF reader, because
a 32-bit big-endian ELF is all a GameCube object ever is and needing a venv to run a triage
script is a bad trade.

Copy them into your project's `tools/` directory — they locate `objdiff.json` one level up.

### `tools/stack-frame-diff.py`

```
python tools/stack-frame-diff.py                  # summary + frame-only list
python tools/stack-frame-diff.py Enemy/           # only units matching a substring
python tools/stack-frame-diff.py --by-idiom       # group by method name, not by file
python tools/stack-frame-diff.py --json out.json  # machine-readable worklist
```

It reads each unit's extracted target object and your built object, compares every function
present in both, and sorts the failures into the four buckets above. A function is reported
`frame-only` only after checking that *every* instruction difference is exactly the uniform
displacement shift the frame change implies — that check cannot produce a false positive.

`regperm` detection decodes PowerPC D-form and X-form instructions and blanks their register
fields. It fails closed on forms it does not decode, so that count is a floor.

**`--by-idiom` is usually the more useful view.** Grouping by unit answers "which file is
closest to done". Grouping by method name across every class implementing it answers "which
idiom am I writing wrong" — and in a rushed, copy-pasted codebase the same virtual exists
dozens of times, so one reconstruction can be worth a dozen matches:

```
  method                    count  classes   most common deltas
  execute                      39       15   +8x17, +16x8, +32x4, -8x3
  perform                      29       25   +8x10, +16x8, +24x7, +48x2
  receiveMessage               16       14   +8x12, ...            <-- 12/16 agree
  makeDrawBuffer                4        1   +32x4                 <-- 4/4 agree
```

### `tools/levers.py` + `tools/levers.jsonl`

A catalogue of compiler behaviours that have been used to turn a non-matching function into a
matching one. Each entry is a `symptom` → `lever` → `confidence` → `source` record.

```
python tools/levers.py                            # human-readable table
python tools/levers.py --match "stack"            # filter by symptom text
python tools/levers.py --id inline-pass-ladder    # one entry, in full
python tools/levers.py --format prompt            # the block to hand a model
```

Most of what makes a function match is folklore about one specific compiler, currently spread
across commit messages, Discord and people's heads. Writing it down in a machine-readable form
means a person can grep it by symptom and a model can be handed exactly the subset that applies.

`confidence` is `proven` only when a byte-match was actually reproduced.

#### Cross-project

The schema is deliberately identical to
[tangosdev/sm64ds-decomp](https://github.com/tangosdev/sm64ds-decomp)'s
`notes/levers.jsonl`, which covers **mwccarm** on the DS. One reader loads either:

```
python tools/levers.py --repo ../sm64ds-decomp --catalogue notes/levers.jsonl --arch arm
```

That project originated the idea and this catalogue is the PowerPC counterpart to it.

**A lever proven on one compiler is only a hypothesis on the other**, which is why entries
carry `compiler_version`. A worked example of why: their `volatile-local-stack-slot` lever is
the sibling of this project's `dead-stack-slots-never-reclaimed`, but the mechanism is the
*opposite* — mwccarm reclaims dead slots and needs `volatile` to pin one, while GC/1.x never
reclaims at all. Their lever that class-typed by-value parameters home to the stack was tested
here against all 605 frame-only functions and **does not hold** for mwcceppc.

---

## Prior art and credit

- **[tangosdev/sm64ds-decomp](https://github.com/tangosdev/sm64ds-decomp)** — originated the
  lever-catalogue format this copies, and its `notes/mwccarm-codegen.md` is the most substantial
  written record of CodeWarrior matching technique anywhere.
- **[cadmic/mwcc-debugger](https://github.com/cadmic/mwcc-debugger)** — dumps MWCC's internal
  state pass by pass. Its README is the public reference for the stack-allocation behaviour and
  the register allocator's priority levels that this work rests on.
- **mwcc-izer** — a private tool used for the per-function frame and inlining analysis cited in
  some lever entries. Not publicly available; `mwcc-debugger` above is the public equivalent for
  the behaviours involved.
- **[encounter/decomp-toolkit](https://github.com/encounter/decomp-toolkit)** and
  **[objdiff](https://github.com/encounter/objdiff)** — the foundation everything here sits on.
- **[doldecomp/sms](https://github.com/doldecomp/sms)** — where all the measurements come from.

## License

CC0-1.0, matching doldecomp/sms, so these can be contributed upstream without a licence clash.

No game data, ROM images, linker maps or compiler binaries are included or required to read
this repository.
