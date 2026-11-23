# Security policy

NuraMesh is the AI control plane for agent teams, so we take reports about its checks, budgets, ledger and anchors seriously.

## Reporting a vulnerability

Email **security@nuramesh.com**. Please include what you found, how to reproduce it, and what you think
the impact is. Do not open a public issue for a vulnerability, and do not include real customer data or
credentials in a report.

You can also use GitHub's **private vulnerability reporting** (the repository's Security tab,
"Report a vulnerability").

We will acknowledge a report within 3 business days, share our assessment and next steps within 10 business days, and keep you updated until it is resolved. Please give us up to 90 days to release a fix before you share details publicly.

## What is in scope

- Ways to make a task count as done without its gates passing, to exceed a budget without a hard stop, or
  to act outside a role's policy.
- Ways to edit, delete or reorder ledger entries, or to forge or hide a signed anchor, without verification
  reporting it.
- Leaks of secrets, or of data a verifier or another tenant should not see.
- Weaknesses in the API server, the app or the website.

## Out of scope

- Denial of service and volume-based attacks.
- Social engineering or phishing of NuraMesh staff.
- Issues in third-party model providers or dependencies without a working exploit against NuraMesh.
- Reports from automated scanners without a proof of impact.

## Recognition

We do not run a paid bug bounty at this time. With your permission, we credit reporters in the release notes for the fix.

## Supported versions

The latest 0.1.x release is supported.

## Safe harbor

If you act in good faith, stay within these rules and give us time to respond, we will not take legal
action against you for your research.
