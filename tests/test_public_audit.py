"""Offline regressions for the 2026-11-15 public audit."""

import json
import io
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from test_audit_fixes import _load_script
import panel
import mcp_server


class InstallerBackupTests(unittest.TestCase):
    def test_pnl01_atomic_replacement_closes_temp_before_replace(self):
        inst = _load_script("panel-install")
        original_temp, original_replace = tempfile.NamedTemporaryFile, os.replace
        opened = []
        def temporary(**kwargs):
            handle = original_temp(**kwargs)
            opened.append(handle)
            return handle
        def replace(source, target):
            self.assertTrue(opened[-1].closed, "Windows cannot replace an open temp file")
            original_replace(source, target)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("old")
            with patch.object(inst.tempfile, "NamedTemporaryFile", side_effect=temporary), \
                    patch.object(inst.os, "replace", side_effect=replace):
                inst.write_atomic(path, "new")
            self.assertEqual(path.read_text(), "new")

    def test_pnl01_valid_project_is_backed_up(self):
        inst = _load_script("panel-install")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".mcp.json"
            path.write_text('{"mcpServers": {}}')
            with patch.object(inst, "python_exe", return_value="python3"):
                inst.write_project_mcp(tmp, False)
            self.assertEqual([p.read_text() for p in Path(tmp).glob(
                ".mcp.json.bak-*")], ['{"mcpServers": {}}'])

    def test_pnl01_launcher_and_project_have_collision_safe_backups(self):
        inst = _load_script("panel-install")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "source-panel"
            launcher.write_text("launcher")
            target = root / ".local/bin/panel"
            target.parent.mkdir(parents=True)
            target.write_text("original launcher")
            project = root / ".mcp.json"
            project.write_text('{"mcpServers": {}}')
            with patch.object(Path, "home", return_value=root), \
                    patch.object(inst, "LAUNCHER", launcher), \
                    patch.object(inst, "WINDOWS", False), \
                    patch.object(inst, "python_exe", return_value="python3"):
                inst.install_launcher(False)
                inst.write_project_mcp(root, False)
                self.assertEqual([p.read_text() for p in target.parent.glob(
                    "panel.bak-*")], ["original launcher"])
                self.assertEqual([p.read_text() for p in root.glob(
                    ".mcp.json.bak-*")], ['{"mcpServers": {}}'])
                # A second run in the same second must not overwrite a backup.
                project.write_text('{"mcpServers": {}, "other": 2}')
                inst.write_project_mcp(root, False)
                self.assertEqual(len(list(root.glob(".mcp.json.bak-*"))), 2)

    def test_pnl01_dangling_launcher_symlink_is_backed_up(self):
        inst = _load_script("panel-install")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / ".local/bin/panel"
            target.parent.mkdir(parents=True)
            target.symlink_to(root / "old-missing-launcher")
            launcher = root / "source-panel"
            launcher.write_text("launcher")
            with patch.object(Path, "home", return_value=root), \
                    patch.object(inst, "LAUNCHER", launcher), \
                    patch.object(inst, "WINDOWS", False):
                inst.install_launcher(False)
            backups = list(target.parent.glob("panel.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(os.readlink(backups[0]), str(root / "old-missing-launcher"))


class InstallerMergeTests(unittest.TestCase):
    def test_pnl02_codex_preserves_custom_fields_and_nested_tables(self):
        inst = _load_script("panel-install")
        original = ('model = "x"\n[mcp_servers.panel]\n'
                    'command = "old-python"\nargs = [\n "old-server"\n]\n'
                    'enabled = false\nstartup_timeout_sec = 45\n'
                    '[mcp_servers.panel.env]\nMODE = "test"\n'
                    '[mcp_servers.other]\ncommand = "other"\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(original)
            with patch.object(inst, "CODEX", path), \
                    patch.object(inst, "python_exe", return_value="python3"):
                inst.install_codex(set(), False)
                got = tomllib.loads(path.read_text())
                expected = tomllib.loads(original)
                expected["mcp_servers"]["panel"].update(inst.entry())
                self.assertEqual(got, expected)
                self.assertEqual(inst.install_codex(set(), False)[0], "ok")

    def test_pnl02_project_preserves_custom_fields(self):
        inst = _load_script("panel-install")
        original = {"mcpServers": {"panel": {"command": "old", "args": [],
                    "env": {"MODE": "test", "EXTRA": "value"}, "timeout": 45},
                    "other": {"command": "other"}}, "extra": [1, 2]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".mcp.json"
            path.write_text(json.dumps(original))
            with patch.object(inst, "python_exe", return_value="python3"):
                inst.write_project_mcp(tmp, False)
                original["mcpServers"]["panel"].update(inst.entry())
            self.assertEqual(json.loads(path.read_text()), original)


class InstallerCliTests(unittest.TestCase):
    def install(self, listing, dry=False):
        inst = _load_script("panel-install")
        self.calls = []
        def run(argv, **kwargs):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, listing, "")
        with patch.object(inst.shutil, "which", return_value="/fake/claude"), \
                patch.object(inst.subprocess, "run", side_effect=run), \
                patch.object(inst, "python_exe", return_value="python3"):
            return inst.install_cli("claude-code", "claude", ["claude", "mcp", "list"],
                ["claude", "mcp", "add", "panel", "--", "python3", str(inst.SERVER)],
                set(), dry)

    def test_pnl05_relocated_cli_registration_is_replaced(self):
        self.assertEqual(self.install("panel: python3 /old/server.py - Connected")[0],
                         "updated")
        self.assertEqual([c[2] for c in self.calls], ["list", "remove", "add"])

    def test_pnln1_similar_name_and_path_do_not_count_as_panel(self):
        self.assertEqual(self.install(
            "grafana-panels: python3 /tools/panel/server.py - Connected")[0], "added")
        self.assertEqual([c[2] for c in self.calls], ["list", "add"])

    def test_pnl05_check_reports_stale_without_mutating(self):
        self.assertEqual(self.install("panel: python3 /old/server.py", dry=True)[0],
                         "update")
        self.assertEqual([c[2] for c in self.calls], ["list"])


    def test_pnl05_matching_command_and_args_are_idempotent(self):
        inst = _load_script("panel-install")
        self.assertEqual(self.install(f'panel: python3 "{inst.SERVER}" - Connected')[0],
                         "ok")
        self.assertEqual([c[2] for c in self.calls], ["list"])


class HttpBudgetTests(unittest.TestCase):
    SPEC = {"url": "http://example.invalid", "body": {}, "extract": ["text"]}

    def test_pnl07_fallbacks_share_one_deadline(self):
        calls = []
        def stalled(req, timeout):
            calls.append(timeout)
            time.sleep(timeout)
            raise TimeoutError("timed out")
        start = time.monotonic()
        with patch.object(panel.urllib.request, "urlopen", side_effect=stalled):
            code, text = panel.http_seat([self.SPEC, self.SPEC], "p", "/r", 0.15)
        self.assertEqual(code, -3)
        self.assertLess(time.monotonic() - start, 0.25)
        self.assertEqual(len(calls), 1)

    def test_pnln2_dripping_body_cannot_extend_total_budget(self):
        class Drip:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                # Bytes arrive within each socket timeout, but not the total.
                for _ in range(8):
                    time.sleep(0.04)
                return b'{"text": "late answer"}'
        start = time.monotonic()
        with patch.object(panel.urllib.request, "urlopen", return_value=Drip()):
            code, text = panel.http_seat([self.SPEC], "p", "/r", 0.15)
        elapsed = time.monotonic() - start
        self.assertEqual(code, -3)
        self.assertIn("budget", text)
        self.assertLess(elapsed, 0.25)


class McpGradeTests(unittest.TestCase):
    def test_pnl08_invalid_grade_returns_error_and_stdio_ping_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key.json"
            key.write_text('{"defects": []}')
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "panel_grade", "arguments": {"run_dir": tmp, "key": str(key)}}},
                {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
            server = Path(panel.__file__).with_name("mcp_server.py")
            result = subprocess.run([sys.executable, str(server)],
                input="".join(json.dumps(m) + "\n" for m in messages),
                text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([r["id"] for r in replies], [1, 2, 3])
            self.assertTrue(replies[1]["result"]["isError"])
            self.assertEqual(replies[2]["result"], {})


class McpSeatPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.run.mkdir()
        (self.run / "run.json").write_text(json.dumps({"planned": ["allowed"]}))
        (self.run / "allowed.md").write_text("answer")
        (self.root / "outside.md").write_text("synthetic outside file")

    def test_pnl04_traversal_and_unplanned_names_are_rejected(self):
        (self.run / "unplanned.md").write_text("not a seat")
        for name in ("../outside", "unplanned"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                mcp_server.tool_panel_seat({"run_dir": str(self.run), "seat": name})

    def test_pnl04_symlink_answer_and_manifest_are_rejected(self):
        (self.run / "allowed.md").unlink()
        (self.run / "allowed.md").symlink_to(self.root / "outside.md")
        with self.assertRaises(ValueError):
            mcp_server.tool_panel_seat({"run_dir": str(self.run), "seat": "allowed"})
        (self.run / "allowed.md").unlink()
        (self.run / "allowed.md").write_text("answer")
        (self.run / "run.json").rename(self.root / "manifest.json")
        (self.run / "run.json").symlink_to(self.root / "manifest.json")
        with self.assertRaises(ValueError):
            mcp_server.tool_panel_seat({"run_dir": str(self.run), "seat": "allowed"})

    def test_pnl04_planned_regular_answer_is_still_readable(self):
        result = mcp_server.tool_panel_seat({"run_dir": str(self.run),
                                            "seat": "allowed", "max_chars": 3})
        self.assertEqual(result["text"], "ans")
        self.assertTrue(result["truncated"])


class HttpCredentialTests(unittest.TestCase):
    def test_pnl06_success_fallback_and_streaming_redact_credentials(self):
        first, second = "synthetic-audit-key-A", "synthetic-audit-key-B"
        for mode in ("success", "extract-fallback", "stream"):
            with self.subTest(mode=mode):
                answer = f"echo {first} and {second}"
                if mode == "stream":
                    payload = ("data: " + json.dumps({"choices": [{"delta": {
                        "content": answer}}]}) + "\ndata: [DONE]\n").encode()
                    paths = ["choices[0].message.content"]
                else:
                    payload = json.dumps({"text": answer}).encode()
                    paths = ["text"] if mode == "success" else ["missing", "text"]
                specs = [{"url": "http://example.invalid", "body": {},
                          "key_from": {"env": first}, "extract": ["text"]},
                         {"url": "http://example.invalid", "body": {
                             "stream": mode == "stream"}, "key_from": {"env": second},
                          "extract": paths}]
                with patch.object(panel, "load_key", side_effect=lambda s: (s["env"], None)), \
                        patch.object(panel.urllib.request, "urlopen", side_effect=[
                            RuntimeError("first route unavailable"), io.BytesIO(payload)]):
                    code, text = panel.http_seat(specs, "p", "/r", 5)
                self.assertEqual(code, 0)
                self.assertNotIn(first, text)
                self.assertNotIn(second, text)
                self.assertEqual(text.count("[key]"), 2)
                if mode == "extract-fallback":
                    self.assertIn("fell back", text)


class PythonMinimumTests(unittest.TestCase):
    def test_pnl09_entrypoints_reject_unsupported_python_clearly(self):
        root = Path(panel.__file__).resolve().parents[2]
        paths = ["skills/panel/panel.py", "skills/panel/mcp_server.py",
                 "skills/panel/build.py", "scripts/seat-refresh.py",
                 "scripts/panel-install.py", "scripts/panel-on-tag.py", "bin/panel"]
        for path in paths:
            with self.subTest(path=path):
                script = ("import sys, runpy; sys.version_info = (3, 9, 6); "
                          f"runpy.run_path({str(root / path)!r}, run_name='version_probe')")
                result = subprocess.run([sys.executable, "-c", script],
                                        text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Python 3.11", result.stderr)

    def test_pnl09_installer_does_not_select_python39_on_path(self):
        inst = _load_script("panel-install")
        def probe(argv, **kwargs):
            output = "3" if "version_info[0]" in argv[-1] else str(argv[0] == "/new-python")
            return subprocess.CompletedProcess(argv, 0, output, "")
        with patch.object(inst.shutil, "which", return_value="/old-python"), \
                patch.object(inst.sys, "executable", "/new-python"), \
                patch.object(inst.subprocess, "run", side_effect=probe):
            self.assertEqual(inst.python_exe(), "/new-python")


class McpAvailabilityTests(unittest.TestCase):
    def test_pnl12_reports_configuration_and_missing_reasons_like_cli(self):
        registry = {
            "missing-cli": {"cmd": {"argv": ["unavailable"]}},
            "ready-cli": {"cmd": {"argv": ["present"]}},
            "missing-env": {"cmd": {"argv": ["present"],
                            "env_from": {"KEY": {"env": "missing"}}}},
            "missing-http": {"http": {"key_from": {"env": "missing"}}},
            "ready-http": {"http": {"key_from": {"env": "ready"}}},
            "keyless-http": {"http": {}},
            "unconfigured": {}}
        def key(source):
            if not source:
                return None, None
            return ("synthetic-value", None) if source["env"] == "ready" else (
                None, "$missing is not set")
        with patch.object(panel, "REGISTRY", registry), \
                patch.object(panel, "seat_health", return_value={}), \
                patch.object(panel, "track_record", return_value={}), \
                patch.object(panel, "load_key", side_effect=key), \
                patch.object(panel.shutil, "which", side_effect=lambda x: "/fake/present" if x == "present" else None):
            rows = {r["seat"]: r for r in mcp_server.tool_panel_seats({})["seats"]}
            output = io.StringIO()
            with patch("sys.stdout", output):
                panel.list_seats(registry)
        for name in registry:
            with self.subTest(name=name):
                ready = name.startswith(("ready", "keyless"))
                self.assertEqual(rows[name]["configured"], ready)
                self.assertEqual(rows[name]["availability"], "configured" if ready else "missing")
                if not ready:
                    self.assertTrue(rows[name]["reason"])
                    self.assertIn(rows[name]["reason"], output.getvalue())
