"""Offline tests for the build pilot (skills/panel/build.py): no model calls.

    python3 -m unittest discover -s tests
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parent.parent / "skills" / "panel"
sys.path.insert(0, str(SKILL))
import build  # noqa: E402
import panel  # noqa: E402

# a "seat" that edits the repo it is started in, the way a build agent would
WRITER = ("import pathlib, sys; sys.stdin.read(); "
          "pathlib.Path('added.txt').write_text('one\\ntwo\\n'); "
          "print('added a file in', pathlib.Path.cwd().name)")


def seat(script, **cmd):
    return {"lineage": "fake", "cmd": {
        "stdin": True, "argv": [sys.executable, "-c", script],
        "argv_by_mode": {"build": []}, **cmd}}


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


class BuildCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.repo = self.tmp / "src"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "keep.txt").write_text("keep\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        env = patch.dict(os.environ, {
            "PANEL_BUILDS": str(self.tmp / "builds"),
            "PANEL_RUNS": str(self.tmp / "runs")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(panel._CURRENT_RUN.clear)
        self.addCleanup(panel.set_active_mode, panel.DIFF_MODE)

    def run_build(self, registry, *extra, test="test -f added.txt"):
        argv = ["--task", "add a file", "--repo", str(self.repo),
                "--test", test, "--json", *extra]
        with patch("builtins.print") as out:
            code = build.main(argv, registry=registry)
        self.assertEqual(code, 0)
        printed = [c.args[0] for c in out.call_args_list if c.args]
        return json.loads(printed[-1])


class SeatSelectionTests(unittest.TestCase):
    def test_a_review_seat_without_a_build_entry_is_refused(self):
        # resolve_argv falls back to the review argv for an unknown mode: a
        # builder must never be that fallback
        registry = {"reviewer": {"lineage": "x", "cmd": {"argv": ["true"]}},
                    "builder": seat(WRITER)}
        with self.assertRaises(SystemExit) as caught:
            build.plan_seats(["reviewer"], registry, 3)
        self.assertIn("cannot build", str(caught.exception))
        self.assertIn("builder", str(caught.exception))
        with self.assertRaises(SystemExit):
            build.plan_seats(["nobody"], registry, 3)
        self.assertEqual(build.plan_seats([], registry, 3), ["builder"])

    def test_the_seat_list_is_capped(self):
        registry = {f"s{i}": seat(WRITER) for i in range(5)}
        self.assertEqual(build.plan_seats([], registry, 3), ["s0", "s1", "s2"])
        with self.assertRaises(SystemExit) as caught:
            build.plan_seats(list(registry), registry, 3)
        self.assertIn("cap is 3", str(caught.exception))
        self.assertEqual(len(build.plan_seats(list(registry), registry, 5)), 5)

    def test_a_seat_named_twice_is_refused(self):
        # two jobs would share one clone directory and one set of files
        registry = {"s0": seat(WRITER)}
        with self.assertRaises(SystemExit) as caught:
            build.plan_seats(["s0", "s0"], registry, 3)
        self.assertIn("twice", str(caught.exception))

    def test_the_real_roster_names_three_builders(self):
        able = build.build_seats(panel.REGISTRY)
        self.assertEqual(able, ["codex", "gemini-argon", "claude-fable"])
        self.assertEqual(len({panel.lineage_of(n) for n in able}), 3)

    def test_real_build_argv_is_write_capable_and_stays_off_max(self):
        def argv(name):
            return [panel._subst(a, {"{repo}": "/clone"}) for a in
                    panel.resolve_argv(panel.REGISTRY[name]["cmd"],
                                       build.BUILD_MODE)]
        codex = argv("codex")
        self.assertEqual(codex[codex.index("-C") + 1], "/clone")
        self.assertEqual(codex[-1], "-")  # the prompt still arrives on stdin
        claude = argv("claude-fable")
        self.assertEqual(claude[claude.index("--permission-mode") + 1],
                         "acceptEdits")
        self.assertNotIn("--dangerously-skip-permissions", claude)
        argon = argv("gemini-argon")
        self.assertEqual([v for f, v in zip(argon, argon[1:])
                          if f == "--agent"], ["build"])
        self.assertIn("--standalone", argon)  # a stop must reach its server
        for name in ("codex", "claude-fable", "gemini-argon"):
            self.assertFalse(any("max" in a or "ultra" in a
                                 for a in argv(name)), name)


class BuildRunTests(BuildCase):
    def test_a_build_lands_in_a_clone_and_never_in_the_source(self):
        data = self.run_build({"w": seat(WRITER)})
        (result,) = data["results"]
        clone = Path(result["clone"])
        self.assertEqual(data["candidates"], ["w"])
        self.assertEqual(result["tests"], "pass")
        self.assertEqual(result["diff"], {"files": 1, "insertions": 2,
                                          "deletions": 0})
        self.assertEqual(data["base"], self.base)
        # the seat ran inside its clone, which has no remote to push to
        self.assertEqual((clone / "added.txt").read_text(), "one\ntwo\n")
        self.assertEqual(git(clone, "remote"), "")
        self.assertEqual(git(clone, "rev-parse", "HEAD"), self.base)
        # the source repo: same commit, clean tree, no new file
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertFalse((self.repo / "added.txt").exists())
        rundir = Path(data["dir"])
        self.assertIn("+one", (rundir / "w.diff").read_text())
        self.assertIn("added a file in w", (rundir / "w.md").read_text())
        board = (rundir / build.SCOREBOARD).read_text()
        self.assertIn("| 1 | w | pass | 1 | 2 | 0 |", board)
        self.assertIn("--diff", board)  # the judge command, printed not run

    def test_build_runs_live_apart_from_review_runs(self):
        data = self.run_build({"w": seat(WRITER)})
        rundir = Path(data["dir"])
        self.assertEqual(rundir.parent, self.tmp / "builds" / "src")
        # nothing a review tool looks for: no run.json, no run under
        # $PANEL_RUNS for --resume last or panel_status to find
        self.assertFalse((rundir / panel.RUN_META).exists())
        self.assertFalse((self.tmp / "runs").exists())
        self.assertIsNone(panel._latest_run(panel.default_out(self.repo)))
        # and a review run later in the same process is a review again
        self.assertNotIn("on_forced_exit", panel._CURRENT_RUN)
        self.assertEqual(panel._active_mode, panel.DIFF_MODE)

    def test_a_missing_repo_is_named_before_anything_is_created(self):
        with self.assertRaises(SystemExit) as caught:
            build.main(["--task", "t", "--repo", str(self.tmp / "nowhere"),
                        "--test", "true"], registry={"w": seat(WRITER)})
        self.assertIn("is not a directory", str(caught.exception))
        self.assertFalse((self.tmp / "builds").exists())

    def test_ranking_is_tests_then_diff_size_then_time(self):
        big = WRITER.replace("one\\ntwo\\n", "1\\n2\\n3\\n4\\n5\\n")
        idle = "import sys; sys.stdin.read(); print('nothing to do')"
        wrong = WRITER.replace("added.txt", "other.txt")
        data = self.run_build({"big": seat(big), "small": seat(WRITER),
                               "idle": seat(idle), "wrong": seat(wrong)},
                              "--max-seats", "4")
        self.assertEqual(data["candidates"], ["small", "big"])
        by_seat = {r["seat"]: r for r in data["results"]}
        self.assertEqual(by_seat["wrong"]["tests"], "fail")
        # a seat that changed nothing is not a candidate, even though the
        # test command would pass on an untouched tree in another task
        self.assertEqual(by_seat["idle"]["diff"]["files"], 0)
        board = (Path(data["dir"]) / build.SCOREBOARD).read_text()
        self.assertIn("| - | idle |", board)
        self.assertIn("no change", board)

    def test_a_failing_seat_is_still_measured_and_tested(self):
        # a seat that died after writing (a timeout, a crash) may have left
        # the work done: the tests decide, the exit code is only reported
        crashed = WRITER + "; sys.exit(3)"
        data = self.run_build({"c": seat(crashed)})
        (result,) = data["results"]
        self.assertEqual((result["seat_exit"], result["tests"]), (3, "pass"))
        self.assertEqual(data["candidates"], ["c"])

    def test_only_quota_and_login_failures_reach_seat_health(self):
        broke = ("import sys; sys.stdin.read(); "
                 "print('Error: Incorrect API key provided.'); sys.exit(1)")
        self.run_build({"ok": seat(WRITER), "bug": seat("raise SystemExit(2)"),
                        "nokey": seat(broke)})
        health = panel.seat_health()
        self.assertEqual(list(health), ["nokey"])
        self.assertEqual(health["nokey"]["kind"], "auth")

    def test_an_inherited_git_dir_cannot_point_the_build_at_the_source(self):
        # GIT_DIR/GIT_WORK_TREE outrank the working directory: inherited from
        # a hook or a wrapper, they sent checkout --detach, remote remove and
        # add -A to the source repository (codex review, 2026-11-10)
        git(self.repo, "remote", "add", "origin", "https://example.invalid/x")
        branch = git(self.repo, "symbolic-ref", "HEAD")
        redirect = {"GIT_DIR": str(self.repo / ".git"),
                    "GIT_WORK_TREE": str(self.repo)}
        with patch.dict(os.environ, redirect):
            data = self.run_build({"w": seat(WRITER)})
            self.assertEqual(os.environ["GIT_DIR"], redirect["GIT_DIR"])
        self.assertEqual(data["candidates"], ["w"])
        self.assertEqual(git(self.repo, "remote"), "origin")
        self.assertEqual(git(self.repo, "symbolic-ref", "HEAD"), branch)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_output_inside_the_source_or_the_review_root_is_refused(self):
        registry = {"w": seat(WRITER)}
        for out, why in ((self.repo / "builds", "inside the source"),
                         (self.tmp / "runs" / "x", "review run root")):
            with self.assertRaises(SystemExit) as caught:
                build.main(["--task", "t", "--repo", str(self.repo),
                            "--test", "true", "--out", str(out)],
                           registry=registry)
            self.assertIn(why, str(caught.exception))
            self.assertFalse(out.exists())
        with patch.dict(os.environ,
                        {"PANEL_BUILDS": str(self.tmp / "runs" / "builds")}):
            with self.assertRaises(SystemExit):
                build.main(["--task", "t", "--repo", str(self.repo),
                            "--test", "true"], registry=registry)

    def test_a_relative_out_directory_works(self):
        rel = os.path.relpath(self.tmp / "rel-out")
        self.assertFalse(os.path.isabs(rel))
        data = self.run_build({"w": seat(WRITER)}, "--out", rel)
        self.assertEqual(data["candidates"], ["w"])
        self.assertTrue(Path(data["dir"]).is_absolute())
        self.assertEqual(Path(data["dir"]).parent, self.tmp / "rel-out")

    def test_a_seat_that_worked_then_failed_does_not_mark_its_lane_dead(self):
        # failure_kind reads words: a builder that exits 1 after saying
        # "FAILED tests/test_billing.py" is not out of credit. A lane that
        # edited files was alive
        talker = WRITER + ("; print('FAILED tests/test_billing.py::test_total"
                           " - quota exceeded for plan'); sys.exit(1)")
        data = self.run_build({"t": seat(talker)})
        self.assertEqual(data["results"][0]["seat_exit"], 1)
        self.assertEqual(panel.seat_health(), {})

    def test_a_diff_git_could_not_write_is_not_a_candidate(self):
        real = build._git

        def flaky(args, cwd, timeout=600):
            if any(a.startswith("--output=") for a in args):
                return 1, "fatal: unable to write the patch"
            return real(args, cwd, timeout)
        with patch.object(build, "_git", side_effect=flaky):
            data = self.run_build({"w": seat(WRITER)})
        (result,) = data["results"]
        self.assertEqual(result["tests"], "pass")
        self.assertIsNone(result["diff"])
        self.assertEqual(data["candidates"], [])

    def test_a_stop_before_the_run_exists_exits_as_a_stop(self):
        refused = (panel.NOT_STARTED, "panel is stopping; seat not started")
        panel._REAPED.set()
        try:
            with patch.object(build, "_git", return_value=refused), \
                    patch.object(panel, "stop_requested", return_value=True), \
                    patch.object(panel, "_STOP_SIGNUM", [signal.SIGTERM]):
                code = build.main(["--task", "t", "--repo", str(self.repo),
                                   "--test", "true"],
                                  registry={"w": seat(WRITER)})
        finally:
            panel._REAPED.clear()
        self.assertEqual(code, 128 + signal.SIGTERM)

    def test_the_test_command_has_its_own_timeout(self):
        data = self.run_build({"w": seat(WRITER)}, "--test-timeout", "1",
                              test="sleep 30")
        (result,) = data["results"]
        self.assertEqual(result["tests"], "timeout")
        self.assertEqual(data["candidates"], [])
        self.assertLess(result["test_seconds"], 20)


class PanelHookTests(unittest.TestCase):
    def test_run_cmd_starts_the_process_in_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            where = str(Path(tmp).resolve())
            code, out = panel.run_cmd(
                [sys.executable, "-c", "import os; print(os.getcwd())"], 30,
                cwd=where)
            self.assertEqual((code, out), (0, where))
            entry = {"lineage": "x", "cmd": {"argv": [
                sys.executable, "-c", "import os; print(os.getcwd())"]}}
            self.assertEqual(
                panel.make_seat(entry)("p", "/unused", 30, cwd=where),
                (0, where))
            # a review seat keeps the launch directory, as before
            self.assertEqual(panel.make_seat(entry)("p", "/unused", 30),
                             (0, os.getcwd()))

    def test_a_forced_exit_writes_the_build_scoreboard_not_a_summary(self):
        # the reaper's last act when the main thread cannot unwind: with a
        # build in progress it must call the build's writer
        called = []
        with patch.object(panel, "reap_live_seats"), \
                patch.object(panel, "HARD_EXIT_AFTER", 0), \
                patch.object(panel, "_drain"), \
                patch.object(panel, "collect_run") as collect, \
                patch.object(panel.os, "_exit", side_effect=SystemExit), \
                patch.dict(panel._CURRENT_RUN,
                           {"on_forced_exit": lambda: called.append(1)},
                           clear=True):
            panel._STOP_SIGNAL.set()
            try:
                with self.assertRaises(SystemExit):
                    panel._reaper()
            finally:
                panel._STOP_SIGNAL.clear()
                panel._REAPED.clear()
        self.assertEqual(called, [1])
        collect.assert_not_called()


class StopTests(BuildCase):
    def test_a_stop_ends_the_seat_tree_and_still_writes_the_scoreboard(self):
        registry = self.tmp / "seats.json"
        marker = self.tmp / "child.pid"
        # the seat starts a grandchild that would outlive a plain kill
        sleeper = (
            "import subprocess, sys, pathlib, time; "
            "p = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(120)']); "
            f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); "
            "time.sleep(120)")
        registry.write_text(json.dumps({"seats": {"slow": seat(sleeper)}}))
        env = {**os.environ, "PANEL_SEATS": str(registry)}
        proc = subprocess.Popen(
            [sys.executable, str(SKILL / "build.py"), "--task", "t",
             "--repo", str(self.repo), "--test", "true"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(marker.exists(), "the seat never started")
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, out)
        child = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(child, signal.SIGKILL)
            self.fail("the seat's grandchild survived the stop")
        (rundir,) = (self.tmp / "builds" / "src").iterdir()
        data = json.loads((rundir / build.RESULT_FILE).read_text())
        self.assertEqual(data["candidates"], [])
        self.assertTrue((rundir / build.SCOREBOARD).exists())
        # the source is untouched by a stopped build too
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
