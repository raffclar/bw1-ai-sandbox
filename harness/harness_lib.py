#!/usr/bin/env python3
"""Helpers for bw1-harness.sh: choose work, judge results, track limits, write PR bodies.

Only the Python standard library is used, so it runs on the droplet and on a dev machine
(dry runs) alike. Nothing here talks to the network.

Subcommands
  pick      choose one unit and up to N functions from an objdiff report
  compare   compare a baseline report with the post-run report for one unit
  limits    check / record the per-day PR and spend counters
  cost      sum total_cost_usd and detect usage/rate limits in a Claude Code transcript
  prbody    render the pull-request body
  paths     check that changed files stay inside the allowed paths
  render    fill the agent prompt template from a pick result
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import sys
from pathlib import Path


def _load(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save(path: str | Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _units(report: dict) -> dict:
    return {u["name"]: u for u in report.get("units", [])}


def _functions(unit: dict) -> dict:
    return {f["name"]: f for f in unit.get("functions", []) or []}


# --- pick -------------------------------------------------------------------------------


def cmd_pick(args: argparse.Namespace) -> int:
    report = _load(args.report)
    objdiff = _load(args.objdiff) if args.objdiff and Path(args.objdiff).exists() else {"units": []}
    base_paths = {u["name"]: u.get("base_path") for u in objdiff.get("units", [])}
    state = _load(args.state) if args.state and Path(args.state).exists() else {}
    attempts = state.get("attempts", {})
    cooldown = dt.timedelta(days=args.cooldown_days)
    now = dt.datetime.now(dt.timezone.utc)
    skip_units = {s.strip() for s in (args.skip_units or "").split(",") if s.strip()}
    repo = Path(args.repo) if args.repo else None

    def recently_tried(key: str) -> bool:
        when = attempts.get(key)
        if not when:
            return False
        try:
            return now - dt.datetime.fromisoformat(when) < cooldown
        except ValueError:
            return False

    candidates = []
    for unit in report.get("units", []):
        meta = unit.get("metadata") or {}
        src = meta.get("source_path")
        if not src or meta.get("auto_generated") or meta.get("complete"):
            continue
        if not unit["name"].startswith(args.unit_prefix):
            continue
        if any(fnmatch.fnmatch(unit["name"], pat) for pat in args.exclude_unit or []):
            continue
        if unit["name"] in skip_units:
            continue
        if repo is not None and not (repo / src).exists():
            continue
        for fn in unit.get("functions", []) or []:
            size = int(_num(fn.get("size")))
            pct = _num(fn.get("fuzzy_match_percent"), 0.0)
            if pct >= 100.0 or size < args.min_size or size > args.max_size:
                continue
            if pct < args.min_fuzzy or pct > args.max_fuzzy:
                continue
            if recently_tried(f'{unit["name"]}::{fn["name"]}'):
                continue
            # Closer to done and smaller first: remaining bytes to explain.
            remaining = size * (100.0 - pct) / 100.0
            candidates.append((remaining, -pct, unit, fn, size, pct, src))

    if not candidates:
        print(json.dumps({"unit": None, "reason": "no candidates"}))
        return 3

    candidates.sort(key=lambda c: (c[0], c[1]))
    best_unit = candidates[0][2]
    picked = [c for c in candidates if c[2] is best_unit][: args.max_functions]
    out = {
        "unit": best_unit["name"],
        "source_path": picked[0][6],
        "object": base_paths.get(best_unit["name"]),
        "functions": [
            {
                "name": c[3]["name"],
                "demangled": (c[3].get("metadata") or {}).get("demangled_name", c[3]["name"]),
                "size": c[4],
                "fuzzy": round(c[5], 2),
            }
            for c in picked
        ],
    }
    print(json.dumps(out, indent=2))
    return 0


# --- compare ----------------------------------------------------------------------------


def cmd_compare(args: argparse.Namespace) -> int:
    base = _units(_load(args.baseline)).get(args.unit)
    new = _units(_load(args.report)).get(args.unit)
    if base is None or new is None:
        print(json.dumps({"improved": False, "reason": f"unit {args.unit} missing from a report"}))
        return 1
    bm, nm = base.get("measures", {}), new.get("measures", {})
    bf, nf = _functions(base), _functions(new)
    changes, newly_matched, worse = [], [], []
    for name, fn in nf.items():
        before = _num((bf.get(name) or {}).get("fuzzy_match_percent"), 0.0)
        after = _num(fn.get("fuzzy_match_percent"), 0.0)
        if abs(after - before) < 1e-6:
            continue
        entry = {
            "name": name,
            "demangled": (fn.get("metadata") or {}).get("demangled_name", name),
            "size": int(_num(fn.get("size"))),
            "before": round(before, 2),
            "after": round(after, 2),
        }
        changes.append(entry)
        if after >= 100.0 > before:
            newly_matched.append(entry)
        if after < before:
            worse.append(entry)
    result = {
        "unit": args.unit,
        "matched_code_before": int(_num(bm.get("matched_code"))),
        "matched_code_after": int(_num(nm.get("matched_code"))),
        "fuzzy_before": round(_num(bm.get("fuzzy_match_percent")), 3),
        "fuzzy_after": round(_num(nm.get("fuzzy_match_percent")), 3),
        "newly_matched": newly_matched,
        "worse": worse,
        "changes": sorted(changes, key=lambda c: c["after"] - c["before"], reverse=True),
    }
    gain = result["fuzzy_after"] - result["fuzzy_before"]
    result["improved"] = (
        not worse
        and result["matched_code_after"] >= result["matched_code_before"]
        and (bool(newly_matched) or gain >= args.min_fuzzy_gain)
    )
    print(json.dumps(result, indent=2))
    return 0 if result["improved"] else 1


# --- limits -----------------------------------------------------------------------------


def cmd_limits(args: argparse.Namespace) -> int:
    state = _load(args.state) if Path(args.state).exists() else {}
    day = state.setdefault("days", {}).setdefault(_today(), {"prs": 0, "usd": 0.0, "runs": 0})
    if args.action == "check":
        reasons = []
        if day["prs"] >= args.max_prs_per_day:
            reasons.append(f'PR limit reached ({day["prs"]}/{args.max_prs_per_day} today)')
        # Dollar caps apply to API-key billing only; under a subscription the figure is an estimate.
        if args.enforce_usd and day["usd"] >= args.max_usd_per_day:
            reasons.append(f'spend limit reached (${day["usd"]:.2f}/${args.max_usd_per_day:.2f} today)')
        until = state.get("backoff_until")
        if until:
            try:
                if dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(until):
                    reasons.append(f"usage/rate-limit back-off until {until}")
            except ValueError:
                pass
        if day["runs"] >= args.max_runs_per_day:
            reasons.append(f'run limit reached ({day["runs"]}/{args.max_runs_per_day} today)')
        print(json.dumps({"ok": not reasons, "reasons": reasons, "today": day, "enforce_usd": args.enforce_usd}))
        return 0 if not reasons else 1
    if args.action == "run":
        day["runs"] += 1
    elif args.action == "pr":
        day["prs"] += 1
    elif args.action == "spend":
        day["usd"] = round(day["usd"] + float(args.amount or 0.0), 4)
    elif args.action == "backoff":
        until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=args.minutes or 0)
        if args.reset_epoch:
            reset = dt.datetime.fromtimestamp(args.reset_epoch, dt.timezone.utc)
            until = max(until, reset)
        state["backoff_until"] = until.isoformat()
    elif args.action == "attempt":
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        for key in args.keys or []:
            state.setdefault("attempts", {})[key] = stamp
    _save(args.state, state)
    print(json.dumps({"today": day}))
    return 0


# --- cost -------------------------------------------------------------------------------


# Text Claude Code / the API produce when a subscription usage limit or a rate limit is hit,
# e.g. "Claude AI usage limit reached|1760140800", "rate_limit_error", HTTP 429.
LIMIT_PATTERN = re.compile(
    r"usage limit|rate[ _-]?limit|rate_limit_error|limit reached|too many requests|\b429\b|quota",
    re.IGNORECASE,
)
RESET_PATTERN = re.compile(r"limit reached\|(\d{10})")


def cmd_cost(args: argparse.Namespace) -> int:
    """Claude Code's stream-json ends with a {"type": "result", "total_cost_usd": ...} line.

    Also reports whether the run stopped on a usage or rate limit, from the final result, error
    messages in the stream, or the agent's stderr.
    """
    total, turns, subtype, is_error = 0.0, None, None, False
    limit_text, reset_epoch = "", None
    texts, result_text = [], ""
    path = Path(args.transcript)
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "result":
                total = _num(msg.get("total_cost_usd"), total)
                turns = msg.get("num_turns", turns)
                subtype = msg.get("subtype", subtype)
                is_error = bool(msg.get("is_error", is_error))
                result_text = str(msg.get("result", ""))
            elif msg.get("type") == "error" or msg.get("error"):
                texts.append(json.dumps(msg))
    failed = is_error or args.exit_code not in (None, 0)
    if failed and args.stderr and Path(args.stderr).exists():
        texts.append(Path(args.stderr).read_text(encoding="utf-8", errors="replace")[-4000:])
    # Only error messages, and the final result and stderr of a failed run, are searched: the
    # agent's own conversation may mention "limit" for unrelated reasons.
    if failed:
        texts.append(result_text)
    for text in texts:
        match = LIMIT_PATTERN.search(text)
        if match:
            limit_text = text[max(0, match.start() - 80) : match.end() + 80].replace("\n", " ")
            reset = RESET_PATTERN.search(text)
            if reset:
                reset_epoch = int(reset.group(1))
            break
    print(
        json.dumps(
            {
                "usd": round(total, 4),
                "turns": turns,
                "subtype": subtype,
                "is_error": is_error,
                "rate_limited": bool(limit_text),
                "limit_text": limit_text,
                "reset_epoch": reset_epoch,
            }
        )
    )
    return 0


# --- prbody -----------------------------------------------------------------------------


def cmd_prbody(args: argparse.Namespace) -> int:
    cmp_ = _load(args.compare)
    summary = ""
    if args.summary and Path(args.summary).exists():
        summary = Path(args.summary).read_text(encoding="utf-8", errors="replace").strip()
    lines = [
        f'Automated matching run on `{cmp_["unit"]}` (BW1W120).',
        "",
        "| | before | after |",
        "|---|---|---|",
        f'| unit fuzzy match | {cmp_["fuzzy_before"]}% | {cmp_["fuzzy_after"]}% |',
        f'| matched code bytes | {cmp_["matched_code_before"]} | {cmp_["matched_code_after"]} |',
        "",
    ]
    if cmp_["changes"]:
        lines += ["### Functions", "", "| function | size | before | after |", "|---|---|---|---|"]
        for c in cmp_["changes"]:
            name = c["demangled"].replace("|", "\\|")
            lines.append(f'| `{name}` | {c["size"]} | {c["before"]}% | {c["after"]}% |')
        lines.append("")
    if summary:
        lines += ["### Agent notes", "", summary[:6000], ""]
    lines += [
        "### Verification",
        "",
        "Run on the harness droplet before this PR was opened:",
        "",
        "- `ninja baseline` on the base commit, before any edit",
        "- `python tools/decomp-verify.py --changed --run --baseline build/BW1W120/baseline.json` "
        "(whole-file clang-format, original compiler replay, full build with SHA-1 checks, report refresh, "
        "regression audit)",
        "- `python tools/decomp-regress.py --fail-on-regression` against the same baseline: no regressions",
        "",
        f"Run id `{args.run_id}`, model `{args.model}`, agent cost ${args.cost}.",
        "",
        "Review note: this was written by an AI agent without human review. Check names, "
        "fabricated code and TODOs against AGENTS.md before merging.",
        "",
        "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)",
    ]
    print("\n".join(lines))
    return 0


# --- paths ------------------------------------------------------------------------------


def cmd_paths(args: argparse.Namespace) -> int:
    allowed = [p.strip() for p in args.allowed.split(",") if p.strip()]
    bad = []
    for line in sys.stdin.read().splitlines():
        path = line.strip()
        if path and not any(fnmatch.fnmatch(path, pat) for pat in allowed):
            bad.append(path)
    print(json.dumps({"ok": not bad, "outside": bad}))
    return 0 if not bad else 1


# --- render -----------------------------------------------------------------------------


def cmd_render(args: argparse.Namespace) -> int:
    work = _load(args.work)
    text = Path(args.template).read_text(encoding="utf-8")
    functions = "\n".join(
        f'  - `{f["demangled"]}` (symbol `{f["name"]}`, {f["size"]} bytes, {f["fuzzy"]}%)' for f in work["functions"]
    )
    values = {
        "UNIT": work["unit"],
        "SOURCE": work["source_path"],
        "OBJECT": work.get("object") or "(see objdiff.json base_path)",
        "FUNCTIONS": functions,
        "CYCLES": str(args.cycles),
        "SUMMARY": args.summary,
    }
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    sys.stdout.write(text)
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pick")
    p.add_argument("--report", required=True)
    p.add_argument("--objdiff")
    p.add_argument("--repo")
    p.add_argument("--state")
    p.add_argument("--unit-prefix", default="runblack/")
    p.add_argument("--exclude-unit", action="append")
    p.add_argument("--skip-units", default="")
    p.add_argument("--min-size", type=int, default=16)
    p.add_argument("--max-size", type=int, default=600)
    p.add_argument("--min-fuzzy", type=float, default=50.0)
    # Functions sitting at 99.9% are usually known compiler tie-breaks (AGENTS.md says leave them).
    p.add_argument("--max-fuzzy", type=float, default=99.5)
    p.add_argument("--max-functions", type=int, default=4)
    p.add_argument("--cooldown-days", type=float, default=7.0)
    p.set_defaults(func=cmd_pick)

    p = sub.add_parser("compare")
    p.add_argument("--baseline", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--unit", required=True)
    p.add_argument("--min-fuzzy-gain", type=float, default=0.5)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("limits")
    p.add_argument("action", choices=["check", "run", "pr", "spend", "attempt", "backoff"])
    p.add_argument("--state", required=True)
    p.add_argument("--max-prs-per-day", type=int, default=3)
    p.add_argument("--max-usd-per-day", type=float, default=25.0)
    p.add_argument("--max-runs-per-day", type=int, default=12)
    p.add_argument("--amount", type=float)
    p.add_argument("--keys", nargs="*")
    p.add_argument("--enforce-usd", type=int, choices=[0, 1], default=1)
    p.add_argument("--minutes", type=float)
    p.add_argument("--reset-epoch", type=int)
    p.set_defaults(func=cmd_limits)

    p = sub.add_parser("cost")
    p.add_argument("--transcript", required=True)
    p.add_argument("--stderr")
    p.add_argument("--exit-code", type=int)
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("prbody")
    p.add_argument("--compare", required=True)
    p.add_argument("--summary")
    p.add_argument("--run-id", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--cost", required=True)
    p.set_defaults(func=cmd_prbody)

    p = sub.add_parser("paths")
    p.add_argument("--allowed", required=True)
    p.set_defaults(func=cmd_paths)

    p = sub.add_parser("render")
    p.add_argument("--work", required=True)
    p.add_argument("--template", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--cycles", type=int, default=12)
    p.set_defaults(func=cmd_render)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
