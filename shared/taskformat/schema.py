"""
The structured output contract between the authoring model and the pipeline.

GLM-5.1 returns one JSON object matching ``TaskDraft``. Everything about a task
that code can check is checked here, before anything touches the filesystem:
path safety, required files, Dockerfile build-context consistency, Python
syntax, shell syntax, test-runner import limits, and that the
requirement-to-test coverage map names real tests. A failure raises a
``ValidationError`` whose message goes back to the model as a fix request.

Files the pipeline owns (task.toml, tests/test.sh, canary lines, shebangs) are
deliberately absent: materialize.py writes them, so the model cannot get them
wrong.
"""

import ast
import fnmatch
import json
import posixpath
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,48}[a-z0-9]$")

# Allowed base images and their pinned digests (scripts/pin_base_images.py).
BASE_IMAGES: dict[str, str] = json.loads(
    (Path(__file__).resolve().parents[2] / "config" / "base_images.json").read_text()
)

# The agent runs as this non-root user; materialize.py creates it.
AGENT_USER = "agent"
# Directories the agent must never be handed ownership of.
SYSTEM_DIRS = ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/sbin",
               "/sys", "/usr", "/var", "/tests", "/solution", "/logs")
# Nothing below these either. (A service's own /etc/<app> or /var/lib/<app> is fine.)
CLOSED_TREES = ("/bin", "/boot", "/dev", "/lib", "/lib64", "/proc", "/root", "/sbin", "/sys", "/usr",
                "/tests", "/solution", "/logs")
MIN_TESTS = 3
MAX_FILES = 80
MAX_TOTAL_BYTES = 2_000_000

# Paths the pipeline writes itself; a draft that supplies them is rejected.
RESERVED_ENV_PATHS = {"Dockerfile"}
RESERVED_TEST_PATHS = {"test.sh", "test_outputs.py"}
RESERVED_SOLUTION_PATHS = {"solve.sh", "wrong_fix.sh"}

# Strings that would leak grading mechanics into the agent-facing instruction.
INSTRUCTION_LEAKS = ("/tests", "test_outputs", "/solution", "solve.sh", "test.sh")

# Instruction headings or lead-ins that hand the agent the diagnosis.
HINT_HEADING_RE = re.compile(
    r"(?im)^\s*(#+\s*|\*\*)?(hints?|approach|suggested approach|key files[^\n]*|files to (investigate|inspect|"
    r"look at)|where to look|getting started with the fix)\s*:?\s*(\*\*)?\s*:?\s*$")

# Comments in the starting code that point the agent at the planted defect,
# e.g. "# BUG: ack happens before the write" or "// the bug is here".
DEFECT_MARKER_RE = re.compile(r"(#|//|/\*|--|<!--)[^\n]*?(\b(BUG|FIXME|XXX)\b|\b[Bb]ug\b)")


class _Strict(BaseModel):
    # extra="forbid" emits additionalProperties: false, which strict JSON
    # schema mode requires.
    model_config = ConfigDict(extra="forbid")


class FileEntry(_Strict):
    path: str = Field(description="POSIX path relative to the containing directory, e.g. 'jobqueue/worker.py'.")
    content: str = Field(description="Full file contents.")

    @field_validator("path")
    @classmethod
    def _safe_relative_path(cls, v: str) -> str:
        if not v or v.startswith("/") or "\\" in v:
            raise ValueError(f"path {v!r} must be a relative POSIX path")
        if posixpath.normpath(v) != v or v == "." or v.split("/")[0] == "..":
            raise ValueError(f"path {v!r} must be normalized and stay inside its directory")
        return v


class Constraint(_Strict):
    id: str = Field(description="Short identifier such as 'C1'.")
    statement: str = Field(description="The correctness constraint, stated as observable behavior.")


class CoverageItem(_Strict):
    requirement_id: str = Field(description="A constraint id from spec.correctness_constraints.")
    tests: list[str] = Field(description="Names of test functions in test_outputs that verify it.")


class TaskSpec(_Strict):
    """Author-facing design record. Never shown to the agent."""

    title: str
    summary: str = Field(description="Two or three sentences describing the task.")
    scenario: str = Field(description="The system the agent is dropped into and its situation.")
    root_cause: str = Field(description="The actual defect(s) and where they live. Hidden from the agent.")
    correctness_constraints: list[Constraint]
    subgoals: list[str] = Field(description="The dependent engineering subgoals, in order.")
    failure_scenario: str = Field(description="The reproducible failure and how the tests trigger it.")
    expected_agent_pitfalls: list[str] = Field(
        description="Plausible ways a capable agent gets this wrong (why the task is not trivial)."
    )
    anti_shortcut_measures: list[str] = Field(
        description="How the tests catch shortcuts such as hard-coding outputs or disabling the fault."
    )


class TaskDraft(_Strict):
    name: str = Field(description="Task slug: lowercase letters, digits and hyphens, 4-50 chars.")
    description: str = Field(description="One-sentence description for task.toml.")
    difficulty: Literal["easy", "medium", "hard"]
    category: str = Field(description="Single category, e.g. 'distributed-systems'.")
    tags: list[str]
    expert_time_estimate_min: float
    junior_time_estimate_min: float
    spec: TaskSpec
    instruction: str = Field(description="Markdown shown to the agent. Self-contained; never mentions tests.")
    dockerfile: str = Field(description="environment/Dockerfile. The build context is environment/. "
                            "FROM must be one of the allowed base images, written without a digest.")
    agent_writable_paths: list[str] = Field(
        description="Absolute directories outside WORKDIR that the non-root agent must be able to write "
        "(e.g. a data directory). WORKDIR is always made writable. Empty if none."
    )
    environment_files: list[FileEntry] = Field(
        description="Every file in the build context except the Dockerfile; paths relative to environment/."
    )
    solution_script: str = Field(
        description="Body of solution/solve.sh, without a shebang. Runs as root in WORKDIR after the image starts."
    )
    solution_files: list[FileEntry] = Field(
        description="Extra files the solution uses (e.g. patches), paths relative to solution/. "
        "Available at /solution/<path> while solve.sh runs."
    )
    wrong_fix_script: str = Field(
        default="",
        description="Body of solution/wrong_fix.sh, without a shebang: the plausible but WRONG fix a capable "
        "engineer would likely make (it fixes the visible symptom but breaks a stated requirement). The tests "
        "must reject it; the pipeline runs it like the reference solution and requires a failing verdict. "
        "Empty string if the task has none.",
    )
    test_outputs: str = Field(description="tests/test_outputs.py: pytest tests over the final container state.")
    test_support_files: list[FileEntry] = Field(
        description="Extra files the tests use (fixtures, helper modules), paths relative to tests/. "
        "Available at /tests/<path>."
    )
    coverage: list[CoverageItem] = Field(description="Requirement-to-test coverage mapping.")

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError(f"name {v!r} must match {SLUG_RE.pattern}")
        return v

    @model_validator(mode="after")
    def _check_task(self) -> "TaskDraft":
        errors: list[str] = []
        errors += _check_file_sets(self)
        errors += _check_dockerfile(self.dockerfile, [f.path for f in self.environment_files])
        errors += _check_writable_paths(self.agent_writable_paths)
        errors += _check_python_syntax(self)
        errors += _check_shell_syntax(self)
        test_names, test_errors = _check_tests(self)
        errors += test_errors
        errors += _check_coverage(self, test_names)
        errors += _check_instruction(self.instruction)
        errors += _check_defect_markers(self)
        if errors:
            raise ValueError("; ".join(errors))
        return self


def _check_file_sets(d: TaskDraft) -> list[str]:
    errors = []
    groups = [
        ("environment_files", d.environment_files, RESERVED_ENV_PATHS),
        ("solution_files", d.solution_files, RESERVED_SOLUTION_PATHS),
        ("test_support_files", d.test_support_files, RESERVED_TEST_PATHS),
    ]
    total_files = 0
    total_bytes = len(d.dockerfile) + len(d.solution_script) + len(d.test_outputs) + len(d.instruction)
    for label, files, reserved in groups:
        paths = [f.path for f in files]
        dupes = sorted({p for p in paths if paths.count(p) > 1})
        if dupes:
            errors.append(f"{label} has duplicate paths {dupes}")
        clash = sorted(set(paths) & reserved)
        if clash:
            errors.append(f"{label} must not contain {clash}; that file has its own field or is written by the pipeline")
        total_files += len(files)
        total_bytes += sum(len(f.content) for f in files)
    if not d.environment_files:
        errors.append("environment_files is empty; the agent needs a starting repository")
    if total_files > MAX_FILES:
        errors.append(f"{total_files} files exceeds the limit of {MAX_FILES}")
    if total_bytes > MAX_TOTAL_BYTES:
        errors.append(f"{total_bytes} bytes of content exceeds the limit of {MAX_TOTAL_BYTES}")
    return errors


def _dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    joined = re.sub(r"\\\r?\n", " ", text)
    out = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        out.append((keyword.upper(), rest.strip()))
    return out


def _check_dockerfile(text: str, env_paths: list[str]) -> list[str]:
    errors = []
    instructions = _dockerfile_instructions(text)
    froms = [args for kw, args in instructions if kw == "FROM"]
    if not froms:
        return ["dockerfile has no FROM instruction"]
    stages: set[str] = set()
    for args in froms:
        parts = [p for p in args.split() if not p.startswith("--")]
        image = parts[0] if parts else ""
        if image not in stages and image not in BASE_IMAGES:
            hint = " (written without a digest; the pipeline pins it)" if "@" in image else ""
            errors.append(f"FROM {image!r} is not an allowed base image{hint}; use one of {sorted(BASE_IMAGES)}")
        if len(parts) >= 3 and parts[1].upper() == "AS":
            stages.add(parts[2])
    if not any(kw == "WORKDIR" for kw, _ in instructions):
        errors.append("dockerfile must set a WORKDIR")

    for kw, args in instructions:
        if kw not in ("COPY", "ADD"):
            continue
        try:
            parts = json.loads(args) if args.startswith("[") else shlex.split(args)
        except (ValueError, json.JSONDecodeError):
            errors.append(f"cannot parse {kw} {args!r}")
            continue
        flags = [p for p in parts if p.startswith("--")]
        if any(f.startswith("--from") for f in flags):
            continue
        sources = [p for p in parts if not p.startswith("--")][:-1]
        for src in sources:
            if kw == "ADD" and re.match(r"^https?://", src):
                continue
            src = src[2:] if src.startswith("./") else src
            src = src.rstrip("/")
            if src in ("", "."):
                continue
            if src.split("/")[0] in ("tests", "solution") or src.startswith(".."):
                errors.append(f"{kw} {src!r}: tests and solution must never be copied into the image")
                continue
            if any(ch in src for ch in "*?["):
                matched = any(fnmatch.fnmatch(p, src) for p in env_paths)
            else:
                matched = src in env_paths or any(p.startswith(src + "/") for p in env_paths)
            if not matched:
                errors.append(f"{kw} source {src!r} does not exist in environment_files")
    return errors


def _check_writable_paths(paths: list[str]) -> list[str]:
    errors = []
    for p in paths:
        if not p.startswith("/") or posixpath.normpath(p) != p or any(ch in p for ch in " '\"$`\\;&|*?"):
            errors.append(f"agent_writable_paths entry {p!r} must be a plain normalized absolute path")
        elif p in SYSTEM_DIRS or any(p.startswith(d + "/") for d in CLOSED_TREES):
            errors.append(f"agent_writable_paths entry {p!r} is a system or grading directory")
    return errors


def dockerfile_workdir(text: str) -> str:
    """The final WORKDIR, resolved against earlier ones like Docker does."""
    workdir = "/"
    for kw, args in _dockerfile_instructions(text):
        if kw == "WORKDIR" and args:
            workdir = posixpath.normpath(posixpath.join(workdir, args.strip().strip('"')))
    return workdir


def _all_python_files(d: TaskDraft) -> list[tuple[str, str]]:
    files = [("tests/test_outputs.py", d.test_outputs)]
    for prefix, group in (("environment", d.environment_files), ("solution", d.solution_files),
                          ("tests", d.test_support_files)):
        files += [(f"{prefix}/{f.path}", f.content) for f in group if f.path.endswith(".py")]
    return files


def _check_python_syntax(d: TaskDraft) -> list[str]:
    errors = []
    for path, source in _all_python_files(d):
        try:
            compile(source, path, "exec")
        except SyntaxError as e:
            errors.append(f"{path} has a Python syntax error at line {e.lineno}: {e.msg}")
    return errors


def _check_shell_syntax(d: TaskDraft) -> list[str]:
    scripts = [("solution/solve.sh", d.solution_script)]
    if d.wrong_fix_script.strip():
        scripts.append(("solution/wrong_fix.sh", d.wrong_fix_script))
    for prefix, group in (("environment", d.environment_files), ("solution", d.solution_files),
                          ("tests", d.test_support_files)):
        scripts += [(f"{prefix}/{f.path}", f.content) for f in group if f.path.endswith(".sh")]
    errors = []
    if d.wrong_fix_script.strip() and d.wrong_fix_script.strip() == d.solution_script.strip():
        errors.append("wrong_fix_script is identical to solution_script; it must be a plausible WRONG fix")
    for path, source in scripts:
        result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True)
        if result.returncode != 0:
            errors.append(f"{path} has a bash syntax error: {result.stderr.strip()[:300]}")
    return errors


def _check_tests(d: TaskDraft) -> tuple[set[str], list[str]]:
    """Collect test names; enforce the verifier's isolated-interpreter limits."""
    try:
        tree = ast.parse(d.test_outputs)
    except SyntaxError:
        return set(), []  # already reported by the syntax check

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    names.add(item.name)
                    names.add(f"{node.name}.{item.name}")

    errors = []
    if len({n for n in names if "." not in n}) < MIN_TESTS:
        errors.append(f"test_outputs defines fewer than {MIN_TESTS} test functions")

    # The verifier runs pytest in an isolated Python 3.13 that has only pytest,
    # so tests may import the standard library, pytest, and their own helpers.
    helpers = {f.path[:-3].replace("/", ".").split(".")[0] for f in d.test_support_files if f.path.endswith(".py")}
    allowed = set(sys.stdlib_module_names) | {"pytest", "_pytest"} | helpers
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    bad = sorted(imported - allowed)
    if bad:
        errors.append(
            f"test_outputs imports {bad}, but tests run in an isolated interpreter with only the standard "
            "library and pytest; drive the system through subprocess, files or sockets instead"
        )
    return names, errors


def _check_coverage(d: TaskDraft, test_names: set[str]) -> list[str]:
    errors = []
    constraint_ids = [c.id for c in d.spec.correctness_constraints]
    if not constraint_ids:
        errors.append("spec.correctness_constraints is empty")
    covered = {c.requirement_id for c in d.coverage if c.tests}
    missing = [cid for cid in constraint_ids if cid not in covered]
    if missing:
        errors.append(f"constraints {missing} have no tests in the coverage mapping")
    unknown_ids = sorted({c.requirement_id for c in d.coverage} - set(constraint_ids))
    if unknown_ids:
        errors.append(f"coverage names unknown constraint ids {unknown_ids}")
    unknown_tests = sorted({t for c in d.coverage for t in c.tests} - test_names)
    if unknown_tests and test_names:
        errors.append(f"coverage names tests that test_outputs does not define: {unknown_tests}")
    return errors


def _check_defect_markers(d: TaskDraft) -> list[str]:
    """The agent reads the starting code, so it must not announce the defect."""
    errors = []
    for path, content in [("environment/Dockerfile", d.dockerfile)] + [
            (f"environment/{f.path}", f.content) for f in d.environment_files]:
        for lineno, line in enumerate(content.splitlines(), 1):
            if DEFECT_MARKER_RE.search(line):
                errors.append(f"{path}:{lineno} has a comment that points at the defect ({line.strip()[:80]!r}); "
                              "the starting code must read like ordinary code")
                break
    return errors


def _check_instruction(text: str) -> list[str]:
    if not text.strip():
        return ["instruction is empty"]
    errors = []
    leaks = [s for s in INSTRUCTION_LEAKS if s in text]
    if leaks:
        errors.append(f"instruction mentions grading internals {leaks}; it must not refer to tests or the solution")
    hints = [m.group(0).strip() for m in HINT_HEADING_RE.finditer(text)]
    if hints:
        errors.append(f"instruction has hint sections {hints}; describe symptoms and required guarantees, "
                      "not where to look or how to fix it")
    return errors
