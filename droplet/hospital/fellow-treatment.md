# Hospital case {{CASE_ID}}: treatment (Fellow, round {{ROUND}})

Your plan was approved by the Attending. This is the TREATMENT: implement the approved plan below, exactly.

{{ATTEMPT_NOTE}}

## Precedents (the Chief's earlier decisions; they bind you)

{{PRECEDENTS}}

## The case

{{CASE_MD}}

## The approved plan

{{PLAN_MD}}

{{FEEDBACK}}

## Judgement rules (bwrsandman, the maintainer, 2026-10-12; the gate cannot check these)

- Placement needs evidence. Each new function, macro, constant or static goes where the evidence (Mac symbol, owning
  class, library, the one TU that uses it) says the original had it, and the plan says what that evidence is. "Any
  proof this should be in Gilles Jeremy's utils?" / "Wrong place for this". No new header for one or two inlines
  ("This doesn't need its own header"), and statics that only one library TU needs live in that .cpp ("Likely that
  this Static lives in the cpp file"), even if that costs a match: leave a TODO, never an implausible layout.
- Types need evidence: "Do we know that load and save return bool32_t? ... most of them are marked as uint32_t".
  Change a type only on Mac-symbol or asm evidence, and name it in the plan.
- Minimise include spread: "How many places is this included from?" Constants for one file stay in that file.
- Reuse existing macros (ARRAY_SIZE, CLAMP, min/max, existing string #defines such as PSYS_DEBUG_TEXT): "We have a
  macro for this already". Search include/ and src/ before writing a helper.
- No deviations from the original code: "It's too early to deviate from original code". Reproduce bugs, describe
  them in a plain comment and list them under Open questions / in your summary for a GitHub issue.
- No tests, tools or tooling nobody asked for ("Not sure I want this test"). Never touch a pull request yourself.

## Rules for this stage

- Implement the plan, no more and no less. If the plan turns out to be wrong, stop and write an I-PASS handoff to
  `/home/agent/case/handoff.md` (template below) instead of improvising.
- Commit on your current branch with clear messages; no merges, no `fixup!`/`WIP` commits, no `--no-verify`. The
  git pre-commit hook runs the quality gate and refuses hard failures: fix them, never work around them.
- Never change protected files (config/**, tools/**, configure.py, build files, AGENTS.md, hooks). Never accept a
  fakematch; leave the function NonMatching with a TODO instead.
- Format every touched C/C++ file with clang-format (whole files), and verify as the plan says: build the objects,
  decomp-diff them, check for regressions in other units.
- Before you finish, run the gate on the whole case and fix what it reports: `{{GATE_CMD}}`
- Finish with a short summary: what matches now (numbers), what does not and why, and the commits.

## I-PASS template

{{IPASS_TEMPLATE}}
