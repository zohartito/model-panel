# Using panel

One question → every wired frontier model in parallel → you synthesize.

## Install (or repair) every surface

Use Python 3.11 or newer for the CLI, MCP server, installer, and offline tests.

```bash
python3 <repo>/scripts/panel-install.py --check   # report only
python3 <repo>/scripts/panel-install.py           # do it
```

Idempotent and safe to re-run — it backs up each file once, updates an
existing `panel` entry in place instead of duplicating it, merges rather than
clobbers (an editor's own fields survive), and skips surfaces you don't have.
Run it again after moving the repo, upgrading Python, or setting up another
machine. Windows works the same way, from wherever you cloned the repo.

The terminal launcher needs `~/.local/bin` on PATH on macOS/Linux. If missing,
run `export PATH="$HOME/.local/bin:$PATH"`, put that line in your shell's startup
file, and open a new terminal or source that file. The installer only prints
a hint; it does not change your shell profile.

## Surfaces the installer can wire

| Surface | How you use it |
|---|---|
| **Terminal** (any shell) | `panel "question"` |
| **Claude Code** | ask it to use the panel MCP tools; `/panel` and the skill require the separate `claude plugin marketplace add zohartito/model-panel` and `claude plugin install panel@panel` commands |
| **Codex CLI** | ask it to use the panel MCP tools |
| **Cursor** (IDE + `cursor-agent`) | MCP tools available in chat |
| **VS Code** | MCP tools in Copilot Chat agent mode |
| **Claude Desktop** | MCP tools in chat |
| **Gemini CLI** / **Qwen CLI** | MCP tools in chat |
| **grok CLI** | MCP tools in chat |

Not wired, and why: **PyCharm** — JetBrains AI Assistant takes MCP servers
through its UI, not a file this script can safely write. Add it by hand:
Settings → Tools → AI Assistant → Model Context Protocol → **+**, command
the absolute path of your `python3`, argument the absolute path of
`<repo>/skills/panel/mcp_server.py`. **Zed** and **OpenCode** use a server
shape this installer doesn't speak, so add those by hand too.

## From a terminal

```bash
panel "Should the job runner retry a failed lock acquire, or fail closed?"
panel --diff /tmp/change.diff --repo ~/src/my-service \
      --context "What changed and what earlier rounds already fixed." \
      --criteria "1) never runs two jobs on one lock. 2) ..."

panel seats      # who is wired up here, and whether each one resolves
panel last       # the newest run's summary
panel where      # path to the newest run
panel resume     # refill seats that failed
panel bg "..."   # detach and hand back the log path
```

Seats take 10–40 minutes. In the foreground you watch each one land; `panel bg`
detaches. Every flag from `panel.py --help` passes straight through.

## Carbon, Argon and Muse routes

`muse` now uses the installed CLI at xhigh effort:

```bash
muse exec --model muse-wonder-1.0 --reasoning-effort xhigh "Your question"
panel bg --seats muse --task "Your question"
```

It uses the CLI's own sign-in and subscription. It also joins `strong`.

`gemini-carbon` is the default Google voice: Gemini 4 Carbon,
a metered Google API route through OpenCode with your saved key, read-only
`plan` agent, `#high` reasoning (the top variant; `#xhigh` is unavailable):

```bash
panel bg --seats gemini-carbon --task "Your question"
# Equivalent transport:
opencode run --agent plan --model "google/gemini-4-carbon#high" "Your question"
```

Carbon scores 60 on the Artificial Analysis index at high, against 50 for
Argon, at the same price. `gemini-argon` stays wired on the same route as the
fallback Google voice. Carbon also sits in `quick`; its speed on real panel
prompts is not measured yet.

A free-tier Google key cannot run this route; calls are billed to your Google
API project. The `gemini` CLI seat (free key, Flash) is in no preset except
`full`. Cursor also lists Argon (low/medium/high, untested). See
[Google's Argon specification](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon).

## MiMo V2.6 Pro UltraSpeed

Select `mimo-ultraspeed` explicitly; it is outside the default and named
presets because it is metered. This uses Xiaomi's direct API, not OpenCode
Zen's free Flash model or Go's ordinary Pro model.

Connect the **Xiaomi** provider in OpenCode (`/connect`). Panel and Model
Scout reuse its saved API key at `xiaomi.key` in
`~/.local/share/opencode/auth.json`; no key is copied into either repository.
The account must have UltraSpeed access; seeing a model in OpenCode's catalog
or passing a key-presence check alone cannot confirm that.

```bash
panel seats
panel bg --seats mimo-ultraspeed --task "Your public-context question"
```

Thinking is enabled with a 32,768-token completion budget. Only final answer
text is returned; reasoning-only output is not counted as a completed answer.
Use public context until the service's data terms have been verified.
No grade or live-inference success is implied by registration.

Sources checked 2026-09-22:
[model and pricing](https://mimo.mi.com/models/en-US/mimo-v2.6-pro-ultraspeed),
[request schema](https://mimo.mi.com/docs/en-US/api/chat/openai-api).
Published input/output rates are $4.35/$8.70 per million tokens (cached input
$0.036); these are service prices, not a measured run cost.

## From any agent (MCP)

Ask in plain language — "run this past the panel", "get a second opinion on
this diff" — and the agent picks the tools up. What it's doing underneath:

| Tool | What it does |
|---|---|
| `panel_run` | Launches **detached**, returns the run directory immediately |
| `panel_status` | Poll: which seats landed, which are out, compact finding counts |
| `panel_summary` | The consensus table + every seat's bottom line |
| `panel_seat` | One named seat's full answer |
| `panel_resume` | Refill only the failed seats |
| `panel_grade` | Score a run against an answer key |
| `panel_seats` | The roster, each seat's lineage and measured track record |
| `panel_verdict` | Record that a finding was reproduced (confirmed) or ruled out (rejected) |

The intended loop is **run → poll → summary → drill into one seat → check it
yourself → `panel_verdict`**. Don't let
an agent read every seat file: that's hundreds of KB of reasoning and it will
eat the context you need for thinking. In Claude Code, hand the run directory
to the `panel-synthesizer` subagent instead; elsewhere, point a second model at
`prompts/synthesize.md`.

## Reading the result

A run lands in `~/panel-runs/<repo>/<timestamp>/`:

```
_summary.md    consensus table + bottom lines   <- read this first
_result.json   the same, machine-readable
<seat>.md      one per model, written the moment it lands
prompt.md      initial prompt: all task seats; all diff seats except codex
prompt.codex.md initial prompt for codex in diff mode (file-reference variant)
reviewed.diff  saved diff, referenced by prompt.codex.md
run.json       what was planned, the models that ran (used by resume)
_merge.json    the merge judge's proposals, keyed to the rows it saw
_verdicts.json what you confirmed or rejected after checking
```

Only the seat literally named `codex` uses the variant; `codex-astra` and
`codex-luna` receive `prompt.md`. Findings-block repair, when needed, is a
separate follow-up request, not the saved initial prompt.

Each consensus row has an id (`#3fa2c91b`). `4 (+2 likely)` means four
lineages raised the claim and the merge judge thinks two more raised the same
claim in other words: those rows are listed under the table, and the `+2` is
never counted. Read them before leaning on it.

The consensus table counts **lineages, not seats**. Seats sharing a lineage are
not independent: the three Claude seats are one voice, `grok` and `cursor-grok`
are one model behind two scaffolds, a local gemma is the same family as hosted
gemini. Two lineages agreeing is real signal; two Claude seats agreeing is one
model agreeing with itself.

**A finding only one lineage raised is often the real one.** Read that seat in
full before dismissing it.

**Record what held up.** After you reproduce (or rule out) a finding:

```bash
python3 <repo>/skills/panel/panel.py --verdict <run dir> --finding 3fa2c91b --mark confirmed --note "failing test added"
```

Verdicts go to `<run>/_verdicts.json` and `~/panel-runs/_track-record.jsonl`.
Every summary, `--list-seats` and `panel_seats` then show each seat's record
(`track 7/9 confirmed, 2 solo`) for the model it actually ran; a model bump
starts a fresh count. Solo = the only lineage that raised a confirmed finding.

Two more lines can appear under the header. `SKIPPED as dead lanes`: the
seat's last call failed on quota or login within 24 h, so the default and
preset rosters leave it out (name it in `--seats`, or pass `--include-dead`).
`Findings block recovered`: the seat answered without the block, and one
follow-up call to the same seat restated its claims as one.

**Seats are ranked by intelligence.** `_summary.md` lists seats highest first
with `AA <score>`: the [Artificial Analysis Intelligence
Index](https://artificialanalysis.ai/leaderboards/models) v5.0 score for that
model at the effort the seat runs, read 2026-10-29 (Fable 5.2 62, read
2026-11-09 on v5.0.1; Carbon 60, read 2026-11-10; Opus 5.5 57, Astra 55, Sol 52, Argon/Sonnet 50, Kimi 46, Muse ≤43, Grok and Qwen Max 34, GLM and
DeepSeek ≤32; Perplexity is unscored). `≤` means the index measured a higher
effort than the seat runs; `stale` means seat-refresh moved the pin since, so
re-read the leaderboard and update the seat's `intelligence` block. The
consensus table's `top AA` column is the best score among the seats behind a
finding. On a judgment call where lineages disagree and neither shows a check,
lean toward the higher score. Never mute a lower-scoring family: it is seated
because it reasons differently, and a finding with a repro beats any score.

## Quick answers and escalation

`--preset quick` asks six lineages whose seats answer fastest (medians of
1.5–5.5 minutes over 91 runs; no Claude seat, each has an 11+ minute median).
`--escalate` starts there and seats the rest of the default roster only when
the quick tier leaves doubt: fewer than four lineages voted, a high-severity
claim came from one lineage alone, or a diff review split between CLEAN and
FINDINGS. The summary says which happened. An escalated run takes longer than
a plain full run, so use it when the question is probably easy.

## Build pilot: several seats implement, tests score

```bash
python3 <repo>/skills/panel/build.py \
  --task "what to build" --repo ~/src/some-repo \
  --test "python3 -m unittest discover -s tests"
```

Up to three seats (`codex`, `gemini-argon`, `claude-fable`; `--seats` picks,
`--max-seats` raises the cap) each get their own full clone of the repo at
HEAD, with no remote, and implement the task there. The test command then
runs in each clone. `~/panel-builds/<repo>/<timestamp>/_scoreboard.md` ranks
the candidates by arithmetic only: tests pass and something changed, then the
smaller diff, then the faster seat. Nothing is merged or pushed and your repo
is only read. To have the panel judge a candidate, run the `panel.py --diff`
command the scoreboard prints. Uncommitted changes in your repo are not in
the clones. A held-out check can ride in the test command
(`cp /path/check_test.py tests/ && ...`).

## Stopping a run

Ctrl-C (or killing the process) stops every seat and its child processes
within a second or two. Answers that already landed stay on disk, the summary
is still written, and seats that never started stay owed, so
`--resume <run dir>` picks up where it stopped. A second Ctrl-C quits at once.

## When to reach for it

Worth the minutes: fail-closed logic, anything running unattended (schedulers,
hardware control, deploys), decisions that are expensive to reverse, research where
being confidently wrong costs real time.

Not worth it: trivial edits, anything you can verify yourself in a minute,
anything where you already have ground truth — run the test instead.

## Adding a model

Seats are data. Edit `skills/panel/seats.json`:

```json
"my-model": {
  "lineage": "acme",
  "default": true,
  "cmd": {"argv": ["acme-cli", "-p", "{prompt}"]}
}
```

or an HTTP endpoint with `url` / `headers` / `body` / `extract` and a
`key_from` that reads the credential out of a file — never inline a key. Full
spec in [AGENTS.md](AGENTS.md). Then `panel seats` to confirm it resolves.

Get `lineage` right or the consensus count lies.

## Model versions keep themselves fresh

For demanding questions, use `panel bg --preset strong --task "..."`.
It selects eight independent families: Claude Fable, GPT-6 Astra, Gemini 4
Carbon, Grok, Kimi Pro, Muse Wonder, GLM Flash, and DeepSeek Flash. Every seat
runs xhigh where its route offers it (never max), else the route's top level. Routes were live-tested on 2026-10-25–26;
this is a practical roster, not a quality benchmark.
The original `trio`, `providers`, default roster, and main `codex` seat remain
available. `strong` avoids the metered HTTP seats and local hardware
dependencies. `gemini-agy` uses an Antigravity sign-in, separately from the
Gemini CLI's API-key quota. Free endpoints can expire or exhaust quota. All
three are also in `full`.

`glm-flash-free` and `deepseek-flash-free` also join the `free` preset.
`opencode-free` now follows Nemotron Ultra/Super VL releases; its working
Super VL replacement is **public-context only** under NVIDIA trial terms.
OpenCode documents no training and zero retention for the GLM and DeepSeek
free endpoints. See [OpenCode's model terms](https://dev.opencode.ai/v2/docs/console/models).

`panel seats`
reports **configured**, meaning only that the CLI or credential exists;
the accompanying last-failure message is evidence from a real call.

An optional launchd job (template `scripts/seat-refresh.launchd.plist`, 07:25 /
11:25 / 15:25 / 19:25, so a release is picked up the day it lands) checks every pinned seat against the models.dev catalog and bumps stale ids after a live
verification call — log at `$PANEL_RUNS/_seat-refresh.log`. It keeps each
seat's effort. A newer model in a seated family that no seat tracks (a new
tier, priced at or above what that family already seats) prints as
`NEW LINE` and pops a macOS notification; wire those by hand. The catalog
fetch retries for ~8 minutes (a run fired on wake often beats DNS), an
unreachable catalog posts its own alert, and each alert fires once rather than
every run.

The same run keeps a **challenger watch**, which answers one question: is
there a model panel could call today that is smarter than the seats it runs?
"Callable" means listed on a route panel uses: the models.dev `opencode`,
`opencode-go`, Google, OpenAI, Anthropic, xAI, Moonshot/Kimi, Meta and Model
Studio catalogs, plus `cursor-agent models`, `opencode models` and the codex
models cache. "Smarter" means a higher Artificial Analysis Intelligence Index
score (rounded to the seats' whole numbers) at the effort a seat would run it
on that route (xhigh where offered, never max) than the strongest default
seat of its lineage, or than the weakest default seat for a lab panel does
not seat. Scores come from AA's free API (about 4 requests a run, under its
100-a-day limit); put a free key in `~/.config/artificialanalysis/env` as
`AA_API_KEY=...`. Each `CHALLENGER` gets its own notification, once. A
smarter AA row with no exact catalog match is listed `UNMATCHED`, never
guessed; one AA scored only at max, or whose lineage's seats have no current
score, is `UNCLEAR`. New OpenCode models from the last 14 days with no
matched score are `UNSCORED`. The watch never edits `seats.json`; wiring a
challenger is a reviewed change. Run it by hand:

```bash
python3 <repo>/scripts/seat-refresh.py            # report only
python3 <repo>/scripts/seat-refresh.py --apply    # verify + bump
python3 <repo>/scripts/seat-refresh.py --apply --notify  # as launchd runs it
```

## The rules that cost something to learn

1. **Neutral phrasing.** Never write "adversarial", "break", "attack" or
   "exploit" in a prompt — they trip OpenAI's content filter *at output time*
   and destroy the report after the full paid reasoning run. Say "verify
   correctness", "find defects", "check these invariants hold".
2. **Synthesize, never relay.** The panel's output is evidence, not a verdict.
3. **Reproduce contested claims before acting.** If a finding is real you can
   write a failing test for it first. If you can't, it isn't real.
4. **Fix, then re-panel** with `--round N` so seats narrow instead of repeating.
5. **Accepted residuals go in `<repo>/.panel/residuals.md`** — they ride along
   automatically, so rounds stop rediscovering what you already decided to live
   with.
6. **A CLEAN from a seat that found nothing all round is weak evidence.**
7. **Know each lane's terms.** Never route private code through a plan with a
   data-training clause (the Token-Plan `qwen` CLI seat was removed 2026-08-11
   for exactly that). `qwen-max` uses the Standard ModelStudio key, like `glm`.
8. **No findings block = no vote.** Silence is not agreement.

## When something breaks

```bash
python3 <repo>/scripts/panel-install.py --check   # every surface
panel seats                                       # every model
cd <repo> && python3 -m unittest discover -s tests # offline tests
```

A seat showing `MISSING` needs its CLI installed or its credential file
present; the panel runs fine without it, just with one fewer voice.
