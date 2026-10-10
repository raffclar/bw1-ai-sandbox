#!/usr/bin/env python3
"""Run bw1-ai-sandbox with the DigitalOcean droplet standing in for its libvirt/KVM VM.

bw1-ai-sandbox (https://github.com/bwrsandman/bw1-ai-sandbox) talks to "the VM" in two ways:

* libvirt (create, lock down, start/stop, network filter), and
* ssh as `decomp@<vm ip>` (paramiko), which carries everything else: vm_agent.py runs as root through
  sudo, SFTP for files and logs, git and rsync for sync/fetch, and the Docker SDK (dockervm.py).

On the droplet there is no VM. The droplet has a local `decomp` user set up exactly like the VM's
(cloud-init.yaml.in), so the ssh path works unchanged against 127.0.0.1. Only the libvirt-facing methods
are replaced, by patching the `Sandbox` class in place before any of the sandbox's own front ends
(sandbox.py, portal.py, mcp_server.py) run. Nothing in the sandbox checkout is modified.

Every change, so it can be offered upstream or rebased (see also README "Changes to bw1-ai-sandbox"):

  method          upstream (VM)                          droplet
  --------------  -------------------------------------  ------------------------------------------------
  vm_state        libvirt domain state                   "running" once provisioning made /srv/decomp
  vm_ip           DHCP lease of the VM                   127.0.0.1 (local sshd, decomp user)
  host_ip         libvirt "default" network address      docker0 gateway (only used for ghidra.host)
  locked          nwfilter attached to the VM            bw1-egress check (DOCKER-USER rules on docker0)
  setup_host      enables libvirt sockets                no-op (provisioning did the host setup)
  create          virt-install + cloud-init + image      push_bin + build_image + lockdown (no VM)
  build_image     refuses while locked; default network  host network for the build (docker0 is locked)
  lockdown        defines + attaches the nwfilter        sudo bw1-egress apply, then check
  unlock          detaches the nwfilter                  refused (edit the firewall by hand if needed)
  check           probes from inside the VM              probes from a throwaway worker-image container
  start/shutdown  libvirt domain start/stop              refused (the droplet is the VM)
  destroy         deletes the VM                         refused (terraform destroy / teardown.ps1)
  ghidra_forward  forwards to host Ghidra                refused (Ghidra is off on the droplet)
  portal          any --host with --pants-down-mode      localhost only; --pants-down-mode refused

Usage (as the `sandbox` user, from anywhere):
  sandbox_droplet.py sandbox <sandbox.py args...>   e.g. `sandbox spawn farmer "..." -m opus`
  sandbox_droplet.py portal [--port 8765] [--no-autoresume]
  sandbox_droplet.py mcp                             the orchestrator's MCP server (stdio)
  sandbox_droplet.py export --out FILE               fetch finished workers, write JSON for the publisher
"""

import json
import os
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SANDBOX_DIR = Path(os.environ.get("SANDBOX_DIR", "/opt/bw1-sandbox/bw1-ai-sandbox"))
os.environ.setdefault("SANDBOX_CONFIG", str(SANDBOX_DIR / "sandbox.droplet.toml"))
EGRESS = ["sudo", "-n", "/usr/local/sbin/bw1-egress"]
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# Probes run from a container on docker0, like `check` runs them from inside the VM upstream.
PROBE_CODE = r"""
import json, socket, sys
res = {}
for host, port in json.loads(sys.argv[1]):
    try:
        with socket.create_connection((host, port), timeout=5):
            res[f"{host}:{port}"] = True
    except OSError:
        res[f"{host}:{port}"] = False
print(json.dumps(res))
"""


def _load_sandbox():
    sys.path.insert(0, str(SANDBOX_DIR))
    import sandbox  # noqa: E402  (bw1-ai-sandbox's own module)
    return sandbox


def patch(sb_mod) -> None:
    """Replace the libvirt-facing methods of sandbox.Sandbox in place."""
    S, Err = sb_mod.Sandbox, sb_mod.SandboxError

    def vm_state(self) -> str:
        return "running" if Path("/srv/decomp/mirror.git").is_dir() else "absent"

    def vm_ip(self) -> str:
        return "127.0.0.1"

    def host_ip(self) -> str:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "docker0"], capture_output=True, text=True).stdout
        for word in out.split():
            if "/" in word and word[0].isdigit():
                return word.split("/")[0]
        return "172.17.0.1"

    def locked(self) -> bool:
        return subprocess.run(EGRESS + ["check"], capture_output=True).returncode == 0

    def setup_host(self) -> str:
        return "droplet: host setup is done by provisioning (setup-droplet.sh)"

    def create(self) -> str:
        if self.vm_state() != "running":
            raise Err("droplet not provisioned: /srv/decomp/mirror.git is missing (run setup-droplet.sh)")
        self.wait_ssh(120)
        self.exec(["sudo", "install", "-d", "-o", "decomp", "-g", "decomp", sb_mod.BIN])
        self.push_bin()
        self.build_image()
        return self.lockdown()

    def build_image(self) -> str:
        # Upstream builds on the VM's (then open) network. Here docker0 is locked for workers, so the build
        # uses the host network instead; the workers themselves still run on docker0.
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for f in ("Dockerfile", "worker_entry.py"):
                tar.add(str(sb_mod.HERE / f), arcname=f)
        buf.seek(0)
        self.docker.images.build(fileobj=buf, custom_context=True, tag=sb_mod.IMAGE, rm=True, forcerm=True,
                                 network_mode="host", buildargs={"PACKAGES": " ".join(self.cfg.packages)})
        self.docker.api.prune_builds()
        return "worker image built"

    def lockdown(self) -> str:
        p = subprocess.run(EGRESS + ["apply"], capture_output=True, text=True)
        if p.returncode != 0:
            raise Err(f"bw1-egress apply failed: {p.stderr.strip()[-800:]}")
        return self.check()

    def check(self) -> str:
        if not self.locked():
            raise Err("worker egress rules are not in place (sudo bw1-egress apply)")
        import socket
        api = (self.api_ips() or [sb_mod.FALLBACK_API_IP])[0]
        try:
            github = socket.getaddrinfo("github.com", 443, socket.AF_INET)[0][4][0]
        except OSError:
            github = sb_mod.FALLBACK_GITHUB_IP
        gw = self.host_ip()
        leaks = sb_mod.LEAK_PROBES + [(github, 443), ("169.254.169.254", 80), (gw, 22)]
        out = self.docker.containers.run(
            sb_mod.IMAGE, entrypoint=["python3", "-c", PROBE_CODE, json.dumps(leaks + [(api, 443)])],
            remove=True, user="2000:2000", cap_drop=["ALL"], security_opt=["no-new-privileges"], read_only=True,
            dns=["127.0.0.1"])
        res = json.loads(out.decode().strip().splitlines()[-1])
        api_key = f"{api}:443"
        bad = [k for k, ok in res.items() if ok and k != api_key]
        if bad:
            raise Err(f"LEAK: a worker container reached {', '.join(bad)}")
        if not res.get(api_key):
            raise Err(f"a worker container cannot reach api.anthropic.com ({api})")
        blocked = ", ".join(k for k in res if k != api_key)
        return (f"lockdown OK (droplet): {blocked} blocked from workers (github, DO metadata, host sshd included); "
                f"api.anthropic.com ({api}) reachable")

    def refuse(what: str):
        def method(self, *a, **k):
            raise Err(f"{what} is not available on the droplet: {REFUSALS[what]}")
        return method

    REFUSALS = {
        "unlock": "the worker firewall stays on; change /usr/local/sbin/bw1-egress by hand if you must",
        "start": "the droplet is the VM; it is always on",
        "shutdown": "the droplet is the VM; stop the services or tear the droplet down",
        "destroy": "use terraform destroy (teardown.ps1)",
        "ghidra_forward": "there is no Ghidra on the droplet",
        "_set_filter": "there is no libvirt network filter",
        "_restart": "there is no VM to restart",
    }
    for name in REFUSALS:
        setattr(S, name, refuse(name))
    for fn in (vm_state, vm_ip, host_ip, locked, setup_host, create, build_image, lockdown, check):
        setattr(S, fn.__name__, fn)


def export(sb_mod, out_path: Path) -> int:
    """Fetch every finished worker into the sandbox's clone (sandbox/<name>, the sandbox's own `fetch`) and
    describe it for the publisher. Running workers are left alone."""
    sb = sb_mod.Sandbox()
    workers = []
    for w in sb.overview()["workers"]:
        entry = {k: w.get(k) for k in ("name", "state", "status", "model", "run_model", "effort", "limited")}
        result = w.get("result") or {}
        entry["result"] = {k: result.get(k) for k in ("subtype", "text", "turns", "cost", "error")}
        entry["prompt"] = (w.get("prompt") or "")[:2000]
        if w.get("state") == "exited":
            try:
                sb.fetch([w["name"]])
                repo = str(sb.repo)
                head = sb_mod.out(sb_mod.run(["git", "-C", repo, "rev-parse", f"sandbox/{w['name']}"]))
                base = sb.base_of(w["name"])
                count = sb_mod.out(sb_mod.run(["git", "-C", repo, "rev-list", "--count", f"{base}..{head}"]))
                entry.update(head=head, base=base, commits=int(count), ref=f"refs/remotes/sandbox/{w['name']}")
            except sb_mod.SandboxError as e:
                entry["fetch_error"] = str(e)
        workers.append(entry)
    doc = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "repo": str(sb.repo),
           "workers": workers}
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o640)
    os.replace(tmp, out_path)
    print(f"exported {len(workers)} worker(s) to {out_path}")
    return 0


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    sb_mod = _load_sandbox()
    patch(sb_mod)
    if cmd == "sandbox":
        sys.argv = [str(SANDBOX_DIR / "sandbox.py")] + rest
        if rest[:1] == ["portal"]:
            return main(["portal"] + rest[1:])
        return sb_mod.main()
    if cmd == "portal":
        if "--pants-down-mode" in rest:
            print("refused: --pants-down-mode is never used on the droplet", file=sys.stderr)
            return 2
        hosts = [rest[i + 1] for i, a in enumerate(rest[:-1]) if a == "--host"]
        hosts += [a.split("=", 1)[1] for a in rest if a.startswith("--host=")]
        if any(h not in LOOPBACK for h in hosts):
            print("refused: the portal only listens on localhost; reach it through an SSH tunnel", file=sys.stderr)
            return 2
        sys.argv = [str(SANDBOX_DIR / "portal.py"), "--host", "127.0.0.1"] + rest
        runpy.run_path(str(SANDBOX_DIR / "portal.py"), run_name="__main__")
        return 0
    if cmd == "mcp":
        sys.argv = [str(SANDBOX_DIR / "mcp_server.py")]
        runpy.run_path(str(SANDBOX_DIR / "mcp_server.py"), run_name="__main__")
        return 0
    if cmd == "export":
        if rest[:1] != ["--out"] or len(rest) != 2:
            print("usage: sandbox_droplet.py export --out FILE", file=sys.stderr)
            return 2
        try:
            return export(sb_mod, Path(rest[1]))
        except sb_mod.SandboxError as e:
            print(f"sandbox: {e}", file=sys.stderr)
            return 1
    print(f"unknown command {cmd!r}; see --help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
