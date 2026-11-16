#!/usr/bin/env python3
"""Multi-model panel of judges -- for a code diff OR any other task.

Fans one question out to every wired frontier model in parallel, saves each
answer with provenance the moment it lands, clusters the seats' findings by how
many INDEPENDENT lineages agree, and prints a combined report for whichever
agent is driving to SYNTHESIZE (never relay). Built 2026-07-19 after a
single-reviewer content-filter failure showed why redundancy matters;
generalized 2026-07-20 to arbitrary tasks after 10 review rounds proved
the pattern.

Seats are DATA, not code. `seats.json` says how to reach each model -- `cmd`
shells out to a CLI, `http` POSTs JSON -- so adding a provider, or moving to a
machine with a different set of CLIs installed, is a config edit and not a
patch. Nothing here is specific to any one agent harness: it is a plain CLI
with stdlib-only Python, and `_result.json` carries the whole run in machine
-readable form for whatever is orchestrating.

Four modes:
  --diff <path>      code review: invariant-by-invariant, ends VERDICT: line
  --task "<text>"    anything else: research, design calls, judgement calls,
                     plan critique, naming, tradeoffs. Ends BOTTOM LINE: line.
                     Attach context with repeatable --file <path>.
  --resume <dir|last>  refill only the seats that are missing or failed, using
                     the ORIGINAL prompt. Never re-pays for a seat that landed.
  --grade <dir> --key <key.json>   score a finished run against a known answer
                     key, so seat selection is evidence and not memory.

PROMPT PHRASING RULE (hard-learned): review prompts must use neutral
verification language -- "verify correctness", "find defects", "check
invariants". Words like "adversarial", "break", "attack", "exploit" trip
OpenAI's cyber-content filter AT OUTPUT TIME and eat the final report after a
full paid reasoning run.

Usage:
    python3 panel.py --diff <path> --context "<one-paragraph context>" \
        [--criteria "<numbered invariants>"] [--out DIR] [--round N] \
        [--timeout 2400] [--skip codex,grok,...] [--seats codex,kimi] [--json]

    python3 panel.py --task "Should X be Y or Z, and why?" \
        [--context "..."] [--criteria "what a good answer must cover"] \
        [--file notes.md --file data.csv] [--seats ...] [--include-local]

    python3 panel.py --resume last --repo /path/to/repo
    python3 panel.py --grade ~/panel-runs/myrepo/20260802_1200 --key key.json
    python3 panel.py --list-seats

Runs land in ~/panel-runs/<repo>/<timestamp>/ by default, so the history is
queryable across repos instead of scattering into whatever cwd you were in.

Local (self-hosted) seats are EXCLUDED by default: the 2026-07-20 entrance
exam found zero real defects from any local model across the whole roster.
Add them with --include-local when the value is redundancy, not rigor.
"""

import argparse
import concurrent.futures
import contextlib
import datetime
import errno
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

try:
    import fcntl  # POSIX file locks
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt  # Windows file locks
except ImportError:
    msvcrt = None

REGISTRY_PATH = Path(os.environ.get("PANEL_SEATS")
                     or Path(__file__).with_name("seats.json"))
RUN_META = "run.json"

FINDINGS_BLOCK = """
After your prose, and as the very last thing in your answer, emit a
machine-readable block in exactly this form:

FINDINGS_JSON
[{{"file": "path/as/given.py", "line": 42, "claim": "one specific sentence", "severity": "high"}}]
END_FINDINGS_JSON

One entry per distinct {unit} you are asserting. severity is high, medium or
low. Use null for file/line when they do not apply. Emit [] if you have none.
The block is parsed by a script: valid JSON only, no commentary inside it.
"""

PROMPT_TEMPLATE = """You are one reviewer on an independent multi-model panel.
Review the following code diff for correctness and safety-invariant defects.
Be rigorous and specific; do not assume other reviewers exist.

CONTEXT: {context}

INVARIANTS TO VERIFY: {invariants}

Report format: for each invariant, either state it HOLDS or report a defect
with the location, the concrete failure scenario, and severity. Then list any
OTHER defects you see in the diff (logic, races, validation, tests). Then one
line, exactly: VERDICT: CLEAN or VERDICT: FINDINGS.
{findings_block}
DIFF:
{diff}
"""


TASK_TEMPLATE = """You are one member of an independent multi-model panel, all
answering the same question in parallel. You cannot see the others and must not
assume they will catch anything you miss. Answer completely on your own.

TASK: {task}

CONTEXT: {context}

WHAT A GOOD ANSWER MUST COVER: {criteria}
{attachments}
Rules for your answer:
  * Lead with your actual position. No throat-clearing, no restating the task.
  * Give the reasoning that would let a skeptic check you, not a summary of it.
  * State your assumptions explicitly, and flag anything you are guessing at.
  * If the task is underspecified, answer the most useful reading of it AND
    say which reading you took.
  * Name what would change your mind, and any risk the obvious answer carries.

Then one line, exactly, no markdown:
BOTTOM LINE: <your position in one sentence>
{findings_block}"""


# A seat that answers without the block cannot vote (glm did so in 34 of 63
# runs). One short follow-up to the SAME seat asks for the block alone, so the
# lineage count stays honest: no other model speaks for it.
REPAIR_MARK = "<!-- findings block recovered by a follow-up call to this seat -->"
REPAIR_TIMEOUT = 600
REPAIR_MAX_CHARS = 120_000
REPAIR_TEMPLATE = """Below is an answer you wrote for a multi-model review panel.
It is missing the machine-readable findings block the panel needs to count it.
Do not redo the work, do not add anything new, do not use tools or read files.
Restate ONLY the claims the answer below already makes, as this block and
nothing else:

FINDINGS_JSON
[{{"file": "path/as/given.py", "line": 42, "claim": "one specific sentence", "severity": "high"}}]
END_FINDINGS_JSON

One entry per distinct {unit} the answer asserts. severity is high, medium or
low. Use null for file/line when they do not apply. Emit [] if the answer
asserts nothing. Valid JSON only.

THE ANSWER:
{answer}
"""


def _verdict_line(out):
    """The seat's own one-line summary, for the console + summary file."""
    for marker in ("BOTTOM LINE:", "VERDICT:"):
        start = re.compile(r"^[\s>*#_`-]*" + re.escape(marker))
        for line in reversed(out.splitlines()):
            # must START the line: a finding that quotes "VERDICT: CLEAN"
            # inside its JSON is not the seat's verdict
            if start.match(line) and "FINDINGS_JSON" not in line:
                return line.strip().lstrip("*# ").strip()
    return "?"


# --------------------------------------------------------------- seat engine
# A seat is a row in seats.json, not a function in this file. `cmd` runs a CLI,
# `http` posts JSON. Provider-agnostic on purpose: the panel should not care
# whose model is answering, and a machine with different tooling installed
# should need a different config, not a different program.

def _lock_file(fh, blocking=True):
    """Exclusive lock on an open file: flock on POSIX, msvcrt on Windows.
    Non-blocking raises OSError when another process holds it. The OS
    releases it when the holder exits, so a crash never leaves it stuck."""
    if fcntl:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    elif msvcrt:
        fh.seek(0)
        msvcrt.locking(fh.fileno(),
                       msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)


RUN_LOCK = ".run.lock"
_HELD_RUN_LOCKS = {}


def claim_run(outdir):
    """Hold this run's lock for as long as this process works on it. False
    when another live process (a running panel, a concurrent resume) holds
    it. One atomic OS lock, so two resumes cannot both pass the check."""
    key = str(Path(outdir).resolve())
    if key in _HELD_RUN_LOCKS:
        return True
    fh = open(Path(outdir) / RUN_LOCK, "a+")
    try:
        _lock_file(fh, blocking=False)
    except OSError:
        fh.close()
        return False
    _HELD_RUN_LOCKS[key] = fh
    return True


def release_runs():
    for fh in _HELD_RUN_LOCKS.values():
        try:
            fh.close()
        except OSError:
            pass
    _HELD_RUN_LOCKS.clear()


def _atomic_write(path, text):
    """Write via a sibling temp file and os.replace, so a reader (a status
    poll, a resume, another run) never sees a half-written file."""
    path = Path(path)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _decode(data):
    # One stray byte must not throw away a paid answer (codex echoes tool
    # output on stderr; any CLI can emit a non-UTF-8 byte).
    return (data or b"").decode("utf-8", "replace")


# Every seat CLI runs in its own session/process group so a timeout can stop
# the whole tree (node-wrapped CLIs spawn children that would keep working,
# and spending). The flip side: the terminal's Ctrl-C no longer reaches them.
#
# Stopping on a signal (four review rounds taught this): the handler only
# sets an Event. Work done INSIDE a handler interrupts the main thread
# mid-critical-section -- it lost a serial seat's returned answer, and
# deadlocked on a lock the interrupted code held. A reaper THREAD does the
# killing while the main thread carries on, persists what it holds, writes
# the summary and exits 128+signal on its own.
_LIVE = set()
_LIVE_LOCK = threading.RLock()
# Every seat process carries these in its environment, and every process it
# starts inherits them -- even one that double-forks into its own session and
# is re-parented to init, which no parent-pid walk can find. Cleanup kills
# exactly the processes carrying the token: an unrelated process (a reused
# pid, a stranger in a process group) never matches.
RUN_TOKEN_VAR, SEAT_TOKEN_VAR = "PANEL_RUN_TOKEN", "PANEL_SEAT_TOKEN"
_RUN_TOKEN = uuid.uuid4().hex
NOT_STARTED = -100  # a seat refused because panel is stopping (never -N:
                    # a negative returncode is a real signal exit)
_SIGNALED = [False]  # plain flag: Event.set() itself takes a lock
_REAPED = threading.Event()
_CURRENT_RUN = {}  # outdir/meta/registry of the run in progress
_STOPPING = threading.Event()   # no new seat process may start
_STOP_SIGNAL = threading.Event()  # set only by the signal handler
_STOP_SIGNUM = [0]
HARD_EXIT_AFTER = 20  # seconds the main thread gets to unwind after a stop
_ACTIVE = set()  # threads inside a seat call (ask + persist)
_ACTIVE_LOCK = threading.Lock()


def _ps(args, timeout):
    """(`ps` output, the ps process's own pid). The caller drops that pid:
    the snapshot's own helper is a fresh child of panel every time, and
    counting it kept the freeze loop from ever seeing a stable tree."""
    try:
        proc = subprocess.Popen(["ps", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        out, _ = proc.communicate(timeout=timeout)
        return out.decode("utf-8", "replace"), proc.pid
    except (OSError, subprocess.SubprocessError):
        return "", None


def _descendants(pid):
    """Every process below pid, from one `ps` snapshot."""
    out, helper = _ps(["-A", "-o", "pid=,ppid="], 5)
    if not out:
        return []
    children = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in found and child != helper:
                found.append(child)
                todo.append(child)
    return found


def _pids_with_token(var, token):
    """Pids whose environment carries var=token (POSIX). Linux reads
    /proc/<pid>/environ; macOS/BSD read `ps -E`. Empty when neither works."""
    needle, found, me = f"{var}={token}", set(), os.getpid()
    proc = Path("/proc")
    if (proc / "self" / "environ").exists():
        for entry in proc.iterdir():
            if entry.name.isdigit():
                try:
                    data = (entry / "environ").read_bytes()
                except OSError:
                    continue
                if needle.encode() in data.split(b"\0"):
                    found.add(int(entry.name))
    else:
        out, helper = _ps(["-A", "-ww", "-E", "-o", "pid=,command="], 10)
        found.discard(helper)
        for line in out.splitlines():
            head, _, rest = line.strip().partition(" ")
            if head.isdigit() and needle in rest.split():
                found.add(int(head))
    found.discard(me)
    return found


def _freeze_and_kill(root, leaders=(), token=None):
    """Freeze, then kill. SIGSTOP every process below `root` and every
    process carrying `token` (var, value) until a snapshot finds nothing new:
    a frozen process cannot spawn, cannot be re-parented away, and cannot
    exit and hand its pid to a stranger. Then SIGKILL every frozen process,
    and the process groups of `leaders` -- sessions panel created itself."""
    frozen, gone = set(), set()
    if root != os.getpid():  # a seat's own process: freeze it too
        try:
            os.kill(root, signal.SIGSTOP)
            frozen.add(root)
        except OSError:
            pass
    for _ in range(FREEZE_ROUNDS):
        seen = set(_descendants(root))
        if token:
            seen |= _pids_with_token(*token)
        new = [pid for pid in seen if pid not in frozen and pid not in gone]
        if not new:
            break
        for pid in new:
            try:
                os.kill(pid, signal.SIGSTOP)
                frozen.add(pid)
            except OSError:
                gone.add(pid)
    for pid in leaders:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    for pid in frozen:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


FREEZE_ROUNDS = 64


def _kill_tree(proc, seat_token=None):
    """One seat's tree, on its timeout: everything below it, and everything
    that carries its seat token even after its parent exited."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        _freeze_and_kill(proc.pid, leaders=[proc.pid],
                         token=(SEAT_TOKEN_VAR, seat_token)
                         if seat_token else None)
    try:
        proc.kill()
    except OSError:
        pass


def reap_live_seats():
    """Stop every seat process tree and refuse new ones. _STOPPING is set
    under _LIVE_LOCK, the lock run_cmd holds from its last stop check to
    registering its child, so no seat can start after this returns."""
    with _LIVE_LOCK:
        _STOPPING.set()
        live = list(_LIVE)
    if os.name == "nt":
        for proc in live:
            _kill_tree(proc)
        return
    _freeze_and_kill(os.getpid(), leaders=[proc.pid for proc in live],
                     token=(RUN_TOKEN_VAR, _RUN_TOKEN))


@contextlib.contextmanager
def _tracked_call():
    me = threading.get_ident()
    with _ACTIVE_LOCK:
        _ACTIVE.add(me)
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(me)


def _drain(timeout=10.0):
    """Wait (bounded) for threads still inside a seat call to persist."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _ACTIVE_LOCK:
            if not _ACTIVE:
                return
        time.sleep(0.05)


def stop_requested():
    return _STOP_SIGNAL.is_set()


STDERR_KEEP = 200_000  # chars: an answer on stderr is content, not a log tail


def run_cmd(cmd, timeout, stdin_text=None, env=None, cwd=None):
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
             if os.name == "nt" else {"start_new_session": True})
    seat_token = uuid.uuid4().hex
    tagged = {**os.environ, **(env or {}), RUN_TOKEN_VAR: _RUN_TOKEN,
              SEAT_TOKEN_VAR: seat_token}
    if cwd:
        # some CLIs work in $PWD, not in the directory they were started in:
        # OpenCode followed a stale PWD into another checkout (2026-11-10)
        tagged["PWD"] = str(cwd)
    with _LIVE_LOCK:  # last stop check -> spawn -> register, one critical section
        if _STOPPING.is_set():
            return NOT_STARTED, "panel is stopping; seat not started"
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=tagged, cwd=cwd, **group)
        except FileNotFoundError:
            return -2, "CLI not installed"
        except OSError as exc:
            if exc.errno == errno.E2BIG:
                size = sum(len(str(a).encode()) for a in cmd) // 1024
                return -2, (f"command line too long ({size} KB): this seat "
                            f"takes the prompt as an argument. Give it "
                            f"\"stdin\": true in seats.json if its CLI reads "
                            f"stdin, or attach less.")
            return -2, f"could not start the CLI: {exc}"
        _LIVE.add(proc)
    try:
        try:
            out_b, err_b = proc.communicate(
                input=(stdin_text or "").encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired as first:
            _kill_tree(proc, seat_token)
            out_b, err_b = first.output or b"", first.stderr or b""
            try:
                more, more_err = proc.communicate(timeout=5)
                out_b, err_b = more or out_b, more_err or err_b
            except subprocess.TimeoutExpired as second:
                # something still holds the pipe: keep what was read
                out_b = second.output or out_b
                err_b = second.stderr or err_b
            except (ValueError, OSError):
                pass
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except (OSError, AttributeError):
                    pass
            partial = _decode(out_b).strip()
            err_partial = _decode(err_b).strip()
            if err_partial:  # some CLIs answer on stderr, progress on stdout
                partial = (f"{partial}\n\n[stderr]\n"
                           f"{err_partial[:STDERR_KEEP]}"
                           if partial else err_partial)
            # keep what the seat had written: a 40-minute answer cut off at
            # the end is still worth reading, though it never votes
            return -1, (f"TIMEOUT after {timeout}s" + (
                f"\n\npartial output before the timeout:\n{partial}"
                if partial else ""))
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(proc)
    out, err = _decode(out_b).strip(), _decode(err_b).strip()
    if proc.returncode != 0 and out and err:
        # progress text on stdout must not hide the fatal line on stderr
        out = f"{out}\n\n[stderr]\n{err[:STDERR_KEEP]}"
    return proc.returncode, out or err


def _subst(obj, mapping):
    """Fill {prompt}/{repo}/{key} placeholders anywhere in a spec.

    A value that is exactly one placeholder is replaced wholesale, so a prompt
    never gets mangled by str() and non-string spec values survive intact.
    """
    if isinstance(obj, str):
        if obj in mapping:
            return mapping[obj]
        for k, v in mapping.items():
            if k in obj:
                obj = obj.replace(k, str(v))
        return obj
    if isinstance(obj, list):
        return [_subst(o, mapping) for o in obj]
    if isinstance(obj, dict):
        return {k: _subst(v, mapping) for k, v in obj.items()}
    return obj


def load_key(spec):
    """(key, error) for a key_from spec. Keys never touch argv or a log."""
    if not spec:
        return None, None
    if spec.get("env"):
        val = os.environ.get(spec["env"])
        return val, None if val else f"${spec['env']} is not set"
    path = Path(spec.get("file", "")).expanduser()
    if not path.exists():
        return None, f"no credentials file at {path}"
    try:
        if spec.get("json_path"):
            node = json.loads(path.read_text())
            for part in spec["json_path"].split("."):
                node = node.get(part) if isinstance(node, dict) else None
            return (node, None) if node else (
                None, f"{spec['json_path']} not in {path}")
        want = spec.get("env_key", "")
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            if line.startswith(want + "="):
                return line.split("=", 1)[1].strip().strip("\"'"), None
        return None, f"{want} not in {path}"
    except Exception as exc:  # noqa: BLE001 - a bad key file is a dead seat
        return None, f"cannot read {path}: {exc}"


def _path_tokens(path):
    """'output[].content[0].text' -> ['output','[]','content','[0]','text']"""
    return [t for t in re.split(r"\.|(\[\d*\])", path) if t]


def _walk(node, tokens):
    if not tokens:
        return node if isinstance(node, str) else None
    tok, rest = tokens[0], tokens[1:]
    if tok == "[]":
        if not isinstance(node, list):
            return None
        for item in node:
            got = _walk(item, rest)
            if got:
                return got
        return None
    if tok.startswith("[") and tok.endswith("]"):
        idx = int(tok[1:-1])
        if isinstance(node, list) and -len(node) <= idx < len(node):
            return _walk(node[idx], rest)
        return None
    if isinstance(node, dict):
        return _walk(node.get(tok), rest)
    return None


def extract(data, path):
    """Pull text out of a response body by dotted path. [] = first match."""
    return _walk(data, _path_tokens(path))


# A streaming spec ("stream": true in its body) is read with this idle
# timeout instead of the whole seat budget. Model Studio sometimes accepts a
# request and never answers it: all three of its seats sat 40 minutes on one
# in run 20261028_114718, while fresh calls answered in 3 s. A stream
# that goes this long without a byte is stalled, and is retried.
STREAM_IDLE_TIMEOUT = 180
STREAM_ATTEMPTS = 3


def _stalled(exc):
    return isinstance(exc, TimeoutError) or isinstance(
        getattr(exc, "reason", None), TimeoutError)


def _read_sse(resp, deadline):
    """Fold an OpenAI-style event stream into the non-streaming response
    shape, so the spec's extract paths work unchanged."""
    content, reasoning = [], []
    for raw in resp:
        if time.monotonic() > deadline:
            raise RuntimeError("seat budget ran out mid-stream")
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        event = json.loads(payload)
        if event.get("error"):
            raise RuntimeError(f"stream error: {event['error']}")
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            content.append(delta.get("content") or "")
            reasoning.append(delta.get("reasoning_content") or "")
    return {"choices": [{"message": {"content": "".join(content),
                                     "reasoning_content": "".join(reasoning)}}]}


def _http_response(req, streaming, deadline, read_timeout):
    """Bound the entire urllib operation, including headers and body reads.

    Socket timeouts alone reset with each read. urllib is not cancellable;
    a daemon may finish draining later, but cannot publish a late answer.
    """
    done, result = threading.Event(), []

    def read():
        try:
            try:
                with urllib.request.urlopen(req, timeout=read_timeout) as resp:
                    data = (_read_sse(resp, deadline) if streaming
                            else json.loads(resp.read()))
            except urllib.error.HTTPError as exc:
                try:
                    reason = exc.read(400).decode("utf-8", "replace").strip()
                except Exception:  # noqa: BLE001
                    reason = ""
                finally:
                    exc.close()
                raise RuntimeError(f"{exc}: {reason}" if reason else str(exc))
            result.append((True, data))
        except Exception as exc:  # noqa: BLE001
            result.append((False, exc))
        finally:
            done.set()

    if time.monotonic() >= deadline:
        raise RuntimeError("seat budget ran out")
    threading.Thread(target=read, daemon=True).start()
    if not done.wait(max(0, deadline - time.monotonic())):
        raise RuntimeError("seat budget ran out")
    ok, value = result[0]
    if not ok:
        raise value
    return value


def http_seat(specs, prompt, repo, timeout):
    """POST to each spec in turn until one yields text. Seats degrade, never
    raise: one unreachable provider must not take the panel down."""
    last = "no http spec"
    keys = []
    deadline = time.monotonic() + timeout
    for spec in specs:
        if _STOPPING.is_set():
            return NOT_STARTED, "panel is stopping; seat not started"
        key, err = load_key(spec.get("key_from"))
        if spec.get("key_from") and not key:
            last = err or "missing credentials"
            continue
        if key:
            keys.append(key)
        mapping = {"{prompt}": prompt, "{repo}": str(repo), "{key}": key or ""}
        headers = _subst(spec.get("headers",
                                  {"Content-Type": "application/json"}),
                         mapping)
        body = json.dumps(_subst(spec["body"], mapping)).encode()
        streaming = spec["body"].get("stream") is True
        data = None
        for attempt in range(1, (STREAM_ATTEMPTS if streaming else 1) + 1):
            if _STOPPING.is_set():
                return NOT_STARTED, "panel is stopping; seat not started"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last = "seat budget ran out"
                break
            try:
                req = urllib.request.Request(_subst(spec["url"], mapping),
                                             data=body, headers=headers)
                # Socket waits use whole seconds; the outer deadline retains
                # exact subsecond precision across all retries/fallbacks.
                read_timeout = min(timeout, math.ceil(remaining),
                                   STREAM_IDLE_TIMEOUT if streaming else timeout)
                data = _http_response(req, streaming, deadline, read_timeout)
                break
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                if streaming and _stalled(exc):
                    last = (f"stalled {STREAM_IDLE_TIMEOUT}s without a byte "
                            f"on attempt {attempt} of {STREAM_ATTEMPTS}")
                    if deadline - time.monotonic() > STREAM_IDLE_TIMEOUT:
                        continue
                break
        if data is None:
            continue
        paths = spec.get("extract", [])
        for i, path in enumerate(paths):
            text = extract(data, path)
            if text and text.strip():
                # Thinking models sometimes spend the whole budget inside
                # reasoning and leave the answer field empty: surface the
                # fallback rather than nothing, but say that is what happened.
                note = "" if i == 0 else f"[fell back to `{path}`]\n\n"
                answer = note + text
                for known_key in keys:
                    answer = answer.replace(known_key, "[key]")
                return 0, answer
        last = f"no text at any of {paths}"
    # Every error path, not only HTTPError: a stream error or exception text
    # that echoes the key must not ride into the seat file or _result.json.
    for key in keys:
        last = last.replace(key, "[key]")
    return -3, f"seat unavailable: {last}"


def _retry_delay(text):
    """Seconds a provider explicitly asked us to wait, or None.

    A 429 that says 'Please retry in 24s' with 2400s of budget left is a
    pause, not a failure: honoring one short sleep keeps that lineage in the
    panel instead of silently shrinking it (gemini, run 20260813_182034).
    """
    m = (re.search(r"[Rr]etry-?[Aa]fter:?\s+(\d+(?:\.\d+)?)", text or "")
         or re.search(r"[Rr]etry in (\d+(?:\.\d+)?)\s*s", text or ""))
    return float(m.group(1)) if m else None


# The two run modes, spelled exactly as meta["mode"] records them. A seat's
# CLI wiring can differ between them: a diff review locks the seat down to the
# attached diff, while a --task run may need to open a file the prompt attaches
# by reference. Default to the locked-down mode so an unknown context stays safe.
DIFF_MODE = "diff review"
TASK_MODE = "task"
_active_mode = DIFF_MODE


def set_active_mode(mode):
    """Record the mode of the run about to fan out. Read by cmd seats that ship
    mode-specific argv (see `argv_by_mode` in seats.json). Set once before the
    seats launch; every seat in a run shares it."""
    global _active_mode
    _active_mode = mode or DIFF_MODE


def resolve_argv(spec, mode):
    """A cmd spec's argv for one mode: the base `argv` plus any `argv_by_mode`
    tail for that mode. Falls back to the diff-review tail (the locked-down one)
    when the mode has no entry, so a stray mode never unlocks a seat."""
    by_mode = spec.get("argv_by_mode") or {}
    tail = by_mode.get(mode, by_mode.get(DIFF_MODE, []))
    return list(spec["argv"]) + list(tail)


def make_seat(entry):
    """One registry row -> one callable seat."""
    if "cmd" in entry:
        def seat(prompt, repo, timeout, cwd=None, _e=entry):
            spec = _e["cmd"]
            argv = [_subst(a, {"{prompt}": prompt, "{repo}": str(repo)})
                    for a in resolve_argv(spec, _active_mode)]
            stdin_text = prompt if spec.get("stdin") else None
            # settings a CLI only takes from its environment (Kimi's effort)
            env = dict(spec.get("env") or {})
            # and credentials it only takes from there ({VAR: key_from spec}):
            # never in argv, and scrubbed from whatever the CLI prints back
            secrets = []
            for var, source in (spec.get("env_from") or {}).items():
                key, err = load_key(source)
                if not key:
                    return -3, f"seat unavailable: {err or 'missing credentials'}"
                env[var] = key
                secrets.append(key)
            # a review seat keeps the launch directory; build.py names one
            where = {"cwd": cwd} if cwd else {}
            started = time.monotonic()
            code, out = run_cmd(argv, timeout, stdin_text=stdin_text, env=env,
                                **where)
            if code != 0:
                delay = _retry_delay(out)
                remaining = timeout - (time.monotonic() - started)
                # one retry, only when the asked-for wait costs less than
                # half of what is left of this seat's budget
                if delay is not None and delay < remaining / 2:
                    time.sleep(delay + 1)
                    retry = run_cmd(argv, remaining - delay - 1,
                                    stdin_text=stdin_text, env=env, **where)
                    if retry[0] != NOT_STARTED:  # stopped: keep what ran
                        code, out = retry
            for key in secrets:
                out = out.replace(key, "[key]")
            return code, out
        return seat
    if "http" in entry:
        def seat(prompt, repo, timeout, _e=entry):
            spec = _e["http"]
            return http_seat(spec if isinstance(spec, list) else [spec],
                             prompt, repo, timeout)
        return seat

    def unconfigured(prompt, repo, timeout):
        return -6, "seat has neither a `cmd` nor an `http` spec in seats.json"
    return unconfigured


def load_registry(path=None):
    """Seat metadata AND wiring -- lineage, default-on, serial, how to invoke,
    graded track record. Set $PANEL_SEATS to point at your own roster."""
    path = Path(path) if path else REGISTRY_PATH
    data = json.loads(path.read_text()) if path.exists() else {"seats": {}}
    return {k: v for k, v in data.get("seats", {}).items()
            if not k.startswith("_")}


def build_panel(registry):
    return {name: make_seat(entry) for name, entry in registry.items()}


def load_presets(path=None):
    """Named rosters from the registry file's `presets` block ({name: [seats]})."""
    path = Path(path) if path else REGISTRY_PATH
    data = json.loads(path.read_text()) if path.exists() else {}
    return {k: v for k, v in data.get("presets", {}).items()
            if not k.startswith("_")}


def load_intelligence(path=None):
    """Which leaderboard the seats' `intelligence` scores come from, and when
    they were read ({index, url, as_of})."""
    path = Path(path) if path else REGISTRY_PATH
    data = json.loads(path.read_text()) if path.exists() else {}
    return {k: v for k, v in data.get("intelligence", {}).items()
            if not k.startswith("_")}


REGISTRY = load_registry()
PANEL = build_panel(REGISTRY)
PRESETS = load_presets()
INTELLIGENCE = load_intelligence()


def lineage_of(seat, registry=None):
    """Which independent model family a seat speaks for. Seats that share a
    lineage count as ONE voice: three Claude seats, two grok scaffolds, or a
    local gemma sitting next to the hosted gemini."""
    reg = registry if registry is not None else REGISTRY
    return reg.get(seat, {}).get("lineage", seat)


def pinned_model(entry):
    """The model string a seat pins: the argv value after -m/--model, else
    the first http body.model. None when the CLI picks its own model."""
    if "cmd" in entry:
        argv = entry["cmd"]["argv"]
        for flag, value in zip(argv, argv[1:]):
            if flag in ("-m", "--model"):
                return value
        return None
    specs = entry.get("http")
    for spec in specs if isinstance(specs, list) else [specs] if specs else []:
        model = spec.get("body", {}).get("model")
        if isinstance(model, str):
            return model
    return None


def seat_identity(entry):
    """What a verdict credits: the pinned model when there is exactly one
    route, else every route's model/preset/url joined. pinned_model alone
    would credit pplx's unused fallback model when its first route (an
    Agent-API preset with no model) answered. A changed identity starts a
    fresh track record."""
    if "cmd" in entry:
        return pinned_model(entry) or entry["cmd"]["argv"][0]
    specs = entry.get("http")
    specs = specs if isinstance(specs, list) else [specs] if specs else []
    parts = []
    for spec in specs:
        body = spec.get("body") or {}
        parts.append(str(body.get("model") or body.get("preset")
                         or spec.get("url") or "?"))
    return "|".join(parts) or None


def intelligence_of(seat, registry=None):
    """The seat's intelligence-index score, or None when unmeasured.

    A score belongs to the model it was measured on. seat-refresh bumps pins
    by itself, so once the pin moves the old number reads as None (stale)
    until someone re-reads the leaderboard."""
    reg = registry if registry is not None else REGISTRY
    entry = reg.get(seat, {})
    info = entry.get("intelligence") or {}
    if info.get("score") is None or info.get("model") != pinned_model(entry):
        return None
    return info["score"]


def intelligence_label(seat, registry=None):
    """'57'; '≤43' when the index measured a higher effort than the seat
    runs; 'stale' when the pin moved since; '-' when unmeasured."""
    reg = registry if registry is not None else REGISTRY
    info = reg.get(seat, {}).get("intelligence") or {}
    score = intelligence_of(seat, reg)
    if score is None:
        return "stale" if info.get("score") is not None else "-"
    return f"≤{score}" if info.get("ceiling") else str(score)


_STOP = {
    "the", "and", "for", "that", "this", "with", "from", "into", "when",
    "will", "would", "could", "should", "not", "but", "are", "was", "were",
    "has", "have", "had", "its", "which", "then", "than", "there", "their",
    "they", "you", "your", "can", "may", "might", "does", "did", "line",
    "code", "file", "issue", "problem", "bug",
}


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9_]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _containment(a, b):
    """How much of the smaller claim lives inside the bigger one. Jaccard
    punishes a short claim agreeing with a long one; containment doesn't."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _extract_json_array(blob):
    """First balanced [...] in blob, tolerating code fences and stray prose."""
    start = blob.find("[")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(blob)):
        ch = blob[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return blob[start:i + 1]
    return None


_START_MARKER = re.compile(r"(?<!END_)FINDINGS_JSON")
# The block the prompt asks for starts on its own line (code fences, bold or
# a quote marker allowed in front). A mention of the marker inside a claim or
# in prose never starts a line: JSON strings cannot hold a raw newline.
_LINE_MARKER = re.compile(r"(?m)^[ \t>*`#_-]*(?<!END_)FINDINGS_JSON\b")
# The FINDINGS_BLOCK example. A seat that echoes the prompt must not turn
# it into a high-severity finding.
_TEMPLATE_CLAIM = ("path/as/given.py", "one specific sentence")
_SEVERITY = {"high": "high", "critical": "high", "blocker": "high",
             "severe": "high", "p0": "high", "p1": "high",
             "medium": "medium", "moderate": "medium", "major": "medium",
             "p2": "medium",
             "low": "low", "minor": "low", "nit": "low", "trivial": "low",
             "info": "low", "p3": "low"}


def _clean_findings(items):
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()
        if not claim:
            continue
        line = item.get("line")
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        sev = str(item.get("severity") or "unspecified").strip().lower()
        f = item.get("file")
        out.append({"file": (str(f).strip() if f else None), "line": line,
                    "claim": claim,
                    "severity": _SEVERITY.get(sev, "unspecified")})
    return out


def parse_findings(text):
    """(findings, parsed_ok) from a seat's answer.

    parsed_ok is False when the seat never emitted a usable block -- per hard
    rule 8, silence is not agreement, so callers must show that as 'no vote'
    rather than as 'found nothing'.

    The LAST complete block wins (a seat that corrects itself means the
    second one), markers on their own line beat inline mentions, and the
    END marker is not a cut point: a claim may quote it.
    """
    text = text or ""
    anchored = [m.end() for m in _LINE_MARKER.finditer(text)]
    inline = [m.end() for m in _START_MARKER.finditer(text)
              if m.end() not in anchored]
    for starts in (anchored, inline):
        parsed = []  # (array_start, array_end, findings), in text order
        for start in starts:
            raw = _extract_json_array(text[start:])
            if raw is None:
                continue
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(items, list):
                continue
            at = text.index(raw, start)
            parsed.append((at, at + len(raw), _clean_findings(items)))
        # a marker quoted INSIDE another block's JSON string is not a block
        outer = [p for p in parsed
                 if not any(o[0] < p[0] < o[1] for o in parsed if o is not p)]
        for _, _, findings in reversed(outer):
            echoed = findings and all(
                (f["file"], f["claim"]) == _TEMPLATE_CLAIM for f in findings)
            if not echoed:
                return findings, True
    return [], False


LINE_WINDOW = 25

_EXHIBIT_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff",
                 ".mp4", ".mov", ".webm", ".avi", ".mkv",
                 ".mp3", ".wav", ".m4a", ".flac"}


def _is_exhibit(path):
    return Path(path).suffix.lower() in _EXHIBIT_EXTS


def _place_relation(a, b):
    """'same', 'different' or 'unknown' for two findings' locations.

    Seats cite the same defect at slightly different lines, so a window is
    needed; but two findings that name genuinely different places are two
    findings. A false merge inflates the lineage count that drives action,
    which is the more expensive mistake, so 'different' never merges.

    Media paths are EXHIBITS, not locations: a frame still or video names
    where the evidence was seen, not where the defect lives. In the
    20260813_182034 video run, seats citing different stills of the same
    moment (t10.5.png vs t11.4.png) split one defect in two, and two seats
    citing the same mp4 got the generous same-place bar for unrelated
    claims. An exhibit neither merges nor separates.
    """
    fa = Path(a["file"]).name.lower() if a.get("file") else None
    fb = Path(b["file"]).name.lower() if b.get("file") else None
    if (fa and _is_exhibit(fa)) or (fb and _is_exhibit(fb)):
        return "unknown"
    if fa is None or fb is None:
        return "unknown"
    if fa != fb:
        return "different"
    if a.get("line") is None or b.get("line") is None:
        return "same"
    return "same" if abs(a["line"] - b["line"]) <= LINE_WINDOW else "different"


SAME_PLACE_BAR = 0.3
ANY_PLACE_BAR = 0.45
CONTAINED_BAR = 0.5
NEGATION_BAR = 0.5
# Polarity for the negation guard: explicit negators and absence words
# ("missing", "skipped", "lacks") each flip it, so "the check is missing" and
# "the check is skipped" agree while "the check is not missing" disputes
# both. The guard fires only on near-identical words (NEGATION_BAR on stemmed
# tokens): "the lock is released" vs "the lock is never released".
_EXPLICIT_NEG = re.compile(r"\b(?:not|never|no|none|cannot|\w+n't)\b", re.I)
_ABSENCE = re.compile(
    r"\b(?:missing|lacks?|lacking|absent|without|skip(?:s|ped|ping)?|"
    r"omit(?:s|ted|ting)?|fails?|failed|unable|ignor(?:es|ed|ing)|"
    r"drops?|dropped)\b", re.I)


def _polarity(claim):
    """How many negators and absence words a claim carries. "missing X",
    "no X" and "X is skipped" all carry one; "X is not missing" carries two
    and "X is present" none. Counts, not parity: "the lock is not released
    and cleanup is not performed" (two) must not agree with the plain
    positive sentence (none). Claims that differ in count and share most
    words are a dispute (Codex reviews 2026-11-09)."""
    claim = claim or ""
    return (len(_EXPLICIT_NEG.findall(claim))
            + len(_ABSENCE.findall(claim)))


def _stem_tokens(claim):
    out = set()
    for w in _tokens(claim):
        for suffix in ("ing", "ed", "es", "s"):
            if len(w) > 4 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.add(w)
    return out


def _disputes(a, b):
    """True when two claims say near-identical words with opposite
    polarity (one denies what the other asserts)."""
    if _polarity(a) == _polarity(b):
        return False
    return _jaccard(_stem_tokens(a), _stem_tokens(b)) >= NEGATION_BAR


def _best_cluster(clusters, seat, f):
    """The cluster this finding belongs to, or None.

    A cluster qualifies only if the finding is compatible with EVERY member
    (no member at a different place, no member it disputes), so claims at
    lines 10 and 50 cannot be bridged by one at line 30. Among qualifying
    clusters the best-matching member decides, not the first cluster over
    the bar. A seat's own second finding never joins a cluster it is already
    in: one seat asserting two things is two claims. A false merge inflates
    the lineage count that drives action, so every doubt resolves toward
    keeping claims apart."""
    toks = _tokens(f["claim"])
    best, best_score = None, 0.0
    for c in clusters:
        if seat in c["seats"]:
            continue
        places = [_place_relation(m, f) for m in c["members"]]
        if "different" in places:
            continue
        if any(_disputes(m["claim"], f["claim"]) for m in c["members"]):
            continue
        for place, m_toks in zip(places, c["member_tokens"]):
            sim = _jaccard(m_toks, toks)
            score = 0.0
            if sim >= (SAME_PLACE_BAR if place == "same" else ANY_PLACE_BAR):
                score = sim
            elif (place == "unknown"
                  and _containment(m_toks, toks) >= CONTAINED_BAR):
                score = _containment(m_toks, toks) * 0.9
            if score > best_score:
                best, best_score = c, score
    return best


def cluster_findings(per_seat, registry=None):
    """Group the seats' findings into one entry per distinct claim.

    Two findings merge when they point at the same place and overlap loosely,
    or when they overlap strongly wherever they point. Where the place gives
    no signal (no file cited, or an exhibit), a claim also merges when it is
    largely CONTAINED in one member claim: paraphrases of one defect share a
    core phrase but differ wildly in length, which sinks their jaccard against
    a cluster's ever-growing token union. Containment is judged per member,
    not against the union, so a big cluster does not get stickier over time.
    Seats are walked in registry order so the same input always produces the
    same clusters, and the sharpest seat's wording ends up representing the
    cluster.
    """
    reg = registry if registry is not None else REGISTRY
    clusters = []
    for seat, findings in per_seat.items():
        for f in findings:
            match = _best_cluster(clusters, seat, f)
            toks = _tokens(f["claim"])
            if match is None:
                # tokens (the union) grows in place; member_tokens must keep
                # each claim's own set, so the first member gets a copy
                clusters.append({"rep": f, "tokens": set(toks),
                                 "member_tokens": [toks], "members": [f],
                                 "seats": [seat],
                                 "severities": [f["severity"]],
                                 "claims": [f["claim"]],
                                 "cited": [_where(f)] if f.get("file") else [],
                                 "cited_by": [bool(f.get("file"))]})
            else:
                match["seats"].append(seat)
                match["cited_by"].append(bool(f.get("file")))
                match["tokens"] |= toks
                match["member_tokens"].append(toks)
                match["members"].append(f)
                match["severities"].append(f["severity"])
                match["claims"].append(f["claim"])
                if f.get("file"):
                    match["cited"].append(_where(f))
    order = {"high": 0, "medium": 1, "low": 2, "unspecified": 3}
    for c in clusters:
        c["lineages"] = sorted({lineage_of(s, reg) for s in c["seats"]})
        c["severity"] = sorted(c["severities"], key=lambda s: order[s])[0]
        # One seat's guessed path must not become the cluster's headline
        # location just because it was added first. Seats naming DIFFERENT
        # files are disputing where the thing is; seats naming the same file a
        # few lines apart are agreeing, so the dispute is judged on files.
        cited = sorted(set(c["cited"]))
        files = sorted({w.split(":")[0] for w in cited})
        c["locations"] = cited
        c["files"] = files
        # Findings merge on FILENAME, so two seats can agree on the defect
        # while naming different paths to it (src/synthesizer.py vs
        # synthesizer.py). Picking one silently presents a guess as fact.
        if not files:
            c["where"] = "-"
        elif len(files) > 1:
            c["where"] = "disputed: " + " / ".join(files[:3])
        else:
            c["where"] = cited[0] if len(cited) == 1 else files[0]
        # Several lineages can also agree on a claim while only ONE of them
        # named a file at all. Say whose path it is, so a multi-lineage finding
        # never lends its weight to a single seat's guess.
        if files and len(c["cited"]) < len(c["seats"]):
            citing = [s for s, f in zip(c["seats"], c["cited_by"]) if f]
            c["where"] += f" (path from {citing[0]} only)"
    clusters.sort(key=lambda c: (-len(c["lineages"]), order[c["severity"]]))
    return clusters


def _where(f):
    if not f.get("file"):
        return "-"
    return f"{f['file']}:{f['line']}" if f.get("line") else f["file"]


def _run_intelligence(meta, seat):
    """(score, label) recorded in run.json when the seat ran. Not the
    registry: a reader may hold another roster (seat-refresh moved a pin, a
    long-lived MCP server, scores re-read later), and a score belongs to the
    model that actually answered. Runs older than scoring read as unscored."""
    rec = (meta.get("intelligence") or {}).get(seat) or {}
    return rec.get("score"), rec.get("label", "-")


def _top_intelligence(seats, meta):
    """Best recorded intelligence score among the seats behind a finding."""
    scores = [s for s in (_run_intelligence(meta, n)[0] for n in seats)
              if s is not None]
    return max(scores) if scores else None


def render_summary(outdir, meta, seat_results, clusters, no_vote,
                   registry=None, pending=None, merge_note=None):
    """_summary.md: the consensus table first, then every seat's bottom line.

    A seat that exited non-zero produced NO answer. Counting it as answered --
    and worse, counting its lineage as an independent voice -- overstates how
    much the panel actually agrees, which is the one number this file exists to
    report honestly.
    """
    reg = registry if registry is not None else REGISTRY
    pending = list(pending or [])
    answered = {n: r for n, r in seat_results.items() if r["exit"] == 0}
    failed = {n: r for n, r in seat_results.items() if r["exit"] != 0}
    lineages = {lineage_of(s, reg) for s in answered}
    lines = [f"# panel run {meta['ts']}", ""]
    still_out = [n for n in pending if n not in failed]
    if still_out:
        lines.append(f"> **STATUS: INCOMPLETE.** {len(still_out)} seat(s) have "
                     f"not reported yet: {', '.join(still_out)}. This file is "
                     f"rebuilt on every status check, so its existence does "
                     f"NOT mean the run finished. Do not synthesize yet.")
        lines.append("")
    lines.append(f"mode: {meta['mode']}")
    if meta.get("round", 1) > 1:
        lines.append(f"round: {meta['round']}")
    lines.append(f"question: {meta.get('question')}")
    lines.append(f"seats: {len(answered)} answered "
                 f"({len(lineages)} independent lineages"
                 f"{': ' + ', '.join(sorted(lineages)) if lineages else ''})")
    if failed:
        lines.append(f"FAILED, contributed nothing: "
                     + ", ".join(f"{n} (exit {r['exit']})"
                                 for n, r in failed.items()))
    if meta.get("skipped"):
        lines.append("SKIPPED as dead lanes (no vote; top up or re-login, or "
                     "name them with --seats): "
                     + "; ".join(f"{n}: {why}"
                                 for n, why in meta["skipped"].items()))
    repaired = [n for n, r in answered.items() if r.get("repaired")]
    if repaired:
        lines.append("Findings block recovered by a follow-up call to the "
                     "same seat: " + ", ".join(repaired))
    lines.append("")

    lines.append("## Consensus")
    lines.append("")
    if clusters:
        lines.append("Seats that share a lineage count once -- the Claude "
                     "seats together, both grok scaffolds together. "
                     "`lineages` is the number that matters; `+N likely` "
                     "is the merge judge's proposal, NOT counted.")
        lines.append("")
        lines.append("| id | lineages | severity | where | claim | seats | top AA |")
        lines.append("| --- | ---: | --- | --- | --- | --- | ---: |")
        for c in clusters:
            claim = c["rep"]["claim"].replace("|", "\\|")
            top = _top_intelligence(c["seats"], meta)
            likely = c.get("likely_lineages") or []
            count = str(len(c["lineages"])) + (
                f" (+{len(likely)} likely)" if likely else "")
            lines.append(f"| #{finding_id(c)} | {count} | {c['severity']} | "
                         f"{c['where']} | {claim} | "
                         f"{', '.join(c['seats'])} | "
                         f"{'-' if top is None else top} |")
        merged = [c for c in clusters if c.get("likely")]
        if merged:
            lines.append("")
            lines.append(f"Likely the same claim (merge judge "
                         f"{(merge_note or {}).get('model') or '?'}; rows "
                         f"moved here, read them before trusting a `+N`):")
            for c in merged:
                for m in c["likely"]:
                    lines.append(f"- #{finding_id(c)} ← #{finding_id(m)} "
                                 f"[{', '.join(m['lineages'])}] "
                                 f"{m['rep']['claim']} "
                                 f"({', '.join(m['seats'])})")
    else:
        lines.append("No machine-readable findings were emitted by any seat.")
    if merge_note and merge_note.get("status") in ("stale", "failed"):
        lines.append("")
        lines.append(f"Merge judge: {merge_note['status']}"
                     + (f" ({merge_note.get('error')})"
                        if merge_note.get("error") else "")
                     + "; counts above are lexical clustering only.")
    lines.append("")

    lines.append("## Seats")
    lines.append("")
    # name the edition the scores were recorded from, not today's
    sources = sorted({f"{r['index']} (read {r.get('as_of', '?')})"
                      for r in (meta.get("intelligence") or {}).values()
                      if r.get("index")})
    lines.append(f"Highest intelligence first. AA = "
                 f"{'; '.join(sources) or 'no scores recorded for this run'} "
                 f"for the model and effort the seat ran; ≤ means it was "
                 f"measured at a higher effort, stale means the pin had moved.")
    lines.append("")
    ranked = sorted(seat_results, key=lambda n: (
        _run_intelligence(meta, n)[0] is None,
        -(_run_intelligence(meta, n)[0] or 0)))
    stats = track_record(reg)
    for name in ranked:
        res = seat_results[name]
        flag = "" if res["ok"] else "  **[no findings block]**"
        track = track_label(name, stats)
        track = "" if track == "-" else f", track {track}"
        lines.append(f"- **{name}** (AA {_run_intelligence(meta, name)[1]}"
                     f"{track}, exit {res['exit']}, {res['chars']} chars, "
                     f"{res['findings']} findings): {res['verdict']}{flag}")
    esc = meta.get("escalation") or {}
    if esc.get("decided"):
        lines.append("")
        lines.append(f"Escalation: {'seated the full roster' if esc.get('escalated') else 'stopped at the quick tier'}"
                     f" -- {esc.get('reason')}.")
    lines.append("")
    if no_vote:
        lines.append("Silent on findings (hard rule 8 -- silence is NOT "
                     f"agreement, count these as no vote): {', '.join(no_vote)}")
        lines.append("")
    lines.append("A finding only one lineage raised is often the real one -- "
                 "read that seat's file in full. Reproduce every contested "
                 "claim yourself before acting, and state your own conclusion; "
                 "never relay a seat verbatim.")
    lines.append("")
    lines.append("Weigh by intelligence, but hear every family: when lineages "
                 "disagree on a judgment call and neither shows a check, lean "
                 "toward the higher AA score. A lower-scoring family sits on "
                 "the panel because it reasons differently, so read its "
                 "distinct points; a finding with a concrete repro outranks "
                 "any score.")
    _atomic_write(outdir / "_summary.md", "\n".join(lines) + "\n")


def _error_tail(text, limit=200):
    """A failed seat's actual complaint, with CLI chrome stripped.

    The gemini CLI prepends a TTY color warning to every error and appends a
    node stack trace, so the naive first-200-chars made every quota failure
    look like a terminal problem (run 20260813_182034).
    """
    lines = [ln for ln in (text or "").splitlines()
             if ln.strip()
             and not ln.startswith("Warning:")
             and not re.match(r"\s+at ", ln)]
    return "\n".join(lines).strip()[:limit]


def _health_path():
    return Path(os.environ.get("PANEL_RUNS")
                or Path.home() / "panel-runs") / "_seat-health.json"


_HEALTH_LOCK = threading.Lock()


def record_seat_health(name, code, out):
    """Persist each seat's LAST outcome across runs, so --list-seats and
    panel_seats can say a lane is quota-dead before a caller waits 20 minutes
    to rediscover it. A hint, never a crash: failures here are swallowed."""
    try:
        with _HEALTH_LOCK:
            path = _health_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Runs in separate processes share this file: lock across
            # processes where the OS allows, and replace it atomically, so a
            # torn write can never leave invalid JSON behind.
            with open(path.with_suffix(".lock"), "a") as lock:
                _lock_file(lock)
                try:
                    data = json.loads(path.read_text())
                    if not isinstance(data, dict):
                        data = {}
                except (OSError, ValueError):
                    data = {}  # missing or corrupt: start over, never stay stuck
                entry = {"ok": code == 0,
                         "ts": datetime.datetime.now().isoformat(
                             timespec="seconds")}
                if code != 0:
                    entry["exit"] = code
                    entry["error"] = _error_tail(out)
                    entry["kind"] = failure_kind(code, out)
                data[name] = entry
                _atomic_write(path, json.dumps(data, indent=2))
    except Exception:  # noqa: BLE001
        pass


def seat_health():
    """{seat: last outcome} from the health file, {} when there is none."""
    try:
        data = json.loads(_health_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# A lane whose last call died on billing or login will die the same way on the
# next run: pplx failed 13 runs in a row on an empty quota while still seated.
# Such a seat sits out implicit rosters for a while, then gets tried again (a
# skipped seat records nothing, so the window, not a success, ends the skip).
DEAD_LANE_HOURS = 24
_QUOTA = re.compile(r"quota|insufficient_quota|credit balance|out of credit|"
                    r"add credits|billing|\b402\b|usage limit|out of usage",
                    re.I)
_AUTH = re.compile(r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|not logged in|"
                   r"log ?in again|/login|authenticat|invalid api key|"
                   r"api key (?:is )?(?:missing|invalid)|"
                   r"incorrect api key|provide an api key", re.I)


def failure_kind(code, out):
    """'quota', 'auth' or 'other' for a failed seat call. Quota wins: a
    401 that says 'exceeded your current quota' is a billing problem."""
    if code in (-1, NOT_STARTED):  # a timeout or a stop: nothing about the lane
        return "other"
    # the error sits at the end; a failed seat's partial answer about, say,
    # a payment form must not read as a billing failure
    text = (out or "")[-1500:]
    if _QUOTA.search(text):
        return "quota"
    if _AUTH.search(text):
        return "auth"
    return "other"


def dead_lanes(names, health=None, now=None):
    """{seat: reason} for seats whose last outcome was a quota or login
    failure inside the DEAD_LANE_HOURS window."""
    health = seat_health() if health is None else health
    now = now or datetime.datetime.now()
    out = {}
    for name in names:
        last = health.get(name) or {}
        kind = last.get("kind") or (failure_kind(last.get("exit"),
                                                 last.get("error", ""))
                                    if not last.get("ok", True) else None)
        if last.get("ok", True) or kind not in ("quota", "auth"):
            continue
        try:
            when = datetime.datetime.fromisoformat(last.get("ts", ""))
        except ValueError:
            continue
        if now - when < datetime.timedelta(hours=DEAD_LANE_HOURS):
            label = "out of quota/credit" if kind == "quota" else "login/auth failing"
            out[name] = f"{label} since {last['ts']}"
    return out


def collect_run(outdir, meta, registry=None):
    """Re-read every seat file in a run dir; rebuild _summary.md and
    _result.json. The JSON is the machine-readable contract: any orchestrator,
    in any language, can read a run without parsing markdown."""
    reg = registry if registry is not None else REGISTRY
    seat_results, clusters, no_vote, pending = gather_run(outdir, meta, reg)
    assign_ids(clusters)
    rows, merge_note = apply_merge(clusters, _load_merge(outdir))
    render_summary(outdir, meta, seat_results, rows, no_vote, reg, pending,
                   merge_note)
    result = {
        "run": meta,
        "dir": str(outdir),
        # an escalating run that has not decided may still seat a tier
        "complete": not pending and (
            not meta.get("escalation")
            or bool(meta["escalation"].get("decided"))),
        "failed": {n: {"exit": r["exit"],
                       "error": _error_tail(Path(r["path"]).read_text(
                           errors="replace").split("\n\n", 1)[-1])}
                   for n, r in seat_results.items() if r["exit"] != 0},
        "seats": seat_results,
        "no_vote": no_vote,
        # pending = still running; refill = what a resume would re-run
        "pending": pending,
        "refill": [n for n in meta.get("planned", [])
                   if seat_status(outdir, n) != (True, 0)],
        "merge": merge_note,
        "findings": [_finding_record(c, meta) for c in rows],
    }
    _atomic_write(outdir / "_result.json", json.dumps(result, indent=2))
    return result


def _finding_record(c, meta):
    return {"id": finding_id(c),
            "lineages": c["lineages"],
            # proposed by the merge judge, never counted in `lineages`
            "likely_lineages": c.get("likely_lineages", []),
            # a moved row keeps its own severity, places and wording
            "likely_merged": [_finding_record(m, meta)
                              for m in c.get("likely", [])],
            "seats": c["seats"],
            "severity": c["severity"],
            # only an UNDISPUTED location; seats guess paths
            "file": c["files"][0] if len(c["files"]) == 1 else None,
            "locations_cited": c["locations"],
            "claim": c["rep"]["claim"],
            "top_intelligence": _top_intelligence(c["seats"], meta),
            "variants": c["claims"]}


def finding_id(cluster):
    """A short stable handle for one consensus row, so verdicts can name a
    finding across status polls: the representative seat, claim and place
    (one seat may report the same sentence about two files). assign_ids
    makes any remaining collision unique."""
    if cluster.get("id"):
        return cluster["id"]
    seed = (f"{cluster['seats'][0]}\0{cluster['rep']['claim']}\0"
            f"{_where(cluster['rep'])}")
    return hashlib.sha1(seed.encode()).hexdigest()[:8]


def assign_ids(clusters):
    """Give every cluster a unique id, in clustering order."""
    seen = {}
    for c in clusters:
        base = finding_id({**c, "id": None})
        n = seen.get(base, 0) + 1
        seen[base] = n
        c["id"] = base if n == 1 else f"{base}-{n}"
    return clusters


def cluster_key(clusters):
    """Fingerprint of everything the merge judge was shown, so a cached
    judgement is applied only to those exact rows: a refill that changes a
    row's seats, place or severity makes it stale."""
    seed = "\n".join(
        f"{','.join(c['seats'])}\0{c['rep']['claim']}\0{c['where']}\0"
        f"{c['severity']}" for c in clusters)
    return hashlib.sha1(seed.encode()).hexdigest()


def gather_run(outdir, meta, registry=None):
    """(seat_results, clusters, no_vote, pending) straight from the seat
    files, before any merge judge has spoken."""
    reg = registry if registry is not None else REGISTRY
    seat_results, per_seat, no_vote = {}, {}, []
    for name in meta.get("planned", []):
        f = outdir / f"{name}.md"
        landed, code = seat_status(outdir, name)
        if not landed:
            continue
        body = f.read_text(errors="replace")
        findings, ok = parse_findings(body)
        seat_results[name] = {"exit": code, "chars": len(body),
                              "verdict": _verdict_line(body),
                              "findings": len(findings), "ok": ok,
                              "repaired": REPAIR_MARK in body,
                              "lineage": lineage_of(name, reg),
                              "intelligence": _run_intelligence(meta, name)[0],
                              "path": str(f)}
        # Only a seat that exited 0 votes. A crashed or timed-out seat may
        # still have printed a block (partial output is kept for reading),
        # but the summary lists it as FAILED, and it must not also count.
        if ok and code == 0:
            per_seat[name] = findings
        elif code == 0:
            no_vote.append(name)
    ordered = {n: per_seat[n] for n in reg if n in per_seat}
    ordered.update({n: v for n, v in per_seat.items() if n not in ordered})
    clusters = cluster_findings(ordered, reg)
    # "pending" means STILL RUNNING -- a seat that landed with an error is
    # finished, just useless. Conflating the two makes a run look permanently
    # incomplete and hides the fact that a seat needs fixing, not waiting.
    pending = [n for n in meta.get("planned", [])
               if not seat_status(outdir, n)[0]]
    return seat_results, clusters, no_vote, pending


# ------------------------------------------------------------ merge judge
# Lexical clustering misses paraphrases (one claim, worded six ways by six
# lineages, showed as 4 + 1 + 1, audit 2026-11-09). A judge seat reads
# the rows once a run lands and proposes which state the same claim. Its
# proposals are shown as `+N likely`, never added to a row's counted
# lineages, and the guards that keep clustering honest still apply.

def load_merge_config(path=None):
    path = Path(path) if path else REGISTRY_PATH
    data = json.loads(path.read_text()) if path.exists() else {}
    return {k: v for k, v in data.get("merge", {}).items()
            if not k.startswith("_")}


MERGE_CONFIG = load_merge_config()
MERGE_FILE = "_merge.json"
MERGE_TEMPLATE = """You are checking findings from independent reviewers of the same question
for duplicates. Do not use tools or read files.

Two items are the SAME only when they assert the same specific fact, defect,
risk or recommendation about the same thing: worded differently, at a
different level of detail, or with numbers rounded differently. Items that
only share a topic, a file, or a cause-and-effect link are NOT the same.
Items that disagree with each other are NOT the same. When unsure, leave them
apart: a false merge overstates how many reviewers agree.

Reply with only this JSON and nothing else:
{"groups": [[3, 17], [5, 9, 22]]}
listing the numbers of items that are the same as each other. Leave out every
item that has no duplicate. Reply {"groups": []} when none are duplicates.

ITEMS:
"""


def _load_merge(outdir):
    try:
        data = json.loads((Path(outdir) / MERGE_FILE).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _parse_groups(out, count):
    """0-based, disjoint groups of 2+ items from the judge's reply, or None
    when the reply holds no usable groups JSON."""
    at = (out or "").find('"groups"')
    raw = _extract_json_array(out[at:]) if at >= 0 else None
    try:
        groups = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None
    if not isinstance(groups, list):
        return None
    seen, clean = set(), []
    for group in groups:
        if not isinstance(group, list):
            continue
        idx = []
        for i in group:
            if (isinstance(i, int) and not isinstance(i, bool)
                    and 1 <= i <= count and i - 1 not in seen
                    and i - 1 not in idx):
                idx.append(i - 1)
        if len(idx) >= 2:
            seen.update(idx)
            clean.append(idx)
    return clean


def _may_merge(a, b):
    """The clustering guards, applied to a judge's proposal against every
    member of both rows: one seat's two rows stay two claims, different
    places stay different, and a claim and its negation are a dispute."""
    if set(a["seats"]) & set(b["seats"]):
        return False
    for x in a.get("members") or [a["rep"]]:
        for y in b.get("members") or [b["rep"]]:
            if (_place_relation(x, y) == "different"
                    or _disputes(x["claim"], y["claim"])):
                return False
    return True


_SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "unspecified": 3}


def apply_merge(clusters, merge):
    """(rows, note). Rows are the clusters with the judge's accepted
    proposals folded in as `likely`; absorbed clusters leave the table but
    stay attached to the row they joined. note says what happened."""
    if not merge:
        return clusters, None
    note = {k: merge.get(k) for k in ("judge", "model", "ts", "status")}
    if merge.get("key") != cluster_key(clusters):
        note["status"] = "stale"  # judged on other rows: never misapply it
        return clusters, note
    if merge.get("status") != "applied":
        note["error"] = merge.get("error")
        return clusters, note
    absorbed, accepted, refused = set(), 0, 0
    for group in merge.get("groups") or []:
        members = [i for i in group if 0 <= i < len(clusters)
                   and i not in absorbed]
        if len(members) < 2:
            continue
        primary = min(members, key=lambda i: (
            -len(clusters[i]["lineages"]),
            _SEV_ORDER[clusters[i]["severity"]], i))
        head = clusters[primary]
        for i in members:
            if i == primary:
                continue
            if not all(_may_merge(head_or, clusters[i])
                       for head_or in [head] + head.get("likely", [])):
                refused += 1
                continue
            head.setdefault("likely", []).append(clusters[i])
            absorbed.add(i)
            accepted += 1
    rows = [c for i, c in enumerate(clusters) if i not in absorbed]
    for c in rows:
        extra = {lin for m in c.get("likely", []) for lin in m["lineages"]}
        c["likely_lineages"] = sorted(extra - set(c["lineages"]))
    rows.sort(key=lambda c: (-len(c["lineages"]), -len(c["likely_lineages"]),
                             _SEV_ORDER[c["severity"]]))
    note.update(merged=accepted, refused=refused)
    return rows, note


def semantic_merge(outdir, meta, registry, repo):
    """Ask the merge judge once, after the seats land; cache its answer in
    _merge.json keyed to the exact rows it saw. Status polls only read it."""
    judge = MERGE_CONFIG.get("judge")
    if not judge or judge not in PANEL:
        return None
    _, clusters, _, _ = gather_run(outdir, meta, registry)
    if len(clusters) < 2:
        return None
    items = clusters[:int(MERGE_CONFIG.get("max_items", 200))]
    prompt = MERGE_TEMPLATE + "\n".join(
        f"[{i + 1}] (where: {c['where']}) {c['rep']['claim']}"
        for i, c in enumerate(items))
    record = {"key": cluster_key(clusters), "judge": judge,
              "model": pinned_model(registry.get(judge, {})),
              "ts": datetime.datetime.now().isoformat(timespec="seconds"),
              "items": len(items)}
    try:
        code, out = PANEL[judge](prompt, repo,
                                 int(MERGE_CONFIG.get("timeout", 600)))
    except Exception as exc:  # noqa: BLE001 - no merge is a fine outcome
        code, out = -5, f"judge raised: {exc!r}"
    groups = _parse_groups(out, len(items)) if code == 0 else None
    if groups is None:
        record.update(status="failed", groups=[],
                      error=(_error_tail(out) if code != 0
                             else "no usable groups JSON in the reply"))
    else:
        record.update(status="applied", groups=groups)
    _atomic_write(Path(outdir) / MERGE_FILE, json.dumps(record, indent=2))
    print(f"[merge] {judge}: {record['status']}, "
          f"{len(record['groups'])} group(s) proposed over {len(items)} rows",
          flush=True)
    return record


def merge_if_needed(outdir, meta, registry, repo):
    """Judge the rows unless the cached judgement already covers exactly
    these rows (a resume that changed nothing must not pay again)."""
    if not meta.get("merge", True) or _STOPPING.is_set():
        return None
    _, clusters, _, _ = gather_run(outdir, meta, registry)
    cached = _load_merge(outdir)
    if (cached and cached.get("status") == "applied"
            and cached.get("key") == cluster_key(clusters)):
        return cached
    return semantic_merge(outdir, meta, registry, repo)


# ------------------------------------------------------------- escalation
# Most questions do not need 14 frontier seats for 25 minutes. --escalate
# asks the quick tier first and seats the slow frontier seats only when the
# quick answers leave doubt -- "accept when confident, escalate when unsure".
ESCALATE_MIN_LINEAGES = 4


def escalation_reason(result, meta):
    """Why the quick tier is not enough, or None when it is. Deterministic:
    too few lineages voted, a high-severity claim only one COUNTED lineage
    raised (the merge judge's `likely` proposals are never counted, here
    either), or a split CLEAN/FINDINGS verdict on a diff."""
    voting = [n for n, r in result["seats"].items()
              if r["exit"] == 0 and r["ok"]]
    lineages = {result["seats"][n]["lineage"] for n in voting}
    if len(lineages) < ESCALATE_MIN_LINEAGES:
        return f"only {len(lineages)} lineage(s) voted"
    rows = [r for f in result["findings"]
            for r in [f] + list(f.get("likely_merged") or [])]
    lonely = [f for f in rows if f["severity"] == "high"
              and len(f["lineages"]) < 2]
    if lonely:
        return (f"{len(lonely)} high-severity finding(s) raised by one "
                f"lineage only")
    if meta.get("mode") == DIFF_MODE:
        verdicts = set()
        for n in voting:
            line = result["seats"][n]["verdict"].upper()
            if "CLEAN" in line:
                verdicts.add("CLEAN")
            elif "FINDINGS" in line:
                verdicts.add("FINDINGS")
        if len(verdicts) > 1:
            return "the quick tier split between CLEAN and FINDINGS"
    return None


def run_escalating(outdir, meta, rest, prompts, repo, timeout, registry,
                   to_run=None):
    """Run the quick tier (or, on a resume, the quick seats still owed),
    decide, then seat `rest` only on doubt. A quick tier with no seat left
    (all skipped or dead) is itself a reason to escalate."""
    quick = meta["escalation"]["quick"]
    to_run = quick if to_run is None else to_run
    if to_run:
        execute(outdir, meta, to_run, prompts, repo, timeout, registry)
    if _STOPPING.is_set():  # stopped: leave it undecided for a resume
        return collect_run(outdir, meta, registry)
    if quick:
        merge_if_needed(outdir, meta, registry, repo)
        reason = escalation_reason(collect_run(outdir, meta, registry), meta)
    else:
        reason = "no quick-tier seat could run"
    meta["escalation"].update(decided=True, escalated=bool(reason and rest),
                              reason=reason or "the quick tier agreed")
    if reason and rest:
        print(f"[escalate] {reason}: seating {', '.join(rest)}", flush=True)
        execute(outdir, meta, rest, prompts, repo, timeout, registry)
        merge_if_needed(outdir, meta, registry, repo)
    else:
        print(f"[escalate] stopping at the quick tier: "
              f"{meta['escalation']['reason']}", flush=True)
        meta["planned"] = list(quick)
        _atomic_write(outdir / RUN_META, json.dumps(meta, indent=2))
    return collect_run(outdir, meta, registry)


# ---------------------------------------------------------- track record
# Every hosted seat's exam grade reads "?": each model bump wipes it and
# nothing re-grades. The caller already reproduces findings before acting
# (hard rule 3); recording that outcome gives each seat a measured record on
# real work, per model, that no bump can silently carry over.
VERDICTS_FILE = "_verdicts.json"
TRACK_FILE = "_track-record.jsonl"


def _runs_root():
    return Path(os.environ.get("PANEL_RUNS") or Path.home() / "panel-runs")


def record_verdict(run_dir, finding, verdict, note="", registry=None):
    """Record that a finding was reproduced (confirmed) or ruled out
    (rejected). Writes the run's _verdicts.json and appends one line to the
    cross-run track record. Returns the entry."""
    if verdict not in ("confirmed", "rejected"):
        raise ValueError("verdict must be confirmed or rejected")
    # one identity per run however the path is spelled (".", a symlink)
    run_dir = Path(run_dir).resolve()
    meta = json.loads((run_dir / RUN_META).read_text())
    result = collect_run(run_dir, meta, registry)
    fid = str(finding).lstrip("#")
    hit = None
    for row in result["findings"]:
        if row["id"] == fid:
            hit = row
            break
        for m in row["likely_merged"]:
            if m["id"] == fid:  # a moved row is still its own seats' claim
                hit = m
                break
        if hit:
            break
    if hit is None:
        raise ValueError(f"no finding #{fid} in {run_dir}; ids are in the "
                         f"first column of _summary.md")
    models = meta.get("models") or {}
    entry = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
             "run": str(run_dir), "finding": fid, "verdict": verdict,
             "note": note, "claim": hit["claim"], "seats": hit["seats"],
             "lineages": hit["lineages"], "solo": len(hit["lineages"]) == 1,
             "severity": hit.get("severity"), "mode": meta.get("mode"),
             "models": {s: models.get(s) for s in hit["seats"]}}
    path = run_dir / VERDICTS_FILE
    with open(run_dir / ".verdicts.lock", "a") as lock:
        _lock_file(lock)  # read-modify-write: concurrent callers serialize
        try:
            verdicts = json.loads(path.read_text())
        except (OSError, ValueError):
            verdicts = {}
        verdicts[fid] = entry
        _atomic_write(path, json.dumps(verdicts, indent=2))
    track = _runs_root() / TRACK_FILE
    track.parent.mkdir(parents=True, exist_ok=True)
    with open(track, "a") as fh:
        _lock_file(fh)
        fh.write(json.dumps(entry) + "\n")
    return entry


def track_record(registry=None):
    """{seat: {confirmed, rejected, solo_confirmed, prior}} over every
    recorded verdict, latest verdict per finding. Counts belong to the model
    that answered: verdicts on an older pin are only tallied as `prior`."""
    reg = registry if registry is not None else REGISTRY
    latest = {}
    try:
        for line in (_runs_root() / TRACK_FILE).read_text().splitlines():
            try:
                e = json.loads(line)
                latest[(e["run"], e["finding"])] = e
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        return {}
    stats = {}
    for e in latest.values():
        for seat in e.get("seats") or []:
            s = stats.setdefault(seat, {"confirmed": 0, "rejected": 0,
                                        "solo_confirmed": 0, "prior": 0})
            model = (e.get("models") or {}).get(seat)
            current = seat_identity(reg.get(seat, {}))
            if model and current and model != current:
                s["prior"] += 1
                continue
            if e.get("verdict") in ("confirmed", "rejected"):
                s[e["verdict"]] += 1
                if e["verdict"] == "confirmed" and e.get("solo"):
                    s["solo_confirmed"] += 1
    return stats


def track_label(seat, stats):
    """'7/9 confirmed (2 solo)', or '-' with nothing recorded."""
    s = (stats or {}).get(seat)
    total = (s["confirmed"] + s["rejected"]) if s else 0
    if not total:
        return "-"
    solo = f", {s['solo_confirmed']} solo" if s["solo_confirmed"] else ""
    return f"{s['confirmed']}/{total} confirmed{solo}"


def seat_status(outdir, name):
    """(landed, exit_code) for a seat file already on disk. A file without
    the header panel writes is not a landed answer (a hand edit, a crash
    mid-write before writes were atomic): resume re-runs it."""
    f = Path(outdir) / f"{name}.md"
    if not f.exists():
        return False, None
    head = f.read_text(errors="replace")[:200]
    m = re.match(r"# \S+ \(exit (-?\d+)\)", head)
    return (True, int(m.group(1))) if m else (False, None)


def plan_seats(args, registry):
    """Which seats run, honouring --preset / --seats / --skip / --include-local."""
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    only = {s.strip() for s in args.seats.split(",") if s.strip()}
    preset = getattr(args, "preset", None)
    if preset and only:
        raise SystemExit("--preset and --seats are mutually exclusive")
    if preset:
        if preset not in PRESETS:
            raise SystemExit(
                f"unknown preset '{preset}' (have: {', '.join(sorted(PRESETS))})")
        unknown = set(PRESETS[preset]) - set(PANEL)
        if unknown:
            raise SystemExit(
                f"preset '{preset}' names unknown seat(s): {', '.join(sorted(unknown))}")
        # preset listing is explicit selection: local seats run without --include-local
        chosen = [n for n in PRESETS[preset]]
    elif only:
        unknown = only - set(PANEL)
        if unknown:
            raise SystemExit(f"unknown seat(s): {', '.join(sorted(unknown))}")
        chosen = [n for n in PANEL if n in only]
    else:
        chosen = [n for n in PANEL
                  if registry.get(n, {}).get("default")
                  or (args.include_local and registry.get(n, {}).get("local"))]
    return [n for n in chosen if n not in skip]


def billing_for(seat, registry):
    """Unrecognized or absent billing metadata fails closed."""
    value = registry.get(seat, {}).get("billing")
    return value if value in ("subscription", "metered", "credit") else "unknown"


def filter_billing(names, args, registry):
    """Apply the same billing policy to seats, escalation and the merge judge."""
    if not args.no_metered:
        return list(names)
    allowed = set(args.allow_unknown)
    kept = []
    for name in names:
        billing = billing_for(name, registry)
        if billing == "subscription":
            kept.append(name)
        elif billing == "unknown" and name in allowed:
            print(f"WARNING: [{name}] billing is unverified; allowed by "
                  "--allow-unknown", flush=True)
            kept.append(name)
        else:
            print(f"[{name}] dropped by --no-metered: billing={billing}",
                  flush=True)
    return kept


# Seat files land just after a timeout while process cleanup finishes. A
# fixed minute allows that overhead without accepting much later rewrites.
STATS_MTIME_GRACE = 60


def stats_for(seat, runs_root):
    """Nearest-rank minutes from run start to seat-file mtime, across repos.

    Includes failed attempts as well as successful ones. This is elapsed
    completion time (including queueing/repair), not model execution time.
    Resume/edit mtimes beyond timeout + 60 seconds are excluded. Earlier
    rewrites and model changes cannot be distinguished in legacy metadata.
    """
    samples = []
    if not re.fullmatch(r"[\w-]+", seat):
        return {"n": 0, "p50": None, "p95": None}
    for path in Path(runs_root).expanduser().glob("*/*/run.json"):
        try:
            meta = json.loads(path.read_text())
            if not isinstance(meta, dict):
                continue
            planned = meta.get("planned")
            if not isinstance(planned, list) or seat not in planned:
                continue
            ts = meta["ts"]
            if not isinstance(ts, str) or not re.fullmatch(r"\d{8}_\d{6}", ts):
                continue
            if not re.fullmatch(re.escape(ts) + r"(?:_\d+)?", path.parent.name):
                continue
            # Naive datetime.timestamp uses the machine's local timezone,
            # matching the local strftime timestamp written by panel.
            start = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S").timestamp()
            timeout = meta["timeout"]
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or timeout <= 0):
                continue
            answer = path.parent / f"{seat}.md"
            if not answer.is_file():
                continue
            elapsed = answer.stat().st_mtime - start
            if math.isfinite(elapsed) and 0 <= elapsed <= timeout + STATS_MTIME_GRACE:
                samples.append(elapsed / 60)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            continue  # missing, partial or malformed history is not a sample
    samples.sort()
    n = len(samples)
    return {"n": n,
            "p50": samples[math.ceil(n * .50) - 1] if n else None,
            "p95": samples[math.ceil(n * .95) - 1] if n else None}


def print_estimate(names, registry, runs_root, rest=(), merge=False):
    print("Estimate only: no seats launched; no run directory created.")
    print("seat                   lineage       billing       runs  P50 min  P95 min")
    observed = []
    for name in names:
        stats = stats_for(name, runs_root)
        history = (f"{stats['p50']:7.2f}  {stats['p95']:7.2f}" if stats["n"]
                   else "no history")
        print(f"{name:22} {lineage_of(name, registry):13} "
              f"{billing_for(name, registry):12} {stats['n']:5}  {history}")
        if stats["n"]:
            observed.append((stats["p95"], name))
    metered = [n for n in names if billing_for(n, registry) == "metered"]
    print(f"metered seats: {len(metered)} ({', '.join(metered) or 'none'})")
    if observed:
        slowest, name = max(observed)
        print(f"slowest P95: {slowest:.2f} min ({name}; seats with history only)")
    else:
        print("slowest P95: no history")
    if rest:
        print(f"conditional escalation seats (included above): {', '.join(rest)}")
    judge = MERGE_CONFIG.get("judge")
    if merge and judge in PANEL:
        print(f"conditional merge judge: {judge}, "
              f"billing={billing_for(judge, registry)} (outside seat table)")
    print("Approximate elapsed completion times, including failed attempts; "
          "nearest-rank percentiles. Files later than timeout + 60s are ignored.")
    print("History spans tasks and model versions; queueing, retries and edits "
          "can affect mtimes. Slowest P95 is not a whole-run deadline.")


_MEDIA_KINDS = {".png": "image", ".jpg": "image", ".jpeg": "image",
                ".gif": "image", ".webp": "image", ".bmp": "image",
                ".tiff": "image",
                ".mp4": "video", ".mov": "video", ".webm": "video",
                ".avi": "video", ".mkv": "video",
                ".mp3": "audio", ".wav": "audio", ".m4a": "audio",
                ".flac": "audio",
                ".pdf": "document", ".zip": "archive", ".gz": "archive"}


def _attach_file(f):
    """One --file -> its slice of the prompt.

    Text embeds. Media and anything undecodable is attached as a REFERENCE --
    path, size, kind -- never as bytes: dumping an mp4 through read_text()
    kills the whole prompt build, and image bytes in a text prompt help no
    seat. A seat with file access (codex) can open the path; the rest are
    told to judge from the other context.
    """
    kind = _MEDIA_KINDS.get(f.suffix.lower())
    if kind is None:
        try:
            return (f"\n--- ATTACHED FILE: {f} ---\n"
                    f"{f.read_text()}\n--- END {f.name} ---\n")
        except UnicodeDecodeError:
            kind = "binary"
    return (f"\n--- ATTACHED {kind.upper()} (by reference): {f} "
            f"({f.stat().st_size} bytes) ---\n"
            f"The {kind} itself is not inlined. If you can read files, open "
            f"it at that path; otherwise judge from the rest of the context "
            f"and say you could not view it.\n--- END {f.name} ---\n")


def _read_residuals(repo, explicit, disabled):
    """Accepted residuals for this repo -- the panel's memory across rounds.

    Hard rule 5: anything consciously not-fixed has to ride along in the
    context or every round rediscovers it and buries the new findings.
    """
    if disabled:
        return ""
    path = Path(explicit) if explicit else Path(repo) / ".panel" / "residuals.md"
    if not path.exists():
        return ""
    text = path.read_text().strip()
    if not text:
        return ""
    return ("\n\nACCEPTED RESIDUALS -- reviewed in an earlier round and "
            "consciously accepted. Do NOT re-report these; report only "
            "something new, or a concrete change in their risk:\n" + text)


def _prior_findings(prev_dir):
    """Compact one-liners from a previous round, so findings narrow. Read
    from _result.json; the summary table is the fallback for old runs."""
    items = []
    try:
        result = json.loads((Path(prev_dir) / "_result.json").read_text())
        for f in result.get("findings") or []:
            where = f.get("file") or (
                ("disputed: " + " / ".join(f["locations_cited"][:2]))
                if f.get("locations_cited") else "-")
            items.append(f"  - [{f.get('severity')}] {where} {f.get('claim')}")
    except (OSError, ValueError, KeyError, TypeError):
        items = []
    summary = Path(prev_dir) / "_summary.md"
    if not items and summary.exists():
        for row in summary.read_text().splitlines():
            if not row.startswith("| ") or row.startswith("| ---"):
                continue
            # claims escape their own pipes as \| when the table is written
            cells = [c.strip().replace("\\|", "|")
                     for c in re.split(r"(?<!\\)\|", row.strip().strip("|"))]
            if cells and cells[0].startswith("#"):
                cells = cells[1:]  # id column (2026-11-09 layout)
            if len(cells) >= 4 and cells[0].split(" ")[0].isdigit():
                items.append(f"  - [{cells[1]}] {cells[2]} {cells[3]}")
    if not items:
        return ""
    return ("\n\nREPORTED IN AN EARLIER ROUND (re-report one of these only if "
            "you can show it is still live in the code you were given):\n"
            + "\n".join(items[:20]))


def _latest_run(root):
    runs = sorted(p for p in Path(root).glob("*") if (p / RUN_META).exists())
    return runs[-1] if runs else None


def default_out(repo):
    return Path(os.environ.get("PANEL_RUNS") or Path.home() / "panel-runs") \
        / Path(repo).resolve().name


def repair_block(name, answer, repo, timeout, unit):
    """The answer plus a recovered findings block, or the answer unchanged
    when the follow-up fails. The original answer is never rewritten."""
    prompt = REPAIR_TEMPLATE.format(unit=unit,
                                    answer=answer[-REPAIR_MAX_CHARS:])
    try:
        code, out = PANEL[name](prompt, repo, min(timeout, REPAIR_TIMEOUT))
    except Exception:  # noqa: BLE001 - a failed repair leaves a no vote
        return answer
    findings, ok = parse_findings(out) if code == 0 else ([], False)
    if not ok:
        return answer
    print(f"[{name}] answered without a findings block; recovered "
          f"{len(findings)} finding(s) in a follow-up call", flush=True)
    return (f"{answer}\n\n{REPAIR_MARK}\nFINDINGS_JSON\n"
            f"{json.dumps(findings)}\nEND_FINDINGS_JSON")


def execute(outdir, meta, names, prompts, repo, timeout, registry):
    """Run seats and persist each answer THE MOMENT it lands.

    as_completed, not in-order .result(): with in-order collection one slow
    seat holds every finished seat's file hostage, so killing a long run threw
    away answers that were already paid for and already in memory.
    """
    set_active_mode(meta.get("mode"))
    # a score belongs to the model that answers: record it now, while this
    # process's registry is the roster actually running the seats
    recorded = meta.setdefault("intelligence", {})
    for name in names:
        recorded[name] = {"score": intelligence_of(name, registry),
                          "label": intelligence_label(name, registry),
                          "index": INTELLIGENCE.get("index"),
                          "as_of": INTELLIGENCE.get("as_of")}
    _CURRENT_RUN.update(outdir=outdir, meta=meta, registry=registry)
    # the model each seat ran, so a verdict credits that model and not
    # whatever seat-refresh pins later
    models = meta.setdefault("models", {})
    for name in names:
        models[name] = seat_identity(registry.get(name, {}))
    # the run's owner: a resume refuses to start while this process lives,
    # or it would pay for the in-flight seats twice and race their files
    meta["pid"] = os.getpid()
    meta["completed"] = False
    _atomic_write(outdir / RUN_META, json.dumps(meta, indent=2))
    serial = [n for n in names if registry.get(n, {}).get("serial")]
    parallel = [n for n in names if n not in serial]

    def _report(name, code, out):
        findings, ok = parse_findings(out)
        status = "ok" if code == 0 else f"exit={code}"
        note = "" if ok else " | no findings block"
        print(f"[{name}] {status} | {_verdict_line(out)} | {len(out)} chars | "
              f"{len(findings)} findings{note} -> {outdir / (name + '.md')}",
              flush=True)

    repair = meta.get("repair", True)
    unit = ("defect" if meta.get("mode") == DIFF_MODE
            else "position, risk or claim")

    def _call(name):
        """Ask one seat and persist its answer HERE, in the worker, the
        moment it lands: a stop signal must never find an answer that had
        returned but was still waiting for the main thread to write it."""
        with _tracked_call():
            try:
                code, out = PANEL[name](prompts.get(name, prompts["_default"]),
                                        repo, timeout)
            except Exception as exc:  # noqa: BLE001 - one seat never kills a run
                code, out = -5, f"seat raised: {exc!r}"
            if code == NOT_STARTED:  # panel stopping: still owed, resumable
                return code, out
            if (repair and code == 0 and out.strip()
                    and not parse_findings(out)[1]):
                # the paid answer is on disk first, under a header that
                # reads as still running, so no status poll calls the run
                # complete while its repair call is out
                _atomic_write(outdir / f"{name}.md",
                              f"# {name} (exit {code}, repairing)\n\n{out}\n")
                out = repair_block(name, out, repo, timeout, unit)
            _atomic_write(outdir / f"{name}.md",
                          f"# {name} (exit {code})\n\n{out}\n")
            record_seat_health(name, code, out)
            return code, out

    if parallel:
        print(f"panel of {len(parallel)} parallel (timeout {timeout}s each): "
              f"{', '.join(parallel)}"
              + (f" | serial after: {', '.join(serial)}" if serial else ""),
              flush=True)
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(parallel)) as pool:
            futures = {pool.submit(_call, n): n for n in parallel}
            for fut in concurrent.futures.as_completed(futures):
                code, out = fut.result()
                _report(futures[fut], code, out)

    for name in serial:
        print(f"[serial] {name} taking its turn...", flush=True)
        code, out = _call(name)
        _report(name, code, out)

    meta["completed"] = True
    _atomic_write(outdir / RUN_META, json.dumps(meta, indent=2))


def grade(rundir, keypath, registry):
    """Score a finished run against a known answer key.

    Turns 'which seats are worth their minutes' from remembered prose into a
    number you can re-measure whenever the models ship new versions.
    """
    rundir = Path(rundir)
    key = json.loads(Path(keypath).read_text())
    defects = key.get("defects", [])
    if not defects:
        raise SystemExit(f"{keypath} has no 'defects' list")
    meta = json.loads((rundir / RUN_META).read_text())

    def matches(finding, defect):
        df = defect.get("file")
        if df and finding.get("file"):
            if Path(df).name.lower() != Path(finding["file"]).name.lower():
                return False
        toks = _tokens(finding["claim"])
        kw = {k.lower() for k in defect.get("keywords", [])}
        if kw & toks:
            return True
        return _jaccard(toks, _tokens(defect.get("desc", ""))) >= 0.3

    rows, per_defect = [], {d["id"]: [] for d in defects}
    for name in meta.get("planned", []):
        f = rundir / f"{name}.md"
        if not f.exists():
            continue
        findings, ok = parse_findings(f.read_text())
        if not ok:
            rows.append((name, 0, len(defects), 0, "no findings block"))
            continue
        hit, matched = set(), set()
        for i, finding in enumerate(findings):
            for d in defects:
                if matches(finding, d):
                    hit.add(d["id"])
                    matched.add(i)
                    per_defect[d["id"]].append(name)
        rows.append((name, len(hit), len(defects),
                     len(findings) - len(matched), ""))

    rows.sort(key=lambda r: (-r[1], r[3]))
    out = [f"# grade: {rundir.name}", "",
           f"answer key: {keypath} ({len(defects)} known defects)", "",
           "| seat | found | of | unmatched | note |",
           "| --- | ---: | ---: | ---: | --- |"]
    for name, found, total, extra, note in rows:
        out.append(f"| {name} | {found} | {total} | {extra} | {note} |")
    out += ["", "## Per defect", ""]
    for d in defects:
        seats = per_defect[d["id"]]
        lin = sorted({lineage_of(s, registry) for s in seats})
        out.append(f"- **{d['id']}** ({len(lin)} lineages): "
                   f"{', '.join(seats) if seats else 'MISSED BY EVERY SEAT'}"
                   f" -- {d.get('desc', '')}")
    out += ["", "Unmatched findings are not automatically false: the key lists "
                "only the defects known when it was written. Read them before "
                "writing a seat off.", ""]
    # written, not printed: the MCP server calls this in-process, and its
    # stdout is the protocol channel
    (rundir / "_grade.md").write_text("\n".join(out) + "\n")
    return rows


def list_seats(registry):
    """Who is wired up on THIS machine, and whether the wiring resolves.

    Wiring that resolves is not a lane that answers: a quota-dead seat has a
    working CLI and a valid key. The last recorded outcome rides along so a
    caller can see a dead lane before waiting 20 minutes on it.
    """
    health = seat_health()
    stats = track_record(registry)
    rows = []
    for name, e in registry.items():
        kind = "cmd" if "cmd" in e else ("http" if "http" in e else "-")
        if kind == "cmd":
            exe = e["cmd"]["argv"][0]
            reach = "configured" if shutil.which(exe) else f"MISSING: {exe}"
            for source in (e["cmd"].get("env_from") or {}).values():
                if reach == "configured" and not load_key(source)[0]:
                    reach = load_key(source)[1]
        elif kind == "http":
            spec = e["http"] if isinstance(e["http"], list) else [e["http"]]
            key, err = load_key(spec[0].get("key_from"))
            reach = "configured" if (key or not spec[0].get("key_from")) else err
        else:
            reach = "unconfigured"
        last = health.get(name)
        if last and not last.get("ok"):
            err = " ".join(last.get("error", "").split())[:80]
            reach += f"  [last fail {last.get('ts', '?')}: {err}]"
        track = track_label(name, stats)
        if track != "-":
            reach += f"  [track {track}]"
        rows.append((name, e.get("lineage", "?"),
                     "on" if e.get("default") else
                     ("local" if e.get("local") else "off"),
                     e.get("grade", "?"), intelligence_label(name, registry),
                     kind, reach))
    width = max(len(r[0]) for r in rows) if rows else 4
    print(f"{'seat'.ljust(width)}  lineage      default  grade  aa     "
          f"via   status")
    for r in rows:
        print(f"{r[0].ljust(width)}  {r[1][:11].ljust(11)}  {r[2].ljust(7)}  "
              f"{r[3].ljust(5)}  {r[4].ljust(5)}  {r[5].ljust(4)}  {r[6]}")
    return rows


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--diff", type=Path, help="code-review mode: diff to review")
    ap.add_argument("--task", help="general mode: the question to answer")
    ap.add_argument("--resume", help="run dir to refill, or 'last'")
    ap.add_argument("--grade", dest="grade_dir", help="run dir to score")
    ap.add_argument("--key", help="answer-key JSON for --grade")
    ap.add_argument("--list-seats", action="store_true",
                    help="show the roster and whether each seat resolves here")
    ap.add_argument("--context", default="none given")
    ap.add_argument("--criteria", "--invariants", dest="criteria",
                    default="none stated; use your judgement")
    ap.add_argument("--file", dest="files", action="append", type=Path,
                    default=[], help="attach a file as context (repeatable); "
                                     "media/binary files are attached by "
                                     "path reference, never inlined")
    ap.add_argument("--out", type=Path, default=None,
                    help="run root (default ~/panel-runs/<repo>)")
    ap.add_argument("--timeout", type=int, default=2400)
    ap.add_argument("--skip", default="")
    ap.add_argument("--preset", default=None,
                    help="named roster from seats.json presets "
                         "(full | providers | trio | free); "
                         "mutually exclusive with --seats")
    ap.add_argument("--seats", default="",
                    help="run ONLY these seats (comma-separated)")
    ap.add_argument("--estimate", action="store_true",
                    help="print billing and historical P50/P95 minutes; "
                         "launch nothing and create no run directory")
    ap.add_argument("--no-metered", action="store_true",
                    help="exclude metered, credit and unknown billing routes")
    ap.add_argument("--allow-unknown", action="append", default=[], metavar="SEAT",
                    help="with --no-metered, allow this unknown-billing seat "
                         "with a warning (repeatable; does not select the seat)")
    ap.add_argument("--include-local", action="store_true",
                    help="also seat the self-hosted models (off by default)")
    ap.add_argument("--repo", type=Path, default=None)
    ap.add_argument("--round", type=int, default=1,
                    help="round number; >1 injects the previous round's "
                         "findings so this round narrows instead of repeating")
    ap.add_argument("--prev", help="explicit previous run dir for --round")
    ap.add_argument("--residuals", help="accepted-residuals file "
                                        "(default <repo>/.panel/residuals.md)")
    ap.add_argument("--no-residuals", action="store_true")
    ap.add_argument("--json", action="store_true",
                    help="print the run's _result.json to stdout when done")
    ap.add_argument("--force", action="store_true",
                    help="with --resume: resume even though the run's "
                         "recorded process still looks alive")
    ap.add_argument("--include-dead", action="store_true",
                    help="seat lanes whose last call failed on quota or "
                         f"login in the past {DEAD_LANE_HOURS}h (left out "
                         "of default/preset rosters otherwise)")
    ap.add_argument("--no-repair", action="store_true",
                    help="do not ask a seat that answered without a "
                         "findings block for the block in a follow-up call")
    ap.add_argument("--no-merge", action="store_true",
                    help="skip the merge judge (rows that likely state the "
                         "same claim are proposed, never counted)")
    ap.add_argument("--escalate", action="store_true",
                    help="run the quick preset first; seat the rest of the "
                         "default roster only when the quick tier leaves a "
                         "one-lineage high finding, too few lineages, or a "
                         "split verdict")
    ap.add_argument("--verdict", dest="verdict_dir",
                    help="run dir whose finding you reproduced; with "
                         "--finding ID --mark confirmed|rejected")
    ap.add_argument("--finding", help="finding id from _summary.md (#abc123)")
    ap.add_argument("--mark", choices=("confirmed", "rejected"))
    ap.add_argument("--note", default="", help="with --verdict: why")
    return ap


def main(argv=None):
    try:
        code = _main(argv)
    finally:
        release_runs()  # the run lock lives exactly as long as the work
    # a stopped run still wrote what it had; say it was stopped, and do not
    # exit before the reaper has killed the whole tree (it may be mid-pass)
    if stop_requested():
        _REAPED.wait(30)
        return 128 + _STOP_SIGNUM[0]
    return code


def _main(argv=None):
    args = build_parser().parse_args(argv)
    registry = REGISTRY
    repo = (args.repo or Path.cwd()).resolve()
    out_root = Path(args.out) if args.out else default_out(repo)

    if args.estimate and (args.resume or args.grade_dir or args.verdict_dir
                          or args.list_seats):
        raise SystemExit("--estimate is for a new plan; do not combine it with "
                         "--resume, --grade, --verdict or --list-seats")
    unknown = set(args.allow_unknown) - set(registry)
    if unknown:
        raise SystemExit(f"--allow-unknown names unknown seat(s): {', '.join(sorted(unknown))}")
    if args.allow_unknown and not args.no_metered and not args.resume:
        raise SystemExit("--allow-unknown requires --no-metered")

    if args.list_seats:
        list_seats(registry)
        return 0

    if args.verdict_dir:
        if not (args.finding and args.mark):
            raise SystemExit("--verdict needs --finding <id> and "
                             "--mark confirmed|rejected")
        try:
            entry = record_verdict(args.verdict_dir, args.finding, args.mark,
                                   args.note, registry)
        except ValueError as exc:
            raise SystemExit(str(exc))
        print(f"recorded #{entry['finding']} {entry['verdict']} for "
              f"{', '.join(entry['seats'])}: {entry['claim']}")
        return 0

    if args.grade_dir:
        if not args.key:
            raise SystemExit("--grade needs --key <answer_key.json>")
        grade(args.grade_dir, args.key, registry)
        print((Path(args.grade_dir) / "_grade.md").read_text(), end="")
        return 0

    # ---- resume: same prompt, same dir, only the seats that owe an answer ----
    if args.resume:
        if args.resume == "last":
            rundir = _latest_run(out_root)
            if rundir is None:
                raise SystemExit(f"no runs under {out_root}")
        else:
            rundir = Path(args.resume)
        # same first line as a fresh run: an MCP caller waits for it
        print(f"run dir: {rundir}", flush=True)
        meta = json.loads((rundir / RUN_META).read_text())
        # Retain a restricted run's policy on resume, including an undecided
        # escalation tier. Filtering must precede renaming any old answers.
        args.no_metered = args.no_metered or meta.get("no_metered", False)
        args.allow_unknown = sorted(set(args.allow_unknown)
                                    | set(meta.get("allow_unknown", [])))
        if args.allow_unknown and not args.no_metered:
            raise SystemExit("--allow-unknown requires --no-metered")
        if not claim_run(rundir) and not args.force:
            raise SystemExit(
                f"run {rundir} is still in progress (pid {meta.get('pid')}): "
                f"another panel process holds its lock, and resuming now "
                f"would pay for its in-flight seats twice. Wait for it.")
        # A seat can fail *because* of the repo it was pointed at (codex
        # refuses a directory that is not a trusted git repo). Without this,
        # every resume repeats the original mistake forever.
        if args.repo is not None:
            meta["repo"] = str(repo)
            _atomic_write(rundir / RUN_META, json.dumps(meta, indent=2))
            print(f"repo overridden for this resume: {repo}", flush=True)
        prompts = {"_default": (rundir / "prompt.md").read_text()}
        codex_file = rundir / "prompt.codex.md"
        if codex_file.exists():
            prompts["codex"] = codex_file.read_text()
        todo = [n for n in meta["planned"]
                if seat_status(rundir, n) != (True, 0)]
        esc = meta.get("escalation") or {}
        pending_rest = esc.get("rest", []) if not esc.get("decided") else []
        candidates = list(dict.fromkeys(todo + pending_rest))
        permitted = set(filter_billing(candidates, args, registry))
        todo = [n for n in todo if n in permitted]
        if args.no_metered:
            meta.update(no_metered=True, allow_unknown=args.allow_unknown)
            meta["planned"] = [n for n in meta["planned"]
                               if n not in candidates or n in permitted]
            if esc:
                esc["quick"] = [n for n in esc.get("quick", [])
                                if n in meta["planned"]]
                esc["rest"] = [n for n in esc.get("rest", [])
                               if n not in candidates or n in permitted]
        if not todo:
            reason = ("no permitted seats need a refill" if args.no_metered
                      else "every seat landed clean")
            print(f"nothing to resume in {rundir}: {reason}")
        else:
            # a stale failure file would read as landed while its refill
            # runs; keep it as <seat>.prev.md so status says "running"
            for name in todo:
                old = rundir / f"{name}.md"
                if old.exists():
                    k, keep = 1, rundir / f"{name}.prev.md"
                    while keep.exists():  # every earlier attempt survives
                        k += 1
                        keep = rundir / f"{name}.prev{k}.md"
                    os.replace(old, keep)
            print(f"resuming {len(todo)} seat(s) in {rundir}: "
                  f"{', '.join(todo)}", flush=True)
        skip = {x.strip() for x in args.skip.split(",") if x.strip()}
        judge = MERGE_CONFIG.get("judge")
        if (args.no_merge or judge in skip
                or (judge and meta.get("merge", True)
                    and not filter_billing([judge], args, registry))):
            meta["merge"] = False
        _atomic_write(rundir / RUN_META, json.dumps(meta, indent=2))
        esc = meta.get("escalation") or {}
        if esc and not esc.get("decided"):
            # interrupted before it decided: refill the quick tier, decide,
            # and only then (maybe) seat the rest -- never seat it blind
            run_escalating(rundir, meta, esc.get("rest", []), prompts,
                           meta.get("repo", str(repo)), args.timeout,
                           registry,
                           to_run=[n for n in todo if n in esc["quick"]])
        elif todo:
            execute(rundir, meta, todo, prompts, meta.get("repo", str(repo)),
                    args.timeout, registry)
        merge_if_needed(rundir, meta, registry, meta.get("repo", str(repo)))
        result = collect_run(rundir, meta, registry)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"\nAll answers in {rundir}/ (see _summary.md) -- synthesize "
                  f"before acting.")
        return 0

    if (args.diff and args.task) or (not args.estimate and not (args.diff or args.task)):
        raise SystemExit("give exactly one of --diff (code review), --task "
                         "(anything else), --resume, --grade or --list-seats")

    if args.escalate and (args.seats or args.preset):
        raise SystemExit("--escalate picks its own tiers (the quick preset, "
                         "then the default roster); drop --seats/--preset")
    names = plan_seats(args, registry)
    rest = []
    if args.escalate:
        skip = {s.strip() for s in args.skip.split(",") if s.strip()}
        quick = [n for n in PRESETS.get("quick", [])
                 if n in PANEL and n not in skip]
        rest = [n for n in names if n not in quick]
        names = quick
    permitted = set(filter_billing(names + rest, args, registry))
    names = [n for n in names if n in permitted]
    rest = [n for n in rest if n in permitted]
    # Seats named with --seats always run; a roster chosen implicitly (the
    # defaults, a preset) leaves out lanes that just died on quota or login.
    skipped = ({} if args.seats or args.include_dead
               else dead_lanes(names + rest))
    names = [n for n in names if n not in skipped]
    rest = [n for n in rest if n not in skipped]
    if not names and not rest and not args.estimate:
        raise SystemExit("no seats selected" + (
            f" (skipped as dead lanes: {', '.join(skipped)}; pass "
            f"--include-dead to try them)" if skipped else ""))
    for name, why in skipped.items():
        print(f"[{name}] skipped: {why} (name it with --seats, or pass "
              f"--include-dead, to try it anyway)", flush=True)

    judge = MERGE_CONFIG.get("judge")
    merge = not args.no_merge and judge not in {
        s.strip() for s in args.skip.split(",") if s.strip()}
    if merge and judge:
        merge = bool(filter_billing([judge], args, registry))
    if args.estimate:
        print_estimate(names + rest, registry, _runs_root(), rest, merge)
        return 0

    prev = args.prev or _latest_run(out_root)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = out_root / ts
    # Two runs inside one second must not land in the same directory and
    # silently overwrite each other's seats: creating the directory IS the
    # claim, so the check and the claim cannot race.
    seq = 1
    while True:
        try:
            outdir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            seq += 1
            outdir = out_root / f"{ts}_{seq}"
    claim_run(outdir)
    # First line of stdout, before any seat runs: an orchestrator that launched
    # this detached needs the run directory immediately, not in 40 minutes.
    print(f"run dir: {outdir}", flush=True)

    context = args.context + _read_residuals(repo, args.residuals,
                                             args.no_residuals)
    if args.round > 1 and prev:
        context += _prior_findings(prev)

    if args.diff:
        shutil.copy(args.diff, outdir / "reviewed.diff")
        block = FINDINGS_BLOCK.format(unit="defect")
        embedded = PROMPT_TEMPLATE.format(
            context=context, invariants=args.criteria,
            findings_block=block, diff=args.diff.read_text())
        codex_prompt = PROMPT_TEMPLATE.format(
            context=context, invariants=args.criteria, findings_block=block,
            diff=f"(read it from the repo; the reviewed diff is also saved at "
                 f"{(outdir / 'reviewed.diff').resolve()})")
        (outdir / "prompt.codex.md").write_text(codex_prompt)
        prompts = {"_default": embedded, "codex": codex_prompt}
        question = str(args.diff)
    else:
        attached = "".join(_attach_file(f) for f in args.files)
        if attached:
            attached = "\nATTACHMENTS:\n" + attached
        embedded = TASK_TEMPLATE.format(
            task=args.task, context=context, criteria=args.criteria,
            attachments=attached,
            findings_block=FINDINGS_BLOCK.format(
                unit="position, risk or claim"))
        prompts = {"_default": embedded}
        question = args.task
    (outdir / "prompt.md").write_text(embedded)

    meta = {"ts": ts, "mode": "diff review" if args.diff else "task",
            "question": question, "repo": str(repo), "round": args.round,
            # an escalating run lists its second tier as planned until it
            # decides, so no status poll calls the run complete too early
            "planned": names + rest, "timeout": args.timeout,
            "completed": False, "repair": not args.no_repair,
            "merge": merge,
            "billing": {n: billing_for(n, registry) for n in names + rest}}
    if args.no_metered:
        meta.update(no_metered=True, allow_unknown=args.allow_unknown)
    if args.escalate:
        meta["escalation"] = {"quick": names, "rest": rest,
                              "decided": False}
    if skipped:
        meta["skipped"] = skipped
    _atomic_write(outdir / RUN_META, json.dumps(meta, indent=2))

    if args.escalate:
        result = run_escalating(outdir, meta, rest, prompts, repo,
                                args.timeout, registry)
    else:
        execute(outdir, meta, names, prompts, repo, args.timeout, registry)
        merge_if_needed(outdir, meta, registry, repo)
        result = collect_run(outdir, meta, registry)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\nAll answers in {outdir}/ (see _summary.md) -- synthesize "
              f"before acting.")
        print(f"Refill any seat that failed with: python3 "
              f"{Path(__file__).name} --resume {outdir}")
        print(f"After reproducing a finding, record it: python3 "
              f"{Path(__file__).name} --verdict {outdir} --finding <id> "
              f"--mark confirmed|rejected")
    return 0


def _on_signal(signum, _frame):
    """A plain flag first: a second signal arriving inside Event.set()
    (which takes a lock) must not re-enter it. The second signal means the
    user will not wait: leave at once."""
    if _SIGNALED[0]:
        os._exit(128 + signum)
    _SIGNALED[0] = True
    _STOP_SIGNUM[0] = signum
    _STOP_SIGNAL.set()


def _reaper():
    _STOP_SIGNAL.wait()
    try:
        reap_live_seats()
    finally:
        _REAPED.set()  # main waits for this before it exits
    # the main thread now unwinds by itself (killed seats return at once);
    # only an uncancellable HTTP call can hold it, and only this long
    time.sleep(HARD_EXIT_AFTER)
    _drain(5)
    run = dict(_CURRENT_RUN)
    if run:
        try:  # still leave a summary of what landed
            if run.get("on_forced_exit"):  # build.py: its scoreboard
                run["on_forced_exit"]()
            else:
                collect_run(run["outdir"], run["meta"], run["registry"])
        except Exception:  # noqa: BLE001 - exiting regardless
            pass
    sys.stdout.flush()
    os._exit(128 + _STOP_SIGNUM[0])


def install_signal_handlers():
    """Seats run in their own process groups, so stopping panel (Ctrl-C, or
    the MCP launcher killing it) must stop them explicitly and at once."""
    threading.Thread(target=_reaper, name="panel-reaper", daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _on_signal)


if __name__ == "__main__":
    install_signal_handlers()
    sys.exit(main())
