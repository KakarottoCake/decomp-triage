# decomp-triage

Triage tools for matching decompilation with Metrowerks CodeWarrior, plus a
machine-readable catalogue of compiler behaviours.

They answer one question in six ways: **this function doesn't match — why, and is it
worth my time?** None of them write source. They sort, measure, and point; the fix is
yours.

Built against [doldecomp/sms](https://github.com/doldecomp/sms) (Super Mario Sunshine,
GMSJ01, MWCC `GC/1.2.5`). Nothing is Sunshine-specific — any dtk-based project with an
`objdiff.json` can run them. Everything is stdlib-only; drop the scripts in your
project's `tools/` and go.

---

## The tools

| | question it answers |
| --- | --- |
| `stack-frame-diff.py` | which bucket is each failure in, and which are worth working |
| `structural-diff.py` | *what kind* of wrong is this function |
| `data-pool-diff.py` | is the problem actually in the data, not the code |
| `try-variants.py` | which of these candidate rewrites is closest |
| `review-lint.py` | will a maintainer send this back |
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

Two traps it has to handle, both of which produce confidently wrong answers:

- Compiler-generated `@NNNN` names are a **per-compilation counter**, not an identity. The
  same literal is `@1490` in one build and `@597` in another. Compare by content.
- `.bss` symbols have no content at all. Compare those by size.

### `try-variants.py` — measure instead of guessing

```bash
python tools/try-variants.py src/Foo.cpp 'int TFoo::bar()' variants.txt
```

Compiles several candidate bodies for one function and reports frame size against target,
byte-identity, and length. Rebuilds a single object, so a sweep is seconds. Restores your
source on success, failure, and Ctrl-C.

A variant that reaches the right frame but the wrong length tells you the shape is
plausible and the body isn't — which is more than a percentage ever tells you.

### `review-lint.py` — spend the maintainer's attention on real problems

Encodes review comments a maintainer already made, so nobody has to make them twice.
Diff-scoped by default, so switching it on mid-project doesn't bury you.

Rules live in `tools/review-rules.jsonl` because they're per-project by nature; the
shipped set is an example from one GameCube project. One rule is built in rather than
configurable: a local array read only at `[0]`, the signature of a match bought with
source nobody would write.

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
- **mwcc-izer** — a private tool used for some per-function frame and inlining analysis
  cited in the catalogue. Not publicly available.
- **[doldecomp/sms](https://github.com/doldecomp/sms)** — where every measurement comes from.

## License

CC0-1.0, matching doldecomp/sms, so any of this can be contributed upstream without a
licence clash.

No game data, ROM images, linker maps, or compiler binaries are included or required.
