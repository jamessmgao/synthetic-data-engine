"""
Render a Harbor terminus-2 trial as readable Markdown: each step's analysis,
the commands it typed, and (truncated) terminal output, then the verdict.

Usage: python experiments/trace_to_md.py <trial_dir> <out.md>
"""

import json
import sys
from pathlib import Path

OUTPUT_CHARS = 1500


def render(trial: Path) -> str:
    steps = json.loads((trial / "agent" / "trajectory.json").read_text())["steps"]
    result = json.loads((trial / "result.json").read_text())
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    lines = [f"# Agent trace: {trial.name}", "",
             f"- Reward: **{reward}** ({'pass' if reward == 1.0 else 'fail'})",
             f"- Steps: {len(steps)}", f"- Started: {result.get('started_at')}  Finished: {result.get('finished_at')}",
             "- Source files: `agent/trajectory.json`, `agent/terminus_2.pane`, `agent/recording.cast`, "
             "`verifier/test-stdout.txt`", ""]
    for s in steps:
        lines.append(f"## Step {s.get('step_id')} ({s.get('source')})")
        msg = s.get("message")
        if isinstance(msg, list):
            msg = " ".join(x.get("text", "") for x in msg if isinstance(x, dict))
        if msg:
            # The first user message is everything the agent is told, so it is shown in full.
            lines += ["", str(msg).strip(), ""]
        for call in s.get("tool_calls") or []:
            keys = (call.get("arguments") or {}).get("keystrokes")
            if keys:
                lines += ["**Command:**", "", "```bash", str(keys).rstrip(), "```", ""]
        out = " ".join(str(r.get("content", "")) for r in ((s.get("observation") or {}).get("results") or []))
        if out.strip():
            shown = out if len(out) <= OUTPUT_CHARS else out[:OUTPUT_CHARS] + f"\n[... {len(out) - OUTPUT_CHARS} more chars ...]"
            lines += ["<details><summary>Terminal output</summary>", "", "```", shown.rstrip(), "```", "", "</details>", ""]
    verifier = trial / "verifier" / "test-stdout.txt"
    if verifier.exists():
        tail = verifier.read_text(errors="replace")[-3000:]
        lines += ["## Verifier (tests run after the agent finished)", "", "```", tail.rstrip(), "```", ""]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python experiments/trace_to_md.py <trial_dir> <out.md>")
    Path(sys.argv[2]).write_text(render(Path(sys.argv[1])))
    print(sys.argv[2])
