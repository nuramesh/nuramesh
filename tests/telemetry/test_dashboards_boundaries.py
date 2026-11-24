"""R211-23 and R211-15 (import graph): the dashboards sit above the engine, not inside it."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "nuramesh"
INNER = ("core", "engine", "adapters", "stores")
BANNED = ("nuramesh.telemetry", "nuramesh.server.dashboards", "sentry_sdk", "opentelemetry")


def imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


def hits(names: set[str], banned: tuple[str, ...]) -> list[str]:
    return sorted(n for n in names for b in banned if n == b or n.startswith(b + "."))


def test_inner_layers_never_import_the_dashboards_telemetry_or_sentry() -> None:
    bad: list[str] = []
    for layer in INNER:
        for path in (SRC / layer).rglob("*.py"):
            bad += [f"{path.relative_to(SRC)}: {h}" for h in hits(imports(path), BANNED)]
    assert bad == []


def test_the_formulas_import_only_stdlib_and_core() -> None:
    names = imports(SRC / "telemetry" / "dashboards.py")
    outside = sorted(
        n for n in names if n.startswith("nuramesh.") and not n.startswith("nuramesh.core")
    )
    assert outside == []
    assert not hits(names, ("sentry_sdk", "opentelemetry", "nuramesh.telemetry.otel"))


def test_the_server_dashboards_never_import_otel_or_sentry() -> None:
    for name in ("dashboards.py", "dashboards_reader.py"):
        names = imports(SRC / "server" / name)
        assert not hits(names, ("sentry_sdk", "opentelemetry", "nuramesh.telemetry.otel")), name
