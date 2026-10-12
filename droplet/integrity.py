#!/usr/bin/env python3
"""Tamper protection for the harness: a root-owned manifest of sha256 hashes that the publisher and the Discharge
Nurse check before they act.

  integrity.py write [--droplet /opt/bw1-sandbox/droplet] [--out /etc/bw1-sandbox/integrity.json]   (root, setup)
  integrity.py check [--manifest /etc/bw1-sandbox/integrity.json]                                   (exit 1 = refuse)

The manifest lists
  files         the host's copies of the quality gate, hospital.py, this file, the publisher, the harness rules,
                the worker hooks and the hospital's prompts, templates and verification script (absolute paths), and
  worker_hooks  what each worker must have installed, relative to its folder /srv/decomp/workers/<name>/
                (home/.claude/hooks/*, repo/.git/hooks/pre-commit); the Discharge Nurse compares them.

The manifest must be owned by root and writable by nobody else; any missing file or other hash is a refusal. After
changing any listed file, re-run `integrity.py write` as root (setup-droplet.sh does).
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DROPLET = Path("/opt/bw1-sandbox/droplet")
MANIFEST = Path("/etc/bw1-sandbox/integrity.json")
HOST_FILES = ("quality_gate.py", "hospital.py", "integrity.py", "publisher.py", "preamble-droplet.md",
              "hooks/install_hooks.py", "hooks/no_extern.py", "hooks/stop_gate.py", "hooks/pre-commit")
HOSPITAL_DIR = "hospital"  # prompts, templates and the discharge verification script: every file in it
WORKER_HOOKS = {  # path in the worker's folder -> source in the droplet folder
    "home/.claude/hooks/quality_gate.py": "quality_gate.py",
    "home/.claude/hooks/no_extern.py": "hooks/no_extern.py",
    "home/.claude/hooks/stop_gate.py": "hooks/stop_gate.py",
    "home/.claude/hooks/pre-commit": "hooks/pre-commit",
    "home/.claude/hooks/install_hooks.py": "hooks/install_hooks.py",
    "repo/.git/hooks/pre-commit": "hooks/pre-commit",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def build(droplet: Path) -> dict:
    files = [droplet / f for f in HOST_FILES]
    files += sorted(p for p in (droplet / HOSPITAL_DIR).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    return {
        "version": 1,
        "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "droplet": str(droplet),
        "files": {p.as_posix(): sha256(p) for p in files},
        "worker_hooks": {rel: sha256(droplet / src) for rel, src in WORKER_HOOKS.items()},
    }


def root_owned(path: Path) -> bool:
    """Owned by root and writable by nobody else (always true where there are no uids, e.g. Windows)."""
    if not hasattr(os, "geteuid"):
        return True
    st = os.stat(path)
    return st.st_uid == 0 and not st.st_mode & 0o022


def load(manifest: Path, require_root: bool = True):
    """(manifest dict or None, problems)."""
    try:
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, [f"no readable integrity manifest {manifest}: {e}"]
    if require_root and not root_owned(Path(manifest)):
        return None, [f"{manifest} is not owned by root (or is writable by others)"]
    return data, []


def verify(manifest: Path = MANIFEST, require_root: bool = True) -> list:
    """Problems with the host's files (an empty list means everything matches)."""
    data, problems = load(manifest, require_root)
    if data is None:
        return problems
    files = data.get("files") or {}
    if not files:
        return [f"{manifest} lists no files"]
    for path, want in sorted(files.items()):
        p = Path(path)
        if not p.is_file():
            problems.append(f"{path} is missing")
        elif sha256(p) != want:
            problems.append(f"{path} has changed since {manifest} was written")
    return problems


def worker_hook_problems(manifest: dict, read) -> list:
    """Compare a worker's installed hooks with the manifest. read(rel) returns the bytes of the file at that path in
    the worker's folder, or None."""
    problems = []
    for rel, want in sorted((manifest.get("worker_hooks") or {}).items()):
        data = read(rel)
        if data is None:
            problems.append(f"worker hook {rel} is missing")
        elif sha256_bytes(data) != want:
            problems.append(f"worker hook {rel} differs from the installed harness")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("--droplet", default=str(DROPLET))
    w.add_argument("--out", default=str(MANIFEST))
    c = sub.add_parser("check")
    c.add_argument("--manifest", default=str(MANIFEST))
    a = ap.parse_args(argv)
    if a.cmd == "write":
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            print("integrity: write runs as root (setup-droplet.sh)", file=sys.stderr)
            return 2
        out = Path(a.out)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(build(Path(a.droplet)), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)
        print(f"integrity: wrote {out}")
        return 0
    problems = verify(Path(a.manifest))
    for p in problems:
        print(f"integrity: {p}", file=sys.stderr)
    print("integrity: OK" if not problems else f"integrity: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
