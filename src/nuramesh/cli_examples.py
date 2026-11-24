# ruff: noqa: E501
"""The `Examples:` block of every command's `--help` (R1.43),
and the finishing pass over the parser tree: the flags every command accepts (`--dry-run`,
`--org`) and the examples. One line per example, 1 to 3 lines per command, taken from
the CLI guide on the docs site."""

import argparse

from nuramesh.cli_rules import NO_YES, add_global_options, leaf_parsers

EXAMPLES: dict[str, tuple[str, ...]] = {
    "validate": ("nuramesh validate my-team/nuramesh.yaml",),
    "try": ("nuramesh try my-team --fake", "nuramesh try my-team --fake --approve-as human:demo"),
    "status": ("nuramesh status --team my-team", "nuramesh status RUN_ID --team my-team"),
    "reject": (
        'nuramesh reject TASK_ID --team my-team --human human:u_7k2x9q --reason "not ready"',
    ),
    "report": ("nuramesh report RUN_ID --team my-team",),
    "start": (
        "nuramesh start my-team --plan plan.json --human human:u_7k2x9q",
        "nuramesh start my-team --plan plan.json --human human:u_7k2x9q --dry-run",
    ),
    "stop": ('nuramesh stop RUN_ID --team my-team --human human:u_7k2x9q --reason "wrong plan"',),
    "org status": ("nuramesh org status --team my-team",),
    "org prompt-cache": (
        "nuramesh org prompt-cache on --team my-team --human human:u_7k2x9q",
        "nuramesh org prompt-cache off --team my-team --human human:u_7k2x9q",
    ),
    "savings tokens": (
        "nuramesh savings tokens --team my-team",
        "nuramesh savings tokens --team my-team --group-by model --output json",
    ),
    "org halt": ("nuramesh org halt --team my-team --human human:u_7k2x9q",),
    "roles pause": ("nuramesh roles pause reviewer --team my-team --human human:u_7k2x9q",),
    "breakers list": ("nuramesh breakers list --team my-team",),
    "breakers trip": ("nuramesh breakers trip NAME --team my-team --human human:u_7k2x9q",),
    "db migrate": (
        "nuramesh db migrate --team my-team",
        "nuramesh db migrate --team my-team --grant-to ROLE",
    ),
    "content shred": (
        "nuramesh content shred --team my-team --org my-org --human human:u_7k2x9q --yes",
    ),
    "content expire": ("nuramesh content expire --team my-team",),
    "keys status": (
        "nuramesh keys status --team my-team",
        "nuramesh keys status --team my-team --org my-org",
    ),
    "keys reconcile": ("nuramesh keys reconcile --adopt --org my-org --team my-team",),
    "keys adopt-legacy": ("nuramesh keys adopt-legacy --org my-org --team my-team",),
    "integrations list": ("nuramesh integrations list",),
    "integrations hash": ("nuramesh integrations hash tool.json",),
    "minter anchor-record": (
        "nuramesh minter anchor-record --host db-1 --seq 42 --hash HASH --key-fp FP --epoch 1",
    ),
    "minter anchor-verify": ("nuramesh minter anchor-verify --team my-team",),
    "minter anchor-role-check": ("nuramesh minter anchor-role-check",),
    "minter anchor-install": (
        "nuramesh minter anchor-install --owner-role nuramesh_owner --minter-role nuramesh_minter",
    ),
    "scorecards show": ("nuramesh scorecards show --role reviewer --team my-team",),
    "autonomy propose": ("nuramesh autonomy propose --role reviewer --team my-team",),
    "autonomy grants list": (
        "nuramesh autonomy grants list --team my-team",
        "nuramesh autonomy grants list --all",
    ),
    "autonomy grants create": (
        "nuramesh autonomy grants create --role reviewer --rule 3 --as-of 2026-11-01 "
        "--ledger-position 120 --evidence-hash HASH --human human:u_7k2x9q --reason approved",
    ),
    "autonomy grants revoke": (
        'nuramesh autonomy grants revoke GRANT_ID --human human:u_7k2x9q --reason "no longer needed"',
    ),
    "autonomy ceilings set": (
        "nuramesh autonomy ceilings set --role reviewer --kind deploy --value none --human human:u_7k2x9q --reason why",
    ),
    "autonomy settings set": (
        "nuramesh autonomy settings set --name NAME --value VALUE --human human:u_7k2x9q --reason why",
    ),
    "otel check": ("nuramesh otel check",),
    "otel export": ("nuramesh otel export --once --team my-team",),
    "otel rotate-pseudonym-key": ("nuramesh otel rotate-pseudonym-key",),
    "email status": ("nuramesh email status",),
    "doctor": ("nuramesh doctor", "nuramesh doctor --port 9000 --output json"),
    "demo": (
        "nuramesh demo",
        "nuramesh demo --dir my-demo --allow-unsandboxed",
        "nuramesh demo --stack --port 8765",
    ),
    "email check": ("nuramesh email check --to you@example.com --team my-team",),
    "serve": ("nuramesh serve my-team", "nuramesh serve my-team --port 9000"),
    "worker": ("nuramesh worker my-team --address localhost:7233",),
    "auth new-setup-token": ("nuramesh auth new-setup-token --team my-team",),
    "auth invites create": (
        "nuramesh auth invites create alice@example.com --role viewer --team my-team",
    ),
    "auth invites list": ("nuramesh auth invites list --team my-team",),
    "auth invites revoke": ("nuramesh auth invites revoke INVITE_ID --team my-team",),
    "auth invites resend": ("nuramesh auth invites resend INVITE_ID --team my-team",),
    "auth password-reset": ("nuramesh auth password-reset --user human:u_example --team my-team",),
    "auth purge-sessions": ("nuramesh auth purge-sessions --team my-team",),
    "auth erase-owner": (
        "nuramesh auth erase-owner --user USER_ID --promote USER_ID --team my-team",
    ),
    "auth factor-key init": ("nuramesh auth factor-key init --file ./auth.key --team my-team",),
    "auth factor-key rotate": ("nuramesh auth factor-key rotate --team my-team",),
    "auth factor-key destroy": ("nuramesh auth factor-key destroy --team my-team",),
    "auth mfa status": ("nuramesh auth mfa status --team my-team",),
    "auth mfa reset": ("nuramesh auth mfa reset --user human:u_example --team my-team",),
    "auth close-org": ("nuramesh auth close-org --team my-team",),
    "auth recovery-code issue": ("nuramesh auth recovery-code issue --team my-team",),
    "auth tokens create": (
        "nuramesh auth tokens create --name ci-nightly --scope read --team my-team",
    ),
    "auth tokens list": ("nuramesh auth tokens list --team my-team",),
    "auth tokens revoke": ("nuramesh auth tokens revoke ci-nightly --team my-team",),
    "auth tokens rename": ("nuramesh auth tokens rename ci-nightly ci-main --team my-team",),
    "auth tokens delete": ("nuramesh auth tokens delete ci-nightly --team my-team",),
    "auth tokens scrub-erased": ("nuramesh auth tokens scrub-erased --team my-team",),
    "agents credentials list": ("nuramesh agents credentials list --agent dana --team my-team",),
    "agents credentials revoke": (
        "nuramesh agents credentials revoke CREDENTIAL_ID --team my-team",
    ),
    "agents credentials revoke-all": (
        "nuramesh agents credentials revoke-all --agent dana --team my-team",
    ),
    "anchor keygen": ("nuramesh anchor keygen --private anchor.key --public anchor.pub",),
    "anchor export": ("nuramesh anchor export --repo ../anchors --key anchor.key --team my-team",),
    "anchor rotate": (
        "nuramesh anchor rotate my-team --repo ../anchors --key old.key --new-key new.key",
    ),
    "anchor verify": ("nuramesh anchor verify my-team --repo ../anchors --pubkey anchor.pub",),
    "init": ("nuramesh init my-team --template dev-team",),
    "eval run": ("nuramesh eval run --suite dev-basics --agent dana --fake --team my-team",),
    "eval status": ("nuramesh eval status --agent dana --team my-team",),
    "calibrate run": ("nuramesh calibrate run --suite SUITE --verifier V --fake --team my-team",),
    "calibrate status": ("nuramesh calibrate status --verifier V --team my-team",),
    "models catalog": ("nuramesh models catalog --team my-team",),
    "models assignments": ("nuramesh models assignments --team my-team",),
    "models connections": ("nuramesh models connections --team my-team",),
    "models disable": ("nuramesh models disable openai --team my-team --human human:u_7k2x9q",),
    "models disconnect": (
        "nuramesh models disconnect openai --team my-team --human human:u_7k2x9q",
    ),
    "models key set": (
        "nuramesh models key set anthropic --reason 'first stored key'",
        "nuramesh models key set anthropic --fd 3 --reason ROTATION",
    ),
    "models key rotate": ("nuramesh models key rotate anthropic --grace-seconds 3600",),
    "models key revoke": (
        "nuramesh models key revoke anthropic --reason 'leaked at the provider' --confirm my-org",
    ),
    "models key status": ("nuramesh models key status anthropic",),
    "models test": ("nuramesh models test openai --team my-team",),
    "models connect": (
        "nuramesh models connect openai-compatible --endpoint http://localhost:11434/v1 --team my-team",
        "nuramesh models connect openai-compatible --preset ollama --team my-team",
    ),
    "models presets": ("nuramesh models presets",),
    "models pause": ("nuramesh models pause openai/MODEL --team my-team --human human:u_7k2x9q",),
    "models retire": ("nuramesh models retire openai/MODEL --team my-team --human human:u_7k2x9q",),
    "models price": (
        "nuramesh models price openai-compatible/MODEL --free --effective-from 2026-01-01T00:00:00Z --source local --team my-team",
    ),
    "models local-model": (
        "nuramesh models local-model --team my-team",
        "nuramesh models local-model --team my-team --yes",
    ),
    "models check": ("nuramesh models check --data synthetic --team my-team",),
    "ledger verify": (
        "nuramesh ledger verify --team my-team",
        "nuramesh ledger verify --team my-team --require-anchors",
    ),
    "ledger export": ("nuramesh ledger export --out ledger.jsonl --team my-team",),
    "completion": ("nuramesh completion bash", "nuramesh completion zsh"),
}


def finish_parser(root: argparse.ArgumentParser) -> None:
    """Add the flags every command accepts and the `Examples:` block to every leaf command."""
    for words, leaf in leaf_parsers(root):
        has_team = any(a.dest == "team" for a in leaf._actions)
        add_global_options(leaf, has_team=has_team, allow_yes=" ".join(words) not in NO_YES)
        lines = EXAMPLES.get(" ".join(words))
        if lines:
            leaf.formatter_class = argparse.RawDescriptionHelpFormatter
            leaf.epilog = "Examples:\n" + "\n".join(f"  {line}" for line in lines)
