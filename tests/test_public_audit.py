"""Offline regressions for the 2026-11-15 public audit."""

import json
import os
import subprocess
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from test_audit_fixes import _load_script
import panel


class InstallerBackupTests(unittest.TestCase):
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
