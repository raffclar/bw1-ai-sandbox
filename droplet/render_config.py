#!/usr/bin/env python3
"""Render the droplet's generated sandbox files (called by setup-droplet.sh; idempotent).

  render_config.py toml --template sandbox.droplet.toml.in --out FILE [--set NAME=VALUE]...
      @NAME@ placeholders replaced by the --set values, and @HOOKS_BUNDLE@ by the worker hooks (droplet/hooks plus
      quality_gate.py) as a base64 tar.gz that the worker setup unpacks and installs.
  render_config.py preamble --upstream preamble.md --section preamble-droplet.md --out preamble.droplet.md
      upstream's preamble with the harness section inserted before its "## Task" heading (appended when there is
      none), between markers. Upstream's file is never edited; an older marked section is replaced.
  render_config.py claude-settings --file ~/.claude/settings.json
      a host-side Claude Code's settings with CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=42 (other settings kept).
"""
import argparse
import base64
import gzip
import io
import json
import re
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_SOURCES = ("hooks/install_hooks.py", "hooks/no_extern.py", "hooks/stop_gate.py", "hooks/pre-commit",
                "quality_gate.py")
BEGIN, END = "<!-- bw1-harness: begin (droplet/preamble-droplet.md) -->", "<!-- bw1-harness: end -->"
COMPACT_ENV, COMPACT_PCT = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "42"


def hooks_bundle(root: Path = HERE) -> str:
    """The worker hooks as a reproducible base64 tar.gz (flat: file names only)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for rel in HOOK_SOURCES:
            data = (root / rel).read_bytes()
            info = tarfile.TarInfo(Path(rel).name)
            info.size, info.mode, info.mtime = len(data), 0o755, 0
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(gzip.compress(raw.getvalue(), mtime=0)).decode()


def render_toml(template: str, values: dict, root: Path = HERE) -> str:
    out = template
    if "@HOOKS_BUNDLE@" in out:
        out = out.replace("@HOOKS_BUNDLE@", hooks_bundle(root))
    for name, value in values.items():
        out = out.replace(f"@{name}@", value)
    left = sorted(set(re.findall(r"@[A-Z_]+@", out)))
    if left:
        raise SystemExit(f"render_config: unreplaced placeholders {left}")
    return out


def render_preamble(upstream: str, section: str) -> str:
    text = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n*", "", upstream, flags=re.S)
    block = f"{BEGIN}\n{section.strip()}\n{END}\n\n"
    m = re.search(r"^## Task\s*$", text, re.M)
    if m:
        return text[:m.start()] + block + text[m.start():]
    return text.rstrip("\n") + "\n\n" + block


def merge_claude_settings(settings: dict) -> dict:
    settings.setdefault("env", {})[COMPACT_ENV] = COMPACT_PCT
    return settings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("toml")
    t.add_argument("--template", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    p = sub.add_parser("preamble")
    p.add_argument("--upstream", required=True)
    p.add_argument("--section", required=True)
    p.add_argument("--out", required=True)
    c = sub.add_parser("claude-settings")
    c.add_argument("--file", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "toml":
        values = dict(s.split("=", 1) for s in a.set)
        Path(a.out).write_text(render_toml(Path(a.template).read_text(encoding="utf-8"), values), encoding="utf-8")
    elif a.cmd == "preamble":
        Path(a.out).write_text(render_preamble(Path(a.upstream).read_text(encoding="utf-8"),
                                               Path(a.section).read_text(encoding="utf-8")), encoding="utf-8")
    else:
        path = Path(a.file)
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merge_claude_settings(settings), indent=2) + "\n", encoding="utf-8")
        if json.loads(path.read_text(encoding="utf-8")).get("env", {}).get(COMPACT_ENV) != COMPACT_PCT:
            print(f"render_config: SELF-CHECK FAILED: {path} lacks {COMPACT_ENV}={COMPACT_PCT}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
