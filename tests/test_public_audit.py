"""Offline regressions for the 2026-11-15 public audit."""

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from test_audit_fixes import _load_script


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
