#!/usr/bin/env python3
"""The "teaching hospital": a reviewed pipeline for one bw1-decomp change (a case), run on the droplet.

Roles
  Triage            `hospital.py triage` writes the case: case.md (units or functions, impact and difficulty grades,
                    brief, constraints) and case.json (stage, rounds, fellow, base, start, hold).
  Fellow            an existing (or new) sandbox worker. WORKUP: it reproduces the current state and writes
                    ~/case/plan.md from the plan template, and no code. TREATMENT: it implements the approved plan
                    and commits. When stuck it writes an I-PASS ~/case/handoff.md and stops (the case escalates).
  Attending         a NEW worker each round, att-<case>-<n>, so it never shares the Fellow's session or context. It
                    gets the case, the plan and the head sha in its prompt, fetches the Fellow's commits with
                    `git fetch /peers/<fellow>/repo agent/<fellow>` and writes ~/case/review.md from the fixed review
                    template (verdict, checks, [B#] blocking findings, resolution of prior findings, plan
                    conformance, match verification with a revert check, bwrsandman's 13-rule checklist and the
                    anti-cheat checklist).
                    Isolation: upstream bw1-ai-sandbox mounts ALL worker folders at /peers. sandbox_droplet.py patches
                    the launch so a worker named att-<case>-<n> gets only /peers/<fellow>/repo (read-only): never the
                    Fellow's home (transcript, notes) or any other worker. An unreadable case mounts no peers at all.
  Discharge Nurse   deterministic, no LLM (`discharge`): integrity manifest, approved plan, templates complete, last
                    code review APPROVE on this head with no unresolved [B#], quality gate green (host, git objects
                    only), clang-format 22 clean, clean history (no merges, sandbox snapshots, fixup!/WIP; head ==
                    reviewed head), case delta <= 1000 lines, the worker's hooks match the manifest, and pristine
                    verification in a throwaway bw1-worker container with --network none, capped at DISCHARGE_CPUS
                    (default 4) and DISCHARGE_MEMORY (default 5g) so running workers are never starved: base tree +
                    setup + `ninja baseline`; the case's start (measured once per start, then cached) and the head,
                    each with tools/ and configure.py overlaid from the base, objects deleted, `ninja all_source` and
                    decomp-regress.py --refresh (its exit code is only recorded). REGRESSIONS are judged on the
                    host from the three reports: no unit outside the case worse at the head than at the base unless
                    the start already was (config the user merged into the start), and none worse than at the
                    start. Then PROGRESS: each of the case's
                    units at or above its start (matched functions, matched code, fuzzy and every section's score)
                    and its triage floor (--floor UNIT=N matched functions), and at least one target function (the
                    case's functions, else any function of its units) scoring higher than at the start. A Chief floor
                    override (accept-floor) replaces the start as the floor for one unit and metric. No progress
                    ESCALATES to the Chief rather than going back to treatment. Writes checklist.md and
                    discharge.json (head, passed), which the publisher requires.
  Chief             the user, as root: `status` lists escalations; `precedent` appends to the root-owned precedent
                    log (injected into every prompt); `release` lets the publisher publish one head of a held case;
                    `allow-paths` approves protected-path changes for one head; `resolve` moves an escalated case on.

STOP files
  /etc/bw1-sandbox/STOP halts everything: the publisher units and the hospital (advance/charge).
  /etc/bw1-sandbox/STOP-PUBLISH halts only the publisher (both its units and publisher.py itself), so the hospital
  can run its stages while publishing stays off.

Loops and circuit breaker
  plan rejected -> workup; changes requested or discharge failed -> treatment; more than 3 rework rounds -> escalate.
  A stalled stage (the worker exited without its artifact) is re-run by `advance`/`charge` up to 2 times, then
  escalates. A run that ended on the account's usage limit is not a stall: `advance` waits for the reset (the
  sandbox's autoresume continues the worker) and, 15 minutes past it, continues the worker itself. A case delta over 1000 lines escalates: it must be split. A tamper-path finding or an integrity mismatch
  escalates at once.

State: /var/lib/bw1-sandbox/cases/<id>/ (case.json, case.md, plan.md, reviews, checklist.md, discharge.json,
events.jsonl; readable by the publisher), the precedent log /var/lib/bw1-sandbox/cases/precedents.md (root-owned),
and the Chief's per-case overrides in <id>/chief.json (root-owned; ignored otherwise).

Usage (as the sandbox user, except the Chief commands):
  hospital.py triage ID --fellow NAME --brief FILE [--title T] [--units A,B] [--functions F,G] [--impact low|mid|high]
                     [--difficulty low|mid|high] [--constraints FILE] [--base REV] [--start SHA] [--hold]
                     [--floor UNIT=MATCHED_FUNCTIONS]... [--fellow-model M] [--attending-model M] [--effort E]
  hospital.py advance ID          one step: evaluate the finished stage, then launch the next
  hospital.py charge              advance every open case (the bw1-hospital-charge timer; installed disabled)
  hospital.py status [ID]         cases, stages, rounds and escalations, and the queued cases
  hospital.py queue ID --brief FILE [--title T] [--after CASE]
                                  record a case to triage later (cases/queue/ID.md); nothing runs. When its turn
                                  comes, triage it with the command its file names
  hospital.py discharge ID        run the Discharge Nurse now (the case must be in its discharge stage)
  Chief (root): precedent TEXT | release ID --head SHA | allow-paths ID --head SHA PATH... |
                accept-floor ID --unit U --metric M --value V --ref TEXT   (an accepted loss: V is the new floor for
                  that unit and metric, a number or start-N; metrics: matched_functions, matched_code, fuzzy,
                  section:<name> such as section:.text or section:.rdata, and improved_functions (0 waives "a
                  target function improved")) |
                resolve ID --stage workup|plan_review|treatment|code_review|discharge [--note TEXT] [--reset-rounds] |
                hold ID [--off]
"""
import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import integrity  # noqa: E402
import quality_gate  # noqa: E402

CASES = Path(os.environ.get("BW1_CASES", "/var/lib/bw1-sandbox/cases"))
PROMPTS = HERE / "hospital"
STOP_FILE = Path("/etc/bw1-sandbox/STOP")
WORKERS = "/srv/decomp/workers"
SHARED_TREE = "/srv/decomp/shared/tree"
SHARED_DATA = "/srv/decomp/shared/data"
CLANG_FORMAT = SHARED_TREE + "/build/tools/clang-format22/bin/clang-format"
SANDBOX_TOML = "/opt/bw1-sandbox/bw1-ai-sandbox/sandbox.droplet.toml"
IMAGE = "bw1-worker"
MAX_ROUNDS = 3  # rework rounds; the 4th escalates
MAX_RERUNS = 2  # re-runs of a stalled stage; the 3rd stall escalates
MAX_DELTA = 1000  # changed lines from the case's start; more must be split
LIMIT_GRACE = 15 * 60  # after a usage-limit reset, the sandbox's autoresume gets this long before the hospital resumes
CONTINUE_PROMPT = ("Your last run stopped on the account's usage limit, which has reset. Continue your task from "
                   "where you stopped: check your branch and working tree first (`git status`, `git log`), finish "
                   "the stage's work and write its artifact.")
# The Discharge container's caps. The droplet has 8 vCPUs and 16 GB; each worker gets 2 CPUs and 4g. Half the CPUs,
# and 5g, leave two running workers (8g), the host and Ghidra (~2.5g) their memory (the Chief, 2026-10-12).
DISCHARGE_CPUS = float(os.environ.get("DISCHARGE_CPUS", "4"))
DISCHARGE_MEMORY = os.environ.get("DISCHARGE_MEMORY", "5g")
EPS = 1e-4  # score tolerance (objdiff reports floats)
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STAGES = ("workup", "plan_review", "treatment", "code_review", "discharge")
CLOSED = ("discharged", "escalated")
PLAN_SECTIONS = ("Diagnosis", "Files", "Approach", "Risks", "Verification", "Open questions", "Size estimate")
REVIEW_SECTIONS = ("Checks", "Blocking findings", "Resolution of prior findings", "Plan conformance",
                   "Match verification", "bwr checklist", "Anti-cheat checklist")
VERDICTS = ("APPROVE", "REQUEST_CHANGES", "ESCALATE")
BAD_SUBJECTS = (re.compile(r"^sandbox .*: uncommitted work at exit"), re.compile(r"^(fixup|squash|amend)!"),
                re.compile(r"\bWIP\b", re.I))
SOURCES = quality_gate.SOURCES


class HospitalError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def git(repo, *args, check=True) -> str:
    p = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise HospitalError(f"git {' '.join(args[:3])}: {p.stderr.strip()[-500:]}")
    return p.stdout.strip()


def sections(text: str) -> dict:
    """{heading: body} of a markdown document's `## ` sections."""
    out, current = {}, None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = m.group(1)
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def filled(body: str) -> bool:
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S).strip()
    return bool(text)


def missing_sections(text: str, wanted) -> list:
    have = sections(text)
    return [w for w in wanted if not filled(have.get(w, ""))]


def parse_review(text: str) -> dict:
    """verdict, reviewed head, blocking ids, resolution lines, missing sections, and whether it is valid."""
    verdicts = re.findall(r"^Verdict:\s*([A-Z_]+)\s*$", text, re.M)
    verdict = verdicts[0] if len(verdicts) == 1 and verdicts[0] in VERDICTS else ""
    head = re.search(r"^Reviewed head:\s*([0-9a-f]{40})\s*$", text, re.M)
    secs = sections(text)
    blocking = secs.get("Blocking findings", "")
    ids = sorted(set(re.findall(r"\[(B\d+)\]", blocking)), key=lambda x: int(x[1:]))
    resolution = secs.get("Resolution of prior findings", "")
    resolved = {m.group(1) for m in re.finditer(r"\[(B\d+)\]\s*resolved\b", resolution, re.I)}
    missing = missing_sections(text, REVIEW_SECTIONS)
    return {"verdict": verdict, "head": head.group(1) if head else "", "blocking": ids, "resolved": sorted(resolved),
            "missing": missing, "valid": bool(verdict) and not missing}


# ------------------------------------------------------------------------------------------------- the ward


class SandboxWard:
    """The real workers, through bw1-ai-sandbox's Sandbox (as the sandbox user, like bw1-sandbox)."""

    def __init__(self):
        import sandbox_droplet
        self.sb_mod = sandbox_droplet._load_sandbox()
        sandbox_droplet.patch(self.sb_mod)
        self.sb = self.sb_mod.Sandbox()
        self.repo = Path(self.sb.repo)

    def path(self, name: str, rel: str) -> str:
        if not NAME_RE.match(name) or ".." in rel.split("/"):
            raise HospitalError(f"bad worker path {name}/{rel}")
        return f"{WORKERS}/{name}/{rel}"

    def running(self, name: str) -> bool:
        return name in self.sb.running_workers()

    def limited(self, name: str):
        """The worker's last run ended on the account's usage limit: {"resetsAt": epoch or None, ...}, else None."""
        for w in self.sb.workers():
            if w.get("name") == name:
                return w.get("limited") or None
        return None

    def exists(self, name: str) -> bool:
        return name in self.sb.worker_names()

    def spawn(self, name, prompt, model="", effort=""):
        return self.sb.spawn(name, prompt, model, effort)

    def resume(self, name, prompt, model="", effort=""):
        return self.sb.resume(name, prompt, model, effort, mode="hospital")

    def read(self, name: str, rel: str):
        try:
            return self.sb.read_file(self.path(name, rel))
        except Exception:  # missing file, or no such worker
            return None

    def write(self, name: str, rel: str, data: bytes) -> None:
        path = self.path(name, rel)
        self.sb.exec(["sudo", "install", "-d", "-o", "2000", "-g", "2000", str(Path(path).parent)])
        self.sb.exec(["sudo", "tee", path], stdin=data)
        self.sb.exec(["sudo", "chown", "2000:2000", path])

    def remove(self, name: str, rel: str) -> None:
        self.sb.exec(["sudo", "rm", "-f", self.path(name, rel)])

    def head(self, name: str) -> str:
        self.sb.fetch([name])
        return git(self.repo, "rev-parse", f"refs/remotes/sandbox/{name}")

    def start_from(self, name: str, sha: str, case_id: str) -> str:
        """Point the worker's branch agent/<name> at sha (via the mirror, as case/<id>). Returns its old head."""
        git(self.repo, "cat-file", "-e", f"{sha}^{{commit}}")
        ip = self.sb.need_ip()
        subprocess.run(["git", "-C", str(self.repo), "push", "-q", "-f", f"ssh://decomp@{ip}/srv/decomp/mirror.git",
                        f"{sha}:refs/heads/case/{case_id}"], env=self.sb.git_env, check=True)
        self.sb.agent("update_workers")
        w = f"{WORKERS}/{name}"
        # uid 2000 has no passwd entry on the droplet, so `sudo -u '#2000'` is refused; setpriv needs none
        as_worker = ["sudo", "setpriv", "--reuid=2000", "--regid=2000", "--clear-groups", "env", f"HOME={w}/home",
                     "git", "-C", f"{w}/repo"]
        _, dirty, _ = self.sb.exec(as_worker + ["status", "--porcelain"])
        if dirty.strip():
            raise HospitalError(f"worker {name} has uncommitted changes; not moving its branch")
        _, old, _ = self.sb.exec(as_worker + ["rev-parse", "HEAD"])
        self.sb.exec(as_worker + ["branch", "-f", f"case-old/{case_id}", "HEAD"])
        self.sb.exec(as_worker + ["checkout", "-q", "-B", f"agent/{name}", f"origin/case/{case_id}"])
        return old.decode().strip()

    @property
    def docker(self):
        return self.sb.docker


# ------------------------------------------------------------------------------------------------- verification


def worker_setup_argv(toml_path: str = SANDBOX_TOML) -> list:
    import tomllib
    with open(toml_path, "rb") as f:
        return list(tomllib.load(f)["worker"]["setup"])


class PristineVerifier:
    """Discharge's build check in a throwaway worker-image container with no network (see verify.sh)."""

    def __init__(self, docker, repo: Path, version: str = "BW1W120", setup=None, image: str = IMAGE,
                 shared_tree: str = SHARED_TREE, shared_data: str = SHARED_DATA, timeout: int = 8 * 3600,
                 cpus: float = DISCHARGE_CPUS, memory: str = DISCHARGE_MEMORY):
        self.docker, self.repo, self.version, self.image = docker, Path(repo), version, image
        self.setup = setup
        self.shared_tree, self.shared_data, self.timeout = shared_tree, shared_data, timeout
        self.cpus, self.memory = cpus, memory

    @staticmethod
    def extract(repo: Path, rev: str, dest: Path, paths=()) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), "archive", "--format=tar", rev,
                                  "--", *paths], capture_output=True, check=True).stdout
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(dest, filter="data")

    def prepare(self, base: str, head: str, workdir: Path, start: str = "") -> None:
        """workdir/tree = the base tree; workdir/head = the head tree, and workdir/start (only when start is given)
        the start tree, both with tools/ and configure.py from the base. Old reports are removed."""
        for sub in ("tree", "head", "start"):
            shutil.rmtree(workdir / sub, ignore_errors=True)
        for f in ("verify.json", "base-report.json", "head-report.json"):
            (workdir / f).unlink(missing_ok=True)
        self.extract(self.repo, base, workdir / "tree")
        overlay = [p for p in ("tools", "configure.py")
                   if git(self.repo, "ls-tree", "--name-only", base, "--", p, check=False)]
        for sub, rev in (("head", head), ("start", start)):
            if not rev:
                continue
            self.extract(self.repo, rev, workdir / sub)
            shutil.rmtree(workdir / sub / "tools", ignore_errors=True)
            (workdir / sub / "configure.py").unlink(missing_ok=True)
            if overlay:
                self.extract(self.repo, base, workdir / sub, overlay)
        for sub in ("build/tools", "build/compilers"):
            (workdir / "tree" / sub).mkdir(parents=True, exist_ok=True)
        try:
            (workdir / "tree" / "orig").unlink(missing_ok=True)
            (workdir / "tree" / "orig").symlink_to("/opt/orig")
        except OSError:
            pass  # no symlinks here (tests on Windows); the container has its own check

    def container_args(self, workdir: Path) -> dict:
        uid = os.getuid() if hasattr(os, "getuid") else 2000
        gid = os.getgid() if hasattr(os, "getgid") else 2000
        tree = f"{workdir}/tree"
        return dict(
            image=self.image, entrypoint=["/bin/sh", "/verify/verify.sh"], detach=True, user=f"{uid}:{gid}",
            network_mode="none", cap_drop=["ALL"], security_opt=["no-new-privileges"], read_only=True,
            tmpfs={"/tmp": "rw,exec,size=2g"}, pids_limit=2048, working_dir="/work/tree",
            nano_cpus=int(self.cpus * 1e9), mem_limit=self.memory, memswap_limit=self.memory,
            environment={"WORKER_SETUP": json.dumps(self.setup or []), "VERSION": self.version,
                         "HOME": "/tmp/home"},
            volumes={str(workdir): {"bind": "/work", "mode": "rw"},
                     str(PROMPTS / "verify.sh"): {"bind": "/verify/verify.sh", "mode": "ro"},
                     f"{self.shared_tree}/build/tools": {"bind": "/work/tree/build/tools", "mode": "ro"},
                     f"{self.shared_tree}/build/compilers": {"bind": "/work/tree/build/compilers", "mode": "ro"},
                     self.shared_data: {"bind": "/opt/orig", "mode": "ro"}},
        )

    def __call__(self, base: str, head: str, workdir: Path, start: str = ""):
        """(passed, detail). Leaves workdir/base-report.json and head-report.json; with a start other than the base
        whose report is not cached yet (workdir/start-report.json for that sha), also measures the start."""
        start = start if start and start != base and cached_start(workdir) != start else ""
        self.prepare(base, head, workdir, start)
        if start:
            for f in ("start-report.json", "start-report.sha"):
                (workdir / f).unlink(missing_ok=True)
        args = self.container_args(workdir)
        image = args.pop("image")
        container = self.docker.containers.run(image, **args)
        try:
            status = container.wait(timeout=self.timeout)
            logs = container.logs(stdout=True, stderr=True)
        finally:
            container.remove(force=True)
        (workdir / "verify.log").write_bytes(logs or b"")
        code = status.get("StatusCode", 1)
        try:
            steps = json.loads((workdir / "verify.json").read_text())
        except (OSError, ValueError):
            steps = {}
        if start and steps.get("start") == 0 and (workdir / "start-report.json").is_file():
            (workdir / "start-report.sha").write_text(start + "\n")
        detail = f"exit {code}; steps {steps or 'unknown'}; log {workdir / 'verify.log'}"
        # The builds decide this item; regressions are judged on the host from the three reports (regressions()),
        # because decomp-regress compares with the base only and so also flags what the case's start already
        # changed (e.g. config the user merged into the start). Its exit code stays in verify.json and the log.
        return steps.get("baseline") == 0 and steps.get("head_build") == 0, detail


def cached_start(workdir: Path) -> str:
    """The start sha whose report is cached in workdir, or ""."""
    try:
        if (workdir / "start-report.json").is_file():
            return (workdir / "start-report.sha").read_text().strip()
    except OSError:
        pass
    return ""


def load_report(path: Path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("report", data) if isinstance(data, dict) else None


def report_unit(report: dict, unit: str):
    """A report's unit by its full name or its last path component (CitadelHeart = runblack/Black/CitadelHeart)."""
    for u in (report or {}).get("units", []):
        if u.get("name") == unit or u.get("name", "").rsplit("/", 1)[-1] == unit:
            return u
    return None


def unit_scores(u: dict) -> dict:
    m = u.get("measures", {})
    text = next((x for x in u.get("sections", []) if x.get("name") == ".text"), {})
    out = {"matched_functions": int(m.get("matched_functions", 0) or 0),
           "total_functions": int(m.get("total_functions", 0) or 0),
           "matched_code": int(m.get("matched_code", 0) or 0), "total_code": int(m.get("total_code", 0) or 0),
           "fuzzy": float(m.get("fuzzy_match_percent", 0.0) or 0.0),
           "text_fuzzy": float(text.get("fuzzy_match_percent", 0.0) or 0.0)}
    for x in u.get("sections", []):
        if x.get("name"):
            out["section:" + x["name"]] = float(x.get("fuzzy_match_percent", 0.0) or 0.0)
    return out


FLOOR_METRICS = ("matched_functions", "matched_code", "fuzzy", "improved_functions")  # plus section:<name>
INT_METRICS = ("matched_functions", "matched_code", "improved_functions")
FLOAT_TOL = 0.01  # a Chief's percentage is given to the hundredth


def valid_floor(metric: str, value: str) -> bool:
    import re as _re
    if metric not in FLOOR_METRICS and not _re.fullmatch(r"section:\.?[\w$.]+", metric):
        return False
    return bool(_re.fullmatch(r"(start-)?\d+(\.\d+)?", value)) and \
        not (metric in INT_METRICS and "." in value)


def resolve_floor(value: str, start: float) -> float:
    """A Chief floor: an absolute number, or start-N (N below the unit's score at the start)."""
    return start - float(value[6:]) if value.startswith("start-") else float(value)


def floor_overrides(overrides, unit: str) -> dict:
    """{metric: the latest override} for a unit (an override is the Chief's {unit, metric, value, ref, at})."""
    out = {}
    for o in overrides or []:
        if o.get("unit") == unit:
            out[o.get("metric")] = o
    return out


def is_case_unit(name: str, units) -> bool:
    return any(name == u or name.rsplit("/", 1)[-1] == u for u in units or [])


def worse_metrics(a: dict, b: dict) -> set:
    """The metrics (matched_functions, matched_code, fuzzy, section:<name>) on which unit b is below unit a."""
    sa, sb = unit_scores(a), unit_scores(b)
    out = set()
    for k in ["matched_functions", "matched_code", "fuzzy"] + [k for k in sa if k.startswith("section:")]:
        if k in sa and sb.get(k, 0) < sa[k] - (0 if k in INT_METRICS else EPS):
            out.add(k)
    return out


def regressions(base_report, start_report, head_report, case_units=()) -> list:
    """No regression against the base beyond what the case's start already had, and none from start to head:
    a unit outside the case may not be worse at the head than at the base on any metric on which the start was not
    already worse, nor worse than at the start; a unit present at the start may not be gone at the head. The
    case's own units are judged by progress() (the start and the Chief's accepted floors)."""
    if base_report is None or head_report is None or start_report is None:
        return ["missing a report (base, start or head) to judge regressions with"]
    B = {u["name"]: u for u in base_report.get("units", [])}
    S = {u["name"]: u for u in start_report.get("units", [])}
    H = {u["name"]: u for u in head_report.get("units", [])}
    problems = []
    for name in sorted(set(B) | set(S)):
        if is_case_unit(name, case_units):
            continue
        if name not in H:
            if name in S:
                problems.append(f"{name}: removed at the head")
            continue
        new = set()
        if name in B:
            new |= worse_metrics(B[name], H[name]) - (worse_metrics(B[name], S[name]) if name in S else set())
        if name in S:
            new |= worse_metrics(S[name], H[name])
        if new:
            problems.append(f"{name}: worse at the head ({', '.join(sorted(new))})")
    return problems


def function_targets(names, function: str) -> bool:
    """Whether a report function name (MSVC-mangled) is one of the case's functions (Class::Method or a name)."""
    for f in names:
        if function == f:
            return True
        parts = f.split("::")
        if len(parts) >= 2 and function.startswith("?" + "@".join(reversed(parts)) + "@@"):
            return True
        if len(parts) == 1 and (function.startswith(f"?{f}@") or function.lstrip("_") == f):
            return True
    return False


def progress(c: dict, start_report, head_report, overrides=()):
    """(problems, record) for the case's progress from its start to its head (see the docstring). overrides are
    the Chief's accepted floors (chief.json "floor_overrides")."""
    units, floors, wanted = c.get("units") or [], c.get("floors") or {}, c.get("functions") or []
    if not units:
        return ["the case lists no units, so its progress can't be measured"], {}
    if start_report is None or head_report is None:
        return [f"no {'start' if start_report is None else 'head'} report to measure progress with"], {}
    problems, record, improved = [], {"units": {}, "improved": []}, []
    for unit in units:
        s_u, h_u = report_unit(start_report, unit), report_unit(head_report, unit)
        if h_u is None or s_u is None:
            problems.append(f"{unit}: not in the {'head' if h_u is None else 'start'} report")
            continue
        s, h = unit_scores(s_u), unit_scores(h_u)
        over = floor_overrides(overrides, unit)
        record["units"][unit] = {"start": s, "head": h, "floor": floors.get(unit),
                                 "overrides": {k: {"value": v.get("value"), "ref": v.get("ref")}
                                               for k, v in over.items()}}
        metrics = ["matched_functions", "matched_code", "fuzzy"] + sorted(k for k in s if k.startswith("section:"))
        for key in metrics:
            if key not in h:
                problems.append(f"{unit}: no {key} at the head")
                continue
            integer = key in INT_METRICS
            if key in over:
                floor, why = resolve_floor(str(over[key]["value"]), s[key]), f"the Chief's accepted floor " \
                    f"{over[key]['value']} ({over[key].get('ref', '')})"
                tol = 0 if integer else FLOAT_TOL
            else:
                floor, why, tol = s[key], "the start", 0 if integer else EPS
            if h[key] < floor - tol:
                shown = (f"{h[key]} < {floor:g}" if integer else f"{h[key]:.4f}% < {floor:.4f}%")
                problems.append(f"{unit}: {key} {shown}, {why}")
        if unit in floors and h["matched_functions"] < int(floors[unit]):
            problems.append(f"{unit}: {h['matched_functions']} matched functions, below the triage floor "
                            f"{floors[unit]}")
        before = {f.get("name"): float(f.get("fuzzy_match_percent", 0.0) or 0.0) for f in s_u.get("functions", [])}
        for f in h_u.get("functions", []):
            name = f.get("name", "")
            if wanted and not function_targets(wanted, name):
                continue
            score = float(f.get("fuzzy_match_percent", 0.0) or 0.0)
            if score > before.get(name, 0.0) + EPS:
                improved.append(f"{unit}: {name} {before.get(name, 0.0):.2f}% -> {score:.2f}%")
    record["improved"] = improved
    waived = [o for o in overrides or [] if o.get("metric") == "improved_functions" and o.get("unit") in units
              and float(o.get("value", 1)) <= 0]
    if waived:
        record["improvement_waived"] = waived[-1].get("ref", "")
    if not improved and not waived:
        problems.append("no target function improved from the start "
                        + (f"(functions {', '.join(wanted)})" if wanted else f"(units {', '.join(units)})"))
    return problems, record


# ------------------------------------------------------------------------------------------------- the hospital


class Hospital:
    def __init__(self, ward, cases: Path = CASES, prompts: Path = PROMPTS, manifest: Path = integrity.MANIFEST,
                 verifier=None, clang_format: str = CLANG_FORMAT, stop_file: Path = STOP_FILE, root=None,
                 require_root_files: bool = True):
        self.ward, self.cases, self.prompts, self.manifest = ward, Path(cases), Path(prompts), Path(manifest)
        self.verifier = verifier
        self.clang_format, self.stop_file = clang_format, Path(stop_file)
        self._root = root
        self.require_root_files = require_root_files

    # --- files -------------------------------------------------------------------------------

    def is_root(self) -> bool:
        return is_root() if self._root is None else self._root

    def need_root(self, what: str) -> None:
        if not self.is_root():
            raise HospitalError(f"{what} is the Chief's: run it as root")

    def root_file(self, path: Path) -> bool:
        return path.is_file() and (not self.require_root_files or integrity.root_owned(path))

    def dir(self, cid: str) -> Path:
        if not ID_RE.match(cid or ""):
            raise HospitalError(f"bad case id {cid!r} (lowercase letters, digits and -, up to 31)")
        return self.cases / cid

    def load(self, cid: str) -> dict:
        try:
            return json.loads((self.dir(cid) / "case.json").read_text(encoding="utf-8"))
        except OSError:
            raise HospitalError(f"no case {cid}")

    def save(self, c: dict) -> None:
        d = self.dir(c["id"])
        tmp = d / "case.json.tmp"
        tmp.write_text(json.dumps(c, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, d / "case.json")
        if self.is_root() and hasattr(os, "chown"):
            st = d.stat()
            os.chown(d / "case.json", st.st_uid, st.st_gid)

    def put(self, cid: str, name: str, text: str) -> Path:
        p = self.dir(cid) / name
        p.write_text(text, encoding="utf-8")
        return p

    def get(self, cid: str, name: str) -> str:
        try:
            return (self.dir(cid) / name).read_text(encoding="utf-8")
        except OSError:
            return ""

    def event(self, c: dict, what: str) -> None:
        with open(self.dir(c["id"]) / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now(), "stage": c.get("stage"), "what": what}) + "\n")

    def precedents(self) -> str:
        p = self.cases / "precedents.md"
        if not p.exists():
            return "None yet."
        if not self.root_file(p):
            return "(The precedent log is not root-owned, so it is ignored. Tell the Chief.)"
        return p.read_text(encoding="utf-8").strip() or "None yet."

    def chief(self, cid: str) -> dict:
        p = self.dir(cid) / "chief.json"
        if not self.root_file(p):
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def allowed_paths(self, cid: str, head: str) -> list:
        return list((self.chief(cid).get("allow_paths") or {}).get(head, []))

    def harness_rules(self) -> str:
        """The preamble's harness section (droplet/preamble-droplet.md), for prompts that skip the preamble."""
        try:
            text = (self.prompts.parent / "preamble-droplet.md").read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return ("# Harness rules (they apply to you even if your session started before them)\n\n"
                + text.replace("\n## ", "\n### ") + "\n\n---\n\n")

    def template(self, name: str, **values) -> str:
        text = (self.prompts / name).read_text(encoding="utf-8")
        for k, v in values.items():
            text = text.replace("{{" + k + "}}", str(v))
        return text

    # --- triage ------------------------------------------------------------------------------

    def triage(self, cid: str, fellow: str, brief: str, title: str = "", units=(), functions=(), impact: str = "mid",
               difficulty: str = "mid", constraints: str = "", base: str = "", start: str = "", hold: bool = False,
               fellow_model: str = "", attending_model: str = "", effort: str = "", floors=None) -> dict:
        d = self.dir(cid)
        if d.exists():
            raise HospitalError(f"case {cid} exists")
        if not NAME_RE.match(fellow):
            raise HospitalError(f"bad fellow name {fellow!r}")
        repo = self.ward.repo
        old_head = ""
        if start:
            if not self.ward.exists(fellow):
                raise HospitalError(f"--start needs an existing fellow; {fellow} does not exist")
            start = git(repo, "rev-parse", f"{start}^{{commit}}")
        elif self.ward.exists(fellow):
            start = self.ward.head(fellow)
        if not base:
            if not start:
                raise HospitalError("--base is needed for a new fellow")
            base = git(repo, "merge-base", "upstream/main", start, check=False) or \
                git(repo, "merge-base", "origin/main", start)
        base = git(repo, "rev-parse", f"{base}^{{commit}}")
        d.mkdir(parents=True)
        c = {"id": cid, "title": title or cid, "fellow": fellow, "base": base, "start": start, "old_head": "",
             "hold": bool(hold), "stage": "workup", "rounds": 0, "attendings": 0, "running": None, "rerun": 0,
             "escalations": [], "violations": [], "impact": impact, "difficulty": difficulty,
             "units": list(units), "functions": list(functions), "models": {"fellow": fellow_model,
                                                                            "attending": attending_model},
             "effort": effort, "created": now(), "feedback": "", "reviews": [], "reviewed_head": "",
             "plan_review": "", "code_review": "", "floors": dict(floors or {})}
        if start and self.ward.head(fellow) != start:
            try:
                old_head = self.ward.start_from(fellow, start, cid)
            except Exception as e:  # leave no half-made case behind, so the triage can simply be re-run
                shutil.rmtree(d, ignore_errors=True)
                raise HospitalError(f"could not move {fellow} to {start[:10]}: {e}")
            c["old_head"] = old_head
        case_md = "\n".join([
            f"# Case {cid}: {c['title']}", "",
            f"- Fellow: `{fellow}`; impact **{impact}**, difficulty **{difficulty}**",
            f"- Base (upstream merge-base): `{base}`",
            f"- Start: `{start or '(the fellow starts from origin/base)'}`"
            + (f" (the fellow's earlier head `{old_head}` is kept as branch case-old/{cid})" if old_head else ""),
            f"- Hold: {'yes: the Chief decides on publishing' if hold else 'no'}",
            f"- Units: {', '.join(units) or '(see brief)'}",
            f"- Functions: {', '.join(functions) or '(see brief)'}",
            "- Progress floor (matched functions): "
            + (", ".join(f"{u} >= {n}" for u, n in sorted((floors or {}).items())) or "the start's own scores"),
            "", "## Brief", "", brief.strip(), "",
            "## Constraints", "",
            (constraints.strip() or "None beyond AGENTS.md, the preamble's rules and the precedents."), "",
            f"- The case's change (start..head) stays at or under {MAX_DELTA} changed lines; larger cases are split.",
            "- Discharge needs progress: no regression against the base, every unit at or above its score at "
            "the start (and its floor), and at least one target function scoring higher than at the start. Without "
            "that, the case goes to the Chief.",
            "- Protected files (config/**, tools/**, configure.py, build files, AGENTS.md, hooks) are not changed; "
            "any such need goes to the Chief.", ""])
        self.put(cid, "case.md", case_md)
        self.save(c)
        self.event(c, f"triaged: fellow {fellow}, base {base[:10]}, start {start[:10] if start else '-'}, "
                      f"hold {hold}" + (f", old head {old_head[:10]}" if old_head else ""))
        return c

    # --- prompts -----------------------------------------------------------------------------

    def gate_cmd(self, c: dict) -> str:
        allow = f" --allow-paths-from {c['start']}" if c.get("start") else ""
        return f"python3 ~/.claude/hooks/quality_gate.py --base {c['base']}{allow}"

    def review_template(self, c: dict, kind: str, n: int) -> str:
        return self.template("review-template.md", CASE_ID=c["id"], ROUND=n, KIND=kind,
                             BWR_CHECKLIST=(self.prompts / "bwr-checklist.md").read_text(encoding="utf-8").strip(),
                             ANTICHEAT_CHECKLIST=(self.prompts / "anticheat-checklist.md").read_text(
                                 encoding="utf-8").strip())

    def prior_reviews(self, c: dict, kind: str) -> str:
        parts = [f"### {r['file']} (verdict {r['verdict']})\n\n{self.get(c['id'], r['file']).strip()}"
                 for r in c.get("reviews", []) if r["kind"] == kind]
        return "\n\n".join(parts) or "None."

    def feedback(self, c: dict) -> str:
        name = c.get("feedback")
        if not name:
            return ""
        return (f"## Feedback to address (from {name})\n\nThe last round was sent back. Address every blocking "
                f"finding below; the next review checks each one.\n\n{self.get(c['id'], name).strip()}\n")

    def attempt_note(self, c: dict) -> str:
        if c.get("rerun"):
            return (f"Note: this stage is being re-run (attempt {c['rerun'] + 1}): the last run ended without its "
                    "artifact. Produce it this time, or write the handoff.")
        return ""

    def common(self, c: dict) -> dict:
        return dict(CASE_ID=c["id"], FELLOW=c["fellow"], BASE=c["base"], START=c.get("start") or c["base"],
                    ROUND=c["rounds"] + 1, PRECEDENTS=self.precedents() + self.accepted_floors(c["id"]), CASE_MD=self.get(c["id"], "case.md").strip(),
                    PLAN_MD=self.get(c["id"], "plan.md").strip() or "(no plan)", GATE_CMD=self.gate_cmd(c),
                    ATTEMPT_NOTE=self.attempt_note(c), FEEDBACK=self.feedback(c),
                    PLAN_TEMPLATE=self.template("plan-template.md", CASE_ID=c["id"]).strip(),
                    IPASS_TEMPLATE=self.template("ipass-template.md", CASE_ID=c["id"]).strip())

    # --- stages ------------------------------------------------------------------------------

    def launch(self, c: dict) -> str:
        stage, fellow = c["stage"], c["fellow"]
        models = c.get("models") or {}
        if stage in ("workup", "treatment"):
            exists = self.ward.exists(fellow)
            if exists:  # a new fellow gets its case in the prompt (its folder must not exist before the spawn)
                for f in ("home/case/handoff.md",) + (("home/case/plan.md",) if stage == "workup" else ()):
                    self.ward.remove(fellow, f)
                self.ward.write(fellow, "home/case/case.md", self.get(c["id"], "case.md").encode())
                self.ward.write(fellow, "home/case/gate-base", (c["base"] + "\n").encode())
                if c.get("start"):
                    self.ward.write(fellow, "home/case/gate-allow", (c["start"] + "\n").encode())
            head_before = self.ward.head(fellow) if exists else ""
            prompt = self.template("fellow-workup.md" if stage == "workup" else "fellow-treatment.md",
                                   **self.common(c))
            if exists:
                # A resumed session never sees the preamble again, and an older worker started before these rules
                # existed: the harness rules go in front of every resumed prompt.
                self.ward.resume(fellow, self.harness_rules() + prompt, models.get("fellow", ""), c.get("effort", ""))
            else:
                self.ward.spawn(fellow, prompt, models.get("fellow", ""), c.get("effort", ""))
            worker = fellow
        elif stage in ("plan_review", "code_review"):
            head_before = self.ward.head(fellow)
            c["attendings"] = c.get("attendings", 0) + 1
            worker = f"att-{c['id']}-{c['attendings']}"
            kind = "plan" if stage == "plan_review" else "code"
            prompt = self.template("attending-plan.md" if kind == "plan" else "attending-code.md",
                                   **{**self.common(c), "HEAD": head_before,
                                      "PRIOR_REVIEWS": self.prior_reviews(c, kind),
                                      "REVIEW_TEMPLATE": self.review_template(c, kind, c["rounds"] + 1).strip()})
            self.put(c["id"], f"prompt-{worker}.md", prompt)
            self.ward.spawn(worker, prompt, models.get("attending", ""), c.get("effort", ""))
        else:
            raise HospitalError(f"nothing to launch in stage {stage}")
        c["running"] = {"worker": worker, "stage": stage, "started": now(), "head_before": head_before,
                        "attempt": c.get("rerun", 0) + 1}
        self.event(c, f"launched {worker} ({stage}, attempt {c['running']['attempt']})")
        return f"{c['id']}: launched {worker} for {stage}"

    def models_for(self, c: dict, worker: str):
        models = c.get("models") or {}
        return (models.get("fellow" if worker == c["fellow"] else "attending", ""), c.get("effort", ""))

    def accepted_floors(self, cid: str) -> str:
        """The Chief's accepted floors for this case, for every prompt (the latest per unit and metric counts)."""
        latest = {}
        for o in self.chief(cid).get("floor_overrides") or []:
            latest[(o.get("unit"), o.get("metric"))] = o
        if not latest:
            return ""
        rows = [f"- {u} {m} >= {o.get('value')} ({o.get('ref')})" for (u, m), o in latest.items()]
        return ("\n\nThis case's accepted floors (the Chief's rulings; discharge checks these instead of the start, "
                "and a loss down to them is decided, not a finding):\n" + "\n".join(rows))

    def escalate(self, c: dict, why: str) -> str:
        c["escalations"].append({"at": now(), "stage": c["stage"], "why": why, "open": True})
        c["stage"], c["running"] = "escalated", None
        self.event(c, f"ESCALATED: {why}")
        return f"{c['id']}: escalated to the Chief: {why}"

    def rework(self, c: dict, to: str, feedback: str, why: str) -> str:
        c["rounds"] += 1
        c["running"], c["rerun"] = None, 0
        if c["rounds"] > MAX_ROUNDS:
            return self.escalate(c, f"more than {MAX_ROUNDS} rework rounds (last: {why})")
        c["stage"], c["feedback"] = to, feedback
        self.event(c, f"rework round {c['rounds']}: back to {to}: {why}")
        return f"{c['id']}: rework round {c['rounds']}, back to {to}: {why}"

    def stall(self, c: dict, why: str) -> str:
        attempt = c["running"]["attempt"] if c.get("running") else 1
        c["running"] = None
        if attempt > MAX_RERUNS:
            return self.escalate(c, f"stage {c['stage']} stalled {attempt} times ({why})")
        c["rerun"] = attempt
        self.event(c, f"stalled ({why}); re-run {attempt} of {MAX_RERUNS}")
        return f"{c['id']}: {c['stage']} stalled ({why}); re-running"

    def to_stage(self, c: dict, stage: str, what: str) -> str:
        c["stage"], c["running"], c["rerun"] = stage, None, 0
        self.event(c, what)
        return f"{c['id']}: {what}"

    def evaluate(self, c: dict) -> str:
        run, fellow, cid = c["running"], c["fellow"], c["id"]
        stage = run["stage"]
        handoff = self.ward.read(fellow, "home/case/handoff.md") if stage in ("workup", "treatment") else None
        if handoff:
            n = len(list(self.dir(cid).glob("handoff-*.md"))) + 1
            self.put(cid, f"handoff-{n}.md", handoff.decode(errors="replace"))
            plan = self.ward.read(fellow, "home/case/plan.md") if stage == "workup" else None
            if plan:  # the Chief needs the plan the handoff refers to
                self.put(cid, f"plan-r{c['rounds'] + 1}-handoff.md", plan.decode(errors="replace"))
            return self.escalate(c, f"the Fellow wrote an I-PASS handoff (handoff-{n}.md)")
        if stage == "workup":
            plan = self.ward.read(fellow, "home/case/plan.md")
            if not plan:
                return self.stall(c, "no ~/case/plan.md")
            text = plan.decode(errors="replace")
            missing = missing_sections(text, PLAN_SECTIONS)
            if missing:
                return self.stall(c, f"plan.md lacks sections: {', '.join(missing)}")
            if self.ward.head(fellow) != run["head_before"]:
                c["violations"].append({"at": now(), "what": "commits during the workup (code before an approved plan)"})
            self.put(cid, f"plan-r{c['rounds'] + 1}.md", text)
            self.put(cid, "plan.md", text)
            c["feedback"] = ""
            return self.to_stage(c, "plan_review", "plan written; to plan review")
        if stage == "treatment":
            head = self.ward.head(fellow)
            if head == run["head_before"]:
                return self.stall(c, "no new commits")
            delta = self.delta(c, head)
            if delta > MAX_DELTA:
                return self.escalate(c, f"case delta is {delta} changed lines (> {MAX_DELTA}): split the case")
            c["treated_head"], c["feedback"] = head, ""
            return self.to_stage(c, "code_review", f"treated (head {head[:10]}, {delta} lines); to code review")
        # an Attending's review
        worker = run["worker"]
        raw = self.ward.read(worker, "home/case/review.md")
        if not raw:
            return self.stall(c, f"{worker} wrote no ~/case/review.md")
        text = raw.decode(errors="replace")
        r = parse_review(text)
        if not r["valid"]:
            why = "no single valid verdict" if not r["verdict"] else f"missing sections {', '.join(r['missing'])}"
            return self.stall(c, f"{worker}'s review is incomplete: {why}")
        kind = "plan" if stage == "plan_review" else "code"
        name = f"review-{kind}-{len([x for x in c['reviews'] if x['kind'] == kind]) + 1}-{worker}.md"
        self.put(cid, name, text)
        c["reviews"].append({"file": name, "kind": kind, "worker": worker, "verdict": r["verdict"],
                             "head": r["head"] or run["head_before"], "blocking": r["blocking"]})
        c[f"{kind}_review"] = name
        if r["verdict"] == "ESCALATE":
            return self.escalate(c, f"the Attending escalated ({name})")
        if r["verdict"] == "REQUEST_CHANGES":
            return self.rework(c, "workup" if kind == "plan" else "treatment", name,
                               f"{worker} requested changes ({', '.join(r['blocking']) or 'see review'})")
        if kind == "code":
            if r["head"] != run["head_before"] or self.ward.head(fellow) != run["head_before"]:
                return self.stall(c, f"{worker} approved {r['head'][:10] or '?'}, but the head is "
                                     f"{self.ward.head(fellow)[:10]}")
            c["reviewed_head"] = run["head_before"]
            return self.to_stage(c, "discharge", f"code approved by {worker}; to discharge")
        return self.to_stage(c, "treatment", f"plan approved by {worker}; to treatment")

    def delta(self, c: dict, head: str) -> int:
        lines = 0
        for row in git(self.ward.repo, "diff", "--numstat", f"{c.get('start') or c['base']}..{head}").splitlines():
            cols = row.split("\t")
            if len(cols) >= 2 and cols[0].isdigit() and cols[1].isdigit():
                lines += int(cols[0]) + int(cols[1])
        return lines

    def advance(self, cid: str) -> str:
        if self.stop_file.exists():
            return f"{cid}: stop file {self.stop_file} present; nothing done"
        c = self.load(cid)
        if c["stage"] in CLOSED:
            return f"{cid}: {c['stage']}"
        out = []
        if c.get("running"):
            worker = c["running"]["worker"]
            if self.ward.running(worker):
                return f"{cid}: waiting for {worker} ({c['stage']})"
            limit = self.ward.limited(worker) if hasattr(self.ward, "limited") else None
            if limit:
                # The run ended on the account's usage limit, not with its artifact: never a stall. Wait for the
                # reset (the sandbox's autoresume continues the worker); well past it, continue the worker here.
                resets = limit.get("resetsAt") or 0
                if not resets or time.time() < resets + LIMIT_GRACE:
                    when = datetime.fromtimestamp(resets, timezone.utc).strftime("%H:%M UTC") if resets else "?"
                    return f"{cid}: {worker} stopped on the usage limit (resets {when}); waiting"
                self.ward.resume(worker, CONTINUE_PROMPT, *self.models_for(c, worker))
                self.event(c, f"continued {worker} after the usage limit reset")
                self.save(c)
                return f"{cid}: continued {worker} after the usage limit reset"
            out.append(self.evaluate(c))
            self.save(c)
        if c["stage"] == "discharge":
            out.append(self.discharge_case(c))
        elif c["stage"] not in CLOSED:
            out.append(self.launch(c))
        self.save(c)
        return "\n".join(out)

    def charge(self) -> list:
        if not self.cases.is_dir():
            return []
        out = []
        for d in sorted(self.cases.iterdir()):
            if (d / "case.json").is_file():
                try:
                    out.append(self.advance(d.name))
                except Exception as e:  # one case failing must not stop the others
                    out.append(f"{d.name}: ERROR {e}")
        return out

    # --- discharge ---------------------------------------------------------------------------

    def format_problems(self, repo: Path, base: str, head: str, workdir: Path) -> list:
        files = [f for f in git(repo, "diff", "--name-only", "--diff-filter=AM", f"{base}..{head}").splitlines()
                 if f.endswith(SOURCES)]
        if not files:
            return []
        if not Path(self.clang_format).is_file():
            return [f"clang-format 22 not found at {self.clang_format}"]
        tree = workdir / "format"
        shutil.rmtree(tree, ignore_errors=True)
        PristineVerifier.extract(repo, head, tree, [".clang-format", *files])
        p = subprocess.run([self.clang_format, "--dry-run", "--Werror", "--style=file", *files], cwd=tree,
                           capture_output=True, text=True)
        if p.returncode == 0:
            return []
        bad = sorted(set(re.findall(r"^(\S+?):\d+:\d+: (?:error|warning)", p.stderr, re.M)))
        return [f"not clang-format clean: {', '.join(bad[:10]) or p.stderr.strip()[-300:]}"]

    def history_problems(self, repo: Path, start: str, head: str, reviewed: str) -> list:
        problems = []
        if git(repo, "rev-list", "--merges", f"{start}..{head}"):
            problems.append("merge commits in the case")
        for row in git(repo, "log", "--format=%h %s", f"{start}..{head}").splitlines():
            sha, _, subject = row.partition(" ")
            if any(p.search(subject) for p in BAD_SUBJECTS):
                problems.append(f"commit {sha} {subject[:60]!r} (sandbox snapshot, fixup or WIP)")
        if head != reviewed:
            problems.append(f"head {head[:10]} is not the reviewed head {reviewed[:10] or '(none)'}")
        return problems

    def review_problems(self, c: dict, head: str) -> list:
        code = [r for r in c.get("reviews", []) if r["kind"] == "code"]
        if not code:
            return ["no code review"]
        last = code[-1]
        r = parse_review(self.get(c["id"], last["file"]))
        problems = []
        if r["verdict"] != "APPROVE":
            problems.append(f"last code review {last['file']} is {r['verdict'] or 'invalid'}")
        if r["head"] != head:
            problems.append(f"last code review covers {r['head'][:10] or '?'}, not {head[:10]}")
        if r["blocking"]:
            problems.append(f"last code review still has blocking findings {', '.join(r['blocking'])}")
        earlier = sorted({b for x in code[:-1] for b in x.get("blocking", [])}, key=lambda x: int(x[1:]))
        open_ = [b for b in earlier if b not in r["resolved"]]
        if open_:
            problems.append(f"prior findings not marked resolved: {', '.join(open_)}")
        return problems

    def discharge_case(self, c: dict) -> str:
        cid, fellow, repo = c["id"], c["fellow"], self.ward.repo
        head = self.ward.head(fellow)
        start = c.get("start") or c["base"]
        workdir = self.dir(cid) / "discharge"
        workdir.mkdir(exist_ok=True)
        items = []

        def item(key, label, problems, skipped=False):
            items.append({"key": key, "label": label, "ok": not problems and not skipped, "skipped": skipped,
                          "problems": problems})

        manifest, mproblems = integrity.load(self.manifest, self.require_root_files)
        mproblems = mproblems or integrity.verify(self.manifest, self.require_root_files)
        item("integrity", "Harness files match /etc/bw1-sandbox/integrity.json", mproblems)
        plan_ok = bool(self.get(cid, "plan.md")) and any(r["kind"] == "plan" and r["verdict"] == "APPROVE"
                                                         for r in c.get("reviews", []))
        item("plan", "Approved plan exists", [] if plan_ok else ["no plan approved by an Attending"])
        tmpl = [f"plan.md lacks {s}" for s in missing_sections(self.get(cid, "plan.md"), PLAN_SECTIONS)]
        for r in c.get("reviews", []):
            tmpl += [f"{r['file']} lacks {s}" for s in missing_sections(self.get(cid, r["file"]), REVIEW_SECTIONS)]
        item("templates", "Plan and reviews follow their templates", tmpl)
        item("review", "Last code review APPROVE on this head, no unresolved [B#]", self.review_problems(c, head))
        try:
            findings = quality_gate.run(str(repo), c["base"], head, allow_from=c.get("start", ""),
                                        allow_paths=self.allowed_paths(cid, head))
        except quality_gate.GitError as e:
            findings = [quality_gate.Finding("tamper-path", "(gate error)", 0, "", str(e))]
        hard = [f for f in findings if f.level == "hard"]
        warn = [f for f in findings if f.level == "warn"]
        item("gate", "Quality gate green (host, git objects only)", [f"{f.rule}: {f.path}:{f.line}" for f in hard])
        item("format", "clang-format 22 clean", self.format_problems(repo, c["base"], head, workdir))
        item("history", "Clean history (no merges, snapshots, fixup!/WIP; head == reviewed head)",
             self.history_problems(repo, start, head, c.get("reviewed_head", "")))
        delta = self.delta(c, head)
        item("delta", f"Case delta at most {MAX_DELTA} lines ({delta})",
             [] if delta <= MAX_DELTA else [f"{delta} changed lines: split the case"])
        if manifest:
            hooks = integrity.worker_hook_problems(manifest, lambda rel: self.ward.read(fellow, rel))
        else:
            hooks = ["no integrity manifest to compare the worker's hooks with"]
        item("hooks", "Worker hooks match the manifest", hooks)
        if all(i["ok"] for i in items):
            if self.verifier is None:
                item("verify", "Pristine verification (no network, base tools)", ["no verifier configured"])
            else:
                ok, detail = self.verifier(c["base"], head, workdir, start)
                item("verify", "Pristine verification (no network, base tools)", [] if ok else [detail])
                items[-1]["detail"] = detail
        else:
            item("verify", "Pristine verification (no network, base tools)", [], skipped=True)
        record = {}
        if all(i["ok"] for i in items):
            start_report = load_report(workdir / ("base-report.json" if start == c["base"] else "start-report.json"))
            if start != c["base"] and cached_start(workdir) != start:
                start_report = None
            head_report = load_report(workdir / "head-report.json")
            item("regress", "No regression against the base beyond the start's, none from start to head",
                 regressions(load_report(workdir / "base-report.json"), start_report, head_report,
                             c.get("units") or []))
            pproblems, record = progress(c, start_report, head_report,
                                         self.chief(cid).get("floor_overrides") or [])
            item("progress", "Progress: units at or above the start and floor, a target function improved",
                 pproblems)
            if record.get("units") and not c.get("baseline"):
                c["baseline"] = {"start": start, **{u: v["start"] for u, v in record["units"].items()}}
        else:
            item("regress", "No regression against the base beyond the start's, none from start to head", [],
                 skipped=True)
            item("progress", "Progress: units at or above the start and floor, a target function improved", [],
                 skipped=True)
        passed = all(i["ok"] for i in items)
        result = {"case": cid, "head": head, "passed": passed, "at": now(), "items": items, "progress": record,
                  "warnings": [f"{f.rule}: {f.path}:{f.line} {f.detail}".strip() for f in warn]}
        self.put(cid, "discharge.json", json.dumps(result, indent=2) + "\n")
        self.put(cid, "checklist.md", checklist_md(result))
        c.setdefault("discharges", []).append({"at": result["at"], "head": head, "passed": passed})
        if passed:
            return self.to_stage(c, "discharged", f"DISCHARGED at {head[:10]}")
        failed = {i["key"] for i in items if not i["ok"] and not i["skipped"]}
        if failed & {"integrity", "hooks"} or any(f.rule in quality_gate.ESCALATE for f in hard):
            return self.escalate(c, f"discharge found tampering ({', '.join(sorted(failed))}); see checklist.md")
        if "delta" in failed:
            return self.escalate(c, f"case delta {delta} > {MAX_DELTA} lines: split the case")
        if "progress" in failed:
            return self.escalate(c, "discharge found no progress (units below the start or floor, or no target "
                                    "function improved); see checklist.md")
        return self.rework(c, "treatment", "checklist.md", f"discharge failed: {', '.join(sorted(failed))}")

    def discharge(self, cid: str) -> str:
        c = self.load(cid)
        if c["stage"] != "discharge":
            raise HospitalError(f"case {cid} is in {c['stage']}, not discharge")
        out = self.discharge_case(c)
        self.save(c)
        return out

    # --- the Chief ---------------------------------------------------------------------------

    def precedent(self, text: str) -> str:
        self.need_root("precedent")
        p = self.cases / "precedents.md"
        self.cases.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("# Precedents (the Chief's decisions; they bind every case)\n\n", encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"- {now()[:10]}: {text.strip()}\n")
        os.chmod(p, 0o644)
        return f"precedent recorded in {p}"

    def write_chief(self, cid: str, change) -> dict:
        self.need_root("chief.json")
        self.load(cid)
        p = self.dir(cid) / "chief.json"
        data = self.chief(cid) if p.exists() else {}
        change(data)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
        return data

    def release(self, cid: str, head: str) -> str:
        if not SHA_RE.match(head):
            raise HospitalError("--head must be a full sha")
        self.write_chief(cid, lambda d: d.setdefault("release", []).append(head))
        c = self.load(cid)
        self.event(c, f"Chief released head {head[:10]} for publishing")
        return f"{cid}: head {head[:10]} released"

    def queue(self, cid: str, brief: str, title: str = "", after: str = "") -> str:
        """A case for later: its brief in cases/queue/<id>.md. Nothing is triaged or launched."""
        self.dir(cid)  # validates the id
        q = self.cases / "queue"
        q.mkdir(parents=True, exist_ok=True)
        p = q / f"{cid}.md"
        if p.exists() or (self.cases / cid).exists():
            raise HospitalError(f"case {cid} exists")
        head = [f"# Queued case {cid}: {title or cid}", "", f"- Queued: {now()}"]
        if after:
            head.append(f"- After: case `{after}` (start from its discharged, released head)")
        p.write_text("\n".join(head) + "\n\n" + brief.strip() + "\n", encoding="utf-8")
        return f"{cid}: queued in {p}"

    def queued(self) -> list:
        q = self.cases / "queue"
        out = []
        for p in sorted(q.glob("*.md")) if q.is_dir() else []:
            first = p.read_text(encoding="utf-8").splitlines()[:1]
            out.append(f"{p.stem:24} queued       {first[0].split(': ', 1)[-1][:70] if first else ''}")
        return out

    def accept_floor(self, cid: str, unit: str, metric: str, value: str, ref: str) -> str:
        """The Chief accepts a loss: value becomes the floor of this unit's metric at discharge (chief.json)."""
        if not unit or not ref.strip():
            raise HospitalError("accept-floor needs --unit and a --ref to the case and ruling")
        if not valid_floor(metric, str(value)):
            raise HospitalError(f"bad floor {metric}={value} (metrics {', '.join(FLOOR_METRICS)} or "
                                "section:<name>; value a number or start-N, whole for counts)")
        entry = {"unit": unit, "metric": metric, "value": str(value), "ref": ref.strip(), "at": now()}
        self.write_chief(cid, lambda d: d.setdefault("floor_overrides", []).append(entry))
        c = self.load(cid)
        self.event(c, f"Chief accepted floor {unit} {metric} >= {value} ({ref.strip()})")
        return f"{cid}: floor {unit} {metric} >= {value} accepted"

    def allow_paths(self, cid: str, head: str, paths) -> str:
        if not SHA_RE.match(head):
            raise HospitalError("--head must be a full sha")
        self.write_chief(cid, lambda d: d.setdefault("allow_paths", {}).setdefault(head, []).extend(paths))
        c = self.load(cid)
        self.event(c, f"Chief allowed protected paths at {head[:10]}: {', '.join(paths)}")
        return f"{cid}: {len(paths)} path(s) allowed at {head[:10]}"

    def resolve(self, cid: str, stage: str, note: str = "", reset_rounds: bool = False) -> str:
        self.need_root("resolve")
        if stage not in STAGES:
            raise HospitalError(f"--stage must be one of {', '.join(STAGES)}")
        c = self.load(cid)
        for e in c["escalations"]:
            if e.get("open"):
                e["open"], e["resolved"], e["note"] = False, now(), note
        if reset_rounds:
            c["rounds"] = 0
        c["stage"], c["running"], c["rerun"] = stage, None, 0
        if note:
            c["feedback"] = self.put(cid, f"chief-note-{len(c['escalations'])}.md",
                                     f"# The Chief's decision\n\n{note.strip()}\n").name
        self.event(c, f"Chief resolved: to {stage}" + (f" ({note})" if note else ""))
        self.save(c)
        return f"{cid}: resolved, now in {stage}"

    def hold(self, cid: str, on: bool = True) -> str:
        if not on:
            self.need_root("releasing a hold")
        c = self.load(cid)
        c["hold"] = on
        self.event(c, f"hold {'set' if on else 'lifted by the Chief'}")
        self.save(c)
        return f"{cid}: hold {'on' if on else 'off'}"

    def status(self, cid: str = "") -> str:
        ids = [cid] if cid else sorted(d.name for d in self.cases.iterdir() if (d / "case.json").is_file()) \
            if self.cases.is_dir() else []
        lines, escalated = [], []
        for i in ids:
            c = self.load(i)
            run = c.get("running") or {}
            state = ""
            if run:
                try:
                    running = self.ward is not None and self.ward.running(run["worker"])
                    alive = "?" if self.ward is None else ("running" if running else "finished")
                except Exception:  # e.g. root, who has no sandbox ssh key: the case list still prints
                    alive = "?"
                state = f", {run['worker']} {alive}"
            lines.append(f"{i:24} {c['stage']:12} rounds {c['rounds']}/{MAX_ROUNDS}  fellow {c['fellow']}"
                         f"{'  HOLD' if c.get('hold') else ''}{state}  {c['title'][:50]}")
            for e in c.get("escalations", []):
                if e.get("open"):
                    escalated.append(f"  {i}: [{e['stage']}] {e['why']} ({e['at']})")
            for v in c.get("violations", []):
                lines.append(f"    violation: {v['what']} ({v['at']})")
            for o in self.chief(i).get("floor_overrides") or []:
                lines.append(f"    accepted floor: {o.get('unit')} {o.get('metric')} >= {o.get('value')} "
                             f"({o.get('ref')})")
        if not cid:
            lines += self.queued()
        if escalated:
            lines += ["", "Escalations waiting for the Chief (resolve as root: hospital.py resolve ID --stage S "
                          "[--note ...]):", *escalated]
        return "\n".join(lines) or "no cases"


def checklist_md(result: dict) -> str:
    out = [f"# Discharge checklist: {result['case']}", "",
           f"Head `{result['head']}`, {result['at']}: **{'PASSED' if result['passed'] else 'FAILED'}**", "",
           "| Check | Result | Problems |", "|-------|--------|----------|"]
    for i in result["items"]:
        mark = "skipped" if i["skipped"] else ("ok" if i["ok"] else "FAIL")
        probs = "; ".join(i["problems"])[:600].replace("|", "\\|") or ""
        out.append(f"| {i['label']} | {mark} | {probs} |")
    prog = result.get("progress") or {}
    if prog.get("units"):
        out += ["", "| Unit | Matched functions (start -> head) | Fuzzy % | .text % | Floor |",
                "|------|------|------|------|------|"]
        for unit, v in prog["units"].items():
            s, h = v["start"], v["head"]
            out.append(f"| {unit} | {s['matched_functions']}/{s['total_functions']} -> "
                       f"{h['matched_functions']}/{h['total_functions']} | {s['fuzzy']:.2f} -> {h['fuzzy']:.2f} | "
                       f"{s['text_fuzzy']:.2f} -> {h['text_fuzzy']:.2f} | {v.get('floor') or '-'} |")
        accepted = [f"- {u}: {m} >= {o['value']} ({o['ref']})" for u, v in prog["units"].items()
                    for m, o in (v.get("overrides") or {}).items()]
        if accepted:
            out += ["", "The Chief's accepted floors (they replace the start for these metrics):", "", *accepted]
        if prog.get("improvement_waived"):
            out += ["", f"The improvement requirement is waived by the Chief ({prog['improvement_waived']})."]
        out += ["", f"Improved functions ({len(prog.get('improved', []))}):", ""]
        out += [f"- {x}" for x in prog.get("improved", [])[:40]] or ["- none"]
    if result.get("warnings"):
        out += ["", f"Quality gate warnings ({len(result['warnings'])}):", ""]
        out += [f"- {w}" for w in result["warnings"][:80]]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("triage")
    t.add_argument("id")
    t.add_argument("--fellow", required=True)
    t.add_argument("--brief", required=True, help="file with the brief (e.g. the review comments)")
    t.add_argument("--title", default="")
    t.add_argument("--units", default="")
    t.add_argument("--functions", default="")
    t.add_argument("--impact", choices=["low", "mid", "high"], default="mid")
    t.add_argument("--difficulty", choices=["low", "mid", "high"], default="mid")
    t.add_argument("--constraints", default="", help="file")
    t.add_argument("--base", default="")
    t.add_argument("--start", default="")
    t.add_argument("--hold", action="store_true")
    t.add_argument("--floor", action="append", default=[], metavar="UNIT=N",
                   help="the unit must end with at least N matched functions (objdiff report); repeatable")
    t.add_argument("--fellow-model", default="")
    t.add_argument("--attending-model", default="")
    t.add_argument("--effort", default="")
    for name in ("advance", "discharge"):
        sub.add_parser(name).add_argument("id")
    sub.add_parser("charge")
    sub.add_parser("status").add_argument("id", nargs="?", default="")
    sub.add_parser("precedent").add_argument("text")
    r = sub.add_parser("release")
    r.add_argument("id")
    r.add_argument("--head", required=True)
    qu = sub.add_parser("queue")
    qu.add_argument("id")
    qu.add_argument("--brief", required=True)
    qu.add_argument("--title", default="")
    qu.add_argument("--after", default="")
    af = sub.add_parser("accept-floor")
    af.add_argument("id")
    af.add_argument("--unit", required=True)
    af.add_argument("--metric", required=True)
    af.add_argument("--value", required=True)
    af.add_argument("--ref", required=True, help="the case and the ruling, e.g. 'pr431 R13 ruling 2026-10-12'")
    al = sub.add_parser("allow-paths")
    al.add_argument("id")
    al.add_argument("--head", required=True)
    al.add_argument("paths", nargs="+")
    rs = sub.add_parser("resolve")
    rs.add_argument("id")
    rs.add_argument("--stage", required=True, choices=STAGES)
    rs.add_argument("--note", default="")
    rs.add_argument("--reset-rounds", action="store_true")
    h = sub.add_parser("hold")
    h.add_argument("id")
    h.add_argument("--off", action="store_true")
    a = ap.parse_args(argv)
    needs_ward = a.cmd in ("triage", "advance", "charge", "discharge")
    try:
        ward = SandboxWard() if needs_ward else None
        if a.cmd == "status":
            try:
                ward = SandboxWard()
            except Exception:  # e.g. the Chief, as root, has no sandbox state: list cases without worker states
                ward = None
        verifier = None
        if ward is not None and a.cmd in ("advance", "charge", "discharge"):
            version = os.environ.get("GAME_VERSION", "BW1W120")
            verifier = PristineVerifier(ward.docker, ward.repo, version, worker_setup_argv())
        hosp = Hospital(ward, verifier=verifier)
        if a.cmd == "triage":
            split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
            floors = {}
            for f in a.floor:
                unit, _, n = f.partition("=")
                if not unit or not n.isdigit():
                    raise HospitalError(f"bad --floor {f!r} (UNIT=N)")
                floors[unit] = int(n)
            c = hosp.triage(a.id, a.fellow, Path(a.brief).read_text(encoding="utf-8"), a.title, split(a.units),
                            split(a.functions), a.impact, a.difficulty,
                            Path(a.constraints).read_text(encoding="utf-8") if a.constraints else "", a.base, a.start,
                            a.hold, a.fellow_model, a.attending_model, a.effort, floors)
            print(f"triaged {c['id']}: stage {c['stage']}, base {c['base'][:10]}, start {c['start'][:10]}, "
                  f"hold {c['hold']}" + (f", old head {c['old_head'][:10]}" if c["old_head"] else ""))
        elif a.cmd == "advance":
            print(hosp.advance(a.id))
        elif a.cmd == "charge":
            print("\n".join(hosp.charge()) or "no cases")
        elif a.cmd == "discharge":
            print(hosp.discharge(a.id))
        elif a.cmd == "status":
            print(hosp.status(a.id))
        elif a.cmd == "precedent":
            print(hosp.precedent(a.text))
        elif a.cmd == "release":
            print(hosp.release(a.id, a.head))
        elif a.cmd == "queue":
            print(hosp.queue(a.id, Path(a.brief).read_text(encoding="utf-8"), a.title, a.after))
        elif a.cmd == "accept-floor":
            print(hosp.accept_floor(a.id, a.unit, a.metric, a.value, a.ref))
        elif a.cmd == "allow-paths":
            print(hosp.allow_paths(a.id, a.head, a.paths))
        elif a.cmd == "resolve":
            print(hosp.resolve(a.id, a.stage, a.note, a.reset_rounds))
        elif a.cmd == "hold":
            print(hosp.hold(a.id, not a.off))
    except HospitalError as e:
        print(f"hospital: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
