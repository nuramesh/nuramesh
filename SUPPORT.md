# Getting help with NuraMesh

NuraMesh is pre-1.0 open-source software, offered as it is under the Apache License 2.0. This page says
where to ask and what to expect.

## Before you ask

- Read the quickstart and the guides that `README.md` lists. The troubleshooting and errors guides sit
  next to them and cover the most common problems.
- Run `nuramesh doctor`. It checks this machine: Python, port, database, sign-in key, container runtime
  and extras.
- Search the existing issues and discussions of this repository. Your question may already have an answer.

## Where to ask

| You have | Do this |
| --- | --- |
| A bug, or a guide that is wrong | Open an issue in this repository. Say what you ran, what you expected and what happened, with the NuraMesh version (`nuramesh --version`), your Python version and your operating system. |
| A question about using NuraMesh, or an idea | Start a discussion in this repository (the Discussions tab). |
| A change you would like to make | Open an issue first, as `CONTRIBUTING.md` describes. |
| A security problem | Do **not** open an issue. Email **security@nuramesh.com**, as `SECURITY.md` describes. |
| A concern about someone's conduct | Email **opensource@nuramesh.com**, as `CODE_OF_CONDUCT.md` describes. |

Never put secrets, API keys, tokens or real customer data in an issue. Remove them from logs before you
paste them.

## What to expect

The maintainers read issues and discussions and answer when they can, with no promise of a response time. Only the
`main` branch is supported until the first release is published; after that, fixes go into the newest
release.
