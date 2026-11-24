# NuraMesh Temporal worker image (extra nuramesh[temporal]). Build with scripts/images/build.sh;
# see the container images notes. Base pinned by digest (scripts/images/update-digest.sh refreshes it).
# Nothing secret is baked in: settings come from NURAMESH_* (NURAMESH_TEMPORAL_ADDRESS) at run time.

# ---- builder: installs only hash-locked dependencies and the built wheel ----
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
COPY build/images/worker.requirements.txt /tmp/requirements.txt
COPY build/images/wheel/ /tmp/wheel/
RUN python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install --require-hashes --no-deps -r /tmp/requirements.txt \
 && pip --python /opt/venv/bin/python install --no-deps /tmp/wheel/nuramesh-*.whl \
 && /opt/venv/bin/python -m compileall -q /opt/venv/lib

# ---- runtime: no pip cache, no build tools, no secrets, non-root ----
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
LABEL org.opencontainers.image.title="nuramesh-worker" \
      org.opencontainers.image.source="https://github.com/nuramesh/nuramesh" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="NuraMesh is an open-core platform: the control plane for AI agents. Every action verified, approved where it matters, and always on the record. This image is the worker." \
      org.opencontainers.image.vendor="AI Collective Labs Inc." \
      org.opencontainers.image.url="https://nuramesh.com"
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
COPY --from=builder /opt/venv /opt/venv
# The base image's pip is removed, so the image has no pip command (ensurepip's bundled wheel and
# apt remain; this makes installing harder, not impossible).
RUN /usr/local/bin/python -m pip uninstall -y pip setuptools wheel
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin nuramesh \
 && mkdir -p /data /keys && chown nuramesh /data /keys
# R1.29: builds NURAMESH_DATABASE_URL from a password file (deploy/compose); a no-op without it
COPY --chmod=755 build/images/nuramesh-env /usr/local/bin/nuramesh-env
USER 10001
WORKDIR /data
# The default command is `nuramesh worker --fake`: it refuses to start until
# NURAMESH_TEMPORAL_ADDRESS, NURAMESH_TEAM_DIR (a team folder mounted or baked by the deployment)
# and NURAMESH_KEY_DIR (a mounted volume shared by all replicas) are set, and it runs
# on the team's fake model until real providers are approved. SIGTERM stops it gracefully.
# The health check only proves the install is importable: a worker has no HTTP port.
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import nuramesh.temporal.worker"]
ENTRYPOINT ["nuramesh"]
CMD ["worker", "--fake"]
