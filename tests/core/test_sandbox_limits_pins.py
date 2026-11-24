"""Mutation pins for core/sandbox.py (#1278): request limits, defaults, env rules, run args."""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.sandbox import (
    IMAGE_PATTERN,
    SANDBOX_USER,
    WORKDIR,
    Isolation,
    SandboxLimits,
    SandboxRequest,
    SandboxResult,
    TestCommandGateConfig,
    oci_run_args,
)

DIGEST = "a" * 64
IMAGE = "registry.test/tools@sha256:" + DIGEST


def req(**kw: Any) -> SandboxRequest:
    return SandboxRequest.model_validate({"image": IMAGE, "command": ["true"], **kw})


def bad(**kw: Any) -> None:
    with pytest.raises(ValidationError):
        req(**kw)


def lim(**kw: Any) -> SandboxLimits:
    return SandboxLimits.model_validate(kw)


def test_constants() -> None:
    assert SANDBOX_USER == "65532:65532"
    assert WORKDIR == "/workspace"
    assert IMAGE_PATTERN.endswith("@sha256:[0-9a-f]{64}$")


def test_limit_defaults() -> None:
    d = SandboxLimits()
    assert (d.memory_mb, d.cpus, d.pids, d.timeout_seconds, d.output_bytes) == (
        1_024,
        Decimal("1"),
        256,
        300,
        65_536,
    )
    assert req().limits == d
    assert req().env == {}
    assert TestCommandGateConfig(image=IMAGE, command=("true",)).limits == d


@pytest.mark.parametrize(
    "field,low,high",
    [
        ("memory_mb", 64, 16_384),
        ("pids", 16, 4_096),
        ("timeout_seconds", 1, 3_600),
        ("output_bytes", 1_024, 1_048_576),
        ("cpus", Decimal("0.1"), Decimal("16")),
    ],
)
def test_each_limit_is_accepted_at_the_bounds_and_refused_beyond(
    field: str, low: Any, high: Any
) -> None:
    assert getattr(lim(**{field: low}), field) == low
    assert getattr(lim(**{field: high}), field) == high
    step = Decimal("0.01") if field == "cpus" else 1
    with pytest.raises(ValidationError):
        lim(**{field: low - step})
    with pytest.raises(ValidationError):
        lim(**{field: high + step})


def test_limits_are_frozen_and_closed() -> None:
    with pytest.raises(ValidationError):
        SandboxLimits().memory_mb = 5
    with pytest.raises(ValidationError):
        lim(gpus=1)


def test_the_same_limits_apply_through_a_request() -> None:
    assert req(limits={"memory_mb": 16_384}).limits.memory_mb == 16_384
    bad(limits={"memory_mb": 16_385})
    bad(limits={"pids": 15})


# ------------------------------------------------------------ image


def test_image_must_be_a_lowercase_digest_pin() -> None:
    for img in (
        "a@sha256:" + DIGEST,
        "a.b-c_d/e:tag@sha256:" + DIGEST,
        "0reg/x@sha256:" + "0123456789abcdef" * 4,
    ):
        assert req(image=img).image == img
    for img in (
        "@sha256:" + DIGEST,
        "-a@sha256:" + DIGEST,
        "a@sha256:" + "A" * 64,
        "a@sha256:" + "a" * 63,
        "a@sha256:" + "a" * 65,
        "a@sha256:" + "g" * 64,
        "a b@sha256:" + DIGEST,
        "a@sha512:" + DIGEST,
    ):
        bad(image=img)


def test_image_name_may_start_with_any_digit() -> None:
    for first in "0123456789":
        img = f"{first}reg/x@sha256:{DIGEST}"
        assert req(image=img).image == img


def test_image_length_limit_is_300() -> None:
    tail = "@sha256:" + DIGEST
    assert len(req(image="a" * (300 - len(tail)) + tail).image) == 300
    bad(image="a" * (301 - len(tail)) + tail)


# ------------------------------------------------------------ command


def test_command_bounds() -> None:
    bad(command=[])
    assert len(req(command=["a"] * 100).command) == 100
    bad(command=["a"] * 101)
    assert len(req(command=["a" * 4_096]).command[0]) == 4_096
    bad(command=["a" * 4_097])
    bad(command=[""])
    bad(command=["ok", ""])
    bad(command=["ok", "x\x00y"])
    bad(command=["x\x00"])


# ------------------------------------------------------------ environment


def test_env_count_limit() -> None:
    assert len(req(env={f"K{i}": "v" for i in range(50)}).env) == 50
    bad(env={f"K{i}": "v" for i in range(51)})


@pytest.mark.parametrize(
    "key,ok",
    [
        ("A", True),
        ("_", True),
        ("_A1_B", True),
        ("A" * 64, True),
        ("A" * 65, False),
        ("a", False),
        ("Ab", False),
        ("1A", False),
        ("A-B", False),
        ("A B", False),
        ("", False),
    ],
)
def test_env_key_shape(key: str, ok: bool) -> None:
    if ok:
        assert req(env={key: "v"}).env == {key: "v"}
    else:
        with pytest.raises(ValidationError, match="isn't allowed"):
            req(env={key: "v"})


def test_env_key_in_the_error_is_cut_at_40_characters() -> None:
    with pytest.raises(ValidationError) as info:
        req(env={"a" * 60: "v"})
    text = str(info.value)
    assert "a" * 40 in text and "a" * 41 not in text


def test_nuramesh_settings_are_never_passed() -> None:
    for key in ("NURAMESH_X", "NURAMESH_", "NURAMESH_API_TOKEN"):
        with pytest.raises(ValidationError, match=r"NURAMESH_\* settings are never passed"):
            req(env={key: "v"})
    assert req(env={"NURAMESH": "v", "XNURAMESH_A": "v"}).env  # only the prefix is barred


def test_env_value_rules() -> None:
    assert len(req(env={"A": "v" * 4_096}).env["A"]) == 4_096
    with pytest.raises(ValidationError, match="environment variable 'A' has an invalid value"):
        req(env={"A": "v" * 4_097})
    with pytest.raises(ValidationError, match="invalid value"):
        req(env={"A": "x\x00y"})
    assert req(env={"A": ""}).env == {"A": ""}


# ------------------------------------------------------------ test-command gate


def test_gate_config_has_the_same_rules_and_builds_its_request() -> None:
    assert TestCommandGateConfig.__test__ is False
    cfg = TestCommandGateConfig.parse(
        {
            "image": IMAGE,
            "command": ["pytest", "-q"],
            "env": {"A": "1"},
            "limits": {"timeout_seconds": 5},
        }
    )
    r = cfg.request()
    assert isinstance(r, SandboxRequest)
    assert (r.image, r.command, dict(r.env), r.limits.timeout_seconds) == (
        IMAGE,
        ("pytest", "-q"),
        {"A": "1"},
        5,
    )
    top = {"image": IMAGE, "command": ["a"] * 100, "env": {f"K{i}": "v" for i in range(50)}}
    assert len(TestCommandGateConfig.parse(top).command) == 100
    assert TestCommandGateConfig.__test__ is False
    for broken in (
        {"image": "busybox", "command": ["x"]},
        {"image": IMAGE, "command": []},
        {"image": IMAGE, "command": ["a"] * 101},
        {"image": IMAGE, "command": ["a\x00"]},
        {"image": IMAGE, "command": ["a"], "env": {"NURAMESH_A": "1"}},
        {"image": IMAGE, "command": ["a"], "env": {f"K{i}": "v" for i in range(51)}},
        {"image": IMAGE, "command": ["a"], "limits": {"pids": 4_097}},
        {"image": IMAGE, "command": ["a"], "extra": 1},
    ):
        with pytest.raises(ValidationError):
            TestCommandGateConfig.parse(broken)


# ------------------------------------------------------------ result


@pytest.mark.parametrize(
    "kw,ok",
    [
        ({}, True),
        ({"exit_code": 1}, False),
        ({"exit_code": None}, False),
        ({"timed_out": True}, False),
        ({"started": False}, False),
    ],
)
def test_result_ok_needs_started_not_timed_out_and_exit_zero(kw: dict[str, Any], ok: bool) -> None:
    base: dict[str, Any] = {
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": 0,
        "isolation": Isolation.OCI,
        "runtime": "podman",
    }
    r = SandboxResult(**{**base, **kw})
    assert r.ok is ok
    assert r.stdout_tail == "" and r.stderr_tail == ""
    assert r.started is (kw.get("started") is not False)


def test_result_defaults_to_started() -> None:
    r = SandboxResult(
        exit_code=0, timed_out=False, duration_ms=1, isolation=Isolation.OCI, runtime="docker"
    )
    assert r.started is True and r.ok is True


def test_result_duration_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        SandboxResult(
            exit_code=0, timed_out=False, duration_ms=-1, isolation=Isolation.HOST, runtime="x"
        )
    assert (Isolation.OCI.value, Isolation.HOST.value) == ("oci", "host")


# ------------------------------------------------------------ run arguments


def run_args(request: SandboxRequest, *, podman: bool, **kw: Any) -> list[str]:
    return oci_run_args("rt", request, workspace_copy="/w/copy", name="job-1", podman=podman, **kw)


COMMON = [
    "--rm",
    "--name",
    "job-1",
    "--network=none",
    "--read-only",
    "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--user=65532:65532",
    "--memory=1024m",
    "--memory-swap=1024m",
    "--cpus=1",
    "--pids-limit=256",
    "--pull=never",
    "--mount=type=bind,src=/w/copy,dst=/workspace",
    "--workdir=/workspace",
]


def test_podman_arguments_exactly() -> None:
    args = run_args(req(command=["prog", "a1", "a2"], env={"B": "2", "A": "1"}), podman=True)
    assert args == [
        "rt",
        "run",
        "--http-proxy=false",
        "--userns=keep-id",
        *COMMON,
        "--env=A=1",
        "--env=B=2",
        '--entrypoint=["prog"]',
        IMAGE,
        "a1",
        "a2",
    ]


def test_podman_without_keep_id() -> None:
    args = run_args(req(), podman=True, keep_id=False)
    assert args[:3] == ["rt", "run", "--http-proxy=false"]
    assert "--userns=keep-id" not in args


def test_podman_entrypoint_is_a_json_array_even_for_odd_programs() -> None:
    args = run_args(req(command=['sh"-c', "x"]), podman=True)
    assert '--entrypoint=["sh\\"-c"]' in args and args[-1] == "x"


def test_docker_arguments_blank_every_proxy_variable() -> None:
    args = run_args(req(command=["prog", "x"]), podman=False)
    assert args[:2] == ["rt", "run"]
    proxies = [a for a in args if a.startswith("--env=") and a.endswith("=")]
    names = ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY")
    assert sorted(proxies) == sorted(
        [f"--env={n}=" for n in names] + [f"--env={n.lower()}=" for n in names]
    )
    assert "--http-proxy=false" not in args and "--userns=keep-id" not in args
    # each name is inserted at index 2, lower-case twin last: so it ends up before its upper case
    assert proxies[:2] == ["--env=no_proxy=", "--env=NO_PROXY="]
    assert proxies[-2:] == ["--env=http_proxy=", "--env=HTTP_PROXY="]
    assert args[2 + len(proxies) :] == [*COMMON, "--entrypoint=prog", IMAGE, "x"]


def test_docker_keeps_the_job_name_even_though_the_loop_uses_the_same_variable() -> None:
    args = run_args(req(), podman=False)
    assert args[args.index("--name") + 1] == "job-1"


def test_user_memory_cpus_and_pids_come_from_the_request() -> None:
    r = req(limits={"memory_mb": 512, "cpus": "2.50", "pids": 64})
    args = run_args(r, podman=True, user="1000:1000")
    assert "--user=1000:1000" in args and "--user=65532:65532" not in args
    assert "--memory=512m" in args and "--memory-swap=512m" in args
    assert "--cpus=2.5" in args
    assert "--pids-limit=64" in args


@pytest.mark.parametrize(
    "cpus,text",
    [("1", "1"), ("1.0", "1"), ("10", "10"), ("16", "16"), ("0.1", "0.1"), ("0.50", "0.5")],
)
def test_cpus_are_written_without_exponent_or_trailing_zeros(cpus: str, text: str) -> None:
    args = run_args(req(limits={"cpus": cpus}), podman=True)
    assert f"--cpus={text}" in args
