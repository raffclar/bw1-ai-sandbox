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
