#!/usr/bin/env python3
"""Offline tests for droplet/hospital.py (stdlib unittest; needs git). A fake ward stands in for the sandbox: its
workers "run" synchronously, doing whatever the test scripted, and never touch a droplet.

  python3 droplet/tests/test_hospital.py -v
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DROPLET = HERE.parent
sys.path.insert(0, str(DROPLET))
import hospital as H  # noqa: E402
import integrity  # noqa: E402

G = ["git", "-c", "user.name=t", "-c", "user.email=t@e", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
SYMBOLS = "?Real@Thing@@QAEXXZ = .text:0x00401000; // type:function size:0x10\n"
THING_H = "#ifndef BW1_DECOMP_THING_INCLUDED_H\n#define BW1_DECOMP_THING_INCLUDED_H\nstruct Thing { void Real(); };\n#endif\n"
THING_CPP = '#include "Thing.h"\n\nvoid Thing::Real()\n{\n}\n'
PLAN = "# Plan\n\n" + "\n\n".join(f"## {s}\n\nSomething for {s}." for s in H.PLAN_SECTIONS) + "\n"


def git(repo, *a):
    return subprocess.run([*G, "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def report(matched=157, total=161, real=100.0, other=50.0, text=97.0, unit="runblack/Black/Thing"):
    """An objdiff report (the parts the progress check reads) with one unit and two functions."""
    return {"measures": {}, "units": [{
        "name": unit,
        "measures": {"fuzzy_match_percent": text, "matched_functions": matched, "total_functions": total,
                     "matched_code": str(matched * 10), "total_code": str(total * 10)},
        "sections": [{"name": ".data", "fuzzy_match_percent": 100.0}, {"name": ".text", "fuzzy_match_percent": text}],
        "functions": [{"name": "?Real@Thing@@QAEXXZ", "fuzzy_match_percent": real},
                      {"name": "?Other@Thing@@QAEXXZ", "fuzzy_match_percent": other}]}]}


def review(verdict, head, blocking=(), resolved=(), drop=()):
    secs = {s: "None." for s in H.REVIEW_SECTIONS}
    secs["Checks"] = "| Check | Result | Evidence |\n|---|---|---|\n| gate | pass | 0 hard |"
    if blocking:
        secs["Blocking findings"] = "\n".join(f"[{b}] src/Thing.cpp:3: fix it" for b in blocking)
    if resolved:
        secs["Resolution of prior findings"] = "\n".join(f"[{b}] resolved: done" for b in resolved)
    body = "\n\n".join(f"## {k}\n\n{v}" for k, v in secs.items() if k not in drop)
    return f"# Review\n\nVerdict: {verdict}\nReviewed head: {head}\n\n{body}\n"


class FakeWard:
    """Workers as dicts of files; the fellow's branch is the `fellow` branch of the test's host clone."""

    def __init__(self, repo: Path, fellow: str):
        self.repo, self.fellow = repo, fellow
        self.files = {fellow: {}}
        self.calls = []
        self.on = {}  # stage kind -> fn(ward, worker, prompt)
        self.busy = set()
        self.limits = {}

    def exists(self, name):
        return name in self.files

    def running(self, name):
        return name in self.busy

    def limited(self, name):
        return self.limits.get(name)

    @staticmethod
    def kind(prompt):
        m = re.search(r"^# Hospital case \S+: (workup|treatment|plan review|code review)", prompt, re.M)
        return m.group(1).replace(" review", "") if m else "?"

    def _run(self, name, prompt):
        fn = self.on.get(self.kind(prompt))
        if fn:
            fn(self, name, prompt)

    def spawn(self, name, prompt, model="", effort=""):
        assert name not in self.files, f"spawned {name} twice"
        self.files[name] = {}
        self.calls.append(("spawn", name, prompt))
        self._run(name, prompt)

    def resume(self, name, prompt, model="", effort=""):
        assert name in self.files
        self.calls.append(("resume", name, prompt))
        self._run(name, prompt)

    def read(self, name, rel):
        return self.files.get(name, {}).get(rel)

    def write(self, name, rel, data):
        self.files[name][rel] = data

    def remove(self, name, rel):
        self.files.get(name, {}).pop(rel, None)

    def head(self, name):
        return git(self.repo, "rev-parse", "fellow")

    def start_from(self, name, sha, cid):
        old = self.head(name)
        git(self.repo, "checkout", "-q", "-B", "fellow", sha)
        self.calls.append(("start_from", name, sha))
        return old

    # helpers for the scripted workers
    def commit(self, files, message="Thing: real change"):
        for p, c in files.items():
            (self.repo / p).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / p).write_text(c)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)  # a re-treatment may repeat a change
        return self.head(self.fellow)


class HospitalTests(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="hosptest-"))
        self.repo = self.t / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        self.write({"config/BW1W120/symbols.txt": SYMBOLS, "src/Thing.h": THING_H, "src/Thing.cpp": THING_CPP,
                    "tools/decomp-diff.py": "print('base tool')\n", "configure.py": "print('base configure')\n"})
        self.base = self.commit_all("base")
        git(self.repo, "update-ref", "refs/remotes/upstream/main", self.base)
        git(self.repo, "checkout", "-q", "-b", "fellow")
        self.write({"src/Old.cpp": "int old;\n"})
        self.old = self.commit_all("first: earlier work")
        git(self.repo, "checkout", "-q", "-b", "pr", self.base)
        self.write({"config/BW1W120/symbols.txt": SYMBOLS + "merged = .text:0x2; // data merged by the user\n"})
        self.start = self.commit_all("PR head with the merged data part")
        git(self.repo, "checkout", "-q", "fellow")
        self.ward = FakeWard(self.repo, "first")
        for rel, src in integrity.WORKER_HOOKS.items():
            self.ward.files["first"][rel] = (DROPLET / src).read_bytes()
        self.cases = self.t / "cases"
        self.manifest = self.t / "integrity.json"
        self.manifest.write_text(json.dumps(integrity.build(DROPLET)))
        self.verify_calls = []
        self.verify_result = (True, "verified")
        # the reports the fake container leaves: the start at 157/161, the head improving Thing::Other
        self.reports = {"base": report(matched=10, real=40.0, other=0.0, text=20.0), "start": report(),
                        "head": report(other=80.0, text=98.0)}
        self.hosp = H.Hospital(self.ward, cases=self.cases, manifest=self.manifest, verifier=self.verifier,
                               stop_file=self.t / "STOP", root=False, require_root_files=False)
        self.hosp.format_problems = lambda *a: []
        self.script_happy()

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def write(self, files):
        for p, c in files.items():
            (self.repo / p).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / p).write_text(c)

    def commit_all(self, msg):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", msg)
        return git(self.repo, "rev-parse", "HEAD")

    def verifier(self, base, head, workdir, start=""):
        self.verify_calls.append((base, head))
        workdir = Path(workdir)
        (workdir / "base-report.json").write_text(json.dumps(self.reports["base"]))
        if start and start != base and H.cached_start(workdir) != start:
            self.start_builds = getattr(self, "start_builds", 0) + 1
            (workdir / "start-report.json").write_text(json.dumps(self.reports["start"]))
            (workdir / "start-report.sha").write_text(start + "\n")
        (workdir / "head-report.json").write_text(json.dumps(self.reports["head"]))
        return self.verify_result

    def script_happy(self):
        w = self.ward

        def workup(ward, name, prompt):
            ward.files[name]["home/case/plan.md"] = PLAN.encode()

        def treatment(ward, name, prompt):
            ward.commit({"src/Thing.cpp": THING_CPP.replace("{\n}", "{\n\tint a = 1;\n}")})

        def attending(ward, name, prompt):
            head = re.search(r"Reviewed head\"\s+(?:is the Fellow's head|must be)\s+`([0-9a-f]{40})`", prompt).group(1)
            ward.files[name]["home/case/review.md"] = review("APPROVE", head).encode()

        w.on = {"workup": workup, "treatment": treatment, "plan": attending, "code": attending}

    def triage(self, **kw):
        args = dict(cid="pr431", fellow="first", brief="bwr's 13 comments", start=self.start, hold=True,
                    units=["Thing"])
        args.update(kw)
        return self.hosp.triage(args.pop("cid"), args.pop("fellow"), args.pop("brief"), **args)

    def run_until_closed(self, cid="pr431", limit=30):
        out = []
        for _ in range(limit):
            out.append(self.hosp.advance(cid))
            if self.hosp.load(cid)["stage"] in H.CLOSED:
                break
        return out

    # --- tests ---------------------------------------------------------------------------------

    def test_triage_starts_from_the_pr_head_and_records_the_old_one(self):
        c = self.triage()
        self.assertEqual((c["base"], c["start"], c["old_head"], c["hold"]), (self.base, self.start, self.old, True))
        self.assertEqual(self.ward.head("first"), self.start)
        md = (self.cases / "pr431/case.md").read_text()
        self.assertIn("bwr's 13 comments", md)
        self.assertIn(self.old, md)
        with self.assertRaises(H.HospitalError):
            self.triage()  # exists

    def test_a_failed_start_leaves_no_case_behind(self):
        def fail(*a):
            raise RuntimeError("sudo: unknown user #2000")
        self.ward.start_from = fail
        with self.assertRaises(H.HospitalError):
            self.triage()
        self.assertFalse((self.cases / "pr431").exists())
        del self.ward.start_from
        self.assertEqual(self.triage()["old_head"], self.old)  # the triage can simply be re-run

    def test_sandbox_ward_runs_git_as_the_worker_uid_with_setpriv(self):
        calls = []

        class SB:
            git_env = None

            def need_ip(self):
                return "127.0.0.1"

            def agent(self, *a, **k):
                calls.append(("agent",) + a)

            def exec(self, argv, stdin=None):
                calls.append(tuple(argv))
                return 0, (b"" if "status" in argv else b"%s\n" % self.old.encode()), b""

        ward = object.__new__(H.SandboxWard)
        ward.sb, ward.repo = SB(), self.repo
        ward.sb.old = self.old
        orig = H.subprocess.run
        H.subprocess.run = lambda *a, **k: calls.append(("push",) + tuple(a[0][-2:])) if "push" in a[0]             else orig(*a, **k)
        try:
            self.assertEqual(ward.start_from("first", self.start, "pr431"), self.old)
        finally:
            H.subprocess.run = orig
        git_calls = [c for c in calls if c[0] == "sudo"]
        self.assertEqual(len(git_calls), 4)
        for c in git_calls:
            self.assertEqual(c[:5], ("sudo", "setpriv", "--reuid=2000", "--regid=2000", "--clear-groups"))
            self.assertNotIn("-u", c)
        self.assertIn(("push", "ssh://decomp@127.0.0.1/srv/decomp/mirror.git", f"{self.start}:refs/heads/case/pr431"),
                      calls)
        self.assertEqual(git_calls[-1][-4:], ("-q", "-B", "agent/first", "origin/case/pr431"))

    def test_full_pipeline_to_discharge(self):
        self.triage()
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "discharged", "\n".join(out))
        names = [(k, n) for k, n, _ in [x for x in self.ward.calls if x[0] != "start_from"]]
        self.assertEqual(names, [("resume", "first"), ("spawn", "att-pr431-1"), ("resume", "first"),
                                 ("spawn", "att-pr431-2")])
        head = self.ward.head("first")
        d = json.loads((self.cases / "pr431/discharge.json").read_text())
        self.assertEqual((d["head"], d["passed"]), (head, True))
        self.assertEqual(self.verify_calls, [(self.base, head)])
        self.assertIn("PASSED", (self.cases / "pr431/checklist.md").read_text())
        # The Attending's prompt: the case, the plan and the head, the /peers repo only, both checklists
        att = next(p for k, n, p in self.ward.calls if n == "att-pr431-2")
        self.assertIn(f"git fetch /peers/first/repo agent/first", att)
        self.assertIn("NEVER read `/peers/first/home`", att)
        self.assertIn(head, att)
        self.assertIn("Something for Approach", att)
        self.assertIn("bwrsandman's review of PR #431", att)
        self.assertIn("A1 | No inline assembly", att)
        # A resumed Fellow gets the harness rules in front (its session predates them); an Attending gets the
        # preamble from the sandbox's spawn instead
        fellow_prompts = [p for k, n, p in self.ward.calls if n == "first" and k == "resume"]
        self.assertTrue(all(p.startswith("# Harness rules") and "Maintainer review rules" in p for p in fellow_prompts))
        self.assertFalse(att.startswith("# Harness rules"))
        # The Fellow got its case and gate base in ~/case
        self.assertEqual(self.ward.files["first"]["home/case/gate-base"].decode().strip(), self.base)
        self.assertEqual(self.ward.files["first"]["home/case/gate-allow"].decode().strip(), self.start)

    def test_waiting_while_the_worker_runs(self):
        self.triage()
        self.hosp.advance("pr431")
        self.ward.busy.add("first")
        self.assertIn("waiting for first", self.hosp.advance("pr431"))

    def test_rejected_plans_loop_then_escalate_after_three_rework_rounds(self):
        self.triage()
        n = {"i": 0}

        def reject(ward, name, prompt):
            n["i"] += 1
            head = ward.head("first")
            ward.files[name]["home/case/review.md"] = review("REQUEST_CHANGES", head, [f"B{n['i']}"]).encode()

        self.ward.on["plan"] = reject
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "escalated", "\n".join(out))
        self.assertEqual(c["rounds"], 4)
        self.assertIn("more than 3 rework rounds", c["escalations"][-1]["why"])
        workups = [p for k, n_, p in self.ward.calls if k == "resume"]
        self.assertEqual(len(workups), 4)
        self.assertIn("Feedback to address", workups[1])  # the review went back to the Fellow
        self.assertIn("[B1]", workups[1])
        self.assertIn("Escalations waiting for the Chief", self.hosp.status())

    def test_a_stalled_stage_is_rerun_twice_then_escalates(self):
        self.triage()
        self.ward.on["workup"] = lambda *a: None  # the fellow exits without a plan
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "escalated", "\n".join(out))
        self.assertIn("stalled 3 times", c["escalations"][-1]["why"])
        resumes = [p for k, n, p in self.ward.calls if k == "resume"]
        self.assertEqual(len(resumes), 3)
        self.assertIn("re-run (attempt 2)", resumes[1])

    def test_an_incomplete_review_is_a_stall_and_a_new_attending_reruns_it(self):
        self.triage()
        first = {"done": False}
        happy = self.ward.on["plan"]

        def sloppy(ward, name, prompt):
            if not first["done"]:
                first["done"] = True
                ward.files[name]["home/case/review.md"] = review("APPROVE", ward.head("first"),
                                                                 drop=("Anti-cheat checklist",)).encode()
            else:
                happy(ward, name, prompt)

        self.ward.on["plan"] = sloppy
        self.hosp.advance("pr431")
        self.hosp.advance("pr431")
        self.assertIn("missing sections Anti-cheat checklist", self.hosp.advance("pr431"))
        self.hosp.advance("pr431")
        spawned = [n for k, n, _ in self.ward.calls if k == "spawn"]
        self.assertEqual(spawned, ["att-pr431-1", "att-pr431-2"])
        self.assertEqual(self.hosp.load("pr431")["stage"], "treatment")

    def test_handoff_escalates(self):
        self.triage()
        self.ward.on["workup"] = lambda w, n, p: w.files[n].__setitem__("home/case/handoff.md", b"# I-PASS\nstuck")
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "escalated", "\n".join(out))
        self.assertIn("I-PASS", c["escalations"][-1]["why"])
        self.assertTrue((self.cases / "pr431/handoff-1.md").exists())

    def test_a_workup_handoff_keeps_the_plan_for_the_chief(self):
        self.triage()

        def workup(w, n, p):
            w.files[n]["home/case/plan.md"] = PLAN.encode()
            w.files[n]["home/case/handoff.md"] = b"# I-PASS\nneeds a Chief ruling"
        self.ward.on["workup"] = workup
        self.run_until_closed()
        self.assertEqual(self.hosp.load("pr431")["stage"], "escalated")
        self.assertIn("Something for Approach", (self.cases / "pr431/plan-r1-handoff.md").read_text())

    def test_a_case_over_1000_lines_must_be_split(self):
        self.triage()
        self.ward.on["treatment"] = lambda w, n, p: w.commit({"src/Big.cpp": "int x;\n" * 1001})
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "escalated", "\n".join(out))
        self.assertIn("split the case", c["escalations"][-1]["why"])

    def test_gate_failure_at_discharge_goes_back_to_treatment_with_the_checklist(self):
        self.triage()
        calls = {"n": 0}

        def treat(ward, name, prompt):
            calls["n"] += 1
            if calls["n"] == 1:  # first treatment uses a field_0x member: a hard gate failure
                ward.commit({"src/Thing.cpp": THING_CPP.replace("{\n}", "{\n\tthis->field_0x10 = 1;\n}")})
            else:
                self.assertIn("Discharge checklist", prompt)
                ward.commit({"src/Thing.cpp": THING_CPP.replace("{\n}", "{\n\tthis->Count = 1;\n}")})

        self.ward.on["treatment"] = treat
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "discharged", "\n".join(out))
        self.assertEqual(c["rounds"], 1)
        self.assertEqual([d["passed"] for d in c["discharges"]], [False, True])

    def test_tamper_paths_escalate_at_discharge(self):
        self.triage()
        self.ward.on["treatment"] = lambda w, n, p: w.commit({"tools/decomp-diff.py": "print('always 100%')\n"})
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "escalated", "\n".join(out))
        self.assertIn("tampering", c["escalations"][-1]["why"])
        self.assertEqual(self.verify_calls, [])  # never verified a tampered tree

    def test_the_cases_merged_data_is_allowed_but_chief_approval_is_needed_for_more(self):
        self.triage()
        self.ward.on["treatment"] = lambda w, n, p: w.commit(
            {"config/BW1W120/symbols.txt": SYMBOLS + "merged = .text:0x2; // data merged by the user\nnew = 1;\n"})
        self.run_until_closed()
        self.assertEqual(self.hosp.load("pr431")["stage"], "escalated")
        # The Chief approves this exact path at this head and resolves: the discharge then passes
        head = self.ward.head("first")
        self.hosp._root = True
        self.hosp.allow_paths("pr431", head, ["config/BW1W120/symbols.txt"])
        self.hosp.resolve("pr431", "discharge", "symbols change approved")
        self.hosp._root = False
        self.hosp.advance("pr431")
        self.assertEqual(self.hosp.load("pr431")["stage"], "discharged")

    def test_worker_hook_tampering_escalates(self):
        self.triage()
        self.ward.files["first"]["repo/.git/hooks/pre-commit"] = b"#!/bin/sh\nexit 0\n"
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "escalated", "\n".join(out))
        cl = (self.cases / "pr431/checklist.md").read_text()
        self.assertIn("repo/.git/hooks/pre-commit differs", cl)

    def test_integrity_mismatch_refuses_and_escalates(self):
        self.triage()
        m = json.loads(self.manifest.read_text())
        m["files"][next(iter(m["files"]))] = "0" * 64
        self.manifest.write_text(json.dumps(m))
        self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "escalated")
        d = json.loads((self.cases / "pr431/discharge.json").read_text())
        self.assertFalse(d["passed"])
        self.assertIn("changed since", json.dumps(d))

    def test_unresolved_blocking_findings_fail_discharge(self):
        self.triage()
        n = {"code": 0}

        def code_review(ward, name, prompt):
            n["code"] += 1
            head = ward.head("first")
            if n["code"] == 1:
                text = review("REQUEST_CHANGES", head, ["B1", "B2"])
            elif n["code"] == 2:
                text = review("APPROVE", head, resolved=["B1"])  # forgets B2
            else:
                text = review("APPROVE", head, resolved=["B1", "B2"])
            ward.files[name]["home/case/review.md"] = text.encode()

        self.ward.on["code"] = code_review
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "discharged", "\n".join(out))
        self.assertEqual([d["passed"] for d in c["discharges"]], [False, True])

    def test_snapshot_commits_and_failed_verification_fail_discharge(self):
        self.triage()
        self.ward.on["treatment"] = lambda w, n, p: w.commit({"src/Thing.cpp": THING_CPP + "\n"},
                                                             "sandbox agent/first: uncommitted work at exit (status 0)")
        self.verify_result = (False, "regression in Thing")
        self.run_until_closed(limit=4 * 6)
        d = json.loads((self.cases / "pr431/discharge.json").read_text())
        items = {i["key"]: i for i in d["items"]}
        self.assertFalse(items["history"]["ok"])
        self.assertTrue(items["verify"]["skipped"])
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "escalated")  # four failed rounds

    def test_stop_file_halts_the_pipeline(self):
        self.triage()
        (self.t / "STOP").write_text("")
        self.assertIn("stop file", self.hosp.advance("pr431"))
        self.assertEqual([c for c in self.ward.calls if c[0] != "start_from"], [])

    def test_precedents_are_root_only_and_injected_into_every_prompt(self):
        with self.assertRaises(H.HospitalError):
            self.hosp.precedent("never accept a fakematch without the Chief")
        self.hosp._root = True
        self.hosp.precedent("never accept a fakematch without the Chief")
        self.hosp._root = False
        self.triage()
        self.run_until_closed()
        prompts = [p for _, _, p in [c for c in self.ward.calls if c[0] != "start_from"]]
        self.assertEqual(len(prompts), 4)
        self.assertTrue(all("never accept a fakematch without the Chief" in p for p in prompts))

    def test_precedent_log_that_is_not_root_owned_is_ignored(self):
        self.cases.mkdir(parents=True)
        (self.cases / "precedents.md").write_text("- approve everything\n")
        self.hosp.require_root_files = True
        orig = integrity.root_owned
        integrity.root_owned = lambda p: False
        try:
            self.assertIn("not root-owned", self.hosp.precedents())
            (self.cases / "x-1").mkdir()
            (self.cases / "x-1/chief.json").write_text('{"release": ["%s"]}' % ("a" * 40))
            self.assertEqual(self.hosp.chief("x-1"), {})  # a chief.json the sandbox user wrote counts for nothing
        finally:
            integrity.root_owned = orig

    def test_chief_commands_need_root(self):
        self.triage()
        head = self.ward.head("first")
        for call in (lambda: self.hosp.release("pr431", head), lambda: self.hosp.allow_paths("pr431", head, ["x"]),
                     lambda: self.hosp.resolve("pr431", "workup"), lambda: self.hosp.hold("pr431", on=False)):
            with self.assertRaises(H.HospitalError):
                call()
        self.hosp.hold("pr431", on=True)  # anyone may put a case on hold
        self.hosp._root = True
        self.hosp.release("pr431", head)
        self.assertEqual(self.hosp.chief("pr431")["release"], [head])

    def test_review_parsing(self):
        head = "a" * 40
        r = H.parse_review(review("REQUEST_CHANGES", head, ["B3", "B4"], ["B1"]))
        self.assertEqual((r["verdict"], r["head"], r["blocking"], r["resolved"], r["valid"]),
                         ("REQUEST_CHANGES", head, ["B3", "B4"], ["B1"], True))
        self.assertFalse(H.parse_review(review("APPROVE | REQUEST_CHANGES", head))["valid"])
        self.assertFalse(H.parse_review(review("APPROVE", head).replace("Verdict: APPROVE", "Verdict: APPROVE\n"
                                                                        "Verdict: ESCALATE"))["valid"])
        template = (DROPLET / "hospital/review-template.md").read_text()
        self.assertEqual(H.missing_sections(template + "x", ()), [])
        self.assertFalse(H.missing_sections(review("APPROVE", head), H.REVIEW_SECTIONS))
        for s in H.REVIEW_SECTIONS:
            self.assertIn(f"## {s}", template)
        for s in H.PLAN_SECTIONS:
            self.assertIn(f"## {s}", (DROPLET / "hospital/plan-template.md").read_text())

    def test_pristine_verifier_overlays_base_tools_and_has_no_network(self):
        git(self.repo, "checkout", "-q", "fellow")
        head = self.ward.commit({"tools/decomp-diff.py": "print('tampered')\n", "configure.py": "print('x')\n",
                                 "src/Thing.cpp": THING_CPP + "// head\n"})
        work = self.t / "work"
        work.mkdir()
        v = H.PristineVerifier(docker=None, repo=self.repo, setup=["python3", "-c", "pass"])
        v.prepare(self.base, head, work)
        self.assertEqual((work / "head/tools/decomp-diff.py").read_text(), "print('base tool')\n")
        self.assertEqual((work / "head/configure.py").read_text(), "print('base configure')\n")
        self.assertIn("// head", (work / "head/src/Thing.cpp").read_text())
        self.assertNotIn("// head", (work / "tree/src/Thing.cpp").read_text())
        args = v.container_args(work)
        self.assertEqual(args["network_mode"], "none")
        self.assertEqual(args["cap_drop"], ["ALL"])
        self.assertTrue(args["read_only"])
        ro = {v_["bind"] for k, v_ in args["volumes"].items() if v_["mode"] == "ro"}
        self.assertEqual(ro, {"/verify/verify.sh", "/work/tree/build/tools", "/work/tree/build/compilers", "/opt/orig"})
        self.assertEqual(json.loads(args["environment"]["WORKER_SETUP"]), ["python3", "-c", "pass"])

    def test_pristine_verifier_runs_the_container_and_reads_its_result(self):
        work = self.t / "work"
        work.mkdir()

        class Container:
            def __init__(self, code):
                self.code = code

            def wait(self, timeout):
                (work / "verify.json").write_text(json.dumps({"baseline": 0, "head_build": self.code, "regress": 0}))
                return {"StatusCode": self.code}

            def logs(self, **k):
                return b"log"

            def remove(self, force):
                pass

        class Docker:
            def __init__(self, code):
                self.containers, self.code, self.args = self, code, None

            def run(self, image, **args):
                self.args = (image, args)
                return Container(self.code)

        for code, ok in ((0, True), (1, False)):
            d = Docker(code)
            v = H.PristineVerifier(d, self.repo, setup=[])
            passed, detail = v(self.base, self.start, work)
            self.assertEqual(passed, ok, detail)
            self.assertEqual(d.args[0], "bw1-worker")
            self.assertEqual(d.args[1]["network_mode"], "none")

    def test_format_check_reports_a_missing_formatter(self):
        hosp = H.Hospital(self.ward, cases=self.cases, clang_format=str(self.t / "no-clang-format"))
        git(self.repo, "checkout", "-q", "fellow")
        head = self.ward.commit({"src/Thing.cpp": THING_CPP + "int  x;\n"})
        problems = hosp.format_problems(self.repo, self.base, head, self.t)
        self.assertTrue(problems and "not found" in problems[0])

    def test_charge_advances_every_open_case(self):
        self.triage()
        self.triage(cid="other")
        out = self.hosp.charge()
        self.assertEqual(len(out), 2)

    def test_a_usage_limit_is_waited_out_not_counted_as_a_stall(self):
        import time as _time
        self.triage()
        self.ward.on["workup"] = lambda w, n, p: None  # the run ends on the limit, with no plan
        self.hosp.advance("pr431")
        self.ward.limits["first"] = {"resetsAt": int(_time.time()) + 3600}
        for _ in range(5):
            self.assertIn("stopped on the usage limit", self.hosp.advance("pr431"))
        c = self.hosp.load("pr431")
        self.assertEqual((c["stage"], c.get("rerun", 0)), ("workup", 0))
        # well past the reset and nobody resumed it: the hospital continues the same worker (no new stall)
        self.ward.limits["first"] = {"resetsAt": int(_time.time()) - 3600}
        self.ward.on["workup"] = lambda w, n, p: w.files[n].__setitem__("home/case/plan.md", PLAN.encode())
        self.assertIn("continued first", self.hosp.advance("pr431"))
        self.assertEqual(self.ward.calls[-1][2], H.CONTINUE_PROMPT)
        self.ward.limits.pop("first")
        self.ward.files["first"]["home/case/plan.md"] = PLAN.encode()  # the continued run wrote its plan
        self.assertIn("to plan review", self.hosp.advance("pr431"))

    def test_status_survives_a_ward_that_cannot_reach_the_sandbox(self):
        self.triage()
        self.hosp.advance("pr431")  # a worker is now recorded as running

        def broken(name):
            raise RuntimeError("ssh to VM failed: no key for root")
        self.ward.running = broken
        self.assertIn("first ?", self.hosp.status())


    # --- progress (the Chief, 2026-10-12: discharge needs progress, else the Chief decides) ----------------

    def test_every_prompt_carries_the_accepted_floors(self):
        self.triage()
        self.hosp._root = True
        self.hosp.accept_floor("pr431", "Thing", "section:.bss", "97.20", "R13")
        self.hosp.accept_floor("pr431", "Thing", "section:.bss", "96.26", "R7 ruling")  # the later one counts
        self.hosp._root = False
        self.run_until_closed()
        prompts = [p for k, n, p in self.ward.calls if k in ("spawn", "resume")]
        self.assertTrue(prompts)
        for p in prompts:
            self.assertIn("- Thing section:.bss >= 96.26 (R7 ruling)", p)
            self.assertNotIn(">= 97.20", p)

    def test_discharge_records_progress_and_measures_the_start_once(self):
        self.triage(floors={"Thing": 157})
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "discharged", "\n".join(out))
        d = json.loads((self.cases / "pr431/discharge.json").read_text())
        items = {i["key"]: i for i in d["items"]}
        self.assertTrue(items["progress"]["ok"], items["progress"])
        self.assertEqual(d["progress"]["units"]["Thing"]["start"]["matched_functions"], 157)
        self.assertEqual(d["progress"]["improved"], ["Thing: ?Other@Thing@@QAEXXZ 50.00% -> 80.00%"])
        self.assertEqual(c["baseline"]["Thing"]["matched_functions"], 157)
        self.assertEqual(c["baseline"]["start"], self.start)
        md = (self.cases / "pr431/checklist.md").read_text()
        self.assertIn("157/161 -> 157/161", md)
        self.assertIn("Floor (matched functions): Thing >= 157", (self.cases / "pr431/case.md").read_text()
                      .replace("Progress floor (matched functions)", "Floor (matched functions)"))
        self.assertEqual(self.start_builds, 1)
        # a second discharge at the same start reuses the cached start report
        c["stage"] = "discharge"
        self.hosp.discharge_case(c)
        self.assertEqual(self.start_builds, 1)

    def assert_no_progress_escalates(self, why):
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "escalated", "\n".join(out))
        self.assertIn("no progress", c["escalations"][-1]["why"])
        self.assertEqual(c["rounds"], 0)  # straight to the Chief, not back to treatment
        d = json.loads((self.cases / "pr431/discharge.json").read_text())
        items = {i["key"]: i for i in d["items"]}
        self.assertFalse(d["passed"])
        self.assertTrue(items["verify"]["ok"])
        self.assertTrue(any(why in p for p in items["progress"]["problems"]), items["progress"]["problems"])

    def test_no_improved_function_escalates(self):
        self.triage()
        self.reports["head"] = report()  # same as the start
        self.assert_no_progress_escalates("no target function improved")

    def test_a_unit_below_its_start_escalates(self):
        self.triage()
        self.reports["head"] = report(matched=156, other=90.0)  # one function better, one match lost
        self.assert_no_progress_escalates("matched_functions 156 < 157")

    def test_a_lower_text_score_escalates(self):
        self.triage()
        self.reports["head"] = report(other=80.0, text=96.0)
        self.assert_no_progress_escalates("section:.text 96.0000% < 97.0000%")

    def test_a_unit_below_its_floor_escalates(self):
        self.triage(floors={"Thing": 158})
        self.assert_no_progress_escalates("below the triage floor 158")

    def test_named_target_functions_must_improve(self):
        self.triage(functions=["Thing::Real"])  # Other improves, Real does not
        self.assert_no_progress_escalates("improved from the start (functions Thing::Real)")

    def test_a_case_without_units_cannot_discharge(self):
        self.triage(units=[])
        self.assert_no_progress_escalates("lists no units")

    def test_a_header_only_unit_is_noted_not_failed(self):
        self.triage(units=["Thing", "InfluenceCircle"])  # InfluenceCircle has no objdiff unit: header only
        out = self.run_until_closed()
        self.assertEqual(self.hosp.load("pr431")["stage"], "discharged", "\n".join(out))
        d = json.loads((self.cases / "pr431/discharge.json").read_text())
        self.assertEqual(d["progress"]["no_unit"], ["InfluenceCircle"])
        # a unit in only one of the two reports is still a problem
        problems, _ = H.progress({"units": ["Thing"]}, self.reports["start"], {"units": []})
        self.assertIn("Thing: not in the head report", problems)

    def test_a_section_below_its_start_escalates(self):
        self.triage()
        self.reports["head"] = report(other=80.0, text=98.0)
        self.reports["head"]["units"][0]["sections"][0]["fuzzy_match_percent"] = 99.0  # .data 100 -> 99
        self.assert_no_progress_escalates("section:.data 99.0000% < 100.0000%")

    def test_chief_floor_overrides_accept_a_measured_loss(self):
        self.triage()
        # a loss the Chief accepted: two matches and the .data score; something else still improves
        self.reports["head"] = report(matched=155, other=80.0, text=97.0)
        self.reports["head"]["units"][0]["sections"][0]["fuzzy_match_percent"] = 99.33
        with self.assertRaises(H.HospitalError):
            self.hosp.accept_floor("pr431", "Thing", "matched_functions", "155", "pr431 R13")  # root only
        self.hosp._root = True
        for metric, value in (("matched_functions", "155.5"), ("bogus", "1"), ("fuzzy", "x")):
            with self.assertRaises(H.HospitalError):
                self.hosp.accept_floor("pr431", "Thing", metric, value, "pr431 R13")
        with self.assertRaises(H.HospitalError):
            self.hosp.accept_floor("pr431", "Thing", "fuzzy", "1", " ")  # a ruling needs a reference
        self.hosp.accept_floor("pr431", "Thing", "matched_functions", "155", "pr431 R13, Chief 2026-10-12")
        self.hosp.accept_floor("pr431", "Thing", "matched_code", "start-20", "pr431 R13, Chief 2026-10-12")
        self.hosp.accept_floor("pr431", "Thing", "section:.data", "99.33", "pr431 rdata ruling")
        self.hosp._root = False
        self.assertIn("accepted floor: Thing section:.data >= 99.33 (pr431 rdata ruling)", self.hosp.status("pr431"))
        out = self.run_until_closed()
        c = self.hosp.load("pr431")
        self.assertEqual(c["stage"], "discharged", "\n".join(out))
        d = json.loads((self.cases / "pr431/discharge.json").read_text())
        self.assertEqual(d["progress"]["units"]["Thing"]["overrides"]["matched_functions"]["value"], "155")
        md = (self.cases / "pr431/checklist.md").read_text()
        self.assertIn("The Chief's accepted floors", md)
        self.assertIn("- Thing: section:.data >= 99.33 (pr431 rdata ruling)", md)
        self.assertTrue(any("Chief accepted floor" in line for line in
                            (self.cases / "pr431/events.jsonl").read_text().splitlines()))

    def test_an_accepted_floor_still_bounds_the_loss(self):
        self.triage()
        self.hosp._root = True
        self.hosp.accept_floor("pr431", "Thing", "matched_functions", "156", "pr431 R13")
        self.hosp._root = False
        self.reports["head"] = report(matched=155, other=80.0)  # one more lost than the Chief accepted
        self.assert_no_progress_escalates("matched_functions 155 < 156, the Chief's accepted floor 156")

    def test_the_chief_can_waive_the_improvement_requirement(self):
        self.triage()
        self.reports["head"] = report()  # nothing improved
        self.hosp._root = True
        self.hosp.accept_floor("pr431", "Thing", "improved_functions", "0", "pr431 cleanup-only ruling")
        self.hosp._root = False
        out = self.run_until_closed()
        self.assertEqual(self.hosp.load("pr431")["stage"], "discharged", "\n".join(out))
        self.assertIn("waived by the Chief (pr431 cleanup-only ruling)",
                      (self.cases / "pr431/checklist.md").read_text())

    def test_a_queued_case_is_listed_but_never_run(self):
        self.triage()
        self.hosp.queue("pr431-incl", "Restructure the includes.", "CitadelHeart includes", after="pr431")
        self.assertIn("pr431-incl", self.hosp.status())
        text = (self.cases / "queue/pr431-incl.md").read_text()
        self.assertIn("After: case `pr431`", text)
        out = self.hosp.charge()
        self.assertEqual(len(out), 1)  # only pr431 advances
        self.assertFalse((self.cases / "pr431-incl").exists())
        with self.assertRaises(H.HospitalError):
            self.hosp.queue("pr431-incl", "again")

    def other(self, rep, name, matched, sections=None):
        rep["units"].append({"name": name, "measures": {"fuzzy_match_percent": float(matched),
                                                        "matched_functions": matched, "matched_code": str(matched)},
                             "sections": [{"name": ".text", "fuzzy_match_percent": float(matched)}],
                             "functions": []})

    def test_regressions_beyond_the_start_send_the_case_back(self):
        self.triage()
        for k in ("base", "start"):
            self.other(self.reports[k], "runblack/Black/Other", 10)
        self.other(self.reports["head"], "runblack/Black/Other", 9)
        out = []
        while not (self.cases / "pr431/discharge.json").exists() and len(out) < 12:
            out.append(self.hosp.advance("pr431"))
        d = json.loads((self.cases / "pr431/discharge.json").read_text())
        items = {i["key"]: i for i in d["items"]}
        self.assertFalse(items["regress"]["ok"])
        self.assertIn("runblack/Black/Other: worse at the head", items["regress"]["problems"][0])
        self.assertEqual(self.hosp.load("pr431")["stage"], "treatment", "\n".join(out))

    def test_what_the_start_already_changed_is_not_a_regression(self):
        self.triage()
        # a gap unit the start's merged config split away, and a unit the start already lowered
        self.other(self.reports["base"], "runblack/fn_0083F270", 1)
        self.other(self.reports["base"], "runblack/Black/Merged", 10)
        self.other(self.reports["start"], "runblack/Black/Merged", 8)
        self.other(self.reports["head"], "runblack/Black/Merged", 8)
        out = self.run_until_closed()
        self.assertEqual(self.hosp.load("pr431")["stage"], "discharged", "\n".join(out))
        d = json.loads((self.cases / "pr431/discharge.json").read_text())
        self.assertTrue({i["key"]: i for i in d["items"]}["regress"]["ok"])
        # but a unit the head drops that the start still had is a regression
        gone = H.regressions(self.reports["base"], self.reports["base"], self.reports["start"], ["Thing"])
        self.assertIn("runblack/fn_0083F270: removed at the head", gone)

    def test_progress_function_matching(self):
        self.assertTrue(H.function_targets(["Thing::Real"], "?Real@Thing@@QAEXXZ"))
        self.assertTrue(H.function_targets(["Outer::Thing::Real"], "?Real@Thing@Outer@@QAEXXZ"))
        self.assertFalse(H.function_targets(["Thing::Real"], "?Really@Thing@@QAEXXZ"))
        self.assertTrue(H.function_targets(["CFunc"], "_CFunc"))
        self.assertTrue(H.function_targets(["Free"], "?Free@@YAXXZ"))

    def test_pristine_verifier_is_capped_and_measures_the_start(self):
        work = self.t / "work"
        work.mkdir()
        v = H.PristineVerifier(docker=None, repo=self.repo, setup=[])
        args = v.container_args(work)
        self.assertEqual(args["nano_cpus"], 4 * 10 ** 9)
        self.assertEqual((args["mem_limit"], args["memswap_limit"]), ("5g", "5g"))
        v2 = H.PristineVerifier(docker=None, repo=self.repo, setup=[], cpus=2.5, memory="3g")
        self.assertEqual(v2.container_args(work)["nano_cpus"], 2_500_000_000)
        v.prepare(self.base, self.start, work, start=self.old)
        self.assertTrue((work / "start/src/Old.cpp").is_file())
        self.assertEqual((work / "start/tools/decomp-diff.py").read_text(), "print('base tool')\n")
        v.prepare(self.base, self.start, work)
        self.assertFalse((work / "start").exists())
        sh = (DROPLET / "hospital/verify.sh").read_text()
        for want in ("/work/base-report.json", "/work/start-report.json", "/work/head-report.json",
                     "--fail-on-regression"):
            self.assertIn(want, sh)


class AttendingIsolationTests(unittest.TestCase):
    """sandbox_droplet.patch on a fake bw1-ai-sandbox: an Attending's container gets only /peers/<fellow>/repo."""

    def setUp(self):
        import sandbox_droplet
        self.SD = sandbox_droplet
        self.t = Path(tempfile.mkdtemp(prefix="isotest-"))
        (self.t / "pr431").mkdir()
        (self.t / "pr431/case.json").write_text(json.dumps({"id": "pr431", "fellow": "first"}))
        self.old_cases, sandbox_droplet.CASES = sandbox_droplet.CASES, self.t
        self.runs = []
        runs = self.runs

        class Containers:
            def run(self, image, **kw):
                runs.append(kw)

            def list(self, **kw):
                return []

        class Docker:
            containers = Containers()

        class SandboxError(Exception):
            pass

        class Sandbox:  # the upstream shape: _launch mounts WORKERS at /peers through the docker property
            @property
            def docker(self):
                return Docker()

            def _launch(self, name, prompt, model, effort, resume, mode=""):
                w = f"/srv/decomp/workers/{name}"
                self.docker.containers.run("bw1-worker", name=f"w-{name}", volumes={
                    f"{w}/repo": {"bind": "/work", "mode": "rw"},
                    f"{w}/home": {"bind": "/home/agent", "mode": "rw"},
                    "/srv/decomp/workers": {"bind": "/peers", "mode": "ro"},
                    "/srv/decomp/shared/data": {"bind": "/opt/orig", "mode": "ro"}})
                return "ok"

            def spawn(self, name, prompt, model="", effort=""):
                return self._launch(name, prompt, model, effort, resume=False)

            def resume(self, name, prompt, model="", effort="", mode=""):
                return self._launch(name, prompt, model, effort, resume=True, mode=mode)

        class Mod:
            pass

        self.mod = Mod()
        self.mod.Sandbox, self.mod.SandboxError, self.mod.WORKERS = Sandbox, SandboxError, "/srv/decomp/workers"
        sandbox_droplet.patch(self.mod)
        sandbox_droplet.patch(self.mod)  # idempotent: patching twice must not wrap twice
        self.sb = Sandbox()

    def tearDown(self):
        self.SD.CASES = self.old_cases
        shutil.rmtree(self.t, ignore_errors=True)

    def peers(self):
        return {k: v for k, v in self.runs[-1]["volumes"].items() if v["bind"].startswith("/peers")}

    def test_attending_sees_only_the_fellows_repo(self):
        for call in (lambda: self.sb.spawn("att-pr431-1", "review"),
                     lambda: self.sb.resume("att-pr431-1", "go on", mode="auto")):  # e.g. autoresume
            call()
            self.assertEqual(self.peers(), {"/srv/decomp/workers/first/repo": {"bind": "/peers/first/repo",
                                                                               "mode": "ro"}})
            vols = self.runs[-1]["volumes"]
            self.assertNotIn("/srv/decomp/workers", vols)
            self.assertNotIn("/srv/decomp/workers/first/home", vols)
            self.assertEqual(vols["/srv/decomp/workers/att-pr431-1/home"]["bind"], "/home/agent")  # its own home
            self.assertIn("/srv/decomp/shared/data", vols)

    def test_other_workers_keep_the_upstream_mounts(self):
        self.sb.spawn("first", "work")
        self.assertEqual(self.peers(), {"/srv/decomp/workers": {"bind": "/peers", "mode": "ro"}})
        self.assertIsNone(getattr(self.sb, "_bw1_peer_scope", None))

    def test_unknown_case_mounts_no_peers(self):
        self.sb.spawn("att-nocase-1", "review")
        self.assertEqual(self.peers(), {})
        (self.t / "pr431/case.json").write_text(json.dumps({"id": "pr431", "fellow": "../../etc"}))
        self.sb.spawn("att-pr431-2", "review")
        self.assertEqual(self.peers(), {})

    def test_attending_name_parsing(self):
        self.assertEqual(self.SD.attending_fellow("att-pr431-12"), (True, "first"))
        self.assertEqual(self.SD.attending_fellow("att-pr431"), (False, ""))
        self.assertEqual(self.SD.attending_fellow("first"), (False, ""))


class FilesTests(unittest.TestCase):
    def test_charge_timer_is_installed_disabled(self):
        timer = (DROPLET / "systemd/bw1-hospital-charge.timer").read_text()
        service = (DROPLET / "systemd/bw1-hospital-charge.service").read_text()
        self.assertIn("hospital.py charge", service)
        self.assertIn("ConditionPathExists=!/etc/bw1-sandbox/STOP", service)
        self.assertIn("OnUnitInactiveSec", timer)
        setup = (DROPLET / "setup-droplet.sh").read_text()
        self.assertIn("bw1-hospital-charge.timer", setup)
        code = "\n".join(line for line in setup.splitlines() if not line.lstrip().startswith("#"))
        self.assertIsNone(re.search(r"enable[^\n]*bw1-hospital-charge", code))

    def test_integrity_manifest_covers_the_gate_hospital_hooks_and_prompts(self):
        m = integrity.build(DROPLET)
        names = {Path(p).relative_to(DROPLET).as_posix() for p in m["files"]}
        for want in ("quality_gate.py", "hospital.py", "hooks/pre-commit", "hooks/stop_gate.py",
                     "hospital/attending-plan.md", "hospital/attending-code.md", "hospital/verify.sh",
                     "hospital/review-template.md"):
            self.assertIn(want, names)
        self.assertIn("repo/.git/hooks/pre-commit", m["worker_hooks"])


if __name__ == "__main__":
    unittest.main()
