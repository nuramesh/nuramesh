# NuraMesh anchor job image (`nuramesh anchor export`; also the staging "job_image"). Build with
# scripts/images/build.sh; see the container images notes. Base pinned by digest
# (scripts/images/update-digest.sh refreshes it). No keys are baked in: the job reaches the
# signing key through its managed identity (AZURE_CLIENT_ID) or a mounted key file at run time.

# ---- builder: installs only hash-locked dependencies and the built wheel ----
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
COPY build/images/anchor.requirements.txt /tmp/requirements.txt
COPY build/images/wheel/ /tmp/wheel/
RUN python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install --require-hashes --no-deps -r /tmp/requirements.txt \
 && pip --python /opt/venv/bin/python install --no-deps /tmp/wheel/nuramesh-*.whl \
 && /opt/venv/bin/python -m compileall -q /opt/venv/lib

# ---- runtime: git and ssh are needed to commit and push anchors; nothing else ----
# Reproducible: apt reads a Debian snapshot pinned by timestamp (the same one the base image was
# built from: see the comment in /etc/apt/sources.list.d/debian.sources of the pinned base), so
# the same git and openssh-client versions come out on every build. Refresh: see the container images notes.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
LABEL org.opencontainers.image.title="nuramesh-anchor" \
      org.opencontainers.image.source="https://github.com/nuramesh/nuramesh" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="NuraMesh is an open-core platform: the control plane for AI agents. Every action verified, approved where it matters, and always on the record. This image is the ledger anchor." \
      org.opencontainers.image.vendor="AI Collective Labs Inc." \
      org.opencontainers.image.url="https://nuramesh.com"
RUN printf '%s\n' \
      'Types: deb' \
      'URIs: http://snapshot.debian.org/archive/debian/20261020T213255Z' \
      'Suites: bookworm bookworm-updates' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      '' \
      'Types: deb' \
      'URIs: http://snapshot.debian.org/archive/debian-security/20261020T213255Z' \
      'Suites: bookworm-security' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Check-Valid-Until=false update \
 && apt-get install -y --no-install-recommends git openssh-client \
 && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
COPY --from=builder /opt/venv /opt/venv
# The base image's pip is removed, so the image has no pip command (ensurepip's bundled wheel and
# apt remain; this makes installing harder, not impossible).
RUN /usr/local/bin/python -m pip uninstall -y pip setuptools wheel
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin nuramesh \
 && mkdir -p /data && chown nuramesh /data
USER 10001
WORKDIR /data
# A job has no HTTP port: the check only proves the install is importable.
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import nuramesh.engine.anchors"]
ENTRYPOINT ["nuramesh"]
CMD ["anchor", "--help"]
