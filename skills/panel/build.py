#!/usr/bin/env python3
"""Build pilot: one task, a few seats, each in its own clone, scored by tests.

    python3 build.py --task "..." --repo /path/to/repo \\
        --test "python3 -m unittest discover -s tests"

Each seat gets a full local clone of the repo at the base commit, with no
remote, under ~/panel-builds (or $PANEL_BUILDS) -- never under ~/panel-runs,
so the review tooling (status, summary, resume) cannot mistake a build for a
review run. The seat's write-capable `build` argv runs inside its clone, then
the test command runs there. The scoreboard is arithmetic: tests pass or
fail, diff size against the base commit, wall time. No model ranks the
candidates, and nothing is merged, pushed or written to the source repo.
Judging a candidate is an ordinary `panel.py --diff` run; the command is
printed, not run.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel  # noqa: E402

BUILD_MODE = "build"
MAX_SEATS = 3
RESULT_FILE = "_build.json"
SCOREBOARD = "_scoreboard.md"
# Git obeys these before the working directory. Inherited from a hook or a
# wrapper, they would point every git call here, and every seat's own git,
# at the source repository instead of the clone.
GIT_LOCAL_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                  "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                  "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX")

BUILD_TEMPLATE = """You are implementing one change in the git repository in the current
directory. Other implementers are working on the same task in their own
copies; a test run decides which copies are kept.

TASK:
{task}

RULES:
- Work only inside this directory. Do not push, do not add a remote, and do
  not touch any other repository or any file outside this directory.
- Make the smallest change that fully does the task. Follow the repository's
  own AGENTS.md or CLAUDE.md if it has one.
- Your work is checked afterwards with this command, run from this directory:
      {test}
  Run it yourself if you can, and fix what fails.
- Leave the result in the working tree. Committing is optional.

End with one short paragraph saying what you changed.
"""


def builds_root():
    return Path(os.environ.get("PANEL_BUILDS") or Path.home() / "panel-builds")


def build_seats(registry):
    """Seats that ship a write-capable `build` argv."""
    return [name for name, entry in registry.items()
            if BUILD_MODE in (entry.get("cmd", {}).get("argv_by_mode") or {})]


def plan_seats(names, registry, cap):
    """The seats to run, or SystemExit. A seat without a `build` entry is
    refused: resolve_argv would hand back its review argv, and a reviewer
    locked to its prompt is not a builder."""
    able = build_seats(registry)
    names = names or able[:cap]
    for name in names:
        if name not in registry:
            raise SystemExit(f"unknown seat: {name}")
        if name not in able:
            raise SystemExit(
                f"{name} has no `build` entry under argv_by_mode in "
                f"seats.json, so it cannot build. Seats that can: "
                f"{', '.join(able) or 'none'}")
    if not names:
        raise SystemExit("no seat in the roster has a `build` entry")
    if len(set(names)) != len(names):
        raise SystemExit("a seat is named twice: each seat builds once, in "
                         "a clone of its own")
    if len(names) > cap:
        raise SystemExit(f"{len(names)} seats asked for, cap is {cap}: every "
                         f"seat is a full agent run. Raise --max-seats to "
                         f"run more.")
    return names


def _git(args, cwd, timeout=600):
    """git through run_cmd, so a stop or a timeout ends it like any seat."""
    return panel.run_cmd(["git", *args], timeout, cwd=str(cwd))


def make_clone(repo, base, dest):
    """A full copy of `repo` at `base`, with no remote. Not a git worktree
    (worktrees share one .git with the source) and not a hardlinked clone
    (its object files would be the source's own files): some seats run
    without a sandbox."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for args, cwd in ((["clone", "--quiet", "--no-hardlinks", str(repo),
                        str(dest)], dest.parent),
                      (["checkout", "--quiet", "--detach", base], dest),
                      (["remote", "remove", "origin"], dest)):
        code, out = _git(args, cwd)
        if code != 0:
            return code, f"git {args[0]} failed: {out[-400:]}"
    return 0, ""


def measure_diff(clone, base, diff_path):
    """What the seat left against the base, committed or not: {files,
    insertions, deletions}, or None when git could not say. Staging happens
    in the clone only; git writes the diff file itself, byte for byte."""
    code, _ = _git(["add", "-A"], clone)
    if code != 0:
        return None
    code, stat = _git(["diff", "--cached", "--numstat", base], clone)
    if code != 0:
        return None
    size = {"files": 0, "insertions": 0, "deletions": 0}
    for line in stat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        size["files"] += 1
        for key, value in (("insertions", parts[0]), ("deletions", parts[1])):
            size[key] += int(value) if value.isdigit() else 0  # "-": binary
    code, _ = _git(["diff", "--cached", f"--output={diff_path}", base], clone)
    return size if code == 0 else None  # no patch on disk: nothing to judge


def build_one(name, entry, prompt, repo, base, rundir, opts, result):
    """Clone, build, measure, test. `result` is filled in as each step lands,
    so a forced exit still reports how far this seat got."""
    clone = rundir / "clones" / name
    result.update(clone=str(clone), status="cloning")
    with panel._tracked_call():
        code, detail = make_clone(repo, base, clone)
        if code != 0:
            result.update(status="stopped" if code == panel.NOT_STARTED
                          else "clone failed", note=detail)
            return
        result["status"] = "building"
        started = time.monotonic()
        code, out = panel.make_seat(entry)(prompt, clone, opts.timeout,
                                           cwd=str(clone))
        result.update(seat_exit=code,
                      seat_seconds=round(time.monotonic() - started, 1))
        if code == panel.NOT_STARTED:
            result["status"] = "stopped"
            return
        panel._atomic_write(rundir / f"{name}.md",
                            f"# {name} (exit {code})\n\n{out}\n")
        if code != 0:
            result["note"] = panel._error_tail(out) or f"exit {code}"
        result["status"] = "measuring"
        result["diff"] = measure_diff(clone, base, rundir / f"{name}.diff")
        # A build says nothing about a review lane, except that the lane is
        # out of quota or logged out: that the review roster should know.
        # failure_kind reads words, and a builder's answer is full of them
        # ("FAILED tests/test_billing.py"), so only a seat that changed
        # nothing counts: a lane that edited files was alive.
        if (code != 0 and not (result["diff"] or {}).get("files")
                and panel.failure_kind(code, out) in ("quota", "auth")):
            panel.record_seat_health(name, code, out)
        result["status"] = "testing"
        started = time.monotonic()
        code, out = panel.run_cmd(["/bin/sh", "-c", opts.test],
                                  opts.test_timeout, cwd=str(clone))
        if code == panel.NOT_STARTED:
            result["status"] = "stopped"
            return
        panel._atomic_write(rundir / f"{name}.test.log", out + "\n")
        result.update(test_exit=code,
                      test_seconds=round(time.monotonic() - started, 1),
                      tests=("pass" if code == 0 else
                             "timeout" if code == -1 else "fail"),
                      status="done")


def _lines(result):
    diff = result.get("diff") or {}
    return diff.get("insertions", 0) + diff.get("deletions", 0)


def is_candidate(result):
    """Changed something and the tests pass."""
    return (result.get("tests") == "pass"
            and (result.get("diff") or {}).get("files", 0) > 0)


def ranked(results):
    """Candidates first: smaller diff, then faster seat. The rest keep the
    order they were asked for in. Arithmetic only."""
    good = sorted((r for r in results if is_candidate(r)),
                  key=lambda r: (_lines(r), r.get("seat_seconds", 0)))
    return good + [r for r in results if not is_candidate(r)]


def judge_command(meta, result):
    task = " ".join(meta["task"].split())[:300]
    return shlex.join([
        "python3", str(Path(panel.__file__).resolve()),
        "--diff", str(Path(meta["dir"]) / f"{result['seat']}.diff"),
        "--repo", result["clone"],
        "--context", f"One candidate implementation ({result['seat']}) of "
                     f"this task: {task}",
        "--criteria", "1) The change fully does the task. 2) It introduces "
                      "no defect or regression. 3) It is no larger than the "
                      "task needs."])


def write_results(rundir, meta, results):
    """_build.json for programs, _scoreboard.md for people."""
    order = ranked(results)
    panel._atomic_write(rundir / RESULT_FILE, json.dumps(
        {**meta, "results": order,
         "candidates": [r["seat"] for r in order if is_candidate(r)]},
        indent=2))
    rows = ["# build " + meta["ts"], "",
            f"- task: {' '.join(meta['task'].split())[:300]}",
            f"- repo: {meta['repo']} at {meta['base'][:12]}",
            f"- test: `{meta['test']}`", "",
            "| rank | seat | tests | files | + | - | seat min | test s | "
            "seat exit | status |",
            "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | "
            "--- |"]
    for i, r in enumerate(order, 1):
        diff = r.get("diff") or {}
        minutes = (f"{r['seat_seconds'] / 60:.1f}"
                   if "seat_seconds" in r else "-")
        status = r["status"]
        if r["status"] == "done" and not diff.get("files"):
            status = "no change"
        rows.append(
            f"| {i if is_candidate(r) else '-'} | {r['seat']} | "
            f"{r.get('tests', '-')} | {diff.get('files', '-')} | "
            f"{diff.get('insertions', '-')} | {diff.get('deletions', '-')} | "
            f"{minutes} | {r.get('test_seconds', '-')} | "
            f"{r.get('seat_exit', '-')} | {status} |")
    rows += ["", "Ranked by arithmetic only: tests pass and something "
                 "changed, then the smaller diff, then the faster seat. "
                 "Passing tests is not a review.", ""]
    notes = [f"- {r['seat']}: {' '.join(r['note'].split())[:300]}"
             for r in order if r.get("note")]
    if notes:
        rows += ["## Notes", "", *notes, ""]
    rows += ["## Files", ""]
    for r in order:
        rows.append(f"- {r['seat']}: clone `{r.get('clone', '-')}`, answer "
                    f"`{r['seat']}.md`, diff `{r['seat']}.diff`, tests "
                    f"`{r['seat']}.test.log`")
    judged = [r for r in order if is_candidate(r)]
    if judged:
        rows += ["", "## Judge a candidate (a normal panel review; not run "
                     "for you)", ""]
        rows += [f"```bash\n{judge_command(meta, r)}\n```" for r in judged]
    panel._atomic_write(rundir / SCOREBOARD, "\n".join(rows) + "\n")


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", help="what to build")
    ap.add_argument("--task-file", type=Path, help="read the task from a file")
    ap.add_argument("--repo", type=Path, default=None,
                    help="repository to clone (default: current directory); "
                         "never written to")
    ap.add_argument("--test", required=True,
                    help="shell command run in each clone after its seat "
                         "finishes; exit 0 means pass. The seats are shown "
                         "it, and its raw output is kept in <seat>.test.log")
    ap.add_argument("--seats", default="",
                    help="comma-separated seats; each needs a `build` entry "
                         f"in seats.json (default: the first {MAX_SEATS})")
    ap.add_argument("--max-seats", type=int, default=MAX_SEATS)
    ap.add_argument("--base", default="HEAD",
                    help="commit every clone starts from (default: HEAD)")
    ap.add_argument("--timeout", type=int, default=2400,
                    help="seconds per seat")
    ap.add_argument("--test-timeout", type=int, default=900,
                    help="seconds per test run")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"run root (default: {builds_root()}/<repo>)")
    ap.add_argument("--json", action="store_true",
                    help=f"print {RESULT_FILE} instead of the scoreboard")
    return ap


def main(argv=None, registry=None):
    inherited = {var: os.environ.pop(var) for var in GIT_LOCAL_VARS
                 if var in os.environ}
    try:
        code = _main(argv, panel.REGISTRY if registry is None else registry)
    except SystemExit:
        if not panel.stop_requested():
            raise
        code = 1  # stopped before the run existed: exit as a stop, below
    finally:
        os.environ.update(inherited)
    # same contract as panel.main: a stopped build still wrote what it had,
    # and does not exit before the reaper has killed every process tree
    if panel.stop_requested():
        panel._REAPED.wait(30)
        return 128 + panel._STOP_SIGNUM[0]
    return code


def _main(argv, registry):
    args = build_parser().parse_args(argv)
    if bool(args.task) == bool(args.task_file):
        raise SystemExit("give exactly one of --task or --task-file")
    task = args.task or args.task_file.read_text()
    names = plan_seats([s.strip() for s in args.seats.split(",") if s.strip()],
                       registry, args.max_seats)
    repo = (args.repo or Path.cwd()).resolve()
    if not repo.is_dir():
        raise SystemExit(f"{repo} is not a directory")
    code, base = _git(["rev-parse", "--verify", f"{args.base}^{{commit}}"],
                      repo)
    if code != 0:
        raise SystemExit(f"{repo}: cannot resolve {args.base}: {base}")
    # --no-optional-locks: plain `git status` may rewrite the source's index
    code, dirty = _git(["--no-optional-locks", "status", "--porcelain"], repo)
    if code == 0 and dirty:
        print(f"note: {repo} has uncommitted changes; the clones start from "
              f"commit {base[:12]} and do not include them", flush=True)

    root = (args.out or builds_root() / repo.name).resolve()
    for parent, what in ((repo, "inside the source repository, which a "
                                "build never writes to"),
                         (panel._runs_root().resolve(),
                          "inside the review run root, where status, summary "
                          "and resume would find it")):
        if root == parent or parent in root.parents:
            raise SystemExit(f"build output {root} is {what}; pick another "
                             f"--out or $PANEL_BUILDS")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rundir, seq = root / ts, 1
    while True:  # creating the directory is the claim, as in panel.py
        try:
            rundir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            seq += 1
            rundir = root / f"{ts}_{seq}"
    print(f"build dir: {rundir}", flush=True)

    prompt = BUILD_TEMPLATE.format(task=task.strip(), test=args.test)
    (rundir / "prompt.md").write_text(prompt)
    meta = {"ts": ts, "dir": str(rundir), "repo": str(repo), "base": base,
            "task": task.strip(), "test": args.test, "seats": names,
            "timeout": args.timeout, "test_timeout": args.test_timeout,
            "pid": os.getpid()}
    results = [{"seat": name, "status": "not started",
                "lineage": panel.lineage_of(name, registry),
                "model": panel.seat_identity(registry[name])}
               for name in names]
    # a forced exit (the main thread could not unwind after a stop) still
    # writes the scoreboard instead of a review summary
    panel._CURRENT_RUN.update(
        on_forced_exit=lambda: write_results(rundir, meta, results))
    panel.set_active_mode(BUILD_MODE)
    print(f"building with {len(names)} seat(s) (timeout {args.timeout}s "
          f"each): {', '.join(names)}", flush=True)
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(names)) as pool:
            futures = {pool.submit(build_one, r["seat"], registry[r["seat"]],
                                   prompt, repo, base, rundir, args, r): r
                       for r in results}
            for fut in concurrent.futures.as_completed(futures):
                r = futures[fut]
                if fut.exception() is not None:  # one seat never kills a build
                    r.update(status="error", note=repr(fut.exception()))
                print(f"[{r['seat']}] {r['status']} | tests "
                      f"{r.get('tests', '-')} | {_lines(r)} lines changed",
                      flush=True)
        write_results(rundir, meta, results)
    finally:
        # a review run later in this process gets its own summary back
        panel._CURRENT_RUN.pop("on_forced_exit", None)
        panel.set_active_mode(panel.DIFF_MODE)
    if args.json:
        print((rundir / RESULT_FILE).read_text())
    else:
        print("\n" + (rundir / SCOREBOARD).read_text(), end="")
    return 0


if __name__ == "__main__":
    panel.install_signal_handlers()
    sys.exit(main())
