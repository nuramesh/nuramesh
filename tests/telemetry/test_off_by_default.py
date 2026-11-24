"""Off by default (E3 spec 12.7, case E3-15) and the import boundary (12.8), checked in a fresh
interpreter so earlier imports of other tests cannot hide anything."""

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

SRC = Path(__file__).resolve().parents[2] / "src" / "nuramesh"

PROBE = textwrap.dedent(
    """
    import json, socket, sys, threading
    from opentelemetry import metrics, trace

    providers = (trace.get_tracer_provider(), metrics.get_meter_provider())

    def no_socket(*a, **k):
        raise AssertionError("the exporter opened a socket")

    socket.create_connection = no_socket
    socket.getaddrinfo = no_socket
    from fastapi.testclient import TestClient
    from nuramesh.server.app import create_app
    from nuramesh.stores import MemoryStore

    with TestClient(create_app(MemoryStore(), otel_env=ENV), base_url="http://localhost") as client:
        assert client.get("/v1/health").status_code == 200
        names = [t.name for t in threading.enumerate()]
    print(json.dumps({
        "modules": sorted(
            m for m in sys.modules
            if m.startswith(("nuramesh.telemetry.otel", "opentelemetry.sdk",
                             "opentelemetry.exporter.otlp"))
        ),
        "threads": [n for n in names if n.startswith("nuramesh-otel")],
        "providers_same": providers == (trace.get_tracer_provider(), metrics.get_meter_provider()),
    }))
    """
)


def _run(env: dict[str, str] | None) -> dict[str, object]:
    code = "ENV = " + repr(env) + "\n" + PROBE
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr[-2000:]
    found: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    return found


@pytest.mark.parametrize(
    "env",
    [None, {}, {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.test:4318"}],
    ids=["not-asked", "empty", "standard-otel-variable-only"],
)
def test_no_endpoint_means_no_sdk_no_thread_no_socket(env: dict[str, str] | None) -> None:
    found = _run(env)
    assert found == {"modules": [], "threads": [], "providers_same": True}


def test_a_refused_setting_starts_the_server_with_the_exporter_off() -> None:
    found = _run({"NURAMESH_OTEL_ENDPOINT": "http://otel.example.test:4318"})
    assert found["threads"] == [] and found["modules"] == []


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_only_the_adapter_imports_opentelemetry() -> None:
    offenders = sorted(
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if any(m.split(".")[0] == "opentelemetry" for m in _imports(p))
        and p.relative_to(SRC).as_posix() != "telemetry/otel.py"
    )
    assert offenders == []


@pytest.mark.parametrize("package", ["core", "engine", "stores", "adapters"])
def test_lower_layers_never_import_telemetry(package: str) -> None:
    offenders = sorted(
        str(p.relative_to(SRC))
        for p in (SRC / package).rglob("*.py")
        if any(
            m == "nuramesh.telemetry" or m.startswith("nuramesh.telemetry.") for m in _imports(p)
        )
    )
    assert offenders == []


def test_telemetry_core_modules_import_only_stdlib_pydantic_and_nuramesh() -> None:
    # env_bool and secret_env are stdlib-only leaf modules (the one boolean grammar and _FILE reads)
    allowed_nuramesh = (
        "nuramesh.core",
        "nuramesh.telemetry",
        "nuramesh.paths",
        "nuramesh.env_bool",
        "nuramesh.secret_env",
    )
    for path in (SRC / "telemetry").glob("*.py"):
        if path.name == "otel.py":
            continue
        for module in _imports(path):
            top = module.split(".")[0]
            ok = (
                top in sys.stdlib_module_names
                or top == "pydantic"
                or module.startswith(allowed_nuramesh)
            )
            assert ok, f"{path.name} imports {module}"


def test_the_module_never_calls_float() -> None:
    for path in (SRC / "telemetry").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "float" or path.name in ("tailer.py", "otel.py"), path.name
