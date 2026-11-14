#!/usr/bin/env python3
"""Wire panel into every agent surface on this machine.

One command per machine, idempotent:

    python3 scripts/panel-install.py --check     # report, change nothing
    python3 scripts/panel-install.py             # install / repair everything
    python3 scripts/panel-install.py --project . # drop a .mcp.json in a repo

It registers the MCP server in every config it finds (Claude Code, Codex,
Cursor, VS Code, Windsurf, Zed, Gemini CLI, Qwen CLI, grok CLI, OpenCode,
Claude Desktop) and puts a `panel` launcher on PATH for plain terminals.

Rules it follows so it is safe to re-run:
  * every file is backed up once per run before it is touched
  * an existing `panel` entry is UPDATED in place, never duplicated
  * a surface that is not installed is skipped, not created
  * nothing outside the panel entry is rewritten; JSON keeps its other keys,
    TOML keeps its other blocks
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
SERVER = HOME / "skills" / "panel" / "mcp_server.py"
LAUNCHER = HOME / "bin" / "panel"
WINDOWS = platform.system() == "Windows"
STAMP = time.strftime("%Y%m%d_%H%M%S")

GREEN, RED, DIM, YELLOW, RESET = (
    ("\033[32m", "\033[31m", "\033[2m", "\033[33m", "\033[0m")
    if sys.stdout.isatty() and not WINDOWS else ("", "", "", "", ""))


def python_exe():
    """An absolute, STABLE interpreter path.

    Absolute because GUI apps (Cursor, VS Code, Claude Desktop) do not inherit
    a shell PATH. Stable because sys.executable can be a version-pinned path
    like .../python@3.14/bin/python3.14 that evaporates on the next upgrade
    and silently kills every registration -- so a plain `python3` on PATH
    wins when it works, and sys.executable is only the fallback.
    """
    for cand in (shutil.which("python3"), sys.executable):
        if not cand:
            continue
        try:
            r = subprocess.run([cand, "-c", "import sys;print(sys.version_info[0])"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip() == "3":
                shim = Path.home() / ".pyenv" / "shims" / "python3"
                if "/.pyenv/" in cand and shim.exists():
                    return str(shim)  # versions/3.x.y/... dies on upgrade
                return str(Path(cand).resolve()) if Path(cand).is_symlink() \
                    and "/opt/homebrew/bin/" not in cand else cand
        except Exception:  # noqa: BLE001
            continue
    return "python3"


def app_support():
    if WINDOWS:
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    if platform.system() == "Darwin":
        return Path.home() / "Library/Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


# name, path, style. `style` says which key holds the servers.
def targets():
    sup = app_support()
    return [
        ("cursor", Path.home() / ".cursor/mcp.json", "mcpServers"),
        ("cursor-agent", Path.home() / ".cursor/mcp.json", "mcpServers"),
        ("vscode", sup / "Code/User/mcp.json", "servers"),
        ("vscode-insiders", sup / "Code - Insiders/User/mcp.json", "servers"),
        ("windsurf", Path.home() / ".codeium/windsurf/mcp_config.json",
         "mcpServers"),
        ("gemini-cli", Path.home() / ".gemini/settings.json", "mcpServers"),
        ("qwen-cli", Path.home() / ".qwen/settings.json", "mcpServers"),
        ("claude-desktop", sup / "Claude/claude_desktop_config.json",
         "mcpServers"),
    ]
    # Zed and OpenCode are deliberately absent: both use a server shape this
    # writer does not speak, and guessing a schema is worse than a documented
    # manual step. See USAGE.md.


def entry():
    return {"command": python_exe(), "args": [str(SERVER)]}


def backup(path, done):
    if path in done or not path.exists():
        done.add(path)
        return
    shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{STAMP}"))
    done.add(path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - a broken config is not ours to fix
        return None


def current_json(path, key):
    data = read_json(path)
    if data is None:
        return None
    got = (data.get(key) or {}).get("panel")
    return " ".join(got.get("args", [])) if got else None


def install_json(path, key, backups, dry):
    if not path.exists():
        return "absent", None
    data = read_json(path)
    if data is None:
        return "unreadable", None
    have = (data.get(key) or {}).get("panel")
    want = entry()
    if key == "servers":  # VS Code requires an explicit transport
        want["type"] = "stdio"
    # Merge, never replace: an editor may have added fields of its own
    # (transport, enabled, env) and clobbering them breaks the entry.
    merged = {**(have or {}), **want}
    if merged == have:
        return "ok", str(SERVER)
    if dry:
        return ("update" if have else "add"), str(SERVER)
    backup(path, backups)
    data.setdefault(key, {})["panel"] = merged
    path.write_text(json.dumps(data, indent=2) + "\n")
    return ("updated" if have else "added"), str(SERVER)


CODEX = Path.home() / ".codex/config.toml"


def current_codex():
    if not CODEX.exists():
        return None
    m = re.search(r"^\[mcp_servers\.panel\]\s*$(.*?)(?=^\[|\Z)",
                  CODEX.read_text(), re.S | re.M)
    if not m:
        return None
    args = re.search(r'args\s*=\s*\[(.*?)\]', m.group(1), re.S)
    return args.group(1).strip().strip('"') if args else "?"


def install_codex(backups, dry):
    if not CODEX.exists():
        return "absent", None
    # json.dumps escapes backslashes: a Windows path in a bare TOML basic
    # string reads C:\Users as an invalid \U escape and breaks the file
    # (ensure_ascii=False: a surrogate-pair escape for an emoji is not valid TOML)
    block = (f"[mcp_servers.panel]\n"
             f"command = {json.dumps(python_exe(), ensure_ascii=False)}\n"
             f"args = [{json.dumps(str(SERVER), ensure_ascii=False)}]\n")
    text = CODEX.read_text()
    existing = re.search(r"^\[mcp_servers\.panel\]\s*$(.*?)(?=^\[|\Z)",
                         text, re.S | re.M)
    if existing and existing.group(0).strip() == block.strip():
        return "ok", str(SERVER)
    if dry:
        return ("update" if existing else "add"), str(SERVER)
    backup(CODEX, backups)
    if existing:
        text = text[:existing.start()] + block + text[existing.end():]
    else:
        text = text.rstrip() + "\n\n" + block
    CODEX.write_text(text)
    return ("updated" if existing else "added"), str(SERVER)


def cli_registered(cmd, probe):
    if not shutil.which(cmd):
        return None
    try:
        out = subprocess.run(probe, capture_output=True, text=True,
                             timeout=30).stdout
    except Exception:  # noqa: BLE001
        return None
    return "panel" in out


def install_cli(name, cmd, probe, add, backups, dry):
    del backups
    state = cli_registered(cmd, probe)
    if state is None:
        return "absent", None
    if state:
        return "ok", str(SERVER)
    if dry:
        return "add", str(SERVER)
    try:
        r = subprocess.run(add, capture_output=True, text=True, timeout=60)
        return ("added" if r.returncode == 0
                else f"failed: {(r.stderr or r.stdout).strip()[:60]}"), \
            str(SERVER)
    except Exception as exc:  # noqa: BLE001
        return f"failed: {exc}", None


def install_launcher(dry):
    """`panel` on PATH, so a plain terminal is a first-class surface."""
    bindir = Path.home() / (".local/bin" if not WINDOWS else ".local\\bin")
    target = bindir / ("panel.cmd" if WINDOWS else "panel")
    if not dry:
        bindir.mkdir(parents=True, exist_ok=True)
        if WINDOWS:
            target.write_text(f'@echo off\r\n"{python_exe()}" "{LAUNCHER}" %*\r\n')
        else:
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(LAUNCHER)
            LAUNCHER.chmod(0o755)
    on_path = str(bindir) in os.environ.get("PATH", "").split(os.pathsep)
    return ("ok" if target.exists() else "added"), str(target), on_path


def write_project_mcp(project, dry):
    path = Path(project).expanduser().resolve() / ".mcp.json"
    data = read_json(path) if path.exists() else {}
    if not isinstance(data, dict):
        # unparseable: keep the user's text instead of silently replacing it
        if not dry:
            backup(path, set())
        data = {}
    data.setdefault("mcpServers", {})["panel"] = entry()
    if not dry:
        path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--project", help="also write .mcp.json into this repo")
    args = ap.parse_args()
    dry = args.check
    backups = set()

    if not SERVER.exists():
        sys.exit(f"cannot find {SERVER} -- run this from the panel repo")

    print(f"panel  {HOME}")
    print(f"server {SERVER}")
    print(f"python {python_exe()}\n")

    rows = []
    seen_paths = set()
    for name, path, key in targets():
        if (path, key) in seen_paths:
            continue
        seen_paths.add((path, key))
        before = current_json(path, key) if path.exists() else None
        state, _ = install_json(path, key, backups, dry)
        rows.append((name, state, before, path))

    state, _ = install_codex(backups, dry)
    rows.append(("codex", state, current_codex(), CODEX))

    for name, cmd, probe, add in (
        ("claude-code", "claude", ["claude", "mcp", "list"],
         ["claude", "mcp", "add", "panel", "--", python_exe(), str(SERVER)]),
        ("grok-cli", "grok", ["grok", "mcp", "list"],
         ["grok", "mcp", "add", "panel", "--", python_exe(), str(SERVER)]),
    ):
        state, _ = install_cli(name, cmd, probe, add, backups, dry)
        rows.append((name, state, None, cmd))

    lstate, lpath, on_path = install_launcher(dry)
    rows.append(("terminal (panel)", lstate, None, lpath))

    width = max(len(r[0]) for r in rows)
    for name, state, before, where in rows:
        colour = (DIM if state == "absent" else
                  RED if str(state).startswith(("failed", "unread")) else
                  GREEN if state in ("ok", "added", "updated") else YELLOW)
        stale = ""
        if before and str(SERVER) not in before:
            stale = f"  {YELLOW}(was {before}){RESET}"
        print(f"  {name.ljust(width)}  {colour}{state}{RESET}{stale}"
              f"{'' if state != 'absent' else f'  {DIM}{where}{RESET}'}")

    if args.project:
        path = write_project_mcp(args.project, dry)
        print(f"\n  project config {'would be ' if dry else ''}written: {path}")

    if not on_path:
        bindir = Path(lpath).parent
        print(f"\n{YELLOW}! {bindir} is not on your PATH.{RESET} Add it:")
        print(f'  echo \'export PATH="{bindir}:$PATH"\' >> ~/.zshrc')

    if dry:
        print(f"\n{DIM}--check: nothing was written.{RESET}")
    else:
        print(f"\nBacked up {len(backups)} file(s) as *.bak-{STAMP}")
        print("Restart any running IDE or agent to pick up the change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
