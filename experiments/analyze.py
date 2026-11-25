"""
Build the pilots' data tables from experiments/results/<pilot>/.

Every number in the generated tables comes from a candidate record; nothing
is typed by hand. The narrative (why tasks land where they do) is written on
top of these tables.

Usage: python experiments/analyze.py [--pilot p1] > experiments/results/p1/tables.md
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEVELS = ["easy", "medium", "hard"]
MARK = {"in_band": "**IN BAND**", "too_easy": "too easy", "too_hard": "too hard", "invalid": "invalid (gates)",
        "generation_failed": "draft failed", "unresolved": "unresolved", "error": "error", None: "running"}


def load(pilot: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((ROOT / "experiments" / "results" / pilot / "candidates")
                                                      .glob("*.json"))]


def pattern(rec: dict) -> str:
    valid = [r for r in sorted(rec.get("rollouts", []), key=lambda r: (r["slot"], r["retry"]))
             if r["outcome"] != "INVALID"]
    return "".join("✓" if r["outcome"] == "VALID_PASS" else "✗" for r in valid) or "-"


def cell(rec: dict | None) -> str:
    if rec is None:
        return "–"
    v = rec.get("verdict")
    if v in ("in_band", "too_easy", "too_hard", "unresolved"):
        return f"{MARK[v]} {pattern(rec)}"
    return MARK.get(v, str(v))


def med(xs: list) -> str:
    xs = [x for x in xs if x is not None]
    return f"{statistics.median(xs):,.0f}" if xs else "–"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", default="p1")
    recs = load(ap.parse_args().pilot)
    by_id = {r["id"]: r for r in recs}
    out: list[str] = []

    # 1. Calibration: the human-written examples.
    out += ["### Calibration: examples/ against glm-5.3-flash", "",
            "| Example | Human label | Verdict | Runs (✓ pass, ✗ fail) |", "|---|---|---|---|"]
    for r in recs:
        if not r["id"].startswith("ex-"):
            continue
        label = "?"
        toml = Path(r.get("bundle_dir", "")) / "task.toml"
        if toml.exists():
            for line in toml.read_text().splitlines():
                if line.startswith("difficulty"):
                    label = line.split("=")[1].strip().strip('"')
        out.append(f"| {r['id'][3:]} | {label} | {MARK.get(r.get('verdict'), r.get('verdict'))} | {pattern(r)} |")

    # 2. The sweep grid.
    envs = sorted({r["id"].rsplit("-", 1)[0] for r in recs if not r["id"].startswith("ex-")})
    out += ["", "### Sweep: environment x requested difficulty", "",
            "| Environment | " + " | ".join(LEVELS) + " |", "|---|" + "---|" * len(LEVELS)]
    for env in envs:
        out.append(f"| {env} | " + " | ".join(cell(by_id.get(f"{env}-{lv}")) for lv in LEVELS) + " |")

    # 3. Verdicts by requested level.
    counts: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for r in recs:
        if r["id"].startswith("ex-"):
            continue
        counts[r["id"].rsplit("-", 1)[1]][r.get("verdict")] += 1
    out += ["", "### Verdicts by requested level", "",
            "| Level | in band | too easy | too hard | invalid | draft failed | other |", "|---|---|---|---|---|---|---|"]
    for lv in LEVELS:
        c = counts[lv]
        other = sum(n for v, n in c.items() if v not in ("in_band", "too_easy", "too_hard", "invalid",
                                                         "generation_failed"))
        out.append(f"| {lv} | {c['in_band']} | {c['too_easy']} | {c['too_hard']} | {c['invalid']} | "
                   f"{c['generation_failed']} | {other} |")

    # 4. Authoring and gates.
    gen = [r for r in recs if not r["id"].startswith("ex-")]
    drafts = [s for r in gen for s in r.get("steps", []) if s["type"] == "generate"]
    vals = [s for r in gen for s in r.get("steps", []) if s["type"] == "validate"]
    first_try = sum(1 for r in gen if any(s["type"] == "validate" for s in r.get("steps", []))
                    and next(s for s in r["steps"] if s["type"] == "validate")["outcome"] == "OK")
    gated = sum(1 for r in gen if any(s["type"] == "validate" for s in r.get("steps", [])))
    failed_gates: dict[str, int] = defaultdict(int)
    for s in vals:
        if s["outcome"] != "OK":
            failed_gates[s["failed_gate"]] += 1
    out += ["", "### Authoring and gates", "",
            f"- Drafts (incl. repairs): {len(drafts)}; median draft time {med([s['draft_sec'] for s in drafts])} s; "
            f"median completion tokens {med([s['completion_tokens'] for s in drafts])}",
            f"- Candidates that passed every gate on the first validate: {first_try} of {gated}",
            f"- Validate failures by gate: {dict(failed_gates) or 'none'}",
            f"- Schema rounds rejected (re-asked within a draft): "
            f"{sum(len(s['round_errors']) for s in drafts)}"]

    # 5. Model profile.
    rolls = [x for r in recs for x in r.get("rollouts", [])]
    passes = [x for x in rolls if x["outcome"] == "VALID_PASS"]
    fails = [x for x in rolls if x["outcome"] == "VALID_FAIL"]
    invalid = [x for x in rolls if x["outcome"] == "INVALID"]
    out += ["", "### glm-5.3-flash profile (all rollouts)", "",
            "| | runs | median input tokens | median steps | median duration (s) |", "|---|---|---|---|---|",
            f"| pass | {len(passes)} | {med([x['input_tokens'] for x in passes])} | {med([x['steps'] for x in passes])} "
            f"| {med([x['duration_sec'] for x in passes])} |",
            f"| fail | {len(fails)} | {med([x['input_tokens'] for x in fails])} | {med([x['steps'] for x in fails])} "
            f"| {med([x['duration_sec'] for x in fails])} |",
            f"| invalid | {len(invalid)} | | | |",
            "", "Invalid runs by exception: " + json.dumps(
                {e: sum(1 for x in invalid if x['exception'] == e) for e in {x['exception'] for x in invalid}})]

    # 6. Cost and time.
    cost = sum(r.get("cost_usd", {}).get("total", 0) for r in recs)
    auth = sum(r.get("cost_usd", {}).get("authoring", 0) for r in recs)
    in_band = [r for r in recs if r.get("verdict") == "in_band" and not r["id"].startswith("ex-")]
    out += ["", "### Cost", "",
            f"- Total ${cost:.2f} (authoring ${auth:.2f}, rollouts ${cost - auth:.2f})",
            f"- In-band generated tasks: {len(in_band)}"
            + (f"; ${cost / len(in_band):.2f} per in-band task including all misses" if in_band else "")]

    # 7. Failure evidence for in-band and too-hard tasks.
    out += ["", "### Failing tests per failed run (in-band and too-hard tasks)", ""]
    for r in recs:
        if r.get("verdict") not in ("in_band", "too_hard"):
            continue
        out.append(f"**{r['id']}** ({r.get('name', '')}) {pattern(r)}")
        for x in r["rollouts"]:
            if x["outcome"] == "VALID_FAIL":
                tests = ", ".join(t["test"] for t in x["failing_tests"]) or "(no ctrf)"
                out.append(f"- rollout{x['slot']}: {tests}")
        out.append("")
    print("\n".join(out))


if __name__ == "__main__":
    main()
