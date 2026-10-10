#!/usr/bin/env python3
"""Publish finished bw1-ai-sandbox workers as draft PRs on the fork. Runs on the droplet as the
`publisher` user, the only user holding the GitHub token. Worker containers never see it.

Each run:
  1. reads the export written by `sandbox_droplet.py export` (the sandbox's own `fetch`, run as the
     sandbox user just before this), which lists workers with their state, final result and the head
     of `sandbox/<name>` in the sandbox's bw1-decomp clone;
  2. picks finished workers (exited, not stopped on the usage limit, with new commits, not yet published);
  3. fetches each one from the sandbox clone into its own bare clone: objects and refs only, no hooks
     or config, so untrusted work is never executed here;
  4. refuses anything touching orig/ (the copyrighted game files) or adding a Git LFS pointer, then
     checks the changed paths against ALLOWED_PATHS and the size limits;
  5. pushes the commits unchanged as a NEW branch <BRANCH_PREFIX><date>-<name>-<sha7> to FORK_REPO. The
     pre-push hook refuses anything but creating a new branch under the prefix: no force push, no update,
     no main;
  6. opens a draft PR on FORK_REPO (base main) with the worker's commits, diffstat and final summary.

Limits: MAX_PRS_PER_DAY, MAX_OPEN_PRS, the stop file. FORK_REPO and PR_REPO must be the same repository
and never openblack/*. Nothing here builds or runs the worker's code.

Usage:
  publisher.py [--config /etc/bw1-sandbox/publisher.env] [--dry-run]
"""

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULTS = {
    "EXPORT": "/var/lib/bw1-sandbox/export/workers.json",
    "SANDBOX_CLONE": "/opt/bw1-sandbox/bw1-ai-sandbox/bw1-decomp",
    "PUBLISH_CLONE": "/var/lib/bw1-publisher/bw1-decomp.git",
    "STATE": "/var/lib/bw1-publisher/state.json",
    "LOG": "/var/log/bw1-publisher/publisher.log",
    "STOP_FILE": "/etc/bw1-sandbox/STOP",
    "GITHUB_ENV": "/etc/bw1-sandbox/github.env",
    "UPSTREAM_REPO": "openblack/bw1-decomp",
    "FORK_REPO": "raffclar/bw1-decomp",
    "PR_REPO": "raffclar/bw1-decomp",
    "PR_BASE": "main",
    "BRANCH_PREFIX": "ai-sandbox/",
    "MAX_PRS_PER_DAY": "3",
    "MAX_OPEN_PRS": "5",
    "MAX_CHANGED_FILES": "40",
    "MAX_CHANGED_LINES": "4000",
    "ALLOWED_PATHS": "src/*,include/*,tools/matching_ledger.jsonl",
}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LFS_MAGIC = b"version https://git-lfs"


class Stop(Exception):
    """Ends the run cleanly (not an error)."""


def read_env(path: str) -> dict:
    out = {}
    if os.path.exists(path):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    return out


class Publisher:
    def __init__(self, cfg: dict, dry_run: bool):
        self.cfg, self.dry = cfg, dry_run
        self.token = ""
        self.today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    # --- plumbing -------------------------------------------------------------------------------

    def log(self, msg: str) -> None:
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {'[dry-run] ' if self.dry else ''}{msg}"
        print(line, file=sys.stderr, flush=True)
        try:
            Path(self.cfg["LOG"]).parent.mkdir(parents=True, exist_ok=True)
            with open(self.cfg["LOG"], "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def git(self, *args: str, check: bool = True, env: dict = None) -> str:
        p = subprocess.run(["git", "-C", self.cfg["PUBLISH_CLONE"], *args], capture_output=True, text=True,
                           env={**os.environ, **(env or {})})
        if check and p.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {p.stderr.strip()[-600:]}")
        return p.stdout.strip()

    def gh(self, *args: str) -> str:
        p = subprocess.run(["gh", *args], capture_output=True, text=True,
                           env={**os.environ, "GH_TOKEN": self.token, "GH_PROMPT_DISABLED": "1"})
        if p.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args[:2])} failed: {p.stderr.strip()[-600:]}")
        return p.stdout.strip()

    def load_state(self) -> dict:
        try:
            return json.loads(Path(self.cfg["STATE"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        if self.dry:
            return
        path = Path(self.cfg["STATE"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    # --- guards ---------------------------------------------------------------------------------

    def guards(self) -> None:
        fork, pr_repo = self.cfg["FORK_REPO"], self.cfg["PR_REPO"]
        if not re.match(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", fork):
            raise SystemExit(f"FORK_REPO {fork!r} is not owner/name")
        if fork.lower().startswith("openblack/") or pr_repo.lower().startswith("openblack/"):
            raise SystemExit("refusing: branches and PRs go to your fork only, never openblack/*")
        if pr_repo.lower() != fork.lower():
            raise SystemExit(f"refusing: PR_REPO ({pr_repo}) must be FORK_REPO ({fork})")
        prefix = self.cfg["BRANCH_PREFIX"]
        if not re.match(r"^[a-z0-9][a-z0-9._-]*/$", prefix) or prefix.startswith(("main", "master")):
            raise SystemExit(f"bad BRANCH_PREFIX {prefix!r}")
        if os.path.exists(self.cfg["STOP_FILE"]):
            raise Stop(f"stop file {self.cfg['STOP_FILE']} present")
        self.token = read_env(self.cfg["GITHUB_ENV"]).get("GH_TOKEN", "")
        if not self.token and not self.dry:
            raise SystemExit(f"no GH_TOKEN in {self.cfg['GITHUB_ENV']}: refusing to publish")

    def open_prs(self) -> int:
        if self.dry:
            self.log(f"would run: gh pr list --repo {self.cfg['PR_REPO']} --state open --author @me")
            return 0
        rows = json.loads(self.gh("pr", "list", "--repo", self.cfg["PR_REPO"], "--state", "open", "--author", "@me",
                                  "--limit", "200", "--json", "headRefName"))
        return sum(1 for r in rows if r["headRefName"].startswith(self.cfg["BRANCH_PREFIX"]))

    # --- one worker -----------------------------------------------------------------------------

    def changed(self, base: str, head: str):
        files = [f for f in self.git("diff", "--name-only", f"{base}..{head}").splitlines() if f]
        stat = self.git("diff", "--numstat", f"{base}..{head}").splitlines()
        lines = 0
        for row in stat:
            parts = row.split("\t")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                lines += int(parts[0]) + int(parts[1])
        return files, lines

    def game_files(self, head: str, files) -> list:
        """Paths that must never leave the droplet: anything under orig/, and Git LFS pointers (the game's
        exe/dll/lib files live in bw1-build's LFS)."""
        bad = [f for f in files if f == "orig" or f.startswith("orig/") or f.endswith(".gitattributes")]
        for f in files:
            p = subprocess.run(["git", "-C", self.cfg["PUBLISH_CLONE"], "cat-file", "-p", f"{head}:{f}"],
                               capture_output=True)
            if p.returncode == 0 and p.stdout[:200].lstrip().startswith(LFS_MAGIC):
                bad.append(f)
        return bad

    def publish(self, w: dict, state: dict) -> bool:
        name, head = w["name"], w.get("head", "")
        if not NAME_RE.match(name) or not SHA_RE.match(head):
            self.log(f"skip {name!r}: bad name or head")
            return False
        local = f"refs/incoming/{name}"
        # Objects and refs only: nothing from the worker runs here.
        self.git("fetch", "-q", "--no-tags", self.cfg["SANDBOX_CLONE"], f"+{w['ref']}:{local}")
        if self.git("rev-parse", local) != head:
            self.log(f"skip {name}: sandbox/{name} moved since the export; next run")
            return False
        upstream = "refs/remotes/origin/main"
        base = self.git("merge-base", upstream, head, check=False) or w.get("base", "")
        if not base:
            self.log(f"skip {name}: no merge base with upstream main")
            return False
        commits = self.git("log", "--reverse", "--format=%h %s", f"{base}..{head}").splitlines()
        if not commits:
            self.log(f"skip {name}: no commits beyond upstream main")
            return False
        files, lines = self.changed(base, head)
        game = self.game_files(head, files)
        if game:
            self.log(f"REFUSED {name}: touches game files or LFS pointers: {game[:10]}")
            state.setdefault("rejected", {})[name] = {"head": head, "why": f"game files / LFS pointers: {game[:10]}"}
            return False
        allowed = [p.strip() for p in self.cfg["ALLOWED_PATHS"].split(",") if p.strip()]
        outside = [f for f in files if not any(fnmatch.fnmatch(f, p) for p in allowed)]
        if outside:
            self.log(f"skip {name}: changes outside {allowed}: {outside[:10]}")
            state.setdefault("rejected", {})[name] = {"head": head, "why": f"paths outside allowed: {outside[:10]}"}
            return False
        if len(files) > int(self.cfg["MAX_CHANGED_FILES"]) or lines > int(self.cfg["MAX_CHANGED_LINES"]):
            self.log(f"skip {name}: too large ({len(files)} files, {lines} lines)")
            state.setdefault("rejected", {})[name] = {"head": head, "why": f"too large: {len(files)} files, {lines} lines"}
            return False

        branch = f"{self.cfg['BRANCH_PREFIX']}{self.today.replace('-', '')}-{name}-{head[:7]}"
        title = f"[ai-sandbox] {name}: {commits[-1].split(' ', 1)[-1][:80]}"
        body = self.body(w, commits, base, head, files, lines)
        fork = self.cfg["FORK_REPO"]
        if self.dry:
            self.log(f"would push {head[:10]} ({len(commits)} commit(s), {len(files)} file(s), {lines} lines) "
                     f"to {fork} as NEW branch {branch} (pre-push hook: create-only, prefix {self.cfg['BRANCH_PREFIX']})")
            self.log(f"would run: gh pr create --repo {fork} --base {self.cfg['PR_BASE']} --head {branch} --draft "
                     f"--title {title!r}")
            print("----- PR body -----\n" + body + "\n-------------------", file=sys.stderr)
            return True
        helper = "!f() { echo username=x-access-token; echo \"password=$GH_TOKEN\"; }; f"
        self.git("-c", "credential.helper=", "-c", f"credential.helper={helper}",
                 "push", "-q", "fork", f"{head}:refs/heads/{branch}", env={"GH_TOKEN": self.token})
        body_file = Path(self.cfg["STATE"]).parent / f"pr-body-{name}.md"
        body_file.write_text(body, encoding="utf-8")
        url = self.gh("pr", "create", "--repo", fork, "--base", self.cfg["PR_BASE"], "--head", branch,
                      "--draft", "--title", title, "--body-file", str(body_file))
        self.log(f"published {name} {head[:10]} -> {branch}: {url}")
        state.setdefault("published", {})[name] = {"head": head, "branch": branch, "url": url}
        return True

    def body(self, w: dict, commits, base: str, head: str, files, lines: int) -> str:
        result = w.get("result") or {}
        text = (result.get("text") or "").strip().replace("```", "'''")[:5000]
        out = [
            f"Draft PR from bw1-ai-sandbox worker `{w['name']}` on the harness droplet.",
            "",
            f"- model: `{w.get('run_model') or w.get('model') or 'default'}`"
            + (f", effort `{w['effort']}`" if w.get("effort") else ""),
            f"- result: `{result.get('subtype')}`, {result.get('turns')} turns",
            f"- base: `{base[:10]}` (upstream main), head: `{head[:10]}`",
            f"- {len(files)} file(s), {lines} changed lines",
            "",
            "### Commits",
            "",
            *[f"- {c}" for c in commits],
            "",
            "### Files",
            "",
            *[f"- `{f}`" for f in files[:60]],
            "",
        ]
        if text:
            out += ["### Worker's final summary (unreviewed, written by the agent)", "", "```", text, "```", ""]
        out += [
            "### Not verified",
            "",
            "The publisher does not build or run worker code on the droplet. Workers check their own matches "
            "with the repo's tools inside their containers, but nobody has reviewed this. Run "
            "`tools/decomp-verify.py` and read the diff against AGENTS.md before marking it ready.",
            "",
            "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)",
        ]
        return "\n".join(out)

    # --- run ------------------------------------------------------------------------------------

    def run(self) -> int:
        try:
            self.guards()
            state = self.load_state()
            day = state.setdefault("days", {}).setdefault(self.today, {"prs": 0})
            if day["prs"] >= int(self.cfg["MAX_PRS_PER_DAY"]):
                raise Stop(f"PR limit reached ({day['prs']}/{self.cfg['MAX_PRS_PER_DAY']} today)")
            export = json.loads(Path(self.cfg["EXPORT"]).read_text(encoding="utf-8"))
            published = state.get("published", {})
            rejected = state.get("rejected", {})
            todo = [w for w in export.get("workers", [])
                    if w.get("state") == "exited" and not w.get("limited") and (w.get("commits") or 0) > 0
                    and w.get("head") and published.get(w["name"], {}).get("head") != w["head"]
                    and rejected.get(w["name"], {}).get("head") != w["head"]]
            self.log(f"{len(export.get('workers', []))} worker(s) exported, {len(todo)} ready to publish")
            if not todo:
                return 0
            open_count = self.open_prs()
            if not self.dry:
                self.git("fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
            for w in todo:
                if open_count >= int(self.cfg["MAX_OPEN_PRS"]):
                    raise Stop(f"{open_count} harness PRs open (cap {self.cfg['MAX_OPEN_PRS']}): waiting for review")
                if day["prs"] >= int(self.cfg["MAX_PRS_PER_DAY"]):
                    raise Stop("PR limit reached")
                try:
                    if self.publish(w, state):
                        day["prs"] += 1
                        open_count += 1
                except RuntimeError as e:
                    self.log(f"ERROR publishing {w['name']}: {e}")
                self.save_state(state)
            return 0
        except Stop as e:
            self.log(f"stopping: {e}")
            return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="/etc/bw1-sandbox/publisher.env")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    cfg = dict(DEFAULTS)
    cfg.update(read_env(args.config))
    return Publisher(cfg, args.dry_run).run()


if __name__ == "__main__":
    sys.exit(main())
