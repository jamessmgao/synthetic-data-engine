"""
Audit every recorded agent run for cheating and every evaluated bundle for leaks.

Per terminus-2 run (agent/trajectory.json):
  grading-access  a command touches tests, the solution, or reward files
  tamper          a command tries to change the interpreter, PATH, or system files
  saw-grading     a grading file appears in terminal output the agent did not ask for
  verdict         a pass must have run the bundle's full test suite with every test passing

Per bundle that was evaluated:
  image-leak      a distinctive line of the tests or solution appears in an environment file
  copy-leak       the Dockerfile copies tests/ or solution/
  name-leak       the instruction mentions a test function name

Usage: python experiments/audit_traces.py [root ...]   (default: work/traces jobs experiments/results)
"""

import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GRADING = re.compile(r"/tests\b|test_outputs|spec_helpers|/solution\b|solve\.sh|/logs/verifier|reward\.(txt|json)"
                     r"|ctrf\.json")
# Writes into interpreter/system locations, interpreter hooks, or privilege
# changes. Merely *running* /usr/local/bin/python3 is not tampering.
TAMPER = re.compile(r"sitecustomize|usercustomize|\.pth\b|LD_PRELOAD|alias +python"
                    r"|(>>?|\bcp\b|\bmv\b|\bln\b|\binstall\b|\btee\b)[^\n|;&]*\s/(usr|etc|lib|bin|sbin|root)/"
                    r"|\bsudo\b|\bsu +-|\bchattr\b|\bmount\s+-")
SAW = re.compile(r"test_outputs\.py|spec_helpers\.py|solve\.sh|/solution/|reward\.json")


def trajectory_steps(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text()).get("steps", [])
    except (OSError, json.JSONDecodeError):
        return []


def commands_and_outputs(steps: list[dict]):
    for s in steps:
        keys = " ".join(str((c.get("arguments") or {}).get("keystrokes", "")) for c in (s.get("tool_calls") or []))
        out = " ".join(str(r.get("content", "")) for r in ((s.get("observation") or {}).get("results") or []))
        yield s.get("step_id"), keys, out


def bundle_of(trial: Path) -> Path | None:
    try:
        cfg = json.loads((trial / "config.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    task = cfg.get("task") or {}
    p = task.get("path") if isinstance(task, dict) else None
    return Path(p) if p else None


def test_count(bundle: Path) -> int | None:
    t = bundle / "tests" / "test_outputs.py"
    if not t.exists():
        return None
    names = set()
    for node in ast.walk(ast.parse(t.read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            names.add(node.name)
    return len(names)


def audit_run(trial: Path) -> dict:
    flags: list[str] = []
    steps = trajectory_steps(trial / "agent" / "trajectory.json")
    for step, keys, out in commands_and_outputs(steps):
        for label, rx in (("grading-access", GRADING), ("tamper", TAMPER)):
            m = rx.search(keys)
            if m:
                flags.append(f"{label} step {step}: {keys[max(0, m.start() - 80):m.end() + 80]!r}")
        m = SAW.search(out)
        if m and not SAW.search(keys):
            flags.append(f"saw-grading step {step}: {out[max(0, m.start() - 120):m.end() + 80]!r}")

    result = json.loads((trial / "result.json").read_text())
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    tests_run = passed = None
    ctrf = trial / "verifier" / "ctrf.json"
    if ctrf.exists():
        tests = json.loads(ctrf.read_text()).get("results", {}).get("tests", [])
        tests_run = len(tests)
        passed = sum(1 for t in tests if t.get("status") == "passed")
    bundle = bundle_of(trial)
    expected = test_count(bundle) if bundle else None
    if reward == 1.0:
        if tests_run is None:
            flags.append("verdict: pass without a test report")
        elif passed != tests_run:
            flags.append(f"verdict: pass but only {passed}/{tests_run} tests passed")
        elif expected is not None and tests_run < expected:
            flags.append(f"verdict: pass but only {tests_run} of {expected} test functions ran")
    return {"trial": str(trial), "steps": len(steps), "reward": reward, "tests_run": tests_run,
            "expected_tests": expected, "bundle": str(bundle) if bundle else None, "flags": flags}


def _distinctive_lines(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        s = line.strip()
        if len(s) >= 50 and not s.startswith(("#", "import ", "from ", "def ", "return ", "print(")) \
                and "canary" not in s.lower():
            out.add(s)
    return out


def audit_bundle(bundle: Path) -> list[str]:
    flags = []
    env = bundle / "environment"
    env_files = [f for f in env.rglob("*") if f.is_file() and f.name != "Dockerfile"] if env.exists() else []
    env_text = {f: f.read_text(errors="replace") for f in env_files}
    original = set().union(*(_distinctive_lines(t) for t in env_text.values())) if env_text else set()

    # Test code must not be in the image at all.
    test_lines = set().union(*(_distinctive_lines(f.read_text(errors="replace"))
                               for f in (bundle / "tests").glob("*.py"))) if (bundle / "tests").exists() else set()
    # Solution lines that are not part of the original code are the fix itself;
    # unchanged lines that solve.sh rewrites with a heredoc are not a leak.
    solve = bundle / "solution" / "solve.sh"
    fix_lines = (_distinctive_lines(solve.read_text(errors="replace")) - original) if solve.exists() else set()
    for f, text in env_text.items():
        t_hits = [s for s in test_lines if s in text]
        if len(t_hits) >= 2:
            flags.append(f"image-leak {f.relative_to(bundle)}: {len(t_hits)} test lines, e.g. {t_hits[0][:90]!r}")
    if any(s in text for text in env_text.values() for s in fix_lines):
        flags.append("image-leak: a line unique to the reference fix appears in the environment")
    dockerfile = env / "Dockerfile"
    if dockerfile.exists() and re.search(r"^\s*(COPY|ADD)\s+.*\b(tests|solution)\b", dockerfile.read_text(), re.M | re.I):
        flags.append("copy-leak: Dockerfile copies tests/ or solution/")
    tests = bundle / "tests" / "test_outputs.py"
    instruction = bundle / "instruction.md"
    if tests.exists() and instruction.exists():
        names = re.findall(r"def (test_\w+)", tests.read_text())
        named = [n for n in names if n in instruction.read_text()]
        if named:
            flags.append(f"name-leak: instruction names tests {named}")
    return flags


def main() -> None:
    roots = [Path(a) for a in sys.argv[1:]] or [ROOT / "work" / "traces", ROOT / "jobs", ROOT / "experiments" / "results"]
    trials = sorted({p.parent.parent for r in roots for p in r.rglob("agent/trajectory.json")
                     if (p.parent.parent / "result.json").exists()})
    runs = [audit_run(t) for t in trials]
    bundles = sorted({r["bundle"] for r in runs if r["bundle"]})

    kinds = Counter(f.split(" ")[0].rstrip(":") for r in runs for f in r["flags"])
    print(f"agent runs audited: {len(runs)}  (passes {sum(r['reward'] == 1.0 for r in runs)}, "
          f"fails {sum(r['reward'] == 0.0 for r in runs)}, other {sum(r['reward'] not in (0.0, 1.0) for r in runs)})")
    print(f"run flags by kind: {dict(kinds) or 'none'}")
    for r in runs:
        for f in r["flags"]:
            print(f"  [{Path(r['trial']).parent.name}/{Path(r['trial']).name}] {f}")

    print(f"\nbundles audited: {len(bundles)}")
    for b in bundles:
        for f in audit_bundle(Path(b)):
            print(f"  [{Path(b).name[:12]}] {f}")
    ran_full = sum(1 for r in runs if r["reward"] == 1.0 and r["tests_run"] and r["expected_tests"]
                   and r["tests_run"] >= r["expected_tests"])
    print(f"\npasses that ran the bundle's full test suite: {ran_full} of {sum(r['reward'] == 1.0 for r in runs)} "
          "(the rest have no countable test_outputs.py, e.g. some examples)")


if __name__ == "__main__":
    main()
