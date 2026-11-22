# Kubernetes: a beta arriving in December

NuraMesh has no Kubernetes install yet. A **Helm chart is a beta that we expect to ship in December**. It is
not available today, and this page does not give a firmer date. **Docker Compose is the supported way to run
NuraMesh at launch**, and it is the only self-hosting path you should build on now.

## What works today

Run NuraMesh on one machine with Docker Compose: the API server, the worker, PostgreSQL, Temporal and the web
app, started with one command. Follow the self-hosting guide in the repository docs
(`docs/self-hosting.md`) and the [Quickstart](/quickstart/) for a first run.

The Compose stack uses PostgreSQL only. Its Temporal dev server is for evaluation only; production needs a real
Temporal cluster. No real customer data is allowed until the privacy policy and the data processing agreement are
published.

## What to expect from the beta

- It is a **beta**: expect rough edges and changes between releases. Do not make it your only production path
  yet.
- The chart is meant to run the same NuraMesh images with the same settings as Compose, and to show the same
  behavior. It is not shown to anyone until it passes the same end-to-end tests as the Compose stack.
- Kubernetes brings things Compose does not have: ingress, secrets and storage classes. Their details are not
  settled, so this page does not describe them.
- There are no chart values, commands or install steps to follow yet. Anything you find elsewhere that claims
  otherwise is not from us.

## Running Kubernetes now

If your platform is Kubernetes only, you can run the Compose stack's images yourself, but that is not a
supported path and we cannot help you debug it. Telling us what your cluster needs is useful: it shapes the
beta.

## How you will know

When the chart is available, this page is replaced by install steps, and the [Upgrade notes](/guides/upgrade/)
will say so. Until then, treat anything not on this page as undecided.
