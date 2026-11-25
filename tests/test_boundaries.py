"""The package boundaries (CLAUDE.md): services never import each other, everything shared
lives under shared/, and inside shared/ the dependencies go one way.

Every non-test .py file under the five services and shared/ is parsed, and each import of
a repo package is checked: `import x`, `from x import y` at any nesting depth (so a lazy
import inside a function counts), and `importlib.import_module("x")` (also under an alias or
with `name=`), `__import__("x")`, `runpy.run_module("x")`, and `sys.modules["x"]` /
`sys.modules.get("x")` with a literal string. A top-level package the lists below don't name
fails too: it could carry a service's code to another service unseen.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SERVICES = ("api", "generator", "validator", "evaluation", "authoring")
SHARED = "shared"
TOOLING = ("scripts", "experiments", "deploy", "tests")
SKIP_DIRS = ("authoring/scaffold",)  # stdlib-only code copied into generated worlds, never imported
NOT_PACKAGES = ("web", "docs", "config", "output", "work")  # top-level directories that hold no importable code
IMPORT_CALLS = ("importlib.import_module", "import_module", "__import__", "runpy.run_module", "run_module")

# Inside shared/: which subpackage may import which. Anything not listed is forbidden.
SHARED_EDGES = {
    "contracts": set(),
    "storage": {"contracts"},
    "jobs": {"contracts", "storage"},
    "taxonomy": {"storage"},  # world selection reads the store
    "llm": {"contracts"},
    "taskformat": {"contracts", "llm"},  # the leak review calls the model
}


def is_test_file(path: Path) -> bool:
    return path.name.startswith("test_")


def unit_of(module: str) -> str | None:
    """The unit a dotted module name belongs to: a service, `shared.<area>`, `shared`
    (the package itself), `tooling`, or None for anything outside the repo's packages."""
    parts = module.split(".")
    if parts[0] in SERVICES:
        return parts[0]
    if parts[0] == SHARED:
        return f"{SHARED}.{parts[1]}" if len(parts) > 1 else SHARED
    if parts[0] in TOOLING:
        return "tooling"
    return None


def unit_of_file(rel: Path) -> str | None:
    parts = rel.with_suffix("").parts
    return unit_of(".".join(parts[:-1] if parts[-1] == "__init__" else parts))


def _literal(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def imports_of(source: str, package: str) -> list[tuple[int, str]]:
    """(line, dotted module) for every import in `source`; `package` is the dotted package
    of the file, for relative imports. `from x import y` yields `x.y` as well as `x`: `y`
    may be a submodule (`from shared import storage`)."""
    found: list[tuple[int, str]] = []
    tree = ast.parse(source)
    # `from importlib import import_module as load`: `load("x")` is an import too.
    aliases = {a.asname for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module in ("importlib", "runpy")
               for a in n.names if a.asname and a.name in ("import_module", "run_module")}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                up = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*up, base] if base else up)
            found.append((node.lineno, base))
            found += [(node.lineno, f"{base}.{alias.name}") for alias in node.names if alias.name != "*"]
        elif isinstance(node, ast.Call):
            func = _dotted(node.func)
            arg = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg in ("name", "mod_name")), None)
            name = _literal(arg) if arg is not None else None
            if name and (func in IMPORT_CALLS or func in aliases or func in ("sys.modules.get", "sys.modules.pop")):
                found.append((node.lineno, name))
        elif isinstance(node, ast.Subscript) and _dotted(node.value) == "sys.modules" and (name := _literal(node.slice)):
            found.append((node.lineno, name))
    return found


def forbidden(src: str, dst: str) -> str | None:
    """Why `src` may not import `dst` (both units), or None if it may."""
    if dst == src:
        return None
    if dst == "tooling":
        return "services and shared/ never import tooling"
    if src in SERVICES:
        return "a service never imports another service" if dst in SERVICES else None
    # src is under shared/
    if dst in SERVICES:
        return "nothing under shared/ imports a service"
    if dst == SHARED or src == SHARED:
        return None  # the package itself (shared/__init__.py holds no code)
    a, b = src.split(".")[1], dst.split(".")[1]
    if a not in SHARED_EDGES:
        return f"shared.{a} is not a known shared package (add it to SHARED_EDGES)"
    return None if b in SHARED_EDGES[a] else f"shared.{a} may import only {sorted(SHARED_EDGES[a]) or 'nothing'}"


def checked_files(root: Path) -> list[Path]:
    files = []
    for top in (*SERVICES, SHARED):
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root)
            if is_test_file(path) or "tests" in rel.parts or any(rel.as_posix().startswith(d + "/") for d in SKIP_DIRS):
                continue
            files.append(rel)
    return files


def edges(root: Path) -> dict[tuple[str, str], list[tuple[str, int, str]]]:
    """{(source unit, imported unit): [(file, line, module)]} over the checked files."""
    out: dict[tuple[str, str], list[tuple[str, int, str]]] = {}
    for rel in checked_files(root):
        src = unit_of_file(rel)
        package = ".".join(rel.parts[:-1])
        for line, module in imports_of((root / rel).read_text(), package):
            dst = unit_of(module)
            uses = out.setdefault((src, dst), []) if dst is not None and dst != src else None
            if uses is not None and (rel.as_posix(), line) not in [(f, n) for f, n, _ in uses]:
                uses.append((rel.as_posix(), line, module))  # one per line: `from x import y` yields x and x.y
    return out


def violations(root: Path) -> list[str]:
    found = []
    for (src, dst), uses in sorted(edges(root).items()):
        if why := forbidden(src, dst):
            found += [f"{file}:{line}: {src} -> {dst} (imports {module}): {why}" for file, line, module in uses]
    return found


def test_no_forbidden_imports():
    found = violations(ROOT)
    assert not found, "forbidden imports:\n" + "\n".join(found)


def test_every_shared_package_has_its_edges_listed():
    packages = {p.name for p in (ROOT / SHARED).iterdir() if p.is_dir() and (p / "__init__.py").is_file()}
    assert packages == set(SHARED_EDGES)


def test_every_top_level_package_is_listed():
    """A new top-level package would be invisible to the rule (its imports unchecked, and
    imports of it treated as third-party): it must be a service, shared/ or tooling."""
    packages = {p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")
                and p.name not in NOT_PACKAGES and any(p.rglob("*.py"))}
    assert packages <= {*SERVICES, SHARED, *TOOLING}, sorted(packages - {*SERVICES, SHARED, *TOOLING})


def test_the_allowed_shared_edges_have_no_cycle():
    def reaches(a: str, b: str, seen: frozenset = frozenset()) -> bool:
        return any(n == b or (n not in seen and reaches(n, b, seen | {n})) for n in SHARED_EDGES[a])

    assert not [a for a in SHARED_EDGES if reaches(a, a)]


def test_the_checker_catches_planted_violations(tmp_path):
    def write(rel: str, text: str) -> None:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    for top in (*SERVICES, SHARED):
        write(f"{top}/__init__.py", "")
    for area in SHARED_EDGES:
        write(f"shared/{area}/__init__.py", "")
    write("api/ok.py", "from shared.storage import sql\nfrom api import models\nimport json\n")
    write("shared/jobs/ok.py", "from shared.storage import sql\nfrom shared.contracts.models import JobType\n")
    assert violations(tmp_path) == []

    write("api/a.py", "from evaluation.calibration import in_band\n")
    write("generator/b.py", "def f():\n    if True:\n        from validator import gates\n        return gates\n")
    write("validator/c.py", "import importlib\nm = importlib.import_module('generator.llm')\n")
    write("evaluation/d.py", "x = __import__('api.service')\n")
    write("authoring/e.py", "import sys\nm = sys.modules['generator.author']\n")
    write("shared/jobs/f.py", "from generator import author\n")
    write("shared/storage/g.py", "from shared.jobs import jobqueue\n")
    write("shared/contracts/h.py", "import shared.storage.sql\n")
    write("generator/i.py", "from scripts import generate\n")
    write("shared/llm/j.py", "from . import k\nfrom ..taskformat import schema\n")
    write("api/test_k.py", "from evaluation import verdict\n")  # a test file: may import anything
    write("authoring/scaffold/l.py", "from generator import author\n")  # copied into worlds: skipped
    write("validator/n.py", "import sys\nm = sys.modules.get('generator.author')\n")
    write("validator/o.py", "from importlib import import_module as load\nm = load('generator.llm')\n")
    write("validator/p.py", "import importlib\nm = importlib.import_module(name='generator.llm')\n")
    write("validator/q.py", "import runpy\nrunpy.run_module('generator.service')\n")
    write("shared/newarea/__init__.py", "")
    write("shared/newarea/m.py", "from shared.contracts import models\n")

    found = violations(tmp_path)
    expected = [
        ("api/a.py:1:", "api -> evaluation"),
        ("generator/b.py:3:", "generator -> validator"),
        ("validator/c.py:2:", "validator -> generator"),
        ("evaluation/d.py:1:", "evaluation -> api"),
        ("authoring/e.py:2:", "authoring -> generator"),
        ("shared/jobs/f.py:1:", "shared.jobs -> generator"),
        ("shared/storage/g.py:1:", "shared.storage -> shared.jobs"),
        ("shared/contracts/h.py:1:", "shared.contracts -> shared.storage"),
        ("generator/i.py:1:", "generator -> tooling"),
        ("shared/llm/j.py:2:", "shared.llm -> shared.taskformat"),
        ("shared/newarea/m.py:1:", "shared.newarea -> shared.contracts"),
        ("validator/n.py:2:", "validator -> generator"),
        ("validator/o.py:2:", "validator -> generator"),
        ("validator/p.py:2:", "validator -> generator"),
        ("validator/q.py:2:", "validator -> generator"),
    ]
    for where, edge in expected:
        assert any(v.startswith(where) and edge in v for v in found), (where, edge, found)
    assert not [v for v in found if "test_k.py" in v or "scaffold" in v or "ok.py" in v]
    assert {v.split(":")[0] for v in found} == {where.split(":")[0] for where, _ in expected}
