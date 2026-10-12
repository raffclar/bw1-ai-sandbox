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

    def test_module_level_guard_is_allowed_but_not_defined_in_source(self):
        self.write({"configure.py": 'cflags = ["/DNDEBUG", "/D_LH_3D_LIB_"]\nmp = [*cflags, "/D_LH_MULTIPLAYER_LIB_"]\n'})
        self.base = self.commit()
        for macro in ("_LH_3D_LIB_", "_LH_MULTIPLAYER_LIB_"):
            h = BASE_H.replace("struct Thing {", f"#ifdef {macro}\nstatic int g;\n#endif\nstruct Thing {{")
            self.assertEqual(self.rules({"src/Thing.h": h}), [], macro)
        # a per-header macro stays hard, and so does defining the module guard in a source file
        h = BASE_H.replace("struct Thing {", "#ifdef LAND_CELL_DARKNESS_COLOURS\nstatic int g;\n#endif\nstruct Thing {")
        self.assertIn(("header-toggle", "hard"), self.rules({"src/Thing.h": h}))
        self.assertIn(("header-toggle", "hard"),
                      self.rules({"src/Thing.cpp": '#define _LH_3D_LIB_\n#include "Thing.h"\n\nvoid Thing::Real()\n{\n}\n'}))
        # a _LH_..._LIB_ name configure.py does not define is just a new macro
        h = BASE_H.replace("struct Thing {", "#ifdef _LH_GAME_LIB_\nstatic int g;\n#endif\nstruct Thing {")
        self.assertIn(("header-toggle", "hard"), self.rules({"src/Thing.h": h}))

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

    def fresh(self):
        self.tearDown()
        self.setUp()

    def body(self, text):
        return '#include "Thing.h"\n\nvoid Thing::Real()\n{\n' + text + "}\n"

    def test_define_continuations_are_preprocessor(self):
        # A multi-line #define after the first function is neither a mid-file declaration nor a helper definition
        cpp = self.body("\tint a = 1;\n") + "\n#define SWAP(a, b) \\\n\t{ \\\n\t\tint t = a; \\\n\t\ta = b; \\\n" \
                                            "\t\tb = t; \\\n\t}\n"
        self.assertEqual(self.rules({"src/Thing.cpp": cpp}), [])

    def test_one_sided_if_clamp_is_a_warning(self):
        got = self.rules({"src/Thing.cpp": self.body("\tif (x < lo)\n\t\tx = lo;\n\tUse(x);\n")})
        self.assertEqual(got, [("clamp-if", "warn")])

    def test_two_sided_if_clamp_is_hard(self):
        for second in ("\telse if (x > hi)\n\t\tx = hi;\n", "\tif (x > hi)\n\t\tx = hi;\n"):
            got = self.rules({"src/Thing.cpp": self.body("\tif (x < lo)\n\t\tx = lo;\n" + second)})
            self.assertEqual(got, [("clamp", "hard")], second)

    def test_anti_cheat_constructs(self):
        cases = {
            "inline-asm": "\t__asm\n\t{\n\t\tmov eax, 1\n\t}\n",
            "emit": "\t_asm _emit 0x90\n",
            "byte-code": "\t((void (*)())Code)();\n",
        }
        for rule, text in cases.items():
            self.fresh()
            cpp = '#include "Thing.h"\n\nstatic unsigned char Code[] = {0x90, 0xc3};\n\nvoid Thing::Real()\n{\n' + \
                  text + "}\n"
            self.assertIn((rule, "hard"), self.rules({"src/Thing.cpp": cpp}), rule)
        self.fresh()
        got = self.rules({"src/Thing.cpp": self.body('\t((int (__cdecl *)(void))"\\x33\\xc0\\xc3")();\n')})
        self.assertIn(("byte-code", "hard"), got)
        for pragma in ('#pragma code_seg(".text$a")', '#pragma comment(linker, "/merge:.rdata=.text")',
                       "#pragma alloc_text(x, Real)"):
            self.fresh()
            got = self.rules({"src/Thing.cpp": pragma + "\n" + self.body("")})
            self.assertIn(("pragma-section", "hard"), got, pragma)

    def test_optimisation_pragmas_new_to_the_tree_are_warnings(self):
        self.write({"src/Old.cpp": "#pragma inline_depth(2)\n"})
        self.base = self.commit()
        self.assertEqual(self.rules({"src/Thing.cpp": "#pragma inline_depth(2)\n" + self.body("")}), [])
        self.assertEqual(self.rules({"src/Thing.cpp": "#pragma optimize(\"g\", off)\n" + self.body("")}),
                         [("pragma-opt", "warn")])

    def test_tamper_paths_are_hard_and_escalate(self):
        for path in ("config/BW1W120/symbols.txt", "tools/decomp-diff.py", "configure.py", "objdiff.json",
                     "build.ninja", "build/BW1W120/src/x.o", "AGENTS.md", ".claude/settings.json", ".clang-format",
                     ".github/workflows/build.yml"):
            self.fresh()
            self.write({path: "changed\n"})
            head = self.commit()
            found = Q.run(str(self.t), self.base, head)
            self.assertEqual([(f.rule, f.path) for f in found], [("tamper-path", path)], path)
            self.assertEqual(Q.summary(found)["escalate"], [f"tamper-path: {path}"])
        self.fresh()
        self.write({"tools/matching_ledger.jsonl": "{}\n"})  # no exception: tools/** in full (the Chief, 2026-10-12)
        found = Q.run(str(self.t), self.base, self.commit())
        self.assertEqual([(f.rule, f.path, f.level) for f in found],
                         [("tamper-path", "tools/matching_ledger.jsonl", "hard")])

    def test_inline_asm_only_in_files_that_already_had_it(self):
        asm_cpp = "void P()\n{\n\t__asm cpuid\n}\n"
        self.write({"src/CPUCheck.cpp": asm_cpp, "src/Comment.cpp": '// __asm in a comment\nconst char* s = "_asm";\n'})
        self.base = self.commit()
        # more asm in a file that had asm at the base: allowed, reported as a warning
        got = self.rules({"src/CPUCheck.cpp": "void P()\n{\n\t__asm cpuid\n\t_asm mov eax, 1\n\tasm(\"nop\");\n}\n"})
        self.assertEqual(got, [("inline-asm-existing", "warn")])
        # a file whose base only mentions asm in a comment or a string is not an asm file; Thing.cpp had none
        for path in ("src/Comment.cpp", "src/Thing.cpp", "src/New.cpp"):
            self.fresh()
            self.write({"src/CPUCheck.cpp": asm_cpp,
                        "src/Comment.cpp": '// __asm in a comment\nconst char* s = "_asm";\n'})
            self.base = self.commit()
            got = self.rules({path: "void Q()\n{\n\t__asm nop\n}\n"})
            self.assertIn(("inline-asm", "hard"), got, path)
        # naked, _emit, section pragmas and byte-code stay hard in an asm file too
        for text, rule in (("__declspec(naked) void N()\n{\n}\n", "inline-asm"),
                           ("void E()\n{\n\t__asm _emit 0x90\n}\n", "emit"),
                           ('#pragma code_seg(".x")\n', "pragma-section"),
                           ("static unsigned char C[] = {0xc3};\nvoid B()\n{\n\t((void (*)())C)();\n}\n",
                            "byte-code")):
            self.fresh()
            self.write({"src/CPUCheck.cpp": asm_cpp})
            self.base = self.commit()
            got = self.rules({"src/CPUCheck.cpp": asm_cpp + text})
            self.assertIn((rule, "hard"), got, text)
        # --asm-base: the staged check (whose base is HEAD) takes the case's base for the asm files
        self.fresh()
        base = self.base
        self.write({"src/Thing.cpp": asm_cpp})
        self.commit()  # a later commit (e.g. the worker's own, made with --no-verify) added asm to Thing.cpp
        self.write({"src/Thing.cpp": asm_cpp.replace("cpuid", "cpuid\n\t__asm nop")})
        git(self.t, "add", "-A")
        self.assertEqual(Q.main(["--repo", str(self.t), "--staged"]), 0)  # HEAD has asm there
        self.assertEqual(Q.main(["--repo", str(self.t), "--staged", "--asm-base", base]), 1)

    def test_allow_paths_from_a_revision_and_by_name(self):
        self.write({"config/BW1W120/symbols.txt": SYMBOLS + "merged = .text:0x1;\n"})
        start = self.commit()  # e.g. the case's starting point, whose data change the user merged
        self.write({"src/Thing.cpp": self.body("\tint a = 2;\n")})
        head = self.commit()
        self.assertEqual([f.rule for f in Q.run(str(self.t), self.base, head)], ["tamper-path"])
        self.assertEqual(Q.run(str(self.t), self.base, head, allow_from=start), [])
        self.write({"config/BW1W120/symbols.txt": SYMBOLS + "merged = .text:0x2;\n"})  # changed again after start
        head2 = self.commit()
        self.assertEqual([f.rule for f in Q.run(str(self.t), self.base, head2, allow_from=start)], ["tamper-path"])
        self.assertEqual(Q.run(str(self.t), self.base, head2, allow_from=start,
                               allow_paths=["config/BW1W120/symbols.txt"]), [])

    def test_staged_mode_blocks_tamper_and_cheats(self):
        self.write({"tools/decomp-diff.py": "x\n"})
        git(self.t, "add", "-A")
        self.assertEqual(Q.main(["--repo", str(self.t), "--staged"]), 1)
        git(self.t, "reset", "-q", "--hard")
        self.write({"src/Thing.cpp": self.body("\tint a = 3;\n")})
        git(self.t, "add", "-A")
        self.assertEqual(Q.main(["--repo", str(self.t), "--staged"]), 0)


if __name__ == "__main__":
    unittest.main()
