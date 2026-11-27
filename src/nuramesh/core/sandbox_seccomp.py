"""Seccomp for the sandbox service: the command's filter and the service profile (R47, R56).

Two filters apply to a command in the sandbox service (real-models spec section 7):

- **The command's own filter** (`command_filter`), loaded by `bwrap --seccomp`. A raw classic-BPF
  program built here without libseccomp, so the standard-library supervisor needs nothing else.
  It refuses every ABI but the native one, every x32 call number, the calls a command could use
  to make namespaces or mounts or to reach kernel features it never needs (`EPERM`), `clone`
  with any namespace flag (`EPERM`) and `clone3` (`ENOSYS`: its flags sit behind a pointer a
  filter cannot read). Everything else is allowed: the container's profile is the first filter.
- **The service profile** (`service_profile`): the engine's pinned default profile plus exactly
  one entry that allows the five calls `bwrap` needs for nested namespaces. It is for the
  supervisor and `bwrap`; the command never has it.

The bytes are pinned by hash (`COMMAND_FILTER_SHA256`); a golden test fails on any change.
"""

import struct
from collections.abc import Mapping
from typing import Any

_LD_W_ABS, _JEQ_K, _JGE_K, _JSET_K, _RET_K = 0x20, 0x15, 0x35, 0x45, 0x06
_ALLOW, _ERRNO = 0x7FFF0000, 0x00050000
_EPERM, _ENOSYS = 1, 38
_AUDIT_ARCH_X86_64 = 0xC000003E
_OFF_NR, _OFF_ARCH, _OFF_ARG0 = 0, 4, 16
_X32_BIT = 0x40000000

# x86_64 call numbers refused with EPERM (R56): mount, keyring, tracing and io_uring routes.
REFUSED_CALLS_X86_64: Mapping[str, int] = {
    "unshare": 272,
    "setns": 308,
    "mount": 165,
    "umount2": 166,
    "pivot_root": 155,
    "fsopen": 430,
    "fsconfig": 431,
    "fsmount": 432,
    "move_mount": 429,
    "open_tree": 428,
    "mount_setattr": 442,
    "keyctl": 250,
    "add_key": 248,
    "request_key": 249,
    "bpf": 321,
    "perf_event_open": 298,
    "userfaultfd": 323,
    "io_uring_setup": 425,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    "ptrace": 101,
    "process_vm_readv": 310,
    "process_vm_writev": 311,
    "fspick": 433,
    "open_tree_attr": 467,
}
_CLONE_X86_64, _CLONE3_X86_64 = 56, 435
# CLONE_NEWTIME | NEWNS | NEWCGROUP | NEWUTS | NEWIPC | NEWUSER | NEWPID | NEWNET
_NAMESPACE_FLAGS = (
    0x00000080
    | 0x00020000
    | 0x02000000
    | 0x04000000
    | 0x08000000
    | 0x10000000
    | 0x20000000
    | 0x40000000
)

COMMAND_FILTER_SHA256: Mapping[str, str] = {
    "x86_64": "618bbf4652878ed99bd23e0c31b486499cc4f2e8345e6a1736188f3c9bb75193",
}

# R47: the pinned default profile is `default.json` of `github.com/moby/profiles/seccomp` v0.2.4
# (what Docker Engine 29.9.2 vendors). The file is kept next to the supervisor; a test checks the
# file against this hash, so the pin and the file cannot change together unnoticed.
PINNED_DEFAULT_PROFILE_SHA256 = "785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334"

# R47: the calls the service profile allows beyond the engine's default profile.
PROFILE_CALLS = ("clone", "mount", "pivot_root", "umount2", "unshare")


def _ins(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k & 0xFFFFFFFF)


def command_filter(arch: str) -> bytes:
    """The command's seccomp filter for `arch` as `struct sock_filter` bytes.

    Only `x86_64` is built today; any other name raises `ValueError`, so a host of another
    architecture runs nothing (the service state is `sandbox_selftest_failed`).
    """
    if arch != "x86_64":
        raise ValueError(f"no command filter for '{arch}': only x86_64 is supported")
    prog = [
        _ins(_LD_W_ABS, 0, 0, _OFF_ARCH),
        _ins(_JEQ_K, 1, 0, _AUDIT_ARCH_X86_64),  # native: skip the refusal
        _ins(_RET_K, 0, 0, _ERRNO | _EPERM),  # any other ABI (i386)
        _ins(_LD_W_ABS, 0, 0, _OFF_NR),
        _ins(_JGE_K, 0, 1, _X32_BIT),
        _ins(_RET_K, 0, 0, _ERRNO | _EPERM),
        _ins(_JEQ_K, 0, 1, _CLONE3_X86_64),
        _ins(_RET_K, 0, 0, _ERRNO | _ENOSYS),
    ]
    for number in sorted(REFUSED_CALLS_X86_64.values()):
        prog += [_ins(_JEQ_K, 0, 1, number), _ins(_RET_K, 0, 0, _ERRNO | _EPERM)]
    prog += [
        _ins(_JEQ_K, 0, 3, _CLONE_X86_64),  # not clone: allow
        _ins(_LD_W_ABS, 0, 0, _OFF_ARG0),  # the low 32 bits of the flags
        _ins(_JSET_K, 0, 1, _NAMESPACE_FLAGS),
        _ins(_RET_K, 0, 0, _ERRNO | _EPERM),
        _ins(_RET_K, 0, 0, _ALLOW),
    ]
    return b"".join(prog)


def service_profile(default_profile: Mapping[str, Any]) -> dict[str, Any]:
    """R47: `default_profile` (the engine's pinned default) plus one entry for `PROFILE_CALLS`.

    Refuses a default whose default action allows, or that has no default action: the result
    must never be a profile that allows what it does not name.
    """
    action = default_profile.get("defaultAction")
    if not isinstance(action, str) or action == "SCMP_ACT_ALLOW":
        raise ValueError("the default profile must have a default action that does not allow")
    profile = dict(default_profile)
    profile["syscalls"] = [
        *default_profile.get("syscalls", []),
        {
            "names": list(PROFILE_CALLS),
            "action": "SCMP_ACT_ALLOW",
            "comment": "R47 (#963): nested namespaces for bwrap, no capability needed",
        },
    ]
    return profile
