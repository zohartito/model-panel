#!/usr/bin/env python3
"""Tests for panel.py. Stdlib only -- python3 -m unittest discover -s tests.

Every seat here is a fake: the point is to prove the harness behaves (persists
work as it lands, resumes without re-paying, clusters by lineage, grades) with
zero real model calls.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "panel"))
import panel  # noqa: E402


def findings_block(items):
    return ("prose here\nVERDICT: FINDINGS\n\nFINDINGS_JSON\n"
            + json.dumps(items) + "\nEND_FINDINGS_JSON\n")


class RegistryTests(unittest.TestCase):
    def test_every_seat_is_registered(self):
        reg = panel.load_registry()
        missing = [n for n in panel.PANEL
                   if reg[n].get("note") == "not in registry"]
        self.assertEqual(missing, [], "seats.json must describe every seat")

    def test_claude_seats_are_one_lineage(self):
        lin = {panel.lineage_of(s) for s in
               ("claude-sonnet", "claude-opus", "claude-fable")}
        self.assertEqual(lin, {"anthropic"})

    def test_grok_scaffolds_are_one_lineage(self):
        self.assertEqual(panel.lineage_of("grok"),
                         panel.lineage_of("cursor-grok"))

    def test_local_mac_shares_gemini_lineage(self):
        # gemma4 and the gemini seat are the same family: seating both must not
        # look like two independent voices agreeing.
        self.assertEqual(panel.lineage_of("local-mac"),
                         panel.lineage_of("gemini"))


class PlanSeatsTests(unittest.TestCase):
    class Args:
        skip = ""
        seats = ""
        include_local = False

    def test_defaults_exclude_local_and_qwen(self):
        chosen = panel.plan_seats(self.Args(), panel.REGISTRY)
        self.assertIn("codex", chosen)
        self.assertNotIn("qwen", chosen)
        self.assertFalse([c for c in chosen if c.startswith("local")])

    def test_include_local_adds_locals_but_never_qwen(self):
        args = self.Args()
        args.include_local = True
        chosen = panel.plan_seats(args, panel.REGISTRY)
        self.assertIn("local-27b", chosen)
        self.assertNotIn("qwen", chosen,
                         "Token Plan lane must stay opt-in by name")

    def test_unknown_seat_is_an_error(self):
        args = self.Args()
        args.seats = "codex,nope"
        with self.assertRaises(SystemExit):
            panel.plan_seats(args, panel.REGISTRY)

    def test_skip_wins_over_seats(self):
        args = self.Args()
        args.seats = "codex,kimi"
        args.skip = "kimi"
        self.assertEqual(panel.plan_seats(args, panel.REGISTRY), ["codex"])


class ParseFindingsTests(unittest.TestCase):
    def test_happy_path(self):
        text = findings_block([{"file": "a.py", "line": 4, "claim": "boom",
                                "severity": "high"}])
        items, ok = panel.parse_findings(text)
        self.assertTrue(ok)
        self.assertEqual(items[0]["file"], "a.py")
        self.assertEqual(items[0]["severity"], "high")

    def test_empty_list_is_a_vote(self):
        items, ok = panel.parse_findings(findings_block([]))
        self.assertTrue(ok)
        self.assertEqual(items, [])

    def test_missing_block_is_no_vote(self):
        items, ok = panel.parse_findings("VERDICT: CLEAN")
        self.assertFalse(ok)
        self.assertEqual(items, [])

    def test_malformed_json_is_no_vote(self):
        items, ok = panel.parse_findings(
            "FINDINGS_JSON\n[{'claim': broken}]\nEND_FINDINGS_JSON")
        self.assertFalse(ok)

    def test_survives_code_fence_and_trailing_prose(self):
        text = ("FINDINGS_JSON\n```json\n"
                '[{"file": null, "line": null, "claim": "x y z"}]\n```\n'
                "END_FINDINGS_JSON\nthanks!")
        items, ok = panel.parse_findings(text)
        self.assertTrue(ok)
        self.assertEqual(items[0]["severity"], "unspecified")

    def test_nested_brackets_do_not_truncate(self):
        text = findings_block([{"file": "a.py", "line": 1,
                                "claim": "list[0] index is wrong"}])
        items, ok = panel.parse_findings(text)
        self.assertTrue(ok)
        self.assertIn("index", items[0]["claim"])

    def test_verdict_line_ignores_the_findings_block(self):
        self.assertEqual(
            panel._verdict_line(findings_block([])), "VERDICT: FINDINGS")


class ClusterTests(unittest.TestCase):
    def test_same_defect_from_three_lineages_is_one_row(self):
        f = lambda: [{"file": "panel.py", "line": 100,
                      "claim": "results are written only after the slow seat "
                               "finishes", "severity": "high"}]
        clusters = panel.cluster_findings(
            {"codex": f(), "kimi": f(), "gemini": f()})
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["lineages"]), 3)

    def test_claude_trio_counts_as_one_lineage(self):
        f = lambda: [{"file": "x.py", "line": 5, "claim": "unbounded retry "
                      "loop hammers the endpoint", "severity": "medium"}]
        clusters = panel.cluster_findings(
            {"claude-sonnet": f(), "claude-opus": f(), "claude-fable": f()})
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["lineages"], ["anthropic"])
        self.assertEqual(len(clusters[0]["seats"]), 3)

    def test_unrelated_claims_stay_separate(self):
        clusters = panel.cluster_findings({
            "codex": [{"file": "a.py", "line": 1,
                       "claim": "timeout never applied to the socket",
                       "severity": "high"}],
            "kimi": [{"file": "b.py", "line": 90,
                      "claim": "summary table sorts alphabetically instead of "
                               "by severity", "severity": "low"}],
        })
        self.assertEqual(len(clusters), 2)

    def test_highest_severity_and_lineage_count_win_the_sort(self):
        clusters = panel.cluster_findings({
            "codex": [{"file": "a.py", "line": 1, "claim": "lonely low thing",
                       "severity": "low"}],
            "kimi": [{"file": "b.py", "line": 2,
                      "claim": "shared critical defect here", "severity": "high"}],
            "gemini": [{"file": "b.py", "line": 2,
                        "claim": "shared critical defect here",
                        "severity": "high"}],
        })
        self.assertEqual(len(clusters[0]["lineages"]), 2)
        self.assertEqual(clusters[0]["severity"], "high")

    def test_same_claim_far_apart_in_one_file_stays_separate(self):
        clusters = panel.cluster_findings({
            "codex": [{"file": "a.py", "line": 10,
                       "claim": "missing timeout", "severity": "low"}],
            "kimi": [{"file": "a.py", "line": 400,
                      "claim": "missing timeout", "severity": "low"}],
        })
        self.assertEqual(len(clusters), 2)


class PresetTests(unittest.TestCase):
    class Args:
        skip = ""
        seats = ""
        include_local = False
        preset = None

    def test_preset_selects_its_seats_in_order(self):
        args = self.Args()
        args.preset = "trio"
        self.assertEqual(panel.plan_seats(args, panel.REGISTRY),
                         ["claude-fable", "codex", "gemini-carbon"])

    def test_preset_and_seats_are_mutually_exclusive(self):
        args = self.Args()
        args.preset = "trio"
        args.seats = "codex"
        with self.assertRaises(SystemExit):
            panel.plan_seats(args, panel.REGISTRY)

    def test_unknown_preset_is_an_error(self):
        args = self.Args()
        args.preset = "nope"
        with self.assertRaises(SystemExit):
            panel.plan_seats(args, panel.REGISTRY)

    def test_every_preset_names_only_registered_seats(self):
        # a named roster with a dead seat is how "full panel" silently
        # becomes not-full (kimi-code/k2.7, run 20260813_182034)
        for name, seats in panel.PRESETS.items():
            unknown = set(seats) - set(panel.REGISTRY)
            self.assertEqual(unknown, set(), f"preset '{name}'")

    def test_strong_preset_uses_astra_and_eight_distinct_hosted_lineages(self):
        seats = panel.PRESETS["strong"]
        self.assertIn("codex-astra", seats)
        self.assertIn("claude-fable", seats)
        self.assertIn("kimi", seats)
        self.assertIn("gemini-carbon", seats)
        self.assertIn("muse", seats)
        self.assertEqual(len(seats), 8)
        self.assertEqual(len({panel.REGISTRY[n]["lineage"] for n in seats}), 8)
        self.assertFalse(any(panel.REGISTRY[n].get("local") for n in seats))


class ClusterFixtureTests(unittest.TestCase):
    """Real findings from run 20260813_182034 -- a VIDEO judgement, where
    seats cite frame stills (t10.5.png, t11.4.png, the poster) and the mp4
    itself. Those paths are exhibits, not locations: two seats citing
    different stills of the same moment are agreeing, and two seats citing
    the same mp4 are not thereby talking about the same defect."""

    FIXTURE = (Path(__file__).resolve().parent / "fixtures"
               / "clustering_20260813.json")

    def clusters(self):
        raw = json.loads(self.FIXTURE.read_text())
        ordered = {n: raw[n] for n in panel.REGISTRY if n in raw}
        return panel.cluster_findings(ordered, panel.REGISTRY)

    def _cluster_containing(self, clusters, needle):
        for c in clusters:
            if any(needle in claim for claim in c["claims"]):
                return c
        self.fail(f"no cluster contains {needle!r}")

    def test_stale_closer_defect_is_one_row_across_frame_stills(self):
        # the run split this 6-lineage defect into two high rows because
        # t10.5.png and t11.4.png are different filenames
        clusters = self.clusters()
        rows = [c for c in clusters
                if any("still needs to post" in cl for cl in c["claims"])]
        self.assertEqual(len(rows), 1,
                         [c["rep"]["claim"][:60] for c in rows])
        self.assertGreaterEqual(len(rows[0]["lineages"]), 5)

    def test_monotonic_counters_and_both_busy_stay_two_findings(self):
        # the run merged these because both cited the same mp4, which put
        # them under the generous same-place similarity bar
        clusters = self.clusters()
        mono = self._cluster_containing(clusters, "Monotonic on-screen")
        busy = self._cluster_containing(clusters, "t4.5 send frame")
        self.assertIsNot(mono, busy,
                         "corroborating-counters and both-panes-busy are "
                         "different claims")


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._panel = dict(panel.PANEL)
        self.addCleanup(self._restore_panel)
        self._env = dict(os.environ)
        os.environ["PANEL_RUNS"] = str(self.tmp / "runs")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _restore_panel(self):
        panel.PANEL.clear()
        panel.PANEL.update(self._panel)

    def _prompt_for(self, files):
        seen = []
        panel.PANEL.clear()
        panel.PANEL["a"] = lambda p, r, t: (seen.append(p),
                                            (0, findings_block([])))[1]
        argv = ["--task", "judge the clip", "--seats", "a",
                "--out", str(self.tmp), "--repo", str(self.tmp)]
        for f in files:
            argv += ["--file", str(f)]
        panel.main(argv)
        return seen[0]

    def test_media_files_are_attached_as_references_not_bytes(self):
        png = self.tmp / "still.png"
        png.write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64)
        mp4 = self.tmp / "clip.mp4"
        mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256)
        notes = self.tmp / "notes.md"
        notes.write_text("the OCR brief text")
        prompt = self._prompt_for([png, mp4, notes])
        self.assertIn("the OCR brief text", prompt, "text still embeds")
        self.assertIn(str(png), prompt)
        self.assertIn(str(mp4), prompt)
        self.assertIn("image", prompt)
        self.assertIn("video", prompt)
        self.assertIn("72 bytes", prompt, "size is stated")
        self.assertNotIn("PNG\r", prompt, "no raw bytes in any seat prompt")
        self.assertNotIn("ftyp", prompt)

    def test_undecodable_file_without_known_extension_degrades(self):
        blob = self.tmp / "dump.bin"
        blob.write_bytes(b"\xff\xfe\x00\x01" * 10)
        prompt = self._prompt_for([blob])  # must not raise UnicodeDecodeError
        self.assertIn(str(blob), prompt)
        self.assertIn("40 bytes", prompt)


class RetryAfterTests(unittest.TestCase):
    """Run 20260813_182034: gemini died on a 429 carrying 'Please retry in
    23.98s' with 2400s of budget left. One honored sleep keeps the Google
    lineage in the panel."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_retry_hint_parsing(self):
        self.assertAlmostEqual(
            panel._retry_delay("Please retry in 23.987671527s."), 23.987671527)
        self.assertEqual(panel._retry_delay("Retry-After: 20"), 20.0)
        self.assertIsNone(panel._retry_delay("plain failure, no hint"))
        self.assertIsNone(panel._retry_delay(""))

    def _flaky_script(self, hint, succeed_after=1):
        state = self.tmp / "calls"
        script = self.tmp / "flaky.sh"
        script.write_text(
            "#!/bin/sh\n"
            f"echo x >> {state}\n"
            f"if [ $(wc -l < {state}) -gt {succeed_after} ]; then "
            "echo recovered; exit 0; fi\n"
            f"echo 'Quota exceeded. {hint}'; exit 1\n")
        script.chmod(0o755)
        return script, state

    def test_cmd_seat_honours_a_short_retry_after(self):
        script, state = self._flaky_script("Please retry in 0.1s.")
        seat = panel.make_seat({"cmd": {"argv": [str(script)]}})
        code, out = seat("p", "/r", 30)
        self.assertEqual(code, 0, out)
        self.assertIn("recovered", out)
        self.assertEqual(state.read_text().count("x"), 2)

    def test_no_retry_when_the_wait_exceeds_the_budget(self):
        script, state = self._flaky_script("Please retry in 3600s.")
        seat = panel.make_seat({"cmd": {"argv": [str(script)]}})
        code, out = seat("p", "/r", 5)
        self.assertNotEqual(code, 0)
        self.assertEqual(state.read_text().count("x"), 1,
                         "a wait longer than the budget must not be paid")

    def test_only_one_retry_is_ever_attempted(self):
        script, state = self._flaky_script("Please retry in 0.1s.",
                                           succeed_after=99)
        seat = panel.make_seat({"cmd": {"argv": [str(script)]}})
        code, _ = seat("p", "/r", 30)
        self.assertNotEqual(code, 0)
        self.assertEqual(state.read_text().count("x"), 2)


GEMINI_CHROME = (
    "Warning: True color (24-bit) support not detected. Using a terminal "
    "with true color enabled will result in a better visual experience.\n"
    "Error when talking to Gemini API TerminalQuotaError: You exceeded your "
    "current quota, please check your plan and billing details.\n"
    "    at classifyGoogleError (file:///x/chunk.js:306535:12)\n"
    "    at retryWithBackoff (file:///x/chunk.js:307295:31)\n")


class SeatWiringTests(unittest.TestCase):
    """The roster moved from Python functions into seats.json. These pin the
    rendered invocation to what the hand-written seats used to send, so a
    config edit can't quietly change which model answers or at what effort."""

    def render_argv(self, name, prompt="P", repo="/r", mode=panel.DIFF_MODE):
        entry = panel.REGISTRY[name]
        return [panel._subst(a, {"{prompt}": prompt, "{repo}": str(repo)})
                for a in panel.resolve_argv(entry["cmd"], mode)]

    def test_codex_runs_sol_at_xhigh_in_yolo_mode(self):
        # the model VERSION floats (seat-refresh bumps it); the sol line
        # (Zohar, 2026-09-29, when 6.1 Sol outranked astra), xhigh effort
        # (2026-10-28) and yolo mode (no sandbox, no approvals; 2026-09-22,
        # every GPT call) are pinned wiring
        argv = self.render_argv("codex")
        self.assertEqual(argv[:3], ["codex", "exec", "-m"])
        self.assertRegex(argv[3], r"^gpt-[\d.]+-sol$")
        self.assertRegex(argv[3], panel.REGISTRY["codex"]["track"]["match"])
        # the prompt rides stdin ("-"), never argv: a 550 KB prompt is over
        # Linux's 128 KiB per-argument cap (audit 2026-11-09, D4)
        self.assertEqual(argv[4:], [
            "--config", 'model_reasoning_effort="xhigh"',
            "--dangerously-bypass-approvals-and-sandbox", "-C", "/r", "-"])
        self.assertTrue(panel.REGISTRY["codex"]["cmd"]["stdin"])

    def test_extra_openai_seats_run_the_gpt6_family_at_xhigh(self):
        # Zohar, 2026-10-29: astra sits by default (AA v5.0: astra xhigh 55,
        # above sol xhigh 52); luna stays opt-in
        for seat, line, default in (("codex-astra", "astra", True),
                                    ("codex-luna", "luna", False)):
            entry = panel.REGISTRY[seat]
            self.assertEqual(entry["lineage"], "openai")
            self.assertEqual(entry["default"], default, seat)
            argv = self.render_argv(seat)
            self.assertRegex(argv[3], rf"^gpt-6(\.\d+)?-{line}$")
            self.assertIn('model_reasoning_effort="xhigh"', argv)
            self.assertEqual(argv[-4:], ["--dangerously-bypass-approvals-and-sandbox",
                                         "-C", "/r", "-"])
            self.assertTrue(entry["cmd"]["stdin"], seat)

    def test_gemini_cli_fallback_never_pins_a_model_id(self):
        self.assertNotIn("-m", self.render_argv("gemini"))
        self.assertFalse(panel.REGISTRY["gemini"]["default"])

    def test_antigravity_google_seat_pins_flash_at_high(self):
        argv = self.render_argv("gemini-agy")
        self.assertEqual(argv[:2], ["agy", "--model"])
        self.assertRegex(argv[2], r"^gemini-[\d.]+-flash-high$")
        self.assertEqual(argv[3:], ["--effort", "high", "--print", "P"])
        self.assertEqual(panel.REGISTRY["gemini-agy"]["lineage"], "google")
        self.assertFalse(panel.REGISTRY["gemini-agy"]["default"])

    def test_carbon_is_the_default_google_voice_read_only_at_high(self):
        # #high is the top variant the direct Google route serves (#xhigh is
        # "Variant unavailable", 2026-11-10); the plan agent cannot edit the
        # caller's cwd, which a judge seat must never do
        # (the agent is a per-mode tail: OpenCode honors the FIRST --agent,
        # so the build pilot's `--agent build` could not follow a base `plan`)
        argv = self.render_argv("gemini-carbon")
        self.assertEqual(argv[:4], ["opencode", "run", "--standalone",
                                    "--model"])
        self.assertRegex(argv[4], r"^google/gemini-[\d.]+-carbon#high$")
        self.assertEqual(argv[5:], ["--agent", "plan"])  # prompt on stdin
        self.assertEqual(self.render_argv("gemini-carbon", mode="no such mode"),
                         argv)  # an unknown mode stays read-only
        self.assertTrue(panel.REGISTRY["gemini-carbon"]["cmd"]["stdin"])
        self.assertEqual(panel.REGISTRY["gemini-carbon"]["lineage"], "google")
        for preset in ("trio", "providers", "strong", "quick", "full"):
            self.assertIn("gemini-carbon", panel.PRESETS[preset], preset)
        defaults = [n for n, e in panel.REGISTRY.items()
                    if e.get("default") is True and e["lineage"] == "google"]
        self.assertEqual(defaults, ["gemini-carbon"])

    def test_argon_stays_wired_as_the_fallback_google_seat(self):
        # same route and read-only agent as carbon, off by default
        argon = self.render_argv("gemini-argon")
        carbon = self.render_argv("gemini-carbon")
        self.assertEqual(argon[:4] + argon[5:], carbon[:4] + carbon[5:])
        self.assertRegex(argon[4], r"^google/gemini-[\d.]+-argon#high$")
        self.assertEqual(panel.REGISTRY["gemini-argon"]["cmd"]["env"],
                         panel.REGISTRY["gemini-carbon"]["cmd"]["env"])
        self.assertEqual(self.render_argv("gemini-argon", mode="no such mode"),
                         argon)  # an unknown mode stays read-only
        self.assertFalse(panel.REGISTRY["gemini-argon"]["default"])
        self.assertIn("gemini-argon", panel.PRESETS["full"])
        # one Google voice per provider-style roster, never both
        for preset in ("trio", "providers", "strong", "quick"):
            google = [s for s in panel.PRESETS[preset]
                      if panel.REGISTRY[s]["lineage"] == "google"]
            self.assertEqual(len(google), 1, preset)

    def test_every_opencode_review_seat_runs_the_read_only_agent(self):
        # `opencode run` without --agent uses the write-capable build agent:
        # glm-flash-free created a file in the launch directory when a prompt
        # asked for one (2026-11-10). A cmd seat runs where panel was started,
        # so every review mode must name the plan agent. And the plan agent
        # only refuses the edit tool: with an allow-all OpenCode config it
        # ran `touch` for the same prompt, so the seat's environment also
        # strips it to read tools, and --standalone is what makes OpenCode
        # read that environment instead of its background service's
        seats = [n for n, e in panel.REGISTRY.items()
                 if e.get("cmd", {}).get("argv", [None])[0] == "opencode"]
        self.assertGreaterEqual(len(seats), 5)
        for name in seats:
            for mode in (panel.DIFF_MODE, panel.TASK_MODE):
                argv = self.render_argv(name, mode=mode)
                agents = [value for flag, value in zip(argv, argv[1:])
                          if flag == "--agent"]
                self.assertEqual(agents, ["plan"], (name, mode))
                self.assertIn("--standalone", argv, (name, mode))
            config = json.loads(
                panel.REGISTRY[name]["cmd"]["env"]["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(config["agent"]["plan"]["permission"],
                             {"*": "deny", "read": "allow", "glob": "allow",
                              "grep": "allow"}, name)

    def test_muse_uses_headless_cli_at_xhigh_not_the_billing_blocked_api(self):
        argv = self.render_argv("muse")
        self.assertEqual(argv[:3], ["muse", "exec", "--model"])
        self.assertRegex(argv[3], r"^muse-wonder-[\d.]+$")
        self.assertEqual(argv[4:], ["--reasoning-effort", "xhigh", "P"])
        self.assertEqual(panel.REGISTRY["muse"]["lineage"], "meta")
        self.assertNotIn("http", panel.REGISTRY["muse"])

    def test_claude_seats_run_at_xhigh_effort(self):
        for seat, family in (("claude-sonnet", "claude-sonnet-"),
                             ("claude-opus", "claude-opus-"),
                             ("claude-fable", "claude-fable-")):
            argv = self.render_argv(seat)
            model = argv[argv.index("--model") + 1]
            self.assertTrue(model.startswith(family), (seat, model))
            self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_kimi_code_runs_the_managed_versionless_alias(self):
        # kimi-code/k2.7 was a catalog-derived id the CLI never served: the
        # seat died on startup in run 20260813_182034 and "full" wasn't full.
        # The managed alias is versionless (server-side updated), so there is
        # no version literal to rot and no track block to bump it.
        argv = self.render_argv("kimi-code")
        self.assertEqual(argv[argv.index("-m") + 1],
                         "kimi-code/kimi-for-coding")
        self.assertNotIn("track", panel.REGISTRY["kimi-code"])

    def test_mimo_flash_free_is_opt_in_on_the_free_preset(self):
        entry = panel.REGISTRY["mimo-flash-free"]
        self.assertEqual(entry["lineage"], "xiaomi")
        self.assertFalse(entry["default"])
        argv = self.render_argv("mimo-flash-free")
        self.assertRegex(argv[argv.index("-m") + 1],
                         r"^opencode/mimo-v[\d.]+-flash-free$")
        self.assertIn("mimo-flash-free", panel.PRESETS["free"])

    def test_new_free_seats_preserve_provider_lineages_and_are_opt_in(self):
        for name, lineage, family in (
                ("glm-flash-free", "zhipu", r"glm-[\d.]+"),
                ("deepseek-flash-free", "deepseek", r"deepseek-v[\d.]+")):
            entry = panel.REGISTRY[name]
            argv = self.render_argv(name)
            self.assertFalse(entry["default"])
            self.assertEqual(entry["lineage"], lineage)
            self.assertEqual(entry["grade"], "?")
            self.assertEqual(argv[:3], ["opencode", "run", "-m"])
            # #high is the top variant both free routes serve (2026-10-28)
            self.assertRegex(argv[3], rf"^opencode/{family}-flash-free#high$")
            self.assertIn(name, panel.PRESETS["free"])

    def test_grok_seats_are_the_same_model_two_scaffolds(self):
        # whichever grok version is pinned, both scaffolds must run it, xhigh
        argv = self.render_argv("grok")
        model = argv[argv.index("-m") + 1]
        ver = re.fullmatch(r"grok-(\d+\.\d+)", model).group(1)
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        # Cursor names 4.7+ grok-X-<effort>[-fast] (4.6 and older: cursor-grok-*)
        cursor = self.render_argv("cursor-grok")
        self.assertEqual(cursor[cursor.index("--model") + 1],
                         f"grok-{ver}-xhigh")

    def test_no_seat_runs_max_effort(self):
        # Zohar, 2026-10-28: xhigh wherever a route offers it, never max;
        # the top level only where that is below xhigh
        for name, entry in panel.REGISTRY.items():
            if "cmd" in entry:
                argv = entry["cmd"]["argv"]
                for flag in ("--effort", "--reasoning-effort"):
                    if flag in argv:
                        self.assertNotIn(argv[argv.index(flag) + 1],
                                         ("max", "ultra"), name)
                self.assertFalse(any(a.endswith(("#max", "#ultra")) or
                                     'effort="max"' in a or
                                     'effort="ultra"' in a for a in argv), name)
                for value in (entry["cmd"].get("env") or {}).values():
                    self.assertNotIn(value, ("max", "ultra"), name)
            specs = entry.get("http")
            for spec in specs if isinstance(specs, list) else [specs] if specs else []:
                self.assertNotIn(spec["body"].get("reasoning_effort"),
                                 ("max", "ultra"), name)

    def test_kimi_seats_pin_high_effort_through_the_env(self):
        # the Kimi CLI has no effort flag, but KIMI_MODEL_THINKING_EFFORT
        # forces one (a bad value 400s at the API, 2026-10-29). K3.1 Pro and
        # kimi-for-coding offer low/high/max, and kimi-for-coding defaults
        # to max, so the seat pins high instead of trusting config.toml
        for name in ("kimi", "kimi-code"):
            env = panel.REGISTRY[name]["cmd"].get("env") or {}
            self.assertEqual(env.get("KIMI_MODEL_THINKING_EFFORT"), "high", name)

    def test_cmd_seat_runs_its_cli_with_the_spec_env(self):
        entry = {"lineage": "x", "cmd": {
            "argv": [sys.executable, "-c",
                     "import os; print(os.environ['PANEL_T_EFFORT'], "
                     "bool(os.environ.get('PATH')))"],
            "env": {"PANEL_T_EFFORT": "high"}}}
        rc, out = panel.make_seat(entry)("p", Path.cwd(), 60)
        self.assertEqual((rc, out), (0, "high True"))

    def test_model_studio_pins_follow_their_tracks(self):
        # a pin its own track cannot match is invisible to seat-refresh
        for name in ("glm", "deepseek", "qwen-max"):
            entry = panel.REGISTRY[name]
            self.assertRegex(entry["http"]["body"]["model"],
                             entry["track"]["match"], name)

    def test_deepseek_runs_the_snapshot_the_index_scored_highest(self):
        # AA v5.0 (2026-10-29): V4.1 Flash 1017 (max) 32, V4 Pro 0813 (max) 23.
        # Model Studio names the snapshot; the Token-Plan catalog is the only
        # models.dev listing that carries those names. It is read for ids
        # only: the seat still calls the Standard key (rule 7)
        entry = panel.REGISTRY["deepseek"]
        self.assertEqual(entry["track"]["provider"], "alibaba-token-plan")
        self.assertEqual(entry["http"]["url"], panel.REGISTRY["glm"]["http"]["url"])
        self.assertRegex(entry["http"]["body"]["model"],
                         r"^deepseek-v[\d.]+-(pro|flash)(-\d{4})?$")

    def test_model_studio_bodies_think_at_the_top_level(self):
        # extra_body is an OpenAI-SDK wrapper, not an API field: in a raw
        # request DashScope ignored it (2026-10-28)
        for name in ("glm", "deepseek", "qwen-max"):
            body = panel.REGISTRY[name]["http"]["body"]
            self.assertNotIn("extra_body", body, name)
            self.assertIs(body["enable_thinking"], True, name)
        self.assertEqual(panel.REGISTRY["glm"]["http"]["body"]["reasoning_effort"],
                         "high")
        for name in ("deepseek", "qwen-max"):
            self.assertEqual(
                panel.REGISTRY[name]["http"]["body"]["reasoning_effort"],
                "xhigh", name)

    def test_every_seat_declares_a_transport(self):
        for name, entry in panel.REGISTRY.items():
            self.assertTrue("cmd" in entry or "http" in entry,
                            f"{name} has no cmd/http spec")

    def test_prompt_is_substituted_whole_never_stringified(self):
        prompt = 'multi\nline "quoted" {braces}'
        self.assertIn(prompt, self.render_argv("kimi", prompt=prompt))

    def test_http_seat_bodies_carry_the_prompt(self):
        for name in ("glm", "deepseek", "qwen-max", "pplx", "local-27b",
                     "mimo-ultraspeed"):
            spec = panel.REGISTRY[name]["http"]
            spec = spec[0] if isinstance(spec, list) else spec
            rendered = panel._subst(spec["body"], {"{prompt}": "THE PROMPT"})
            self.assertIn("THE PROMPT", json.dumps(rendered), name)

    def test_mimo_uses_opencode_xiaomi_key_and_remains_opt_in(self):
        from unittest.mock import patch
        from io import BytesIO
        entry = panel.REGISTRY["mimo-ultraspeed"]
        self.assertEqual(entry["lineage"], "xiaomi")
        self.assertFalse(entry["default"])
        self.assertEqual(entry["grade"], "?")
        spec = entry["http"]
        self.assertEqual(spec["key_from"], {
            "file": "~/.local/share/opencode/auth.json", "json_path": "xiaomi.key"})
        self.assertRegex(spec["body"]["model"], r"^mimo-v[\d.]+-pro-ultraspeed$")
        self.assertEqual(spec["body"]["thinking"], {"type": "enabled"})
        self.assertEqual(spec["extract"], ["choices[0].message.content"])
        with patch.object(panel, "load_key", return_value=(None, "missing Xiaomi key")), \
                patch.object(panel.urllib.request, "urlopen") as request:
            rc, detail = panel.http_seat([spec], "public prompt", "/unused", 5)
            self.assertNotEqual(rc, 0)
            request.assert_not_called()
        answer = {"choices": [{"message": {"content": "answer"}}]}
        with patch.object(panel, "load_key", return_value=("test-secret", None)), \
                patch.object(panel.urllib.request, "urlopen",
                             return_value=BytesIO(json.dumps(answer).encode())) as request:
            self.assertEqual(panel.http_seat([spec], "public prompt", "/unused", 5),
                             (0, "answer"))
            req = request.call_args.args[0]
            self.assertEqual(req.get_header("Api-key"), "test-secret")
            self.assertEqual(req.full_url, "https://api.xiaomimimo.com/v1/chat/completions")
            self.assertEqual(json.loads(req.data)["messages"][0]["content"], "public prompt")

    def test_http_error_carries_the_provider_reason_without_the_key(self):
        # pplx died for days as a bare "HTTP Error 401: Unauthorized" when the
        # body said the account was out of credit (2026-09-29)
        from unittest.mock import patch
        from io import BytesIO
        body = b'{"error":{"message":"You exceeded your current quota (key test-secret)","type":"insufficient_quota"}}'
        err = panel.urllib.error.HTTPError("https://x", 401, "Unauthorized", {},
                                           BytesIO(body))
        spec = {"url": "https://x", "key_from": {"env": "UNUSED"},
                "body": {"q": "{prompt}"}, "extract": ["a"]}
        with patch.object(panel, "load_key", return_value=("test-secret", None)), \
                patch.object(panel.urllib.request, "urlopen", side_effect=err):
            rc, detail = panel.http_seat([spec], "p", "/unused", 5)
        self.assertEqual(rc, -3)
        self.assertIn("401", detail)
        self.assertIn("insufficient_quota", detail)
        self.assertNotIn("test-secret", detail)

    def test_qwen_max_rides_the_standard_key_not_the_token_plan(self):
        # rule 7: the hosted Alibaba voice must use the same plain Model
        # Studio key as glm/deepseek, never a Token-Plan lane
        entry = panel.REGISTRY["qwen-max"]
        self.assertEqual(entry["lineage"], "alibaba")
        self.assertTrue(entry["default"])
        spec = entry["http"]
        self.assertEqual(spec["key_from"], panel.REGISTRY["glm"]["http"]["key_from"])
        self.assertEqual(spec["url"], panel.REGISTRY["glm"]["http"]["url"])
        # a dated snapshot is allowed: AA scores qwen3.8-max-0902, not the alias
        self.assertRegex(spec["body"]["model"], r"^qwen[\d.]+-max(-\d{4})?$")
        self.assertRegex(spec["body"]["model"], entry["track"]["match"])
        self.assertIn("qwen-max", panel.PRESETS["providers"])

    OC_TWINS = (("glm-oc", "glm"), ("deepseek-oc", "deepseek"),
                ("qwen-max-oc", "qwen-max"))

    def test_oc_seats_twin_the_model_studio_seats_read_only_and_opt_in(self):
        # the same model family on the same Standard key as the HTTP twin
        # (rule 7: never the Token Plan), through OpenCode's read-only agent.
        # --standalone: the key travels in the environment, which OpenCode's
        # shared background service does not take from its caller
        for name, twin in self.OC_TWINS:
            entry, http = panel.REGISTRY[name], panel.REGISTRY[twin]
            self.assertEqual(entry["lineage"], http["lineage"], name)
            self.assertFalse(entry["default"], name)
            for preset, seats in panel.PRESETS.items():
                self.assertNotIn(name, seats, preset)
            argv = self.render_argv(name)
            self.assertEqual(argv[:6], ["opencode", "run", "--standalone",
                                        "--agent", "plan", "--model"])
            self.assertRegex(argv[6], r"^panel-model-studio/" +
                             http["track"]["match"].lstrip("^"))
            self.assertEqual(argv[7:], [])  # prompt on stdin
            self.assertTrue(entry["cmd"]["stdin"], name)
            self.assertEqual(list(entry["cmd"]["env_from"].values()),
                             [http["http"]["key_from"]], name)
            self.assertEqual(entry["track"]["format"],
                             "panel-model-studio/{id}")

    def test_oc_provider_config_is_the_standard_endpoint_without_max(self):
        # the -oc seats' endpoint, model list and effort live in OpenCode's
        # own config, outside this repo: check the file OpenCode loads
        config = Path.home() / ".config/opencode/opencode.json"
        try:
            provider = json.loads(config.read_text())["providers"][
                "panel-model-studio"]
        except (OSError, ValueError, KeyError):
            self.skipTest(f"no panel-model-studio provider in {config}")
        for name, twin in self.OC_TWINS:
            entry, http = panel.REGISTRY[name], panel.REGISTRY[twin]["http"]
            self.assertEqual(
                provider["settings"]["baseURL"] + "/chat/completions",
                http["url"], name)
            (var,) = entry["cmd"]["env_from"]
            self.assertEqual(provider["settings"]["apiKey"], f"{{env:{var}}}")
            model = panel.pinned_model(entry).split("/", 1)[1]
            self.assertIn(model, provider["models"], name)
            effort = provider["models"][model]["body"]["reasoning_effort"]
            self.assertNotIn(effort, ("max", "ultra"), name)
            self.assertEqual(effort, http["body"]["reasoning_effort"], name)

    def test_cmd_seat_takes_a_credential_from_env_from_and_scrubs_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            keyfile = Path(tmp) / "env"
            keyfile.write_text("export T_KEY='test-secret-value'\n")
            entry = {"lineage": "x", "cmd": {
                "argv": [sys.executable, "-c",
                         "import os, sys; print(sys.argv[1:], "
                         "os.environ['PANEL_T_KEY'])", "{prompt}"],
                "env_from": {"PANEL_T_KEY": {"file": str(keyfile),
                                             "env_key": "T_KEY"}}}}
            rc, out = panel.make_seat(entry)("p", Path.cwd(), 60)
            self.assertEqual((rc, out), (0, "['p'] [key]"))
            entry["cmd"]["env_from"]["PANEL_T_KEY"]["env_key"] = "ABSENT"
            rc, out = panel.make_seat(entry)("p", Path.cwd(), 60)
            self.assertEqual(rc, -3)
            self.assertIn("ABSENT not in", out)

    def test_thinking_seats_fall_back_to_reasoning_content(self):
        for name in ("glm", "deepseek", "qwen-max"):
            self.assertEqual(panel.REGISTRY[name]["http"]["extract"][-1],
                             "choices[0].message.reasoning_content")

    def test_no_credential_is_ever_inlined_in_the_registry(self):
        blob = json.dumps(panel.REGISTRY)
        # key-shaped literals, not the letters "sk-" inside a word
        self.assertIsNone(re.search(r"(sk|pplx|gsk)-[A-Za-z0-9_]{16,}", blob))
        self.assertIsNone(re.search(r"Bearer\s+(?!\{key\})\S", blob))
        for name, entry in panel.REGISTRY.items():
            spec = entry.get("http")
            for s in ([spec] if isinstance(spec, dict) else (spec or [])):
                auth = s.get("headers", {}).get("Authorization", "")
                if auth:
                    self.assertIn("{key}", auth, name)

    def test_grok_empty_tools_is_not_a_noop(self):
        """b13b84f: empty `--tools` is a grok CLI no-op (default toolset stays).

        Passing `--tools` followed by an empty string does not disable tools.
        The seat must omit that no-op and deny the real tool IDs.
        """
        grok = self.render_argv("grok")
        self.assertNotIn("--tools", grok)
        self.assertIn("--disallowed-tools", grok)
        denied = grok[grok.index("--disallowed-tools") + 1]
        self.assertIn("run_terminal_command", denied)
        self.assertIn("run_terminal_cmd", denied)
        self.assertIn("read_file", denied)
        self.assertIn("grep", denied)
        self.assertIn("list_dir", denied)
        self.assertIn("search_replace", denied)
        self.assertIn("web_search", denied)
        self.assertIn("web_fetch", denied)
        self.assertIn("Agent", denied)

    def test_grok_task_mode_preserves_attachment_access(self):
        """The b13b84f denylist locks grok down for a diff review, but a --task
        run may attach a file by reference (path only). Codex P2: the lockdown
        must be scoped to the review, not applied to every task, or grok can
        neither open nor decline the attachment safely.
        """
        review = self.render_argv("grok", mode=panel.DIFF_MODE)
        task = self.render_argv("grok", mode=panel.TASK_MODE)

        # the review still bans file reads and hard-denies Read (regression pin)
        review_denied = review[review.index("--disallowed-tools") + 1]
        self.assertIn("read_file", review_denied)
        self.assertIn("grep", review_denied)
        self.assertIn("list_dir", review_denied)
        self.assertIn("Read", review)

        # the task run keeps grok from executing/searching but leaves file
        # reads available so an attached path can be opened
        task_denied = task[task.index("--disallowed-tools") + 1]
        self.assertNotIn("read_file", task_denied)
        self.assertNotIn("grep", task_denied)
        self.assertNotIn("list_dir", task_denied)
        self.assertNotIn("Read", task, "task mode must not hard-deny Read")
        # the dangerous tools stay denied in both modes
        for tool in ("run_terminal_command", "web_search", "web_fetch",
                     "Agent"):
            self.assertIn(tool, task_denied, tool)
        # and the rule invites opening an attached file rather than forbidding it
        task_rule = task[task.index("--rules") + 1]
        self.assertNotIn("Do not inspect the working tree", task_rule)
        self.assertIn("open a file", task_rule)

    def test_grok_rule_defers_final_line_to_the_prompt(self):
        """Codex P2: the CLI rule hard-coded 'Emit VERDICT', but a --task run's
        TASK_TEMPLATE asks for 'BOTTOM LINE:' and _verdict_line parses that. The
        rule must defer to whatever final line the supplied prompt requests, in
        both modes, so a task run still ends with its BOTTOM LINE.
        """
        for mode in (panel.DIFF_MODE, panel.TASK_MODE):
            argv = self.render_argv("grok", mode=mode)
            rule = argv[argv.index("--rules") + 1]
            self.assertNotIn("VERDICT", rule, mode)
            self.assertIn("the prompt asks for", rule, mode)
        # the two templates the rule defers to still carry the right final line
        self.assertIn("VERDICT: CLEAN", panel.PROMPT_TEMPLATE)
        self.assertIn("BOTTOM LINE:", panel.TASK_TEMPLATE)
        self.assertEqual(
            panel._verdict_line("prose\nBOTTOM LINE: ship it\n"),
            "BOTTOM LINE: ship it")

    def test_seat_invocation_follows_the_active_run_mode(self):
        """The seat closure -- not just resolve_argv -- must read the mode the
        run set, so execute()'s set_active_mode actually reaches grok's argv."""
        seat = panel.make_seat(panel.REGISTRY["grok"])
        seen = []
        orig_run, orig_mode = panel.run_cmd, panel._active_mode

        def _capture(cmd, timeout, stdin_text=None, env=None):
            seen.append(list(cmd))
            return 0, "ok"

        panel.run_cmd = _capture
        try:
            panel.set_active_mode(panel.TASK_MODE)
            seat("P", "/r", 5)
            panel.set_active_mode(panel.DIFF_MODE)
            seat("P", "/r", 5)
        finally:
            panel.run_cmd = orig_run
            panel._active_mode = orig_mode

        task_argv, review_argv = seen
        self.assertNotIn("read_file",
                         task_argv[task_argv.index("--disallowed-tools") + 1])
        self.assertIn("read_file",
                      review_argv[review_argv.index("--disallowed-tools") + 1])


class IntelligenceTests(unittest.TestCase):
    """Each seat carries its Artificial Analysis Intelligence Index score so
    whoever synthesizes can weigh a judgment call by how capable the model is
    while still hearing every family (Zohar, 2026-10-29)."""

    def entry(self, pin, **info):
        entry = {"cmd": {"argv": ["x", "-m", pin, "{prompt}"]}}
        if info:
            entry["intelligence"] = info
        return entry

    def test_source_names_the_index_and_the_date(self):
        self.assertIn("Artificial Analysis", panel.INTELLIGENCE["index"])
        self.assertRegex(panel.INTELLIGENCE["as_of"], r"^\d{4}-\d{2}-\d{2}$")

    def test_every_default_seat_states_its_score_or_says_why_not(self):
        for name, entry in panel.REGISTRY.items():
            if not entry.get("default"):
                continue
            info = entry.get("intelligence")
            self.assertIsNotNone(info, name)
            if info.get("score") is None:
                self.assertTrue(info.get("note"), name)
            else:
                # the score belongs to the model the seat pins right now
                self.assertEqual(info["model"], panel.pinned_model(entry), name)
                self.assertTrue(info.get("measured"), name)

    def test_pinned_model_reads_the_argv_flag_or_the_first_body_model(self):
        self.assertEqual(panel.pinned_model(
            {"cmd": {"argv": ["x", "--model", "m1", "{prompt}"]}}), "m1")
        self.assertEqual(panel.pinned_model(
            {"http": [{"body": {}}, {"body": {"model": "m2"}}]}), "m2")
        self.assertIsNone(panel.pinned_model({"cmd": {"argv": ["x", "-p"]}}))

    def test_a_score_measured_on_another_model_reads_as_stale(self):
        # seat-refresh bumps pins on its own; the old number must not ride
        # along onto the new model
        reg = {"s": self.entry("new", score=50, model="old", measured="Old")}
        self.assertIsNone(panel.intelligence_of("s", reg))
        self.assertEqual(panel.intelligence_label("s", reg), "stale")

    def test_labels(self):
        reg = {"a": self.entry("m", score=57, model="m", measured="M"),
               "b": self.entry("m", score=43, model="m", measured="M (max)",
                               ceiling=True),
               "c": self.entry("m", score=None, note="not on the leaderboard"),
               "d": self.entry("m")}
        self.assertEqual([panel.intelligence_label(n, reg) for n in "abcd"],
                         ["57", "≤43", "-", "-"])
        self.assertEqual(panel.intelligence_of("b", reg), 43)
        self.assertIsNone(panel.intelligence_of("c", reg))

    def test_list_seats_shows_the_score(self):
        import contextlib
        import io
        reg = {"zz-test-seat": dict(self.entry("true", score=50, model="true",
                                               measured="T"), lineage="t")}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            panel.list_seats(reg)
        header, row = buf.getvalue().splitlines()[:2]
        self.assertIn("aa", header)
        self.assertRegex(row, r"\b50\b")


class SubstTests(unittest.TestCase):
    def test_whole_value_substitution_preserves_type(self):
        out = panel._subst({"n": 42, "p": "{prompt}"}, {"{prompt}": "hi"})
        self.assertEqual(out, {"n": 42, "p": "hi"})

    def test_embedded_substitution(self):
        self.assertEqual(panel._subst("Bearer {key}", {"{key}": "abc"}),
                         "Bearer abc")

    def test_nested_structures(self):
        out = panel._subst({"a": [{"b": "{prompt}"}]}, {"{prompt}": "x"})
        self.assertEqual(out["a"][0]["b"], "x")


class ExtractTests(unittest.TestCase):
    def test_indexed_path(self):
        data = {"choices": [{"message": {"content": "hello"}}]}
        self.assertEqual(panel.extract(data, "choices[0].message.content"),
                         "hello")

    def test_wildcard_finds_first_match_in_lists(self):
        data = {"output": [{"content": [{"type": "x"},
                                        {"type": "output_text",
                                         "text": "found"}]}]}
        self.assertEqual(panel.extract(data, "output[].content[].text"),
                         "found")

    def test_missing_path_is_none_not_an_error(self):
        self.assertIsNone(panel.extract({"a": 1}, "b.c[0].d"))

    def test_non_string_leaf_is_none(self):
        self.assertIsNone(panel.extract({"a": {"b": 5}}, "a.b"))


class KeyLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_dotenv_with_export_and_quotes(self):
        f = self.tmp / "env"
        f.write_text('export PPLX_API_KEY="secret-value"\nOTHER=1\n')
        key, err = panel.load_key({"file": str(f), "env_key": "PPLX_API_KEY"})
        self.assertEqual(key, "secret-value")
        self.assertIsNone(err)

    def test_json_path(self):
        f = self.tmp / "settings.json"
        f.write_text(json.dumps({"env": {"DASHSCOPE_API_KEY": "k123"}}))
        key, _ = panel.load_key({"file": str(f),
                                 "json_path": "env.DASHSCOPE_API_KEY"})
        self.assertEqual(key, "k123")

    def test_missing_file_explains_itself(self):
        key, err = panel.load_key({"file": str(self.tmp / "nope"),
                                   "env_key": "X"})
        self.assertIsNone(key)
        self.assertIn("no credentials file", err)

    def test_env_var_source(self):
        os.environ["PANEL_TEST_KEY"] = "from-env"
        self.addCleanup(os.environ.pop, "PANEL_TEST_KEY", None)
        self.assertEqual(panel.load_key({"env": "PANEL_TEST_KEY"})[0],
                         "from-env")


class HttpSeatTests(unittest.TestCase):
    """No sockets: urlopen is replaced. Proves fallback order, key gating and
    the degrade-never-raise contract."""

    def fake_urlopen(self, payloads):
        calls = []

        class Resp:
            def __init__(self, body):
                self._body = json.dumps(body).encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            calls.append((req.full_url, json.loads(req.data)))
            item = payloads[min(len(calls) - 1, len(payloads) - 1)]
            if isinstance(item, Exception):
                raise item
            return Resp(item)
        return opener, calls

    def patch(self, opener):
        real = panel.urllib.request.urlopen
        panel.urllib.request.urlopen = opener
        self.addCleanup(lambda: setattr(panel.urllib.request, "urlopen", real))

    def test_extracts_by_first_matching_path(self):
        opener, calls = self.fake_urlopen([{"response": "the answer"}])
        self.patch(opener)
        code, out = panel.http_seat(
            [{"url": "http://x/api", "body": {"prompt": "{prompt}"},
              "extract": ["response"]}], "PROMPT", "/r", 5)
        self.assertEqual((code, out), (0, "the answer"))
        self.assertEqual(calls[0][1], {"prompt": "PROMPT"})

    def test_falls_back_to_the_next_path_and_says_so(self):
        opener, _ = self.fake_urlopen(
            [{"choices": [{"message": {"content": "",
                                       "reasoning_content": "thinking"}}]}])
        self.patch(opener)
        code, out = panel.http_seat(
            [{"url": "http://x", "body": {},
              "extract": ["choices[0].message.content",
                          "choices[0].message.reasoning_content"]}],
            "P", "/r", 5)
        self.assertEqual(code, 0)
        self.assertIn("fell back", out)
        self.assertIn("thinking", out)

    def test_second_endpoint_takes_over_when_the_first_fails(self):
        opener, calls = self.fake_urlopen(
            [RuntimeError("410 gone"), {"choices": [{"message":
                                                     {"content": "legacy"}}]}])
        self.patch(opener)
        code, out = panel.http_seat([
            {"url": "http://new", "body": {}, "extract": ["output[].text"]},
            {"url": "http://legacy", "body": {},
             "extract": ["choices[0].message.content"]}], "P", "/r", 5)
        self.assertEqual((code, out), (0, "legacy"))
        self.assertEqual(len(calls), 2)

    def test_a_seat_without_its_key_degrades_instead_of_raising(self):
        code, out = panel.http_seat(
            [{"url": "http://x", "body": {},
              "key_from": {"file": "/nonexistent/env", "env_key": "K"},
              "extract": ["response"]}], "P", "/r", 5)
        self.assertEqual(code, -3)
        self.assertIn("no credentials file", out)

    STREAM_SPEC = {"url": "http://x", "body": {"stream": True},
                   "extract": ["choices[0].message.content",
                               "choices[0].message.reasoning_content"]}

    def sse_resp(self, events):
        lines = [f"data: {json.dumps(e)}\n".encode() for e in events]
        lines += [b"\n", b"data: [DONE]\n"]

        class Resp:
            def __iter__(self):
                return iter(lines)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Resp()

    def scripted_opener(self, items):
        calls = []

        def opener(req, timeout=None):
            calls.append(timeout)
            item = items[min(len(calls) - 1, len(items) - 1)]
            if isinstance(item, Exception):
                raise item
            return self.sse_resp(item)
        self.patch(opener)
        return calls

    def test_a_streaming_spec_folds_events_into_the_answer(self):
        calls = self.scripted_opener([[
            {"choices": [{"delta": {"reasoning_content": "17*19"}}]},
            {"choices": [{"delta": {"content": "32"}}]},
            {"choices": [{"delta": {"content": "3"}}]},
            {"choices": [], "usage": {"completion_tokens": 3}}]])
        code, out = panel.http_seat([self.STREAM_SPEC], "P", "/r", 2400)
        self.assertEqual((code, out), (0, "323"))
        # read with the idle timeout, not the whole 40-minute budget
        self.assertEqual(calls, [panel.STREAM_IDLE_TIMEOUT])

    def test_a_stalled_stream_is_retried(self):
        # run 20261028_114718: the request was accepted and never
        # answered; a fresh one answered in seconds
        stall = panel.urllib.error.URLError(TimeoutError("timed out"))
        calls = self.scripted_opener([stall, [
            {"choices": [{"delta": {"content": "323"}}]}]])
        code, out = panel.http_seat([self.STREAM_SPEC], "P", "/r", 2400)
        self.assertEqual((code, out), (0, "323"))
        self.assertEqual(len(calls), 2)

    def test_repeated_stalls_give_up_and_say_why(self):
        calls = self.scripted_opener([TimeoutError("timed out")])
        code, out = panel.http_seat([self.STREAM_SPEC], "P", "/r", 2400)
        self.assertEqual(code, -3)
        self.assertIn("stalled", out)
        self.assertEqual(len(calls), panel.STREAM_ATTEMPTS)

    def test_no_retry_when_the_budget_cannot_cover_another_wait(self):
        calls = self.scripted_opener([TimeoutError("timed out")])
        code, _ = panel.http_seat([self.STREAM_SPEC], "P", "/r", 100)
        self.assertEqual(code, -3)
        self.assertEqual(len(calls), 1)

    def test_a_stream_error_event_fails_the_seat(self):
        self.scripted_opener([[{"error": {"message": "Arrearage"}}]])
        code, out = panel.http_seat([self.STREAM_SPEC], "P", "/r", 2400)
        self.assertEqual(code, -3)
        self.assertIn("Arrearage", out)

    def test_non_streaming_specs_are_never_retried(self):
        calls = self.scripted_opener([TimeoutError("timed out")])
        spec = dict(self.STREAM_SPEC, body={})
        code, _ = panel.http_seat([spec], "P", "/r", 2400)
        self.assertEqual(code, -3)
        self.assertEqual(calls, [2400])

    def test_unconfigured_seat_is_reported_not_crashed(self):
        seat = panel.make_seat({"lineage": "x"})
        code, out = seat("P", "/r", 5)
        self.assertEqual(code, -6)
        self.assertIn("seats.json", out)


class TempRunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._panel = dict(panel.PANEL)
        self.addCleanup(self._restore_panel)
        # every panel.main run records seat health under $PANEL_RUNS; tests
        # must never write fake failures into the user's real health file
        self._env = dict(os.environ)
        os.environ["PANEL_RUNS"] = str(self.tmp / "runs")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _restore_panel(self):
        panel.PANEL.clear()
        panel.PANEL.update(self._panel)

    def use_seats(self, seats):
        panel.PANEL.clear()
        panel.PANEL.update(seats)


class PersistenceTests(TempRunCase):
    def test_finished_seat_is_written_before_a_slow_seat_returns(self):
        """The bug this replaced: in-order .result() held every finished
        seat's file hostage behind the slowest one, so killing a long run
        threw away answers already paid for."""
        gate = threading.Event()
        self.use_seats({
            "slow": lambda p, r, t: (gate.wait(10), (0, findings_block([])))[1],
            "fast": lambda p, r, t: (0, findings_block([])),
        })
        meta = {"ts": "t", "mode": "task", "question": "q", "planned":
                ["slow", "fast"], "round": 1}
        thread = threading.Thread(
            target=panel.execute,
            args=(self.tmp, meta, ["slow", "fast"], {"_default": "p"},
                  self.tmp, 5, {}))
        thread.start()
        try:
            fast = self.tmp / "fast.md"
            for _ in range(100):
                if fast.exists():
                    break
                threading.Event().wait(0.05)
            self.assertTrue(fast.exists(),
                            "fast seat must land on disk immediately")
            self.assertFalse((self.tmp / "slow.md").exists())
        finally:
            gate.set()
            thread.join(15)
        self.assertTrue((self.tmp / "slow.md").exists())

    def test_a_raising_seat_does_not_kill_the_run(self):
        def boom(p, r, t):
            raise RuntimeError("seat exploded")
        self.use_seats({"boom": boom,
                        "ok": lambda p, r, t: (0, findings_block([]))})
        meta = {"ts": "t", "mode": "task", "question": "q",
                "planned": ["boom", "ok"], "round": 1}
        panel.execute(self.tmp, meta, ["boom", "ok"], {"_default": "p"},
                      self.tmp, 5, {})
        self.assertIn("seat raised", (self.tmp / "boom.md").read_text())
        self.assertEqual(panel.seat_status(self.tmp, "ok"), (True, 0))
        self.assertEqual(panel.seat_status(self.tmp, "boom")[1], -5)

    def test_serial_seats_run_after_the_fan_out(self):
        order = []
        self.use_seats({
            "par": lambda p, r, t: (order.append("par"), (0, "x"))[1],
            "ser": lambda p, r, t: (order.append("ser"), (0, "x"))[1],
        })
        # repair off: these blockless answers would each get a follow-up call
        meta = {"ts": "t", "mode": "task", "question": "q",
                "planned": ["ser", "par"], "round": 1, "repair": False}
        panel.execute(self.tmp, meta, ["ser", "par"], {"_default": "p"},
                      self.tmp, 5, {"ser": {"serial": True}})
        self.assertEqual(order, ["par", "ser"])


class ResumeTests(TempRunCase):
    def test_resume_refills_only_the_failed_seat(self):
        calls = {"good": 0, "bad": 0}

        def good(p, r, t):
            calls["good"] += 1
            return 0, findings_block([{"file": "a.py", "line": 1,
                                       "claim": "real defect here",
                                       "severity": "high"}])

        def bad(p, r, t):
            calls["bad"] += 1
            if calls["bad"] == 1:
                return -2, "CLI not installed"
            return 0, findings_block([])

        self.use_seats({"good": good, "bad": bad})
        argv = ["--task", "q", "--seats", "good,bad", "--out", str(self.tmp),
                "--repo", str(self.tmp)]
        panel.main(argv)
        rundir = panel._latest_run(self.tmp)
        self.assertEqual(panel.seat_status(rundir, "bad")[1], -2)
        before = (rundir / "good.md").read_text()

        panel.main(["--resume", "last", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        self.assertEqual(calls, {"good": 1, "bad": 2},
                         "a seat that already landed must never be re-paid")
        self.assertEqual((rundir / "good.md").read_text(), before)
        self.assertEqual(panel.seat_status(rundir, "bad"), (True, 0))
        self.assertIn("real defect here", (rundir / "_summary.md").read_text())

    def test_resume_reuses_the_original_prompt(self):
        seen = []
        self.use_seats({
            "a": lambda p, r, t: (seen.append(p), (-2, "nope"))[1],
        })
        argv = ["--task", "the original question", "--seats", "a",
                "--out", str(self.tmp), "--repo", str(self.tmp)]
        panel.main(argv)
        panel.main(["--resume", "last", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])

    def test_resume_of_a_clean_run_is_a_no_op(self):
        calls = []
        self.use_seats({"a": lambda p, r, t: (calls.append(1),
                                              (0, findings_block([])))[1]})
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        panel.main(["--resume", "last", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        self.assertEqual(len(calls), 1)


class MemoryTests(TempRunCase):
    def _capture_prompt(self):
        seen = []
        self.use_seats({"a": lambda p, r, t: (seen.append(p),
                                              (0, findings_block([])))[1]})
        return seen

    def test_residuals_are_injected(self):
        repo = self.tmp / "repo"
        (repo / ".panel").mkdir(parents=True)
        (repo / ".panel" / "residuals.md").write_text(
            "- known: the retry cap is deliberate")
        seen = self._capture_prompt()
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(repo)])
        self.assertIn("retry cap is deliberate", seen[0])
        self.assertIn("Do NOT re-report", seen[0])

    def test_no_residuals_flag_suppresses_them(self):
        repo = self.tmp / "repo"
        (repo / ".panel").mkdir(parents=True)
        (repo / ".panel" / "residuals.md").write_text("- known: whatever")
        seen = self._capture_prompt()
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(repo), "--no-residuals"])
        self.assertNotIn("whatever", seen[0])

    def test_round_two_carries_the_previous_findings(self):
        seen = []
        self.use_seats({
            "a": lambda p, r, t: (seen.append(p), (0, findings_block(
                [{"file": "a.py", "line": 3,
                  "claim": "unbounded queue growth", "severity": "high"}])))[1],
        })
        base = ["--task", "q", "--seats", "a", "--out", str(self.tmp),
                "--repo", str(self.tmp)]
        panel.main(base)
        panel.main(base + ["--round", "2"])
        self.assertNotIn("unbounded queue growth", seen[0])
        self.assertIn("unbounded queue growth", seen[1])
        self.assertIn("EARLIER ROUND", seen[1])


class SummaryTests(TempRunCase):
    def test_summary_reports_lineages_and_no_vote_seats(self):
        self.use_seats({
            "codex": lambda p, r, t: (0, findings_block(
                [{"file": "a.py", "line": 7, "claim": "race on the counter",
                  "severity": "high"}])),
            "kimi": lambda p, r, t: (0, findings_block(
                [{"file": "a.py", "line": 8, "claim": "race on the counter",
                  "severity": "high"}])),
            "gemini": lambda p, r, t: (0, "VERDICT: CLEAN, nothing to add"),
        })
        panel.main(["--task", "q", "--seats", "codex,kimi,gemini",
                    "--out", str(self.tmp), "--repo", str(self.tmp)])
        text = (panel._latest_run(self.tmp) / "_summary.md").read_text()
        self.assertIn("| 2 | high | a.py | race on the counter", text)
        self.assertIn("no vote", text)
        self.assertIn("gemini", text.split("no vote")[1])

    def test_summary_ranks_seats_by_intelligence_and_says_how_to_weigh(self):
        self.use_seats({
            "codex": lambda p, r, t: (0, findings_block(
                [{"file": "a.py", "line": 7, "claim": "race on the counter",
                  "severity": "high"}])),
            "kimi": lambda p, r, t: (0, findings_block(
                [{"file": "a.py", "line": 8, "claim": "race on the counter",
                  "severity": "high"}])),
            "gemini": lambda p, r, t: (0, "VERDICT: CLEAN, nothing to add"),
        })
        panel.main(["--task", "q", "--seats", "gemini,kimi,codex",
                    "--out", str(self.tmp), "--repo", str(self.tmp)])
        run = panel._latest_run(self.tmp)
        text = (run / "_summary.md").read_text()
        seats = text.split("## Seats")[1]
        ranked = sorted(("codex", "kimi", "gemini"),
                        key=lambda n: -(panel.intelligence_of(n) or -1))
        self.assertEqual(sorted(("codex", "kimi", "gemini"),
                                key=lambda n: seats.index(f"**{n}**")), ranked)
        for name in ("codex", "kimi", "gemini"):
            self.assertIn(f"**{name}** (AA {panel.intelligence_label(name)},",
                          seats)
        self.assertIn(panel.INTELLIGENCE["index"], text)
        self.assertIn("| top AA |", text)
        result = json.loads((run / "_result.json").read_text())
        self.assertEqual(result["seats"]["codex"]["intelligence"],
                         panel.intelligence_of("codex"))
        top = max(s for s in (panel.intelligence_of("codex"),
                              panel.intelligence_of("kimi")) if s is not None)
        self.assertEqual(result["findings"][0]["top_intelligence"], top)

    def test_scores_are_the_ones_recorded_when_the_seat_answered(self):
        # Codex review 2026-10-29: a reader holding another roster (a
        # long-lived MCP server after seat-refresh moved a pin, or scores
        # re-read later) must not relabel answers a different model gave
        self.use_seats({"codex": lambda p, r, t: (0, findings_block(
            [{"file": "a.py", "line": 7, "claim": "race", "severity": "high"}]))})
        panel.main(["--task", "q", "--seats", "codex",
                    "--out", str(self.tmp), "--repo", str(self.tmp)])
        run = panel._latest_run(self.tmp)
        meta = json.loads((run / panel.RUN_META).read_text())
        recorded = panel.intelligence_of("codex")
        self.assertEqual(meta["intelligence"]["codex"]["score"], recorded)
        moved = json.loads(json.dumps(panel.REGISTRY))
        moved["codex"]["intelligence"]["score"] = 99
        source = dict(panel.INTELLIGENCE)
        self.addCleanup(panel.INTELLIGENCE.update, source)
        panel.INTELLIGENCE.update(index="Other Index v9", as_of="2030-01-01")
        result = panel.collect_run(run, meta, moved)
        self.assertEqual(result["seats"]["codex"]["intelligence"], recorded)
        self.assertEqual(result["findings"][0]["top_intelligence"], recorded)
        text = (run / "_summary.md").read_text()
        self.assertNotIn("AA 99", text)
        # the edition and date named are the ones the scores came from
        self.assertIn(f"{source['index']} (read {source['as_of']})", text)
        self.assertNotIn("Other Index v9", text)


class HonestAccountingTests(TempRunCase):
    """Regressions from a real run (20260803_104852) where three seats died,
    the summary still reported '6 answered (6 independent lineages)', and the
    agent reading it synthesized a 3-voice panel believing it had 6."""

    def _run(self):
        self.use_seats({
            "codex": lambda p, r, t: (1, "Not inside a trusted directory"),
            "gemini": lambda p, r, t: (0, findings_block(
                [{"file": "a.py", "line": 1, "claim": "real finding here",
                  "severity": "high"}])),
            "cursor-grok": lambda p, r, t: (1, "Authentication required"),
            "claude-fable": lambda p, r, t: (1, "out of usage credits"),
        })
        panel.main(["--task", "q", "--seats",
                    "codex,gemini,cursor-grok,claude-fable",
                    "--out", str(self.tmp), "--repo", str(self.tmp)])
        return panel._latest_run(self.tmp)

    def test_failed_seats_are_not_counted_as_answered(self):
        text = (self._run() / "_summary.md").read_text()
        self.assertIn("seats: 1 answered (1 independent lineages", text)
        self.assertNotIn("4 answered", text)

    def test_failed_seats_are_named_with_their_exit_code(self):
        text = (self._run() / "_summary.md").read_text()
        self.assertIn("FAILED, contributed nothing:", text)
        for seat in ("codex", "cursor-grok", "claude-fable"):
            self.assertIn(seat, text.split("FAILED")[1].split("\n")[0])

    def test_result_json_separates_failed_from_answered(self):
        result = json.loads((self._run() / "_result.json").read_text())
        self.assertEqual(sorted(result["failed"]),
                         ["claude-fable", "codex", "cursor-grok"])
        self.assertIn("trusted directory", result["failed"]["codex"]["error"])
        self.assertTrue(result["complete"])

    def test_incomplete_run_says_so_at_the_top(self):
        self.use_seats({"a": lambda p, r, t: (0, findings_block([]))})
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        meta = json.loads((rundir / "run.json").read_text())
        meta["planned"] = ["a", "never-reported"]
        (rundir / "run.json").write_text(json.dumps(meta))
        panel.collect_run(rundir, meta, panel.REGISTRY)
        text = (rundir / "_summary.md").read_text()
        self.assertIn("STATUS: INCOMPLETE", text)
        self.assertIn("never-reported", text)
        self.assertIn("does NOT mean the run finished", text)


class CitedPathTests(unittest.TestCase):
    """One seat's guessed path must not borrow the credibility of a
    multi-lineage cluster. Live case: muse invented
    morning-report/src/synthesizer.py, deepseek agreed with the claim but named
    no file, and the summary presented the fake path as a 2-lineage location."""

    def test_path_from_a_single_seat_is_attributed(self):
        clusters = panel.cluster_findings({
            "muse": [{"file": "morning-report/src/synthesizer.py", "line": None,
                      "claim": "remove the reflexion loop, use sampling",
                      "severity": "medium"}],
            "deepseek": [{"file": None, "line": None,
                          "claim": "remove the reflexion loop, use sampling",
                          "severity": "medium"}],
        })
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["lineages"]), 2)
        self.assertIn("path from muse only", clusters[0]["where"])

    def test_a_path_every_citing_seat_agrees_on_is_not_hedged(self):
        clusters = panel.cluster_findings({
            "codex": [{"file": "a.py", "line": 7, "claim": "race on counter",
                       "severity": "high"}],
            "kimi": [{"file": "a.py", "line": 8, "claim": "race on counter",
                      "severity": "high"}],
        })
        self.assertEqual(clusters[0]["where"], "a.py")
        self.assertNotIn("only", clusters[0]["where"])

    def test_same_filename_different_paths_is_flagged_disputed(self):
        # The exact live case: both seats named a synthesizer.py, at paths
        # that cannot both be right. Neither may be presented as the location.
        clusters = panel.cluster_findings({
            "muse": [{"file": "morning-report/src/synthesizer.py", "line": None,
                      "claim": "remove the reflexion loop, use sampling",
                      "severity": "medium"}],
            "deepseek": [{"file": "morning-report/synthesizer.py", "line": None,
                          "claim": "remove the reflexion loop, use sampling",
                          "severity": "medium"}],
        })
        self.assertEqual(len(clusters), 1, "same defect, so still one finding")
        self.assertEqual(len(clusters[0]["lineages"]), 2)
        self.assertTrue(clusters[0]["where"].startswith("disputed:"),
                        clusters[0]["where"])
        self.assertIn("morning-report/src/synthesizer.py", clusters[0]["where"])
        self.assertIn("morning-report/synthesizer.py", clusters[0]["where"])

    def test_different_files_never_merge_into_one_finding(self):
        # Understating agreement is safer than inflating the lineage count
        # that drives action, so two files means two findings.
        clusters = panel.cluster_findings({
            "muse": [{"file": "src/grading/strict_grader.py", "line": None,
                      "claim": "distill the judge into a program",
                      "severity": "medium"}],
            "deepseek": [{"file": "distilled_judge.py", "line": None,
                          "claim": "distill the judge into a program",
                          "severity": "medium"}],
        })
        self.assertEqual(len(clusters), 2)
        for c in clusters:
            self.assertEqual(len(c["lineages"]), 1)

    def test_line_jitter_in_one_file_is_agreement_not_dispute(self):
        # two seats naming the same file a line apart agree about where it is
        clusters = panel.cluster_findings({
            "codex": [{"file": "a.py", "line": 7, "claim": "race on counter",
                       "severity": "high"}],
            "kimi": [{"file": "a.py", "line": 8, "claim": "race on counter",
                      "severity": "high"}],
        })
        self.assertEqual(clusters[0]["where"], "a.py")
        self.assertNotIn("disputed", clusters[0]["where"])


class ResumeRepoOverrideTests(TempRunCase):
    """codex refuses a non-git directory. Without an override, every resume
    re-sends it to the same bad directory and fails identically forever."""

    def test_repo_can_be_changed_on_resume(self):
        seen = []

        def codex(prompt, repo, timeout):
            seen.append(str(repo))
            return (1, "Not inside a trusted directory") if len(seen) == 1 \
                else (0, findings_block([]))

        self.use_seats({"codex": codex})
        bad, good = self.tmp / "notarepo", self.tmp / "realrepo"
        bad.mkdir()
        good.mkdir()
        panel.main(["--task", "q", "--seats", "codex", "--out", str(self.tmp),
                    "--repo", str(bad)])
        rundir = panel._latest_run(self.tmp)
        self.assertEqual(panel.seat_status(rundir, "codex")[1], 1)

        panel.main(["--resume", str(rundir), "--out", str(self.tmp),
                    "--repo", str(good)])
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1], str(good.resolve()))
        self.assertEqual(panel.seat_status(rundir, "codex"), (True, 0))
        meta = json.loads((rundir / "run.json").read_text())
        self.assertEqual(meta["repo"], str(good.resolve()))

    def test_resume_without_repo_keeps_the_original(self):
        seen = []
        self.use_seats({"a": lambda p, r, t: (seen.append(str(r)),
                                              (-2, "nope"))[1]})
        repo = self.tmp / "orig"
        repo.mkdir()
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(repo)])
        panel.main(["--resume", "last", "--out", str(self.tmp)])
        self.assertEqual(seen[1], str(repo.resolve()))


class GradeTests(TempRunCase):
    def test_grade_scores_hits_and_misses(self):
        self.use_seats({
            "sharp": lambda p, r, t: (0, findings_block([
                {"file": "panel.py", "line": 100,
                 "claim": "in-order collection blocks persistence",
                 "severity": "high"},
                {"file": "panel.py", "line": 200,
                 "claim": "residuals never reach the prompt",
                 "severity": "medium"}])),
            "shallow": lambda p, r, t: (0, findings_block([
                {"file": "panel.py", "line": 999,
                 "claim": "variable naming could be nicer",
                 "severity": "low"}])),
        })
        panel.main(["--task", "q", "--seats", "sharp,shallow",
                    "--out", str(self.tmp), "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        key = self.tmp / "key.json"
        key.write_text(json.dumps({"defects": [
            {"id": "d1", "file": "panel.py",
             "keywords": ["persistence", "blocks"],
             "desc": "finished seats are not written until the slow seat ends"},
            {"id": "d2", "file": "panel.py", "keywords": ["residuals"],
             "desc": "accepted residuals are not injected"},
        ]}))
        rows = panel.grade(rundir, key, panel.REGISTRY)
        by_seat = {r[0]: r for r in rows}
        self.assertEqual(by_seat["sharp"][1], 2)
        self.assertEqual(by_seat["shallow"][1], 0)
        self.assertEqual(by_seat["shallow"][3], 1, "its one finding is unmatched")
        report = (rundir / "_grade.md").read_text()
        self.assertIn("**d1**", report)
        self.assertIn("**d2**", report)
        self.assertNotIn("MISSED BY EVERY SEAT", report)

    def test_grade_flags_a_defect_every_seat_missed(self):
        self.use_seats({"a": lambda p, r, t: (0, findings_block([]))})
        panel.main(["--task", "q", "--seats", "a", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        key = self.tmp / "key.json"
        key.write_text(json.dumps({"defects": [
            {"id": "d9", "file": "x.py", "keywords": ["silent"],
             "desc": "silent truncation"}]}))
        panel.grade(rundir, key, panel.REGISTRY)
        self.assertIn("MISSED BY EVERY SEAT", (rundir / "_grade.md").read_text())


class ErrorTailTests(TempRunCase):
    def test_tty_chrome_and_stack_frames_are_stripped(self):
        tail = panel._error_tail(GEMINI_CHROME)
        self.assertTrue(tail.startswith("Error when talking"), tail)
        self.assertNotIn("True color", tail)
        self.assertNotIn("classifyGoogleError", tail)

    def test_result_json_reports_the_real_error_not_the_tty_warning(self):
        self.use_seats({"g": lambda p, r, t: (1, GEMINI_CHROME)})
        panel.main(["--task", "q", "--seats", "g", "--out", str(self.tmp),
                    "--repo", str(self.tmp)])
        result = json.loads(
            (panel._latest_run(self.tmp) / "_result.json").read_text())
        err = result["failed"]["g"]["error"]
        self.assertIn("TerminalQuotaError", err)
        self.assertNotIn("True color", err)


class SeatHealthTests(TempRunCase):
    def test_a_failed_seat_records_why(self):
        self.use_seats({"g": lambda p, r, t: (1, GEMINI_CHROME)})
        panel.main(["--task", "q", "--seats", "g", "--repo", str(self.tmp)])
        data = json.loads(
            (self.tmp / "runs" / "_seat-health.json").read_text())
        self.assertFalse(data["g"]["ok"])
        self.assertIn("TerminalQuotaError", data["g"]["error"])
        self.assertNotIn("True color", data["g"]["error"])

    def test_a_later_success_clears_the_flag(self):
        self.use_seats({"g": lambda p, r, t: (1, "quota")})
        panel.main(["--task", "q", "--seats", "g", "--repo", str(self.tmp)])
        self.use_seats({"g": lambda p, r, t: (0, findings_block([]))})
        panel.main(["--task", "q", "--seats", "g", "--repo", str(self.tmp)])
        data = json.loads(
            (self.tmp / "runs" / "_seat-health.json").read_text())
        self.assertTrue(data["g"]["ok"])

    def test_list_seats_surfaces_the_last_failure(self):
        import contextlib
        import io
        self.use_seats({"g": lambda p, r, t: (1, GEMINI_CHROME)})
        panel.main(["--task", "q", "--seats", "g", "--repo", str(self.tmp)])
        registry = {"g": {"lineage": "google",
                          "cmd": {"argv": ["true", "{prompt}"]}}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            panel.list_seats(registry)
        self.assertIn("last fail", buf.getvalue())
        self.assertIn("TerminalQuotaError", buf.getvalue())
        self.assertIn("configured", buf.getvalue())
        self.assertNotIn("ready", buf.getvalue())


class ModeTests(TempRunCase):
    def test_diff_and_task_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            panel.main(["--out", str(self.tmp), "--repo", str(self.tmp)])

    def test_diff_mode_saves_the_diff_and_a_codex_prompt(self):
        seen = {}
        self.use_seats({
            "codex": lambda p, r, t: (seen.__setitem__("codex", p),
                                      (0, findings_block([])))[1],
            "kimi": lambda p, r, t: (seen.__setitem__("kimi", p),
                                     (0, findings_block([])))[1],
        })
        d = self.tmp / "change.diff"
        d.write_text("--- a/x.py\n+++ b/x.py\n+print('hi')\n")
        panel.main(["--diff", str(d), "--seats", "codex,kimi",
                    "--out", str(self.tmp), "--repo", str(self.tmp)])
        rundir = panel._latest_run(self.tmp)
        self.assertTrue((rundir / "reviewed.diff").exists())
        self.assertIn("print('hi')", seen["kimi"])
        self.assertNotIn("print('hi')", seen["codex"])
        self.assertIn("reviewed.diff", seen["codex"])


if __name__ == "__main__":
    unittest.main()
