# panel-on-tag — the EVENT loop

**Event:** a git tag (a release). **Action:** panel the release diff.
**Eval:** `panel_grade` against an answer key.

`scripts/panel-on-tag.py` computes the diff from the previous tag to the new
one, hands it to the panel as a code review, and then scores that run with
`panel_grade` so the loop's own quality is a number, not a vibe.

## Why a script and not a GitHub Action

The panel drives **local** model CLIs and credentials (codex login, gemini
login, keys under `~/.config/…`). A hosted CI runner has none of those, so the
event loop runs where the credentials live — on your machine, triggered off the
tag. CI can, at most, remind you to run it.

## Safety

- **Dry-run is the default and fires nothing.** You must pass `--execute` to
  launch a panel, and even then it launches **detached** — a panel takes 10–40
  minutes and you never block a foreground call on it (panel hard rule #1 lives
  in `AGENTS.md`).
- Prompts use **neutral phrasing** ("verify correctness", "find defects", "check
  these invariants hold") — the OpenAI cyber-content filter eats a finished run
  otherwise.

## Use

```bash
# 1) plan only (default): compute the diff, print the exact panel + grade commands
python3 scripts/panel-on-tag.py --repo ~/projects/my-service --tag v1.4.0

# 2) actually cut the panel (detached); prints the run dir + the eval command
python3 scripts/panel-on-tag.py --repo ~/projects/my-service --execute

# 3) the eval — score a finished run against the answer key (this is panel_grade)
python3 scripts/panel-on-tag.py \
    --grade ~/panel-runs/my-service/20260812_1200 \
    --key ~/panel-runs/_on-tag/my-service/v1.4.0/answer-key.json
```

`--tag` defaults to the latest tag in `--repo`; the previous tag is found with
`git describe`, falling back to the empty-tree base for a first release.

## Wiring it to the tag event

Git has no native `post-tag` hook, so pick one:

- **By hand, right after tagging** (simplest, recommended):
  `git tag v1.4.0 && python3 <panel>/scripts/panel-on-tag.py --repo . --execute`
- **`reference-transaction` hook** in the target repo
  (`.git/hooks/reference-transaction`), firing when a `refs/tags/*` is created:

  ```bash
  #!/bin/bash
  while read -r old new ref; do
    case "$ref" in
      refs/tags/*)
        [ "$1" = "committed" ] || continue
        python3 /path/to/panel/scripts/panel-on-tag.py \
          --repo "$(git rev-parse --show-toplevel)" \
          --tag "${ref#refs/tags/}" --execute &
        ;;
    esac
  done
  ```

  (Detached `&` so tagging never blocks; the panel runs on its own.)

## Structured output

- Plan/execute → `~/panel-runs/_on-tag/<repo>/<tag>/plan.json`
  (schema `panel.on-tag.v1`) plus the saved `<tag>.diff`.
- Eval → `<run_dir>/on-tag-gate.json` (schema `panel.on-tag.grade.v1`):
  per-seat found/of, `best_found`, `defects_missed_by_all`, and `passed`
  (true only when every known defect was caught by at least one seat and the key
  is non-empty). Exit code mirrors `passed`.

## The answer key

`panel_grade` scores against `{"defects": [{id, file, keywords, desc}]}`. Start
from `scripts/panel-answer-key.example.json`, copy it per release next to the
run, and fill it with the defects you already know are in that diff (from review
notes or a postmortem). An empty key never passes — the eval refuses to call a
run good against nothing.

## Flywheel

Each `--execute` and `--grade` appends a dated line to `.claude/LEARNINGS.md`
(created on first use, git-ignored)
(which run, which tag, best score, pass/fail) so seat selection and release
quality accrue evidence over time.
