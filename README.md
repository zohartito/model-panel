# panel

Ask several AI models the same question at once, then compare what they said.

You give panel a question or a code diff. It sends that to every model you
have set up (each one is a "seat"), in parallel, and saves each answer the
moment it arrives. Then it groups the findings by how many **independent model
families** raised them, so three Claude models agreeing counts as one voice, not
three. It also keeps a ledger: after you check a finding yourself, you mark it
confirmed or rejected, and each seat builds a measured track record.

panel does not decide anything for you. Its output is evidence you read and
check before you act.

- Plain Python 3 standard library. No `pip install`.
- Works as a command-line tool, an MCP server (for Claude Code, Codex, Cursor,
  VS Code and other MCP clients), and a Claude Code plugin.
- Seats are configuration (`skills/panel/seats.json`), not code. Adding a
  model is a JSON edit.

## How it works

1. You run `panel.py --task "question"` (or `--diff change.diff`).
2. panel builds one prompt and sends it to every selected seat. A seat is
   either a **CLI** it runs as a subprocess (`claude`, `codex`, `gemini`, ...)
   or an **HTTP** endpoint it POSTs to (any OpenAI-style chat API, Ollama, ...).
3. Each seat ends its answer with a short machine-readable findings block.
4. panel clusters the findings, counts lineages, and writes a summary.
5. You read the summary, reproduce what matters, and record a verdict.

## Requirements

- Python 3 (CI runs 3.12 and 3.13; this copy was also tested on 3.11).
- macOS or Linux. Windows code paths exist but are only exercised in
  simulated tests so far (see `.panel/residuals.md`).
- At least one model you can reach: a model CLI you are already signed in to,
  or an API key. Every seat you have not set up is reported as missing and
  skipped; it does not stop the run.

## Install

```bash
git clone https://github.com/zohartito/model-panel.git
cd model-panel
python3 -m unittest discover -s tests      # offline: no network or model calls
python3 skills/panel/panel.py --list-seats # which seats resolve on this machine
```

Optional, to use it from editors and agents:

```bash
python3 scripts/panel-install.py --check   # report what it would change
python3 scripts/panel-install.py           # register the MCP server and add `panel` to PATH
```

The installer backs up every file it touches, updates an existing `panel`
entry instead of duplicating it, and skips apps you do not have. As a Claude
Code plugin: `claude plugin marketplace add zohartito/model-panel`, then
`claude plugin install panel@panel`.

## Quick start: two seats, one provider

`examples/seats.openrouter-two-seat.json` is a complete two-seat roster that
needs one OpenRouter API key and reaches two different model families (an
OpenAI model and an Anthropic model), so the consensus count is meaningful.

```bash
export OPENROUTER_API_KEY=...            # your key; it is never written to disk
export PANEL_SEATS="$PWD/examples/seats.openrouter-two-seat.json"

python3 skills/panel/panel.py --list-seats
python3 skills/panel/panel.py \
  --task "Should a job runner retry a failed lock acquire, or fail closed?" \
  --context "Jobs are idempotent; the lock is a row in Postgres." \
  --criteria "Cover failure modes and give a concrete retry policy."
```

The example uses `openai/gpt-6.1-sol` and `anthropic/claude-sonnet-5.5`, both
listed by <https://openrouter.ai/api/v1/models> on 2026-11-14. Ids change; swap
in current ones from that list when they do, and keep each seat's `lineage` set
to the lab that trained the model, or the consensus count is wrong.

Code review instead of a question:

```bash
git diff main > /tmp/change.diff
python3 skills/panel/panel.py --diff /tmp/change.diff --repo . \
  --context "What this change does." \
  --criteria "1) never runs two jobs on one lock. 2) errors fail closed."
```

## Where results go

Each run gets its own directory, by default `~/panel-runs/<repo>/<timestamp>/`
(`<repo>` is the name of the `--repo` directory, or the current one):

| File | Contents |
|---|---|
| `_summary.md` | Consensus table (finding id, lineage count, severity, claim, seats) and each seat's bottom line. Read this first. |
| `_result.json` | The same, machine-readable. `--json` also prints it. |
| `<seat>.md` | One seat's full answer, written as soon as it lands. |
| `prompt.md` | Exactly what was sent. |
| `run.json` | What was planned and which model each seat ran (used by `--resume`). |
| `_verdicts.json` | Findings you marked confirmed or rejected. |

Shared state lives in the run root: `_track-record.jsonl` (every verdict, per
seat and model) and `_seat-health.json` (each seat's last outcome).

Useful follow-ups:

```bash
python3 skills/panel/panel.py --resume last --repo .        # refill failed seats only
python3 skills/panel/panel.py --verdict <run dir> --finding 3fa2c91b --mark confirmed --note "failing test added"
python3 skills/panel/panel.py --grade <run dir> --key key.json  # score a run against known defects
```

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `PANEL_SEATS` | `skills/panel/seats.json` | Roster file to use. Point it at your own copy. |
| `PANEL_RUNS` | `~/panel-runs` | Root for run directories, track record and seat health. |
| `PANEL_BUILDS` | `~/panel-builds` | Root for the build pilot (`skills/panel/build.py`). |
| `--seats a,b` / `--skip a` / `--preset name` | default-on seats | Pick seats for one run. Presets live in `seats.json`. |
| `--include-local` | off | Also seat the self-hosted Ollama seats. |
| `--timeout` | 2400 s | Per-seat budget. |

Credentials are never stored in this repo. A seat reads its key from an
environment variable (`"key_from": {"env": "VAR"}`) or from a local file you
create (`{"file": "~/.config/x/env", "env_key": "VAR"}` for dotenv files,
`{"file": "...", "json_path": "a.b"}` for JSON). Keys go into request headers
or the CLI's environment, never argv, and are scrubbed from saved output. The
full seat format is in [AGENTS.md](AGENTS.md#add-or-change-a-seat).

## The bundled roster

`skills/panel/seats.json` is the default roster: the author's seat wiring,
with neutral notes and no recorded grades. It assumes subscriptions you may
not have; copy it, delete what you do not use, and set `PANEL_SEATS`. Model ids move fast, so
`scripts/seat-refresh.py` checks them against the public models.dev catalog
(below).

| Seat(s) | Reached through | You need |
|---|---|---|
| `claude-sonnet`, `claude-opus`, `claude-fable`, `claude-haiku` | `claude` CLI (Claude Code) | Signed in (`claude /login`). One lineage. |
| `codex`, `codex-astra`, `codex-luna` | `codex` CLI (OpenAI) | Signed in. One lineage. `codex` runs with approvals and sandbox **off** inside `--repo`. |
| `gemini`, `gemini-flash` | `gemini` CLI | Signed in or a Gemini API key in the CLI. |
| `gemini-carbon`, `gemini-argon` | `opencode` CLI | A Google API key connected in OpenCode. Metered. Runs a read-only agent. |
| `gemini-agy` | `agy` (Antigravity) CLI | Signed in. |
| `grok` | `grok` CLI | Signed in. |
| `cursor-grok` | `cursor-agent` CLI | Signed in to Cursor. Runs with `-f` (no approval prompts). Same lineage as `grok`. |
| `kimi`, `kimi-code` | `kimi` CLI | Signed in. |
| `muse` | `muse` CLI | Signed in. |
| `copilot` | `copilot` CLI | Signed in to GitHub Copilot. |
| `glm-flash-free`, `deepseek-flash-free`, `mimo-flash-free`, `opencode-free` | `opencode` CLI, free tier | OpenCode installed. Some free routes are public-context only: check their terms before sending private code. |
| `glm`, `deepseek`, `qwen-max` | HTTP, Alibaba Model Studio (international) | Key at `~/.qwen/settings.json` → `env.DASHSCOPE_API_KEY`. Metered after any free quota. |
| `glm-oc`, `deepseek-oc`, `qwen-max-oc` | `opencode` + the same key | Also a `panel-model-studio` provider in `~/.config/opencode/opencode.json`. Opt-in. |
| `pplx` | HTTP, Perplexity Agent API | `PPLX_API_KEY=...` in `~/.config/perplexity/env`. Metered. |
| `mimo-ultraspeed` | HTTP, Xiaomi MiMo API | Xiaomi provider connected in OpenCode (key read from `~/.local/share/opencode/auth.json`). Metered, opt-in. |
| `local-27b`, `local-mac`, `local-glm-air` | HTTP, Ollama on `127.0.0.1:11434` | Ollama with that model pulled. Opt-in (`--include-local`). |
| `local` | HTTP, Ollama on another machine | Replace `OLLAMA_HOST` in its `url` with that machine's address. Opt-in; fails to connect until you do. |

If you want a different provider, copy a seat of the same kind (`cmd` or
`http`) and edit it. `--list-seats` tells you whether each one resolves:
`configured` means the CLI or key is present, not that a live call has
succeeded.

## Keeping model versions current (optional)

```bash
python3 scripts/seat-refresh.py            # report newer model ids, change nothing
python3 scripts/seat-refresh.py --apply    # verify each newer id with a live call, then update seats.json
```

`--apply` spends a small live call per bumped seat. The report also flags
models that score higher on the Artificial Analysis Intelligence Index than
your seats; that part needs a free key as `AA_API_KEY=...` in
`~/.config/artificialanalysis/env` (without it, scores are skipped).

To run it on a schedule on macOS, fill in the two placeholders in
`scripts/seat-refresh.launchd.plist` and load it:

```bash
sed -e "s#__PANEL_REPO__#$PWD#g" -e "s#__PANEL_RUNS__#$HOME/panel-runs#g" \
  scripts/seat-refresh.launchd.plist > ~/Library/LaunchAgents/local.panel.seat-refresh.plist
mkdir -p ~/panel-runs
launchctl load ~/Library/LaunchAgents/local.panel.seat-refresh.plist
```

On Linux, a cron line running `python3 <repo>/scripts/seat-refresh.py --apply`
does the same (`--notify` posts macOS notifications only).

## Costs and limits

- **Seats call real, often paid, models.** One run sends the full prompt,
  including any `--diff` and `--file` contents, to every selected seat. CLI
  seats draw on those CLIs' plans and quotas; HTTP seats and some OpenCode
  routes bill per token. A full default run is roughly a dozen frontier calls
  at high reasoning effort. Start with two or three seats.
- **Your code leaves your machine.** Each seat's provider sees what you send.
  Check each provider's data and training terms before sending private code;
  some free routes are explicitly public-context only.
- **Some CLI seats can act on your files.** `codex` runs with its sandbox and
  approvals bypassed in `--repo`, and `cursor-grok` runs with `-f`. Remove or
  edit those seats if that is not acceptable to you.
- **Runs are slow.** Seats often take 10 to 40 minutes. Run it in the
  background (`panel bg ...`, the MCP `panel_run` tool, or `nohup`), and use
  `--resume` to refill seats that failed without paying again for the ones
  that answered.
- **Clustering is lexical.** The consensus table is a first pass. Two seats
  wording the same claim differently can land in separate rows; an optional
  merge judge proposes such pairs as `+N likely` and never counts them.
- **A CLEAN or a silent seat is weak evidence.** A seat that emits no
  findings block gets no vote. Reproduce a finding before you act on it.
- **Prompt wording matters.** Words such as "attack" or "exploit" have made
  some providers' content filters discard a finished answer. Phrase reviews as
  "verify correctness" or "find defects".

## More

- [USAGE.md](USAGE.md): day-to-day use, the terminal shortcuts, reading results.
- [AGENTS.md](AGENTS.md): the guide for agents driving panel, and the full seat spec.
- [skills/panel/SKILL.md](skills/panel/SKILL.md): the Claude Code skill.
- [scripts/PANEL-ON-TAG.md](scripts/PANEL-ON-TAG.md): review every release tag automatically.

## License

MIT. See [LICENSE](LICENSE).
