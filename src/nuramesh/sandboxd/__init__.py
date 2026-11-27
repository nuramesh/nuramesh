"""The sandbox service's supervisor, `sandboxd` (real-models spec section 7; PR3a of #963).

One long-lived process in the `sandbox` container. It listens on a Unix socket, serves only the
runner's user (`SO_PEERCRED`), answers `health` at once from a cached state and runs **one
command at a time**: unpack the workspace archive into its own tmpfs through directory handles,
start `bwrap` with exactly the argument list of R56 and the command's seccomp filter, kill the
command on the time limit or when the client's connection closes, delete the copy, answer.

It imports the standard library and `nuramesh.core.sandbox*` only (R43), and it runs nothing
unless its state is `ready` (R52): not in the host's own user namespace, a seccomp profile
loaded, finite limits, caps above 0 and a passing self-test. Start: `python -m nuramesh.sandboxd`.
"""
