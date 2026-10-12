# Plan: {{CASE_ID}}

<!-- The Fellow fills in every section; "None" is an answer, an empty or missing section is not. The Discharge
     Nurse checks that every heading below is present and filled. -->

## Diagnosis

One entry per unit or function in the case: its current state (match %, what differs in the diff, which review
comment or gate finding applies), and the cause, with the evidence (Mac symbol, asm, Ghidra, similar matched code).

## Files

Every file you will change, and what changes in each. Protected files (config/**, tools/**, configure.py, build
files, AGENTS.md, hooks) are never on this list: if the case needs one, it is an open question for the Chief.

## Approach

The steps, in order, and for each the source change and why it is the original's shape (not just a shape that
matches). Name the helpers, macros and headers you will use.

## Risks

What could regress (other units including the same headers, other game versions, matched functions nearby), and how
you will notice.

## Verification

The exact commands: the objects to build, decomp-diff calls, the full check (tools/decomp-verify.py or ninja
baseline / decomp-regress.py --refresh --fail-on-regression), the quality gate, clang-format.

## Open questions

Decisions the Attending or the Chief must make (fakematches, protected-file changes, naming disputes), or "None".

## Size estimate

Files and lines you expect to change. A case delta over 1000 lines must be split into several cases.
