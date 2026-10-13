# Hospital case {{CASE_ID}}: workup (Fellow, round {{ROUND}})

You are the Fellow on this case. This is the WORKUP: you diagnose and plan; you write no code and make no commits.
An Attending (a separate reviewer who does not see this conversation) reviews your plan before any treatment.

{{ATTEMPT_NOTE}}

## Precedents (the Chief's earlier decisions; they bind you)

{{PRECEDENTS}}

## The case

{{CASE_MD}}

## Your branch

- Base (upstream merge-base, what the case is judged against): `{{BASE}}`
- Case start (the commit the case begins from): `{{START}}`
- Work on your current branch. Do not rebase, reset or merge.

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

## What to do

1. Reproduce the current state: build the case's objects, run `python3 tools/decomp-diff.py` on them, and run the
   quality gate: `{{GATE_CMD}}`.
2. Diagnose every unit, function and review comment in the case, with evidence.
3. Write `/home/agent/case/plan.md` from the template below. Every section must be filled ("None" is an answer).
4. Stop. Do not edit or commit source files in this stage.

{{FEEDBACK}}

If you cannot make a sound plan (missing evidence, a decision only the Chief can make, a protected file that would
have to change), write an I-PASS handoff to `/home/agent/case/handoff.md` instead, from the template at the end, and
stop.

## Plan template

{{PLAN_TEMPLATE}}

## I-PASS template

{{IPASS_TEMPLATE}}
