"""Offline billing and timing checks; all history is synthetic and temporary."""

import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "panel"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel
from test_panel import TempRunCase, findings_block


class EstimateTests(TempRunCase):
    def setUp(self):
        super().setUp()
        self.history = self.tmp / "runs"
        self.fixture = Path(__file__).parent / "fixtures" / "estimate" / "20260102_030405"
        self.registry = {
            "subscriber": {"billing": "subscription", "default": True, "lineage": "alpha"},
            "metered": {"billing": "metered", "default": True, "lineage": "beta"},
            "credit": {"billing": "credit", "default": True, "lineage": "gamma"},
            "unknown": {"billing": "unknown", "default": True, "lineage": "delta"},
            "missing": {"default": True, "lineage": "epsilon"},
            "invalid": {"billing": "typo", "default": True, "lineage": "zeta"},
        }
        self.use_seats({n: Mock(side_effect=AssertionError("no seat may launch"))
                        for n in self.registry})
        for target, value in (("REGISTRY", self.registry), ("MERGE_CONFIG", {}),
                              ("PRESETS", {"sample": list(self.registry),
                                           "quick": ["metered", "unknown"]})):
            p = patch.object(panel, target, value)
            p.start()
            self.addCleanup(p.stop)

    def sample(self, seconds=60, repo="sample", suffix="", **updates):
        dest = self.history / repo / (self.fixture.name + suffix)
        shutil.copytree(self.fixture, dest)
        meta = json.loads((dest / "run.json").read_text())
        meta.update(updates)
        (dest / "run.json").write_text(json.dumps(meta))
        start = datetime.datetime.strptime(self.fixture.name, "%Y%m%d_%H%M%S").timestamp()
        os.utime(dest / "subscriber.md", (start + seconds, start + seconds))
        return dest

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = panel.main(list(args))
        self.assertEqual(result, 0)
        return output.getvalue()

    def test_nearest_rank_percentiles_across_repos(self):
        # P95 for 20 samples is rank 19, not the maximum or interpolation.
        for minute in reversed(range(1, 21)):
            self.sample(minute * 60, repo=f"sample-{minute}")
        self.assertEqual(panel.stats_for("subscriber", self.history),
                         {"n": 20, "p50": 10.0, "p95": 19.0})

    def test_odd_count_and_single_sample(self):
        self.sample(120)
        self.assertEqual(panel.stats_for("subscriber", self.history),
                         {"n": 1, "p50": 2.0, "p95": 2.0})
        self.sample(60, repo="second")
        self.sample(180, repo="third")
        self.assertEqual(panel.stats_for("subscriber", self.history),
                         {"n": 3, "p50": 2.0, "p95": 3.0})

    def test_missing_history_is_read_only_and_printed(self):
        with patch.object(panel, "execute", side_effect=AssertionError("execute")), \
                patch.object(panel, "merge_if_needed", side_effect=AssertionError("merge")), \
                patch.object(panel, "claim_run", side_effect=AssertionError("lock")), \
                patch.object(panel, "load_key", side_effect=AssertionError("credentials")), \
                patch.object(panel, "run_cmd", side_effect=AssertionError("process")), \
                patch.object(panel.urllib.request, "urlopen", side_effect=AssertionError("network")):
            out = self.cli("--estimate", "--out", str(self.tmp / "output"))
        self.assertIn("no history", out)
        self.assertIn("slowest P95: no history", out)
        self.assertFalse(self.history.exists())
        self.assertFalse((self.tmp / "output").exists())
        for seat in panel.PANEL.values():
            seat.assert_not_called()

    def test_estimate_with_diff_never_reads_inputs_or_creates_output(self):
        self.sample(120)
        out = self.cli("--estimate", "--diff", str(self.tmp / "absent.diff"),
                       "--file", str(self.tmp / "absent.txt"), "--out", str(self.tmp / "out"))
        self.assertIn("2.00", out)
        self.assertIn("metered seats: 1 (metered)", out)
        self.assertIn("slowest P95: 2.00 min (subscriber", out)
        self.assertFalse((self.tmp / "out").exists())
        self.assertEqual(len(list(self.history.glob("*/*/run.json"))), 1)

    def test_invalid_missing_and_late_history_is_ignored(self):
        cases = [
            {"planned": []}, {"planned": "subscriber"}, {"ts": "bad"},
            {"ts": "20260103_030405"}, {"timeout": 0}, {"timeout": -1},
            {"timeout": None}, {"timeout": "2400"}, {"timeout": True},
            {"timeout": float("nan")}, {"timeout": float("inf")},
        ]
        for i, changes in enumerate(cases):
            self.sample(repo=f"invalid-{i}", **changes)
        self.sample(-1, repo="negative")
        self.sample(2461, repo="late")
        missing = self.sample(repo="missing")
        (missing / "subscriber.md").unlink()
        malformed = self.sample(repo="malformed")
        (malformed / "run.json").write_text("{")
        wrong_type = self.sample(repo="wrong-type")
        (wrong_type / "run.json").write_text("[]")
        missing_timeout = self.sample(repo="missing-timeout")
        (missing_timeout / "run.json").write_text(
            json.dumps({"ts": self.fixture.name, "planned": ["subscriber"]}))
        self.assertEqual(panel.stats_for("subscriber", self.history),
                         {"n": 0, "p50": None, "p95": None})

    def test_timeout_grace_boundary_and_collision_suffix(self):
        self.sample(2460, suffix="_2")
        self.assertEqual(panel.stats_for("subscriber", self.history),
                         {"n": 1, "p50": 41.0, "p95": 41.0})

    def test_non_timestamp_suffix_is_ignored(self):
        self.sample(suffix="_copied")
        self.assertEqual(panel.stats_for("subscriber", self.history)["n"], 0)

    def test_failed_and_incomplete_runs_still_supply_elapsed_samples(self):
        dest = self.sample(30, completed=False)
        (dest / "subscriber.md").write_text("# subscriber (exit -1)\n\nTimed out.")
        start = datetime.datetime.strptime(self.fixture.name, "%Y%m%d_%H%M%S").timestamp()
        os.utime(dest / "subscriber.md", (start + 30, start + 30))
        self.assertEqual(panel.stats_for("subscriber", self.history)["p95"], .5)

    @unittest.skipUnless(hasattr(time, "tzset"), "requires timezone switching")
    def test_timestamp_uses_local_timezone(self):
        try:
            with patch.dict(os.environ, {"TZ": "EST5"}):
                time.tzset()
                self.sample(180)
                self.assertEqual(panel.stats_for("subscriber", self.history)["p50"], 3.0)
        finally:
            time.tzset()

    def test_no_metered_filters_default_explicit_and_preset_plans(self):
        for selection in ([], ["--seats", ",".join(self.registry)], ["--preset", "sample"]):
            with self.subTest(selection=selection):
                out = self.cli("--estimate", "--no-metered", *selection)
                self.assertIn("subscriber", out)
                self.assertIn("metered seats: 0 (none)", out)
                for seat in set(self.registry) - {"subscriber"}:
                    self.assertIn(f"[{seat}] dropped by --no-metered: billing=", out)

    def test_allow_unknown_is_explicit_warned_and_cannot_allow_metered_or_credit(self):
        out = self.cli("--estimate", "--no-metered", "--allow-unknown", "unknown",
                       "--allow-unknown", "metered", "--allow-unknown", "credit")
        self.assertIn("WARNING: [unknown] billing is unverified", out)
        self.assertNotIn("[unknown] dropped", out)
        self.assertIn("[metered] dropped", out)
        self.assertIn("[credit] dropped", out)

    def test_allow_unknown_does_not_select_or_override_skip(self):
        out = self.cli("--estimate", "--seats", "subscriber", "--no-metered",
                       "--allow-unknown", "unknown")
        self.assertNotIn("unverified", out)
        out = self.cli("--estimate", "--no-metered", "--allow-unknown", "unknown",
                       "--skip", "unknown")
        self.assertNotIn("unverified", out)

    def test_unknown_allowlist_names_and_missing_policy_are_errors(self):
        for argv in (["--estimate", "--no-metered", "--allow-unknown", "absent"],
                     ["--estimate", "--allow-unknown", "unknown"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                panel.main(argv)

    def test_estimate_refuses_mutating_modes_before_any_write(self):
        for mode in ("--resume", "--grade", "--verdict"):
            with self.subTest(mode=mode), self.assertRaises(SystemExit):
                panel.main(["--estimate", mode, str(self.tmp / "absent")])
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_estimate_all_filtered_is_success_but_execution_is_refused(self):
        out = self.cli("--estimate", "--no-metered", "--seats", "metered")
        self.assertIn("metered seats: 0 (none)", out)
        with self.assertRaisesRegex(SystemExit, "no seats selected"):
            panel.main(["--task", "synthetic", "--no-metered", "--seats", "metered"])
        self.assertFalse(self.history.exists())

    def test_estimate_matches_dead_lane_selection(self):
        with patch.object(panel, "dead_lanes", return_value={"subscriber": "quota"}):
            out = self.cli("--estimate")
            self.assertIn("[subscriber] skipped", out)
            explicit = self.cli("--estimate", "--seats", "subscriber")
            self.assertNotIn("[subscriber] skipped", explicit)
            all_seats = self.cli("--estimate", "--include-dead")
            self.assertNotIn("[subscriber] skipped", all_seats)

    def test_conditional_escalation_and_merge_are_disclosed_and_filtered(self):
        with patch.object(panel, "MERGE_CONFIG", {"judge": "credit"}):
            out = self.cli("--estimate", "--escalate")
            self.assertIn("conditional escalation seats", out)
            self.assertIn("conditional merge judge: credit, billing=credit", out)
            out = self.cli("--estimate", "--escalate", "--no-metered")
            self.assertIn("[credit] dropped", out)
            self.assertNotIn("conditional merge judge", out)
            self.assertIn("conditional escalation seats (included above): subscriber", out)

    def test_execution_escalation_and_later_resume_preserve_billing_policy(self):
        calls = []
        # Only this synthetic subscription seat can be called; all others
        # still raise if the billing filter is bypassed.
        panel.PANEL["subscriber"] = lambda p, r, t: (calls.append("subscriber"), (-2, "unavailable"))[1]
        with patch.object(panel, "MERGE_CONFIG", {"judge": "metered"}):
            self.cli("--task", "synthetic", "--escalate", "--no-metered")
            rundir = next(self.history.glob("*/*/run.json")).parent
            meta = json.loads((rundir / "run.json").read_text())
            self.assertEqual(meta["planned"], ["subscriber"])
            self.assertEqual(meta["billing"], {"subscriber": "subscription"})
            self.assertFalse(meta["merge"])
            # A changed route on resume must be checked again.
            self.registry["subscriber"]["billing"] = "metered"
            out = self.cli("--resume", str(rundir))
            self.assertIn("[subscriber] dropped", out)
            self.assertIn("no permitted seats need a refill", out)
            self.assertEqual(calls, ["subscriber"])
            self.assertFalse((rundir / "subscriber.prev.md").exists())
        for name, seat in panel.PANEL.items():
            if name != "subscriber":
                seat.assert_not_called()

    def test_resume_filters_both_unfinished_tiers_before_renaming(self):
        rundir = self.sample(planned=["subscriber", "metered", "unknown"],
                             escalation={"quick": ["subscriber", "metered"],
                                         "rest": ["unknown"], "decided": False})
        (rundir / "prompt.md").write_text("synthetic")
        (rundir / "metered.md").write_text("# metered (exit -2)\n\nUnavailable")
        with patch.object(panel, "MERGE_CONFIG", {"judge": "credit"}):
            out = self.cli("--resume", str(rundir), "--no-metered")
        self.assertIn("[metered] dropped", out)
        self.assertIn("[unknown] dropped", out)
        self.assertFalse((rundir / "metered.prev.md").exists())
        self.assertEqual(json.loads((rundir / "run.json").read_text())["planned"], ["subscriber"])
        for seat in panel.PANEL.values():
            seat.assert_not_called()

    def test_allowed_unknown_can_run_with_warning(self):
        call = Mock(return_value=(0, findings_block([])))
        panel.PANEL["unknown"] = call
        out = self.cli("--task", "synthetic", "--seats", "unknown", "--no-metered",
                       "--allow-unknown", "unknown")
        self.assertIn("WARNING: [unknown] billing is unverified", out)
        call.assert_called_once()

    def test_resume_preserves_successful_metered_history(self):
        rundir = self.sample(planned=["subscriber", "metered"], merge=False,
                             escalation={"quick": ["subscriber"], "rest": ["metered"],
                                         "decided": True, "escalated": True})
        (rundir / "prompt.md").write_text("synthetic")
        (rundir / "metered.md").write_text("# metered (exit 0)\n\n" + findings_block([]))
        self.cli("--resume", str(rundir), "--no-metered")
        self.assertEqual(json.loads((rundir / "run.json").read_text())["planned"],
                         ["subscriber", "metered"])
        for seat in panel.PANEL.values():
            seat.assert_not_called()


class BillingRegistryTests(unittest.TestCase):
    def test_every_bundled_seat_has_a_known_billing_label(self):
        for name, seat in panel.load_registry().items():
            self.assertIn(seat.get("billing"), {"subscription", "metered", "credit", "unknown"}, name)

    def test_documented_metered_and_credit_routes(self):
        registry = panel.load_registry()
        for name in ("gemini-argon", "gemini-carbon", "mimo-ultraspeed", "pplx",
                     "glm", "deepseek", "qwen-max", "glm-oc", "deepseek-oc", "qwen-max-oc"):
            self.assertEqual(registry[name]["billing"], "metered", name)
        self.assertEqual(registry["copilot"]["billing"], "credit")


if __name__ == "__main__":
    unittest.main()
