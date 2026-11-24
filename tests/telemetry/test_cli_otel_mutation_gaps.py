"""Pins for `nuramesh otel` found by the mutation farm (issue #1197): the exit codes and output
documented in the CLI reference page. A fake sink and a fake socket only: nothing
touches the network."""

from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("fastapi")

from nuramesh.cli import main  # noqa: E402
from nuramesh.telemetry.tailer import SinkError  # noqa: E402

from .fakes import FakeSink  # noqa: E402


class ProbeFails(FakeSink):
    def probe(self, signals: list[str]) -> None:
        raise SinkError(tuple(signals))


def test_otel_without_a_subcommand_is_a_usage_error() -> None:
    """The `otel` group needs one of check, export, rotate-pseudonym-key (argparse exit 2)."""
    with pytest.raises(SystemExit) as exc:
        main(["otel"])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("endpoint", "port"),
    [("https://collector.example.test", 443), ("http://127.0.0.1", 80)],
)
def test_a_failed_check_probes_the_scheme_default_port(
    endpoint: str,
    port: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no port in the endpoint the reachability probe uses 443 (https) or 80 (http); an
    answer means exit 1, no answer exit 5."""
    import socket

    monkeypatch.setenv("NURAMESH_OTEL_ENDPOINT", endpoint)
    monkeypatch.setenv("NURAMESH_OTEL_STATE_FILE", str(tmp_path / "c.json"))
    monkeypatch.setattr("nuramesh.telemetry.runtime.make_sink", lambda settings: ProbeFails())
    seen: list[tuple[str, int]] = []
    timeouts: list[float] = []

    class Conn:
        def __enter__(self) -> "Conn":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def answers(address: tuple[str, int], timeout: float) -> Conn:
        seen.append(address)
        timeouts.append(timeout)
        return Conn()

    monkeypatch.setattr(socket, "create_connection", answers)
    assert main(["otel", "check"]) == 1
    assert seen == [(endpoint.split("//")[1], port)] and timeouts == [3]

    def refuses(address: tuple[str, int], timeout: float) -> Conn:
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, "create_connection", refuses)
    assert main(["otel", "check"]) == 5


def test_rotate_pseudonym_key_fails_with_exit_1_when_the_key_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", "utf-8")
    monkeypatch.setenv("NURAMESH_KEY_DIR", str(blocker / "keys"))
    monkeypatch.delenv("NURAMESH_OTEL_PSEUDONYM_KEY_FILE", raising=False)
    assert main(["otel", "rotate-pseudonym-key"]) == 1
    out = capsys.readouterr()
    assert "could not write the pseudonym key file" in out.err and out.out == ""


def test_export_without_once_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NURAMESH_OTEL_ENDPOINT", "https://collector.example.test")
    monkeypatch.setenv("NURAMESH_OTEL_STATE_FILE", str(tmp_path / "c.json"))
    assert main(["otel", "export", "--team", str(tmp_path)]) == 2
    out = capsys.readouterr()
    assert "use --once" in out.err and out.out == ""


def test_export_is_a_no_op_with_exit_0_when_the_exporter_is_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NURAMESH_OTEL_ENDPOINT", raising=False)
    assert main(["otel", "export", "--once", "--team", str(tmp_path)]) == 0
    assert "otel export is off" in capsys.readouterr().out


@pytest.mark.parametrize("command", [["check"], ["export", "--once", "--team"]])
def test_a_missing_otel_extra_is_exit_2_with_the_closed_code(
    command: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NURAMESH_OTEL_ENDPOINT", "https://collector.example.test")
    monkeypatch.setenv("NURAMESH_OTEL_STATE_FILE", str(tmp_path / "c.json"))

    def missing(settings: object) -> FakeSink:
        raise ImportError("no otel")

    monkeypatch.setattr("nuramesh.telemetry.runtime.make_sink", missing)
    args = ["otel", *command, *([str(tmp_path)] if command[0] == "export" else [])]
    assert main(args) == 2
    out = capsys.readouterr()
    assert "missing_extra" in out.err and "nuramesh[otel]" in out.err and out.out == ""
