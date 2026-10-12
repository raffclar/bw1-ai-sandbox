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

    def exists(self, name):
        return name in self.files

    def running(self, name):
        return name in self.busy

    @staticmethod
    def kind(prompt):
        m = re.match(r"# Hospital case \S+: (workup|treatment|plan review|code review)", prompt)
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

    def verifier(self, base, head, workdir):
        self.verify_calls.append((base, head))
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
        args = dict(cid="pr431", fellow="first", brief="bwr's 13 comments", start=self.start, hold=True)
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
                (work / "verify.json").write_text(json.dumps({"baseline": 0, "head_build": 0, "regress": self.code}))
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
