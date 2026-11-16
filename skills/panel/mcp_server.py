#!/usr/bin/env python3
"""MCP server for the panel -- the doorway for any agent that speaks MCP.

Stdio JSON-RPC 2.0, newline-delimited, stdlib only. No SDK, no dependencies,
so it runs anywhere python3 does: Codex CLI, Cursor, Windsurf, Gemini CLI,
VS Code, Claude Code, or anything else with an MCP client.

DESIGN RULE: tools return PATHS AND COMPACT SUMMARIES, never transcripts.
A twelve-seat run is hundreds of KB of frontier reasoning; dumping that into a
tool result would eat the calling agent's context, which is the whole thing
the panel's file-per-seat layout exists to avoid. `panel_seat` is the one tool
that returns raw text, and only for the single seat you name.

Long runs are the normal case (10-40 minutes), so `panel_run` launches
detached and returns the run directory immediately. Poll `panel_status`.

    python3 mcp_server.py            # speaks MCP on stdin/stdout
"""

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel  # noqa: E402

PANEL_PY = Path(__file__).resolve().with_name("panel.py")
SERVER_VERSION = "0.4.0"
DEFAULT_PROTOCOL = "2025-06-18"


def log(msg):
    """stderr only -- stdout is the protocol channel and must stay clean."""
    print(f"[panel-mcp] {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------- schemas

def _str(desc):
    return {"type": "string", "description": desc}


TOOLS = [
    {
        "name": "panel_run",
        "description": (
            "Fan a question or a code diff out to every wired frontier model "
            "in parallel. Launches DETACHED and returns the run directory "
            "immediately -- seats take 10-40 minutes. Poll panel_status, then "
            "read panel_summary. Use for high-stakes reviews and decisions "
            "that are expensive to get wrong; not for questions you can "
            "verify yourself in a minute."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _str("The question, stated so a stranger could answer "
                             "it. Give this OR diff, not both."),
                "diff": _str("Path to a diff file for code-review mode."),
                "context": _str("Background the models cannot infer, "
                                "including anything already fixed."),
                "criteria": _str("What a good answer must cover, or the "
                                 "numbered invariants to verify."),
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "Extra files to attach as context."},
                "preset": _str("Named roster from seats.json (full | "
                               "providers | trio | strong | quick | free). "
                               "quick = six fast lineages, minutes not "
                               "tens of minutes. Mutually exclusive with "
                               "seats."),
                "seats": {"type": "array", "items": {"type": "string"},
                          "description": "Run ONLY these seats. Mutually "
                                         "exclusive with preset."},
                "skip": {"type": "array", "items": {"type": "string"},
                         "description": "Run everything except these seats."},
                "include_local": {"type": "boolean",
                                  "description": "Also seat self-hosted "
                                                 "models (tripwires, not "
                                                 "votes)."},
                "include_dead": {"type": "boolean",
                                 "description": "Also seat lanes whose last "
                                                "call failed on quota or "
                                                "login in the past 24h "
                                                "(skipped by default)."},
                "escalate": {"type": "boolean",
                             "description": "Ask the quick preset first and "
                                            "seat the default roster only "
                                            "when its answers leave doubt "
                                            "(a one-lineage high finding, "
                                            "under 4 lineages, a split "
                                            "verdict). Not with preset or "
                                            "seats."},
                "no_repair": {"type": "boolean",
                              "description": "Do not ask a seat that "
                                             "answered without a findings "
                                             "block for it in a follow-up "
                                             "call."},
                "repo": _str("Repo the review is about; also where "
                             ".panel/residuals.md is read from."),
                "round": {"type": "integer",
                          "description": "Round number. >1 injects the "
                                         "previous round's findings so this "
                                         "round narrows instead of repeating."},
                "timeout": {"type": "integer",
                            "description": "Per-seat timeout in seconds "
                                           "(default 2400)."},
                "wait": {"type": "boolean",
                         "description": "Block until every seat lands. Only "
                                        "for tiny runs -- default false."},
            },
        },
    },
    {
        "name": "panel_status",
        "description": ("Which seats have landed, which are still out, and "
                        "compact finding counts. Safe to poll. Never returns "
                        "the findings themselves -- read panel_summary for "
                        "the table, _result.json for machine-readable "
                        "findings."),
        "inputSchema": {
            "type": "object",
            "properties": {"run_dir": _str("Run directory from panel_run.")},
            "required": ["run_dir"],
        },
    },
    {
        "name": "panel_summary",
        "description": ("The run's _summary.md: the consensus table (one row "
                        "per finding, counted by INDEPENDENT lineage) plus "
                        "every seat's bottom line. Read this before any seat "
                        "file."),
        "inputSchema": {
            "type": "object",
            "properties": {"run_dir": _str("Run directory.")},
            "required": ["run_dir"],
        },
    },
    {
        "name": "panel_seat",
        "description": ("One seat's full answer. The only tool that returns "
                        "raw model output -- name a specific seat after the "
                        "summary points you at it, rather than reading them "
                        "all."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_dir": _str("Run directory."),
                "seat": _str("Seat name, e.g. codex or kimi."),
                "max_chars": {"type": "integer",
                              "description": "Truncate after this many "
                                             "characters (default 20000)."},
            },
            "required": ["run_dir", "seat"],
        },
    },
    {
        "name": "panel_resume",
        "description": ("Refill only the seats that are missing or failed, "
                        "reusing the original prompt. A seat that already "
                        "answered is never re-paid for."),
        "inputSchema": {
            "type": "object",
            "properties": {"run_dir": _str("Run directory, or 'last'."),
                           "repo": _str("Repo to point seats at. Pass this to "
                                        "CHANGE the directory when a seat "
                                        "failed because of it (codex refuses "
                                        "a non-git directory); it overrides "
                                        "what the run recorded.")},
            "required": ["run_dir"],
        },
    },
    {
        "name": "panel_grade",
        "description": ("Score a finished run against a known answer key, so "
                        "seat selection is evidence instead of memory."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_dir": _str("Run directory."),
                "key": _str("Path to an answer-key JSON: "
                            '{"defects": [{"id", "file", "keywords", "desc"}]}'),
            },
            "required": ["run_dir", "key"],
        },
    },
    {
        "name": "panel_verdict",
        "description": ("Record whether a finding held up when you checked it "
                        "yourself: confirmed (reproduced) or rejected "
                        "(ruled out). Call this after reproducing, per hard "
                        "rule 3. It builds each seat's measured track record "
                        "on real work, which summaries and panel_seats show."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_dir": _str("Run directory."),
                "finding": _str("Finding id from the summary's first column, "
                                "e.g. 3fa2c91b."),
                "verdict": {"type": "string",
                            "enum": ["confirmed", "rejected"]},
                "note": _str("Optional: how you checked it."),
                "verdicts": {"type": "array",
                             "description": "Several at once instead of "
                                            "finding/verdict.",
                             "items": {"type": "object", "properties": {
                                 "finding": {"type": "string"},
                                 "verdict": {"type": "string",
                                             "enum": ["confirmed",
                                                      "rejected"]},
                                 "note": {"type": "string"}}}},
            },
            "required": ["run_dir"],
        },
    },
    {
        "name": "panel_seats",
        "description": ("The roster: every seat, its lineage, whether it is "
                        "seated by default, its graded track record, and "
                        "whether its CLI or credentials actually resolve on "
                        "this machine."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# --------------------------------------------------------------------- tools

def _flag_args(a):
    argv = []
    if a.get("task"):
        argv += ["--task", a["task"]]
    if a.get("diff"):
        argv += ["--diff", a["diff"]]
    for key in ("context", "criteria"):
        if a.get(key):
            argv += [f"--{key}", a[key]]
    for f in a.get("files") or []:
        argv += ["--file", f]
    if a.get("preset"):
        argv += ["--preset", a["preset"]]
    if a.get("seats"):
        argv += ["--seats", ",".join(a["seats"])]
    if a.get("skip"):
        argv += ["--skip", ",".join(a["skip"])]
    if a.get("include_local"):
        argv += ["--include-local"]
    if a.get("include_dead"):
        argv += ["--include-dead"]
    if a.get("no_repair"):
        argv += ["--no-repair"]
    if a.get("escalate"):
        argv += ["--escalate"]
    if a.get("repo"):
        argv += ["--repo", a["repo"]]
    if a.get("round"):
        argv += ["--round", str(a["round"])]
    if a.get("timeout"):
        argv += ["--timeout", str(a["timeout"])]
    return argv


def _launch(argv, wait, log_name):
    """Start panel.py detached; return (run_dir, log_path, returncode|None).

    panel.py prints `run dir: ...` before any seat runs, so the caller gets a
    handle to poll within a second even though the run itself takes tens of
    minutes.
    """
    logdir = Path(os.environ.get("PANEL_RUNS") or Path.home() / "panel-runs")
    logdir.mkdir(parents=True, exist_ok=True)
    log_path = logdir / log_name
    with open(log_path, "w") as fh:
        proc = subprocess.Popen([sys.executable, str(PANEL_PY), *argv],
                                stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True)
    if wait:
        proc.wait()
    run_dir, deadline = None, time.time() + 30
    while time.time() < deadline:
        text = log_path.read_text(errors="replace")
        m = re.search(r"^run dir: (.+)$", text, re.M)
        if m:
            run_dir = m.group(1).strip()
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    return run_dir, log_path, proc.poll()


def tool_panel_run(a):
    if bool(a.get("task")) == bool(a.get("diff")):
        raise ValueError("give exactly one of task or diff")
    if a.get("preset") and a.get("seats"):
        raise ValueError("preset and seats are mutually exclusive")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir, log_path, rc = _launch(_flag_args(a), a.get("wait"),
                                    f"mcp-{stamp}.log")
    if run_dir is None:
        return {"error": "panel did not start",
                "log": str(log_path),
                "tail": log_path.read_text(errors="replace")[-2000:]}
    meta_path = Path(run_dir) / panel.RUN_META
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return {
        "run_dir": run_dir,
        "log": str(log_path),
        "planned_seats": meta.get("planned", []),
        "finished": rc is not None,
        "next": ("Seats are answering now. Poll panel_status, then read "
                 "panel_summary. Do not read every seat file: use "
                 "panel_seat for the ones the summary points you at."),
    }


def _status(run_dir):
    run_dir = Path(run_dir)
    meta_path = run_dir / panel.RUN_META
    if not meta_path.exists():
        raise ValueError(f"no panel run at {run_dir}")
    meta = json.loads(meta_path.read_text())
    result = panel.collect_run(run_dir, meta, panel.REGISTRY)
    return meta, result


def tool_panel_status(a):
    meta, result = _status(a["run_dir"])
    # A seat that exited non-zero answered NOTHING. Listing it as answered --
    # and counting its lineage -- overstates the panel's independence, which
    # is the one number a caller must not get wrong.
    answered = [n for n, r in result["seats"].items() if r["exit"] == 0]
    lineages = sorted({result["seats"][n]["lineage"] for n in answered})
    # DESIGN RULE upheld: counts and paths only. A real post-completion poll
    # once returned the whole clustered findings table -- 65KB into the
    # orchestrator's context -- for a question one panel_summary call answers.
    severities = {}
    for f in result["findings"]:
        severities[f["severity"]] = severities.get(f["severity"], 0) + 1
    out = {
        "run_dir": result["dir"],
        "question": meta.get("question"),
        "round": meta.get("round", 1),
        "complete": result["complete"],
        "answered": answered,
        "failed": result["failed"],
        "still_running": [n for n in result["pending"]
                          if n not in result["failed"]],
        "no_vote": result["no_vote"],
        "answering_lineages": lineages,
        "findings_count": len(result["findings"]),
        "findings_by_severity": severities,
        "summary": str(Path(result["dir"]) / "_summary.md"),
        "result_json": str(Path(result["dir"]) / "_result.json"),
    }
    if not result["complete"]:
        out["warning"] = (
            f"INCOMPLETE: {len(out['still_running'])} seat(s) have not "
            f"reported. Do not synthesize yet. _summary.md is rebuilt on "
            f"every status check, so its existence is not a completion "
            f"signal -- use this `complete` field.")
    if result["failed"]:
        out["warning_failed"] = (
            f"{len(result['failed'])} seat(s) contributed nothing: "
            f"{', '.join(result['failed'])}. Fix the cause and use "
            f"panel_resume (pass `repo` to change the directory a seat is "
            f"pointed at), or accept a smaller panel and say so.")
    return out


def tool_panel_summary(a):
    path = Path(a["run_dir"]) / "_summary.md"
    if not path.exists():
        raise ValueError(f"no _summary.md in {a['run_dir']}")
    return {"summary": path.read_text()}


def tool_panel_seat(a):
    path = Path(a["run_dir"]) / f"{a['seat']}.md"
    if not path.exists():
        raise ValueError(f"no seat file {path}")
    limit = int(a.get("max_chars") or 20000)
    text = path.read_text()
    return {"seat": a["seat"], "path": str(path), "chars": len(text),
            "truncated": len(text) > limit, "text": text[:limit]}


def tool_panel_resume(a):
    argv = ["--resume", a["run_dir"]]
    if a.get("repo"):
        argv += ["--repo", a["repo"]]  # overrides the repo the run stored
    run_dir, log_path, rc = _launch(argv, False,
                                    f"mcp-resume-{time.strftime('%H%M%S')}.log")
    target = run_dir or a["run_dir"]
    out = {"run_dir": target, "log": str(log_path), "finished": rc is not None}
    try:
        out.update(tool_panel_status({"run_dir": target}))
    except Exception:  # noqa: BLE001 - status is a nicety here
        pass
    return out


def tool_panel_grade(a):
    try:
        rows = panel.grade(a["run_dir"], a["key"], panel.REGISTRY)
    except SystemExit as exc:
        # CLI validation must become a tool error, not terminate stdio MCP.
        raise ValueError(str(exc)) from None
    return {"run_dir": a["run_dir"],
            "report": str(Path(a["run_dir"]) / "_grade.md"),
            "scores": [{"seat": r[0], "found": r[1], "of": r[2],
                        "unmatched": r[3], "note": r[4]} for r in rows]}


def tool_panel_seats(_a):
    health = panel.seat_health()
    stats = panel.track_record()
    out = []
    for name, e in panel.REGISTRY.items():
        kind = "cmd" if "cmd" in e else ("http" if "http" in e else "-")
        last = health.get(name)
        row = {"seat": name, "lineage": e.get("lineage"),
               "default": bool(e.get("default")),
               "local": bool(e.get("local")),
               "grade": e.get("grade", "?"), "via": kind,
               "track": stats.get(name) or {},
               "intelligence": {
                   "score": panel.intelligence_of(name),
                   "label": panel.intelligence_label(name),
                   "measured": (e.get("intelligence") or {}).get("measured"),
                   "note": (e.get("intelligence") or {}).get("note")},
               "note": e.get("note", "")}
        # a quota-dead lane resolves fine; only its last outcome says so
        if last and not last.get("ok"):
            row["last_fail"] = {"ts": last.get("ts"),
                                "error": last.get("error", "")[:120]}
        out.append(row)
    src = panel.INTELLIGENCE
    return {"seats": out,
            "note": ("Seats sharing a lineage are NOT independent voices; the "
                     "consensus table counts lineages, not seats. "
                     f"intelligence = {src.get('index', '?')} score (read "
                     f"{src.get('as_of', '?')}) for the model and effort the "
                     "seat runs: on judgment calls lean toward the higher "
                     "score, but every family is seated for its different "
                     "reasoning, and a reproducible finding outranks any "
                     "score.")}


def tool_panel_verdict(a):
    items = a.get("verdicts") or [{"finding": a.get("finding"),
                                   "verdict": a.get("verdict"),
                                   "note": a.get("note", "")}]
    recorded = []
    for item in items:
        entry = panel.record_verdict(a["run_dir"], item.get("finding"),
                                     item.get("verdict"), item.get("note", ""))
        recorded.append({"finding": entry["finding"],
                         "verdict": entry["verdict"],
                         "seats": entry["seats"],
                         "lineages": entry["lineages"]})
    return {"run_dir": a["run_dir"], "recorded": recorded,
            "note": ("Each seat's confirmed/rejected record is shown in "
                     "panel_seats and next to its AA score in later "
                     "summaries, for the model it ran.")}


HANDLERS = {
    "panel_run": tool_panel_run,
    "panel_status": tool_panel_status,
    "panel_summary": tool_panel_summary,
    "panel_seat": tool_panel_seat,
    "panel_resume": tool_panel_resume,
    "panel_grade": tool_panel_grade,
    "panel_seats": tool_panel_seats,
    "panel_verdict": tool_panel_verdict,
}


# ------------------------------------------------------------------ protocol

def handle(msg):
    """One JSON-RPC message in, one response dict out (None for a notification)."""
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        want = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if isinstance(want, str) else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "panel", "version": SERVER_VERSION},
            "instructions": (
                "Multi-model panel of judges. panel_run fans one question out "
                "to every wired frontier model and returns immediately; poll "
                "panel_status, read panel_summary, and only then pull "
                "individual seats with panel_seat. The panel's output is "
                "evidence, not a verdict: reproduce contested claims yourself "
                "and state your own conclusion rather than relaying a seat."),
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"no tool {name}"}}
        try:
            # stdout is the protocol channel: anything a tool prints goes to
            # stderr instead of corrupting the JSON-RPC stream
            with contextlib.redirect_stdout(sys.stderr):
                payload = fn(args)
            is_error = False
        except Exception as exc:  # noqa: BLE001 - report, never die
            log(f"{name} failed: {exc!r}")
            payload, is_error = {"error": str(exc)}, True
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text",
                         "text": json.dumps(payload, indent=2)}],
            "isError": is_error}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"unknown method {method}"}}


def serve(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    log(f"ready, {len(TOOLS)} tools, {len(panel.REGISTRY)} seats")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log("dropped non-JSON line")
            continue
        if not isinstance(msg, dict):
            # a batch array, null, a bare number: invalid for this server,
            # answered rather than allowed to crash the loop
            log(f"rejected non-object message: {type(msg).__name__}")
            stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32600,
                          "message": "Invalid Request: expected a JSON object"}})
                + "\n")
            stdout.flush()
            continue
        try:
            resp = handle(msg)
        except Exception as exc:  # noqa: BLE001 - a bad message is not fatal
            log(f"handler crashed: {exc!r}")
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": str(exc)}}
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


if __name__ == "__main__":
    try:
        serve()
    except KeyboardInterrupt:
        pass
