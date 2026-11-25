"""Offline claim audits through real panel runs, verdicts, CLI and MCP."""
import contextlib
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "panel"))
from test_panel import TempRunCase, findings_block
import mcp_server
import panel


class VerificationAuditTests(TempRunCase):
    def setUp(self):
        super().setUp()
        self.reg = {s: {"lineage": s, "cmd": {"argv": ["fake", "-m", s + "-v1"]}}
                    for s in ("a", "b")}
        self.patch = patch.dict(panel.REGISTRY, self.reg)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        answer = ("I ran the pager test.\nI inspected the cache.\n" + findings_block([
            {"claim": "off by one in the pager", "file": "p.py", "line": 3,
             "severity": "high"},
            {"claim": "cache never expires", "file": "c.py", "line": 9,
             "severity": "high"}]))
        self.use_seats({s: lambda p, r, t: (0, answer) for s in self.reg})
        with contextlib.redirect_stdout(io.StringIO()):
            panel.main(["--task", "q", "--seats", "a,b", "--no-merge",
                        "--out", str(self.tmp), "--repo", str(self.tmp)])
        self.run = panel._latest_run(self.tmp)
        self.ids = [f["id"] for f in json.loads(
            (self.run / "_result.json").read_text())["findings"]]
        (self.run / "trace.txt").write_text(
            "a pager test: NOT RUN\nb pager test: PASS\ncache source: inspected\n",
            encoding="utf-8")

    def audit(self, seat="a", assessment="contradicted", quote="I ran the pager test."):
        raw = (self.run / (seat + ".md")).read_bytes()
        text = raw.decode("utf-8")
        start = text.index(quote)
        return {"seat": seat, "answer_quote": quote,
                "answer_span": [start, start + len(quote)],
                "answer_sha256": hashlib.sha256(raw).hexdigest(),
                "assessment": assessment, "rationale": "Compare the captured test status.",
                "evidence": {"path": "trace.txt",
                             "quote": ("b pager test: PASS" if seat == "b"
                                       else "a pager test: NOT RUN"),
                             "sha256": hashlib.sha256(
                                 (self.run / "trace.txt").read_bytes()).hexdigest()}}

    def record(self, *audits, fid=None):
        return panel.record_verdict(self.run, fid or self.ids[0], "confirmed",
                                    verification_audits=list(audits))

    def test_shared_finding_truth_does_not_credit_a_seats_verification_claim(self):
        entry = self.record(self.audit())
        stats = panel.track_record()
        self.assertEqual((stats["a"]["confirmed"], stats["b"]["confirmed"]), (1, 1))
        self.assertEqual(stats["a"]["verification"]["contradicted"], 1)
        self.assertEqual(stats["a"]["verification"]["assessed"], 1)
        self.assertEqual(stats["b"]["verification"]["assessed"], 0)
        self.assertIn("1/1 audited contradictions", panel.track_label("a", stats))
        self.assertIn("verification unassessed", panel.track_label("b", stats))
        self.assertEqual(entry["verdict"], "confirmed")
        saved = entry["verification_audits"][0]
        self.assertEqual(saved["model"], "a-v1")
        self.assertEqual(saved["answer_span"], self.audit()["answer_span"])
        self.assertEqual(saved["evidence"], self.audit()["evidence"])
        panel.collect_run(self.run, json.loads((self.run / "run.json").read_text()))
        self.assertIn("1/1 audited contradictions", (self.run / "_summary.md").read_text())
        seats = mcp_server.tool_panel_seats({})["seats"]
        self.assertEqual(next(s for s in seats if s["seat"] == "a")["track"], stats["a"])

    def test_supported_unknown_and_legacy_have_distinct_denominators(self):
        supported = self.audit(assessment="supported", quote="I inspected the cache.")
        supported["evidence"]["quote"] = "cache source: inspected"
        self.record(self.audit(), supported, self.audit("b", "unknown"))
        panel.record_verdict(self.run, self.ids[1], "confirmed")  # legacy, not audited
        stats = panel.track_record()
        self.assertEqual(stats["a"]["verification"]["assessed"], 2)
        self.assertEqual(stats["a"]["verification"]["contradicted"], 1)
        self.assertEqual(stats["b"]["verification"]["assessed"], 0)
        self.assertEqual(stats["b"]["verification"]["unknown"], 1)
        self.assertIn("1/2 audited contradictions", panel.track_label("a", stats))

    def test_latest_assessment_counts_once_and_preserves_other_seats(self):
        a, b = self.audit(), self.audit("b", "supported")
        first = self.record(a, b)
        self.record(a)
        # Reusing the same answer span for another finding is still one claim.
        self.record(a, fid=self.ids[1])
        a["assessment"] = "supported"
        last = self.record(a)
        self.assertEqual(first["verification_audits"][0]["id"],
                         last["verification_audits"][0]["id"])
        panel.record_verdict(self.run, self.ids[0], "rejected", "truth changed only")
        saved = json.loads((self.run / panel.VERDICTS_FILE).read_text())[self.ids[0]]
        self.assertEqual(len(saved["verification_audits"]), 2)
        stats = panel.track_record()
        self.assertEqual(stats["a"]["verification"]["assessed"], 1)
        self.assertEqual(stats["a"]["verification"]["contradicted"], 0)
        self.assertEqual(stats["b"]["verification"]["supported"], 1)

    def test_missing_trace_is_unknown_even_when_contradicted_was_requested(self):
        a = self.audit()
        (self.run / "trace.txt").unlink()
        entry = self.record(a)
        self.assertEqual(entry["verification_audits"][0]["assessment"], "unknown")
        self.assertEqual(panel.track_record()["a"]["verification"]["assessed"], 0)
        a.pop("evidence")
        self.assertEqual(self.record(a)["verification_audits"][0]["assessment"], "unknown")

    def test_invalid_audits_never_write_either_ledger(self):
        a = self.audit()
        bad = [{"seat": "absent"}, {"answer_quote": "not said"},
               {"answer_span": [0, 2]}, {"answer_sha256": "0" * 64},
               {"assessment": "false"}, {"rationale": ""},
               {"evidence": dict(a["evidence"], sha256="0" * 64)},
               {"evidence": dict(a["evidence"], quote="not recorded")},
               {"evidence": dict(a["evidence"], path="../trace.txt")},
               {"evidence": dict(a["evidence"], path=str(self.run / "trace.txt"))}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.record(dict(a, **change))
            self.assertFalse((self.run / panel.VERDICTS_FILE).exists())
            self.assertFalse((self.tmp / "runs" / panel.TRACK_FILE).exists())
        with self.assertRaises(ValueError):
            self.record(a, dict(a, assessment="supported"))

    def test_stale_artifacts_are_not_scored_as_contradictions(self):
        self.record(self.audit())
        (self.run / "trace.txt").write_text("changed trace", encoding="utf-8")
        stats = panel.track_record()["a"]
        self.assertEqual(stats["confirmed"], 1)
        self.assertEqual(stats["verification"]["assessed"], 0)
        self.assertEqual(stats["verification"]["unknown"], 1)
        with self.assertRaises(ValueError):
            self.record(self.audit() | {"answer_sha256": "0" * 64})

    def test_model_bumps_keep_audits_historical_even_when_recorded_after_bump(self):
        panel.REGISTRY["a"] = {"cmd": {"argv": ["fake", "-m", "a-v2"]}}
        entry = self.record(self.audit())
        self.assertEqual(entry["verification_audits"][0]["model"], "a-v1")
        stats = panel.track_record()["a"]
        self.assertEqual(stats["prior"], 1)
        self.assertEqual(stats["verification"]["prior"], 1)
        self.assertEqual(stats["verification"]["assessed"], 0)

    def test_changed_or_deleted_answer_invalidates_only_audit_score(self):
        self.record(self.audit())
        answer = self.run / "a.md"
        answer.write_bytes(answer.read_bytes() + b"\nChanged answer\n")
        self.assertEqual(panel.track_record()["a"]["verification"]["assessed"], 0)
        answer.unlink()
        stats = panel.track_record()["a"]
        self.assertEqual(stats["confirmed"], 1)
        self.assertEqual(stats["verification"]["unknown"], 1)

    def test_absent_model_identity_never_borrows_todays_pin(self):
        meta_path = self.run / "run.json"
        meta = json.loads(meta_path.read_text())
        meta.pop("models")
        meta_path.write_text(json.dumps(meta))
        entry = self.record(self.audit())
        self.assertIsNone(entry["verification_audits"][0]["model"])
        self.assertEqual(panel.track_record()["a"]["verification"]["assessed"], 0)

    def test_seat_must_contribute_to_this_finding_not_just_this_run(self):
        (self.run / "b.md").write_text("# b (exit 0)\n" + findings_block([]))
        with self.assertRaisesRegex(ValueError, "contribute"):
            self.record(self.audit("b", quote="VERDICT: FINDINGS"))

    def test_evidence_symlink_cannot_leave_the_run(self):
        link = self.run / "linked.txt"
        outside = self.tmp / "outside.txt"
        outside.write_bytes((self.run / "trace.txt").read_bytes())
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation unavailable")
        a = self.audit()
        a["evidence"]["path"] = "linked.txt"
        with self.assertRaisesRegex(ValueError, "inside the run"):
            self.record(a)

    def test_concurrent_updates_preserve_both_seats_in_both_ledgers(self):
        from concurrent.futures import ThreadPoolExecutor
        audits = [self.audit(), self.audit("b", "supported")] * 2
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda a: self.record(a), audits))
        saved = json.loads((self.run / panel.VERDICTS_FILE).read_text())[self.ids[0]]
        self.assertEqual({a["seat"] for a in saved["verification_audits"]}, {"a", "b"})
        stats = panel.track_record()
        self.assertEqual(stats["a"]["verification"]["contradicted"], 1)
        self.assertEqual(stats["b"]["verification"]["supported"], 1)

    def test_truth_only_update_to_another_finding_cannot_replay_an_old_audit(self):
        a = self.audit()
        self.record(a)
        self.record(a, fid=self.ids[1])
        self.record(dict(a, assessment="unknown"))
        panel.record_verdict(self.run, self.ids[1], "rejected")
        self.assertEqual(panel.track_record()["a"]["verification"]["assessed"], 0)

    def test_cli_and_mcp_single_and_batch_accept_the_same_audits(self):
        audits_file = self.tmp / "audits.json"
        audits_file.write_text(json.dumps([self.audit()]), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            panel.main(["--verdict", str(self.run), "--finding", self.ids[0],
                        "--mark", "confirmed", "--verification-audits", str(audits_file)])
        for payload in ({"finding": self.ids[0], "verdict": "confirmed",
                         "verification_audits": [self.audit("b", "supported")]},
                        {"verdicts": [{"finding": self.ids[0], "verdict": "confirmed",
                                       "verification_audits": [self.audit()]}]}):
            out = mcp_server.tool_panel_verdict({"run_dir": str(self.run), **payload})
            self.assertEqual(len(out["recorded"][0]["verification_audits"]), 2)
        schema = next(t for t in mcp_server.TOOLS if t["name"] == "panel_verdict")
        props = schema["inputSchema"]["properties"]
        self.assertIn("verification_audits", props)
        self.assertIn("verification_audits", props["verdicts"]["items"]["properties"])
        self.assertEqual(panel.track_record()["a"]["verification"]["assessed"], 1)


if __name__ == "__main__":
    unittest.main()
