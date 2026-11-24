"""Sandbox requests and the hardened container command line (R1.08, ND24)."""

import pytest
from pydantic import ValidationError

from nuramesh.core.sandbox import SandboxRequest, TestCommandGateConfig, oci_run_args

IMAGE = "docker.io/library/busybox@sha256:" + "a" * 64


def request(**kw: object) -> SandboxRequest:
    return SandboxRequest.model_validate({"image": IMAGE, "command": ["true"], **kw})


@pytest.mark.parametrize(
    "image",
    ["busybox", "busybox:1.36", "busybox@sha256:abc", "BUSYBOX@sha256:" + "a" * 64, ""],
)
def test_images_must_be_pinned_by_digest(image: str) -> None:
    with pytest.raises(ValidationError):
        request(image=image)


def test_commands_and_environment_are_checked() -> None:
    with pytest.raises(ValidationError):
        request(command=[])
    with pytest.raises(ValidationError):
        request(command=["a\x00b"])
    for env in (
        {"NURAMESH_API_KEY": "x"},
        {"NURAMESH_API_KEY": "x"},
        {"lower": "x"},
        {"A": "x\x00"},
        {"A": "x" * 5000},
    ):
        with pytest.raises(ValidationError):
            request(env=env)
    assert request(env={"CI": "1"}).env == {"CI": "1"}


def test_limits_are_bounded() -> None:
    for limits in ({"timeout_seconds": 0}, {"memory_mb": 1}, {"pids": 1}, {"cpus": "0"}):
        with pytest.raises(ValidationError):
            request(limits=limits)


def test_the_container_command_line_is_hardened() -> None:
    args = oci_run_args(
        "/usr/bin/docker",
        request(command=["pytest", "-q"], env={"CI": "1"}),
        workspace_copy="/tmp/copy",
        name="nuramesh-x",
        podman=False,
        user="1000:1000",
    )
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=1000:1000",
        "--memory=1024m",
        "--memory-swap=1024m",
        "--cpus=1",
        "--pids-limit=256",
        "--pull=never",
        "--mount=type=bind,src=/tmp/copy,dst=/workspace",
        "--env=CI=1",
        "--rm",
    ):
        assert flag in args
    # the program is the entrypoint (the image's own ENTRYPOINT never re-parses the command), then
    # the image, then the program's arguments, nothing after
    assert args[-3:] == ["--entrypoint=pytest", IMAGE, "-q"]
    joined = " ".join(args)
    assert "unconfined" not in joined and "--privileged" not in joined
    assert "--userns=keep-id" not in args
    podman = oci_run_args("podman", request(), workspace_copy="/c", name="n", podman=True)
    assert "--userns=keep-id" in podman and "--user=65532:65532" in podman
    assert "--http-proxy=false" in podman
    tens = oci_run_args(
        "docker", request(limits={"cpus": "1E1"}), workspace_copy="/c", name="n", podman=False
    )
    assert "--cpus=10" in tens


def test_a_test_command_gate_config_makes_a_request() -> None:
    config = TestCommandGateConfig.model_validate(
        {"image": IMAGE, "command": ["pytest"], "limits": {"timeout_seconds": 60}}
    )
    assert config.request().limits.timeout_seconds == 60


def test_the_programme_is_pinned_as_the_entrypoint_so_an_image_entrypoint_cannot_reparse_it() -> (
    None
):
    """An image whose ENTRYPOINT is `sh -c` would otherwise treat the first argument as a script."""
    args = oci_run_args(
        "docker",
        SandboxRequest(image=IMAGE, command=("python", "-m", "unittest", "-v")),
        workspace_copy="/c",
        name="n",
        podman=False,
    )
    assert args[-5:] == ["--entrypoint=python", IMAGE, "-m", "unittest", "-v"]
    assert sum(arg.startswith("--entrypoint") for arg in args) == 1


def test_podman_gets_the_programme_as_a_one_element_json_array() -> None:
    """Podman reads `--entrypoint=["sh","-c","x"]` as that array: a first word that looks like JSON
    must stay one program (it can't smuggle `sh -c` past a policy that approved a single word)."""
    word = '["sh","-c","rm -rf /"]'
    args = oci_run_args(
        "podman",
        SandboxRequest(image=IMAGE, command=(word, "x")),
        workspace_copy="/c",
        name="n",
        podman=True,
    )
    import json

    assert args[-3:] == [f"--entrypoint={json.dumps([word])}", IMAGE, "x"]
    docker = oci_run_args(
        "docker",
        SandboxRequest(image=IMAGE, command=(word, "x")),
        workspace_copy="/c",
        name="n",
        podman=False,
    )
    assert docker[-3:] == [f"--entrypoint={word}", IMAGE, "x"]  # docker takes it literally
