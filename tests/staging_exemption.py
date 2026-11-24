"""The one exemption for the staging deploy workflow (#976, OD-8; decision T976, ND228 style).

`deploy-staging.yml` publishes the built docs site to Cloudflare Pages staging, which the
not-published lint (ND188, ND331, T240) and the workflow rules forbid. The exemption is EXACT: the
file must be named `deploy-staging.yml` and its bytes must hash to STAGING_SHA256, the hash of the
reviewed copy of the workflow (kept in the deploy folder). No glob, no prefix, no other path; a
changed byte, a copy under another name or any other docs-publishing workflow is scanned as usual
(fails closed). To change the workflow, change the reviewed copy and this hash together, in a
reviewed PR.
"""

import hashlib
from pathlib import Path

STAGING_NAME = "deploy-staging.yml"
STAGING_SHA256 = "e003d4a0d0b0c07c8cf0594fee54bb5b3ea9eb1273504afb1a5f79952a72996d"


def is_pinned(path: Path) -> bool:
    """True only for the exact file name with the exact reviewed content."""
    return path.name == STAGING_NAME and (
        hashlib.sha256(path.read_bytes()).hexdigest() == STAGING_SHA256
    )


def invariants(doc: dict[object, object]) -> list[str]:
    """Defence in depth on the exempted file: what it may be even though it is exempt."""
    out: list[str] = []
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict) or set(triggers) != {"push", "workflow_dispatch"}:
        out.append("staging: triggers must be exactly push and workflow_dispatch")
    else:
        push = triggers["push"]
        paths = push.get("paths") if isinstance(push, dict) else None
        if not isinstance(push, dict) or set(push) != {"branches", "paths"}:
            out.append("staging: push needs exactly branches and paths (Actions minutes)")
        elif push["branches"] != ["main"]:
            out.append("staging: push only on main")
        elif (
            not isinstance(paths, list) or not paths or any(p in ("**", "*", "**/*") for p in paths)
        ):
            out.append("staging: push paths must be a non-empty list of specific paths")
    if doc.get("permissions") != {"contents": "read"}:
        out.append("staging: top-level permissions must be contents: read")
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return [*out, "staging: no jobs"]
    for name, job in jobs.items():
        text = str(job)
        for step in job.get("steps", []):
            if "actions/checkout@" in step.get("uses", "") and (
                step.get("with", {}).get("persist-credentials") is not False
            ):
                out.append(f"staging:{name}: checkout must set persist-credentials: false")
        if name.endswith("-deploy") and not any(
            "actions/setup-node@" in s.get("uses", "") for s in job.get("steps", [])
        ):
            out.append(f"staging:{name}: a deploy job installs Node (wrangler needs 22.12+)")
        if name.endswith("-build"):
            if "secrets" in text or "environment" in job:
                out.append(f"staging:{name}: a build job holds no secret and no environment")
        elif job.get("environment") != "staging":
            out.append(f"staging:{name}: a job that is not a build runs in environment staging")
    return out
