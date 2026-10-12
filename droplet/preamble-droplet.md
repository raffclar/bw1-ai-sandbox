## Maintainer review rules (bwrsandman, PR #431)

The maintainer reviewed an earlier worker PR (openblack/bw1-decomp#431) and asked for these. Every change follows
them; the quality gate (below) checks most of them automatically, and a reviewer checks the rest.

1. Clamps: use `CLAMP` from `re_common.h`, or windef.h's `min`/`max`, instead of a hand-written clamp or select.
   `tools/minmax-scan.py --verify --apply <file>` keeps only the byte-identical rewrites.
2. Nothing halfway through a file: includes, globals, statics and declarations go at the top, before the first
   function. A block that looks like another file's code probably belongs to another translation unit.
3. A utility inline that is not a real symbol lives in a header everyone can use (the owning class's, or a shared
   one), never in a .cpp. A .cpp defines only real symbols, with their address comments.
4. Before writing matrix or vector maths element by element, look for the inline helper the original used
   (LHMatrix.h, LHPoint.h, LH3DMath.h: SetRotationY, RotateY, SetTranslateOnly, GetNorme, ...).
5. If you use a field, name it: give the member a readable PascalCase name in the header. No `field_0x..` in .cpp code.
6. Magic values: declare them at the top of the file with `#define`, not as bare literals deep in a function.
7. Finish the control flow: write the natural form the original had (inverting forks, e.g.
   `life = MAX(0.0f, currentLife)`), not the first shape that matches.
8. A list of constants is a list of `#define`s, not an anonymous `enum` (CitadelHeart.h).
9. The same for flags and counts in other headers (Flock.h: "use a define instead").
10. The same for constants in library headers (InfluenceCircle.h: "use a define").
11. The same in LH3DIsland.h (`#define`).
12. When the original evidently had a dead variable or other oddity, reproduce it honestly with a comment; never hide
    it behind an unrelated trick. If only a fakematch matches, leave it NonMatching with a TODO and escalate.
13. No header `#ifdef` toggles for one translation unit ("an LLM hacky solution"): declare things unconditionally in
    the header that owns them.

## Forbidden constructs and protected files

The quality gate (`~/.claude/hooks/quality_gate.py`) runs as your git pre-commit hook (hard failures block the
commit), when you stop, and again on the host before anything is published. Hard failures:

- `extern` anywhere; anonymous enums of constants; header `#ifdef` toggles on new macros; `field_0x..` uses in .cpp
  code; non-symbol helper functions in a .cpp; hand-written two-sided clamps, ternary min/max, if/else and if-return
  selects; includes, globals or declarations after the first function.
- Anti-cheat: inline assembly (`__asm`, `_asm`, `asm(`), `__declspec(naked)`, `_emit`, `#pragma
  code_seg/data_seg/const_seg/bss_seg/section/alloc_text`, `#pragma comment(linker, ...)`, and bytes or strings cast
  to function pointers. A match must come from C++ that the original compiler turns into the original bytes.
- Protected files, which you never change: objdiff.json, build.ninja, configure.py, config/**, tools/** (except
  tools/matching_ledger.jsonl), build/**, object files, AGENTS.md, CLAUDE.md, .claude/**, .github/**,
  .clang-format. Changing one escalates to the human, who alone decides. If a task truly needs a symbols.txt or
  splits.txt change, say so in your summary (or in `~/case/handoff.md`) instead of making it.
- Warnings (fix them where you can): a one-sided `if (x < lo) x = lo;`, new `#pragma optimize/inline_depth/
  auto_inline`, magic float literals, element-by-element matrix maths.

Never bypass the gate (`--no-verify`, editing the hooks, moving code to dodge a rule). Before you finish, run it on
your branch and fix what it reports:

    python3 ~/.claude/hooks/quality_gate.py --base "$(cat ~/case/gate-base 2>/dev/null || git merge-base origin/main HEAD)"

## Hospital rules (when your task is a hospital case)

Some tasks come as a case of the "teaching hospital": you are the Fellow, an Attending (a separate reviewer) reviews
your plan and your code, and a deterministic Discharge Nurse verifies the result before anything is published.

- No code before a plan, and no plan without review. In the workup you write `~/case/plan.md` from the template and
  no code. You start the treatment only when the prompt says the plan was approved.
- Don't improvise. Implement the approved plan. If it turns out to be wrong, stop and say so (handoff below) rather
  than doing something else.
- No weakening of checks and no fakematches: never edit tools, config, build files, hooks, tests or AGENTS.md to make
  something pass, never add a variable, cast or pragma only to steer the compiler without evidence, and never accept
  a fakematch. Leave the function NonMatching with a TODO instead.
- When you are stuck, write an I-PASS handoff in `~/case/handoff.md` (Illness severity, Patient summary, Action list,
  Situation awareness and contingencies, Synthesis by receiver: what the next person must check first) and stop.
- Never read another worker's home folder (`/peers/<name>/home`): its transcript and notes are not yours. Use only
  `/peers/<name>/repo` through `git fetch`, and only when your prompt tells you to.
- Run the quality gate before finishing (above).

