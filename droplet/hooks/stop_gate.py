#!/usr/bin/env python3
"""Worker Claude Code Stop hook (optional layer): before the worker ends its run, run the quality gate on its
committed branch and, if it has hard failures, block the stop ONCE so the worker fixes them.

Base: the first word of ~/case/gate-base when it exists (the hospital writes the case's upstream merge-base there),
else the merge-base of origin/main and HEAD. ~/case/gate-allow, when it exists, names the revision whose protected
paths are allowed (--allow-paths-from: the case's starting point). While HEAD is still that starting point (a
workup, or a treatment that has not committed yet) it never blocks: the case's inherited findings are the
treatment's job, and blocking a workup only pushed the Fellow into writing a handoff (seen on pr431, 2026-10-12).

Claude Code sets stop_hook_active when a stop hook already blocked this stop, so a worker that cannot fix the
findings still ends (the Discharge Nurse and the publisher refuse the branch anyway).
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import quality_gate  # noqa: E402

CASE = Path.home() / "case"


def first_word(path: Path) -> str:
    try:
        words = path.read_text(encoding="utf-8").split()
        return words[0] if words else ""
    except OSError:
        return ""


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    if data.get("stop_hook_active"):
        return 0
    repo = data.get("cwd") or "."
    base = first_word(CASE / "gate-base")
    if not base:
        base = subprocess.run(["git", "-C", repo, "merge-base", "origin/main", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
    if not base:
        return 0
    start = first_word(CASE / "gate-allow")
    if start:
        # A hospital case: findings the case started with are the treatment's job, not this run's. A workup (no
        # commits allowed) or a run that has not committed yet ends normally.
        head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        full = subprocess.run(["git", "-C", repo, "rev-parse", start], capture_output=True, text=True).stdout.strip()
        if head and head == full:
            return 0
    try:
        findings = quality_gate.run(repo, base, "HEAD", allow_from=start)
    except quality_gate.GitError:
        return 0
    hard = [f for f in findings if f.level == "hard"]
    if not hard:
        return 0
    lines = [f.format() for f in hard[:25]]
    more = f"\n... and {len(hard) - 25} more" if len(hard) > 25 else ""
    reason = (f"The quality gate found {len(hard)} hard failure(s) in your committed branch ({base[:10]}..HEAD). "
              "Fix them and commit before you finish; never work around the gate. If one cannot be fixed without "
              "breaking a match, leave the code NonMatching with a TODO and say so in your summary (or write "
              "~/case/handoff.md if you are on a hospital case).\n\n" + "\n".join(lines) + more)
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
