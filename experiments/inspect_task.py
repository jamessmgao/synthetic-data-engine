"""
Trust checks before believing a difficulty verdict. No model calls.

    python experiments/inspect_task.py audit <task_dir>       [--json]
    python experiments/inspect_task.py explain <trial_dir>    [--json]

audit: does the instruction state everything the hidden tests rely on?
    Pulls the concrete values the tests and their helper scripts use (logins
    and passwords, user@host, hosts, ports, URLs, absolute paths) and flags
    each one that appears neither in instruction.md nor anywhere the agent can
    see (environment/). oracle=1 and nop=0 can't catch this:
    configure-git-webserver's verifier logs in as git/password, which the
    instruction never mentions, so 0/5 there measured the task, not the model.

explain: why did this run fail?
    The agent's analysis per step and the commands it ran, the verifier's
    failing tests, and flags for the failure signatures seen so far:
      self_verified_but_failed  the agent tested the result itself and saw it
                                work, yet the verifier failed: suspect the task
      missing_tool              a command the task relies on isn't installed or on PATH
      edit_slip                 the agent noticed its own edit didn't land
      shell_killed              the tmux session died (e.g. a stray `exit`)
      exception                 Harbor recorded an exception (not a model failure)
"""

import argparse
import json
import re
import sys
from pathlib import Path

# --- audit ------------------------------------------------------------------------

# Paths every task may use without stating them.
SYSTEM_PATHS = re.compile(
    r"^/(tests|logs|usr|bin|sbin|lib|lib64|dev|proc|sys|tmp|var/lib/apt|var/cache|root/\.local|etc/apt)(/|$)")
HARNESS_WORDS = {"localhost", "127.0.0.1", "0.0.0.0"}

PATTERNS = {
    # Only quoted literals: a credential the tests log in with, not a variable name.
    "credential": re.compile(r"""(?ix)
        (?:password|passwd|passphrase|secret|token|api[_-]?key)["']?\s*[:=,]\s*["']([^"'\s]{3,})["']
      | _?login\(\s*["'][^"']+["']\s*,\s*["']([^"']{3,})["']     # login("admin", "admin123")
      | send\s+"([^"\\]+)\\r"                                   # expect: send "password\r"
      | chpasswd\W+([\w-]+:[^\s"']+)
    """),
    "user@host": re.compile(r"\b([a-z_][\w-]*@[A-Za-z0-9.-]+)(?=[:\s/'\"]|$)"),
    "url": re.compile(r"\bhttps?://[^\s\"'`)]+"),
    "port": re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\bport\W{1,3}):?(\d{2,5})\b", re.I),
    # Filesystem locations only; `/api/tasks/1` is an HTTP route, covered by url.
    "path": re.compile(r"(?<![\w.:/~-])(/(?:app|opt|etc|home|srv|var|data|git|workspace|mnt|run)(?:/[\w.@-]+)+)"),
}
# Answers to prompts, not secrets.
NOT_CREDENTIALS = {"yes", "no", "y", "n", "true", "false"}


def _stated(kind: str, value: str, visible: str) -> bool:
    """Is this value something the agent could know? Exact text, or for a
    login the exact `user@`, or for a URL the same port and path on any host."""
    if kind == "user@host":
        return f"{value.split('@')[0]}@" in visible
    if kind == "url":
        from urllib.parse import urlparse

        u = urlparse(value)
        try:
            port = u.port
        except ValueError:  # a templated port, e.g. http://localhost:{PORT}/x
            port = None
        # Same port and path on any host; the port may be given in prose ("port 8080").
        port_ok = port is None or re.search(rf"(?<!\d){port}(?!\d)", visible)
        path_ok = u.path in ("", "/") or u.path in visible
        return value in visible or bool(port_ok and path_ok)
    if kind == "port":
        return bool(re.search(rf"(?<!\d){value}(?!\d)", visible))
    return value in visible


def _is_email(value: str) -> bool:
    """git config user.email and the like: identities, not logins."""
    host = value.split("@")[1]
    return "." in host and host not in HARNESS_WORDS and not re.match(r"^\d+(\.\d+){3}$", host)


def _helper_files(task: Path) -> list[Path]:
    """Hidden test code: everything under tests/ except the pipeline-written runner."""
    files = []
    for p in sorted((task / "tests").rglob("*")):
        if p.is_file() and p.suffix in (".py", ".sh", ".exp", ".txt", ".json", "") and p.name != "test.sh":
            files.append(p)
    return files


def audit(task_dir: str) -> dict:
    task = Path(task_dir)
    instruction = (task / "instruction.md").read_text(errors="replace")
    visible = instruction + "\n" + "\n".join(
        p.read_text(errors="replace") for p in (task / "environment").rglob("*")
        if p.is_file() and p.stat().st_size < 200_000)

    findings, seen = [], set()
    for f in _helper_files(task):
        for lineno, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for kind, rx in PATTERNS.items():
                for m in rx.finditer(line):
                    value = next((g for g in m.groups() if g), m.group(0)) if m.groups() else m.group(0)
                    value = value.rstrip(".,:")
                    if kind == "path" and (SYSTEM_PATHS.match(value) or len(value) < 3):
                        continue
                    if kind == "credential" and value.lower() in NOT_CREDENTIALS:
                        continue
                    if kind == "user@host" and _is_email(value):
                        continue
                    stated_in_instruction = _stated(kind, value, instruction)
                    if (kind, value) in seen or _stated(kind, value, visible):
                        continue
                    seen.add((kind, value))
                    findings.append({"kind": kind, "value": value, "file": str(f.relative_to(task)),
                                     "line": lineno, "context": line.strip()[:160],
                                     "in_instruction": stated_in_instruction})
    severe = [x for x in findings if x["kind"] in ("credential", "user@host", "port", "url")]
    return {"task": task.name, "unstated": findings, "severe": len(severe),
            "verdict": "SUSPECT: tests rely on unstated values" if severe else
                       ("CHECK: unstated paths" if findings else "OK")}


# --- leaks --------------------------------------------------------------------------


def _step_lists_in_python(source: str) -> list[list[dict]]:
    """Every list literal of dicts that all have a "type" key (a scenario)."""
    import ast

    found = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts and all(isinstance(e, ast.Dict) for e in node.elts):
            try:
                value = ast.literal_eval(node)
            except ValueError:
                continue
            if all(isinstance(d, dict) and "type" in d for d in value):
                found.append(value)
    return found


def _step_lists_in_json(text: str) -> list[list[dict]]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    candidates = [value] + (list(value.values()) if isinstance(value, dict) else [])
    return [v for v in candidates
            if isinstance(v, list) and v and all(isinstance(d, dict) and "type" in d for d in v)]


def _contains(big: list, small: list) -> bool:
    n = len(small)
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


def leaks(task_dir: str, allowed_example_steps: set[str] | None = None,
          forbidden_phrases: tuple[str, ...] = ()) -> dict:
    """Does anything the agent can see give away the hidden tests or the fix?

    - a scenario in agent-visible files equal to, or containing, a scenario the
      hidden tests replay (the reproductions must stay hidden);
    - a visible scenario using a step type outside `allowed_example_steps`
      (e.g. only a happy path may ship: no restarts, no expiries);
    - a forbidden phrase (the fix stated as a rule) in any agent-visible text.
    """
    task = Path(task_dir)
    hidden = [s for p in (task / "tests").rglob("*.py") for s in _step_lists_in_python(p.read_text(errors="replace"))]
    visible_files = [task / "instruction.md", *[p for p in (task / "environment").rglob("*") if p.is_file()]]
    findings = []
    for p in visible_files:
        rel = str(p.relative_to(task))
        text = p.read_text(errors="replace")
        scenarios = _step_lists_in_json(text) if p.suffix == ".json" else (
            _step_lists_in_python(text) if p.suffix == ".py" else [])
        for s in scenarios:
            happy_path = allowed_example_steps is not None and {d["type"] for d in s} <= allowed_example_steps
            match = next((h for h in hidden if len(h) >= 3 and (_contains(s, h) or _contains(h, s) and len(s) >= 3)), None)
            if match is not None and not happy_path:
                findings.append({"kind": "hidden_scenario", "file": rel,
                                 "detail": f"agent-visible scenario reproduces a hidden test scenario: {json.dumps(s)[:240]}"})
            if allowed_example_steps is not None:
                extra = sorted({d["type"] for d in s} - allowed_example_steps)
                if extra:
                    findings.append({"kind": "failure_scenario", "file": rel,
                                     "detail": f"agent-visible scenario uses steps {extra}; only "
                                               f"{sorted(allowed_example_steps)} may appear in shipped examples"})
        # Phrases are judged in prose (instruction, docs), not code: the starting
        # code legitimately names its own mechanisms.
        prose = p.suffix in (".md", ".txt", ".rst") or p.name == "instruction.md"
        lower = text.lower() if prose else ""
        for phrase in forbidden_phrases if prose else ():
            if phrase.lower() in lower:
                i = lower.index(phrase.lower())
                findings.append({"kind": "states_the_fix", "file": rel,
                                 "detail": f"contains {phrase!r}: …{text[max(0, i - 80):i + 80]!r}…"})
    return {"task": task.name, "leaks": findings, "verdict": "LEAKS" if findings else "OK"}


def leak_feedback(result: dict) -> str:
    """The findings as repair instructions for the authoring model."""
    lines = ["The task gives away its hidden tests or its fix to the agent. Fix every item:"]
    for f in result["leaks"]:
        lines.append(f"- {f['file']}: {f['detail']}")
    lines.append("Remove every agent-visible file, example or sentence that reproduces a failure or states how the "
                 "fix must work. Keep one happy-path example at most. State requirements only as observable "
                 "outcomes. Do not change the hidden tests' expectations.")
    return "\n".join(lines)


# --- explain ------------------------------------------------------------------------

# Matched against the agent's own analysis text (what it concluded), except
# shell_killed, which only shows in the terminal output. Agents probe with
# `command -v X` all the time, so raw "command not found" output proves nothing.
SIGNATURES = {
    "missing_tool": re.compile(r"not on PATH|is unavailable|isn't installed|is not installed|not available in "
                               r"(the|this) (container|environment)", re.I),
    "edit_slip": re.compile(r"edit did not occur|was not modified|remains unchanged|did not match|still unprotected|"
                            r"not matching the file|duplicate", re.I),
}
SHELL_KILLED = re.compile(r"no server running on /tmp/tmux|session not found", re.I)
SELF_VERIFIED = re.compile(
    r"(validated|verified|tested|passed|succeeded)\b.{0,40}\bend[- ]to[- ]end|end[- ]to[- ]end\b.{0,60}"
    r"(succeeded|successful|passed|served|returned)|(workflow|verification|test|check)s? (passed|succeeded)|"
    r"successfully (validated|verified|tested|served|deployed)|(was|were|has been|have been) "
    r"(fully )?(validated|verified)|returned (HTTP )?200|all tests pass", re.I)


def _find(trial: Path, name: str) -> Path | None:
    hits = sorted(trial.rglob(name))
    return hits[0] if hits else None


def explain(trial_dir: str) -> dict:
    trial = Path(trial_dir)
    result_path = _find(trial, "result.json")
    traj_path = _find(trial, "trajectory.json")
    stdout_path = _find(trial, "test-stdout.txt")
    result = json.loads(result_path.read_text()) if result_path else {}
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    exc = (result.get("exception_info") or {}).get("exception_type")

    stdout = stdout_path.read_text(errors="replace") if stdout_path else ""
    failing = [ln[len("FAILED "):].strip() for ln in stdout.splitlines() if ln.startswith("FAILED ")]
    assertions = [ln.strip() for ln in stdout.splitlines() if ln.startswith("E  ")][:6]

    steps, flags, texts = [], set(), []
    if traj_path:
        traj = json.loads(traj_path.read_text())
        for s in traj.get("steps", [])[1:]:
            msg = s.get("message") or ""
            msg = msg if isinstance(msg, str) else json.dumps(msg)
            cmds = [(tc.get("arguments") or {}).get("keystrokes") or tc.get("function_name", "")
                    for tc in s.get("tool_calls") or []]
            obs = " ".join(str(r.get("content", "")) for r in ((s.get("observation") or {}).get("results") or []))
            texts.append(msg)
            for name, rx in SIGNATURES.items():
                if rx.search(msg):
                    flags.add(name)
            if SHELL_KILLED.search(obs):
                flags.add("shell_killed")
            steps.append({"step": s.get("step_id"), "analysis": msg[:400],
                          "commands": [" ⏎ ".join(c.strip().splitlines())[:160] for c in cmds if c and c.strip()]})
    # Did the agent see it working at the end? Only the last few steps count.
    if reward == 0 and any(SELF_VERIFIED.search(t) for t in texts[-3:]):
        flags.add("self_verified_but_failed")
    if exc:
        flags.add("exception")

    if exc:
        verdict = f"INVALID measurement ({exc}): not a model failure"
    elif "self_verified_but_failed" in flags:
        verdict = "SUSPECT TASK: the agent saw its result work; check the verifier for unstated requirements"
    elif reward == 0 and flags & {"missing_tool", "edit_slip", "shell_killed"}:
        verdict = "MECHANICAL: the failure involves tooling/editing slips; check whether the diagnosis was right"
    elif reward == 0:
        verdict = "MODEL FAILURE (no task-defect signature found): read the analysis below"
    else:
        verdict = "PASSED"
    return {"trial": trial.name, "reward": reward, "exception": exc, "verdict": verdict,
            "flags": sorted(flags), "failing_tests": failing, "assertions": assertions,
            "n_steps": len(steps), "metrics": (json.loads(traj_path.read_text()).get("final_metrics")
                                               if traj_path else None), "steps": steps}


# --- cli ----------------------------------------------------------------------------


def _print_audit(r: dict) -> None:
    print(f"{r['task']}: {r['verdict']}")
    for x in r["unstated"]:
        print(f"  [{x['kind']}] {x['value']!r}  ({x['file']}:{x['line']})  {x['context']}")


def _print_explain(r: dict) -> None:
    print(f"{r['trial']}: reward={r['reward']}  {r['verdict']}")
    print(f"  flags: {', '.join(r['flags']) or 'none'}   steps: {r['n_steps']}   metrics: {r['metrics']}")
    for t in r["failing_tests"]:
        print(f"  FAILED {t}")
    for a in r["assertions"]:
        print(f"    {a}")
    for s in r["steps"]:
        print(f"  --- step {s['step']}: {s['analysis'][:300]}")
        for c in s["commands"][:4]:
            print(f"      $ {c}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["audit", "explain"])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    fn, show = (audit, _print_audit) if args.command == "audit" else (explain, _print_explain)
    results = [fn(p) for p in args.paths]
    if args.json:
        json.dump(results, sys.stdout, indent=1)
    else:
        for r in results:
            show(r)
            print()


if __name__ == "__main__":
    main()
