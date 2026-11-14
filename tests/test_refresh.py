#!/usr/bin/env python3
"""Tests for scripts/seat-refresh.py. Offline on purpose: a fake models.dev
catalog and temp copies of seats.json — no network, no model calls.
"""

import copy
import datetime
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "panel"))
import panel  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "seat_refresh", ROOT / "scripts" / "seat-refresh.py")
refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh)

SEATS_JSON = ROOT / "skills" / "panel" / "seats.json"

_RUNS = tempfile.TemporaryDirectory()
_ENV = patch.dict("os.environ", {"PANEL_RUNS": _RUNS.name})
# main() runs the challenger watch: keep it offline. No AA key (so no
# request), and no CLI model lists (cursor-agent, opencode) unless a test
# supplies one.
_REAL_LOCAL_ROUTES = refresh.local_routes
_OFFLINE = [patch.object(refresh, "AA_KEY", {
                "file": str(Path(_RUNS.name) / "no-aa-env"),
                "env_key": "AA_API_KEY"}),
            patch.object(refresh, "local_routes", return_value={})]


def setUpModule():
    # --notify keeps alert state under $PANEL_RUNS; never touch the real one
    _ENV.start()
    for p in _OFFLINE:
        p.start()


def tearDownModule():
    for p in _OFFLINE:
        p.stop()
    _ENV.stop()
    _RUNS.cleanup()

CATALOG = {
    "xai": {"models": {
        "grok-4.5": {"release_date": "2026-07-08"},
        "grok-4.6": {"release_date": "2026-08-12"},
        "grok-4.20-0309-reasoning": {"release_date": "2026-03-09"},
        "grok-build-0.1": {"release_date": "2026-04-16"},
        "grok-imagine-video-1.5": {"release_date": "2026-05-30"},
    }},
    "moonshotai": {"models": {
        "kimi-k3": {"release_date": "2026-07-16"},
        "kimi-k2.7-code": {"release_date": "2026-06-12"},
        "kimi-k2.6": {"release_date": "2026-04-21"},
    }},
}

GROK_TRACK = {"provider": "xai",
              "match": r"^grok-([0-9]+\.[0-9]+)$", "format": "{id}"}
CURSOR_TRACK = {"provider": "xai",
                "match": r"^grok-([0-9]+\.[0-9]+)$",
                "format": "cursor-grok-{1}-high-fast"}
KIMI_TRACK = {"provider": "moonshotai",
              "match": r"^kimi-k([0-9.]+)$", "format": "kimi-code/k{1}"}


def grok_entry(model="grok-4.5"):
    return {"lineage": "xai", "track": dict(GROK_TRACK),
            "cmd": {"argv": ["grok", "-p", "{prompt}", "-m", model,
                             "--effort", "high"]}}


class PickTests(unittest.TestCase):
    def test_release_date_wins_over_id_numerics(self):
        # grok-9.9 has bigger digits but an older date: date is primary
        cat = copy.deepcopy(CATALOG)
        cat["xai"]["models"]["grok-9.9"] = {"release_date": "2026-01-01"}
        self.assertEqual(refresh.pick_latest(cat, GROK_TRACK)[0], "grok-4.6")

    def test_variants_never_match_the_family_regex(self):
        ids = set(refresh._matches(CATALOG, GROK_TRACK))
        self.assertEqual(ids, {"grok-4.5", "grok-4.6"})

    def test_kimi_code_line_and_chat_line_stay_separate(self):
        code_track = {"provider": "moonshotai",
                      "match": r"^kimi-k([0-9.]+)-code$",
                      "format": "kimi-code/k{1}"}
        self.assertEqual(refresh.pick_latest(CATALOG, KIMI_TRACK)[0],
                         "kimi-k3")
        self.assertEqual(refresh.pick_latest(CATALOG, code_track)[0],
                         "kimi-k2.7-code")

    def test_render_substitutes_id_and_groups(self):
        self.assertEqual(refresh.render(GROK_TRACK, "grok-4.6"), "grok-4.6")
        self.assertEqual(refresh.render(CURSOR_TRACK, "grok-4.6"),
                         "cursor-grok-4.6-high-fast")
        self.assertEqual(refresh.render(KIMI_TRACK, "kimi-k3"),
                         "kimi-code/k3")


class ModelSlotTests(unittest.TestCase):
    def test_cmd_short_and_long_flags(self):
        self.assertEqual(refresh.model_slot(grok_entry())[0], "grok-4.5")
        entry = {"cmd": {"argv": ["cursor-agent", "--model", "m1", "-f"]}}
        self.assertEqual(refresh.model_slot(entry)[0], "m1")

    def test_http_body_model(self):
        entry = {"http": {"body": {"model": "glm-5.2", "input": "{prompt}"}}}
        value, setter = refresh.model_slot(entry)
        self.assertEqual(value, "glm-5.2")
        setter("glm-6")
        self.assertEqual(entry["http"]["body"]["model"], "glm-6")

    def test_unpinned_seat_has_no_slot(self):
        entry = {"cmd": {"argv": ["gemini", "-p", "{prompt}"]}}
        self.assertEqual(refresh.model_slot(entry), (None, None))


class PlanTests(unittest.TestCase):
    def test_stale_seat_is_flagged_with_the_rendered_value(self):
        rows = refresh.plan_refresh({"grok": grok_entry()}, CATALOG)
        self.assertEqual(rows[0]["status"], "stale")
        self.assertEqual(rows[0]["proposed"], "grok-4.6")
        self.assertEqual(rows[0]["released"], "2026-08-12")

    def test_current_seat_is_left_alone(self):
        rows = refresh.plan_refresh({"grok": grok_entry("grok-4.6")}, CATALOG)
        self.assertEqual(rows[0]["status"], "current")

    def test_never_downgrade_when_the_pin_vanishes_from_the_catalog(self):
        # pinned 4.7 is unknown to a catalog whose best is 4.6: hold
        rows = refresh.plan_refresh({"grok": grok_entry("grok-4.7")}, CATALOG)
        self.assertEqual(rows[0]["status"], "current")
        self.assertIn("never downgrade", rows[0]["detail"])

    def test_an_older_off_catalog_pin_is_still_flagged_stale(self):
        rows = refresh.plan_refresh({"grok": grok_entry("grok-3.9")}, CATALOG)
        self.assertEqual(rows[0]["status"], "stale")
        self.assertEqual(rows[0]["detail"], "pinned model not in catalog")

    def qwen_entry(self, model):
        return {"lineage": "alibaba",
                "track": {"provider": "alibaba", "format": "{id}",
                          "match": r"^qwen([0-9.]+)-max(-[0-9]{4})?$"},
                "http": {"body": {"model": model}}}

    def test_a_dated_snapshot_pin_does_not_block_a_newer_version(self):
        # Codex review 2026-10-29: read as digits, -0902 made the pin
        # (3, 8, 902) and outranked qwen3.8.1-max's (3, 8, 1)
        cat = {"alibaba": {"models": {
            "qwen3.8-max": {"release_date": "2026-08-03"},
            "qwen3.8.1-max": {"release_date": "2026-11-01"}}}}
        rows = refresh.plan_refresh(
            {"qwen-max": self.qwen_entry("qwen3.8-max-0902")}, cat)
        self.assertEqual((rows[0]["status"], rows[0]["proposed"]),
                         ("stale", "qwen3.8.1-max"))
        # ...while the plain alias of the same version is still no upgrade
        del cat["alibaba"]["models"]["qwen3.8.1-max"]
        rows = refresh.plan_refresh(
            {"qwen-max": self.qwen_entry("qwen3.8-max-0902")}, cat)
        self.assertEqual(rows[0]["status"], "current")

    def test_snapshots_of_one_version_still_order_by_their_date_suffix(self):
        # Codex re-check 2026-10-29: dropping the suffix tied -0902 and -1001
        track = self.qwen_entry("x")["track"]
        cat = {"alibaba": {"models": {"qwen3.8-max-0902": {},
                                      "qwen3.8-max-1001": {}}}}
        self.assertEqual(refresh.pick_latest(cat, track)[0], "qwen3.8-max-1001")
        cat = {"alibaba": {"models": {
            "qwen3.8-max-0902": {},
            "qwen3.8-max-1001": {"release_date": "2026-10-01"}}}}
        rows = refresh.plan_refresh(
            {"qwen-max": self.qwen_entry("qwen3.8-max-0902")}, cat)
        self.assertEqual((rows[0]["status"], rows[0]["proposed"]),
                         ("stale", "qwen3.8-max-1001"))

    def test_equal_digits_with_an_undated_pin_hold_rather_than_guess(self):
        # k3.1-pro vs k3.1 cannot be ordered from digits; bumping on a guess
        # could swap a pro model for the plain one, so the pin holds and a
        # human decides (never downgrade outranks never miss)
        track = {"provider": "moonshotai", "format": "kimi-code/k{1}{2}",
                 "match": r"^kimi-k([0-9.]+)(-pro)?$"}
        cat = {"moonshotai": {"models": {
            "kimi-k3.1-pro": {},
            "kimi-k3.1": {"release_date": "2026-11-01"}}}}
        entry = {"lineage": "moonshot", "track": track,
                 "cmd": {"argv": ["kimi", "-p", "{prompt}", "-m",
                                  "kimi-code/k3.1-pro"]}}
        rows = refresh.plan_refresh({"kimi": entry}, cat)
        self.assertEqual(rows[0]["status"], "current")

    def test_an_undated_catalog_pin_is_not_replaced_by_an_older_dated_one(self):
        # Codex review 2026-10-29: a pin the catalog lists without a release
        # date sorted below every dated model, older ones included
        cat = {"alibaba-token-plan": {"models": {
            "deepseek-v4.1-flash-1017": {},
            "deepseek-v4-pro-0813": {"release_date": "2026-08-13"}}}}
        entry = {"lineage": "deepseek",
                 "track": {"provider": "alibaba-token-plan", "format": "{id}",
                           "match": r"^deepseek-v([0-9.]+)-(pro|flash)(-[0-9]{4})?$"},
                 "http": {"body": {"model": "deepseek-v4.1-flash-1017"}}}
        rows = refresh.plan_refresh({"deepseek": entry}, cat)
        self.assertEqual(rows[0]["status"], "current")

    def test_untracked_seats_are_ignored(self):
        entry = grok_entry()
        del entry["track"]
        self.assertEqual(refresh.plan_refresh({"grok": entry}, CATALOG), [])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "seats.json"
        # A refresh test must not depend on today's live model pin/grade.
        data = json.loads(SEATS_JSON.read_text())
        kimi = data["seats"]["kimi"]
        _, setter = refresh.model_slot(kimi)
        setter("kimi-code/k3")
        kimi.update(grade="A-", exam="fixture exam")
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    def test_surgery_touches_only_the_one_seat_block(self):
        before = self.path.read_text()
        after = refresh.apply_change(before, "kimi",
                                     "kimi-code/k3", "kimi-code/k4")
        data = json.loads(after)  # still valid JSON
        self.assertIn("kimi-code/k4",
                      data["seats"]["kimi"]["cmd"]["argv"])
        # the rest of the file is byte-identical
        s, e = refresh.seat_span(before, "kimi")
        s2, e2 = refresh.seat_span(after, "kimi")
        self.assertEqual(before[:s], after[:s2])
        self.assertEqual(before[e:], after[e2:])

    def test_a_bumped_seat_loses_its_grade_and_exam(self):
        after = refresh.apply_change(self.path.read_text(), "kimi",
                                     "kimi-code/k3", "kimi-code/k4")
        kimi = json.loads(after)["seats"]["kimi"]
        self.assertEqual(kimi["grade"], "?")
        self.assertNotIn("exam", kimi)

    def test_the_intelligence_block_keeps_the_old_id_so_its_score_goes_stale(self):
        # every scored seat names its pin twice: in the wiring and in the
        # intelligence block. Only the wiring moves; the score must then read
        # as stale instead of riding along onto a model nobody measured
        data = json.loads(self.path.read_text())
        data["seats"]["kimi"]["intelligence"] = {
            "score": 46, "measured": "Kimi K3", "model": "kimi-code/k3",
            "note": "a {brace} inside a string"}
        self.path.write_text(json.dumps(data, indent=2) + "\n")
        after = refresh.apply_change(self.path.read_text(), "kimi",
                                     "kimi-code/k3", "kimi-code/k4")
        kimi = json.loads(after)["seats"]["kimi"]
        self.assertIn("kimi-code/k4", kimi["cmd"]["argv"])
        self.assertEqual(kimi["intelligence"]["model"], "kimi-code/k3")
        self.assertIsNone(refresh.panel.intelligence_of("kimi", {"kimi": kimi}))

    def test_ambiguous_surgery_is_refused(self):
        text = self.path.read_text()
        s, e = refresh.seat_span(text, "kimi")
        doctored = text[:e - 1] + ', "extra": "kimi-code/k3"' + text[e - 1:]
        with self.assertRaises(ValueError):
            refresh.apply_change(doctored, "kimi",
                                 "kimi-code/k3", "kimi-code/k4")

    def test_brace_matching_survives_braces_inside_strings(self):
        # every argv carries "{prompt}"; the span scanner must not count it
        text = self.path.read_text()
        s, e = refresh.seat_span(text, "grok")
        self.assertTrue(text[s:e].lstrip().startswith('"grok"'))
        json.loads('{' + text[s:e] + '}')


class VerifyTests(unittest.TestCase):
    def test_success_exit_without_the_requested_answer_is_not_verified(self):
        for out in ("Model unavailable", "Logged in successfully", "", "Not OK"):
            with self.subTest(out=out), patch.object(
                    refresh.panel, "make_seat",
                    return_value=lambda p, r, t: (0, out)):
                ok, _ = refresh.verify_seat(grok_entry(), "grok-4.6", 5)
                self.assertFalse(ok)

    def test_cli_banner_and_answer_bullet_are_allowed(self):
        with patch.object(refresh.panel, "make_seat",
                          return_value=lambda p, r, t: (0, "CLI banner\n• OK\n")):
            ok, _ = refresh.verify_seat(grok_entry(), "grok-4.6", 5)
        self.assertTrue(ok)

    def test_verification_runs_the_seat_with_the_new_model(self):
        seen = {}

        def fake_make_seat(entry):
            seen["model"] = refresh.model_slot(entry)[0]
            return lambda prompt, repo, timeout: (0, "OK")

        original = refresh.panel.make_seat
        refresh.panel.make_seat = fake_make_seat
        try:
            ok, _ = refresh.verify_seat(grok_entry(), "grok-4.6", timeout=5)
        finally:
            refresh.panel.make_seat = original
        self.assertTrue(ok)
        self.assertEqual(seen["model"], "grok-4.6")

    def test_a_dead_lane_fails_verification(self):
        original = refresh.panel.make_seat
        refresh.panel.make_seat = (
            lambda entry: lambda prompt, repo, timeout: (-2, "CLI not installed"))
        try:
            ok, detail = refresh.verify_seat(grok_entry(), "grok-4.6",
                                             timeout=5)
        finally:
            refresh.panel.make_seat = original
        self.assertFalse(ok)
        self.assertIn("rc=-2", detail)


class NewLineTests(unittest.TestCase):
    """A brand-new line in a seated family is reported, never auto-wired."""

    def setUp(self):
        refresh.state_path().unlink(missing_ok=True)  # alerts start unseen
        sol = {"provider": "openai", "match": r"^gpt-([0-9.]+)-sol$",
               "format": "{id}"}
        free = {"provider": "opencode", "match": r"^glm-([0-9.]+)-flash-free$",
                "format": "opencode/{id}"}
        free_ds = {"provider": "opencode",
                   "match": r"^deepseek-v([0-9.]+)-flash-free$",
                   "format": "opencode/{id}"}
        self.registry = {
            "codex": {"lineage": "openai", "track": sol,
                      "cmd": {"argv": ["codex", "-m", "gpt-6.1-sol", "{prompt}"]}},
            "glm-free": {"lineage": "zhipu", "track": free,
                         "cmd": {"argv": ["opencode", "-m",
                                          "opencode/glm-5.3-flash-free"]}},
            "ds-free": {"lineage": "deepseek", "track": free_ds,
                        "cmd": {"argv": ["opencode", "-m",
                                         "opencode/deepseek-v4.1-flash-free"]}},
        }
        self.catalog = {
            "openai": {"models": {
                "gpt-6.1-sol": {"release_date": "2026-09-29",
                                "cost": {"output": 10}},
                "gpt-6.1-terra": {"release_date": "2026-10-26",
                                  "cost": {"output": 5}},
                "gpt-6.2-sol": {"release_date": "2026-11-01",
                                "cost": {"output": 10}},
                "gpt-7-nova": {"release_date": "2026-11-02",
                               "cost": {"output": 40}},
                "gpt-7-preview": {"release_date": "2026-11-03"},
                "o9-deep": {"release_date": "2026-11-04",
                            "cost": {"output": 90}},
            }},
            "opencode": {"models": {
                "glm-5.3-flash-free": {"release_date": "2026-08-26",
                                       "cost": {"output": 0}},
                "deepseek-v4.1-flash-free": {"release_date": "2026-10-17",
                                             "cost": {"output": 0}},
                "claude-fable-6": {"release_date": "2026-11-05",
                                   "cost": {"output": 60}},
            }},
        }

    def test_a_newer_line_at_or_above_the_seated_price_is_reported(self):
        found = refresh.find_new_lines(self.registry, self.catalog)
        self.assertEqual([f["id"] for f in found], ["gpt-7-nova"])
        self.assertEqual(found[0]["lineage"], "openai")
        self.assertEqual(found[0]["output_price"], 40)

    def test_cheaper_siblings_tracked_bumps_and_unpriced_ids_are_not(self):
        ids = {f["id"] for f in refresh.find_new_lines(self.registry,
                                                       self.catalog)}
        self.assertNotIn("gpt-6.1-terra", ids)   # cheaper tier
        self.assertNotIn("gpt-6.2-sol", ids)     # the sol track bumps it
        self.assertNotIn("gpt-7-preview", ids)   # no price to compare
        self.assertNotIn("o9-deep", ids)         # another family's name

    def test_resellers_that_mix_every_family_are_skipped(self):
        # opencode is tracked by two lineages here, so its catalog is a
        # reseller's: a Claude model in it is no news about GLM
        ids = {f["id"] for f in refresh.find_new_lines(self.registry,
                                                       self.catalog)}
        self.assertNotIn("claude-fable-6", ids)

    def with_astra(self, **astra_meta):
        """Add a dearer, newer astra pin to the openai lineage."""
        self.registry["astra"] = {
            "lineage": "openai",
            "track": {"provider": "openai", "match": r"^gpt-([0-9.]+)-astra$",
                      "format": "{id}"},
            "cmd": {"argv": ["codex", "-m", "gpt-6-astra", "{prompt}"]}}
        if astra_meta is not None:
            self.catalog["openai"]["models"]["gpt-6-astra"] = astra_meta

    def test_an_uncatalogued_pin_leaves_the_bar_unknown(self):
        # codex review 2026-10-28: a pin the catalog cannot see must not
        # silently drop out of "newer and dearer than every pin"
        self.with_astra()
        self.assertEqual(refresh.find_new_lines(self.registry, self.catalog), [])

    def test_a_pin_without_date_or_price_leaves_the_bar_unknown(self):
        # each pin passes the other test, so only the missing field can stop
        # nova being reported
        for meta in ({"cost": {"output": 5}},
                     {"release_date": "2026-09-01"}):
            with self.subTest(meta=meta):
                self.setUp()
                self.with_astra(**meta)
                self.assertEqual(
                    refresh.find_new_lines(self.registry, self.catalog), [])

    def test_new_lines_are_judged_against_the_pins_apply_just_wrote(self):
        # bumping sol to a dearer, newer release moves the bar past nova
        self.catalog["openai"]["models"]["gpt-6.3-sol"] = {
            "release_date": "2026-12-01", "cost": {"output": 100}}
        with tempfile.TemporaryDirectory() as tmp:
            seats = Path(tmp) / "seats.json"
            seats.write_text(json.dumps({"seats": self.registry}, indent=2))
            catalog = Path(tmp) / "catalog.json"
            catalog.write_text(json.dumps(self.catalog))
            with patch.object(panel, "REGISTRY_PATH", seats), \
                    patch.object(refresh, "verify_seat",
                                 return_value=(True, "OK")), \
                    patch.object(refresh, "notify") as notify, \
                    patch("sys.stdout", new_callable=io.StringIO) as out:
                refresh.main(["--apply", "--notify", "--catalog", str(catalog)])
            self.assertIn("gpt-6.3-sol", seats.read_text())
            self.assertNotIn("NEW LINE", out.getvalue())
            self.assertNotIn("gpt-7-nova", notify.call_args[0][0])

    def test_a_new_line_released_the_same_day_as_a_bump_is_still_reported(self):
        # 2026-11-09: Fable 5.2 and Mythos 5.2 shipped together; bumping the
        # tracked line to that day's release must not hide the new tier
        self.registry["codex"]["cmd"]["argv"][2] = "gpt-6.2-sol"
        self.catalog["openai"]["models"]["gpt-7-nova"]["release_date"] = \
            "2026-11-01"
        ids = [n["id"] for n in refresh.find_new_lines(self.registry,
                                                       self.catalog)]
        self.assertIn("gpt-7-nova", ids)

    def test_a_serving_variant_of_a_pinned_model_is_not_a_new_line(self):
        # kimi-k3.1-pro-highspeed shipped with the pinned kimi-k3.1-pro: the
        # same model served faster, not a tier worth wiring
        self.catalog["openai"]["models"]["gpt-6.1-sol-highspeed"] = {
            "release_date": "2026-09-29", "cost": {"output": 20}}
        ids = [n["id"] for n in refresh.find_new_lines(self.registry,
                                                       self.catalog)]
        self.assertNotIn("gpt-6.1-sol-highspeed", ids)

    def test_main_reports_new_lines_without_editing_seats(self):
        with tempfile.TemporaryDirectory() as tmp:
            seats = Path(tmp) / "seats.json"
            seats.write_text(json.dumps({"seats": self.registry}, indent=2))
            catalog = Path(tmp) / "catalog.json"
            catalog.write_text(json.dumps(self.catalog))
            before = seats.read_text()
            with patch.object(panel, "REGISTRY_PATH", seats), \
                    patch.object(refresh, "verify_seat",
                                 return_value=(True, "OK")), \
                    patch.object(refresh, "notify") as notify, \
                    patch("sys.stdout", new_callable=io.StringIO) as out:
                refresh.main(["--apply", "--notify", "--catalog", str(catalog)])
            self.assertIn("NEW LINE", out.getvalue())
            self.assertIn("gpt-7-nova", notify.call_args[0][0])
            # the sol bump is the only edit; the new line is never wired
            self.assertNotEqual(seats.read_text(), before)
            self.assertNotIn("gpt-7-nova", seats.read_text())


class ScheduledRunTests(unittest.TestCase):
    """What launchd runs: survive a network that is not up yet, say when the
    catalog never came back, and do not repeat the same alert every run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.seats = self.dir / "seats.json"
        self.seats.write_text(json.dumps(
            {"seats": {"grok": grok_entry("grok-4.5")}}, indent=2))
        self.catalog = self.dir / "catalog.json"
        self.catalog.write_text(json.dumps(CATALOG))
        self.env = patch.dict("os.environ", {"PANEL_RUNS": str(self.dir)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_main(self, args, verify=(True, "OK"), delivered=True):
        with patch.object(panel, "REGISTRY_PATH", self.seats), \
                patch.object(refresh, "verify_seat", return_value=verify), \
                patch.object(refresh, "notify",
                             return_value=delivered) as notify, \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            rc = refresh.main(args)
        return rc, [c.args[0] for c in notify.call_args_list]

    def test_catalog_fetch_retries_until_the_network_is_up(self):
        calls = []

        def flaky(source):
            calls.append(source)
            if len(calls) < 3:
                raise OSError("nodename nor servname provided")
            return CATALOG

        with patch.object(refresh, "load_catalog", side_effect=flaky), \
                patch("sys.stderr", new_callable=io.StringIO):
            got = refresh.fetch_catalog("https://models.dev/api.json",
                                        attempts=5, sleep=lambda s: None)
        self.assertEqual(got, CATALOG)
        self.assertEqual(len(calls), 3)

    def test_a_local_catalog_file_is_never_retried(self):
        sleeps = []
        with self.assertRaises(OSError):
            refresh.fetch_catalog(str(self.dir / "missing.json"),
                                  attempts=5, sleep=sleeps.append)
        self.assertEqual(sleeps, [])

    def test_an_unreachable_catalog_notifies_once_not_every_run(self):
        missing = str(self.dir / "missing.json")
        rc, sent = self.run_main(["--apply", "--notify", "--catalog", missing])
        self.assertEqual(rc, 2)
        self.assertEqual(len(sent), 1)
        self.assertIn("catalog unavailable", sent[0])
        rc, sent = self.run_main(["--apply", "--notify", "--catalog", missing])
        self.assertEqual((rc, sent), (2, []))

    def test_a_lagging_lane_alerts_once_until_it_verifies(self):
        args = ["--apply", "--notify", "--catalog", str(self.catalog)]
        _, sent = self.run_main(args, verify=(False, "rc=1: unknown model"))
        self.assertEqual(len(sent), 1)
        self.assertIn("could not verify", sent[0])
        _, sent = self.run_main(args, verify=(False, "rc=1: unknown model"))
        self.assertEqual(sent, [])
        _, sent = self.run_main(args)
        self.assertEqual(len(sent), 1)
        self.assertIn("grok -> grok-4.6", sent[0])
        self.assertIn("grok-4.6", self.seats.read_text())

    def test_an_alert_that_failed_to_deliver_is_sent_again_next_run(self):
        # Codex review 2026-11-09 finding 1: a failed osascript call must not
        # mark the item as announced
        missing = str(self.dir / "missing.json")
        args = ["--notify", "--catalog", missing]
        with patch.object(refresh, "notify", return_value=False) as first, \
                patch("sys.stderr", new_callable=io.StringIO):
            refresh.main(args)
        self.assertEqual(first.call_count, 1)
        with patch.object(refresh, "notify", return_value=True) as second, \
                patch("sys.stderr", new_callable=io.StringIO):
            refresh.main(args)
        self.assertEqual(second.call_count, 1)

    def test_a_scoped_run_keeps_announcements_it_did_not_check(self):
        # Codex review 2026-11-09 finding 2: --only skips the new-line scan,
        # so it must not forget a new line a full run already announced
        with patch.object(refresh, "notify", return_value=True):
            refresh.alert(["new openai model gpt-7-nova"])
        _, sent = self.run_main(["--apply", "--notify", "--only", "grok",
                                 "--catalog", str(self.catalog)])
        self.assertEqual(len(sent), 1)  # the grok bump itself
        state = json.loads(refresh.state_path().read_text())["announced"]
        self.assertIn("new openai model gpt-7-nova", state)

    def test_an_undelivered_bump_alert_is_retried_after_the_seat_is_current(self):
        # Codex re-check 2026-11-09 finding 1: a bump is a one-shot event;
        # the next run sees the seat current and never regenerates it
        args = ["--apply", "--notify", "--catalog", str(self.catalog)]
        _, sent = self.run_main(args, delivered=False)
        self.assertIn("grok-4.6", self.seats.read_text())
        _, sent = self.run_main(args)
        self.assertEqual(len(sent), 1)
        self.assertIn("grok -> grok-4.6", sent[0])
        _, sent = self.run_main(args)
        self.assertEqual(sent, [])

    def test_an_outage_after_a_scoped_recovery_still_alerts(self):
        # Codex re-check 2026-11-09 finding 2: a run that reached the catalog
        # ends the outage, even a scoped one that keeps other items
        missing = ["--notify", "--catalog", str(self.dir / "missing.json")]
        _, sent = self.run_main(missing)
        self.assertEqual(len(sent), 1)
        self.run_main(["--notify", "--only", "grok",
                       "--catalog", str(self.catalog)])
        _, sent = self.run_main(missing)
        self.assertEqual(len(sent), 1)
        self.assertIn("catalog unavailable", sent[0])

    def test_a_pending_condition_delivered_in_an_outage_is_not_repeated(self):
        # Codex re-check 2 finding 1
        good = ["--apply", "--notify", "--catalog", str(self.catalog)]
        missing = ["--notify", "--catalog", str(self.dir / "missing.json")]
        lag = (False, "rc=1: unknown model")
        self.run_main(good, verify=lag, delivered=False)
        _, sent = self.run_main(missing)
        self.assertIn("could not verify", sent[0])
        _, sent = self.run_main(good, verify=lag)
        self.assertEqual(sent, [])

    def test_a_report_only_run_that_reaches_the_catalog_ends_the_outage(self):
        # Codex re-check 2 finding 2: the invariant is "any run that reached
        # the catalog", with or without --notify
        missing = ["--notify", "--catalog", str(self.dir / "missing.json")]
        self.run_main(missing)
        self.run_main(["--catalog", str(self.catalog)])
        _, sent = self.run_main(missing)
        self.assertEqual(len(sent), 1)
        self.assertIn("catalog unavailable", sent[0])

    def test_a_stale_outage_is_not_sent_by_a_run_that_reached_the_catalog(self):
        missing = ["--notify", "--catalog", str(self.dir / "missing.json")]
        self.run_main(missing, delivered=False)
        _, sent = self.run_main(["--apply", "--notify",
                                 "--catalog", str(self.catalog)])
        self.assertEqual(len(sent), 1)
        self.assertNotIn("catalog unavailable", sent[0])

    def test_a_dead_default_lane_is_announced_once(self):
        # audit 2026-11-09 batch 2: panel now skips a lane that died on quota;
        # a human has to top it up, so the daily job says so, once
        registry = {"grok": grok_entry("grok-4.6"),
                    "pplx": {"lineage": "perplexity", "default": True,
                             "http": {"url": "https://x", "body": {}}}}
        self.seats.write_text(json.dumps({"seats": registry}, indent=2))
        now = panel.datetime.datetime.now().isoformat(timespec="seconds")
        (self.dir / "_seat-health.json").write_text(json.dumps(
            {"pplx": {"ok": False, "ts": now, "exit": -3, "kind": "quota",
                      "error": "insufficient_quota"}}))
        args = ["--notify", "--catalog", str(self.catalog)]
        _, sent = self.run_main(args)
        self.assertEqual(len(sent), 1)
        self.assertIn("pplx lane is down", sent[0])
        _, sent = self.run_main(args)
        self.assertEqual(sent, [])

    def test_a_bump_alert_says_the_intelligence_score_needs_a_re_read(self):
        _, sent = self.run_main(["--apply", "--notify",
                                 "--catalog", str(self.catalog)])
        self.assertIn("re-read its intelligence score", sent[0])

    def test_the_shipped_launchd_job_runs_several_times_a_day(self):
        import plistlib
        plist = plistlib.loads(
            (ROOT / "scripts" / "seat-refresh.launchd.plist")
            .read_bytes())
        times = plist["StartCalendarInterval"]
        self.assertIsInstance(times, list)
        self.assertGreaterEqual(len(times), 3)
        self.assertIn("--notify", plist["ProgramArguments"][-1])


TODAY = datetime.date(2026, 11, 10)


def _ago(days, today=TODAY):
    return (today - datetime.timedelta(days=days)).isoformat()


def watch_registry():
    """Four default seats (62 / 50 / ≤32 / ≤43) and one non-default one."""
    return {
        "fable": {"lineage": "anthropic", "default": True,
                  "track": {"provider": "anthropic", "format": "{id}",
                            "match": r"^claude-fable-([0-9]+(-[0-9]+)?)$"},
                  "cmd": {"argv": ["claude", "--model", "claude-fable-5-2",
                                   "-p", "{prompt}"]},
                  "intelligence": {"score": 62, "model": "claude-fable-5-2",
                                   "measured": "Claude Fable 5.2 (xhigh), "
                                               "read 2026-11-09"}},
        "argon": {"lineage": "google", "default": True,
                  "track": {"provider": "google", "format": "google/{id}#high",
                            "match": r"^gemini-([0-9.]+)-argon$"},
                  "cmd": {"argv": ["opencode", "run", "-m",
                                   "google/gemini-4-argon#high", "{prompt}"]},
                  "intelligence": {"score": 50,
                                   "model": "google/gemini-4-argon#high",
                                   "measured": "Gemini 4 Argon (high)"}},
        "glm": {"lineage": "zhipu", "default": True,
                "track": {"provider": "zhipuai", "format": "{id}",
                          "match": r"^glm-([0-9.]+)$"},
                "http": {"url": "https://x", "body": {"model": "glm-5.3"}},
                "intelligence": {"score": 32, "model": "glm-5.3",
                                 "measured": "GLM-5.3 (max)", "ceiling": True}},
        "muse": {"lineage": "meta", "default": True,
                 "track": {"provider": "meta", "format": "{id}",
                           "match": r"^muse-wonder-([0-9.]+)$"},
                 "cmd": {"argv": ["muse", "exec", "--model", "muse-wonder-1.0",
                                  "{prompt}"]},
                 "intelligence": {"score": 43, "model": "muse-wonder-1.0",
                                  "measured": "Muse Wonder (max)",
                                  "ceiling": True}},
        "mimo-free": {"lineage": "xiaomi",
                      "track": {"provider": "opencode",
                                "format": "opencode/{id}",
                                "match": r"^mimo-v([0-9.]+)-flash-free$"},
                      "cmd": {"argv": ["opencode", "run", "-m",
                                       "opencode/mimo-v2.6-flash-free",
                                       "{prompt}"]},
                      "intelligence": {"score": 27,
                                       "model": "opencode/mimo-v2.6-flash-free",
                                       "measured": "MiMo-V2.6-Flash"}},
    }


def watch_catalog(today=TODAY):
    def m(canon, days, name, out=1):
        return {"canonical_model_id": canon, "release_date": _ago(days, today),
                "name": name, "cost": {"output": out}}
    return {
        "anthropic": {"models": {
            "claude-fable-5-2": m("anthropic/claude-fable-5-2", 1,
                                  "Claude Fable 5.2", 40)}},
        "google": {"models": {
            "gemini-4-argon": m("google/gemini-4-argon", 28, "Gemini 4 Argon", 10),
            "gemini-4-carbon": m("google/gemini-4-carbon", 0,
                                 "Gemini 4 Carbon", 10)}},
        "zhipuai": {"models": {"glm-5.3": m("zhipuai/glm-5.3", 84, "GLM-5.3")}},
        "meta": {"models": {
            "muse-wonder-1.0": m("meta/muse-wonder-1.0", 29, "Muse Wonder", 20),
            "muse-ember-1.0": m("meta/muse-ember-1.0", 1, "Muse Ember 1.0")}},
        "opencode": {"models": {
            "claude-fable-5-2": m("anthropic/claude-fable-5-2", 1,
                                  "Claude Fable 5.2"),
            "gemini-4-carbon": m("google/gemini-4-carbon", 0, "Gemini 4 Carbon"),
            "muse-ember-1.0": m("meta/muse-ember-1.0", 1, "Muse Ember 1.0"),
            "iceman-free": m(None, 4, "Iceman Free", 0),
            "crouton-alpha-free": m(None, 7, "Crouton Alpha Free", 0),
            "inkling-2-small-free": m("thinkingmachines/inkling-2-small", 5,
                                      "Inkling 2 Small Free", 0),
            "longcat-2.5-free": m("meituan/longcat-2.5", 8, "LongCat 2.5 Free", 0),
            "mimo-v2.6-flash-free": m("xiaomi/mimo-v2.6-flash", 49,
                                      "MiMo-V2.6-Flash Free", 0),
            "old-free": m(None, 40, "Old Free", 0)}},
        "opencode-go": {"models": {
            "hy4": m("tencent/hy4", 6, "Hy4", 2.5),
            "crouton-alpha-free": m(None, 7, "Crouton Alpha Free", 0)}},
        # not a route panel calls
        "openrouter": {"models": {
            "genius-9": m("secretlab/genius-9", 3, "Genius 9", 50)}},
    }


def aa_row(name, score, creator, slug=None):
    base = refresh.split_effort(name)[0]
    return {"id": f"id-{base}", "name": name, "slug": slug or base,
            "release_date": "2026-11-01",
            "model_creator": {"id": f"c-{refresh._norm(creator)}",
                              "name": creator},
            "evaluations": {"artificial_analysis_intelligence_index": score}}


def aa_rows():
    return [
        aa_row("Claude Fable 5.2 (xhigh)", 62.3, "Anthropic"),
        aa_row("Gemini 4 Argon (high)", 50.1, "Google"),
        aa_row("Gemini 4 Argon (xhigh)", 52.0, "Google"),
        aa_row("Gemini 4 Carbon (high)", 58.2, "Google"),
        aa_row("Gemini 4 Carbon (low)", 40.0, "Google"),
        aa_row("Gemini 4 Carbon (max)", 61.0, "Google"),
        aa_row("GLM-5.3 (Max)", 31.7, "Z AI", slug="glm-5-3"),
        aa_row("Muse Wonder (Max)", 43.4, "Meta"),
        aa_row("Muse Ember 1.0 (max)", 41.0, "Meta"),
        aa_row("LongCat 2.5 (xhigh)", 45.0, "Meituan"),
        aa_row("Hy4 (high)", 30.0, "Tencent"),
        aa_row("Inkling 2 Small", None, "Thinking Machines"),
        aa_row("Genius 9 (xhigh)", 70.0, "SecretLab"),
        aa_row("Old Model (high)", 20.0, "Nobody"),
    ]


LOCAL = {"cursor-agent": ["gemini-4-carbon-high", "gemini-4-carbon-low",
                          "claude-fable-5-2-xhigh"],
         "codex": ["gpt-6.1-sol"],
         "opencode-cli": ["google/gemini-4-carbon", "opencode/iceman-free"]}


class ChallengerWatchTests(unittest.TestCase):
    """Is there a model panel could call today that is smarter than the
    seats it runs? Report-only: a human wires any seat."""

    def watch(self, rows="default", local=None, registry=None, catalog=None):
        return refresh.challenger_watch(
            registry or watch_registry(), catalog or watch_catalog(),
            aa_rows() if rows == "default" else rows,
            LOCAL if local is None else local, today=TODAY)

    def names(self, items):
        return [i["name"] for i in items]

    def test_a_smarter_callable_model_is_a_challenger(self):
        got = self.watch()
        carbon = [c for c in got["challengers"]
                  if c["name"].startswith("Gemini 4 Carbon")]
        self.assertEqual(len(carbon), 1)
        carbon = carbon[0]
        self.assertEqual((carbon["lineage"], carbon["bar_seat"],
                          carbon["bar_score"]), ("google", "argon", 50))
        # every route that lists it, catalog and local CLI alike
        self.assertIn("google/gemini-4-carbon", carbon["routes"])
        self.assertIn("opencode/gemini-4-carbon", carbon["routes"])
        self.assertIn("cursor-agent:gemini-4-carbon-high", carbon["routes"])
        self.assertIn("opencode-cli:google/gemini-4-carbon", carbon["routes"])

    def test_the_challenger_is_scored_at_the_effort_a_seat_would_run(self):
        # seats run xhigh where offered and never max: carbon's max 61 must
        # not count, its high 58.2 does...
        carbon = [c for c in self.watch()["challengers"]
                  if c["name"].startswith("Gemini 4 Carbon")][0]
        self.assertEqual((carbon["name"], carbon["score"], carbon["ceiling"]),
                         ("Gemini 4 Carbon (high)", 58.2, False))
        # ...and an xhigh row wins over high even when it scores lower
        rows = aa_rows() + [aa_row("Gemini 4 Carbon (xhigh)", 57.0, "Google")]
        carbon = [c for c in self.watch(rows)["challengers"]
                  if c["name"].startswith("Gemini 4 Carbon")][0]
        self.assertEqual((carbon["name"], carbon["score"]),
                         ("Gemini 4 Carbon (xhigh)", 57.0))

    def test_a_max_only_score_is_unclear_not_a_challenger(self):
        # Codex review 2026-11-10 #9: no seat runs max, so a score AA read
        # only at max says nothing about the effort a seat would run
        rows = [r for r in aa_rows() if not r["name"].startswith("LongCat")]
        rows.append(aa_row("LongCat 2.5 (max)", 45.0, "Meituan"))
        got = self.watch(rows)
        self.assertNotIn("LongCat 2.5 (max)", self.names(got["challengers"]))
        longcat = [u for u in got["unclear"] if u["name"] == "LongCat 2.5 (max)"]
        self.assertTrue(longcat[0]["ceiling"])
        self.assertIn("only at max", longcat[0]["why"])

    def test_a_lab_panel_does_not_seat_is_held_to_the_weakest_default_seat(self):
        longcat = [c for c in self.watch()["challengers"]
                   if c["name"].startswith("LongCat")][0]
        self.assertIsNone(longcat["lineage"])
        self.assertEqual((longcat["bar_seat"], longcat["bar_score"]),
                         ("glm", 32))
        # Hy4 at 30 is below that bar (and mimo-free, not a default seat,
        # never sets it)
        self.assertNotIn("Hy4 (high)", self.names(self.watch()["challengers"]))

    def test_a_model_no_smarter_than_its_lineage_seat_is_not_a_challenger(self):
        # Muse Ember 41 vs the meta seat's 43
        got = self.watch()
        self.assertFalse([n for n in self.names(got["challengers"])
                          if n.startswith("Muse")])

    def test_seated_models_are_never_challengers(self):
        # Argon xhigh (52) beats the argon seat's 50, but it IS that seat;
        # Fable is seated on anthropic and listed again on opencode
        names = self.names(self.watch()["challengers"])
        self.assertFalse([n for n in names if "Argon" in n or "Fable" in n])

    def test_seat_scores_are_whole_numbers_so_aa_scores_are_rounded(self):
        rows = [r for r in aa_rows() if "Carbon" not in r["name"]]
        rows.append(aa_row("Gemini 4 Carbon (high)", 50.4, "Google"))
        self.assertNotIn("Gemini 4 Carbon (high)",
                         self.names(self.watch(rows)["challengers"]))

    def test_a_smarter_model_with_no_callable_exact_match_is_unmatched(self):
        got = self.watch()
        self.assertEqual(self.names(got["unmatched"]), ["Genius 9 (xhigh)"])
        self.assertNotIn("Genius 9 (xhigh)", self.names(got["challengers"]))
        # a local CLI that lists it makes it callable
        got = self.watch(local={"cursor-agent": ["genius-9-xhigh"]})
        genius = [c for c in got["challengers"]
                  if c["name"] == "Genius 9 (xhigh)"][0]
        self.assertEqual(genius["routes"], ["cursor-agent:genius-9-xhigh"])

    def test_a_near_miss_name_is_never_matched_as_a_guess(self):
        rows = aa_rows() + [aa_row("Gemini 4 Carbon Preview (high)", 66.0,
                                   "Google")]
        got = self.watch(rows)
        self.assertIn("Gemini 4 Carbon Preview (high)",
                      self.names(got["unmatched"]))

    def test_a_name_two_catalog_models_share_is_unmatched(self):
        catalog = watch_catalog()
        catalog["google"]["models"]["twin-1"] = {
            "canonical_model_id": "google/twin-1", "name": "Twin",
            "release_date": _ago(40)}
        catalog["meta"]["models"]["twin-2"] = {
            "canonical_model_id": "meta/twin-2", "name": "Twin",
            "release_date": _ago(40)}
        got = self.watch(aa_rows() + [aa_row("Twin (high)", 80.0, "Who")],
                         catalog=catalog)
        twin = [u for u in got["unmatched"] if u["name"] == "Twin (high)"]
        self.assertEqual(len(twin), 1)
        self.assertIn("ambiguous", twin[0]["why"])
        self.assertNotIn("Twin (high)", self.names(got["challengers"]))

    def test_without_scores_new_opencode_models_are_listed_unscored(self):
        got = self.watch(rows=None)
        self.assertEqual((got["challengers"], got["unmatched"]), ([], []))
        ids = {u["id"] for u in got["unscored"]}
        self.assertEqual(ids, {
            "google/gemini-4-carbon", "meta/muse-ember-1.0", "iceman-free",
            "crouton-alpha-free", "thinkingmachines/inkling-2-small",
            "meituan/longcat-2.5", "tencent/hy4"})
        # one item per model, every route that lists it
        crouton = [u for u in got["unscored"] if u["id"] == "crouton-alpha-free"]
        self.assertEqual(crouton[0]["routes"], ["opencode-go/crouton-alpha-free",
                                                "opencode/crouton-alpha-free"])

    def test_with_scores_only_unjudged_new_models_stay_unscored(self):
        # inkling is listed without a score; the stealth models are not on
        # AA at all. Carbon, Hy4, LongCat and Ember were judged.
        ids = {u["id"] for u in self.watch()["unscored"]}
        self.assertEqual(ids, {"iceman-free", "crouton-alpha-free",
                               "thinkingmachines/inkling-2-small"})

    # -- Codex review 2026-11-10, reproduced before fixing ------------------

    def test_a_lab_stripped_name_only_matches_that_labs_namespace(self):
        # #5: 'Tencent Hy4' -> 'hy4' must not land on google/hy4
        rows = [r for r in aa_rows() if not r["name"].startswith("Hy4")]
        rows.append(aa_row("Tencent Hy4", 60.0, "Tencent", slug="tencent-hy4"))
        catalog = watch_catalog()
        catalog["opencode-go"]["models"]["hy4"]["canonical_model_id"] = "google/hy4"
        got = self.watch(rows, catalog=catalog)
        self.assertNotIn("Tencent Hy4", self.names(got["challengers"]))
        self.assertIn("Tencent Hy4", self.names(got["unmatched"]))

    def test_an_effort_suffixed_slug_is_not_an_alias(self):
        # #6: 'Carbon (High)' with slug carbon-high is the model 'carbon',
        # not a different catalog model called carbon-high
        catalog = watch_catalog()
        catalog["google"]["models"]["carbon-high"] = {
            "canonical_model_id": "google/carbon-high", "name": "Carbon High",
            "release_date": _ago(40)}
        rows = aa_rows() + [aa_row("Carbon (High)", 70.0, "Google",
                                   slug="carbon-high")]
        got = self.watch(rows, catalog=catalog)
        self.assertNotIn("Carbon (High)", self.names(got["challengers"]))

    def test_a_pin_naming_its_provider_resolves_only_there(self):
        # #7: google/gemini-4-argon must not also claim an unrelated
        # opencode model that happens to share the bare id
        catalog = watch_catalog()
        catalog["opencode"]["models"]["gemini-4-argon"] = {
            "canonical_model_id": "other/brand-new", "name": "Brand New",
            "release_date": _ago(40)}
        rows = aa_rows() + [aa_row("Brand New (high)", 70.0, "Other")]
        got = self.watch(rows, catalog=catalog)
        self.assertIn("Brand New (high)", self.names(got["challengers"]))

    def test_every_http_route_of_a_seat_counts_as_seated(self):
        # #8: a seat's fallback model is a model that seat runs
        reg = watch_registry()
        reg["glm"]["http"] = [{"url": "https://x", "body": {"model": "glm-5.3"}},
                              {"url": "https://y", "body": {"model": "genius-9"}}]
        got = self.watch(registry=reg, local={"cursor-agent": ["genius-9-xhigh"]})
        names = self.names(got["challengers"]) + self.names(got["unmatched"])
        self.assertNotIn("Genius 9 (xhigh)", names)

    def test_a_route_that_runs_one_effort_is_scored_at_that_effort(self):
        # #9: the only route is cursor's -low variant; AA's xhigh 70 is not
        # what it would run (low scores 20)
        rows = aa_rows() + [aa_row("Genius 9 (low)", 20.0, "SecretLab")]
        got = self.watch(rows, local={"cursor-agent": ["genius-9-low"]})
        self.assertNotIn("Genius 9 (xhigh)", self.names(got["challengers"]))
        self.assertNotIn("Genius 9 (low)", self.names(got["challengers"]))
        # and with no score at the route's effort at all, it is unclear
        got = self.watch(local={"cursor-agent": ["genius-9-medium"]})
        genius = [u for u in got["unclear"] if u["name"] == "Genius 9 (xhigh)"]
        self.assertIn("effort", genius[0]["why"])

    # -- Codex re-check 2026-11-10 ------------------------------------------

    def test_a_resolved_pin_claims_its_model_not_its_bare_name(self):
        # re-check #2: argon's pin google/twin is Google's Argon; the bare
        # word 'twin' must not also seat opencode's unrelated other/twin
        reg, catalog = watch_registry(), watch_catalog()
        reg["argon"]["cmd"]["argv"][3] = "google/twin#high"
        reg["argon"]["intelligence"]["model"] = "google/twin#high"
        catalog["google"]["models"]["twin"] = {
            "canonical_model_id": "google/gemini-4-argon"}
        catalog["opencode"]["models"]["twin"] = {
            "canonical_model_id": "other/twin", "release_date": _ago(40)}
        got = self.watch(aa_rows() + [aa_row("Twin (high)", 70.0, "Other")],
                         registry=reg, catalog=catalog)
        self.assertIn("Twin (high)", self.names(got["challengers"]))

    def test_a_pin_the_catalog_lags_keeps_its_lineage(self):
        # re-check #3: argon pinned to a model the catalog does not list
        # yet is still the google seat; its score is unknown, so Carbon is
        # unclear, not a challenger against glm
        reg = watch_registry()
        reg["argon"]["cmd"]["argv"][3] = "google/gemini-4.1-argon#high"
        got = self.watch(registry=reg)
        self.assertNotIn("Gemini 4 Carbon (high)", self.names(got["challengers"]))
        self.assertIn("Gemini 4 Carbon (high)", self.names(got["unclear"]))

    def test_with_no_measured_default_seat_nothing_is_judged(self):
        # re-check #4: no bar at all must not make models vanish from the
        # report; they stay unscored and the watch says why
        reg = watch_registry()
        for entry in reg.values():
            if entry.get("default"):
                entry.pop("intelligence", None)
        got = self.watch(registry=reg)
        self.assertEqual((got["challengers"], got["unmatched"]), ([], []))
        self.assertIn("google/gemini-4-carbon", {u["id"] for u in got["unscored"]})
        self.assertIn("no default seat", got["note"])

    def test_a_future_release_date_is_not_new(self):
        # #14: the window is the last NEW_DAYS days, not the future
        catalog = watch_catalog()
        catalog["opencode"]["models"]["future-free"] = {
            "release_date": _ago(-30), "name": "Future Free"}
        ids = {u["id"] for u in self.watch(rows=None, catalog=catalog)["unscored"]}
        self.assertNotIn("future-free", ids)

    def test_a_lab_name_in_front_of_the_aa_name_still_matches(self):
        # live 2026-11-10: AA lists 'Tencent Hy4' (slug tencent-hy4) for
        # the catalog's tencent/hy4
        rows = [r for r in aa_rows() if not r["name"].startswith("Hy4")]
        rows.append(aa_row("Tencent Hy4", 34.6, "Tencent", slug="tencent-hy4"))
        got = self.watch(rows)
        hy4 = [c for c in got["challengers"] if c["name"] == "Tencent Hy4"]
        self.assertEqual(hy4[0]["routes"], ["opencode-go/hy4"])
        self.assertNotIn("tencent/hy4", {u["id"] for u in got["unscored"]})
        # a bare number left after the lab name is no model name
        self.assertEqual(refresh._aa_keys(aa_row("LongCat 2.0", 1, "LongCat")),
                         ({"longcat-2-0"}, set()))

    def test_unscored_says_no_score_matched_not_that_aa_has_none(self):
        # live 2026-11-10: AA scores 'Muse Ember'; the catalog says 'Muse
        # Ember 1.0'. Exact matching leaves it unscored, and the report
        # must not claim AA never scored it
        # (Codex review 2026-11-10 #15: the fixture used to carry the exact
        # catalog name, so this never exercised the mismatch)
        rows = [r for r in aa_rows() if not r["name"].startswith("Muse Ember")]
        rows.append(aa_row("Muse Ember (xhigh)", 34.1, "Meta"))
        with tempfile.TemporaryDirectory() as tmp:
            seats = Path(tmp) / "seats.json"
            seats.write_text(json.dumps({"seats": watch_registry()}))
            catalog = Path(tmp) / "catalog.json"
            catalog.write_text(json.dumps(watch_catalog(datetime.date.today())))
            aa = Path(tmp) / "aa.json"
            aa.write_text(json.dumps({"data": rows}))
            with patch.object(panel, "REGISTRY_PATH", seats), \
                    patch("sys.stdout", new_callable=io.StringIO) as out:
                refresh.main(["--catalog", str(catalog), "--aa", str(aa)])
        line = [ln for ln in out.getvalue().splitlines()
                if ln.startswith("UNSCORED       meta/muse-ember-1.0")][0]
        self.assertIn("no AA score matches it", line)
        self.assertNotIn("not scored by AA yet", out.getvalue())

    def test_a_stale_seat_score_does_not_set_a_bar(self):
        # argon's pin moved since its score was read: the google lineage
        # then has no measured default seat, so carbon meets the weakest
        # Codex review 2026-11-10 #10: the google lineage IS seated, its
        # current score is just unknown; the weakest seat is no stand-in
        reg = watch_registry()
        reg["argon"]["cmd"]["argv"][3] = "google/gemini-4.1-argon#high"
        catalog = watch_catalog()
        catalog["google"]["models"]["gemini-4.1-argon"] = {
            "canonical_model_id": "google/gemini-4.1-argon",
            "name": "Gemini 4.1 Argon", "release_date": _ago(2)}
        got = self.watch(registry=reg, catalog=catalog)
        self.assertNotIn("Gemini 4 Carbon (high)", self.names(got["challengers"]))
        carbon = [u for u in got["unclear"]
                  if u["name"] == "Gemini 4 Carbon (high)"][0]
        self.assertIn("no current score", carbon["why"])


class AAScoresTests(unittest.TestCase):
    """The Artificial Analysis free endpoint: key from a dotenv file, never
    printed; every page; a changed index major version is not compared."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / "env"
        key = patch.object(refresh, "AA_KEY", {"file": str(self.env),
                                               "env_key": "AA_API_KEY"})
        key.start()
        self.addCleanup(key.stop)

    def test_no_key_means_no_request_and_one_note_for_a_human(self):
        self.env.write_text("AA_API_KEY=\n")
        with patch("urllib.request.urlopen",
                   side_effect=AssertionError("no key, no request")):
            rows, note, human = refresh.aa_scores(None, "v5.0")
        self.assertIsNone(rows)
        self.assertTrue(human)
        self.assertIn("AA_API_KEY", note)

    def test_every_page_is_read_with_the_key_in_the_header_only(self):
        self.env.write_text("AA_API_KEY=sekrit-123\n")
        pages = [{"intelligence_index_version": 5, "data": [aa_row("A (high)", 1, "X")],
                  "pagination": {"page": 1, "has_more": True}},
                 {"intelligence_index_version": 5, "data": [aa_row("B (high)", 2, "X")],
                  "pagination": {"page": 2, "has_more": False}}]
        seen = []

        def fake_urlopen(req, timeout):
            seen.append(req)
            return io.BytesIO(json.dumps(pages[len(seen) - 1]).encode())

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rows, note, human = refresh.aa_scores(None, "v5.0")
        self.assertEqual([r["name"] for r in rows], ["A (high)", "B (high)"])
        self.assertIsNone(note)
        self.assertEqual(seen[0].get_header("X-api-key"), "sekrit-123")
        self.assertTrue(seen[1].full_url.endswith("page=2"))
        self.assertTrue(all("sekrit" not in r.full_url for r in seen))

    def test_a_failed_request_never_shows_the_key(self):
        self.env.write_text("AA_API_KEY=sekrit-123\n")
        err = refresh.urllib.error.HTTPError(
            refresh.AA_URL, 429, "Too Many Requests", {"Retry-After": "60"}, None)
        with patch("urllib.request.urlopen", side_effect=err):
            rows, note, human = refresh.aa_scores(None, "v5.0")
        self.assertIsNone(rows)
        self.assertFalse(human)  # transient: try again next run
        self.assertIn("429", note)
        self.assertNotIn("sekrit", note)
        # a rejected key does not fix itself: that one needs a human
        err = refresh.urllib.error.HTTPError(
            refresh.AA_URL, 401, "Unauthorized", {}, None)
        with patch("urllib.request.urlopen", side_effect=err):
            rows, note, human = refresh.aa_scores(None, "v5.0")
        self.assertTrue(human)
        self.assertIn("key", note)

    def pages(self, *bodies):
        seen = []

        def fake_urlopen(req, timeout):
            seen.append(req)
            body = bodies[min(len(seen), len(bodies)) - 1]
            return io.BytesIO(json.dumps(body).encode())
        return fake_urlopen

    def test_a_list_longer_than_the_page_cap_is_not_taken_as_complete(self):
        # Codex review 2026-11-10 #12
        self.env.write_text("AA_API_KEY=k\n")
        endless = {"intelligence_index_version": 5, "data": [],
                   "pagination": {"has_more": True}}
        with patch("urllib.request.urlopen", side_effect=self.pages(endless)):
            rows, note, human = refresh.aa_scores(None, "v5.0")
        self.assertIsNone(rows)
        self.assertFalse(human)

    def test_pages_on_two_index_versions_are_not_compared(self):
        # Codex review 2026-11-10 #13: the last page's version hid page 1's
        self.env.write_text("AA_API_KEY=k\n")
        v5 = {"intelligence_index_version": 5, "data": [aa_row("A", 1, "X")],
              "pagination": {"has_more": True}}
        v6 = {"intelligence_index_version": 6, "data": [aa_row("B", 2, "X")],
              "pagination": {"has_more": False}}
        with patch("urllib.request.urlopen", side_effect=self.pages(v5, v6)):
            rows, _, _ = refresh.aa_scores(None, "v6.0")
        self.assertIsNone(rows)

    def test_a_new_index_major_version_is_not_compared_to_old_seat_scores(self):
        fixture = Path(self.tmp.name) / "aa.json"
        fixture.write_text(json.dumps({"intelligence_index_version": 6,
                                       "data": aa_rows()}))
        rows, note, human = refresh.aa_scores(
            str(fixture), "Artificial Analysis Intelligence Index v5.0")
        self.assertIsNone(rows)
        self.assertTrue(human)
        self.assertIn("v6", note)
        fixture.write_text(json.dumps({"intelligence_index_version": 5.1,
                                       "data": aa_rows()}))
        rows, _, _ = refresh.aa_scores(
            str(fixture), "Artificial Analysis Intelligence Index v5.0")
        self.assertEqual(len(rows), len(aa_rows()))


class LocalRouteTests(unittest.TestCase):
    def test_cursor_and_opencode_lists_parse_to_ids(self):
        cursor = ("Available models\n\nauto - Auto (default)\n"
                  "gemini-4-carbon-high - Gemini 4 Carbon High\n"
                  "grok-4.7-xhigh-fast - Grok 4.7  Extra High Fast​\n")
        self.assertEqual(refresh.parse_model_list(cursor),
                         ["gemini-4-carbon-high", "grok-4.7-xhigh-fast"])
        opencode = "opencode/iceman-free\ngoogle/gemini-4-carbon\n"
        self.assertEqual(refresh.parse_model_list(opencode),
                         ["opencode/iceman-free", "google/gemini-4-carbon"])

    def test_serving_suffixes_come_off_a_local_id(self):
        for mid, want in (("grok-4.7-xhigh-fast", "grok-4-7"),
                          ("claude-fable-5-thinking-xhigh", "claude-fable-5"),
                          ("google/gemini-4-argon#high", "gemini-4-argon"),
                          ("opencode/iceman-free", "iceman-free")):
            self.assertEqual(refresh._local_key(mid), want)

    def test_hidden_codex_models_and_missing_clis_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "models_cache.json"
            cache.write_text(json.dumps({"models": [
                {"slug": "gpt-6.1-sol", "visibility": "list"},
                {"slug": "codex-auto-review", "visibility": "hide"}]}))
            with patch.object(refresh, "CODEX_CACHE", cache), \
                    patch.object(refresh.shutil, "which", return_value=None):
                got = _REAL_LOCAL_ROUTES()
        self.assertEqual(got, {"codex": ["gpt-6.1-sol"]})


class ChallengerMainTests(unittest.TestCase):
    """The scheduled run: a CHALLENGER section, one notification per
    challenger, alert-once, and seats.json never touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.seats = self.dir / "seats.json"
        self.seats.write_text(json.dumps({
            "intelligence": {"index": "Artificial Analysis Intelligence "
                                      "Index v5.0"},
            "seats": watch_registry()}, indent=2))
        self.catalog = self.dir / "catalog.json"
        self.catalog.write_text(json.dumps(
            watch_catalog(datetime.date.today())))
        self.aa = self.dir / "aa.json"
        self.aa.write_text(json.dumps({"intelligence_index_version": 5,
                                       "data": aa_rows()}))
        env = patch.dict("os.environ", {"PANEL_RUNS": str(self.dir)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_main(self, *extra, aa=None, local=LOCAL, delivered=True):
        """aa=False runs against the live URL, i.e. with no key here."""
        aa_args = [] if aa is False else ["--aa", str(aa or self.aa)]
        args = ["--apply", "--notify", "--catalog", str(self.catalog),
                *aa_args, *extra]
        with patch.object(panel, "REGISTRY_PATH", self.seats), \
                patch.object(refresh, "local_routes", return_value=local), \
                patch.object(refresh, "notify",
                             side_effect=delivered if callable(delivered)
                             else lambda m: delivered) as notify, \
                patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch("sys.stderr", new_callable=io.StringIO):
            refresh.main(args)
        return out.getvalue(), [c.args[0] for c in notify.call_args_list]

    # -- Codex review 2026-11-10, reproduced before fixing ------------------

    def test_a_credential_error_does_not_stop_the_refresh_alerts(self):
        # #2: load_key can raise (an unreadable directory); the bump and
        # new-line alerts must still go out
        with patch.object(refresh.panel, "load_key",
                          side_effect=PermissionError("denied")):
            _, sent = self.run_main(aa=False)
        self.assertTrue(any("new google model gemini-4-carbon" in s for s in sent))

    def test_losing_the_key_for_a_run_does_not_re_announce_challengers(self):
        # #3: no key = cannot judge, not "the challenger went away"
        self.run_main()
        self.run_main(aa=False)
        _, sent = self.run_main()
        self.assertFalse([s for s in sent if s.startswith(refresh.CHALLENGER_TAG)])

    def test_stale_comparison_scores_do_not_repeat_challenger_alerts(self):
        original = self.seats.read_text()
        for all_stale in (False, True):
            with self.subTest(all_stale=all_stale):
                self.run_main()
                data = json.loads(original)
                for name, seat in data["seats"].items():
                    if name == "argon" or all_stale:
                        seat.pop("intelligence", None)
                self.seats.write_text(json.dumps(data))
                self.run_main()
                self.seats.write_text(original)
                _, sent = self.run_main()
                self.assertFalse(any("Carbon" in msg for msg in sent))

    def test_a_challenger_alerts_once_however_its_routes_change(self):
        # #4: the alert identity is the model, not the text around it
        self.run_main()
        _, sent = self.run_main(local={})  # cursor-agent did not answer
        self.assertEqual(sent, [])

    def test_a_failed_challenger_alert_is_retried_once_not_twice(self):
        # #4: Carbon fails to deliver, then its routes change before the retry
        fail_carbon = lambda m: "Carbon" not in m  # noqa: E731
        self.run_main(delivered=fail_carbon)
        _, sent = self.run_main(local={})
        self.assertEqual(len([s for s in sent if "Carbon" in s]), 1)

    def test_a_retried_alert_keeps_its_words_when_no_run_regenerates_them(self):
        # Codex re-check #1: a --only run (no watch) retries the pending
        # challenger; it must still say what the challenger scored
        self.run_main(delivered=lambda m: "Carbon" not in m)
        _, sent = self.run_main("--only", "glm")
        carbon = [s for s in sent if "carbon" in s.lower()]
        self.assertEqual(len(carbon), 1)
        self.assertIn("scores AA", carbon[0])

    def test_a_notification_keeps_non_ascii_text_literal(self):
        # #1: AppleScript rejects json's \\u2264 escape (error -2741), so a
        # '≤' or a zero-width space from cursor's names never delivered
        with patch.object(refresh.shutil, "which", return_value="/usr/bin/osascript"), \
                patch.object(refresh.subprocess, "run") as run:
            run.return_value.returncode = 0
            refresh.notify("AA ≤45, Grok 4.7 Fast​")
        script = run.call_args[0][0][2]
        self.assertIn("≤45", script)
        self.assertNotIn("\\u", script)

    def test_the_report_has_a_challenger_section_and_seats_stay_untouched(self):
        before = self.seats.read_text()
        out, _ = self.run_main()
        self.assertIn("CHALLENGER     Gemini 4 Carbon (high)", out)
        self.assertIn("UNMATCHED      Genius 9 (xhigh)", out)
        self.assertIn("UNSCORED       iceman-free", out)
        self.assertIn("Artificial Analysis", out)  # attribution
        self.assertEqual(self.seats.read_text(), before)

    def test_each_challenger_is_its_own_notification_sent_once(self):
        _, sent = self.run_main()
        solo = [s for s in sent if s.startswith(refresh.CHALLENGER_TAG)]
        self.assertEqual(len(solo), 2)  # Carbon and LongCat
        self.assertTrue(all(";" not in s for s in solo))
        self.assertTrue(any("Gemini 4 Carbon (high)" in s for s in solo))
        _, sent = self.run_main()
        self.assertEqual(sent, [])

    def test_an_aa_outage_neither_repeats_nor_forgets_challengers(self):
        self.run_main()
        _, sent = self.run_main(aa=self.dir / "missing.json")
        self.assertEqual(sent, [])  # no unscored flood while AA is down
        state = json.loads(refresh.state_path().read_text())["announced"]
        self.assertTrue(any(a.startswith(refresh.CHALLENGER_TAG)
                            for a in state))
        _, sent = self.run_main()
        self.assertEqual(sent, [])

    def test_without_a_key_unscored_models_and_the_key_note_alert_once(self):
        args = ["--notify", "--catalog", str(self.catalog)]
        with patch.object(panel, "REGISTRY_PATH", self.seats), \
                patch.object(refresh, "local_routes", return_value={}), \
                patch.object(refresh, "notify", return_value=True) as notify, \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            refresh.main(args)
            refresh.main(args)
        self.assertEqual(out.getvalue().count("AA_API_KEY"), 2)  # once a run
        self.assertEqual(notify.call_count, 1)
        self.assertIn("iceman-free", notify.call_args[0][0])
        self.assertIn("AA_API_KEY", notify.call_args[0][0])

    def test_a_scoped_run_skips_the_watch(self):
        with patch.object(refresh, "aa_scores") as scores:
            self.run_main("--only", "glm")
        scores.assert_not_called()

    def test_json_output_carries_the_watch(self):
        args = ["--json", "--catalog", str(self.catalog), "--aa", str(self.aa)]
        with patch.object(panel, "REGISTRY_PATH", self.seats), \
                patch.object(refresh, "local_routes", return_value=LOCAL), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            refresh.main(args)
        watch = json.loads(out.getvalue())["challengers"]
        self.assertEqual(sorted(c["name"] for c in watch["challengers"]),
                         ["Gemini 4 Carbon (high)", "LongCat 2.5 (xhigh)"])


class RealRegistryTests(unittest.TestCase):
    """The track blocks actually shipped in seats.json stay coherent."""

    def test_every_hosted_seat_with_a_versioned_pin_is_tracked(self):
        # an untracked versioned pin silently ages while its family ships
        # new releases; unversioned aliases (auto, kimi-for-coding) follow
        # the provider on their own
        import re as _re
        for name, entry in self.registry.items():
            current = refresh.model_slot(entry)[0]
            if entry.get("local") or not current:
                continue
            if _re.search(r"\d", current):
                self.assertIn("track", entry,
                              f"{name} pins '{current}' but no track "
                              f"follows its family")

    def setUp(self):
        self.registry = panel.load_registry(SEATS_JSON)

    def test_kimi_tracks_the_new_pro_family_without_selecting_highspeed(self):
        catalog = {"moonshotai": {"models": {
            "kimi-k3": {"release_date": "2026-07-16"},
            "kimi-k3.1-pro": {"release_date": "2026-10-23"},
            "kimi-k3.1-pro-highspeed": {"release_date": "2026-10-24"},
        }}}
        row = refresh.plan_refresh(
            {"kimi": self.registry["kimi"]}, catalog)[0]
        self.assertEqual(row["proposed"], "kimi-code/k3.1-pro")

    def test_free_nemotron_tracks_super_vl_without_selecting_lightning(self):
        track = self.registry["opencode-free"]["track"]
        catalog = {"opencode": {"models": {
            "nemotron-3-ultra-free": {"release_date": "2026-06-01"},
            "nemotron-3.5-super-vl-free": {"release_date": "2026-10-15"},
            "nemotron-3.5-lightning-free": {"release_date": "2026-10-24"},
        }}}
        self.assertEqual(refresh.pick_latest(catalog, track)[0],
                         "nemotron-3.5-super-vl-free")

    def test_every_track_compiles_and_its_seat_pins_a_model(self):
        import re as _re
        for name, entry in self.registry.items():
            track = entry.get("track")
            if not track:
                continue
            _re.compile(track["match"])
            self.assertIn("provider", track, name)
            self.assertIn("format", track, name)
            self.assertIsNotNone(refresh.model_slot(entry)[0],
                                 f"{name} is tracked but pins no model")

    def test_grok_scaffolds_track_the_same_family(self):
        grok = self.registry["grok"]["track"]
        cursor = self.registry["cursor-grok"]["track"]
        self.assertEqual((grok["provider"], grok["match"]),
                         (cursor["provider"], cursor["match"]))

    def test_current_pins_are_reachable_from_their_own_format(self):
        # sanity: each tracked seat's pinned string looks like the format's
        # output, so `render(latest)` compares against a comparable shape
        import fnmatch
        import re as _re
        for name, entry in self.registry.items():
            track = entry.get("track")
            if not track:
                continue
            current = refresh.model_slot(entry)[0]
            pattern = _re.sub(r"\{(id|\d+)\}", "*", track["format"])
            self.assertTrue(fnmatch.fnmatchcase(current, pattern),
                            f"{name}: '{current}' does not fit format "
                            f"'{track['format']}'")


if __name__ == "__main__":
    unittest.main()
