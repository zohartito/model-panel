# panel — for any agent

One question → every wired frontier model in parallel → you synthesize.
`skills/panel/panel.py` is a stdlib-only Python CLI; `mcp_server.py` exposes it
via MCP. Wiring lives in `skills/panel/seats.json`. Each run writes
`_result.json`. Day-to-day instructions: [USAGE.md](USAGE.md).

## Install everywhere

Python 3.11+ is required; the installer only registers a compatible interpreter.
Pyenv shims must pass that version probe before replacing a versioned interpreter.

```bash
python3 scripts/panel-install.py --check   # report only
python3 scripts/panel-install.py           # register installed surfaces
```

Installer: backs up, merges, skips absent apps, creates the `panel` launcher.
On macOS/Linux, add `~/.local/bin` to PATH and refresh the shell if needed;
the installer does not edit shell profiles (see README installation steps).
Existing launcher/config replacements are atomic with collision-safe backups;
Codex and project MCP updates preserve fields other than command and args.
CLI registration checks match the exact `panel` name and command/args; stale
registrations are removed and re-added, while `--check` only reports them.
Quoted paths containing ` - ` remain intact when parsing CLI status suffixes.
MCP clients run `python3 <repo>/skills/panel/mcp_server.py`. Codex config:

```toml
[mcp_servers.panel]
command = "python3"
args = ["/absolute/path/to/panel/skills/panel/mcp_server.py"]
```

Claude Code: `claude mcp add panel -- python3 <repo>/skills/panel/mcp_server.py`.
Plugin: `claude plugin marketplace add zohartito/model-panel`, then
`claude plugin install panel@panel` (skill + synthesizer subagent).

## Run and inspect

```bash
PANEL=<repo>/skills/panel/panel.py
python3 "$PANEL" --task "..." --context "..." --criteria "..." --json
python3 "$PANEL" --diff change.diff --repo /path/to/repo --json
python3 "$PANEL" --list-seats
python3 "$PANEL" --estimate                 # billing + P50/P95; launches nothing
python3 "$PANEL" --estimate --no-metered    # subscription routes only
python3 "$PANEL" --task "..." --preset strong
python3 "$PANEL" --task "..." --escalate          # quick tier first, rest on doubt
python3 "$PANEL" --verdict <run> --finding <id> --mark confirmed|rejected
```

`--preset` and `--seats` are mutually exclusive. Named rosters:
- **full**: broad hosted roster, including extra same-family voices.
- **providers**: ten provider voices, including metered HTTP seats.
- **trio**: claude-fable + codex + gemini-carbon.
- **strong**: Fable + Astra + Gemini 4 Carbon + Grok + Kimi Pro + Muse CLI
  + free GLM/DeepSeek Flash. Eight lineages; ungraded.
- **quick**: six-lineage starting tier (no Claude; current timings are not fully measured). `--escalate`
  starts here; the rest seat only on a 1-lineage high, <4 lineages, or split verdict.
- **free**: Flash, Copilot Auto, free Nemotron/MiMo/GLM/DeepSeek and `local-27b`
  (Ollama on this machine).
  Local seats named in presets run without `--include-local`.

Seats routinely take 10–40 minutes. **Never block a foreground call on a panel.**
Use MCP `panel_run` (detached) or `panel bg --preset strong --task "..."`.
CLI alternative: `nohup python3 "$PANEL" --task "..." > panel.log 2>&1 &`.
The first log line gives the run directory.

MCP: `panel_run`, `panel_status`, `panel_summary`, `panel_seat`, `panel_resume`,
`panel_grade`, `panel_seats`, `panel_verdict`. Status: counts and paths; summary:
findings. Read seats only when the summary points to them. `panel_verdict`
records a reproduced finding: each seat's measured track record, per model.
Invalid grading input returns an MCP tool error and leaves the server running.
`panel_seat` requires a safe name in the run.json roster and rejects symlink
answers/manifests and paths outside the run directory.
Optional `--verification-audits audits.json` / MCP `verification_audits` audits
individual seats' verification claims separately from finding truth. Each audit
binds an exact answer quote/span and SHA-256 to a reviewer rationale and run-local
evidence quote/hash. Missing traces are unknown; only supported/contradicted
claims enter the audited-contradiction rate. Latest assessment per claim wins;
other seats' audits survive updates, old model pins remain historical. See
USAGE.md for the schema. Offline coverage: `tests/test_verification_audits.py`.

Answers save as they land. Refill failed/missing seats without repaying successes:
`python3 "$PANEL" --resume last --repo /path/to/repo`.
Runs contain `<seat>.md`, `prompt.md`, `run.json`, `_summary.md`, `_result.json`.
Read the summary/result first. Do not ingest every seat file: delegate a full
read to a subagent or second model using `prompts/synthesize.md` if needed.

Consensus counts **lineages, not seats**: Claude variants are one family,
Grok behind two scaffolds is one, local Gemma and hosted Gemini are one.
A merge judge (Haiku, `merge` block) proposes same-claim rows as `+N likely`:
never counted; same-seat/different-place/negation guards hold (`--no-merge`).
Rows the negation guard kept apart are linked as `contradiction_candidates`
in `_result.json` and "Possible contradictions — verify" atop `_summary.md`:
lexical hints, never verdicts or counts; moved rows keep their id.
A blockless answer gets one follow-up for its block (`--no-repair`). Implicit
rosters skip lanes that failed on quota/login in 24 h (`--include-dead`).
`--file` embeds text; binary/media paths attach by reference (never bytes).
Cmd seats get one Retry-After retry if the remaining budget allows it.
Last outcomes: `$PANEL_RUNS/_seat-health.json`, surfaced in the roster.
**Configured means CLI/key present, not successful live inference.**
`panel_seats` and `--list-seats` share the wiring resolver; MCP rows include
`configured`, `availability`, and a missing-configuration `reason`.

`billing` is `subscription`, `metered`, `credit`, or `unknown` per seat.
`--no-metered` excludes the latter three, including escalation and the merge
judge; `--allow-unknown SEAT` (repeatable) allows only that unknown route with
a warning. This policy persists on resume. Free/local routes use `unknown`
because the billing vocabulary has no free/local category. Account overages
are not checked. `--estimate` reads `$PANEL_RUNS` (default `~/panel-runs`),
creates nothing, and needs no task. It reports nearest-rank P50/P95 elapsed
minutes from local run timestamps to answer mtimes, including failures;
mismatched folders and mtimes outside 0..timeout+60s are ignored. History can
span model versions and edited files. `--include-dead` previews the full
selected roster. Estimates cover new plans, not `--resume`/`--grade`/`--verdict`.

## Build pilot (not a review)

```bash
python3 skills/panel/build.py --task "..." --repo /path/to/repo \
  --test "python3 -m unittest discover -s tests"
```

Each build seat (at most 3 by default: `codex`, `gemini-argon`, `claude-fable`)
gets a full local clone of the repo at HEAD with no remote, runs its
`argv_by_mode.build` argv inside it, then the test command runs there.
Results: `~/panel-builds/<repo>/<ts>/` (`$PANEL_BUILDS`) holding `_build.json`,
`_scoreboard.md`, `<seat>.diff` and `clones/<seat>`; never under
`~/panel-runs`, so status, summary and resume cannot see a build. Ranking is
arithmetic: tests pass and something changed, then the smaller diff, then the
faster seat. Nothing is merged, pushed or written to the source repo; judging
a candidate is the printed `panel.py --diff` command. A seat without a `build`
entry is refused. Only quota/login failures reach `_seat-health.json`.
Background it like a review. A stop ends every clone's process tree and
still writes the scoreboard.

## Add or change a seat

Each seat is `cmd` or `http`. Example:

```json
"my-model": {
  "lineage": "acme", "default": false,
  "http": {
    "url": "https://api.example.com/v1/chat/completions",
    "key_from": {"file": "~/.config/acme/env", "env_key": "ACME_API_KEY"},
    "headers": {"Authorization": "Bearer {key}", "Content-Type": "application/json"},
    "body": {"model": "acme-1", "messages": [{"role": "user", "content": "{prompt}"}]},
    "extract": ["choices[0].message.content"]
  }
}
```

For CLI use `"cmd": {"argv": ["acme", "-p", "{prompt}"]}`.
- `{prompt}`, `{repo}`, `{key}` substitute everywhere; exact placeholders are
  replaced wholesale. `"stdin": true` pipes the prompt for cmd seats.
  `argv_by_mode` appends a per-mode tail (`diff review`, `task`, `build`); an
  unknown mode gets the diff-review tail. In a build `{repo}` is the clone.
- `key_from`: `{file, env_key}` (dotenv), `{file, json_path}` (JSON), or `{env}`.
  **Never inline credentials or expose them in argv/logs.**
  A cmd seat's `env_from` is `{VAR: key_from spec}`: the credential goes into
  the CLI's environment, never argv, and is scrubbed from its output.
- HTTP may be a list tried in order. Extract paths are ordered fallbacks;
  `[]` means first match in a list. Fallback answers are explicitly marked.
  `"stream": true` in a body reads SSE with a 180 s idle timeout and retries a
  stalled request (Model Studio sometimes accepts one and never answers).
  All HTTP fallbacks/retries share one seat deadline, including body reads.
  Uncancellable urllib I/O may drain in a daemon after timeout; late answers
  are discarded and cannot write run artifacts.
  Known HTTP keys are scrubbed from successful and failed answers, including
  streaming and fallback results, before the caller can save them.
- `default` selects a seat; `local` requires opt-in; `serial` prevents shared
  RAM contention. Set lineage correctly or the consensus count is false.
- **Effort policy (2026-10-28): xhigh wherever the route offers it, never max.**
  Below that, the route's top level: Carbon/Argon `#high`, Antigravity Flash `high`,
  Model Studio GLM `high`, free GLM/DeepSeek `#high`, Kimi `high` (via the
  cmd `env` KIMI_MODEL_THINKING_EFFORT; its CLI has no flag). Gemini CLI,
  Copilot auto and free MiMo expose no effort. A test fails on any `max`.
- **Intelligence:** each seat's `intelligence` block is its Artificial
  Analysis Intelligence Index score for the model AND effort it runs (top-level
  `intelligence` names the index and date). A run records each seat's score in
  `run.json` as it runs; summaries rank seats by that and add a `top AA`
  column. A moved pin reads `stale`: re-read the leaderboard after a bump.
  Weigh judgment calls by it; never mute a family for it.
- `$PANEL_SEATS` overrides the roster; `$PANEL_RUNS` overrides run storage.
- `mimo-ultraspeed` is opt-in, metered, outside presets. It uses OpenCode's
  `xiaomi.key` in `~/.local/share/opencode/auth.json`. Key presence does not
  prove account access. Use public context until its data terms are verified.
- Free Nemotron/MiMo are public-context only. OpenCode documents no training
  and zero retention for free GLM/DeepSeek routes. See USAGE.md sources.
- `gemini-carbon` is the default Google voice (2026-11-10): Gemini 4 Carbon via
  OpenCode's Google API key, read-only plan agent at `#high`. Metered.
  `gemini-argon` (same route, off by default) is the fallback Google voice.
- Every `opencode` review seat runs `--standalone --agent plan` with an env
  `OPENCODE_CONFIG_CONTENT` that leaves the plan agent only read/glob/grep.
  No `--agent` means the write-capable build agent; `plan` alone still ran
  shell commands here (the machine's OpenCode config allows every tool); and
  only a standalone server reads the seat's environment. A test enforces it.
  OpenCode works in `$PWD`, not the process's real directory.
- `glm-oc`, `deepseek-oc`, `qwen-max-oc`: opt-in in-repo twins of the three
  Model Studio HTTP seats (same model and Standard key), through OpenCode so
  they can read the launch directory. In no preset. Their endpoint, model
  list and effort live in the `panel-model-studio` provider of
  `~/.config/opencode/opencode.json`, outside this repo: seat-refresh cannot
  bump them (`could not verify`) until the new model is added there.
- `muse` uses `muse exec --model muse-wonder-1.0 --reasoning-effort xhigh`.

## Keep versions current

**Never bump a seat's model id by hand.** `track` blocks use
`{provider, match, format}` against the keyless models.dev catalog.

```bash
python3 scripts/seat-refresh.py                  # report only
python3 scripts/seat-refresh.py --apply          # live verify, then update
python3 scripts/seat-refresh.py --apply --only kimi,opencode-free
```

New families may require a reviewed track change (Kimi's `-pro` suffix did).
The script never downgrades and resets a changed model's grade to `?`.
Verification requires an actual `OK` answer, not just exit zero/nonempty text.
The optional launchd template `scripts/seat-refresh.launchd.plist` runs
`--apply --notify` at 07:25, 11:25, 15:25 and 19:25 (same-day pickup); log:
`$PANEL_RUNS/_seat-refresh.log`. The catalog
GET retries ~8 min (wake races DNS); an unreachable catalog is itself an alert.
Each alert fires once (`$PANEL_RUNS/_seat-refresh-state.json`), not every run.
After a bump, re-read the seat's AA score. Review diffs and re-grade eventually.
A track only bumps within its line. A same-day-or-newer, at-least-as-pricey
model in a seated family that no track follows (and not a pinned id plus a
serving suffix like `-highspeed`) prints as `NEW LINE`; wire it by hand.
**Challenger watch** (same run, report-only): a model on a route panel calls
(models.dev `opencode`/`opencode-go`/lab APIs, `cursor-agent models`, `opencode
models`, codex cache) that no seat runs and whose Artificial Analysis score, at
the effort a seat would run it, beats its lineage's best default seat (else the
weakest) prints `CHALLENGER`, one notification each. Free AA key: `AA_API_KEY`
in `~/.config/artificialanalysis/env` (credit AA). No exact match: `UNMATCHED`;
max-only score or unscored seat: `UNCLEAR`; new OpenCode models without a
matched score (14 days): `UNSCORED`.
Tests pin family, effort, and scaffold consistency, not version literals.

## Hard rules

1. **Neutral phrasing.** Avoid "adversarial", "break", "attack", "exploit" in
   prompts: these have triggered output filtering. Say "verify correctness",
   "find defects", "check invariants". Rephrase/re-run; never downgrade for a filter.
2. **Synthesize, never relay.** Model output is evidence, not a verdict.
3. **Reproduce contested claims.** Write a failing test before fixing a reported
   defect. If you cannot reproduce it, do not treat it as established. Record
   the outcome with `panel_verdict` so the seats' track record is measured.
4. **Fix, then re-panel** with `--round N` to inject earlier findings.
5. **Accepted residuals** go in `<repo>/.panel/residuals.md`, included automatically.
6. A CLEAN from a seat that found nothing is weak evidence; prefer reproduction.
7. **Respect provider terms.** Never route private code through training-enabled
   or incompatible plans. The lapsed Token-Plan qwen seat was removed.
8. **Missing findings block = no vote.** Silence does not mean agreement.

## Work on panel

`python3 -m unittest discover -s tests` — offline; no model/network calls.
Run before committing.
Seat CLIs run in their own session and carry `PANEL_RUN_TOKEN`/`PANEL_SEAT_TOKEN`
in their environment; a timeout or a stop freezes (SIGSTOP) every process in
the tree or carrying the token until a snapshot is stable, then kills them.
Accepted limits: `.panel/residuals.md`. Ctrl-C/SIGTERM only
set a flag; a reaper thread cleans up CLI seats and attempts a summary before
exit 128+signal. HTTP I/O is uncancellable; the hard-exit fallback waits 20 s
after reaping, plus drain/summary time. A second stop signal or SIGKILL gives
no cleanup/summary guarantee. stdin-capable CLIs
(claude, codex `-`, cursor-agent, opencode) take the prompt on stdin.
Anything requiring exact money, geometry, safety limits, scoring, or pricing
belongs in deterministic code; models orchestrate/judge.
Update this file with run/test/structure changes in the same commit; keep ~150 lines.
