# decomp-triage

Triage tools for matching decompilation with Metrowerks CodeWarrior, plus a
machine-readable catalogue of compiler behaviours.

They answer one question in nine ways: **this function doesn't match — why, and is it
worth my time?** Only `gen-variants.py` writes source, and only candidates for
`try-variants.py` to measure and throw away. The rest sort, measure, and point; the fix
is yours.

Built against [doldecomp/sms](https://github.com/doldecomp/sms) (Super Mario Sunshine,
GMSJ01, MWCC `GC/1.2.5`). Nothing is Sunshine-specific — any dtk-based project with an
`objdiff.json` can run them. Everything is stdlib-only; drop the scripts in your
project's `tools/` and go.

---

## The tools

| | question it answers |
| --- | --- |
| `stack-frame-diff.py` | which bucket is each failure in, and which are worth working |
| `scaffold-scan.py` | is anything left once the frame delta stops hiding it |
| `structural-diff.py` | *what kind* of wrong is this function |
| `data-pool-diff.py` | is the problem actually in the data, not the code |
| `try-variants.py` | which of these candidate rewrites is closest |
| `gen-variants.py` | *generates* those candidates — safe respelling families |
| `review-lint.py` | will a maintainer send this back |
| `unit-deps.py` | which unit should I work on next, and what gates what |
| `levers.py` | has anyone solved this symptom before |

### `stack-frame-diff.py` — the sort

```bash
python tools/stack-frame-diff.py            # summary + frame-only list
python tools/stack-frame-diff.py --by-idiom # group by method name across classes
```

On MWCC **GC/1.0 – GC/1.2.5** the compiler never reclaims unused stack. Anything that
fails to win a register still costs a slot, even when every instruction touching it was
optimised away. That makes `stwu r1,-N(r1)` a fingerprint of the original source's shape.

Across Sunshine's tree — 10,115 functions:

| bucket | count | meaning |
| --- | ---: | --- |
| byte-identical | 8,072 | matching |
| **frame-only** | **~600** | every instruction agrees; only the frame is wrong |
| frame + code | ~1,150 | both differ |
| regperm | 20 | identical but for register choice |
| structural | ~350 | frame agrees, code differs |

**Four in five remaining failures involve a wrong frame.** objdiff can't isolate them: a
frame shift renders as a wall of unrelated-looking offset diffs through the whole function.

**Frame-only functions are the *hardest*, not the cheapest.** objdiff is already saturated
— every instruction matches, so there's no percentage creeping up as you get warmer.
`decomp-permuter` has nothing to optimise toward. You need an edit that adds intermediate
values while generating identical code, and most edits break the match you have. Half the
population differs by the arithmetic minimum. If you want matches rather than archaeology,
work the **structural** bucket.

`--by-idiom` is usually more useful than the per-file view: in a rushed, copy-pasted
codebase the same virtual exists dozens of times, so one reconstruction can be worth a
dozen matches.

### `scaffold-scan.py` — look underneath the frame

```bash
python tools/scaffold-scan.py src/GC2D/Foo.cpp mario/GC2D/Foo
python tools/scaffold-scan.py src/GC2D/Foo.cpp mario/GC2D/Foo --only barFn bazFn
```

When our frame is the wrong size, every stack displacement in the function shifts, so the
diff renders as a wall of unrelated-looking offset changes and the real code mismatch
underneath is invisible. Sizing a throwaway local to the delta cancels the shift:

```cpp
void TFoo::bar()
{
    char trash[0x40];   // TEMPORARY: cancels the frame delta so the diff is readable
    ...
}
```

This script does that for every failing function in a unit, rebuilds the one object,
counts what is left, and restores the file — on success, on failure, and on Ctrl-C. The
output splits the unit in two: functions that drop to nearly nothing are **one fix away**,
functions that stay at forty have **real structural work**. That ranking is the point; it
tells you where the next hour goes.

It also classifies the leftover diff lines into three kinds, which is the difference
between "there is work here" and "there is not":

| kind | meaning |
| --- | --- |
| **real** | different opcode, different immediate, or an instruction on one side only |
| **regperm** | same instructions, the allocator picked different registers |
| **slot** | same instructions, a stack slot moved |

A function whose leftovers are entirely `regperm` and `slot` has no code work left — only
frame size and register colouring, which is the hardest and least tractable bucket. Do not
spend a day discovering that by hand.

**The scaffold is a measuring device, not source.** Shipping one is fabricated padding.
`review-lint.py` carries a `diagnostic-scaffold-left-in` rule for exactly this; keep it at
zero.

Set `DECOMP_VERSION` if your build directory is not `build/GMSJ01`.

### `structural-diff.py` — what kind of wrong

"Structural" is not a diagnosis. This reads the failures instruction by instruction and
names the category, because they have very different fixes and costs: wrong constants,
wrong struct offsets, wrong data ordering, wrong integer width, differing length,
register noise.

Two rules keep it honest, and the first one is the whole ballgame:

- **A displacement is only a struct field if its base register is an object pointer.**
  MWCC addresses statics and literals as `lis rX, sym@ha` + `addi rD, rX, sym@l`, which
  puts a *data* offset in exactly the field a struct displacement uses. Provenance is the
  only thing separating them. Before this was added, 23 functions across 9 units were
  reported as a shared base-class bug when every one was data ordering.
- `addi rD,rA,n` with a real `rA` is a member address, so its immediate is an offset, not
  a literal. Without that split, a params constructor reports 66 wrong constants instead
  of one wrong pool.

`unclassified` is a real category and is named as a gap, not a verdict. Folding it into
"register noise" would tell you to skip functions the tool merely failed to read, which
is the most expensive mistake a triage tool can make.

### `data-pool-diff.py` — the invisible failure

Compares the *data* each file lays out, which no code diff can see. If your data sits at
different offsets than retail's, every instruction reaching it looks wrong while the code
is perfect.

The case that motivated it: four functions looked like a wrong SDK struct. The struct was
fine. Retail's `.data` simply began with 40 bytes of constants the compiler generated and
then optimised the uses of — the footprint of a **precompiled header** the original build
used and we don't. Supplying equivalents made the data byte-identical.

Three traps it has to handle, all of which produce confidently wrong answers:

- Compiler-generated `@NNNN` names are a **per-compilation counter**, not an identity. The
  same literal is `@1490` in one build and `@597` in another. Compare by content.
- `.bss` symbols have no content at all. Compare those by size.
- **"We emit a symbol the target does not" is only half a sentence.** The extracted objects
  you diff against are what the *original linker kept*. Anything that build dead-stripped
  is missing from them while still having been real, and the link map still lists it, with
  a `........` address and an `UNUSED` marker. Read from the objects alone, a dead-stripped
  symbol is indistinguishable from one you invented — and the two want opposite fixes.

That third one is not a rare edge case, and it is the reason this tool now reads the link
map. On the project it was built against, **204 symbols across 185 sections were on the
wrong side of that line — 5.1% of every "the target does not have this" it used to print.**
Two header-level symbols accounted for most of it: one appears in 103 units and never in a
single extracted object, another in 69. Acting on the old output cost that project **4,416
bytes of matched data** in one commit, which had to be caught in review and reverted.

Output now separates them:

```
we emit 1 symbol(s) retail does not: dummyMactorStringValue1
1 symbol(s) look extra but the link map lists them for this file: SMS_NO_MEMORY_MESSAGE
    -> the original build HAD these and dead-stripped them, so they are absent
       from the extracted object only. Keep them
```

The first is genuinely yours and is a candidate for deletion. The second is evidence your
source is **right**. Deleting it removes correct source, and when it came from a widely
included header, it does so across every unit that shares it.

Set `DECOMP_MAP` if your link map is not at `orig/$DECOMP_VERSION/files/mario.MAP`. Without
a map the tool falls back to its previous behaviour and simply does not draw the
distinction.

### `try-variants.py` — measure instead of guessing

```bash
python tools/try-variants.py src/Foo.cpp 'int TFoo::bar()' variants.txt
```

Compiles several candidate bodies for one function and reports frame size against target,
byte-identity, and length. Rebuilds a single object, so a sweep is seconds. Restores your
source on success, failure, and Ctrl-C.

A variant that reaches the right frame but the wrong length tells you the shape is
plausible and the body isn't — which is more than a percentage ever tells you.

### `gen-variants.py` — the candidates

```bash
python tools/gen-variants.py src/Foo.cpp 'int TFoo::bar()' > variants.txt
python tools/try-variants.py src/Foo.cpp 'int TFoo::bar()' variants.txt
```

The only tool here that writes source, and only throwaway candidates for `try-variants.py`
to measure. MWCC emits different code for semantically identical spellings, so when a
function is a few instructions away the fix is usually a *respelling*, not new logic.

Eight families, safest first: `bool`, `compare`, `return`, `decl-order`, `local-form`,
`reassociate` (integers only — reassociating floats changes results), `param-inversion`,
`bitfield`.

`decl-order` is the one to reach for on a frame or register-colouring miss; it is the only
family that moves layout rather than arithmetic. Measured on one already-matching function:
writing a condition as `x != 0` instead of `x` moved the frame by +8 while emitting the same
number of code bytes, so the `bool` family is a genuine frame lever too.

What it will **not** generate, by design: `volatile` locals, empty switch cases to reshape a
jump table, and `#pragma` peephole/scheduling/fp_contract toggles. Those close byte gaps by
producing source nobody would write. `cast` and `width` are also excluded — they change
semantics and must be checked by hand rather than swept.

### `review-lint.py` — spend the maintainer's attention on real problems

Encodes review comments a maintainer already made, so nobody has to make them twice.
Diff-scoped by default, so switching it on mid-project doesn't bury you.

Rules live in `tools/review-rules.jsonl` because they're per-project by nature; the
shipped set is an example from one GameCube project.

Some checks are built in rather than configurable, because they aren't conventions — each
is a signature of source invented to reach a match, and **a wrong function can match
100%**, so no build check will ever catch them:

- **write-only-array** — a local array read only at `[0]`, usually there to move the frame.
- **raw-offset-cast** — reaching a field via `*(T*)((u8*)p + 0xNN)`. It encodes a layout
  guess nothing verifies. Measured across 579 files on the source project: 10 hits, so it
  is high-signal rather than noise.
- **invented-helper-dead / -single** — a file-local `static` helper that is never
  referenced, or has exactly one call site. Headers are exempt: an inline in a class
  header is that class's API, not a guess about a translation unit.

Two calibrations in `invented-helper` are worth knowing before you tune it. It counts
*every* mention of the name, not `name(`, because a callback handed to the engine is
referenced by address and never called — counting call syntax alone reported 153 false
positives instead of 16. And an uncalled `static` named `dummy` is skipped, since that is
a common idiom for forcing section order. **`dead` does not mean delete**: check the
linker map first, because a symbol present there is real code that was dead-stripped.

### `unit-deps.py` — what to work on next

```bash
NM=path/to/nm python tools/unit-deps.py --all      # leaf units: the cheapest work
NM=... python tools/unit-deps.py --chain           # units that unblock the most others
NM=... python tools/unit-deps.py --deps src/Foo.cpp
```

The only tool here that works *between* units rather than inside one function. Builds the
symbol-level dependency graph from the object files and answers which units nothing else is
waiting on (start here), which ones unblock the most others (do these for leverage), and why
a given unit is blocked.

Ported from the dependency graph in [doldecomp/melee](https://github.com/doldecomp/melee).

### `levers.py` + `levers.jsonl` — write the folklore down

A catalogue of compiler behaviours that have turned a non-matching function into a
matching one, as `symptom` → `lever` → `confidence` → `source` records.

```bash
python tools/levers.py --match "stack"      # filter by symptom
python tools/levers.py --format prompt      # the block to hand a model
```

Most of what makes a function match is folklore about one specific compiler, spread across
commit messages, Discord, and people's heads. Machine-readable means a person can grep it
by symptom and a model can be handed exactly the subset that applies. `confidence` is
`proven` only when a byte-match was actually reproduced.

The schema matches [tangosdev/sm64ds-decomp](https://github.com/tangosdev/sm64ds-decomp)'s
`notes/levers.jsonl`, which covers **mwccarm** on the DS, so one reader loads either:

```bash
python tools/levers.py --repo ../sm64ds-decomp --catalogue notes/levers.jsonl --arch arm
```

**A lever proven on one compiler is only a hypothesis on the other.** Their
`volatile-local-stack-slot` is the sibling of this catalogue's
`dead-stack-slots-never-reclaimed`, but the mechanism is opposite — mwccarm reclaims dead
slots and needs `volatile` to pin one; GC/1.x never reclaims at all. Their lever that
class-typed by-value parameters home to the stack was tested here against every frame-only
function and does not hold for mwcceppc.

---

## Credits

- **[encounter/decomp-toolkit](https://github.com/encounter/decomp-toolkit)** and
  **[objdiff](https://github.com/encounter/objdiff)** — the foundation all of this sits on.
- **[cadmic/mwcc-debugger](https://github.com/cadmic/mwcc-debugger)** — dumps MWCC's
  internal state pass by pass; the public reference for the stack-allocation behaviour and
  register allocator priorities this work rests on.
- **[tangosdev/sm64ds-decomp](https://github.com/tangosdev/sm64ds-decomp)** — the
  `levers.jsonl` format is theirs and this reuses the schema. Cited for the file format,
  not as a model for running a decompilation project.
- **[doldecomp/sms](https://github.com/doldecomp/sms)** — where every measurement comes from.

## License

CC0-1.0, matching doldecomp/sms, so any of this can be contributed upstream without a
licence clash.

No game data, ROM images, linker maps, or compiler binaries are included or required.
