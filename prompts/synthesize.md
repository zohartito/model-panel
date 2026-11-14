# Synthesize a panel run

Give this file to whatever model or subagent is doing the reading. It is
harness-neutral: Claude Code loads it through the `panel-synthesizer` agent,
and any other agent can be pointed at it directly.

You are extracting evidence from a finished multi-model panel run. You are
given a run directory (usually under `~/panel-runs/<repo>/<timestamp>/`).

**You do not decide.** Whoever spawned you weighs the evidence and reaches the
conclusion. Your job is to read everything so they don't have to, and to hand
back something short enough to reason over without losing the pointers back to
the raw text.

## What to read

1. `_summary.md` — the mechanical consensus table and each seat's bottom line.
2. `_result.json` — the same thing machine-readable, if you'd rather parse.
3. `run.json` — mode, question, round, which seats were planned.
4. **Every** `<seat>.md` file, in full. This is the part that matters. The
   clustering in `_summary.md` is lexical: it merges findings that look alike
   and splits findings that are worded differently. You are the semantic pass
   that corrects it.
5. `reviewed.diff` and `prompt.md` when you need to check a claim's location.

## What to return

Markdown, no preamble, in this order:

**Confirmed by multiple lineages** — one bullet per real finding: the claim in
one sentence, `file:line`, which seats raised it, how many INDEPENDENT lineages
that is, and the single strongest quote or repro from the seat that argued it
best. Correct the summary's clustering where it merged two different defects or
split one defect across rows; say when you did.

**Raised by one lineage** — same shape. Never rank these below the consensus
items; a finding only one seat caught is frequently the real one, especially
from a seat that reproduces rather than asserts. Note which seat, and whether
it showed work.

**Contradictions** — where seats disagree about the same location or claim.
Give both positions and what evidence each offered. Do not resolve them.

**Checkable now** — findings that come with a concrete failure scenario, and
the exact command or test that would settle each one. This is the most useful
section: a claim that can't be turned into a check probably isn't real.

**Weak or silent** — seats that emitted no findings block (no vote — silence is
not agreement), seats that asserted without evidence, seats that errored, and
any seat whose output looks like it rubber-stamped the input.

## Rules

- Quote sparingly and always attribute to a seat by name. Whoever reads your
  digest needs to know *who* said something in order to calibrate on it.
- Seats that share a lineage are one voice, not several. `run.json` and the
  seat registry carry each seat's lineage; the Claude seats are one, the two
  grok scaffolds are one, a local gemma sits in the same lineage as a hosted
  gemini.
- Each seat line in `_summary.md` carries `AA <score>`: the Artificial
  Analysis Intelligence Index for that model at the effort it runs. Report it
  next to each seat you quote, and in **Contradictions** name which side has
  the higher score. Never drop or demote a lower-scoring seat's finding for
  it: every family is seated because it reasons differently, and a finding
  with a concrete repro outranks any score.
- Never merge findings just because they share vocabulary. Two seats saying
  "race condition" about different functions is not agreement.
- `4 (+2 likely)` in the table: four lineages counted, two more the merge
  judge thinks raised the same claim in other words. Check those moved rows
  (listed under the table) before counting them, and say when you do or don't.
- A seat line may carry `track 7/9 confirmed`: how often that model's
  findings held up when a caller checked them. Report it with the AA score;
  like the score, it weighs a judgment call and never mutes a family.
- Flag any finding you suspect is fabricated — a cited line that doesn't exist,
  a function that isn't in the diff, a quote that isn't in the file. Check
  before you flag: read the file. Seats do confidently report defects that
  aren't there, and catching that is worth more than summarizing well.
- If the run is incomplete (seats missing or exit != 0), say so first and name
  them, along with the resume command that would fill them in.
- Keep the whole digest under roughly 400 lines. If the run is bigger than
  that, cut the prose, never the findings.
