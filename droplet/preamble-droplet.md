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
    the header that owns them. Never put statics in a header under a library's module guard (`#ifdef _LH_3D_LIB_`):
    "This pattern is Claude getting confused and trying to get a match even if the code is implausible. Likely that
    this Static lives in the cpp file" (#431). Define such a static in the .cpp that owns it; if that loses a match,
    accept the loss with a TODO rather than an implausible layout. (This replaces the 2026-10-12 module-guard
    precedent for statics.)

## Maintainer review rules (bwrsandman, 2026-10-12: PR #431 and his comments on #411, #412, #451, #452)

14. Use the macros that exist; never re-implement one. `ARRAY_SIZE(a)` from `re_common.h`, not
    `sizeof(a) / sizeof(a[0])` ("We have an ARRAY_SIZE macro to do this"); `CLAMP`, `min`, `max` instead of a new
    `boundf`/`clamp` helper ("We have a macro for this already"). Search `include/` and `src/` for an existing macro
    (`git grep -n "#define <NAME>"`) before writing a helper.
15. Don't create a header for one or two inline functions ("This doesn't need its own header"): an inline member
    goes in its class's header.
16. Placement needs evidence. Put each function, macro, constant and static where the evidence says the original
    had it: the owning class (Mac symbol), the library, the one TU that uses it. "Any proof this should be in Gilles
    Jeremy's utils?", "Wrong place for this". If there is no evidence, say so in a comment (`// TODO: placement
    unproven`) and choose the most local place, never a shared utility header by convenience.
17. Keep include spread small: "How many places is this included from?" Constants used by one file are `#define`s
    at the top of that file; a new header must be justified by several consumers.
18. A string literal used in many places is a `#define` ("This is a very commonly used string can you put it in a
    define?"); if one exists (e.g. `PSYS_DEBUG_TEXT`), use it.
19. Types need evidence. Don't guess `bool32_t` from a function returning 0/1: "Do we know that load and save
    return bool32_t? ... most of them are marked as uint32_t". Follow the Mac symbol, the asm and the neighbouring
    declarations, and leave the existing type (with a TODO) when the evidence is missing.
20. No deviations from the original code: no BUGFIX blocks, no fixes of original bugs ("It's too early to deviate
    from original code. If we do this in the future it will be on a different branch"). Reproduce the bug, describe
    it in a plain comment, and list it in your summary so a human can open an issue ("make two issues for the bugs
    you find").
21. Don't add tests, tools or other tooling the maintainer didn't ask for ("Not sure I want this test"); a code case
    changes `src/` and `include/` only.
22. Pull requests are the human's and the publisher's business, never a worker's: workers never comment on, ping or
    ask for re-review of a PR ("Why are you spamming already merged PR's?"), and a PR is rebased on main before
    anyone asks for review ("Needs rebase").

## Forbidden constructs and protected files

The quality gate (`~/.claude/hooks/quality_gate.py`) runs as your git pre-commit hook (hard failures block the
commit), when you stop, and again on the host before anything is published. Hard failures:

- `extern` anywhere; anonymous enums of constants; header `#ifdef` toggles on new macros; `field_0x..` uses in .cpp
  code; non-symbol helper functions in a .cpp; hand-written two-sided clamps, ternary min/max, if/else and if-return
  selects; includes, globals or declarations after the first function; `sizeof(a) / sizeof(a[0])` (ARRAY_SIZE);
  new helpers or macros that re-implement CLAMP/min/max/ARRAY_SIZE; statics in a header under a module guard;
  a new header holding only inline members of a class declared elsewhere; a literal that already has a `#define`;
  BUGFIX / FIX_BUGS / "fixes an original bug" markers.
- Anti-cheat: inline assembly (`__asm`, `_asm`, `asm(`) in any file that had none upstream, `__declspec(naked)`,
  `_emit`, `#pragma code_seg/data_seg/const_seg/bss_seg/section/alloc_text`, `#pragma comment(linker, ...)`, and
  bytes or strings cast to function pointers. A match must come from C++ that the original compiler turns into the
  original bytes. (Files that already contain inline asm upstream, such as CPUCheck.cpp, may keep using it.)
- Protected files, which you never change: objdiff.json, build.ninja, configure.py, config/**, tools/** (all of it,
  tools/matching_ledger.jsonl included), build/**, object files, AGENTS.md, CLAUDE.md, .claude/**, .github/**,
  .clang-format. Changing one escalates to the human, who alone decides. If a task truly needs a symbols.txt or
  splits.txt change, say so in your summary (or in `~/case/handoff.md`) instead of making it.
- Warnings (fix them where you can): a one-sided `if (x < lo) x = lo;`, new `#pragma optimize/inline_depth/
  auto_inline`, magic float literals, element-by-element matrix maths, a literal repeated in 3+ places, a new
  constants header included from one file, any other new module-guard block in a header.

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

