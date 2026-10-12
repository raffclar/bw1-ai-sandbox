#!/usr/bin/env python3
"""Install the bw1 harness's worker hooks and Claude Code settings, idempotently, then check them.

Run by the worker setup in sandbox.droplet.toml (inside a new worker's container, in /work), and by hand on the
host to (re)install them into an existing worker:

  install_hooks.py [--home DIR] [--repo DIR] [--src DIR] [--container-home /home/agent] [--chown UID:GID]
                   [--no-stop-hook] [--check-only]

  ~/.claude/hooks/   quality_gate.py, no_extern.py, stop_gate.py, pre-commit, install_hooks.py (copied from --src,
                     default: this file's folder, or its parent: droplet/ holds quality_gate.py)
  ~/.claude/settings.json
                     env CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=42 (compaction at 42% of the context window), a PreToolUse
                     hook (no_extern.py on Edit/Write/MultiEdit) and a Stop hook (stop_gate.py); other settings kept
  <repo>/.git/hooks/pre-commit
                     the pre-commit above: no extern, quality_gate.py --staged (hard failures block the commit)

On the host, --home is the worker's home folder (/srv/decomp/workers/<name>/home) and --container-home the path the
container sees (/home/agent), which the settings' hook commands use. --chown hands the files to the worker uid.

The self-check (also --check-only) fails (exit 1) when a hook differs from --src, the settings lack the hooks or the
compaction override, or the git hook differs. Whether this Claude Code honours CLAUDE_AUTOCOMPACT_PCT_OVERRIDE is
checked by looking for the name in its installed files (a warning only: /context does not show the override).
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOK_FILES = ("quality_gate.py", "no_extern.py", "stop_gate.py", "pre-commit", "install_hooks.py")
COMPACT_ENV = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
COMPACT_PCT = "42"
HERE = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source(src: Path, name: str) -> Path:
    """A hook file in --src, or in its parent (the droplet folder holds quality_gate.py, droplet/hooks the rest)."""
    return src / name if (src / name).is_file() else src.parent / name


def git_hook_path(repo: Path) -> Path:
    out = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), "rev-parse", "--git-path",
                          "hooks/pre-commit"], capture_output=True, text=True, check=True).stdout.strip()
    p = Path(out)
    return p if p.is_absolute() else repo / p


def merge_settings(settings: dict, hooks_dir: str, stop_hook: bool) -> dict:
    """The settings with the compaction override and the harness hooks added (each once)."""
    settings.setdefault("env", {})[COMPACT_ENV] = COMPACT_PCT
    hooks = settings.setdefault("hooks", {})

    def ensure(event, matcher, script):
        entries = hooks.setdefault(event, [])
        command = f"python3 {hooks_dir}/{script}"
        if any(script in h.get("command", "") for e in entries for h in e.get("hooks", [])):
            for e in entries:  # keep one entry, pointing at the current path
                for h in e.get("hooks", []):
                    if script in h.get("command", ""):
                        h["command"] = command
            return
        entry = {"hooks": [{"type": "command", "command": command}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        entries.append(entry)

    ensure("PreToolUse", "Edit|Write|MultiEdit", "no_extern.py")
    if stop_hook:
        ensure("Stop", "", "stop_gate.py")
    return settings


def claude_supports_override() -> bool:
    """Whether the installed Claude Code mentions the compaction override (its native binary, or its package)."""
    exe = shutil.which("claude")
    if not exe:
        return False
    target = Path(os.path.realpath(exe))
    needle = COMPACT_ENV.encode()
    candidates = [target]
    if target.stat().st_size < 1_000_000:  # a launcher script: look through its package
        root = target.parent
        for _ in range(3):
            if (root / "package.json").exists():
                break
            root = root.parent
        candidates = [p for p in root.rglob("*") if p.is_file() and p.suffix in ("", ".js", ".mjs", ".cjs")][:400]
    for p in candidates:
        try:
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(8 << 20)
                    if not chunk:
                        break
                    if needle in chunk:
                        return True
        except OSError:
            continue
    return False


def check(home: Path, repo: Path, src: Path, hooks_dir: str, stop_hook: bool) -> list:
    problems = []
    dest = home / ".claude" / "hooks"
    for name in HOOK_FILES:
        if not (dest / name).is_file():
            problems.append(f"missing {dest / name}")
        elif sha256(dest / name) != sha256(source(src, name)):
            problems.append(f"{dest / name} differs from {source(src, name)}")
    try:
        settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if (settings.get("env") or {}).get(COMPACT_ENV) != COMPACT_PCT:
        problems.append(f"settings.json env {COMPACT_ENV} is not {COMPACT_PCT}")
    commands = [h.get("command", "") for entries in (settings.get("hooks") or {}).values() for e in entries
                for h in e.get("hooks", [])]
    wanted = ["no_extern.py"] + (["stop_gate.py"] if stop_hook else [])
    for script in wanted:
        if f"python3 {hooks_dir}/{script}" not in commands:
            problems.append(f"settings.json has no hook running {hooks_dir}/{script}")
    if repo:
        hook = git_hook_path(repo)
        if not hook.is_file() or sha256(hook) != sha256(source(src, "pre-commit")):
            problems.append(f"git hook {hook} is not the harness pre-commit")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=str(Path.home()))
    ap.add_argument("--repo", default=".", help="the worker's clone ('' to skip the git hook)")
    ap.add_argument("--src", default=str(HERE))
    ap.add_argument("--container-home", default="", help="the home folder as the container sees it (default --home)")
    ap.add_argument("--chown", default="", metavar="UID:GID")
    ap.add_argument("--no-stop-hook", action="store_true")
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args(argv)
    home, src = Path(a.home), Path(a.src).resolve()
    repo = Path(a.repo) if a.repo else None
    hooks_dir = f"{(a.container_home or a.home).rstrip('/')}/.claude/hooks"
    stop_hook = not a.no_stop_hook
    owner = tuple(int(x) for x in a.chown.split(":")) if a.chown else None
    written = []
    if not a.check_only:
        dest = home / ".claude" / "hooks"
        dest.mkdir(parents=True, exist_ok=True)
        for name in HOOK_FILES:
            if (dest / name).resolve() != source(src, name).resolve():
                shutil.copyfile(source(src, name), dest / name)
            os.chmod(dest / name, 0o755)
            written.append(dest / name)
        settings_path = home / ".claude" / "settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        settings_path.write_text(json.dumps(merge_settings(settings, hooks_dir, stop_hook), indent=2) + "\n",
                                 encoding="utf-8")
        written += [settings_path, dest.parent, dest]
        if repo:
            hook = git_hook_path(repo)
            hook.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source(src, "pre-commit"), hook)
            os.chmod(hook, 0o755)
            written.append(hook)
        if owner:
            for p in written:
                os.chown(p, *owner)
    problems = check(home, repo, src, hooks_dir, stop_hook)
    for p in problems:
        print(f"install_hooks: SELF-CHECK FAILED: {p}", file=sys.stderr)
    if not claude_supports_override():
        print(f"install_hooks: warning: the installed Claude Code does not mention {COMPACT_ENV}; compaction stays "
              "at its default threshold", file=sys.stderr)
    if problems:
        return 1
    print(f"install_hooks: OK ({len(HOOK_FILES)} hooks in {home / '.claude' / 'hooks'}, {COMPACT_ENV}={COMPACT_PCT}"
          f"{', stop hook' if stop_hook else ''}{', git pre-commit' if repo else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
