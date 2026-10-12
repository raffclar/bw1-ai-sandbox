#!/usr/bin/env python3
"""Worker hook: the project never uses `extern` (AGENTS.md "Globals": a global is a static member of its owning class,
and a use that can't be declared yet is commented out with a TODO).

  no_extern.py              Claude Code PreToolUse hook (JSON on stdin): blocks an Edit/Write/MultiEdit that adds
                            `extern` to a C/C++ file (exit 2)
  no_extern.py pre-commit   git pre-commit check: refuses a commit whose staged C/C++ lines add `extern`; a merge
                            commit is checked against the merged-in side (MERGE_HEAD), so upstream's own externs pass

Comment lines don't count. The publisher refuses such a branch as the last guard.
"""
import json
import os
import re
import subprocess
import sys

EXTERN = re.compile(r"\bextern\b")
SOURCES = (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".inl", ".inc")
WHY = ("`extern` is never allowed in this project (AGENTS.md, Globals). Declare the global as a static data "
       "member of its owning class in that class's header, or, if it can't be declared there yet, comment the "
       "use out with a // TODO: explaining why.")


def uses(text):
    n = 0
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith(("//", "/*", "*")) and EXTERN.search(s.split("//")[0]):
            n += 1
    return n


def merge_head():
    p = subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def pre_commit():
    # A merge commit is checked against the merged-in side (MERGE_HEAD), as the pre-commit's quality gate is: the
    # externs that side already has are not the merge's own (pr431m2: main's own externs, 2026-10-12)
    base = merge_head()
    diff = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color"] + ([base] if base else [])
                          + ["--"] + ["*" + s for s in SOURCES], capture_output=True, text=True).stdout
    added = [l[1:].strip() for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    bad = [l for l in added if uses(l)]
    if bad:
        sys.exit("commit refused: it adds extern: " + "; ".join(bad[:5]) + "\n" + WHY)
    sys.exit(0)


def tool_hook():
    data = json.load(sys.stdin)
    tool, args = data.get("tool_name", ""), data.get("tool_input", {})
    path = args.get("file_path", "")
    if not path.endswith(SOURCES):
        sys.exit(0)
    if tool == "Write":
        old = open(path, encoding="utf-8", errors="replace").read() if os.path.exists(path) else ""
        added = uses(args.get("content", "")) - uses(old)
    elif tool == "Edit":
        added = uses(args.get("new_string", "")) - uses(args.get("old_string", ""))
    elif tool == "MultiEdit":
        added = sum(uses(e.get("new_string", "")) - uses(e.get("old_string", "")) for e in args.get("edits", []))
    else:
        added = 0
    if added > 0:
        print("Blocked: this edit adds extern. " + WHY, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pre-commit":
        pre_commit()
    tool_hook()
