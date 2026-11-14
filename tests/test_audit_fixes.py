#!/usr/bin/env python3
"""Regression tests for the 2026-11-09 full audit (docs/audits/
2026-11-09-full-audit.md). Each test names the defect it pins. Offline: fake
seats, local subprocesses only, no model calls, no network.
"""

import contextlib
import errno
import importlib.util
import io
import json
import os
import sys
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "panel"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_server  # noqa: E402
import panel  # noqa: E402
from test_panel import TempRunCase, findings_block  # noqa: E402

PY = sys.executable


def finding(claim, file=None, line=None, severity="high"):
    return {"file": file, "line": line, "claim": claim, "severity": severity}


REG = {"codex": {"lineage": "openai"}, "kimi": {"lineage": "moonshot"},
       "grok": {"lineage": "xai"}}


class ConsensusTests(unittest.TestCase):
    """D1: a false merge inflates the lineage count that drives action."""

    def test_a_seats_own_second_finding_never_borrows_another_lineage(self):
        per = {"codex": [finding("race on counter increment in the worker loop",
                                 "foo.py", 10),
                         finding("counter overflow in the worker loop",
                                 "foo.py", 30, "low")],
               "kimi": [finding("counter overflow in the worker loop",
                                "foo.py", 31, "low")]}
        clusters = panel.cluster_findings(per, REG)
        race = next(c for c in clusters if "race" in c["rep"]["claim"])
        overflow = next(c for c in clusters
                        if c["rep"]["claim"].startswith("counter overflow"))
        self.assertEqual(race["lineages"], ["openai"])
        self.assertEqual(overflow["lineages"], ["moonshot", "openai"])
        self.assertEqual(overflow["severity"], "low")

    def test_different_claims_at_one_line_stay_apart(self):
        per = {"codex": [finding("API key is leaked in logs", "auth.py", 20)],
               "kimi": [finding("API key is never refreshed after expiry",
                                "auth.py", 20)]}
        self.assertEqual(len(panel.cluster_findings(per, REG)), 2)

    def test_a_claim_and_its_negation_are_a_dispute_not_agreement(self):
        per = {"codex": [finding("the lock is released on the error path",
                                 "db.py", 5)],
               "kimi": [finding("the lock is never released on the error path",
                                "db.py", 5)]}
        self.assertEqual(len(panel.cluster_findings(per, REG)), 2)

    def test_paraphrases_at_one_place_still_merge(self):
        per = {"codex": [finding("missing bounds check on the buffer index",
                                 "buf.c", 40)],
               "kimi": [finding("buffer index has no bounds check", "buf.c",
                                42)]}
        clusters = panel.cluster_findings(per, REG)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["lineages"], ["moonshot", "openai"])

    def test_the_best_matching_cluster_wins_not_the_first(self):
        # both codex rows qualify for kimi's claim (the first by containment);
        # the stronger match must win (Codex re-check 2026-11-09, 11)
        per = {"codex": [finding("timeout kills the seat and drops the "
                                 "partial answer"),
                         finding("timeout kills the seat but child processes "
                                 "keep running")],
               "kimi": [finding("child processes keep running after the "
                                "timeout kills the seat")]}
        first = panel._tokens(per["codex"][0]["claim"])
        kimi = panel._tokens(per["kimi"][0]["claim"])
        self.assertGreaterEqual(panel._containment(first, kimi),
                                panel.CONTAINED_BAR)
        clusters = panel.cluster_findings(per, REG)
        children = next(c for c in clusters if "child" in c["rep"]["claim"])
        self.assertIn("kimi", children["seats"])

    def test_a_middle_member_cannot_bridge_two_distant_places(self):
        claim = "unchecked index into the frame buffer"
        per = {"codex": [finding(claim, "fb.c", 10)],
               "kimi": [finding(claim, "fb.c", 30)],
               "grok": [finding(claim, "fb.c", 50)]}
        clusters = panel.cluster_findings(per, REG)
        far = next(c for c in clusters if "grok" in c["seats"])
        self.assertEqual(far["seats"], ["grok"])

    def test_an_uncited_member_cannot_bridge_two_files(self):
        claim = "retry loop never backs off"
        per = {"codex": [finding(claim, "a.py", 3)],
               "kimi": [finding(claim)],
               "grok": [finding(claim, "b.py", 3)]}
        clusters = panel.cluster_findings(per, REG)
        b = next(c for c in clusters if "grok" in c["seats"])
        self.assertNotIn("codex", b["seats"])

    def test_a_paraphrase_cannot_bridge_a_claim_and_its_negation(self):
        # kimi's short paraphrase joins codex, and its low overlap with the
        # denial (below the guard bar) used to let the denial in via kimi
        per = {"codex": [finding("the lock is released on the error path")],
               "kimi": [finding("lock released")],
               "grok": [finding("the lock is never released on the error "
                                "path")]}
        clusters = panel.cluster_findings(per, REG)
        never = next(c for c in clusters if "grok" in c["seats"])
        self.assertEqual(never["seats"], ["grok"])

    def test_two_absence_words_are_the_same_polarity(self):
        # Codex re-check 6: "missing" vs "skipped" is a paraphrase
        per = {"codex": [finding("buffer index bounds check is missing "
                                 "before memory access", "buf.c", 40)],
               "kimi": [finding("buffer index bounds check is skipped "
                                "before memory access", "buf.c", 40)]}
        self.assertEqual(len(panel.cluster_findings(per, REG)), 1)

    def test_a_denial_of_a_leak_is_still_a_dispute_after_stemming(self):
        per = {"codex": [finding("the logger leaks the api key", "log.py", 4)],
               "kimi": [finding("the logger does not leak the api key",
                                "log.py", 4)]}
        self.assertEqual(len(panel.cluster_findings(per, REG)), 2)


class ParserTests(unittest.TestCase):
    """D2: the block is found however the answer quotes its markers."""

    def test_the_end_marker_inside_a_claim_does_not_cut_the_block(self):
        text = findings_block([finding("parser stops at END_FINDINGS_JSON "
                                       "inside strings")])
        items, ok = panel.parse_findings(text)
        self.assertTrue(ok)
        self.assertEqual(len(items), 1)

    def test_a_corrected_later_block_wins(self):
        text = (findings_block([]) + "\nActually, one more:\n"
                + findings_block([finding("real one")]))
        items, ok = panel.parse_findings(text)
        self.assertEqual([i["claim"] for i in items], ["real one"])

    def test_an_echoed_prompt_template_is_not_a_finding(self):
        echoed = panel.FINDINGS_BLOCK.format(unit="defect")
        items, ok = panel.parse_findings("I could not run.\n" + echoed)
        self.assertEqual((items, ok), ([], False))

    def test_echoed_template_before_a_real_block_is_ignored(self):
        text = (panel.FINDINGS_BLOCK.format(unit="defect")
                + findings_block([finding("the real claim")]))
        items, _ = panel.parse_findings(text)
        self.assertEqual([i["claim"] for i in items], ["the real claim"])

    def test_an_inline_marker_still_parses(self):
        text = 'Done. FINDINGS_JSON: [{"claim": "inline", "severity": "low"}]'
        items, ok = panel.parse_findings(text)
        self.assertTrue(ok)
        self.assertEqual(items[0]["claim"], "inline")

    def test_a_marker_quoted_inside_an_inline_block_is_not_a_block(self):
        text = ('Done. FINDINGS_JSON: [{"claim": "FINDINGS_JSON silently '
                'returns [] for real findings", "severity": "high"}]')
        items, ok = panel.parse_findings(text)
        self.assertTrue(ok)
        self.assertEqual(len(items), 1)

    def test_marker_mentioned_in_prose_before_the_block(self):
        text = ("The FINDINGS_JSON marker [sic] is mentioned here.\n"
                + findings_block([finding("after prose")]))
        items, _ = panel.parse_findings(text)
        self.assertEqual([i["claim"] for i in items], ["after prose"])

    def test_critical_and_p1_rank_as_high_not_below_low(self):
        text = findings_block([finding("a", severity="critical"),
                               finding("b", severity="P1"),
                               finding("c", severity="nit")])
        items, _ = panel.parse_findings(text)
        self.assertEqual([i["severity"] for i in items],
                         ["high", "high", "low"])


class RunCmdTests(unittest.TestCase):
    """D3, D6, D13 and the E2BIG message, with real local subprocesses."""

    def test_a_non_utf8_byte_keeps_the_answer_and_its_exit(self):
        code = "import sys; sys.stdout.buffer.write(b'VALID ANSWER\\n\\xff')"
        rc, out = panel.run_cmd([PY, "-c", code], 30)
        self.assertEqual(rc, 0)
        self.assertIn("VALID ANSWER", out)

    def test_stderr_is_kept_when_a_failing_cli_printed_progress(self):
        code = ("import sys; print('progress'); "
                "print('fatal auth failure', file=sys.stderr); sys.exit(3)")
        rc, out = panel.run_cmd([PY, "-c", code], 30)
        self.assertEqual(rc, 3)
        self.assertIn("progress", out)
        self.assertIn("fatal auth failure", out)

    def test_a_timeout_keeps_partial_output(self):
        code = ("import sys, time; print('half an answer', flush=True); "
                "time.sleep(30)")
        rc, out = panel.run_cmd([PY, "-c", code], 1)
        self.assertEqual(rc, -1)
        self.assertIn("TIMEOUT", out)
        self.assertIn("half an answer", out)

    def test_a_timeout_stops_the_clis_children_too(self):
        marker = Path(os.environ.get("TMPDIR", "/tmp")) / f"panel-orphan-{os.getpid()}"
        marker.unlink(missing_ok=True)
        child = (f"import time, pathlib; time.sleep(2); "
                 f"pathlib.Path({str(marker)!r}).write_text('alive')")
        parent = (f"import subprocess, sys, time; "
                  f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                  f"time.sleep(30)")
        rc, _ = panel.run_cmd([PY, "-c", parent], 0.5)
        self.assertEqual(rc, -1)
        time.sleep(3)
        self.assertFalse(marker.exists(), "the grandchild outlived the timeout")

    def test_an_escaped_descendant_is_stopped_and_partial_output_kept(self):
        # Codex re-check 3: a child in its own session escapes a group kill,
        # holds the pipe, and used to cost 10 s and the partial answer
        marker = Path(os.environ.get("TMPDIR", "/tmp")) / f"panel-escaped-{os.getpid()}"
        marker.unlink(missing_ok=True)
        child = (f"import time, pathlib; time.sleep(2); "
                 f"pathlib.Path({str(marker)!r}).write_text('alive')")
        parent = (f"import subprocess, sys, time; "
                  f"print('PARTIAL PAID ANSWER', flush=True); "
                  f"subprocess.Popen([sys.executable, '-c', {child!r}], "
                  f"start_new_session=True); time.sleep(30)")
        started = time.monotonic()
        rc, out = panel.run_cmd([PY, "-c", parent], 0.5)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(rc, -1)
        self.assertIn("PARTIAL PAID ANSWER", out)
        time.sleep(3)
        self.assertFalse(marker.exists(), "the escaped child kept running")

    def test_nothing_new_starts_once_panel_is_stopping(self):
        panel.reap_live_seats()
        try:
            rc, out = panel.run_cmd([PY, "-c", "print('ran')"], 5)
        finally:
            panel._STOPPING.clear()
        self.assertEqual(rc, panel.NOT_STARTED)

    def test_ctrl_c_stops_panel_and_its_seats_at_once(self):
        # Codex re-check 2: the interrupt used to wait for every seat
        import signal as sig
        import subprocess
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        pidfile = tmp / "seat.pid"
        driver = tmp / "driver.py"
        seat_code = (f"import os, time, pathlib; "
                     f"pathlib.Path({str(pidfile)!r}).write_text("
                     f"str(os.getpid())); time.sleep(60)")
        driver.write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(ROOT / 'skills' / 'panel')!r})\n"
            "import panel\n"
            "panel.install_signal_handlers()\n"
            f"child = [sys.executable, '-c', {seat_code!r}]\n"
            "panel.PANEL.clear()\n"
            "panel.PANEL['slow'] = lambda p, r, t: panel.run_cmd(child, 120)\n"
            f"sys.exit(panel.main(['--task', 'q', '--seats', 'slow', "
            f"'--out', {str(tmp / 'out')!r}, '--repo', {str(tmp)!r}]))\n")
        env = {**os.environ, "PANEL_RUNS": str(tmp / "runs")}
        proc = subprocess.Popen([PY, str(driver)], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if pidfile.exists() and pidfile.read_text():
                    break
                time.sleep(0.1)
            seat_pid = int(pidfile.read_text())
            proc.send_signal(sig.SIGINT)
            proc.wait(timeout=10)
            time.sleep(0.5)
            with self.assertRaises(ProcessLookupError):
                os.kill(seat_pid, 0)
            self.assertEqual(proc.returncode, 130)
            runs = list((tmp / "out").glob("*/_summary.md"))
            self.assertEqual(len(runs), 1, "a stopped run still summarizes")
        finally:
            if proc.poll() is None:
                proc.kill()

    def driver(self, tmp, body):
        import subprocess
        script = tmp / "driver.py"
        script.write_text(
            "import os, sys, time, signal, pathlib, json\n"
            f"sys.path.insert(0, {str(ROOT / 'skills' / 'panel')!r})\n"
            "import panel\n"
            "panel.install_signal_handlers()\n" + body)
        env = {**os.environ, "PANEL_RUNS": str(tmp / "runs")}
        return subprocess.Popen([PY, str(script)], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    def test_a_serial_answer_returned_at_the_signal_is_kept(self):
        # shutdown re-check 1: the handler interrupted the main thread
        # between a serial seat returning and its file being written
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "out"
        proc = self.driver(tmp, (
            "def seat(p, r, t):\n"
            "    os.kill(os.getpid(), signal.SIGTERM)\n"
            "    time.sleep(0.3)\n"
            "    return 0, 'THE SERIAL ANSWER\\nFINDINGS_JSON\\n[]\\n"
            "END_FINDINGS_JSON'\n"
            "panel.PANEL.clear(); panel.PANEL['s'] = seat\n"
            "panel.REGISTRY['s'] = {'lineage': 'x', 'serial': True}\n"
            f"sys.exit(panel.main(['--task', 'q', '--seats', 's', "
            f"'--out', {str(out)!r}, '--repo', {str(tmp)!r}]))\n"))
        proc.wait(timeout=15)
        files = list(out.glob("*/s.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("THE SERIAL ANSWER", files[0].read_text())
        self.assertEqual(proc.returncode, 143)

    def test_a_tree_that_keeps_spawning_is_frozen_then_killed(self):
        # shutdown re-check 3: children spawned after a snapshot escaped
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        pids = tmp / "pids"
        spawner = (
            "import subprocess, sys, time, pathlib\n"
            f"log = pathlib.Path({str(pids)!r})\n"
            "while True:\n"
            "    c = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)'], start_new_session=True)\n"
            "    with log.open('a') as f: f.write(f'{c.pid}\\n')\n"
            "    time.sleep(0.05)\n")
        rc, _ = panel.run_cmd([PY, "-c", spawner], 1.0)
        self.assertEqual(rc, -1)
        time.sleep(0.5)
        alive = []
        for line in pids.read_text().split():
            try:
                os.kill(int(line), 0)
                alive.append(line)
            except ProcessLookupError:
                pass
        self.assertEqual(alive, [], "spawned children survived the stop")

    def test_a_long_answer_on_stderr_survives_a_timeout(self):
        # shutdown re-check 6: only the last 4000 chars of stderr were kept
        code = ("import sys, time; print('PROGRESS', flush=True); "
                "sys.stderr.write('OPENING DEFECT ' + 'x' * 6000 + "
                "' CLOSING'); sys.stderr.flush(); time.sleep(30)")
        rc, out = panel.run_cmd([PY, "-c", code], 0.5)
        self.assertIn("OPENING DEFECT", out)
        self.assertIn("CLOSING", out)

    def test_an_orphan_of_an_exited_seat_is_still_killed(self):
        # stop-path re-check 3: the CLI exits, its session-leader child is
        # re-parented to init and holds the pipe; only its token finds it
        import tempfile
        marker = Path(tempfile.mkdtemp()) / "orphan-alive"
        child = (f"import time, pathlib; time.sleep(2); "
                 f"pathlib.Path({str(marker)!r}).write_text('alive')")
        parent = (f"import subprocess, sys; "
                  f"subprocess.Popen([sys.executable, '-c', {child!r}], "
                  f"start_new_session=True); print('parent done')")
        rc, out = panel.run_cmd([PY, "-c", parent], 1.0)
        self.assertEqual(rc, -1)
        time.sleep(2.5)
        self.assertFalse(marker.exists(), "the orphan kept running")

    def test_a_hard_exit_still_writes_the_summary(self):
        # stop-path re-check 5: a seat stuck where no kill reaches it (an
        # HTTP call) forced the hard exit before any summary was written
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "out"
        proc = self.driver(tmp, (
            "panel.HARD_EXIT_AFTER = 1\n"
            "def stuck(p, r, t):\n"
            "    time.sleep(60)\n"
            "    return 0, 'never'\n"
            "def quick(p, r, t):\n"
            "    return 0, 'LANDED\\nFINDINGS_JSON\\n[]\\nEND_FINDINGS_JSON'\n"
            "panel.PANEL.clear()\n"
            "panel.PANEL.update(stuck=stuck, quick=quick)\n"
            "def later():\n"
            "    time.sleep(1.0); os.kill(os.getpid(), signal.SIGTERM)\n"
            "import threading; threading.Thread(target=later).start()\n"
            f"sys.exit(panel.main(['--task', 'q', '--seats', 'stuck,quick', "
            f"'--no-merge', '--out', {str(out)!r}, '--repo', {str(tmp)!r}]))\n"))
        proc.wait(timeout=20)
        self.assertEqual(proc.returncode, 143)
        summaries = list(out.glob("*/_summary.md"))
        self.assertEqual(len(summaries), 1)
        self.assertIn("quick", summaries[0].read_text())

    def test_a_retry_refused_by_a_stop_keeps_the_attempt_that_ran(self):
        # stop-path re-check 6
        entry = {"cmd": {"argv": ["x", "{prompt}"]}}
        calls = iter([(1, "PARTIAL ANSWER\nRetry-After: 0"),
                      (panel.NOT_STARTED, "panel is stopping")])
        with patch.object(panel, "run_cmd", side_effect=lambda *a, **k: next(calls)):
            code, out = panel.make_seat(entry)("p", "/r", 100)
        self.assertEqual(code, 1)
        self.assertIn("PARTIAL ANSWER", out)

    def test_not_started_cannot_be_a_real_signal_exit(self):
        # stop-path re-check 7: -7 is what a SIGEMT/SIGBUS death returns
        self.assertLess(panel.NOT_STARTED, -64)

    def test_an_oversized_command_line_says_how_to_fix_it(self):
        err = OSError(errno.E2BIG, "Argument list too long")
        with patch.object(panel.subprocess, "Popen", side_effect=err):
            rc, out = panel.run_cmd(["x", "y" * 2048], 5)
        self.assertEqual(rc, -2)
        self.assertIn('"stdin": true', out)

    def test_every_stdin_seat_keeps_the_prompt_out_of_argv(self):
        seen = []

        def capture(cmd, timeout, stdin_text=None, env=None):
            seen.append((list(cmd), stdin_text))
            return 0, "ok"

        with patch.object(panel, "run_cmd", side_effect=capture):
            for name, entry in panel.REGISTRY.items():
                if "cmd" in entry and entry["cmd"].get("stdin"):
                    panel.make_seat(entry)("THE-PROMPT", "/r", 5)
        self.assertTrue(seen)
        for argv, stdin_text in seen:
            self.assertNotIn("THE-PROMPT", " ".join(argv))
            self.assertEqual(stdin_text, "THE-PROMPT")


class KeyRedactionTests(unittest.TestCase):
    def test_a_non_http_error_never_carries_the_key(self):
        # D5: a stream error / exception that echoes the key
        spec = {"url": "https://x", "key_from": {"env": "UNUSED"},
                "body": {"q": "{prompt}"}, "extract": ["a"]}
        boom = RuntimeError("stream error: invalid API key test-secret-123")
        with patch.object(panel, "load_key",
                          return_value=("test-secret-123", None)), \
                patch.object(panel.urllib.request, "urlopen", side_effect=boom):
            rc, detail = panel.http_seat([spec], "p", "/unused", 5)
        self.assertEqual(rc, -3)
        self.assertNotIn("test-secret-123", detail)
        self.assertIn("[key]", detail)


class RunStateTests(TempRunCase):
    def ok_seat(self, claim="real defect"):
        return lambda p, r, t: (0, findings_block([finding(claim, "a.py", 1)]))

    def test_an_existing_directory_is_never_reused_for_a_new_run(self):
        # D7: the directory, not run.json, is the claim
        fixed = panel.datetime.datetime(2026, 11, 9, 12, 0, 0)
        (self.tmp / "20261109_120000").mkdir()
        self.use_seats({"a": self.ok_seat()})
        with patch.object(panel.datetime, "datetime") as dt:
            dt.now.return_value = fixed
            panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                        "--repo", str(self.tmp)])
        self.assertTrue((self.tmp / "20261109_120000_2" / "a.md").exists())
        self.assertEqual(list((self.tmp / "20261109_120000").iterdir()), [])

    def test_resume_refuses_while_another_process_holds_the_run(self):
        # D8 (+ Codex re-check 4): an OS lock, not a pid check, so two
        # resumes cannot both pass; a separate open() is a separate holder
        self.use_seats({"a": self.ok_seat(), "b": lambda p, r, t: (1, "x")})
        panel.main(["--task", "q", "--seats", "a,b", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        holder = open(rundir / panel.RUN_LOCK, "a+")
        panel._lock_file(holder, blocking=False)
        try:
            with self.assertRaises(SystemExit) as stop:
                panel.main(["--resume", str(rundir), "--repo", str(self.tmp)])
            self.assertIn("still in progress", str(stop.exception))
        finally:
            holder.close()
        panel.main(["--resume", str(rundir), "--repo", str(self.tmp)])
        self.assertEqual(panel.seat_status(rundir, "b"), (True, 1))

    def test_a_dead_owner_does_not_block_resume(self):
        self.use_seats({"a": lambda p, r, t: (1, "x")})
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        meta = json.loads((rundir / "run.json").read_text())
        meta.update(completed=False, pid=2 ** 22 + 12345)  # no such process
        (rundir / "run.json").write_text(json.dumps(meta))
        self.use_seats({"a": self.ok_seat()})
        panel.main(["--resume", str(rundir), "--repo", str(self.tmp)])
        self.assertEqual(panel.seat_status(rundir, "a"), (True, 0))

    def test_a_refill_in_flight_reads_as_running_not_complete(self):
        # Codex re-check 5: the old failure file made the run look done
        self.use_seats({"a": self.ok_seat(), "b": lambda p, r, t: (1, "x")})
        panel.main(["--task", "q", "--seats", "a,b", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        seen = {}

        def refill(p, r, t):
            meta = json.loads((rundir / "run.json").read_text())
            mid = panel.collect_run(rundir, meta)
            seen.update(complete=mid["complete"], pending=mid["pending"])
            return 0, findings_block([])

        self.use_seats({"a": self.ok_seat(), "b": refill})
        panel.main(["--resume", str(rundir), "--repo", str(self.tmp)])
        self.assertEqual(seen, {"complete": False, "pending": ["b"]})
        self.assertTrue((rundir / "b.prev.md").exists())
        self.assertEqual(panel.seat_status(rundir, "b"), (True, 0))

    def test_the_health_file_is_locked_on_windows_too(self):
        # Codex re-check 8: without fcntl only a thread lock remained
        from unittest.mock import MagicMock
        fake = MagicMock(LK_LOCK=1, LK_NBLCK=2)
        with patch.object(panel, "fcntl", None), \
                patch.object(panel, "msvcrt", fake):
            panel.record_seat_health("g", 1, "quota")
        self.assertTrue(fake.locking.called)

    def test_a_corrupt_health_file_recovers_on_the_next_write(self):
        # D9: a torn write used to leave the file broken for good
        path = self.tmp / "runs" / "_seat-health.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"half": ')
        panel.record_seat_health("g", 1, "quota exhausted")
        data = json.loads(path.read_text())
        self.assertFalse(data["g"]["ok"])

    def test_a_seat_killed_by_signal_7_still_lands(self):
        self.use_seats({"a": lambda p, r, t: (-7, "answer before SIGEMT")})
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        self.assertEqual(panel.seat_status(rundir, "a"), (True, -7))

    def test_a_failed_seat_never_votes(self):
        # D12: listed as FAILED, so it must not also count toward consensus
        block = findings_block([finding("shared claim", "a.py", 1)])
        self.use_seats({"good": lambda p, r, t: (0, block),
                        "bad": lambda p, r, t: (7, block)})
        panel.main(["--task", "q", "--seats", "good,bad", "--out",
                    str(self.tmp), "--repo", str(self.tmp)])
        result = json.loads(
            (panel._latest_run(self.tmp) / "_result.json").read_text())
        self.assertEqual(result["findings"][0]["seats"], ["good"])
        self.assertIn("bad", result["failed"])

    def test_pending_means_still_running_and_refill_lists_failures(self):
        self.use_seats({"good": self.ok_seat(),
                        "bad": lambda p, r, t: (1, "no")})
        panel.main(["--task", "q", "--seats", "good,bad", "--out",
                    str(self.tmp), "--repo", str(self.tmp)])
        result = json.loads(
            (panel._latest_run(self.tmp) / "_result.json").read_text())
        self.assertTrue(result["complete"])
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["refill"], ["bad"])

    def test_a_headerless_seat_file_is_not_a_landed_answer(self):
        (self.tmp / "x.md").write_text("")
        self.assertEqual(panel.seat_status(self.tmp, "x"), (False, None))

    def test_round_two_keeps_claims_that_contain_a_pipe(self):
        self.use_seats({"a": self.ok_seat("x | y are swapped")})
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        prior = panel._prior_findings(panel._latest_run(self.tmp))
        self.assertIn("x | y are swapped", prior)

    def test_grade_writes_its_report_and_prints_nothing(self):
        # D11: the MCP server calls grade in-process; stdout is its protocol
        self.use_seats({"a": self.ok_seat("off by one in loop")})
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        key = self.tmp / "key.json"
        key.write_text(json.dumps({"defects": [
            {"id": "d1", "file": "a.py", "keywords": ["off"], "desc": "x"}]}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            panel.grade(panel._latest_run(self.tmp), key, {})
        self.assertEqual(buf.getvalue(), "")
        self.assertTrue((panel._latest_run(self.tmp) / "_grade.md").exists())


class McpFramingTests(unittest.TestCase):
    def test_a_non_object_line_is_answered_and_the_server_keeps_going(self):
        # D10: a batch array used to crash the loop
        lines = "[]\nnull\n42\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            mcp_server.serve(io.StringIO(lines), out)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r.get("error", {}).get("code") for r in replies[:3]],
                         [-32600] * 3)
        self.assertEqual(replies[3], {"jsonrpc": "2.0", "id": 7, "result": {}})

    def test_a_tool_that_prints_cannot_corrupt_the_protocol(self):
        def noisy(_a):
            print("# grade: noise on stdout")
            return {"ok": True}

        out = io.StringIO()
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "noisy", "arguments": {}}})
        # in production the protocol stream IS sys.stdout
        with patch.dict(mcp_server.HANDLERS, {"noisy": noisy}), \
                contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(out):
            mcp_server.serve(io.StringIO(line + "\n"), out)
        for text in out.getvalue().splitlines():
            json.loads(text)  # every stdout line is protocol


class DeadLaneTests(TempRunCase):
    """Batch 2: a lane that just died on quota or login sits out."""

    PPLX_401 = ('seat unavailable: HTTP Error 401: Unauthorized: {"error":'
                '{"message":"You exceeded your current quota, add credits",'
                '"type":"insufficient_quota"}}')

    def health(self, entries):
        path = self.tmp / "runs" / "_seat-health.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries))

    def ago(self, hours):
        return (panel.datetime.datetime.now()
                - panel.datetime.timedelta(hours=hours)).isoformat(
                    timespec="seconds")

    def test_failures_are_classified_quota_auth_or_other(self):
        self.assertEqual(panel.failure_kind(-3, self.PPLX_401), "quota")
        self.assertEqual(panel.failure_kind(1, "Not logged in. Please run "
                                               "/login"), "auth")
        self.assertEqual(panel.failure_kind(-1, "TIMEOUT after 2400s"),
                         "other")

    def test_opencode_model_studio_key_errors_read_as_auth(self):
        # what `opencode run` printed for the -oc seats on 2026-11-10: a wrong
        # key, then no key at all. Both read as "other", so the lane was
        # never skipped as dead
        wrong = ("\x1b[0m\n> plan \u00b7 deepseek-v4.1-flash-1017\n\x1b[91m"
                 "\x1b[1mError: \x1b[0mIncorrect API key provided. For details,"
                 " see: https://www.alibabacloud.com/help/en/model-studio/"
                 "error-code#apikey-error")
        absent = ("Error: You didn't provide an API key. You need to provide "
                  "your API key in an Authorization header using Bearer auth "
                  "(i.e. Authorization: Bearer YOUR_KEY).")
        self.assertEqual(panel.failure_kind(1, wrong), "auth")
        self.assertEqual(panel.failure_kind(1, absent), "auth")

    def test_only_recent_quota_or_auth_failures_are_dead_lanes(self):
        health = {"q": {"ok": False, "ts": self.ago(2), "kind": "quota"},
                  "a": {"ok": False, "ts": self.ago(2), "kind": "auth"},
                  "old": {"ok": False, "ts": self.ago(30), "kind": "quota"},
                  "t": {"ok": False, "ts": self.ago(1), "kind": "other"},
                  "fine": {"ok": True, "ts": self.ago(1)},
                  # recorded before kinds existed: classified from the text
                  "legacy": {"ok": False, "ts": self.ago(1), "exit": -3,
                             "error": self.PPLX_401}}
        dead = panel.dead_lanes(list(health), health)
        self.assertEqual(sorted(dead), ["a", "legacy", "q"])

    def run_preset(self, *extra):
        self.use_seats({"live": lambda p, r, t: (0, findings_block([])),
                        "pplx": lambda p, r, t: (0, findings_block([]))})
        self.health({"pplx": {"ok": False, "ts": self.ago(1), "exit": -3,
                              "error": self.PPLX_401, "kind": "quota"}})
        with patch.dict(panel.PRESETS, {"t": ["live", "pplx"]}):
            panel.main(["--task", "q", "--out", str(self.tmp), "--repo",
                        str(self.tmp), *extra])
        return panel._latest_run(self.tmp)

    def test_an_implicit_roster_leaves_out_a_dead_lane_and_says_so(self):
        rundir = self.run_preset("--preset", "t")
        meta = json.loads((rundir / "run.json").read_text())
        self.assertEqual(meta["planned"], ["live"])
        self.assertIn("pplx", meta["skipped"])
        self.assertIn("SKIPPED as dead lanes", (rundir / "_summary.md")
                      .read_text())

    def test_include_dead_seats_it_anyway(self):
        rundir = self.run_preset("--preset", "t", "--include-dead")
        meta = json.loads((rundir / "run.json").read_text())
        self.assertEqual(meta["planned"], ["live", "pplx"])

    def test_a_seat_named_explicitly_always_runs(self):
        rundir = self.run_preset("--seats", "live,pplx")
        meta = json.loads((rundir / "run.json").read_text())
        self.assertEqual(sorted(meta["planned"]), ["live", "pplx"])

    def test_a_failure_records_its_kind(self):
        self.use_seats({"pplx": lambda p, r, t: (-3, self.PPLX_401)})
        panel.main(["--task", "q", "--seats", "pplx", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        data = json.loads((self.tmp / "runs" / "_seat-health.json")
                          .read_text())
        self.assertEqual(data["pplx"]["kind"], "quota")


class RepairTests(TempRunCase):
    """Batch 2: a blockless answer gets one follow-up call to the same seat."""

    def test_a_blockless_answer_is_repaired_by_the_same_seat(self):
        prompts = []

        def glm(p, r, t):
            prompts.append(p)
            if len(prompts) == 1:
                return 0, "Long answer. The retry loop never backs off.\n"
            return 0, findings_block([finding("retry loop never backs off",
                                              "net.py", 9)])

        self.use_seats({"glm": glm})
        panel.main(["--task", "q", "--seats", "glm", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        self.assertEqual(len(prompts), 2)
        self.assertIn("Restate ONLY the claims", prompts[1])
        self.assertIn("The retry loop never backs off", prompts[1])
        body = (rundir / "glm.md").read_text()
        self.assertIn("Long answer.", body)  # the original is kept
        self.assertIn(panel.REPAIR_MARK, body)
        result = json.loads((rundir / "_result.json").read_text())
        self.assertEqual(result["no_vote"], [])
        self.assertTrue(result["seats"]["glm"]["repaired"])
        self.assertEqual(result["findings"][0]["seats"], ["glm"])
        self.assertIn("recovered by a follow-up call",
                      (rundir / "_summary.md").read_text())

    def test_a_failed_repair_leaves_an_honest_no_vote(self):
        self.use_seats({"glm": lambda p, r, t: (0, "prose only")})
        panel.main(["--task", "q", "--seats", "glm", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        self.assertEqual((rundir / "glm.md").read_text(),
                         "# glm (exit 0)\n\nprose only\n")
        result = json.loads((rundir / "_result.json").read_text())
        self.assertEqual(result["no_vote"], ["glm"])

    def test_no_repair_makes_no_follow_up_call(self):
        calls = []
        self.use_seats({"glm": lambda p, r, t: (calls.append(p),
                                                (0, "prose only"))[1]})
        panel.main(["--task", "q", "--seats", "glm", "--no-repair",
                    "--out", str(self.tmp), "--repo", str(self.tmp)])
        self.assertEqual(len(calls), 1)

    def test_a_seat_with_a_block_is_never_asked_again(self):
        calls = []
        self.use_seats({"a": lambda p, r, t: (calls.append(p),
                                              (0, findings_block([])))[1]})
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        self.assertEqual(len(calls), 1)


class QuickPresetTests(unittest.TestCase):
    def test_quick_is_six_distinct_hosted_default_lineages(self):
        seats = panel.PRESETS["quick"]
        self.assertEqual(len(seats), 6)
        self.assertEqual(len({panel.REGISTRY[n]["lineage"] for n in seats}), 6)
        for name in seats:
            entry = panel.REGISTRY[name]
            self.assertTrue(entry.get("default"), name)
            self.assertFalse(entry.get("local"), name)


def cluster(claim, seats, lineages, where_file=None, line=None,
            severity="high"):
    rep = finding(claim, where_file, line, severity)
    return {"rep": rep, "seats": list(seats), "lineages": sorted(lineages),
            "severity": severity, "claims": [claim], "where": "-",
            "files": [], "locations": []}


class MergeJudgeTests(unittest.TestCase):
    """Batch 3: the judge proposes; code decides; nothing is counted."""

    def merged(self, clusters, groups, key=None):
        record = {"key": key or panel.cluster_key(clusters),
                  "status": "applied", "groups": groups,
                  "judge": "claude-haiku", "model": "m"}
        return panel.apply_merge(clusters, record)

    def test_a_proposal_shows_as_likely_and_is_never_counted(self):
        rows = [cluster("The cache key is shared across tenants", ["kimi"],
                        ["moonshot"]),
                cluster("Two tenants read each other's cached rows because "
                        "the key holds only the user id", ["gemini-argon"],
                        ["google"])]
        out, note = self.merged(rows, [[0, 1]])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["lineages"], ["moonshot"])
        self.assertEqual(out[0]["likely_lineages"], ["google"])
        self.assertEqual(note["merged"], 1)

    def test_guards_refuse_a_shared_seat_another_place_or_a_negation(self):
        same_seat = [cluster("a", ["codex"], ["openai"]),
                     cluster("b", ["codex", "kimi"], ["openai", "moonshot"])]
        places = [cluster("x", ["codex"], ["openai"], "a.py", 1),
                  cluster("x", ["kimi"], ["moonshot"], "b.py", 1)]
        negated = [cluster("the lock is released on error", ["codex"],
                           ["openai"]),
                   cluster("the lock is never released on error", ["kimi"],
                           ["moonshot"])]
        for rows in (same_seat, places, negated):
            out, note = self.merged(rows, [[0, 1]])
            self.assertEqual(len(out), 2)
            self.assertEqual(note["refused"], 1)

    def test_a_judgement_on_other_rows_is_never_applied(self):
        rows = [cluster("a", ["codex"], ["openai"]),
                cluster("a too", ["kimi"], ["moonshot"])]
        out, note = self.merged(rows, [[0, 1]], key="not-these-rows")
        self.assertEqual(len(out), 2)
        self.assertEqual(note["status"], "stale")

    def test_groups_parse_tolerantly_and_stay_disjoint(self):
        out = 'Sure.\n```json\n{"groups": [[1, 2], [2, 3], [4, 99], [true, 5], [6, 6, 7]]}\n```'
        self.assertEqual(panel._parse_groups(out, 7), [[0, 1], [5, 6]])
        self.assertIsNone(panel._parse_groups("no json here", 3))
        self.assertEqual(panel._parse_groups('{"groups": []}', 3), [])


class MergeRunTests(TempRunCase):
    def seats(self, judge_calls, reply='{"groups": [[1, 2]]}'):
        def judge(p, r, t):
            judge_calls.append(p)
            return 0, reply
        self.use_seats({
            "a": lambda p, r, t: (0, findings_block([finding(
                "fire stair count is short by one on level 3")])),
            "b": lambda p, r, t: (0, findings_block([finding(
                "level 3 needs another exit stair")])),
            "claude-haiku": judge})

    def run_panel(self, *extra):
        panel.main(["--task", "q", "--seats", "a,b", "--out", str(self.tmp),
                    "--repo", str(self.tmp), *extra])
        return panel._latest_run(self.tmp)

    def test_a_run_asks_the_judge_once_and_shows_likely_rows(self):
        calls = []
        self.seats(calls)
        rundir = self.run_panel()
        self.assertEqual(len(calls), 1)
        self.assertIn("[1]", calls[0])
        result = json.loads((rundir / "_result.json").read_text())
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(len(result["findings"][0]["lineages"]), 1)
        self.assertEqual(len(result["findings"][0]["likely_lineages"]), 1)
        summary = (rundir / "_summary.md").read_text()
        self.assertIn("(+1 likely)", summary)
        self.assertIn("Likely the same claim", summary)
        # status polls only read the cached judgement
        panel.collect_run(rundir, json.loads((rundir / "run.json")
                                             .read_text()))
        self.assertEqual(len(calls), 1)

    def test_a_resume_that_changes_nothing_does_not_ask_again(self):
        calls = []
        self.seats(calls)
        rundir = self.run_panel()
        panel.main(["--resume", str(rundir), "--repo", str(self.tmp)])
        self.assertEqual(len(calls), 1)

    def test_no_merge_never_asks_the_judge(self):
        calls = []
        self.seats(calls)
        self.run_panel("--no-merge")
        self.assertEqual(calls, [])

    def test_a_bad_judge_reply_leaves_lexical_counts_and_says_so(self):
        calls = []
        self.seats(calls, reply="I think items 1 and 2 match.")
        rundir = self.run_panel()
        summary = (rundir / "_summary.md").read_text()
        self.assertIn("Merge judge: failed", summary)
        result = json.loads((rundir / "_result.json").read_text())
        self.assertEqual(len(result["findings"]), 2)


class VerdictTests(TempRunCase):
    def make_run(self):
        self.use_seats({
            "a": lambda p, r, t: (0, findings_block([
                finding("off by one in the pager", "p.py", 3),
                finding("cache never expires", "c.py", 9)])),
            "b": lambda p, r, t: (0, findings_block([
                finding("off by one in the pager", "p.py", 4)]))})
        panel.main(["--task", "q", "--seats", "a,b", "--no-merge", "--out",
                    str(self.tmp), "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        result = json.loads((rundir / "_result.json").read_text())
        ids = {f["claim"]: f["id"] for f in result["findings"]}
        return rundir, ids

    def test_verdicts_build_a_per_seat_track_record(self):
        rundir, ids = self.make_run()
        panel.record_verdict(rundir, ids["off by one in the pager"],
                             "confirmed", "reproduced with a 3-item list")
        panel.record_verdict(rundir, "#" + ids["cache never expires"],
                             "rejected")
        stats = panel.track_record()
        self.assertEqual(stats["a"]["confirmed"], 1)
        self.assertEqual(stats["a"]["rejected"], 1)
        self.assertEqual(stats["b"], {"confirmed": 1, "rejected": 0,
                                      "solo_confirmed": 0, "prior": 0})
        self.assertEqual(panel.track_label("a", stats), "1/2 confirmed")
        saved = json.loads((rundir / "_verdicts.json").read_text())
        self.assertEqual(len(saved), 2)

    def test_the_latest_verdict_on_a_finding_wins(self):
        rundir, ids = self.make_run()
        fid = ids["cache never expires"]
        panel.record_verdict(rundir, fid, "rejected")
        panel.record_verdict(rundir, fid, "confirmed")
        stats = panel.track_record()
        self.assertEqual((stats["a"]["confirmed"], stats["a"]["rejected"]),
                         (1, 0))
        self.assertEqual(stats["a"]["solo_confirmed"], 1)

    def test_a_verdict_on_an_older_model_is_prior_not_current(self):
        rundir, ids = self.make_run()
        panel.record_verdict(rundir, ids["cache never expires"], "confirmed")
        line = json.loads((self.tmp / "runs" / panel.TRACK_FILE)
                          .read_text().splitlines()[0])
        line["models"] = {"a": "old-model"}
        (self.tmp / "runs" / panel.TRACK_FILE).write_text(json.dumps(line))
        with patch.dict(panel.REGISTRY, {"a": {"lineage": "x", "cmd": {
                "argv": ["x", "-m", "new-model"]}}}):
            stats = panel.track_record()
        self.assertEqual(stats["a"]["confirmed"], 0)
        self.assertEqual(stats["a"]["prior"], 1)

    def test_an_unknown_finding_is_an_error_and_ids_are_stable(self):
        rundir, ids = self.make_run()
        with self.assertRaises(ValueError):
            panel.record_verdict(rundir, "deadbeef", "confirmed")
        meta = json.loads((rundir / "run.json").read_text())
        again = panel.collect_run(rundir, meta)
        self.assertEqual({f["claim"]: f["id"] for f in again["findings"]},
                         ids)

    def test_cli_and_mcp_record_verdicts_and_summaries_show_them(self):
        rundir, ids = self.make_run()
        with contextlib.redirect_stdout(io.StringIO()):
            panel.main(["--verdict", str(rundir), "--finding",
                        ids["off by one in the pager"], "--mark",
                        "confirmed"])
        out = mcp_server.tool_panel_verdict(
            {"run_dir": str(rundir),
             "verdicts": [{"finding": ids["cache never expires"],
                           "verdict": "rejected"}]})
        self.assertEqual(out["recorded"][0]["verdict"], "rejected")
        meta = json.loads((rundir / "run.json").read_text())
        panel.collect_run(rundir, meta)
        self.assertIn("track 1/2 confirmed",
                      (rundir / "_summary.md").read_text())
        self.assertIn("models", meta)


class EscalationTests(TempRunCase):
    """Batch 3: ask the quick tier first; seat the rest only on doubt."""

    REG = {"q1": {"lineage": "l1"}, "q2": {"lineage": "l2"},
           "q3": {"lineage": "l3"}, "q4": {"lineage": "l4"},
           "slow": {"lineage": "l5", "default": True}}

    def run_panel(self, quick_answers, mode=("--task", "q")):
        calls = []

        def seat(name, answer):
            return lambda p, r, t: (calls.append(name), (0, answer))[1]

        seats = {n: seat(n, a) for n, a in quick_answers.items()}
        seats["slow"] = seat("slow", findings_block([]))
        self.use_seats(seats)
        with patch.dict(panel.REGISTRY, self.REG), \
                patch.dict(panel.PRESETS, {"quick": list(quick_answers)}):
            panel.main([*mode, "--escalate", "--no-merge", "--out",
                        str(self.tmp), "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        return calls, json.loads((rundir / "run.json").read_text())

    def agreeing(self):
        both = findings_block([finding("the pager drops the last page",
                                       "p.py", 3)])
        return {"q1": both, "q2": both, "q3": findings_block([]),
                "q4": findings_block([])}

    def test_agreement_stops_at_the_quick_tier(self):
        calls, meta = self.run_panel(self.agreeing())
        self.assertNotIn("slow", calls)
        self.assertEqual(meta["planned"], ["q1", "q2", "q3", "q4"])
        self.assertFalse(meta["escalation"]["escalated"])

    def test_a_one_lineage_high_finding_escalates(self):
        answers = self.agreeing()
        answers["q3"] = findings_block([finding("SQL built by string "
                                                "concatenation", "db.py", 7)])
        calls, meta = self.run_panel(answers)
        self.assertIn("slow", calls)
        self.assertTrue(meta["escalation"]["escalated"])
        self.assertIn("one lineage", meta["escalation"]["reason"])
        self.assertIn("slow", meta["planned"])

    def test_too_few_voting_lineages_escalates(self):
        answers = self.agreeing()
        answers["q4"] = "prose only"
        with patch.object(panel, "repair_block",
                          side_effect=lambda n, a, *rest: a):
            calls, meta = self.run_panel(answers)
        self.assertIn("slow", calls)
        self.assertIn("3 lineage", meta["escalation"]["reason"])

    def test_a_split_diff_verdict_escalates(self):
        diff = self.tmp / "c.diff"
        diff.write_text("--- a\n+++ b\n")
        # findings_block() embeds "VERDICT: FINDINGS"; a clean seat must not
        clean = "All invariants hold.\nVERDICT: CLEAN\n\nFINDINGS_JSON\n[]\nEND_FINDINGS_JSON\n"
        answers = {"q1": clean, "q2": clean, "q3": clean,
                   "q4": findings_block([finding("x", severity="low")])}
        calls, meta = self.run_panel(answers, mode=("--diff", str(diff)))
        self.assertIn("slow", calls)
        self.assertIn("split", meta["escalation"]["reason"])

    def test_the_run_is_not_complete_while_it_decides(self):
        seen = {}
        real = panel.escalation_reason

        def spy(result, meta):
            seen["complete"] = result["complete"]
            return real(result, meta)

        with patch.object(panel, "escalation_reason", side_effect=spy):
            self.run_panel(self.agreeing())
        self.assertFalse(seen["complete"])

    def test_escalate_refuses_an_explicit_roster(self):
        with self.assertRaises(SystemExit):
            panel.main(["--task", "q", "--escalate", "--seats", "a",
                        "--out", str(self.tmp), "--repo", str(self.tmp)])


class FinalReviewTests(TempRunCase):
    """Codex review of batches 2-3 (2026-11-09), one test per finding."""

    def two_seat_run(self, answers, *extra):
        self.use_seats({n: (lambda a: lambda p, r, t: (0, a))(a)
                        for n, a in answers.items()})
        panel.main(["--task", "q", "--seats", ",".join(answers), "--out",
                    str(self.tmp), "--repo", str(self.tmp), *extra])
        rundir = panel._latest_run(self.tmp)
        return rundir, json.loads((rundir / "_result.json").read_text())

    def test_01_not_missing_disputes_missing(self):
        per = {"codex": [finding("authorization check is missing from the "
                                 "delete handler", "h.py", 9)],
               "kimi": [finding("authorization check is not missing from "
                                "the delete handler", "h.py", 9)]}
        self.assertEqual(len(panel.cluster_findings(per, REG)), 2)

    def test_02_a_changed_place_makes_the_merge_cache_stale(self):
        a = cluster("stair is short", ["codex"], ["openai"])
        b = dict(a, where="disputed: stairs.py / generated/stairs.py")
        self.assertNotEqual(panel.cluster_key([a]), panel.cluster_key([b]))

    def test_03_one_seats_same_sentence_about_two_files_gets_two_ids(self):
        rundir, result = self.two_seat_run({"a": findings_block([
            finding("missing bounds check", "a.py", 10),
            finding("missing bounds check", "b.py", 20)])}, "--no-merge")
        ids = [f["id"] for f in result["findings"]]
        self.assertEqual(len(set(ids)), 2)
        entry = panel.record_verdict(rundir, ids[1], "confirmed")
        self.assertEqual(entry["finding"], ids[1])

    def test_04_a_multi_route_seat_is_credited_by_its_wiring(self):
        ident = panel.seat_identity(panel.REGISTRY["pplx"])
        self.assertEqual(ident.split("|")[0], "fast-search")
        self.assertIn("|", ident)

    def test_05_a_moved_row_keeps_its_own_severity(self):
        rows = [cluster("pager drops the last page", ["codex", "kimi"],
                        ["moonshot", "openai"], severity="low"),
                cluster("the pager loses its final page", ["grok"], ["xai"],
                        severity="high")]
        panel.assign_ids(rows)
        out, _ = panel.apply_merge(rows, {"key": panel.cluster_key(rows),
                                          "status": "applied",
                                          "groups": [[0, 1]]})
        record = panel._finding_record(out[0], {})
        self.assertEqual(record["likely_merged"][0]["severity"], "high")

    def test_06_concurrent_verdicts_all_survive(self):
        import threading
        rundir, result = self.two_seat_run({"a": findings_block([
            finding("one", "a.py", 1), finding("two", "b.py", 2),
            finding("three", "c.py", 3)])}, "--no-merge")
        ids = [f["id"] for f in result["findings"]]
        threads = [threading.Thread(target=panel.record_verdict,
                                    args=(rundir, i, "confirmed"))
                   for i in ids for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        saved = json.loads((rundir / "_verdicts.json").read_text())
        self.assertEqual(sorted(saved), sorted(ids))

    def test_07_two_spellings_of_one_run_count_once(self):
        rundir, result = self.two_seat_run({"a": findings_block([
            finding("only one", "a.py", 1)])}, "--no-merge")
        fid = result["findings"][0]["id"]
        panel.record_verdict(rundir, fid, "confirmed")
        cwd = os.getcwd()
        os.chdir(rundir)
        try:
            panel.record_verdict(".", fid, "confirmed")
        finally:
            os.chdir(cwd)
        self.assertEqual(panel.track_record()["a"]["confirmed"], 1)

    def test_08_every_earlier_attempt_survives_repeated_resumes(self):
        attempts = iter([(1, "first"), (2, "second"),
                         (0, findings_block([]))])
        self.use_seats({"a": lambda p, r, t: next(attempts)})
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        panel.main(["--resume", str(rundir), "--repo", str(self.tmp)])
        panel.main(["--resume", str(rundir), "--repo", str(self.tmp)])
        self.assertIn("first", (rundir / "a.prev.md").read_text())
        self.assertIn("second", (rundir / "a.prev2.md").read_text())

    def test_09_the_answer_is_on_disk_before_its_repair_call(self):
        seen = {}

        def glm(p, r, t):
            if "Restate ONLY" in p:
                rundir = panel._latest_run(self.tmp)
                seen["file"] = (rundir / "glm.md").read_text()
                return 0, findings_block([])
            return 0, "the original paid answer"

        self.use_seats({"glm": glm})
        panel.main(["--task", "q", "--seats", "glm", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        self.assertIn("the original paid answer", seen["file"])

    def test_10_http_seats_do_not_start_once_stopping(self):
        spec = {"url": "https://x", "body": {"q": "{prompt}"},
                "extract": ["a"]}
        panel._STOPPING.set()
        try:
            with patch.object(panel.urllib.request, "urlopen") as req:
                rc, _ = panel.http_seat([spec], "p", "/r", 5)
        finally:
            panel._STOPPING.clear()
        self.assertEqual(rc, panel.NOT_STARTED)
        self.assertFalse(req.called)

    def test_11_an_unregistered_child_is_still_reaped(self):
        # last re-check 2: a mask could not cover other threads; the reaper
        # now sweeps every child of this process from a ps snapshot
        import subprocess
        child = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        try:
            panel.reap_live_seats()
            child.wait(timeout=5)
        finally:
            panel._STOPPING.clear()
            if child.poll() is None:
                child.kill()
        self.assertIsNotNone(child.returncode)

    def test_11b_a_serial_seat_still_receives_sigterm(self):
        # last re-check 5: a child must not inherit a blocked signal mask
        code = ("import os, signal; os.kill(os.getpid(), signal.SIGTERM); "
                "print('SURVIVED_SIGTERM')")
        rc, out = panel.run_cmd([PY, "-c", code], 10)
        self.assertNotIn("SURVIVED_SIGTERM", out)
        self.assertNotEqual(rc, 0)

    def test_11c_answers_are_persisted_by_the_worker_that_got_them(self):
        # last re-check 1: a stop must not find an answer still waiting for
        # the main thread to write it
        import threading
        writers = []
        real = panel._atomic_write

        def spy(path, text):
            if Path(path).suffix == ".md" and not Path(path).name.startswith("_"):
                writers.append(threading.current_thread()
                               is threading.main_thread())
            return real(path, text)

        self.use_seats({"a": lambda p, r, t: (0, findings_block([])),
                        "b": lambda p, r, t: (0, findings_block([]))})
        with patch.object(panel, "_atomic_write", side_effect=spy):
            panel.main(["--task", "q", "--seats", "a,b", "--no-merge",
                        "--out", str(self.tmp), "--repo", str(self.tmp)])
        seat_writes = writers[-2:]
        self.assertEqual(seat_writes, [False, False])

    def test_12_a_timeout_keeps_partial_stderr(self):
        code = ("import sys, time; print('ANSWER ON STDERR', file=sys.stderr, "
                "flush=True); time.sleep(30)")
        rc, out = panel.run_cmd([PY, "-c", code], 0.5)
        self.assertEqual(rc, -1)
        self.assertIn("ANSWER ON STDERR", out)

    def test_13_a_timeout_about_payments_is_not_a_quota_failure(self):
        partial = ("TIMEOUT after 2400s\n\npartial output before the "
                   "timeout:\nThe payment form lacks validation; billing "
                   "totals are wrong.")
        self.assertEqual(panel.failure_kind(-1, partial), "other")
        long_answer = "quota " + "x" * 5000 + "\nError: connection reset"
        self.assertEqual(panel.failure_kind(1, long_answer), "other")

    def test_14_skip_reaches_the_merge_judge(self):
        calls = []
        self.use_seats({
            "a": lambda p, r, t: (0, findings_block([finding("x one")])),
            "b": lambda p, r, t: (0, findings_block([finding("x two")])),
            "claude-haiku": lambda p, r, t: (calls.append(p),
                                             (0, '{"groups": []}'))[1]})
        panel.main(["--task", "q", "--seats", "a,b", "--skip",
                    "claude-haiku", "--out", str(self.tmp), "--repo",
                    str(self.tmp)])
        self.assertEqual(calls, [])

    def test_20_a_seat_under_repair_reads_as_running(self):
        seen = {}

        def glm(p, r, t):
            if "Restate ONLY" in p:
                rundir = panel._latest_run(self.tmp)
                meta = json.loads((rundir / "run.json").read_text())
                mid = panel.collect_run(rundir, meta)
                seen.update(complete=mid["complete"], pending=mid["pending"])
                return 0, findings_block([finding("late high", "x.py", 1)])
            return 0, "answer without a block"

        self.use_seats({"glm": glm})
        panel.main(["--task", "q", "--seats", "glm", "--no-merge", "--out",
                    str(self.tmp), "--repo", str(self.tmp)])
        self.assertEqual(seen, {"complete": False, "pending": ["glm"]})

    def test_21_two_negations_are_not_none(self):
        per = {"codex": [finding("the lock is released and cleanup is "
                                 "performed", "db.py", 5)],
               "kimi": [finding("the lock is not released and cleanup is "
                                "not performed", "db.py", 5)]}
        self.assertEqual(len(panel.cluster_findings(per, REG)), 2)

    def test_22_a_moved_high_singleton_still_escalates(self):
        result = {"seats": {n: {"exit": 0, "ok": True, "lineage": n,
                                "verdict": "?"} for n in "abcd"},
                  "findings": [{"severity": "low", "lineages": ["a", "b"],
                                "likely_lineages": ["c"],
                                "likely_merged": [{"severity": "high",
                                                   "lineages": ["c"]}]}]}
        self.assertIn("one lineage",
                      panel.escalation_reason(result, {"mode": "task"}))

    def test_23_a_timeout_keeps_stdout_and_stderr(self):
        code = ("import sys, time; print('PROGRESS', flush=True); "
                "print('THE ANSWER', file=sys.stderr, flush=True); "
                "time.sleep(30)")
        rc, out = panel.run_cmd([PY, "-c", code], 0.5)
        self.assertIn("PROGRESS", out)
        self.assertIn("THE ANSWER", out)

    def test_24_resume_honours_skip_of_the_judge(self):
        calls = []
        self.use_seats({
            "a": lambda p, r, t: (0, findings_block([finding("x one")])),
            "b": lambda p, r, t: (1, "failed"),
            "claude-haiku": lambda p, r, t: (calls.append(p),
                                             (0, "not json"))[1]})
        panel.main(["--task", "q", "--seats", "a,b", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        before = len(calls)
        self.use_seats({
            "a": lambda p, r, t: (0, findings_block([finding("x one")])),
            "b": lambda p, r, t: (0, findings_block([finding("x two")])),
            "claude-haiku": lambda p, r, t: (calls.append(p),
                                             (0, "not json"))[1]})
        panel.main(["--resume", str(panel._latest_run(self.tmp)), "--repo",
                    str(self.tmp), "--skip", "claude-haiku"])
        self.assertEqual(len(calls), before)

    def test_17_a_quoted_verdict_inside_json_is_not_the_verdict(self):
        answer = ("VERDICT: FINDINGS\n\nFINDINGS_JSON\n"
                  '[{"claim": "The reporter incorrectly prints VERDICT: '
                  'CLEAN after failed checks", "severity": "high"}]\n'
                  "END_FINDINGS_JSON\n")
        self.assertEqual(panel._verdict_line(answer), "VERDICT: FINDINGS")

    def test_18_likely_lineages_never_suppress_escalation(self):
        result = {"seats": {n: {"exit": 0, "ok": True, "lineage": n,
                                "verdict": "?"} for n in "abcd"},
                  "findings": [{"severity": "high", "lineages": ["a"],
                                "likely_lineages": ["b"]}]}
        self.assertIn("one lineage",
                      panel.escalation_reason(result, {"mode": "task"}))


class FinalEscalationTests(TempRunCase):
    REG = EscalationTests.REG
    run_panel = EscalationTests.run_panel
    agreeing = EscalationTests.agreeing

    def test_15_an_empty_quick_tier_escalates(self):
        calls = []
        self.use_seats({"q1": lambda p, r, t: (calls.append("q1"),
                                               (0, findings_block([])))[1],
                        "slow": lambda p, r, t: (calls.append("slow"),
                                                 (0, findings_block([])))[1]})
        with patch.dict(panel.REGISTRY, self.REG), \
                patch.dict(panel.PRESETS, {"quick": ["q1"]}):
            panel.main(["--task", "q", "--escalate", "--skip", "q1",
                        "--no-merge", "--out", str(self.tmp), "--repo",
                        str(self.tmp)])
        meta = json.loads((panel._latest_run(self.tmp) / "run.json")
                          .read_text())
        self.assertEqual(calls, ["slow"])
        self.assertIn("no quick-tier seat", meta["escalation"]["reason"])

    def test_16_a_resume_decides_before_seating_the_rest(self):
        calls, meta = self.run_panel(self.agreeing())
        rundir = panel._latest_run(self.tmp)
        # rewind to "quick tier landed, decision interrupted"
        meta["escalation"]["decided"] = False
        meta["planned"] = ["q1", "q2", "q3", "q4", "slow"]
        (rundir / "run.json").write_text(json.dumps(meta))
        slow_calls = []
        seats = dict(panel.PANEL)
        seats["slow"] = lambda p, r, t: (slow_calls.append(1),
                                         (0, findings_block([])))[1]
        self.use_seats(seats)
        with patch.dict(panel.REGISTRY, self.REG):
            panel.main(["--resume", str(rundir), "--repo", str(self.tmp),
                        "--no-merge"])
        after = json.loads((rundir / "run.json").read_text())
        self.assertEqual(slow_calls, [])
        self.assertTrue(after["escalation"]["decided"])
        self.assertNotIn("slow", after["planned"])

    def test_19_no_second_tier_still_waits_for_the_decision(self):
        seen = {}
        real = panel.escalation_reason

        def spy(result, meta):
            seen["complete"] = result["complete"]
            return real(result, meta)

        reg = dict(self.REG, slow={"lineage": "l5", "default": False})
        both = findings_block([finding("p", "p.py", 3)])
        answers = {"q1": both, "q2": both, "q3": findings_block([]),
                   "q4": findings_block([])}
        self.use_seats({n: (lambda a: lambda p, r, t: (0, a))(a)
                        for n, a in answers.items()})
        with patch.dict(panel.REGISTRY, reg), \
                patch.dict(panel.PRESETS, {"quick": list(answers)}), \
                patch.object(panel, "escalation_reason", side_effect=spy):
            panel.main(["--task", "q", "--escalate", "--no-merge", "--out",
                        str(self.tmp), "--repo", str(self.tmp)])
        self.assertFalse(seen["complete"])


def _load_script(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class InstallerTests(unittest.TestCase):
    def test_windows_paths_make_a_codex_config_that_parses(self):
        # D14: C:\Users in a bare TOML basic string is an invalid \U escape
        inst = _load_script("panel-install")
        with tempfile_dir() as tmp:
            codex = tmp / "config.toml"
            codex.write_text('model = "x"\n')
            win = r"C:\Users\someone\projects\panel\skills\panel\mcp_server.py"
            with patch.object(inst, "CODEX", codex), \
                    patch.object(inst, "SERVER", win), \
                    patch.object(inst, "python_exe",
                                 return_value=r"C:\Python312\python.exe"):
                inst.install_codex(set(), False)
            parsed = tomllib.loads(codex.read_text())
        self.assertEqual(parsed["mcp_servers"]["panel"]["args"], [win])

    def test_a_non_bmp_path_still_makes_valid_toml(self):
        inst = _load_script("panel-install")
        with tempfile_dir() as tmp:
            codex = tmp / "config.toml"
            codex.write_text("")
            path = "C:\\Users\\\U0001F98A\\panel\\mcp_server.py"
            with patch.object(inst, "CODEX", codex), \
                    patch.object(inst, "SERVER", path), \
                    patch.object(inst, "python_exe", return_value="python3"):
                inst.install_codex(set(), False)
            parsed = tomllib.loads(codex.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcp_servers"]["panel"]["args"], [path])

    def test_tag_names_become_distinct_flat_folder_names(self):
        tag = _load_script("panel-on-tag")
        a, b = tag.tag_dirname("release/1.0"), tag.tag_dirname("release_1.0")
        self.assertNotEqual(a, b)
        self.assertNotIn("/", a)
        self.assertEqual(tag.tag_dirname("v1.2.3"), "v1.2.3")

    def test_a_malformed_project_config_is_backed_up_not_lost(self):
        inst = _load_script("panel-install")
        with tempfile_dir() as tmp:
            (tmp / ".mcp.json").write_text("{not json")
            inst.write_project_mcp(tmp, False)
            backups = list(tmp.glob(".mcp.json.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "{not json")
            self.assertIn("panel", json.loads(
                (tmp / ".mcp.json").read_text())["mcpServers"])


@contextlib.contextmanager
def tempfile_dir():
    import shutil
    import tempfile
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
