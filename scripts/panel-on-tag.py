#!/usr/bin/env python3
"""panel-on-tag — EVENT loop: when a git tag is cut, panel the release diff;
`panel_grade` is its eval.

The event is a **git tag** (a release). This computes the diff from the previous
tag to the new one and hands it to the panel for a code review, then scores that
run with `panel_grade` against an answer key — so the loop's own quality is
measured, not assumed.

Safety: **dry-run is the default and fires nothing.** You must pass `--execute`
to actually launch a panel (and even then it launches detached — never block a
foreground call on a panel, which takes 10–40 min). The grade step runs against
an already-finished run.

Modes:
  # plan only — compute the diff, print the exact panel + grade commands (default)
  python3 scripts/panel-on-tag.py --repo ~/projects/my-service --tag v1.4.0

  # actually cut the panel (detached); prints the run dir + grade command
  python3 scripts/panel-on-tag.py --repo ~/projects/my-service --execute

  # eval a finished run against the answer key (this is panel_grade)
  python3 scripts/panel-on-tag.py --grade ~/panel-runs/my-service/20260812_1200 \\
      --key scripts/panel-answer-key.example.json

Structured output: a `plan.json` (schema `panel.on-tag.v1`) for plan/execute, and
a gate JSON (schema `panel.on-tag.grade.v1`) for --grade.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit("panel requires Python 3.11 or newer")

SCHEMA = "panel.on-tag.v1"
GRADE_SCHEMA = "panel.on-tag.grade.v1"
REPO = Path(__file__).resolve().parent.parent          # the panel repo
PANEL_PY = REPO / "skills" / "panel" / "panel.py"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's canonical empty tree
DEFAULT_KEY = REPO / "scripts" / "panel-answer-key.example.json"

CONTEXT = ("Release diff between {prev}..{tag} in {name}. Review the change set "
           "before the release is cut.")
CRITERIA = ("1) verify the changes preserve existing behavior and public "
            "contracts. 2) find defects introduced by this diff. 3) check that "
            "error handling and edge cases still hold. 4) confirm no secrets, "
            "credentials, or tokens are added.")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def latest_tag(repo: Path) -> str | None:
    rc, out = git(repo, "describe", "--tags", "--abbrev=0")
    return out if rc == 0 and out else None


def prev_tag(repo: Path, tag: str) -> str:
    rc, out = git(repo, "describe", "--tags", "--abbrev=0", f"{tag}^")
    return out if rc == 0 and out else EMPTY_TREE


def runs_home() -> Path:
    return Path(os.environ.get("PANEL_RUNS") or Path.home() / "panel-runs")


def append_learning(line: str) -> None:
    learnings = REPO / ".claude" / "LEARNINGS.md"
    learnings.parent.mkdir(parents=True, exist_ok=True)
    if not learnings.exists():
        learnings.write_text("# LEARNINGS — panel\n\n"
                             "Durable notes the on-tag loop appends to.\n\n")
    with learnings.open("a") as fh:
        fh.write(f"- {datetime.now(timezone.utc):%Y-%m-%d} — on-tag: {line}\n")


def tag_dirname(tag: str) -> str:
    """A path-safe, collision-free name for a tag: release/1.0 must not land
    in a nested path, nor share a folder with a real tag named release_1.0."""
    safe = re.sub(r"[^\w.-]+", "_", tag)
    if safe == tag:
        return tag
    return f"{safe}-{hashlib.sha1(tag.encode()).hexdigest()[:6]}"


def build_plan(repo: Path, tag: str, key: Path) -> dict:
    name = repo.name
    base = prev_tag(repo, tag)
    rc, diff = git(repo, "diff", f"{base}..{tag}")
    if rc != 0:
        raise SystemExit(f"git diff {base}..{tag} failed: {diff}")

    safe_tag = tag_dirname(tag)
    outdir = runs_home() / "_on-tag" / name / safe_tag
    outdir.mkdir(parents=True, exist_ok=True)
    diff_path = outdir / f"{safe_tag}.diff"
    diff_path.write_text(diff + "\n" if diff else "")

    context = CONTEXT.format(prev=base, tag=tag, name=name)
    panel_cmd = ["python3", str(PANEL_PY), "--diff", str(diff_path),
                 "--repo", str(repo), "--context", context,
                 "--criteria", CRITERIA, "--json"]
    grade_cmd = ["python3", str(PANEL_PY), "--grade", "<run_dir>",
                 "--key", str(key)]

    return {
        "schema": SCHEMA,
        "repo": str(repo),
        "name": name,
        "tag": tag,
        "prev_tag": base,
        "diff_path": str(diff_path),
        "diff_bytes": len(diff),
        "empty_diff": not diff.strip(),
        "panel_cmd": panel_cmd,
        "grade_cmd": grade_cmd,
        "answer_key": str(key),
        "created_at": now_utc(),
    }


def launch_detached(plan: dict, outdir: Path) -> str | None:
    """Fire the panel detached and return its run dir (parsed from the log)."""
    log = outdir / "panel.log"
    with log.open("w") as fh:
        subprocess.Popen(plan["panel_cmd"], stdout=fh, stderr=subprocess.STDOUT,
                         start_new_session=True)
    # panel.py prints "run dir: <path>" early; poll the log briefly for it.
    for _ in range(60):
        if log.exists():
            for line in log.read_text().splitlines():
                m = re.match(r"run dir:\s*(.+)", line.strip())
                if m:
                    return m.group(1).strip()
        time.sleep(1)
    return None


def parse_grade(grade_md: str) -> dict:
    """Read panel's _grade.md into a small gate result."""
    seats = []
    for line in grade_md.splitlines():
        m = re.match(r"\|\s*([\w.\-]+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", line)
        if m and m.group(1) not in ("seat",):
            seats.append({"seat": m.group(1), "found": int(m.group(2)),
                          "of": int(m.group(3))})
    missed = [ln for ln in grade_md.splitlines() if "MISSED BY EVERY SEAT" in ln]
    best = max((s["found"] for s in seats), default=0)
    total = max((s["of"] for s in seats), default=0)
    return {"seats": seats, "best_found": best, "of": total,
            "defects_missed_by_all": len(missed), "passed": not missed and total > 0}


def do_grade(run_dir: Path, key: Path) -> int:
    if not key.exists():
        raise SystemExit(f"answer key not found: {key}")
    rc = subprocess.run(["python3", str(PANEL_PY), "--grade", str(run_dir),
                         "--key", str(key)]).returncode
    grade_md = run_dir / "_grade.md"
    if rc != 0 or not grade_md.exists():
        raise SystemExit(f"panel --grade failed (exit {rc})")
    parsed = parse_grade(grade_md.read_text())
    result = {"schema": GRADE_SCHEMA, "run_dir": str(run_dir), "key": str(key),
              **parsed, "timestamp": now_utc()}
    (run_dir / "on-tag-gate.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    append_learning(f"graded {run_dir.name}: best {parsed['best_found']}/{parsed['of']}, "
                    f"passed={parsed['passed']}")
    return 0 if parsed["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Auto-panel the release diff on a git tag.")
    ap.add_argument("--repo", type=Path, default=Path.cwd(),
                    help="Repo whose tag to review (default: CWD).")
    ap.add_argument("--tag", help="Tag to review (default: latest tag in --repo).")
    ap.add_argument("--key", type=Path, default=DEFAULT_KEY,
                    help="Answer-key JSON for the panel_grade eval.")
    ap.add_argument("--execute", action="store_true",
                    help="Actually launch the panel (detached). Off by default.")
    ap.add_argument("--grade", dest="grade_dir", type=Path,
                    help="Grade a finished run dir against --key (this is the eval).")
    args = ap.parse_args(argv)

    if args.grade_dir:
        return do_grade(args.grade_dir.resolve(), args.key)

    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"not a git repo: {repo}")
    tag = args.tag or latest_tag(repo)
    if not tag:
        raise SystemExit(f"no tags in {repo}; nothing to review "
                         "(the event is a tag). Cut a tag first.")

    plan = build_plan(repo, tag, args.key)
    outdir = Path(plan["diff_path"]).parent
    (outdir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")

    if plan["empty_diff"]:
        print(f"# note: {plan['prev_tag']}..{tag} is an empty diff; nothing to panel",
              file=sys.stderr)

    if not args.execute:
        plan["mode"] = "dry_run"
        print(json.dumps(plan, indent=2))
        print("\n# to run it (detached):\n  "
              + " ".join(f'"{c}"' if " " in c else c for c in plan["panel_cmd"]),
              file=sys.stderr)
        print("# then, when it finishes, the eval:\n  "
              + " ".join(plan["grade_cmd"]).replace("<run_dir>", "<run_dir>"),
              file=sys.stderr)
        return 0

    run_dir = launch_detached(plan, outdir)
    plan["mode"] = "execute"
    plan["run_dir"] = run_dir
    (outdir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    append_learning(f"launched panel for {plan['name']} {tag} "
                    f"({plan['prev_tag']}..{tag}); run_dir={run_dir}")
    print(json.dumps(plan, indent=2))
    if run_dir:
        print(f"\n# eval when done:\n  python3 {PANEL_PY} --grade {run_dir} "
              f"--key {args.key}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
