#!/usr/bin/env python3
"""Offline tests for the publisher's guards (stdlib unittest; needs git and bash, no network).

  python3 droplet/tests/test_publisher.py -v

Covers: the gh allowlist (allowed and refused calls, both remotes), the push-URL guard, the pre-push
hook, a publish dry run with PR_TARGET=openblack (head raffclar:<branch>), the comments command
against scratch data (harness PRs only), and reply on harness / non-harness PRs.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DROPLET = HERE.parent
sys.path.insert(0, str(DROPLET))
import publisher as P  # noqa: E402

FORK, UP = "raffclar/bw1-decomp", "openblack/bw1-decomp"
G = ["git", "-c", "user.name=t", "-c", "user.email=t@e", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]


def git(*a, cwd=None):
    return subprocess.run([*G, *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.g = P.GhGuard(FORK, UP, "ai-sandbox/")
        self.harness = {(UP, 7): True, (FORK, 3): True, (UP, 99): False, (FORK, 98): False}
        self.is_h = lambda repo, n: self.harness.get((repo, n), False)

    def ok(self, args, kind):
        self.assertEqual(self.g.check(args, self.is_h), kind, args)

    def refused(self, args):
        with self.assertRaises(P.Refused, msg=" ".join(args)):
            self.g.check(args, self.is_h)

    def test_allowed(self):
        self.ok(["pr", "list", "--repo", UP, "--author", "@me"], "openblack")
        self.ok(["pr", "list", "--repo", FORK], "fork")
        self.ok(["pr", "view", "7", "--repo", UP, "--json", "headRefName"], "openblack")
        self.ok(["pr", "create", "--repo", UP, "--head", "raffclar:ai-sandbox/x", "--base", "main", "--draft"], "openblack")
        self.ok(["pr", "create", "--repo", FORK, "--head", "ai-sandbox/x", "--draft"], "fork")
        self.ok(["pr", "comment", "7", "--repo", UP, "--body-file", "f"], "openblack")
        self.ok(["pr", "review", "7", "--repo", UP, "--comment", "--body-file", "f"], "openblack")
        self.ok(["pr", "comment", "3", "--repo", FORK, "--body-file", "f"], "fork")
        self.ok(["api", f"repos/{UP}/issues/7/comments", "--paginate"], "openblack")
        self.ok(["api", f"repos/{UP}/pulls/7/comments"], "openblack")
        self.ok(["api", f"repos/{UP}/pulls/7/reviews"], "openblack")
        self.ok(["api", f"repos/{UP}/commits/{'a' * 40}/status"], "openblack")
        self.ok(["api", f"repos/{UP}/actions/runs?head_sha={'a' * 40}&per_page=20"], "openblack")
        self.ok(["api", f"repos/{FORK}/issues/3/comments"], "fork")

    def test_refused_comment_edits_and_deletes(self):
        self.refused(["pr", "comment", "7", "--repo", UP, "--edit-last", "--body", "x"])
        self.refused(["pr", "comment", "7", "--repo", UP, "--delete-last"])
        self.refused(["api", "-X", "PATCH", f"repos/{UP}/issues/comments/123", "-f", "body=x"])
        self.refused(["api", "--method", "DELETE", f"repos/{UP}/issues/comments/123"])
        self.refused(["api", "-X", "DELETE", f"repos/{UP}/pulls/comments/5"])
        self.refused(["api", f"repos/{UP}/issues/comments/123"])  # not an allowed endpoint, even as GET

    def test_refused_pr_state_changes(self):
        for sub in ("close", "merge", "ready", "reopen", "edit", "checkout", "lock"):
            self.refused(["pr", sub, "7", "--repo", UP])
        self.refused(["pr", "edit", "7", "--repo", UP, "--add-label", "x"])
        self.refused(["pr", "review", "7", "--repo", UP, "--approve"])
        self.refused(["pr", "review", "7", "--repo", UP, "--request-changes", "--body", "x"])
        self.refused(["pr", "review", "7", "--repo", UP])  # not --comment

    def test_refused_labels_issues_and_other_commands(self):
        self.refused(["label", "create", "x", "--repo", UP])
        self.refused(["issue", "comment", "7", "--repo", UP, "--body", "x"])
        self.refused(["issue", "edit", "7", "--repo", UP, "--add-label", "x"])
        self.refused(["repo", "delete", UP])
        self.refused(["workflow", "run", "build.yml", "--repo", UP])
        self.refused(["secret", "list", "--repo", UP])
        self.refused(["api", "-X", "POST", f"repos/{UP}/issues/7/labels", "-f", "labels[]=x"])
        self.refused(["api", f"repos/{UP}/issues/7/comments", "-f", "body=x"])  # fields make it a POST
        self.refused(["api", "-X", "PUT", f"repos/{UP}/pulls/7/merge"])

    def test_refused_non_harness_and_other_repos(self):
        self.refused(["pr", "comment", "99", "--repo", UP, "--body", "x"])
        self.refused(["pr", "review", "98", "--repo", FORK, "--comment", "--body", "x"])
        self.refused(["api", f"repos/{UP}/issues/99/comments"])
        self.refused(["pr", "create", "--repo", UP, "--head", "raffclar:feature", "--draft"])
        self.refused(["pr", "create", "--repo", UP, "--head", "someone:ai-sandbox/x", "--draft"])
        self.refused(["pr", "create", "--repo", UP, "--head", "ai-sandbox/x", "--draft"])  # same-repo head on openblack
        self.refused(["pr", "create", "--repo", UP, "--head", "raffclar:ai-sandbox/x"])  # not a draft
        self.refused(["pr", "list", "--repo", "someone/else"])
        self.refused(["pr", "list"])
        self.refused(["api", "repos/someone/else/issues/1/comments"])


class RepoTests(unittest.TestCase):
    """Scratch repos: U (upstream), C (sandbox clone with sandbox/<name> refs), P.git (publisher clone)."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="pubtest-"))
        t = self.t
        git("init", "-q", str(t / "U"))
        (t / "U/src").mkdir()
        (t / "U/src/a.cpp").write_text("int a;\n")
        git("-C", str(t / "U"), "add", "-A")
        git("-C", str(t / "U"), "commit", "-qm", "base")
        git("clone", "-q", str(t / "U"), str(t / "C"))
        self.heads = {}
        for name, path, content in (("good", "src/a.cpp", "int a = 1;\n"), ("orig", "orig/BW1W120/x.exe", "x\n"),
                                    ("lfs", "src/b.lib", "version https://git-lfs.github.com/spec/v1\n")):
            git("-C", str(t / "C"), "checkout", "-q", "-B", f"w-{name}", "origin/main")
            (t / "C" / path).parent.mkdir(parents=True, exist_ok=True)
            (t / "C" / path).write_text(content)
            git("-C", str(t / "C"), "add", "-A")
            git("-C", str(t / "C"), "commit", "-qm", f"{name}: change {path}")
            git("-C", str(t / "C"), "update-ref", f"refs/remotes/sandbox/{name}", "HEAD")
            self.heads[name] = git("-C", str(t / "C"), "rev-parse", "HEAD")
        git("clone", "-q", "--bare", str(t / "U"), str(t / "P.git"))
        git("-C", str(t / "P.git"), "update-ref", "refs/remotes/origin/main", git("-C", str(t / "U"), "rev-parse", "main"))
        git("-C", str(t / "P.git"), "remote", "add", "fork", f"https://github.com/{FORK}.git")
        git("-C", str(t / "P.git"), "remote", "set-url", "--push", "origin", "DISABLED")
        shutil.copy(DROPLET / "pre-push", t / "P.git/hooks/pre-push")
        workers = [{"name": n, "state": "exited", "limited": None, "model": "opus", "head": h, "commits": 1,
                    "ref": f"refs/remotes/sandbox/{n}", "result": {"subtype": "success", "text": f"{n} done", "turns": 9}}
                   for n, h in self.heads.items()]
        (t / "workers.json").write_text(json.dumps({"workers": workers}))
        (t / "github.env").write_text("GH_TOKEN=fork-token-dummy\n")
        (t / "openblack.env").write_text("GH_OPENBLACK_PR_TOKEN=openblack-token-dummy\n")
        (t / "gh.log").write_text("")
        self.fixture = {
            f"pr list --repo {UP}": [
                {"number": 7, "headRefName": "ai-sandbox/20261010-good-abc1234", "headRepositoryOwner": {"login": "raffclar"},
                 "headRefOid": "b" * 40, "url": "u7", "title": "t"},
                {"number": 99, "headRefName": "someone-elses-feature", "headRepositoryOwner": {"login": "raffclar"},
                 "headRefOid": "c" * 40, "url": "u99", "title": "t"}],
            f"pr list --repo {FORK}": [],
            f"pr view 7 --repo {UP}": {"headRefName": "ai-sandbox/20261010-good-abc1234", "headRepositoryOwner": {"login": "raffclar"}},
            f"pr view 99 --repo {UP}": {"headRefName": "someone-elses-feature", "headRepositoryOwner": {"login": "raffclar"}},
            f"api repos/{UP}/issues/7/comments": [{"id": 11, "user": {"login": "bwrsandman"}, "body": "Please rename x.", "created_at": "t", "html_url": "h"}],
            f"api repos/{UP}/pulls/7/comments": [{"id": 12, "user": {"login": "raffclar"}, "body": "Line note", "path": "src/a.cpp", "line": 1}],
            f"api repos/{UP}/pulls/7/reviews": [{"id": 13, "user": {"login": "bwrsandman"}, "state": "COMMENTED", "body": "Looks close"}],
            f"api repos/{UP}/commits/": {"state": "pending"},
            f"api repos/{UP}/actions/runs": {"workflow_runs": [{"name": "Build", "status": "completed", "conclusion": "failure"}]},
        }
        (t / "fixture.json").write_text(json.dumps(self.fixture))
        os.environ["FAKE_GH_LOG"] = str(t / "gh.log")
        os.environ["FAKE_GH_FIXTURE"] = str(t / "fixture.json")

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def cfg(self, **over):
        t = self.t
        c = dict(P.DEFAULTS)
        c.update({"EXPORT": str(t / "workers.json"), "SANDBOX_CLONE": str(t / "C"), "PUBLISH_CLONE": str(t / "P.git"),
                  "STATE": str(t / "state.json"), "COMMENTS_DIR": str(t / "comments"), "LOG": str(t / "pub.log"),
                  "STOP_FILE": str(t / "STOP"), "GITHUB_ENV": str(t / "github.env"), "OPENBLACK_ENV": str(t / "openblack.env"),
                  "GH_BIN": f"{Path(sys.executable).as_posix()} {(HERE / 'fake_gh.py').as_posix()}",
                  "FEEDBACK_DIR": str(t / "feedback")})
        c.update(over)
        return c

    def gh_calls(self):
        return [json.loads(line) for line in (self.t / "gh.log").read_text().splitlines() if line]

    def test_push_url_guard(self):
        pub = P.Publisher(self.cfg(), dry_run=True)
        self.assertEqual(pub.assert_push_url(), f"https://github.com/{FORK}.git")
        git("-C", str(self.t / "P.git"), "remote", "set-url", "--push", "fork", f"https://github.com/{UP}.git")
        with self.assertRaises(P.Refused):
            pub.assert_push_url()
        git("-C", str(self.t / "P.git"), "remote", "set-url", "--push", "fork", "https://github.com/someone/else.git")
        with self.assertRaises(P.Refused):
            pub.assert_push_url()

    def test_origin_push_disabled(self):
        self.assertEqual(git("-C", str(self.t / "P.git"), "remote", "get-url", "--push", "origin"), "DISABLED")

    def test_pre_push_hook(self):
        z = "0" * 40
        hook = (self.t / "P.git/hooks/pre-push").as_posix()

        def run(url, line):
            # shutil.which follows PATH (on Windows, a bare "bash" would start System32's WSL bash)
            # bytes, not text: on Windows text mode would turn the "\n" git sends into "\r\n"
            p = subprocess.run([shutil.which("bash"), hook, "fork", url], input=line.encode(), capture_output=True,
                               cwd=self.t / "P.git", env={**os.environ, "GIT_DIR": "."})
            self.hook_err = p.stderr.decode(errors="replace")
            return p.returncode
        for n in self.heads:
            git("-C", str(self.t / "P.git"), "fetch", "-q", str(self.t / "C"), f"+refs/remotes/sandbox/{n}:refs/incoming/{n}")
        ok = f"https://github.com/{FORK}.git"
        self.assertEqual(run(ok, f"refs/heads/x {self.heads['good']} refs/heads/ai-sandbox/new {z}\n"), 0, self.hook_err)
        self.assertNotEqual(run(ok, f"refs/heads/x {self.heads['orig']} refs/heads/ai-sandbox/new {z}\n"), 0)
        self.assertNotEqual(run(ok, f"refs/heads/x {self.heads['lfs']} refs/heads/ai-sandbox/new {z}\n"), 0)
        self.assertNotEqual(run(f"https://github.com/{UP}.git", f"refs/heads/x {self.heads['good']} refs/heads/ai-sandbox/new {z}\n"), 0)
        self.assertNotEqual(run(ok, f"refs/heads/x {self.heads['good']} refs/heads/main {z}\n"), 0)
        self.assertNotEqual(run(ok, f"refs/heads/x {self.heads['good']} refs/heads/ai-sandbox/old {self.heads['orig']}\n"), 0)
        self.assertNotEqual(run(ok, f"(delete) {z} refs/heads/ai-sandbox/old {self.heads['good']}\n"), 0)

    def test_publish_dry_run_openblack(self):
        pub = P.Publisher(self.cfg(PR_TARGET="openblack", PR_REPO=UP), dry_run=True)
        pub.guards()
        pub.cmd_publish()
        log = (self.t / "pub.log").read_text()
        self.assertIn(f"gh pr create --repo {UP} --base main --head raffclar:ai-sandbox/", log)
        self.assertIn("[token: openblack]", log)
        self.assertIn("REFUSED orig", log)
        self.assertIn("REFUSED lfs", log)
        calls = self.gh_calls()
        self.assertTrue(all(c["args"][:2] in (["pr", "list"], ["pr", "view"]) for c in calls), calls)  # reads only
        self.assertTrue(all(c["token"] == "openblack-token-dummy" for c in calls if UP in c["args"]))
        self.assertEqual(git("-C", str(self.t / "P.git"), "ls-remote", "--heads", ".", "ai-sandbox/*"), "")

    def add_worker(self, name, changes):
        t = self.t
        git("-C", str(t / "C"), "checkout", "-q", "-B", f"w-{name}", "origin/main")
        for path, content in changes.items():
            (t / "C" / path).parent.mkdir(parents=True, exist_ok=True)
            (t / "C" / path).write_text(content)
        git("-C", str(t / "C"), "add", "-A")
        git("-C", str(t / "C"), "commit", "-qm", f"{name}: change {', '.join(changes)}")
        git("-C", str(t / "C"), "update-ref", f"refs/remotes/sandbox/{name}", "HEAD")
        head = git("-C", str(t / "C"), "rev-parse", "HEAD")
        export = json.loads((t / "workers.json").read_text())
        export["workers"].append({"name": name, "state": "exited", "limited": None, "model": "opus", "head": head,
                                  "commits": 1, "ref": f"refs/remotes/sandbox/{name}",
                                  "result": {"subtype": "success", "text": f"{name} done", "turns": 9}})
        (t / "workers.json").write_text(json.dumps(export))
        return head

    def test_code_and_data_go_out_as_two_prs(self):
        head = self.add_worker("mixed", {"src/a.cpp": "int a = 2;\n", "config/BW1W120/symbols.txt": "a = 1;\n"})
        self.add_worker("stray", {"src/a.cpp": "int a = 3;\n", "docs/notes.md": "x\n"})
        pub = P.Publisher(self.cfg(), dry_run=True)
        pub.guards()
        pub.cmd_publish()
        log = (self.t / "pub.log").read_text()
        self.assertIn(f"-mixed-{head[:7]}-code", log)
        self.assertIn(f"-mixed-{head[:7]}-data", log)
        self.assertIn("skip stray: changes outside", log)
        # Each part is one commit on upstream main holding only its own files
        git("-C", str(self.t / "P.git"), "fetch", "-q", str(self.t / "C"), "+refs/remotes/sandbox/mixed:refs/incoming/mixed")
        base = git("-C", str(self.t / "P.git"), "rev-parse", "refs/remotes/origin/main")
        code = pub.part_commit(base, head, ["src/a.cpp"], "mixed", "code", ["abc mixed"])
        data = pub.part_commit(base, head, ["config/BW1W120/symbols.txt"], "mixed", "data", ["abc mixed"])
        names = lambda c: git("-C", str(self.t / "P.git"), "diff", "--name-only", base, c).split()
        self.assertEqual(names(code), ["src/a.cpp"])
        self.assertEqual(names(data), ["config/BW1W120/symbols.txt"])
        self.assertEqual(git("-C", str(self.t / "P.git"), "rev-parse", f"{code}^"), base)

    def test_a_branch_adding_extern_is_refused(self):
        self.add_worker("ext", {"src/a.cpp": "// extern in a comment is fine\nextern int g_counter;\n"})
        self.add_worker("clean", {"src/b.cpp": "// no extern here, only in this comment\nint b = 1;\n"})
        pub = P.Publisher(self.cfg(), dry_run=True)
        pub.guards()
        pub.cmd_publish()
        log = (self.t / "pub.log").read_text()
        self.assertIn("REFUSED ext: adds `extern`", log)
        self.assertIn("src/a.cpp: extern int g_counter;", log)
        self.assertNotIn("REFUSED clean", log)

    def test_part_commits_are_by_the_configured_author_with_the_trailer(self):
        head = self.add_worker("mixed", {"src/a.cpp": "int a = 2;\n", "config/BW1W120/symbols.txt": "a = 1;\n"})
        pub = P.Publisher(self.cfg(COMMIT_AUTHOR_NAME="Someone", COMMIT_AUTHOR_EMAIL="someone@example.com"),
                          dry_run=True)
        pub.guards()
        git("-C", str(self.t / "P.git"), "fetch", "-q", str(self.t / "C"), "+refs/remotes/sandbox/mixed:refs/incoming/mixed")
        base = git("-C", str(self.t / "P.git"), "rev-parse", "refs/remotes/origin/main")
        commit = pub.part_commit(base, head, ["src/a.cpp"], "mixed", "code", ["abc mixed"])
        show = lambda f: git("-C", str(self.t / "P.git"), "log", "-1", f"--format={f}", commit)
        self.assertEqual(show("%an <%ae>"), "Someone <someone@example.com>")
        self.assertEqual(show("%cn <%ce>"), "Someone <someone@example.com>")
        self.assertIn("Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", show("%B"))

    def test_publish_refuses_push_to_openblack(self):
        git("-C", str(self.t / "P.git"), "remote", "set-url", "--push", "fork", f"https://github.com/{UP}.git")
        pub = P.Publisher(self.cfg(PR_TARGET="openblack", PR_REPO=UP), dry_run=False)
        pub.guards()
        pub.cmd_publish()
        log = (self.t / "pub.log").read_text()
        self.assertIn("ERROR publishing good: push URL", log)
        self.assertFalse([c for c in self.gh_calls() if c["args"][:2] == ["pr", "create"]])  # no PR without a push

    def test_guards_refuse_openblack_fork_and_mismatched_pr_repo(self):
        with self.assertRaises(SystemExit):
            P.Publisher(self.cfg(FORK_REPO=UP), dry_run=True).guards()
        with self.assertRaises(SystemExit):
            P.Publisher(self.cfg(PR_TARGET="openblack", PR_REPO=FORK), dry_run=True).guards()
        with self.assertRaises(SystemExit):  # openblack target needs its own credential in a real run
            (self.t / "openblack.env").write_text("")
            P.Publisher(self.cfg(PR_TARGET="openblack"), dry_run=False).guards()

    def test_comments_dry_run(self):
        pub = P.Publisher(self.cfg(FEEDBACK_TO_SANDBOX="true", FEEDBACK_AUTHORS="bwrsandman"), dry_run=True)
        pub.guards()
        pub.cmd_comments()
        log = (self.t / "pub.log").read_text()
        self.assertIn("new comment on openblack/bw1-decomp#7 by bwrsandman", log)
        self.assertIn("new review on openblack/bw1-decomp#7", log)
        self.assertIn("CI {'status': 'pending'", log)
        self.assertNotIn("#99", log)  # not a harness PR: never read
        apis = [c["args"][1] for c in self.gh_calls() if c["args"][0] == "api"]
        self.assertTrue(apis and all("/7/" in a or "/commits/" in a or "/actions/" in a for a in apis), apis)
        self.assertFalse((self.t / "comments").exists())  # dry run stores nothing

    def test_comments_store_and_dedupe(self):
        pub = P.Publisher(self.cfg(), dry_run=False)
        pub.guards()
        pub.cmd_comments()
        first = (self.t / "comments/comments.jsonl").read_text().splitlines()
        self.assertEqual(len(first), 3)
        pub2 = P.Publisher(self.cfg(), dry_run=False)
        pub2.guards()
        pub2.cmd_comments()
        self.assertEqual(len((self.t / "comments/comments.jsonl").read_text().splitlines()), 3)

    def test_reply(self):
        body = self.t / "body.md"
        body.write_text("Thanks, fixed in the next commit.")
        pub = P.Publisher(self.cfg(), dry_run=False)
        pub.guards()
        pub.cmd_reply("openblack", 7, str(body), review=False)
        posted = [c for c in self.gh_calls() if c["args"][:2] == ["pr", "comment"]]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["token"], "openblack-token-dummy")
        with self.assertRaises(P.Refused):
            pub.cmd_reply("openblack", 99, str(body), review=False)
        self.assertEqual(len([c for c in self.gh_calls() if c["args"][:2] == ["pr", "comment"]]), 1)


if __name__ == "__main__":
    unittest.main()
