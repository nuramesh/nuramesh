"""Decision A8: `nuramesh.core` may import only the standard library, pydantic and itself."""

import ast
import sys
from pathlib import Path

import nuramesh.core

CORE_DIR = Path(nuramesh.core.__file__).parent
ALLOWED_THIRD_PARTY = {"pydantic", "pydantic_core"}


def _imported_modules(path: Path) -> set[str]:
    """Absolute module names imported by a file (relative imports stay inside core)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _violation(module: str) -> bool:
    top = module.split(".")[0]
    if top == "nuramesh":
        return not (module == "nuramesh.core" or module.startswith("nuramesh.core."))
    return top not in sys.stdlib_module_names and top not in ALLOWED_THIRD_PARTY


def test_core_imports_only_stdlib_pydantic_and_core() -> None:
    violations = sorted(
        f"{path.relative_to(CORE_DIR)}: {module}"
        for path in CORE_DIR.rglob("*.py")
        for module in _imported_modules(path)
        if _violation(module)
    )
    assert not violations, f"core imports outside its boundary: {violations}"


def test_boundary_check_catches_framework_and_outer_layer_imports(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import json, langgraph\n"
        "from pydantic import BaseModel\n"
        "from nuramesh.core.ledger import X\n"
        "from nuramesh.engine import Y\n"
    )
    flagged = {m for m in _imported_modules(bad) if _violation(m)}
    assert flagged == {"langgraph", "nuramesh.engine"}
