# Review: {{CASE_ID}} round {{ROUND}} ({{KIND}})

Verdict: APPROVE | REQUEST_CHANGES | ESCALATE
Reviewed head: <full 40-character sha you reviewed>

<!-- Keep every heading below. Write "None" or "n/a (why)" rather than deleting a section: the hospital treats a
     review with a missing section, or without exactly one verdict from the list above, as not done. -->

## Checks

| Check | Result (pass/fail/n/a) | Evidence (command and what it showed) |
|-------|------------------------|---------------------------------------|
| Plan or code read in full | | |
| Quality gate on the case (hard failures, warnings) | | |
| Builds the case's objects | | |
| Match percentages before and after | | |
| No regression in other units | | |
| clang-format clean | | |

## Blocking findings

Each finding that must be fixed before approval, numbered after the highest number used in earlier reviews of this
case: `[B1] file:line: what is wrong, and what would fix it`. With verdict APPROVE this section is "None".

## Resolution of prior findings

Every [B#] from earlier reviews of this case, one line each: `[B#] resolved: how` or `[B#] unresolved: why`.
"None" when there were none.

## Plan conformance

Plan review: is the plan complete, evidenced and the smallest change that does the job? Code review: does the code do
what the approved plan says, no more and no less? List any deviation.

## Match verification

Code review: the match numbers you measured yourself, and a revert check: revert the change to one matched function,
rebuild its object, confirm the match drops, restore it. Plan review: "n/a (plan)".

## bwr checklist

{{BWR_CHECKLIST}}

## Anti-cheat checklist

{{ANTICHEAT_CHECKLIST}}
