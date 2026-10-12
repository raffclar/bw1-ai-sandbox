#!/usr/bin/env python3
"""Offline tests for droplet/quality_gate.py (stdlib unittest; needs git).

  python3 droplet/tests/test_quality_gate.py -v
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import quality_gate as Q  # noqa: E402

G = ["git", "-c", "user.name=t", "-c", "user.email=t@e", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
SYMBOLS = "?Real@Thing@@QAEXXZ = .text:0x00401000; // type:function size:0x10\n"
BASE_H = "#ifndef BW1_DECOMP_THING_INCLUDED_H\n#define BW1_DECOMP_THING_INCLUDED_H\nstruct Thing { void Real(); };\n#endif\n"


def git(repo, *a):
    return subprocess.run([*G, "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


class GateTests(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="gatetest-"))
        git(self.t, "init", "-q")
        self.write({"config/BW1W120/symbols.txt": SYMBOLS, "src/Thing.h": BASE_H,
                    "src/Thing.cpp": '#include "Thing.h"\n\nvoid Thing::Real()\n{\n}\n'})
        self.base = self.commit()

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def write(self, files):
        for p, c in files.items():
            (self.t / p).parent.mkdir(parents=True, exist_ok=True)
            (self.t / p).write_text(c)

    def commit(self):
        git(self.t, "add", "-A")
        git(self.t, "commit", "-qm", "x")
        return git(self.t, "rev-parse", "HEAD")

    def rules(self, files):
        self.write(files)
        head = self.commit()
        return sorted({(f.rule, f.level) for f in Q.run(str(self.t), self.base, head)})

    def test_clean_change_passes(self):
        self.assertEqual(self.rules({"src/Thing.cpp": '#include "Thing.h"\n\nvoid Thing::Real()\n{\n\tint a = 1;\n}\n'}), [])

    def test_anonymous_enum(self):
        self.assertIn(("anon-enum", "hard"), self.rules({"src/Thing.h": BASE_H.replace("struct", "enum\n{\n\tTHING_COUNT = 8,\n};\nstruct")}))

    def test_header_toggle(self):
        h = BASE_H.replace("struct Thing {", "#ifdef THING_EXTRA\nstatic int g;\n#endif\nstruct Thing {")
        self.assertIn(("header-toggle", "hard"), self.rules({"src/Thing.h": h}))

    def test_field_helper_clamp_midfile(self):
        cpp = ('#include "Thing.h"\n\nvoid Thing::Real()\n{\n\tthis->field_0x10 = v > 1.5f ? 1.5f : v;\n}\n\n'
               "static float Late = 2.0f;\n\ninline float Helper(float x)\n{\n\treturn x;\n}\n")
        got = self.rules({"src/Thing.cpp": cpp})
        for rule in ("field-name", "clamp", "mid-file", "helper-in-cpp"):
            self.assertIn((rule, "hard"), got)

    def test_warnings_only(self):
        cpp = '#include "Thing.h"\n\nvoid Thing::Real()\n{\n\tfloat p = q * 13.82f;\n}\n'
        self.assertEqual(self.rules({"src/Thing.cpp": cpp}), [("magic-float", "warn")])


if __name__ == "__main__":
    unittest.main()
