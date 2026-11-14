#!/usr/bin/env python3
"""seat-refresh — keep seats.json pointed at each provider's newest model.

Discovery is the models.dev open catalog (one keyless GET covers every
provider the panel seats). Each seat that pins a model id carries a `track`
block in seats.json:

    "track": {"provider": "xai", "match": "^grok-([0-9]+\\.[0-9]+)$",
              "format": "grok-{1}-xhigh"}

`match` filters that provider's catalog ids to the seat's family; the newest
match wins by release_date (numeric-aware id compare breaks ties). `format`
maps the winning id onto the string the seat's lane actually wants: `{id}` is
the whole id, `{1}`..`{n}` are match groups.

Modes:
  python3 scripts/seat-refresh.py                # report stale seats, change nothing
  python3 scripts/seat-refresh.py --apply        # verify each bump live, then rewrite
  python3 scripts/seat-refresh.py --apply --only grok,cursor-grok
  python3 scripts/seat-refresh.py --apply --notify   # what launchd runs, 4x a day

A track bumps versions within one line. A new line in a seated family (a
new tier name, released after and priced at or above what the family seats)
is reported as NEW LINE and never auto-wired: whether it is better is a call.

Challenger watch (every full run, report-only): a model listed on a route
panel calls (ROUTES in the catalog, plus `cursor-agent models`, `opencode
models` and the codex cache) that no seat runs, and whose Artificial Analysis
Intelligence Index score at the effort a seat would run it beats its
lineage's strongest default seat (or the weakest default seat, for a lab
panel does not seat), prints as CHALLENGER and gets its own notification.
AA rows are matched to catalog ids by models.dev `canonical_model_id` (and
the canonical listing's display name), exactly: anything else is UNMATCHED.
A score read only at max, or a lineage whose seats have no current score,
is UNCLEAR. New OpenCode models with no matched score print as UNSCORED. Scores need a
free key: AA_API_KEY in ~/.config/artificialanalysis/env (never printed).
Attribution to Artificial Analysis is required, so the report credits it.

Safety:
- **Verify before write.** --apply calls the seat with the NEW model id on a
  trivial prompt first; a lane that doesn't serve the model yet (Cursor lags
  xAI, Model Studio lags Zhipu) fails verification and the seat stays put.
- **No downgrades.** If the pinned model outranks the catalog's best (catalog
  lag, model pulled), nothing changes.
- **Surgical edits.** The seat's block is rewritten by exact string surgery,
  never json.dump, so hand formatting and comments-in-notes survive. A model
  change resets that seat's grade to "?" (the exam graded the old model).
- seats.json.bak is written before any change; the result must re-parse.

Scheduled runs (launchd, 07:25 / 11:25 / 15:25 / 19:25) wake with the Mac,
often before DNS is up, so the catalog fetch retries with backoff. --notify
alerts once per piece of news: the same lagging lane or new line is not
re-announced every run (state in $PANEL_RUNS/_seat-refresh-state.json), and
an unreachable catalog is itself an alert instead of a silent exit.

Exit codes: 0 = nothing stale (or every stale seat applied), 1 = stale seats
remain (check mode, or verification failed), 2 = catalog unavailable.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "panel"))
import panel  # noqa: E402

CATALOG_URL = "https://models.dev/api.json"
VERIFY_PROMPT = "Do not use tools or read files. Reply with exactly: OK"


def load_catalog(source=None):
    """The models.dev provider->models map, from a URL or a local file."""
    source = source or CATALOG_URL
    if not source.startswith(("http://", "https://")):
        return json.loads(Path(source).expanduser().read_text())
    # models.dev 403s the default Python-urllib agent
    req = urllib.request.Request(
        source, headers={"User-Agent": "panel-seat-refresh"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def fetch_catalog(source=None, attempts=6, first_wait=15, sleep=time.sleep):
    """load_catalog, retried with doubling waits (15 s .. 4 min) for a URL.

    launchd fires a missed run the moment the Mac wakes, usually before the
    network is back; one failed GET used to skip that whole day's refresh.
    A local file is read once: retrying cannot make it appear."""
    source = source or CATALOG_URL
    if not source.startswith(("http://", "https://")):
        return load_catalog(source)
    wait = first_wait
    for attempt in range(1, attempts + 1):
        try:
            return load_catalog(source)
        except Exception as exc:  # noqa: BLE001 - any fetch error is retryable
            if attempt == attempts:
                raise
            print(f"catalog fetch {attempt}/{attempts} failed ({exc}); "
                  f"retrying in {wait}s", file=sys.stderr)
            sleep(wait)
            wait *= 2


def _verkey(model_id):
    """Numeric-aware tie-break: (version digits, snapshot-date digits). A
    trailing snapshot date (-0902, -2026-05-17) only orders snapshots of one
    version: read as a version digit, qwen3.8-max-0902 would outrank
    qwen3.8.1-max."""
    m = re.search(r"-(\d{4}(?:-\d{2}-\d{2})?)$", model_id)
    core, date = (model_id[:m.start()], m.group(1)) if m else (model_id, "")
    return (tuple(int(n) for n in re.findall(r"\d+", core)),
            tuple(int(n) for n in re.findall(r"\d+", date)))


def _sort_key(model_id, meta):
    return ((meta or {}).get("release_date") or "", _verkey(model_id))


def _matches(catalog, track):
    models = catalog.get(track["provider"], {}).get("models", {})
    rx = re.compile(track["match"])
    return {mid: meta for mid, meta in models.items() if rx.match(mid)}


def pick_latest(catalog, track):
    """(model_id, release_date) of the newest catalog match, or None."""
    cands = _matches(catalog, track)
    if not cands:
        return None
    mid = max(cands, key=lambda m: _sort_key(m, cands[m]))
    return mid, (cands[mid] or {}).get("release_date")


def render(track, model_id):
    """Map a catalog id onto the lane's model string via the format template."""
    out = track["format"].replace("{id}", model_id)
    m = re.match(track["match"], model_id)
    for i, group in enumerate(m.groups() if m else (), start=1):
        out = out.replace("{%d}" % i, group or "")
    return out


def model_slot(entry):
    """(current_value, setter) for the seat's pinned model string.

    cmd seats pin it as the argv element after -m/--model; http seats as
    body.model of the first spec that has one. (None, None) if unpinned.
    """
    if "cmd" in entry:
        argv = entry["cmd"]["argv"]
        for i, arg in enumerate(argv[:-1]):
            if arg in ("-m", "--model"):
                return argv[i + 1], (
                    lambda v, argv=argv, i=i: argv.__setitem__(i + 1, v))
    specs = entry.get("http")
    for spec in (specs if isinstance(specs, list) else [specs] if specs else []):
        body = spec.get("body", {})
        if isinstance(body.get("model"), str):
            return body["model"], (
                lambda v, body=body: body.__setitem__("model", v))
    return None, None


def _key_of_rendered(catalog, track, value):
    """Sort key of the catalog model whose rendered string == value, if any."""
    for mid, meta in _matches(catalog, track).items():
        if render(track, mid) == value:
            return _sort_key(mid, meta)
    return None


def plan_refresh(registry, catalog, only=None):
    """One row per tracked seat: current vs newest, and what apply would do."""
    rows = []
    for name, entry in registry.items():
        track = entry.get("track")
        if not track or (only and name not in only):
            continue
        row = {"seat": name}
        current, _ = model_slot(entry)
        row["current"] = current
        if current is None:
            row.update(status="error",
                       detail="no -m/--model argv slot or body.model found")
            rows.append(row)
            continue
        latest = pick_latest(catalog, track)
        if latest is None:
            row.update(status="error",
                       detail=f"nothing under provider '{track['provider']}' "
                              f"matches {track['match']}")
            rows.append(row)
            continue
        latest_id, released = latest
        proposed = render(track, latest_id)
        row.update(latest_id=latest_id, released=released, proposed=proposed)
        if proposed == current:
            row["status"] = "current"
        else:
            current_key = _key_of_rendered(catalog, track, current)
            latest_key = _sort_key(latest_id, _matches(catalog, track)[latest_id])
            if current_key and current_key >= latest_key:
                row.update(status="current",
                           detail="pinned model outranks catalog best "
                                  "(never downgrade)")
            elif ((current_key is None or not current_key[0])
                  and _verkey(current) >= _verkey(proposed)):
                # the catalog cannot date the pin (absent, or listed without
                # a release date) but its digits are not older than the
                # proposal: hold rather than risk a downgrade
                row.update(status="current",
                           detail=f"pinned model "
                                  f"{'unknown to' if current_key is None else 'undated in'}"
                                  f" catalog; holding (never downgrade)")
            else:
                row["status"] = "stale"
                if current_key is None:
                    row["detail"] = "pinned model not in catalog"
        rows.append(row)
    return rows


def _output_price(meta):
    return ((meta or {}).get("cost") or {}).get("output")


def _stem(model_id):
    """Family name of an id: 'gemini-4-argon' -> 'gemini'."""
    m = re.match(r"[a-z]+", model_id)
    return m.group(0) if m else model_id


def find_new_lines(registry, catalog):
    """Models a seated family released no earlier than everything the panel
    pins for it, priced at or above the dearest model it pins, that no seat's track
    regex follows. A track only bumps versions within one line (sol, opus,
    argon); a brand-new line needs a human to wire it, so this reports and
    never edits. Resellers (a provider tracked by seats of more than one
    lineage, like opencode) are skipped: their catalogs mix every family.
    """
    tracked = [e for e in registry.values() if e.get("track")]
    lineages_of = {}
    for e in tracked:
        lineages_of.setdefault(e["track"]["provider"], set()).add(e["lineage"])
    found = []
    for lineage in sorted({e["lineage"] for e in tracked}):
        seats = [e for e in tracked if e["lineage"] == lineage]
        pinned, unseen = [], False
        for e in seats:
            current, _ = model_slot(e)
            hits = [(mid, meta or {})
                    for mid, meta in _matches(catalog, e["track"]).items()
                    if render(e["track"], mid) == current]
            unseen = unseen or not hits
            pinned += hits
        # "after and at least as dear as EVERY pin": a pin the catalog cannot
        # find, date or price leaves that bar unknown, so say nothing
        if unseen or any(not meta.get("release_date")
                         or _output_price(meta) is None
                         for _, meta in pinned):
            continue
        newest = max(meta["release_date"] for _, meta in pinned)
        price_bar = max(_output_price(meta) for _, meta in pinned)
        stems = {_stem(mid) for mid, _ in pinned}
        # pinned-id-plus-suffix (-highspeed, -fast) is the seated model
        # served differently, not a new line
        variants = tuple(f"{mid}-" for mid, _ in pinned)
        for provider in sorted({e["track"]["provider"] for e in seats}):
            if len(lineages_of[provider]) > 1:
                continue
            followed = [re.compile(e["track"]["match"]) for e in tracked
                        if e["track"]["provider"] == provider]
            for mid, meta in catalog.get(provider, {}).get("models", {}).items():
                meta = meta or {}
                price = _output_price(meta)
                # >=, not >: a new tier shipped the same day as the
                # version a track just bumped to is still news
                if (_stem(mid) in stems
                        and (meta.get("release_date") or "") >= newest
                        and price is not None and price >= price_bar
                        and not mid.startswith(variants)
                        and not any(rx.match(mid) for rx in followed)):
                    found.append({"lineage": lineage, "provider": provider,
                                  "id": mid,
                                  "released": meta.get("release_date"),
                                  "output_price": price})
    return found


# --------------------------------------------------------- challenger watch
# One question: is there a model panel could call today that is smarter
# than the seats it runs? Scores are the Artificial Analysis Intelligence
# Index (free API; attribution required, so the report credits it). Callable
# means listed on a route panel calls, in the models.dev catalog or a CLI's
# own model list. It only nominates: wiring a seat stays a reviewed change.

AA_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
AA_KEY = {"file": "~/.config/artificialanalysis/env", "env_key": "AA_API_KEY"}
AA_CREDIT = "Artificial Analysis Intelligence Index, artificialanalysis.ai"
ROUTES = ("opencode", "opencode-go", "google", "openai", "anthropic", "xai",
          "moonshotai", "kimi-code-plan-global", "meta", "alibaba")
LOCAL_LISTS = (("cursor-agent", ["cursor-agent", "models"]),
               ("opencode-cli", ["opencode", "models"]))
CODEX_CACHE = Path.home() / ".codex" / "models_cache.json"
UNSCORED_ROUTES = ("opencode", "opencode-go")
NEW_DAYS = 14
CHALLENGER_TAG, UNSCORED_TAG = "challenger: ", "unscored: "
# The level a seat would run a model at: xhigh where offered, else the top
# level below max (policy 2026-10-28: never max). Unlabeled sits mid-table.
EFFORT_RANK = {"xhigh": 5, "high": 4, "reasoning": 4, None: 3, "medium": 2,
               "low": 1, "minimal": 0, "none": 0, "non-reasoning": 0}
_EFFORT_WORDS = ("non-reasoning", "xhigh", "high", "medium", "low",
                 "minimal", "max", "none", "reasoning")


def _norm(text):
    """'GPT-6.1 Sol' and 'gpt-6.1-sol' -> 'gpt-6-1-sol'."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def split_effort(name):
    """('gemini-4-carbon', 'high') from AA's 'Gemini 4 Carbon (high)'. AA
    lists each effort as its own row and says which only in the name. A
    trailing parenthetical naming no effort ('Qwen3.8 Max (0902)') is part
    of the model's name."""
    m = re.search(r"\(([^()]*)\)\s*$", name or "")
    if m:
        words = re.findall(r"[a-z]+(?:-[a-z]+)*", m.group(1).lower())
        for effort in _EFFORT_WORDS:
            if effort in words:
                return _norm(name[:m.start()]), effort
    return _norm(name), None


def _measured_key(measured):
    """AA key of a seat's `measured` note: 'Claude Fable 5.2 (xhigh), read
    2026-11-09' -> 'claude-fable-5-2'."""
    m = re.match(r"[^,(]*(?:\([^)]*\))?", measured or "")
    head = m.group(0).strip() if m else ""
    return split_effort(head)[0] if head else None


_EFFORT_SUFFIX = r"-(xhigh|high|medium|low|minimal|max|none)$"


def _local_key(model_id):
    """The bare model of a lane string or CLI id: drop the route prefix,
    '#effort', and the -fast / -<effort> / -thinking serving suffixes."""
    mid = re.sub(r"-fast$", "", model_id.split("#")[0].rsplit("/", 1)[-1])
    mid = re.sub(_EFFORT_SUFFIX, "", mid)
    return _norm(re.sub(r"-thinking$", "", mid))


def _id_effort(model_id):
    """The effort a CLI id pins ('gemini-4-carbon-low' -> 'low'); None
    when the id leaves it to the call."""
    mid = re.sub(r"-fast$", "", model_id.split("#")[0].rsplit("/", 1)[-1])
    m = re.search(_EFFORT_SUFFIX, mid)
    return m.group(1) if m else None


def _aa_keys(row):
    """(exact keys, lab-stripped aliases) an AA row answers to. Exact: its
    name without the effort, and its slug (without the effort suffix when
    the name declares that effort: slug 'carbon-high' of 'Carbon (High)'
    is the model 'carbon'). Aliases drop a leading lab name ('Tencent Hy4'
    -> 'hy4') when a model name, not a bare number, remains; they only
    match ids in that lab's own catalog namespace."""
    base, effort = split_effort(row.get("name"))
    slug = _norm(row.get("slug"))
    if effort and slug.endswith(f"-{_norm(effort)}"):
        slug = slug[:-len(_norm(effort)) - 1]
    exact = {base, slug} - {""}
    lab = _norm((row.get("model_creator") or {}).get("name"))
    aliases = {k[len(lab) + 1:] for k in exact
               if lab and k.startswith(f"{lab}-")
               and k[len(lab) + 1:][:1].isalpha()}
    return exact, aliases - exact


def _aa_score(row):
    value = (row.get("evaluations") or {}).get(
        "artificial_analysis_intelligence_index")
    return value if isinstance(value, (int, float)) else None


def load_aa(source=None, key=None, max_pages=20):
    """(rows, intelligence_index_version) from AA's free model list, every
    page, or from a local JSON file holding one response. A list that runs
    past max_pages, or pages on two index versions, raise: half a list
    would read as models that went away."""
    source = source or AA_URL
    if not source.startswith(("http://", "https://")):
        body = json.loads(Path(source).expanduser().read_text())
        return list(body.get("data") or []), body.get("intelligence_index_version")
    rows, versions = [], set()
    for page in range(1, max_pages + 1):
        req = urllib.request.Request(
            f"{source}?page={page}",
            headers={"x-api-key": key, "User-Agent": "panel-seat-refresh"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        rows += body.get("data") or []
        versions.add(body.get("intelligence_index_version"))
        if not (body.get("pagination") or {}).get("has_more"):
            break
    else:
        raise ValueError(f"model list runs past {max_pages} pages")
    if len(versions) > 1:
        raise ValueError(f"pages on index versions {sorted(map(str, versions))}")
    return rows, versions.pop()


def _major(version):
    m = re.search(r"(?:^|\bv)(\d+)", str(version)) if version is not None else None
    return int(m.group(1)) if m else None


def aa_scores(source, seats_index):
    """(rows, note, needs_human). rows is None when there is nothing to
    compare against: no key, a rejected key, or a new index version (a
    human fixes those), or AA unreachable this run (the next run retries).
    The key never reaches a message."""
    key = None
    if not source or source.startswith(("http://", "https://")):
        key, _ = panel.load_key(AA_KEY)
        if not key:
            return None, (f"scores need a free Artificial Analysis key: "
                          f"AA_API_KEY in {AA_KEY['file']}"), True
    try:
        rows, version = load_aa(source, key)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):  # a rejected key does not fix itself
            return None, (f"Artificial Analysis rejected the key (HTTP "
                          f"{exc.code}): check AA_API_KEY in "
                          f"{AA_KEY['file']}"), True
        return None, f"Artificial Analysis answered HTTP {exc.code}", False
    except Exception as exc:  # noqa: BLE001 - any fetch error waits a run
        return None, (f"Artificial Analysis unavailable "
                      f"({type(exc).__name__})"), False
    want, got = _major(seats_index), _major(version)
    if want is not None and got is not None and want != got:
        return None, (f"AA now serves Intelligence Index v{version}; seat "
                      f"scores are {seats_index}: re-read them first"), True
    return rows, None, False


def parse_model_list(text):
    """Ids from `cursor-agent models` ('id - Name') or `opencode models`
    (one 'provider/id' per line); headers and 'auto' are not models."""
    ids = []
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9][\w.\-/:@]*)\s*(?:$| - )", line)
        if m and m.group(1) != "auto":
            ids.append(m.group(1))
    return list(dict.fromkeys(ids))


def local_routes(timeout=60):
    """{route: ids} from the codex models cache and the CLIs on this machine. A
    missing or logged-out one is skipped: it only adds routes."""
    found = {}
    try:
        cache = json.loads(CODEX_CACHE.read_text())
        found["codex"] = [m["slug"] for m in cache.get("models", [])
                          if m.get("slug") and m.get("visibility") != "hide"]
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    for route, argv in LOCAL_LISTS:
        exe = shutil.which(argv[0])
        if not exe:
            continue
        try:
            done = subprocess.run([exe, *argv[1:]], capture_output=True,
                                  text=True, timeout=timeout, check=False,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            continue
        ids = parse_model_list(done.stdout) if done.returncode == 0 else []
        if ids:
            found[route] = ids
    return found


def _models_of(catalog, provider):
    return (catalog.get(provider) or {}).get("models") or {}


def callable_models(catalog, local):
    """Every model a route panel calls lists, with the keys an AA row can
    match: the catalog's canonical id (plus the display name of the listing
    whose id IS that canonical id); a CLI id without one by its bare id.
    `effort` is what a CLI id pins (cursor's -low/-high); None = any."""
    entries = []
    for route in ROUTES:
        for mid, meta in _models_of(catalog, route).items():
            entries.append({"route": route, "id": mid, "label": f"{route}/{mid}",
                            "meta": meta or {}, "effort": None})
    for route, ids in sorted((local or {}).items()):
        for mid in ids:
            prov, _, bare = mid.rpartition("/")
            meta = _models_of(catalog, prov).get(bare) if prov else None
            entries.append({"route": route, "id": mid, "label": f"{route}:{mid}",
                            "meta": meta or {},
                            "effort": None if meta else _id_effort(mid)})
    for e in entries:
        meta = e.pop("meta")
        e["canonical"] = meta.get("canonical_model_id")
        e["released"] = meta.get("release_date")
        if e["canonical"]:
            part = e["canonical"].split("/", 1)[-1]
            e["keys"] = {_norm(part)}
            if e["id"].rsplit("/", 1)[-1] == part and meta.get("name"):
                e["keys"].add(_norm(meta["name"]))
        else:
            e["keys"] = {_local_key(e["id"])}
    return entries


def _seat_pins(entry):
    """Every model string a seat runs: its pin, then any other http route's
    body.model (a fallback route still runs that seat's model)."""
    specs = entry.get("http")
    specs = specs if isinstance(specs, list) else [specs] if specs else []
    pins = [model_slot(entry)[0]] + [(s.get("body") or {}).get("model")
                                     for s in specs]
    return [p for p in dict.fromkeys(pins) if isinstance(p, str) and p]


def _resolve_pin(catalog, pin):
    """Catalog metas a pin names. 'provider/id' looks only in that provider
    when the catalog has it; any other pin looks in every route, and counts
    only if they all agree on one canonical model."""
    prov, _, bare = pin.split("#")[0].rpartition("/")
    if prov in catalog:
        meta = _models_of(catalog, prov).get(bare)
        return [meta] if meta else []
    metas = [m for m in (_models_of(catalog, r).get(bare) for r in ROUTES) if m]
    return metas if len({m.get("canonical_model_id") for m in metas}) == 1 else []


def _seat_models(registry, catalog):
    """Per seat: lineage, the canonical ids it runs, the AA keys that name
    them, and the catalog namespaces that are its lab. A pin the catalog
    resolves claims that canonical model only; a bare pin key counts only
    for a pin it cannot resolve (`measured` names the AA row either way).
    Namespaces also come from the track's whole line, so a pin the catalog
    lags keeps its lineage."""
    seats = []
    for name, entry in registry.items():
        pins = _seat_pins(entry)
        if not pins:
            continue
        track, canon, keys = entry.get("track"), set(), set()
        for pin in pins:
            metas = _resolve_pin(catalog, pin)
            if track:
                metas += [meta for mid, meta in _matches(catalog, track).items()
                          if render(track, mid) == pin]
            found = {m.get("canonical_model_id") for m in metas
                     if (m or {}).get("canonical_model_id")}
            canon |= found
            if not found:
                keys.add(_local_key(pin))
        keys |= {_norm(c.split("/", 1)[-1]) for c in canon}
        info = entry.get("intelligence") or {}
        if info.get("measured") and info.get("model") == pins[0]:
            keys.add(_measured_key(info["measured"]))
        line = _matches(catalog, track).values() if track else ()
        namespaces = {c.split("/", 1)[0] for c in canon} | {
            m["canonical_model_id"].split("/", 1)[0] for m in line
            if (m or {}).get("canonical_model_id")}
        seats.append({"seat": name, "lineage": entry.get("lineage"),
                      "canon": canon, "keys": keys - {None, ""},
                      "namespaces": namespaces})
    return seats


def _bars(registry):
    """({lineage: its strongest measured default seat}, the weakest measured
    default seat, {lineages with a default seat}). The bar is the roster
    panel runs by default: a model that only beats a non-default seat is
    weaker than every seat it would sit beside."""
    scored, seated = [], set()
    for name, entry in registry.items():
        if not entry.get("default"):
            continue
        seated.add(entry.get("lineage"))
        score = panel.intelligence_of(name, registry)
        if score is not None:
            scored.append({"seat": name, "lineage": entry.get("lineage"),
                           "score": score, "ceiling": bool(
                               (entry.get("intelligence") or {}).get("ceiling"))})
    best = {}
    for s in scored:
        if s["score"] > best.get(s["lineage"], {"score": float("-inf")})["score"]:
            best[s["lineage"]] = s
    weakest = min(scored, key=lambda s: s["score"]) if scored else None
    return best, weakest, seated


def _unique_map(pairs):
    """{k: v} for pairs whose key never maps to two values."""
    out, clash = {}, set()
    for k, v in pairs:
        if k in out and out[k] != v:
            clash.add(k)
        out.setdefault(k, v)
    return {k: v for k, v in out.items() if k not in clash}


def _pick_effort(rows, allowed=None):
    """(row, effort, ceiling): the scored row a seat would run on these
    routes: xhigh where offered, else the top level below max, among the
    efforts the routes run (allowed None = any). A max-only score is a
    ceiling. None when no scored row is runnable."""
    scored = [(r, e) for r, e in rows if _aa_score(r) is not None
              and (allowed is None or e in allowed)]
    below = [(r, e) for r, e in scored if e != "max"]
    if below:
        row, effort = max(below, key=lambda p: (EFFORT_RANK.get(p[1], 3),
                                                _aa_score(p[0])))
        return row, effort, False
    if scored:
        row, effort = max(scored, key=lambda p: _aa_score(p[0]))
        return row, effort, True
    return None


def challenger_watch(registry, catalog, aa_rows=None, local=None, today=None):
    """{challengers, unmatched, unclear, unscored}. A challenger is callable
    on a route, run by no seat, and scores (at the effort a seat would run
    it there) above its lineage's strongest default seat, or above the
    weakest default seat for a lab panel does not seat. Above that bar but
    undecidable: `unmatched` when no exact, unique callable id matches the
    AA row (never a guess); `unclear` when the lineage's seats have no
    current score, or AA has no score at a runnable effort. New models on
    the OpenCode routes (last NEW_DAYS days) nothing judged are `unscored`.
    """
    today = today or datetime.date.today()
    entries = callable_models(catalog, local)
    by_key = {}
    for e in entries:
        for k in e["keys"]:
            by_key.setdefault(k, []).append(e)
    seats = _seat_models(registry, catalog)
    seated_canon = set().union(*(s["canon"] for s in seats))
    seated_keys = set().union(*(s["keys"] for s in seats))
    best, weakest, seated_lineages = _bars(registry)
    found = {"challengers": [], "unmatched": [], "unclear": [], "note": None}
    judged = set()
    if aa_rows and weakest is None:
        # nothing to compare against: judge nothing, so new models stay
        # listed as unscored instead of vanishing
        found["note"] = "no default seat has a current score to compare against"
        aa_rows = None

    groups = {}
    for row in aa_rows or []:
        base, effort = split_effort(row.get("name"))
        if base:
            groups.setdefault(base, []).append((row, effort))
    prefix_lineage = _unique_map((ns, s["lineage"])
                                 for s in seats for ns in s["namespaces"])
    creator_lineage = _unique_map(
        ((row.get("model_creator") or {}).get("id"), s["lineage"])
        for rows in groups.values() for row, _ in rows
        for s in seats if _aa_keys(row)[0] & s["keys"])

    for base, rows in sorted(groups.items()):
        exact, aliases = set(), set()
        for row, _ in rows:
            row_exact, row_aliases = _aa_keys(row)
            exact |= row_exact
            aliases |= row_aliases
        lab = _norm((rows[0][0].get("model_creator") or {}).get("name"))
        hits = {e["label"]: e for k in exact for e in by_key.get(k, [])}
        hits.update({e["label"]: e for k in aliases for e in by_key.get(k, [])
                     if e["canonical"] and e["canonical"].split("/", 1)[0]
                     .replace("-", "") == lab.replace("-", "")})
        hits = list(hits.values())
        canons = {e["canonical"] for e in hits if e["canonical"]}
        if exact & seated_keys or canons & seated_canon:
            continue
        scored = [(r, e) for r, e in rows if _aa_score(r) is not None]
        if not scored:
            continue  # listed without a score: its routes stay unscored
        if len(canons) <= 1:
            judged |= {e["label"] for e in hits}
        efforts = None if not hits or any(e["effort"] is None for e in hits) \
            else {e["effort"] for e in hits}
        pick = _pick_effort(scored, efforts)
        if pick:
            row, effort, ceiling = pick
        else:  # nothing scored at a runnable effort: show the best for context
            row, effort = max(scored, key=lambda p: _aa_score(p[0]))
            ceiling = effort == "max"
        creator = row.get("model_creator") or {}
        lineage = (prefix_lineage.get(next(iter(canons)).split("/", 1)[0])
                   if len(canons) == 1 else None) \
            or creator_lineage.get(creator.get("id"))
        bar = best.get(lineage) or weakest
        score = _aa_score(row)
        # seat scores are AA scores rounded to whole numbers (argon stores
        # 50 for 50.4): compare on that scale, or 50.3 would beat 50.4
        missing_lineage_score = lineage in seated_lineages and lineage not in best
        if bar is None or (not missing_lineage_score and round(score) <= bar["score"]):
            continue
        item = {"name": row.get("name"), "score": score, "effort": effort,
                "ceiling": ceiling, "lineage": lineage if lineage in best else None,
                "creator": creator.get("name"), "released": row.get("release_date"),
                "bar_seat": bar["seat"], "bar_score": bar["score"],
                "bar_ceiling": bar["ceiling"],
                "ident": next(iter(canons)) if len(canons) == 1 else base,
                "routes": sorted(e["label"] for e in hits)}
        if not hits:
            why, kind = "no callable id matches it exactly", "unmatched"
        elif len(canons) > 1:
            why, kind = "ambiguous: " + ", ".join(sorted(canons)), "unmatched"
        elif missing_lineage_score:
            why, kind = (f"the {lineage} seats have no current score to "
                         f"compare against"), "unclear"
        elif pick is None:
            why, kind = (f"AA has no score at the effort its routes run "
                         f"({', '.join(sorted(efforts))})"), "unclear"
        elif ceiling:
            why, kind = "AA scored it only at max, which no seat runs", "unclear"
        else:
            why, kind = None, "challengers"
        found[kind].append(dict(item, why=why) if why else item)


    window = ((today - datetime.timedelta(days=NEW_DAYS)).isoformat(),
              today.isoformat())
    unscored = {}
    for e in entries:
        if (e["route"] not in UNSCORED_ROUTES
                or not window[0] <= (e["released"] or "") <= window[1]
                or e["label"] in judged or e["canonical"] in seated_canon
                or e["keys"] & seated_keys):
            continue
        ident = e["canonical"] or e["id"]
        item = unscored.setdefault(ident, {"id": ident, "routes": [],
                                           "released": e["released"]})
        item["routes"] = sorted(item["routes"] + [e["label"]])
    for kind in ("challengers", "unmatched", "unclear"):
        found[kind].sort(key=lambda c: -c["score"])
    found["unscored"] = sorted(unscored.values(),
                               key=lambda u: (u["released"] or "", u["id"]),
                               reverse=True)
    return found


def _scored(item):
    return f"AA {'≤' if item['ceiling'] else ''}{item['score']:g}"


def _bar_label(item):
    who = f"the {item['lineage']} seat" if item["lineage"] \
        else "the weakest default seat"
    mark = "≤" if item["bar_ceiling"] else ""
    return f"{who} {item['bar_seat']} ({mark}{item['bar_score']})"


def watch_news(watch):
    """(key, text) per alert. The key is the model, so alert-once holds
    while the text around it (score, routes, bar) moves; each challenger is
    its own notification. Unclear and unmatched rows are report-only."""
    news = []
    for c in watch["challengers"]:
        route = next((r for r in c["routes"] if ":" not in r), c["routes"][0])
        news.append((f"{CHALLENGER_TAG}{c['ident']}",
                     f"{CHALLENGER_TAG}{c['name']} scores AA "
                     f"{round(c['score'])}, above {_bar_label(c)} - callable "
                     f"on {route}"))
    for u in watch["unscored"]:
        news.append((f"{UNSCORED_TAG}{u['id']}",
                     f"{UNSCORED_TAG}{u['id']} is new on "
                     f"{', '.join(sorted({r.split('/', 1)[0] for r in u['routes']}))}"))
    return news


def state_path():
    return Path(os.environ.get("PANEL_RUNS")
                or Path.home() / "panel-runs") / "_seat-refresh-state.json"


OUTAGE = "catalog unavailable; seats not checked this run"


def _load_state():
    """(announced, pending, texts): texts holds the words of a pending
    item whose key is not its message, for a retry no later run rebuilds."""
    try:
        state = json.loads(state_path().read_text())
    except (OSError, ValueError):
        state = {}
    return (set(state.get("announced", [])), list(state.get("pending", [])),
            dict(state.get("texts") or {}))


def _save_state(announced, pending, texts=None):
    path = state_path()
    words = {n: w for n, w in (texts or {}).items() if n in pending and w != n}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"announced": sorted(announced),
                                    "pending": pending, "texts": words},
                                   indent=1))
    except OSError:
        pass  # alerts may repeat next run; the refresh itself still stands


def alert(news, keep=False, texts=None):
    """Notify only the news the previous run did not already announce.

    `announced` holds what is still true as of this run, so a condition (a
    lagging lane, a new line) is announced again only after it went away
    and came back. `keep` carries those forward when this run could not
    judge them all (catalog unreachable, or --only skipped the rest); a
    callable `keep` carries only the items it accepts.
    `pending` holds messages that failed to deliver, retried every run until
    one lands: a bump is a one-shot event no later run would regenerate.
    A run that reached the catalog ends any outage (see end_outage).
    Each challenger is its own notification; the rest share one. `texts`
    maps an item to what the notification says, so the item itself can
    stay a stable identity while the words around it change."""
    announced, pending, saved = _load_state()
    if OUTAGE not in news:
        announced.discard(OUTAGE)
        pending = [n for n in pending if n != OUTAGE]
    to_send = pending + [n for n in news
                         if n not in announced and n not in pending]
    rest = [n for n in to_send if not n.startswith(CHALLENGER_TAG)]
    batches = [[n] for n in to_send if n.startswith(CHALLENGER_TAG)]
    sent, failed = [], []
    words = {**saved, **(texts or {})}
    for batch in batches + ([rest] if rest else []):
        delivered = notify("; ".join(words.get(n, n) for n in batch))
        (sent if delivered else failed).extend(batch)
    carry = set(filter(keep, announced)) if callable(keep) \
        else (announced if keep else set())
    # told now; a full run drops what is no longer true
    _save_state((set(news) | carry | set(sent)) - set(failed), failed, words)
    return sent


def end_outage():
    """A run without --notify that reached the catalog still ends an
    outage, so the next one alerts."""
    announced, pending, texts = _load_state()
    if OUTAGE in announced or OUTAGE in pending:
        _save_state(announced - {OUTAGE}, [n for n in pending if n != OUTAGE],
                    texts)


def notify(message):
    """One macOS notification, so a new model surfaces outside the log.
    True when it was delivered."""
    if not shutil.which("osascript"):
        return False
    # AppleScript has no \u escapes (error -2741): keep non-ASCII literal
    text = json.dumps(message, ensure_ascii=False)
    done = subprocess.run(["osascript", "-e", f"display notification "
                           f"{text} with title \"panel seat-refresh\""],
                          capture_output=True, check=False)
    return done.returncode == 0


# ------------------------------------------------------------- file surgery
# seats.json is hand-formatted; json.dump would flatten it into a 600-line
# diff. Edits are exact-string replacements scoped to the one seat's block.

def _close_brace(text, open_at):
    """Index just past the brace closing text[open_at], string-literal aware."""
    depth, in_string, escaped = 0, False, False
    for i in range(open_at, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
    return None


def seat_span(text, name):
    """(start, end) of `"name": {...}` at seats level, string-literal aware."""
    m = re.search(r'^    %s: \{' % re.escape(json.dumps(name)), text, re.M)
    if not m:
        raise ValueError(f"seat block for '{name}' not found")
    end = _close_brace(text, m.end() - 1)
    if end is None:
        raise ValueError(f"unbalanced braces in seat block '{name}'")
    return m.start(), end


def apply_change(text, name, old, new):
    """Swap the quoted model string inside one seat's block; reset its grade.

    The old value must occur exactly once in the block's wiring — anything
    else means the surgery would be ambiguous, so bail to a human. The
    intelligence block is not wiring: it keeps the id its score was measured
    on, so the score reads as stale after the bump.
    """
    start, end = seat_span(text, name)
    block = text[start:end]
    needle, repl = json.dumps(old), json.dumps(new)
    intel = re.search(r'"intelligence":\s*\{', block)
    cut = (intel.start(), _close_brace(block, intel.end() - 1)) if intel else (0, 0)
    if None in cut:
        raise ValueError(f"unbalanced braces in {name}'s intelligence block")
    head, frozen, tail = block[:cut[0]], block[cut[0]:cut[1]], block[cut[1]:]
    found = head.count(needle) + tail.count(needle)
    if found != 1:
        raise ValueError(
            f"{name}: expected exactly one {needle} in the seat block, "
            f"found {found}")
    block = head.replace(needle, repl) + frozen + tail.replace(needle, repl)
    # the grade was earned by the old model; the exam record goes with it
    block = re.sub(r'("grade": )"[^"]*"', r'\1"?"', block, count=1)
    block = re.sub(r'\n\s*"exam":\s*"(?:\\.|[^"\\])*",', "", block, count=1)
    # An exam can also be the last property, where the preceding comma
    # belongs to it. Do not leave the old model's exam on the new model.
    block = re.sub(r',\n\s*"exam":\s*"(?:\\.|[^"\\])*"(?=\s*\})',
                   "", block, count=1)
    return text[:start] + block + text[end:]


def verify_seat(entry, new_value, timeout):
    """Run the seat with the NEW model id on a trivial prompt. (ok, detail)."""
    candidate = copy.deepcopy(entry)
    _, setter = model_slot(candidate)
    setter(new_value)
    seat = panel.make_seat(candidate)
    rc, out = seat(VERIFY_PROMPT, Path.cwd(), timeout)
    # Some CLIs exit zero after printing a provider error or login banner.
    # Require the requested answer on its own line (allow CLI bullets/ANSI).
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", out)
    if rc == 0 and re.search(r"(?m)^\s*(?:[•*-]\s*)?OK[.!]?\s*$", clean):
        return True, "OK"
    return False, f"rc={rc}: {out.strip()[:200]}"


def print_watch(watch):
    """The CHALLENGER section of the text report."""
    credit = f"scores: {AA_CREDIT}" if watch["scored"] else "no scores this run"
    print(f"-- challenger watch: callable models smarter than the seats "
          f"({credit})")
    for c in watch["challengers"]:
        print(f"{'CHALLENGER':14} {c['name']} {_scored(c)} > {_bar_label(c)};"
              f" callable: {', '.join(c['routes'])} - wire it by hand")
    for label in ("unmatched", "unclear"):
        for u in watch[label]:
            print(f"{label.upper():14} {u['name']} {_scored(u)} "
                  f"({u['creator'] or 'unknown lab'}) > {_bar_label(u)}; "
                  f"{u['why']}")
    for u in watch["unscored"]:
        print(f"{'UNSCORED':14} {u['id']} (released {u['released']}) on "
              f"{', '.join(u['routes'])}; no AA score matches it")
    if not any(watch[k] for k in ("challengers", "unmatched", "unclear",
                                   "unscored")):
        print(f"{'':14} no challenger: nothing callable beats its seat")
    if watch["note"]:
        print(f"{'':14} {watch['note']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="verify each stale seat's new model live, then "
                         "rewrite seats.json (default: report only)")
    ap.add_argument("--no-verify", action="store_true",
                    help="with --apply: skip the live verification call")
    ap.add_argument("--only", default="",
                    help="restrict to these seats (comma-separated)")
    ap.add_argument("--catalog", default=None,
                    help=f"catalog URL or local JSON file (default {CATALOG_URL})")
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds per verification call")
    ap.add_argument("--aa", default=None,
                    help=f"Artificial Analysis URL or local JSON file for the "
                         f"challenger watch (default {AA_URL})")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--notify", action="store_true",
                    help="post a macOS notification when a seat was bumped, "
                         "failed verification, or a new model line appeared")
    args = ap.parse_args(argv)

    try:
        catalog = fetch_catalog(args.catalog)
    except Exception as exc:  # noqa: BLE001 - offline is a clean exit, not a stack
        print(f"catalog unavailable: {exc}", file=sys.stderr)
        if args.notify:
            alert([OUTAGE], keep=True)
        return 2
    if not args.notify:
        end_outage()

    seats_path = panel.REGISTRY_PATH
    registry = panel.load_registry(seats_path)
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    rows = plan_refresh(registry, catalog, only)

    stale = [r for r in rows if r["status"] == "stale"]
    if args.apply and stale:
        text = seats_path.read_text()
        shutil.copy2(seats_path, seats_path.with_suffix(".json.bak"))
        for row in stale:
            if not args.no_verify:
                ok, detail = verify_seat(registry[row["seat"]],
                                         row["proposed"], args.timeout)
                row["verify"] = detail
                if not ok:
                    row["status"] = "verify-failed"
                    continue
            text = apply_change(text, row["seat"], row["current"],
                                row["proposed"])
            row["status"] = "applied"
        json.loads(text)  # the surgery must leave valid JSON, or nothing lands
        seats_path.write_text(text)

    # judged against the pins as they stand now, after any bump above
    new_lines = [] if only else find_new_lines(
        panel.load_registry(seats_path), catalog)

    # report-only: nominates challengers, never edits seats.json
    watch, aa_rows, human = None, None, False
    if not only:
        try:
            index = (json.loads(seats_path.read_text()).get("intelligence")
                     or {}).get("index")
            aa_rows, note, human = aa_scores(args.aa, index)
            watch = challenger_watch(panel.load_registry(seats_path), catalog,
                                     aa_rows, local_routes())
            human = human or bool(watch["note"])
            watch.update(note=note or watch["note"], note_needs_human=human,
                         scored=aa_rows is not None and not watch["note"])
        except Exception as exc:  # noqa: BLE001 - never sink the refresh
            print(f"challenger watch failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            watch = None

    if args.notify:
        news = [f"{r['seat']} -> {r['proposed']} (re-read its intelligence "
                f"score)" for r in rows if r["status"] == "applied"]
        news += [f"{r['seat']} could not verify {r['proposed']}" for r in rows
                 if r["status"] == "verify-failed"]
        news += [f"new {n['lineage']} model {n['id']}" for n in new_lines]
        # a default seat panel now skips as a dead lane needs a human (top up
        # the account, or log in again): say so once, not every run
        defaults = [n for n, e in registry.items() if e.get("default")]
        news += [f"{name} lane is down ({why}): top up or re-login"
                 for name, why in panel.dead_lanes(defaults).items()]
        # No scores this run cannot say a challenger went away, so keep
        # it announced; AA merely down (or the watch broke) also keeps the
        # unscored list instead of flooding its fallback.
        transient = watch is None or (aa_rows is None and not human)
        carried = () if only or watch and watch["scored"] \
            else (CHALLENGER_TAG, UNSCORED_TAG) if transient \
            else (CHALLENGER_TAG,)
        texts = {}
        if watch and not transient:
            texts = dict(watch_news(watch))
            news += list(texts)
            if watch["note"] and human:
                news.append(f"challenger watch {watch['note']}")
        # --only skips the new-line scan and the other seats: keep what it
        # could not re-check
        undecided = {CHALLENGER_TAG + item["ident"] for item in watch["unclear"]} if watch else set()
        alert(news, keep=bool(only) or (lambda key: key.startswith(carried) or key in undecided),
              texts=texts)

    if args.as_json:
        print(json.dumps({"seats_file": str(seats_path), "rows": rows,
                          "new_lines": new_lines, "challengers": watch},
                         indent=2))
    else:
        for r in rows:
            line = f"{r['seat']:14} {r['status']:14} {r.get('current', '')}"
            if r["status"] != "current" and r.get("proposed"):
                line += f" -> {r['proposed']} (released {r.get('released')})"
            if r.get("detail"):
                line += f"  [{r['detail']}]"
            if r.get("verify"):
                line += f"  verify: {r['verify']}"
            print(line)
        for n in new_lines:
            print(f"NEW LINE       {n['provider']}/{n['id']} (released "
                  f"{n['released']}, ${n['output_price']}/M out) - no "
                  f"{n['lineage']} seat tracks it; wire it by hand if better")
        if watch:
            print_watch(watch)
    return 1 if any(r["status"] in ("stale", "verify-failed") for r in rows) \
        else 0


if __name__ == "__main__":
    raise SystemExit(main())
