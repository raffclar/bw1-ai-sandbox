| # | Check | Result (ok / violated: where) |
|---|-------|-------------------------------|
| A1 | No inline assembly, __declspec(naked), _emit, or bytes/strings cast to function pointers | |
| A2 | No section, alloc_text or linker pragmas; any new optimize/inline_depth/auto_inline pragma has evidence | |
| A3 | No protected file changed: config/**, tools/** (except the ledger), configure.py, objdiff.json, build files, objects, AGENTS.md, CLAUDE.md, .claude/**, .github/**, .clang-format | |
| A4 | No checks weakened: no test, tool, hook or report edited or skipped; no --no-verify; no `Matching` status set on a unit that does not match | |
| A5 | No fakematch: no unused locals, casts, volatile, pragmas or reordering whose only purpose is to steer the compiler without evidence (each needs a // TODO: fakematch and the Chief) | |
| A6 | The revert check shows the match comes from the source (code review), and the objects were rebuilt (no stale objects) | |
| A7 | No `extern`; globals are static members of their owning class | |
