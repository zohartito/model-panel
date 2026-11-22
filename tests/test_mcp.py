#!/usr/bin/env python3
"""Tests for the MCP server. Stdlib only, no network, no model calls.

The end-to-end tests use a real subprocess panel run whose only seat is a
`printf` command -- a genuine cmd seat, just one that answers instantly. That
exercises the whole path an MCP client takes: launch detached, poll, summarize,
drill into one seat, resume.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "skills" / "panel"
sys.path.insert(0, str(HERE))
import mcp_server  # noqa: E402
import panel  # noqa: E402

SEAT_ANSWER = ("The panel is a redundancy device.\n"
               "VERDICT: CLEAN\n\nFINDINGS_JSON\n"
               '[{"file": "a.py", "line": 3, "claim": "the retry loop never '
               'backs off", "severity": "high"}]\n'
               "END_FINDINGS_JSON\n")


def rpc(method, params=None, mid=1):
    return mcp_server.handle({"jsonrpc": "2.0", "id": mid, "method": method,
                              "params": params or {}})


def payload(resp):
    return json.loads(resp["result"]["content"][0]["text"])


class ProtocolTests(unittest.TestCase):
    def test_initialize_echoes_the_client_protocol_version(self):
        res = rpc("initialize", {"protocolVersion": "2024-11-05"})["result"]
        self.assertEqual(res["protocolVersion"], "2024-11-05")
        self.assertEqual(res["serverInfo"]["name"], "panel")
        self.assertIn("tools", res["capabilities"])

    def test_initialize_without_a_version_falls_back_to_ours(self):
        res = rpc("initialize", {})["result"]
        self.assertEqual(res["protocolVersion"], mcp_server.DEFAULT_PROTOCOL)

    def test_notifications_get_no_response(self):
        self.assertIsNone(mcp_server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_ping(self):
        self.assertEqual(rpc("ping")["result"], {})

    def test_tools_are_well_formed(self):
        tools = rpc("tools/list")["result"]["tools"]
        self.assertEqual(len(tools), len(mcp_server.HANDLERS))
        for t in tools:
            self.assertTrue(t["name"] and t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIn(t["name"], mcp_server.HANDLERS)

    def test_unknown_method_is_an_error_not_a_crash(self):
        self.assertEqual(rpc("nope/nope")["error"]["code"], -32601)

    def test_unknown_tool_is_an_error(self):
        resp = rpc("tools/call", {"name": "panel_nope", "arguments": {}})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_tool_failure_is_reported_as_isError_not_raised(self):
        resp = rpc("tools/call", {"name": "panel_summary",
                                  "arguments": {"run_dir": "/nope/nowhere"}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("error", payload(resp))

    def test_panel_run_rejects_both_task_and_diff(self):
        resp = rpc("tools/call", {"name": "panel_run",
                                  "arguments": {"task": "x", "diff": "y"}})
        self.assertTrue(resp["result"]["isError"])

    def test_panel_run_schema_offers_preset(self):
        # a Cursor/Codex caller saying "full panel" must not have to paste
        # 17 seat names
        run = next(t for t in mcp_server.TOOLS if t["name"] == "panel_run")
        self.assertIn("preset", run["inputSchema"]["properties"])

    def test_verdict_schema_offers_unresolved_for_single_and_batch(self):
        schema = next(t for t in rpc("tools/list")["result"]["tools"]
                      if t["name"] == "panel_verdict")["inputSchema"]
        props = schema["properties"]
        for fields in (props, props["verdicts"]["items"]["properties"]):
            self.assertEqual(fields["verdict"]["enum"],
                             ["confirmed", "rejected", "unresolved"])
            self.assertIn("required", fields["note"]["description"].lower())
            self.assertIn("unresolved", fields["note"]["description"])

    def test_panel_run_rejects_preset_plus_seats(self):
        resp = rpc("tools/call", {"name": "panel_run",
                                  "arguments": {"task": "x", "preset": "full",
                                                "seats": ["codex"]}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("mutually exclusive",
                      resp["result"]["content"][0]["text"])

    def test_seats_tool_reports_lineages(self):
        data = payload(rpc("tools/call", {"name": "panel_seats",
                                          "arguments": {}}))
        self.assertEqual(len(data["seats"]), len(panel.REGISTRY))
        by_name = {s["seat"]: s for s in data["seats"]}
        self.assertEqual(by_name["claude-opus"]["lineage"], "anthropic")
        self.assertEqual(by_name["cursor-grok"]["lineage"], "xai")
        self.assertEqual(by_name["claude-opus"]["intelligence"]["score"],
                         panel.intelligence_of("claude-opus"))
        self.assertIn("Artificial Analysis", data["note"])


class EndToEndTests(unittest.TestCase):
    """A real detached run, with a seat that is a shell command answering
    instantly. No network, no models, but every moving part is real."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        seats = self.tmp / "seats.json"
        seats.write_text(json.dumps({"seats": {
            "echo": {"lineage": "test", "default": True,
                     "cmd": {"argv": ["printf", "%s", SEAT_ANSWER]}},
            "broken": {"lineage": "test2", "default": True,
                       "cmd": {"argv": ["definitely-not-a-real-cli-xyz"]}},
        }, "presets": {"solo": ["echo"]}}))
        self._env = dict(os.environ)
        os.environ["PANEL_SEATS"] = str(seats)
        os.environ["PANEL_RUNS"] = str(self.tmp / "runs")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def call(self, name, args):
        resp = rpc("tools/call", {"name": name, "arguments": args})
        self.assertFalse(resp["result"].get("isError"),
                         resp["result"]["content"][0]["text"])
        return payload(resp)

    def wait_for(self, run_dir, seat, limit=30):
        deadline = time.time() + limit
        while time.time() < deadline:
            if (Path(run_dir) / f"{seat}.md").exists():
                return True
            time.sleep(0.1)
        return False

    def test_full_cycle_run_status_summary_seat_resume(self):
        started = self.call("panel_run", {
            "task": "Is the retry loop safe?",
            "context": "smoke", "repo": str(self.tmp)})
        run_dir = started["run_dir"]
        self.assertTrue(run_dir, started)
        self.assertEqual(sorted(started["planned_seats"]), ["broken", "echo"])
        self.assertTrue(self.wait_for(run_dir, "echo"))
        self.assertTrue(self.wait_for(run_dir, "broken"))

        status = self.call("panel_status", {"run_dir": run_dir})
        self.assertIn("echo", status["answered"])
        self.assertNotIn("broken", status["answered"],
                         "a seat that errored answered nothing")
        self.assertIn("broken", status["failed"],
                      "a missing CLI is a failure to fix, not a wait")
        self.assertEqual(status["still_running"], [])
        self.assertTrue(status["complete"])
        self.assertIn("contributed nothing", status["warning_failed"])
        # the failed seat's lineage must not be counted (the server process
        # resolves lineage from its own registry, so an unknown seat is its
        # own lineage -- the point is that only `echo` is counted at all)
        self.assertEqual(len(status["answering_lineages"]), 1)
        # DESIGN RULE: status stays compact -- counts and paths, never the
        # findings table itself (a real poll returned 65KB of clusters and
        # ate the orchestrator's context). The table lives in _summary.md
        # and _result.json.
        self.assertNotIn("findings", status)
        self.assertEqual(status["findings_count"], 1)
        self.assertEqual(status["findings_by_severity"], {"high": 1})
        self.assertTrue(status["summary"].endswith("_summary.md"))
        self.assertTrue(status["result_json"].endswith("_result.json"))
        for err in status["failed"].values():
            self.assertLessEqual(len(err["error"]), 200)

        summary = self.call("panel_summary", {"run_dir": run_dir})["summary"]
        self.assertIn("retry loop never backs off", summary)
        self.assertIn("| 1 | high | a.py:3", summary)

        seat = self.call("panel_seat", {"run_dir": run_dir, "seat": "echo"})
        self.assertIn("redundancy device", seat["text"])
        self.assertFalse(seat["truncated"])

        clipped = self.call("panel_seat", {"run_dir": run_dir,
                                           "seat": "echo", "max_chars": 10})
        self.assertTrue(clipped["truncated"])
        self.assertEqual(len(clipped["text"]), 10)

        before = (Path(run_dir) / "echo.md").read_text()
        self.call("panel_resume", {"run_dir": run_dir, "repo": str(self.tmp)})
        self.assertEqual((Path(run_dir) / "echo.md").read_text(), before,
                         "resume must not re-pay a seat that already landed")

    def test_result_json_is_written_for_non_python_orchestrators(self):
        started = self.call("panel_run", {"task": "q", "repo": str(self.tmp),
                                          "seats": ["echo"]})
        run_dir = Path(started["run_dir"])
        self.assertTrue(self.wait_for(run_dir, "echo"))
        self.call("panel_status", {"run_dir": str(run_dir)})
        result = json.loads((run_dir / "_result.json").read_text())
        self.assertEqual(result["run"]["planned"], ["echo"])
        self.assertEqual(result["findings"][0]["file"], "a.py")
        self.assertIn("echo", result["seats"])

    def test_preset_is_wired_through_to_the_run(self):
        started = self.call("panel_run", {"task": "q", "repo": str(self.tmp),
                                          "preset": "solo"})
        self.assertEqual(started["planned_seats"], ["echo"])
        run_dir = started["run_dir"]
        self.assertTrue(self.wait_for(run_dir, "echo"))
        status = self.call("panel_status", {"run_dir": run_dir})
        self.assertTrue(status["complete"])
        self.assertEqual(status["answered"], ["echo"])

    def test_grade_scores_the_run(self):
        started = self.call("panel_run", {"task": "q", "repo": str(self.tmp),
                                          "seats": ["echo"]})
        run_dir = started["run_dir"]
        self.assertTrue(self.wait_for(run_dir, "echo"))
        key = self.tmp / "key.json"
        key.write_text(json.dumps({"defects": [
            {"id": "d1", "file": "a.py", "keywords": ["backs", "retry"],
             "desc": "the retry loop never backs off"}]}))
        out = self.call("panel_grade", {"run_dir": run_dir, "key": str(key)})
        self.assertEqual(out["scores"][0]["found"], 1)


if __name__ == "__main__":
    unittest.main()
