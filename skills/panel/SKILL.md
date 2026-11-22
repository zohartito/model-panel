---
name: panel
description: Run a question past a full panel of independent frontier models in parallel (codex gpt-6.1-sol + gpt-6-astra, gemini 4 carbon, kimi, grok direct + via Cursor, muse wonder CLI, glm, deepseek, qwen max, claude sonnet/opus/fable, perplexity — every seat at xhigh where its route offers it, never max — versions auto-tracked by seat-refresh, each seat ranked by its Artificial Analysis Intelligence Index score), then synthesize their answers. Use for code review of high-stakes diffs, and for any judgement call worth more than one opinion — architecture decisions, research questions, plan critiques, tradeoffs, "am I about to do something dumb". Triggers on "run it past the panel", "ask the judges", "multi-model review", "get a second/third opinion", "panel this", or any safety-critical or expensive-to-be-wrong decision.
---

# Panel of judges

One question → every wired frontier model in parallel → you synthesize.
Runs on whatever model CLIs and API keys you already have. CLI seats bill to
those CLIs' own plans; HTTP seats and some OpenCode routes are metered per
token. See README.md for which seats cost what.

Script: `panel.py`, next to this file. Self-contained stdlib Python, no
dependencies, works from any repo. Resolve it once per session:

```bash
PANEL="${CLAUDE_PLUGIN_ROOT:-${PANEL_HOME:-$HOME/panel}}/skills/panel/panel.py"
# PANEL_HOME = your panel checkout, only needed outside the plugin
```

Installed as a plugin that resolves to the pinned copy in the plugin cache;
run straight from the repo it resolves to the working tree.

## When to use it

**Do** use it for: safety-critical or fail-closed logic, anything that will run
unattended (schedulers, hardware control, deploys), decisions that are expensive to
reverse, research questions where being confidently wrong costs real time, and
any review where "the code looked fine to me" is not good enough.

**Don't** use it for: trivial edits, questions you can verify yourself in under
a minute, or anything where you already have ground truth (run the test
instead). A panel run costs several minutes and a lot of tokens across accounts.

## Running it

**Code review (diff mode)** — ends each seat with `VERDICT: CLEAN|FINDINGS`:

```bash
python3 "$PANEL" \
  --diff /path/to/change.diff \
  --context "What this change does and what already got fixed in earlier rounds." \
  --criteria "1) invariant one. 2) invariant two. ..." \
  --repo /path/to/repo
```

**Anything else (task mode)** — ends each seat with `BOTTOM LINE: <one sentence>`:

```bash
python3 "$PANEL" \
  --task "The actual question, stated so a stranger could answer it." \
  --context "Background the models can't infer." \
  --criteria "What a good answer must cover." \
  --file notes.md --file data.csv
```

`--file` embeds text files; images/video/audio (and anything undecodable) are
attached by reference — path, size, kind — never as bytes, so a media
judgement can name its mp4 and stills without killing the prompt build.

**Always run it detached and watch it**, never in a blocking foreground call —
seats routinely take 10–40 minutes:

```bash
nohup python3 "$PANEL" ... > panel.log 2>&1 &
# then poll/wait on the PID in a background shell and read the results
```

Every seat's answer is written **the moment it lands**, so a run that dies at
minute 35 keeps everything that already finished. Fill in the rest with:

```bash
python3 "$PANEL" --resume last --repo /path/to/repo
```

`--resume` re-sends the **original** prompt to only the seats that are missing
or exited non-zero. A seat that already answered is never re-paid for.

Useful flags: `--seats codex,kimi` (only these), `--skip grok` (all but these),
`--include-local` (add self-hosted tripwires), `--timeout 2400`, `--out DIR`,
`--round N`, `--no-residuals`, `--json`, `--list-seats`.

Preview a new plan with `--estimate`: billing route, usable history count,
and nearest-rank P50/P95 elapsed minutes per seat. This launches nothing,
creates no run folder, and needs no task. History uses `$PANEL_RUNS` and
excludes answer mtimes later than timeout + 60 seconds; failures count too.
Use `--no-metered` to exclude metered, credit and unknown routes, including
escalation and the merge judge. An unknown route requires an explicit,
repeatable `--allow-unknown SEAT` and prints a warning. Free/local routes
are conservatively unknown under the current billing vocabulary. Account
overages are not checked. The billing policy persists on resume.

**Driving it from something other than Claude Code?** `mcp_server.py` next to
this file speaks MCP over stdio with zero dependencies (`panel_run`,
`panel_status`, `panel_summary`, `panel_seat`, `panel_resume`, `panel_grade`,
`panel_seats`), and `AGENTS.md` in the repo root is the harness-neutral guide.
Every run also writes `_result.json` for orchestrators that don't parse
markdown.

Output lands in `~/panel-runs/<repo>/<timestamp>/`: one `<seat>.md` per model,
the exact `prompt.md` sent, `run.json` (what was planned, for resume), and
`_summary.md` — the consensus table plus every seat's bottom line.

## Reading the result

**Spawn the `panel-synthesizer` subagent on the run directory** instead of
reading the seat files into your own context. Twelve seats of frontier
reasoning is hundreds of KB; the subagent reads all of it and hands back an
evidence-linked digest — confirmed findings, single-lineage findings,
contradictions, what is checkable right now, and which seats went silent. You
reach the conclusion. Read individual seat files yourself only when the digest
points you at one.

`_summary.md` leads with a mechanical consensus table:

| id | lineages | severity | where | claim | seats | top AA |

`lineages` is the number that matters, not `seats`. The three Claude seats
share the synthesizer's own lineage and count once; `grok` and `cursor-grok`
are one model behind two scaffolds and count once; the `local-mac` gemma seat
is the same family as `gemini`. The clustering is lexical, so treat it as a
first pass — the subagent's semantic read overrides it. A merge judge
(Claude Haiku) proposes rows that state the same claim: `4 (+2 likely)` means
four lineages counted plus two the judge thinks said it in other words. The
`+2` is never counted; the moved rows are listed under the table.

Other lines to notice: `SKIPPED as dead lanes` (out of quota or login within
24 h; no vote), `Findings block recovered` (the seat answered without a
block and restated its claims in one follow-up call), and `Escalation:` on a
`--escalate` run (whether the quick tier was enough, and why).

**After you check a finding, record it:** `panel_verdict` (MCP) or
`panel.py --verdict <run> --finding <id> --mark confirmed|rejected|unresolved`.
Use `unresolved` with a nonblank `--note` (MCP: `note`) explaining a missing
check or inconclusive evidence; it does not count as confirmed or rejected.
That is each seat's measured track record on real work, per model; summaries
and `panel_seats` show it as `track 7/9 confirmed, 2 solo, 1 unresolved` next to
the AA score. JSON/Markdown refresh immediately with each finding's saved
verification and note, including likely-merged rows; no record is `unreviewed`.
Verification never changes lineage agreement.

## The seats

`seats.json` is the whole roster **and its wiring**: lineage, default-on,
serial, measured track record, and how to reach each model — a `cmd` argv
template or an `http` spec. Adding a provider is a config edit, never a patch.
`python3 "$PANEL" --list-seats` shows which seats actually resolve on the
machine you're on (missing CLI, missing credentials, or configured). Configured
does not prove a live answer; check the last failure too. `$PANEL_SEATS`
points at a different roster entirely. Full spec format: `AGENTS.md`.

| Seat | Model / path | Notes |
|---|---|---|
| `codex` | gpt-6.1-sol (2026-09-29), **xhigh** reasoning (2026-10-28), **yolo** (no sandbox, no approvals; 2026-09-22) | Only seat run inside the repo (`-C`). Ungraded. If 6.1 Sol genuinely errors, fall back to gpt-6-astra, not a 5.x model. |
| `codex-astra` / `codex-luna` | gpt-6-astra / gpt-6-luna, xhigh | Astra is **on by default** since 2026-10-29 (AA 55, above Sol's 52); Luna (AA 23) stays opt-in. Same lineage as `codex`: counted once. |
| `kimi` | newest Kimi K/Pro (K3.1 Pro), **high** | Ungraded. The CLI has no effort flag; the seat pins `KIMI_MODEL_THINKING_EFFORT=high` in its env (K3.1 Pro offers low/high/max). |
| `gemini-carbon` | Gemini 4 Carbon via OpenCode's Google API key, `#high`, read-only `plan` agent | The default Google voice (2026-11-10, AA 60); metered. `#high` is the route's top variant. `gemini-argon` (same route, AA 50), `gemini` (CLI free key, Flash), `gemini-flash` and `gemini-agy` are off-by-default fallbacks. |
| `grok` / `cursor-grok` | newest grok at xhigh, direct and via Cursor | Same model, two scaffolds → different failure modes. One voice, not two. |
| `muse` | newest Muse Wonder, xhigh | Signed-in Muse CLI (`muse exec`). |
| `glm` / `deepseek` | GLM-5.3 (`reasoning_effort` high: GLM takes only low/high/max) / DeepSeek V4.1 Flash 1017 (xhigh; AA 32 vs V4 Pro 0813's 23) | Alibaba ModelStudio **Standard** key. Both sometimes flood their reasoning and never emit a verdict. |
| `claude-*` | newest Sonnet / Opus / Fable, **xhigh** effort | Claude Code CLI print mode, signed in. One lineage across all three. |
| `mimo-flash-free` | MiMo V2.6 Flash via OpenCode Zen free | Off by default, `free` preset. Public context only. |
| `pplx` | Perplexity Agent API `fast-search` preset (sonar-reasoning-pro is the legacy fallback) | Search-GROUNDED — its edge is task-mode research questions where it cites the live web. |
| `qwen-max` | Qwen3.8 Max, the 0902 snapshot AA scores (34), xhigh | Same ModelStudio **Standard** key as `glm`/`deepseek` (not the Token Plan). The hosted Alibaba voice; the local Qwen seats share its lineage. |
| `local*` | Ollama: Qwen3.8-27B (this machine, or `local` on a second machine), gemma4:31b, GLM-4.5-Air | **Off by default; tripwires, not votes.** In the author's tests against a known answer key, self-hosted models found no real defects and several rubber-stamped CLEAN with fabricated justifications. A CLEAN from a local seat is noise — but if even a local seat FLAGS something, check it. `local-27b` goes first; `local-glm-air` is the alternate. Giants run serially (shared RAM pool). |

## Hard rules (each one was learned the expensive way)

1. **Neutral phrasing, always.** Never write "adversarial", "break", "attack",
   or "exploit" in a prompt. Those words trip OpenAI's cyber-content filter *at
   output time* and destroy the report after the full paid reasoning run. Say
   "verify correctness", "find defects", "check these invariants hold". Same
   rigor, different words. A filtered run means rephrase and re-run — never
   fall back to a weaker model.
2. **Synthesize, never relay.** The panel's output is evidence, not a verdict.
   Read every seat (via the subagent), then state your own conclusion.
3. **Reproduce contested claims before acting.** Seats confidently report
   defects that don't exist (a seat once reported a resolver as "never
   updated" when three others and the test suite showed otherwise) and
   sometimes report bugs already fixed in a later round. If a finding is real,
   write a failing test for it first. If you cannot establish it either way,
   record `unresolved` with the missing check or inconclusive evidence, not
   `rejected`. Record the outcome with `panel_verdict`: it is the measured
   evidence of which seats are right and which checks remain open.
4. **Fix, then re-panel.** One round is a snapshot, not a verdict. Fix every
   confirmed finding with a live repro before and a regression test after, then
   send the cumulative diff back with `--round N`. That injects the previous
   round's findings so seats narrow instead of repeating: broad semantics →
   edge cases → confirmations. Ten rounds is not excessive for a safety gate.
5. **Accepted residuals live in `<repo>/.panel/residuals.md`.** Anything you
   consciously decided not to fix goes there once and rides along in every
   later run's context automatically. Without it, every round rediscovers the
   same accepted risk and buries the new findings.
6. **A CLEAN verdict from a seat that found nothing all round is weak
   evidence.** Weight seats that reproduce and probe over seats that assert.
7. **Never send private code through the Alibaba Token Plan lane.** Its terms
   prohibit API calls outside coding tools and include a data-training clause.
   The `glm`/`deepseek`/`qwen-max` seats use the Standard key, which is fine;
   the Token-Plan `qwen` CLI seat was removed 2026-08-11.
8. **Missing findings block = no vote.** Some seats flood their reasoning and
   never emit one. `_summary.md` lists them under "no vote". Don't infer
   agreement from silence.

## Grading a seat instead of remembering it

When a run has a known answer key — a bug you already found, a review you
already settled — score it:

```bash
python3 "$PANEL" --grade ~/panel-runs/<repo>/<ts> --key key.json
```

```json
{"defects": [
  {"id": "d1", "file": "panel.py", "keywords": ["persistence", "blocking"],
   "desc": "finished seats are not written until the slow seat ends"}
]}
```

`_grade.md` shows found/missed per seat, unmatched findings per seat, and which
lineages caught each defect. Write the result into `seats.json` so seat choice
stays evidence rather than memory. Unmatched findings are not automatically
false — the key only knows the defects that were known when it was written.

## Synthesizing well

Consensus math: collapse shared lineages before counting agreement. Codex
(+codex-astra as one), gemini, kimi, grok(+cursor-grok as one), muse, glm,
deepseek, qwen-max, pplx and the Claude trio (as one) are the independent voices.

Intelligence: every seat line in `_summary.md` carries `AA <score>`, the
Artificial Analysis Intelligence Index for that model at the effort the seat
runs (`≤` = measured at a higher effort, `stale` = the pin moved since), and
the consensus table has a `top AA` column. When lineages disagree on a
judgment call and neither side shows a check, lean toward the higher score —
Opus 5.5 (57) over Qwen3.8 Max (34). Never drop a lower-scoring family for it:
each family reasons differently, which is why it is seated, and a finding with
a concrete repro outranks any score. `--list-seats` and `panel_seats` show the
scores.

Report to the user: what every seat agreed on, what exactly one seat caught
(often the real find), what was contradicted and how you resolved it, what you
reproduced yourself, and what you're choosing to accept as residual risk. Name
the seats — "kimi reproduced it, codex and grok missed it" is information the
user can calibrate on.

## Working on the panel itself

The repo ships as a plugin: this skill plus the `panel-synthesizer` agent.

```bash
cd <your panel checkout>
python3 -m unittest discover -s tests      # offline, no network or model calls
```

Seat wiring is pinned by tests, so a `seats.json` edit cannot quietly change
which model answers or at what effort.

Run the tests before committing anything in here.

The installed plugin is a **pinned copy** under
`~/.claude/plugins/cache/panel/panel/<version>/`, not a live link — editing the
repo does not change the panel you are running, which is deliberate: a run
should not shift under you mid-round. To ship an edit:

```bash
# bump "version" in .claude-plugin/plugin.json AND marketplace.json, then:
claude plugin validate <your panel checkout>
claude plugin marketplace update panel
claude plugin update panel@panel     # NOT `install` (no-ops when already
                                     # installed) and NOT bare `panel`
claude plugin details panel          # confirm the new version, then restart
```

Without a version bump the cache is not refreshed, so a forgotten bump means
you keep running the old panel while the repo looks current.

On another machine, install straight from GitHub:
`claude plugin marketplace add zohartito/model-panel` then
`claude plugin install panel@panel`.
