#!/usr/bin/env python3
"""Offline tests for the worker hooks, their installer and the rendered droplet settings (stdlib unittest; needs git
and, for the pre-commit test, a POSIX sh: git runs hooks with it).

  python3 droplet/tests/test_hooks.py -v
"""
import base64
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DROPLET = HERE.parent
sys.path.insert(0, str(DROPLET))
import render_config as R  # noqa: E402

G = ["git", "-c", "user.name=t", "-c", "user.email=t@e", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
UPSTREAM_PREAMBLE = "<!-- Prepended to every task prompt by sandbox.py spawn. -->\nYou are a worker.\n\n## Shared\n\nx\n\n## Task\n"


def git(repo, *a, env=None, check=True):
    return subprocess.run([*G, "-C", str(repo), *a], check=check, capture_output=True, text=True, env=env)


def rendered_toml():
    return R.render_toml((DROPLET / "sandbox.droplet.toml.in").read_text(encoding="utf-8"),
                         {"WORKER_CPUS": "2", "WORKER_MEMORY": "4g", "GAME_VERSION": "BW1W120"})


def bundle_files(toml_text):
    code = tomllib.loads(toml_text)["worker"]["setup"][2]
    b64 = re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', code).group(1)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


class RenderTests(unittest.TestCase):
    def test_toml_renders_with_the_hook_bundle(self):
        text = rendered_toml()
        cfg = tomllib.loads(text)
        self.assertEqual(cfg["worker"]["preamble"], "preamble.droplet.md")
        self.assertEqual(cfg["worker"]["cpus"], 2)
        self.assertNotIn("@", re.sub(r"[\w.+-]+@[\w.-]+", "", text.split("setup =")[0]))
        files = bundle_files(text)
        self.assertEqual(sorted(files), sorted(Path(p).name for p in R.HOOK_SOURCES))
        for rel in R.HOOK_SOURCES:
            self.assertEqual(files[Path(rel).name], (DROPLET / rel).read_bytes(), rel)
        self.assertEqual(R.hooks_bundle(), R.hooks_bundle())  # reproducible
        # The worker image's ninja (1.12) discards a v5 log ("build log version is too old; starting over") and
        # then re-downloads the toolchain with no network: a fresh tree's log must be v6 (pr431's discharge, 2026-10-12)
        setup = cfg["worker"]["setup"][2]
        self.assertIn('["# ninja log v6"]', setup)
        self.assertNotIn("ninja log v5", setup)
        with self.assertRaises(SystemExit):
            R.render_toml("x = @MISSING@", {})

    def test_setup_code_compiles_and_installs_the_hooks(self):
        code = tomllib.loads(rendered_toml())["worker"]["setup"][2]
        compile(code, "setup", "exec")
        self.assertIn("install_hooks.py", code)

    def test_preamble_is_inserted_before_task_and_idempotent(self):
        section = (DROPLET / "preamble-droplet.md").read_text(encoding="utf-8")
        once = R.render_preamble(UPSTREAM_PREAMBLE, section)
        self.assertEqual(R.render_preamble(once, section), once)
        self.assertEqual(once.count(R.BEGIN), 1)
        self.assertLess(once.index("Maintainer review rules (bwrsandman, PR #431)"), once.index("## Task"))
        self.assertTrue(once.rstrip().endswith("## Task"))
        self.assertIn("Hospital rules", once)
        newer = R.render_preamble(once, section.replace("13. No header", "13. NO header"))
        self.assertEqual(newer.count(R.BEGIN), 1)
        self.assertIn("13. NO header", newer)
        self.assertIn(R.BEGIN, R.render_preamble("no task heading here\n", section))

    def test_preamble_has_the_thirteen_rules_and_the_hospital_rules(self):
        text = (DROPLET / "preamble-droplet.md").read_text(encoding="utf-8")
        rules = text.split("## Maintainer review rules")[1].split("##")[0]
        self.assertEqual(len(re.findall(r"^\d+\. ", rules, re.M)), 13)
        for want in ("No code before a plan", "Don't improvise", "No weakening of checks", "I-PASS",
                     "__declspec(naked)", "Run the quality gate before finishing"):
            self.assertIn(want, text)

    def test_claude_settings_merge(self):
        t = Path(tempfile.mkdtemp())
        try:
            f = t / "settings.json"
            f.write_text(json.dumps({"env": {"X": "1"}, "model": "opus"}))
            self.assertEqual(R.main(["claude-settings", "--file", str(f)]), 0)
            data = json.loads(f.read_text())
            self.assertEqual(data["env"], {"X": "1", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "42"})
            self.assertEqual(data["model"], "opus")
        finally:
            shutil.rmtree(t, ignore_errors=True)


class InstallTests(unittest.TestCase):
    """The hooks as a new worker's setup installs them: unpacked into ~/.claude/hooks, then install_hooks.py."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="hooktest-"))
        self.home = self.t / "home"
        hooks = self.home / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        for name, data in bundle_files(rendered_toml()).items():
            (hooks / name).write_bytes(data)
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {"PATH": "/x:/usr/bin"}}))
        self.repo = self.t / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "config/BW1W120").mkdir(parents=True)
        (self.repo / "config/BW1W120/symbols.txt").write_text("?Real@Thing@@QAEXXZ = .text:0x1; // type:function\n")
        (self.repo / "src").mkdir()
        (self.repo / "src/Thing.cpp").write_text('#include "Thing.h"\n\nvoid Thing::Real()\n{\n}\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.installer = hooks / "install_hooks.py"

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def install(self, *extra):
        return subprocess.run([sys.executable, str(self.installer), "--home", str(self.home), "--repo", str(self.repo),
                               "--container-home", "/home/agent", *extra], capture_output=True, text=True)

    def test_install_is_idempotent_and_self_checks(self):
        for _ in range(2):
            p = self.install()
            self.assertEqual(p.returncode, 0, p.stderr)
        settings = json.loads((self.home / ".claude/settings.json").read_text())
        self.assertEqual(settings["env"], {"PATH": "/x:/usr/bin", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "42"})
        pre = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
        stop = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]]
        self.assertEqual(pre, ["python3 /home/agent/.claude/hooks/no_extern.py"])
        self.assertEqual(stop, ["python3 /home/agent/.claude/hooks/stop_gate.py"])
        self.assertEqual((self.repo / ".git/hooks/pre-commit").read_bytes(), (DROPLET / "hooks/pre-commit").read_bytes())
        self.assertIn("OK", p.stdout)

    def test_self_check_catches_a_changed_hook(self):
        self.assertEqual(self.install().returncode, 0)
        src = self.t / "src"
        shutil.copytree(self.home / ".claude/hooks", src)
        (self.repo / ".git/hooks/pre-commit").write_text("#!/bin/sh\nexit 0\n")
        p = self.install("--check-only", "--src", str(src))
        self.assertEqual(p.returncode, 1)
        self.assertIn("is not the harness pre-commit", p.stderr)
        settings = self.home / ".claude/settings.json"
        data = json.loads(settings.read_text())
        data["env"].pop("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
        settings.write_text(json.dumps(data))
        self.assertIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", self.install("--check-only", "--src", str(src)).stderr)

    def shim_env(self):
        """PATH with a `python3` that is this interpreter (the hooks call python3), HOME = the worker home."""
        bin_ = self.t / "bin"
        bin_.mkdir(exist_ok=True)
        shim = bin_ / "python3"
        shim.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n')
        shim.chmod(0o755)
        return {**os.environ, "HOME": str(self.home), "PATH": str(bin_) + os.pathsep + os.environ["PATH"]}

    @unittest.skipUnless(shutil.which("sh") or shutil.which("bash"), "git hooks need sh")
    def test_pre_commit_blocks_extern_tamper_and_hard_findings(self):
        self.assertEqual(self.install().returncode, 0)
        env = self.shim_env()

        def commit(path, text):
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(text)
            git(self.repo, "add", "-A", env=env)
            p = git(self.repo, "commit", "-qm", f"change {path}", env=env, check=False)
            if p.returncode != 0:
                git(self.repo, "reset", "-q", "--hard", env=env)
            return p

        p = commit("src/Thing.cpp", '#include "Thing.h"\n\nextern int g;\n\nvoid Thing::Real()\n{\n}\n')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("adds extern", p.stderr)
        p = commit("tools/decomp-diff.py", "print('100%')\n")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("tamper-path", p.stdout + p.stderr)
        p = commit("src/Thing.cpp", '#include "Thing.h"\n\nvoid Thing::Real()\n{\n\t__asm nop\n}\n')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("inline-asm", p.stdout + p.stderr)
        p = commit("src/Thing.cpp", '#include "Thing.h"\n\nvoid Thing::Real()\n{\n\tint a = 1;\n}\n')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # a file that had inline asm at the case's base (~/case/gate-base) may gain more; a later commit that slipped
        # asm into another file (--no-verify) does not make that file an asm file
        case_base = git(self.repo, "rev-parse", "HEAD", env=env).stdout.strip()
        (self.home / "case").mkdir(exist_ok=True)
        (self.home / "case/gate-base").write_text(case_base + "\n")
        p = commit("src/CPUCheck.cpp", "void P()\n{\n\t__asm cpuid\n}\n")
        self.assertNotEqual(p.returncode, 0)  # a new file with asm: hard
        (self.repo / "src/CPUCheck.cpp").write_text("void P()\n{\n\t__asm cpuid\n}\n")
        git(self.repo, "add", "-A", env=env)
        git(self.repo, "commit", "-qm", "asm without the hook", "--no-verify", env=env)
        p = commit("src/CPUCheck.cpp", "void P()\n{\n\t__asm cpuid\n\t__asm nop\n}\n")
        self.assertNotEqual(p.returncode, 0, "HEAD has asm there, but the case's base does not")
        (self.home / "case/gate-base").write_text(git(self.repo, "rev-parse", "HEAD", env=env).stdout.strip() + "\n")
        p = commit("src/CPUCheck.cpp", "void P()\n{\n\t__asm cpuid\n\t__asm nop\n}\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("inline-asm-existing", p.stdout + p.stderr)

    @unittest.skipUnless(shutil.which("sh") or shutil.which("bash"), "git hooks need sh")
    def test_pre_commit_checks_a_conflicted_merge_against_what_it_merges(self):
        self.assertEqual(self.install().returncode, 0)
        env = self.shim_env()
        base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        # upstream moves on: a tool, a config line at the top, and Thing.cpp (conflicting with the case)
        git(self.repo, "checkout", "-q", "-b", "up", env=env)
        (self.repo / "tools").mkdir()
        (self.repo / "tools/decomp-diff.py").write_text("print('upstream')\n")
        sym = self.repo / "config/BW1W120/symbols.txt"
        sym.write_text("up = .text:0x2;\n" + sym.read_text())
        (self.repo / "src/Thing.cpp").write_text('#include "Thing.h"\n\nvoid Thing::Real()\n{\n\tint up = 1;\n}\n')
        git(self.repo, "add", "-A", env=env)
        git(self.repo, "commit", "-qm", "upstream", "--no-verify", env=env)
        # the case: a config line at the end and its own Thing.cpp change
        git(self.repo, "checkout", "-q", "-b", "case", base, env=env)
        sym.write_text(sym.read_text() + "case = .text:0x3;\n")
        (self.repo / "src/Thing.cpp").write_text('#include "Thing.h"\n\nvoid Thing::Real()\n{\n\tint mine = 2;\n}\n')
        git(self.repo, "add", "-A", env=env)
        git(self.repo, "commit", "-qm", "case", "--no-verify", env=env)
        self.assertNotEqual(git(self.repo, "merge", "--no-edit", "up", env=env, check=False).returncode, 0)
        (self.repo / "src/Thing.cpp").write_text('#include "Thing.h"\n\nvoid Thing::Real()\n{\n\tint up = 1;\n'
                                                 '\tint mine = 2;\n}\n')
        git(self.repo, "add", "-A", env=env)
        p = git(self.repo, "commit", "--no-edit", env=env, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)  # upstream's tool and the auto-merged config pass
        # a merge that also changes a protected file beyond git's automatic merge is refused
        git(self.repo, "reset", "-q", "--hard", "HEAD~1", env=env)
        git(self.repo, "merge", "--no-edit", "up", env=env, check=False)
        (self.repo / "src/Thing.cpp").write_text('#include "Thing.h"\n\nvoid Thing::Real()\n{\n}\n')
        (self.repo / "tools/decomp-diff.py").write_text("print('always 100%')\n")
        git(self.repo, "add", "-A", env=env)
        p = git(self.repo, "commit", "--no-edit", env=env, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("tamper-path", p.stdout + p.stderr)

    def test_stop_hook_blocks_once(self):
        self.assertEqual(self.install().returncode, 0)
        base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        (self.home / "case").mkdir()
        (self.home / "case/gate-base").write_text(base + "\n")
        (self.repo / "src/Thing.cpp").write_text('#include "Thing.h"\n\nvoid Thing::Real()\n{\n\tthis->field_0x4 = 1;\n}\n')
        git(self.repo, "commit", "-qam", "uses a field", "--no-verify")
        stop = self.home / ".claude/hooks/stop_gate.py"
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}

        def run(active):
            return subprocess.run([sys.executable, str(stop)], input=json.dumps(
                {"stop_hook_active": active, "cwd": str(self.repo)}), capture_output=True, text=True, env=env)

        p = run(False)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("field-name", out["reason"])
        self.assertEqual(run(True).stdout, "")  # never blocks twice
        # a hospital case still at its starting point (a workup): inherited findings never block
        (self.home / "case/gate-allow").write_text(git(self.repo, "rev-parse", "HEAD").stdout.strip() + "\n")
        self.assertEqual(run(False).stdout, "")
        (self.repo / "src/Thing.cpp").write_text('#include "Thing.h"\n\nvoid Thing::Real()\n{\n\tthis->field_0x8 = 1;\n}\n')
        git(self.repo, "commit", "-qam", "the treatment's commit", "--no-verify")
        self.assertEqual(json.loads(run(False).stdout)["decision"], "block")  # once it commits, it is checked


if __name__ == "__main__":
    unittest.main()
