#!/usr/bin/env python3
"""Quality gate for bw1-decomp worker changes: the maintainer's review rules (bwrsandman on PR #431) and
AGENTS.md, checked on the C/C++ lines a change ADDS (base..head, or the staged diff).

  quality_gate.py [--repo DIR] [--base REV] (--head REV | --staged) [--json]

Exit status: 0 = no hard failures (warnings may be printed), 1 = hard failures, 2 = usage or git error.

Hard failures (a commit, a review and the publisher refuse them):
  anon-enum       an anonymous `enum { ... }` used as a list of constants
  header-toggle   an #ifdef/#ifndef/#if defined() in a header around declarations, on a macro that is not the
                  include guard and not already used in an #if anywhere in the base tree
  field-name      a new use of a `field_0x..` member in .c/.cpp code
  helper-in-cpp   a new function definition in a .c/.cpp that is not a real symbol (no matching name in
                  config/BW1W*/symbols.txt), i.e. a helper inline that belongs in a shared header
  clamp           a hand-written clamp/min/max (ternary, if-assign, if/else-assign, if-return) where CLAMP from
                  re_common.h or windef.h's min/max fit
  mid-file        an #include, global definition or declaration after the first function body of a .c/.cpp
Warnings (reported, never failing):
  magic-float     a non-trivial float literal in a function body
  matrix-math     element-by-element matrix or vector maths that the inline helpers already provide

It reads git objects only. Nothing from the change is built or run.
"""
import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass

SOURCES = (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".inl", ".inc")
HEADERS = (".h", ".hpp", ".hh", ".inl", ".inc")
CODE = (".c", ".cpp", ".cc", ".cxx")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

HINTS = {
    "anon-enum": "bwr: \"Not really an enum, use a list of `#define` instead\". Turn each enumerator into a "
                 "`#define NAME value` (or give the enum a real type name if it is one).",
    "header-toggle": "bwr: \"This is part of an LLM hacky solution. Avoid this.\" Declare it unconditionally in the "
                     "header (AGENTS.md: globals are static members declared in the owning class's header).",
    "field-name": "bwr: \"If you use fields, you should name them\". Give the member a readable PascalCase name "
                  "in the header before using it (AGENTS.md, Naming: Member variables).",
    "helper-in-cpp": "bwr: \"any utility inline should probably live in a header that's accessible to everyone\". "
                     "A .cpp defines real symbols only (in config/BW1W120/symbols.txt, declared with an address "
                     "comment); move the helper into the owning class's or a shared header, or reuse an existing one.",
    "clamp": "bwr: \"we have a `CLAMP` macro in `re_common.h`\" / \"you can see this as a `life = MAX(0.0f, "
             "currentLife)`\". Use CLAMP (re_common.h) or windef.h's min/max (AGENTS.md: \"Use windef.h's min/max "
             "where the original did\"; tools/minmax-scan.py --verify --apply keeps only byte-identical rewrites).",
    "mid-file": "bwr: \"Is there a reason these are halfway through the file? Is this a different TU?\" Put "
                "includes, globals and declarations at the top of the file, before the first function.",
    "magic-float": "bwr: \"I would declare these magic values at the top of the file using `#define`\".",
    "matrix-math": "bwr: \"Likely this is from an inline matrix function already defined\". Check LHMatrix.h / "
                   "LHPoint.h / LH3DMath.h (SetRotationY, RotateY, SetTranslateOnly, GetNorme, ...) first.",
}
HARD = {"anon-enum", "header-toggle", "field-name", "helper-in-cpp", "clamp", "mid-file"}
# Macros that are fine in an #if even if the base tree has never used them
KNOWN_MACROS = {"__cplusplus", "_MSC_VER", "_WIN32", "WIN32", "NDEBUG", "_DEBUG", "DEBUG", "__MWERKS__", "defined",
                "VERSION_BW1W100", "VERSION_BW1W110", "VERSION_BW1W120", "VERSION_BW1M119"}
TRIVIAL_FLOATS = {0.0, 1.0, -1.0, 2.0, 0.5, -0.5, 0.25, 10.0, 100.0, 1000.0, 255.0, 256.0, 3.0, 4.0}
KEYWORDS = {"if", "for", "while", "switch", "return", "else", "do", "case", "sizeof", "catch", "new", "delete"}


@dataclass
class Finding:
    rule: str
    path: str
    line: int
    text: str
    detail: str = ""

    @property
    def level(self) -> str:
        return "hard" if self.rule in HARD else "warn"

    def format(self) -> str:
        what = f" ({self.detail})" if self.detail else ""
        return f"{self.level.upper()} {self.rule}: {self.path}:{self.line}{what}: {self.text.strip()[:140]}\n" \
               f"    fix: {HINTS[self.rule]}"


class GitError(Exception):
    pass


class Source:
    """Reads the change from git: the files at head (or in the index) and the lines added since base."""

    def __init__(self, repo: str, base: str, head: str = "", staged: bool = False, paths=()):
        self.repo, self.base, self.head, self.staged = repo, base, head, staged
        self.paths = list(paths)

    def git(self, *args, check=True) -> str:
        p = subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if check and p.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {p.stderr.strip()}")
        return p.stdout

    def spec(self, path: str) -> str:
        return f":{path}" if self.staged else f"{self.head}:{path}"

    def show(self, path: str) -> str:
        return self.git("show", self.spec(path), check=False)

    def added(self) -> dict:
        """{path: set(line numbers added at head)} for the C/C++ files of the change."""
        rng = ["--cached", self.base] if self.staged else [f"{self.base}", self.head]
        diff = self.git("diff", "-U0", "--no-color", "--no-renames", "--diff-filter=AM", *rng, "--",
                        *(self.paths or ["*" + s for s in SOURCES]))
        out, path, line = {}, None, 0
        for row in diff.splitlines():
            if row.startswith("+++ "):
                path = row[6:] if row.startswith("+++ b/") else None
                if path:
                    out.setdefault(path, set())
            elif row.startswith("@@"):
                m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", row)
                line = int(m.group(1)) if m else 0
            elif path and row.startswith("+"):
                out[path].add(line)
                line += 1
        return out

    def symbols(self):
        """(qualified names, free names) of every function in config/BW1W*/symbols.txt at head."""
        listing = self.git("ls-files", "--", "config/") if self.staged else \
            self.git("ls-tree", "-r", "--name-only", self.head, "--", "config/", check=False)
        quals, frees = set(), set()
        for f in listing.splitlines():
            if re.fullmatch(r"config/BW1W\d+/symbols\.txt", f):
                for row in self.show(f).splitlines():
                    name = row.split(" = ", 1)[0].strip()
                    q = demangle_key(name)
                    if q:
                        (quals if "::" in q else frees).add(q)
        return quals, frees

    def base_macros(self) -> set:
        """Every identifier used in an #if/#ifdef/#ifndef/#elif anywhere in the base tree."""
        text = self.git("grep", "-h", "-E", r"^\s*#\s*(if|ifdef|ifndef|elif)\b", self.base, "--", "*.h", "*.hpp",
                        "*.c", "*.cpp", "*.inl", check=False)
        found = set()
        for row in text.splitlines():
            cond = re.sub(r"^\s*#\s*(ifdef|ifndef|if|elif)", "", row.split(":", 1)[-1] if ":" in row[:60] and
                          self.base in row[:60] else row)
            found.update(re.findall(r"[A-Za-z_]\w*", cond))
        return found


def demangle_key(name: str) -> str:
    """'Scope::Name' for an MSVC-mangled function name ('Class::Class' for a constructor, 'Class::~Class' for a
    destructor, 'Class::operator' for operators); the bare name for C and unmangled symbols."""
    if name.startswith("??0") or name.startswith("??1"):
        scopes = name[3:].split("@@", 1)[0].split("@")
        if not scopes or not scopes[0]:
            return ""
        cls = scopes[0]
        outer = "::".join(reversed(scopes[1:]))
        prefix = (outer + "::" if outer else "") + cls
        return f"{prefix}::{'~' if name[2] == '1' else ''}{cls}"
    if name.startswith("??"):
        scopes = name[4:].split("@@", 1)[0].split("@")
        return "::".join(reversed([s for s in scopes if s])) + "::operator" if scopes and scopes[0] else ""
    if name.startswith("?"):
        if name.startswith("?$"):
            return ""
        parts = name[1:].split("@@", 1)[0].split("@")
        if not parts[0]:
            return ""
        return "::".join(reversed(parts))
    bare = re.sub(r"@\d+$", "", name.lstrip("_@"))
    return bare if re.fullmatch(r"\w+", bare) else ""


# ------------------------------------------------------------------------------------------------- lexing

def strip_code(lines):
    """Each line with comments and string/char literals blanked (same length), tracking block comments."""
    out, block = [], False
    for line in lines:
        res, i, n = [], 0, len(line)
        while i < n:
            if block:
                j = line.find("*/", i)
                if j < 0:
                    res.append(" " * (n - i))
                    i = n
                else:
                    res.append(" " * (j + 2 - i))
                    i, block = j + 2, False
                continue
            c = line[i]
            if line.startswith("//", i):
                res.append(" " * (n - i))
                break
            if line.startswith("/*", i):
                block = True
                res.append("  ")
                i += 2
                continue
            if c in "\"'":
                j = i + 1
                while j < n and line[j] != c:
                    j += 2 if line[j] == "\\" else 1
                res.append(c + " " * (min(j, n) - i - 1) + (c if j < n else ""))
                i = j + 1
                continue
            res.append(c)
            i += 1
        out.append("".join(res))
    return out


@dataclass
class Function:
    name: str
    start: int  # 1-based line of the signature's first line
    body_open: int
    body_close: int
    signature: str


def scan_toplevel(code):
    """Walk stripped code: (functions, top-level statements) where statements are (first line, text) of every
    declaration/definition ending in ';' at namespace scope, and functions their signature and body lines."""
    functions, statements = [], []
    stack = []  # 'ns' | 'fn' | 'other'
    pending, pending_start = "", 0
    fn = None
    for idx, line in enumerate(code, 1):
        stripped = line.strip()
        top = all(k == "ns" for k in stack)
        if top and stripped.startswith("#"):
            pending, pending_start = "", 0
            continue
        for ch in line:
            top = all(k == "ns" for k in stack)
            if ch == "{":
                if top:
                    sig = pending.strip()
                    kind = classify_block(sig)
                    if kind == "fn":
                        fn = Function(function_name(sig), pending_start or idx, idx, 0, sig)
                    stack.append(kind)
                    pending, pending_start = "", 0
                else:
                    stack.append("other")
            elif ch == "}":
                kind = stack.pop() if stack else "other"
                if kind == "fn" and all(k == "ns" for k in stack) and fn:
                    fn.body_close = idx
                    functions.append(fn)
                    fn = None
                if all(k == "ns" for k in stack):
                    pending, pending_start = "", 0
            elif ch == ";" and top:
                if pending.strip():
                    statements.append((pending_start or idx, pending.strip()))
                pending, pending_start = "", 0
            elif top:
                if not pending.strip() and not ch.isspace():
                    pending_start = idx
                pending += ch
        if top and pending:
            pending += " "
    return functions, statements


def classify_block(sig: str) -> str:
    if not sig:
        return "other"
    if re.match(r"^(namespace\b|extern\s+\"\s*\"\s*$)", sig) or sig.startswith('extern "'):
        return "ns"
    if re.search(r"\b(class|struct|union|enum)\b", sig.split("(")[0]) or "=" in sig.split("(")[0]:
        return "other"
    if "(" in sig and ")" in sig:
        head = sig.split("(")[0].split()
        if head and head[-1] in KEYWORDS:
            return "other"
        name = function_name(sig)
        if not name or re.fullmatch(r"[A-Z0-9_]+", name.split("::")[-1]) and "::" not in name:
            return "other"  # a macro invocation such as BEGIN_MESSAGE_MAP(...)
        return "fn"
    return "other"


def function_name(sig: str) -> str:
    depth, cut = 0, -1
    for i, ch in enumerate(sig):  # the first '(' at template depth 0 that follows a name
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "(" and depth <= 0:
            if re.search(r"operator\s*$", sig[:i]):
                continue
            cut = i
            break
    if cut < 0:
        return ""
    head = sig[:cut].rstrip()
    m = re.search(r"((?:[A-Za-z_]\w*\s*::\s*)*~?\s*(?:operator\s*\S+|[A-Za-z_]\w*))$", head)
    return re.sub(r"\s+", "", m.group(1)) if m else ""


# ------------------------------------------------------------------------------------------------- checks

def check_file(path, text, added, symbols, macros):
    findings = []
    lines = text.splitlines()
    code = strip_code(lines)
    is_header, is_code = path.endswith(HEADERS), path.endswith(CODE)

    def add(rule, ln, detail=""):
        findings.append(Finding(rule, path, ln, lines[ln - 1] if 0 < ln <= len(lines) else "", detail))

    # anon-enum
    for i, c in enumerate(code, 1):
        if re.search(r"(?<!typedef )\benum\s*(\{|$)", c.strip()) and not c.strip().startswith("typedef"):
            nxt = next((code[j].strip() for j in range(i, min(i + 3, len(code))) if code[j].strip()), "")
            if c.strip().endswith("{") or nxt.startswith("{"):
                if i in added:
                    add("anon-enum", i)

    # header-toggle
    if is_header:
        guard = None
        for c in code:
            m = re.match(r"\s*#\s*ifndef\s+(\w+)", c)
            if m:
                guard = m.group(1)
                break
            if c.strip():
                break
        for i, c in enumerate(code, 1):
            if i not in added:
                continue
            m = re.match(r"\s*#\s*(ifdef|ifndef|if|elif)\b(.*)", c)
            if not m:
                continue
            names = set(re.findall(r"[A-Za-z_]\w*", m.group(2))) - {"defined"}
            new = sorted(n for n in names if n != guard and n not in macros and n not in KNOWN_MACROS)
            if new:
                add("header-toggle", i, "new macro " + ", ".join(new))

    # field-name
    if is_code:
        for i, c in enumerate(code, 1):
            if i in added and re.search(r"(\.|->)\s*field_0x[0-9A-Fa-f]+\b", c):
                add("field-name", i)

    functions, statements = scan_toplevel(code)

    # helper-in-cpp
    quals, frees = symbols
    if is_code:
        for f in functions:
            if f.start not in added or "template" in f.signature.split("(")[0]:
                continue
            name = f.name
            if "operator" in name:
                ok = name.split("::operator")[0] + "::operator" in quals if "::" in name else True
            elif "::" in name:
                ok = name in quals
            else:
                ok = name in frees
            if not ok:
                add("helper-in-cpp", f.start, f"{name} is not in config/BW1W*/symbols.txt")

    # mid-file
    if is_code and functions:
        first_end = functions[0].body_close
        for i, c in enumerate(code, 1):
            if i > first_end and i in added and re.match(r"\s*#\s*include\b", c):
                add("mid-file", i, "include")
        for start, stmt in statements:
            if start <= first_end or start not in added:
                continue
            s = re.sub(r"\s+", " ", stmt)
            if re.match(r"^(typedef|using|friend|template|class|struct|union|enum|namespace)\b", s) and \
                    "(" not in s.split("=")[0] or re.match(r"^(class|struct|union) \w+$", s):
                continue
            if re.match(r"^([A-Z0-9_]+|static_assert)\s*\(", s):
                continue  # macro invocation
            add("mid-file", start, "declaration or definition")

    # clamp
    for i, c in enumerate(code, 1):
        if i in added:
            for m in re.finditer(r"([\w.\->\[\]]+(?:\([^()]*\))?)\s*(<=|>=|<|>)\s*([\w.\->\[\]]+(?:\([^()]*\))?)\s*\?"
                                 r"\s*([^:;?]+?)\s*:\s*([^;,?)]+?)\s*(?=[;,)]|$)", c):
                a, b, e1, e2 = (norm(x) for x in (m.group(1), m.group(3), m.group(4), m.group(5)))
                if {a, b} == {e1, e2} and a != b:
                    add("clamp", i, "ternary min/max")
    findings += check_if_selects(path, lines, code, added)

    # warnings
    if is_code:
        body_lines = set()
        for f in functions:
            body_lines.update(range(f.body_open + 1, f.body_close))
        for i in sorted(body_lines & added):
            c = code[i - 1]
            if re.match(r"\s*#", c) or re.search(r"\bconst\b|\bstatic\b", c):
                continue
            lits = [x for x in re.findall(r"(?<![\w.])(-?\d+\.\d*f?|-?\.\d+f?)(?![\w.])", c)
                    if float(x.rstrip("f")) not in TRIVIAL_FLOATS]
            if lits:
                add("magic-float", i, ", ".join(lits[:4]))
        runs = {}
        for i in sorted(added):
            c = code[i - 1] if i <= len(code) else ""
            m = re.search(r"(\w+)\s*(?:\.|->)\s*(_[1-4][1-4]|m\s*\[\s*\d+\s*\])\s*=[^=]", c)
            if m:
                runs.setdefault(m.group(1), []).append(i)
        for var, rows in runs.items():
            if len(rows) >= 4:
                add("matrix-math", rows[0], f"{len(rows)} element stores into {var}")
        for i in sorted(added):
            c = code[i - 1] if i <= len(code) else ""
            if re.search(r"(\w+)\.x\s*\*\s*\w+\.x\s*\+\s*\w+\.y\s*\*\s*\w+\.y\s*\+\s*\w+\.z\s*\*\s*\w+\.z", c):
                add("matrix-math", i, "hand-written dot product or length")
    return findings


def norm(expr: str) -> str:
    e = re.sub(r"\s+", "", expr)
    while e.startswith("(") and e.endswith(")") and balanced(e[1:-1]):
        e = e[1:-1]
    return e


def balanced(e: str) -> bool:
    d = 0
    for ch in e:
        d += ch == "("
        d -= ch == ")"
        if d < 0:
            return False
    return d == 0


COND_RE = re.compile(r"^\s*(?:else\s+)?if\s*\((.*)\)\s*(\{)?\s*(.*)$")


def parse_cond(text: str):
    m = re.fullmatch(r"\s*(.+?)\s*(<=|>=|<|>)\s*(.+?)\s*", text)
    if not m or re.search(r"&&|\|\||[<>]=?.*[<>]", m.group(1) + m.group(3)):
        return None
    return norm(m.group(1)), norm(m.group(3))


def block_after(code, i):
    """(first line, last line, statements) of the statement or braced block following line i (1-based) of an
    if/else; statements are stripped texts of its lines."""
    j = i  # index of the next line (0-based == i)
    while j < len(code) and not code[j].strip():
        j += 1
    if j >= len(code):
        return None
    if code[j].strip() == "{":
        depth, k = 0, j
        while k < len(code):
            depth += code[k].count("{") - code[k].count("}")
            if depth == 0:
                return j + 1, k + 1, [code[x].strip() for x in range(j + 1, k)]
            k += 1
        return None
    return j + 1, j + 1, [code[j].strip()]


def check_if_selects(path, lines, code, added):
    out = []
    for i, c in enumerate(code, 1):
        if i not in added:
            continue
        m = COND_RE.match(c)
        if not m or m.group(2) or m.group(3):
            continue
        ops = parse_cond(m.group(1))
        if not ops:
            continue
        a, b = ops
        blk = block_after(code, i)
        if not blk:
            continue
        _, last, body = blk
        body = [s for s in body if s]
        assign = lambda s, x, y: re.fullmatch(r"(.+?)\s*=\s*(.+?);", s) and \
            (norm(re.fullmatch(r"(.+?)\s*=\s*(.+?);", s).group(1)), norm(re.fullmatch(r"(.+?)\s*=\s*(.+?);", s).group(2))) == (x, y)
        # if (a < b) x = (the other one); with x one of the two operands
        if len(body) == 1 and (assign(body[0], a, b) or assign(body[0], b, a)):
            out.append(Finding("clamp", path, i, lines[i - 1], "if-assign min/max"))
            continue
        # if (a < b) return a|b; return (the other one);
        rets = [norm(r.group(1)) for r in (re.fullmatch(r"return\s+(.+?);", s) for s in body) if r]
        nxt = next((code[k].strip() for k in range(last, len(code)) if code[k].strip()), "")
        r2 = re.fullmatch(r"return\s+(.+?);", nxt)
        if len(body) == 1 and rets and r2 and {rets[0], norm(r2.group(1))} == {a, b}:
            out.append(Finding("clamp", path, i, lines[i - 1], "if-return min/max"))
            continue
        # if (a < b) { ... x = a|b; } else { x = (the other one); }
        if nxt == "else":
            k = next(k for k in range(last, len(code)) if code[k].strip() == "else")
            eblk = block_after(code, k + 1)
            if eblk:
                tgt = {}
                for s in body:
                    am = re.fullmatch(r"(.+?)\s*=\s*(.+?);", s)
                    if am and norm(am.group(2)) in (a, b):
                        tgt[norm(am.group(1))] = norm(am.group(2))
                for s in eblk[2]:
                    am = re.fullmatch(r"(.+?)\s*=\s*(.+?);", s)
                    if am and norm(am.group(1)) in tgt and {tgt[norm(am.group(1))], norm(am.group(2))} == {a, b}:
                        out.append(Finding("clamp", path, i, lines[i - 1], "if/else min/max"))
                        break
    return out


def run(repo: str, base: str, head: str = "", staged: bool = False, paths=()):
    src = Source(repo, base, head, staged, paths)
    added = src.added()
    if not added:
        return []
    symbols = src.symbols() if any(p.endswith(CODE) for p in added) else (set(), set())
    macros = src.base_macros() if any(p.endswith(HEADERS) for p in added) else set()
    findings = []
    for path, rows in sorted(added.items()):
        if rows:
            findings += check_file(path, src.show(path), rows, symbols, macros)
    return findings


def summary(findings) -> dict:
    return {"hard": [asdict(f) | {"level": f.level, "hint": HINTS[f.rule]} for f in findings if f.level == "hard"],
            "warn": [asdict(f) | {"level": f.level, "hint": HINTS[f.rule]} for f in findings if f.level == "warn"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default="", help="default: merge-base of origin/main and HEAD")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--staged", action="store_true", help="check the staged diff against --base (default HEAD)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("paths", nargs="*", help="limit the check to these paths")
    a = ap.parse_args(argv)
    try:
        base = a.base
        if not base:
            if a.staged:
                base = "HEAD"
            else:
                base = subprocess.run(["git", "-C", a.repo, "merge-base", "origin/main", a.head], capture_output=True,
                                      text=True).stdout.strip()
                if not base:
                    print("quality_gate: no --base and no merge base with origin/main", file=sys.stderr)
                    return 2
        findings = run(a.repo, base, a.head, a.staged, a.paths)
    except GitError as e:
        print(f"quality_gate: {e}", file=sys.stderr)
        return 2
    hard = [f for f in findings if f.level == "hard"]
    if a.json:
        print(json.dumps(summary(findings), indent=2))
    else:
        for f in findings:
            print(f.format())
        print(f"quality gate: {len(hard)} hard failure(s), {len(findings) - len(hard)} warning(s)")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
