"""The verifier file must stay standalone: standard library only, nothing from this repository."""

import ast
import sys

from verify_helpers import VERIFIER


def test_imports_only_the_standard_library() -> None:
    tree = ast.parse(VERIFIER.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            roots.add((node.module or "").split(".")[0])
    assert roots, "found no imports at all"
    assert roots <= set(sys.stdlib_module_names), roots


def test_names_no_dynamic_import_or_other_code_loading() -> None:
    text = VERIFIER.read_text(encoding="utf-8")
    for banned in ("__import__", "importlib", "exec(", "eval(", "socket", "urllib"):
        assert banned not in text, banned
    # The optional git mode (ND107) runs the git binary and nothing else: one call site, no
    # shell, and no other way to start a program.
    for banned in ("shell=True", "os.system", "os.popen", "os.exec", "os.spawn", "Popen"):
        assert banned not in text, banned
    assert text.count("subprocess.run(") == 1
