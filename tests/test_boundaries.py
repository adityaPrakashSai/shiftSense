"""Import boundary (11.3, 11.4 rule 3): app/solver imports only ortools, the standard library and
app.solver; the CLI may also read seed files through app.domain and app.planner."""

import ast
import sys
from pathlib import Path

SOLVER = Path(__file__).resolve().parents[1] / "app" / "solver"
FORBIDDEN = ("app.llm", "app.api", "app.intake", "app.explain", "app.store", "app.agent")
CLI_EXTRA = ("app.domain", "app.planner")


def imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_solver_imports_only_ortools_stdlib_and_itself() -> None:
    files = sorted(SOLVER.glob("*.py"))
    assert files
    for f in files:
        allowed_app = ("app.solver", *CLI_EXTRA) if f.name == "cli.py" else ("app.solver",)
        for mod in imports(f):
            top = mod.split(".")[0]
            assert not mod.startswith(FORBIDDEN), (f.name, mod)
            if top == "app":
                assert mod.startswith(allowed_app), (f.name, mod)
            else:
                assert top in sys.stdlib_module_names or top in ("ortools", "__future__"), (
                    f.name,
                    mod,
                )
