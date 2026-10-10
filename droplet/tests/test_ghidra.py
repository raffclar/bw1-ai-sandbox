#!/usr/bin/env python3
"""Offline tests for the droplet's Ghidra MCP pieces (stdlib unittest; no Ghidra, no network).

  python3 droplet/tests/test_ghidra.py -v

Covers: GhidraMcpServer.java serves every endpoint the pinned bridge (GhidraMCP 1.4) calls and refuses the
editing ones in read-only mode; the workers' mcp.json names the server `ghidra` and runs the bridge from
/opt/mcp with the vendored packages first on PYTHONPATH; those packages are hash-pinned mcp 1.x; setup-ghidra.sh
pins the bridge to the same GhidraMCP commit; the wrapper reads GHIDRA_PORT.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DROPLET = HERE.parent
sys.path.insert(0, str(DROPLET))
import sandbox_droplet as SD  # noqa: E402

# Every safe_get / safe_post endpoint in bridge_mcp_ghidra.py at GhidraMCP tag 1.4 (commit 27f316f8).
BRIDGE_GET = ["methods", "classes", "segments", "imports", "exports", "namespaces", "data", "searchFunctions",
              "get_function_by_address", "get_current_address", "get_current_function", "list_functions",
              "decompile_function", "disassemble_function", "xrefs_to", "xrefs_from", "function_xrefs", "strings"]
BRIDGE_POST_READ = ["decompile"]
BRIDGE_POST_EDIT = ["renameFunction", "renameData", "renameVariable", "set_decompiler_comment",
                    "set_disassembly_comment", "rename_function_by_address", "set_function_prototype",
                    "set_local_variable_type"]
GHIDRAMCP_COMMIT = "27f316f80139e2d5dec882519a1bdf4aa46ac04c"


class ServerScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (DROPLET / "ghidra" / "GhidraMcpServer.java").read_text(encoding="utf-8")
        # context("/path", ex -> ...) blocks, up to the next context( or the end of register().
        cls.blocks = dict(re.findall(r'context\("/(\w+)",(.*?)(?=\n\t\tcontext\(|\n\t}\n)', cls.src, re.S))

    def test_every_bridge_endpoint_is_served(self):
        for ep in BRIDGE_GET + BRIDGE_POST_READ + BRIDGE_POST_EDIT:
            self.assertIn(ep, self.blocks, ep)

    def test_edits_refused_when_read_only(self):
        for ep in BRIDGE_POST_EDIT:
            self.assertIn("if (readOnly) return READ_ONLY;", self.blocks[ep], ep)
        for ep in BRIDGE_GET + BRIDGE_POST_READ:
            self.assertNotIn("READ_ONLY", self.blocks[ep], ep)

    def test_binds_to_the_given_address_only(self):
        self.assertIn("new InetSocketAddress(InetAddress.getByName(args[0])", self.src)
        self.assertNotIn("new InetSocketAddress(port)", self.src)


class ConfigTests(unittest.TestCase):
    def test_mcp_json(self):
        cfg = json.loads((DROPLET / "ghidra" / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(list(cfg["mcpServers"]), ["ghidra"])
        args = cfg["mcpServers"]["ghidra"]["args"]
        self.assertEqual(args[0], "/opt/mcp/bridge_mcp_ghidra.py")
        self.assertEqual(args[1:], ["--ghidra-server", "http://ghidra.host:8080/"])
        self.assertEqual(cfg["mcpServers"]["ghidra"]["env"]["PYTHONPATH"], "/opt/mcp/pylib")

    def test_bridge_packages_hash_pinned(self):
        lines = [ln for ln in (DROPLET / "ghidra" / "bridge-requirements.txt").read_text(encoding="utf-8")
                 .splitlines() if ln and not ln.startswith("#")]
        names = set()
        for ln in lines:
            self.assertRegex(ln, r"^[a-z0-9-]+==[0-9][^ ]* --hash=sha256:[0-9a-f]{64}$")
            names.add(ln.split("==")[0])
        self.assertIn("mcp==1.", "\n".join(lines))  # the 1.4 bridge imports mcp.server.fastmcp (gone in 2.x)
        self.assertTrue({"mcp", "requests", "pydantic", "pydantic-core", "anyio"} <= names)

    def test_bridge_pinned_to_the_ported_commit(self):
        setup = (DROPLET / "setup-ghidra.sh").read_text(encoding="utf-8")
        self.assertIn(f"GHIDRAMCP_COMMIT={GHIDRAMCP_COMMIT}", setup)
        self.assertIn(GHIDRAMCP_COMMIT, (DROPLET / "ghidra" / "GhidraMcpServer.java").read_text(encoding="utf-8"))
        for var in ("JDK_SHA256", "GHIDRA_SHA256", "BRIDGE_SHA256"):
            self.assertRegex(setup, rf"\n{var}=[0-9a-f]{{64}}\n")

    def test_wrapper_reads_ghidra_port(self):
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d) / "ghidra.conf"
            old = SD.GHIDRA_CONF
            SD.GHIDRA_CONF = conf
            try:
                self.assertIsNone(SD.ghidra_port())
                conf.write_text("GHIDRA_PORT=\nGHIDRA_MODE=rw\n")
                self.assertIsNone(SD.ghidra_port())
                conf.write_text("GHIDRA_PORT=8080\nGHIDRA_MODE=rw\n")
                self.assertEqual(SD.ghidra_port(), 8080)
            finally:
                SD.GHIDRA_CONF = old


if __name__ == "__main__":
    unittest.main()
