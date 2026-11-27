# NuraMesh API image. Build with scripts/images/build.sh (it first writes the wheel and the
# hash-locked requirements file under build/images/); see the container images notes.
# The base is pinned by digest (the multi-arch index of python:3.12-slim-bookworm); refresh it
# with scripts/images/update-digest.sh. Nothing secret is baked in: settings come from NURAMESH_*
# environment variables at run time.

# ---- builder: installs only hash-locked dependencies and the built wheel ----
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
COPY build/images/api.requirements.txt /tmp/requirements.txt
COPY build/images/wheel/ /tmp/wheel/
RUN python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install --require-hashes --no-deps -r /tmp/requirements.txt \
 && pip --python /opt/venv/bin/python install --no-deps /tmp/wheel/nuramesh-*.whl \
 && /opt/venv/bin/python -m compileall -q /opt/venv/lib

# ---- runtime: no pip cache, no build tools, no secrets, non-root ----
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
LABEL org.opencontainers.image.title="nuramesh-api" \
      org.opencontainers.image.source="https://github.com/nuramesh/nuramesh" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="NuraMesh is an open-core platform: the control plane for AI agents. Every action verified, approved where it matters, and always on the record. This image is the API server." \
      org.opencontainers.image.vendor="AI Collective Labs Inc." \
      org.opencontainers.image.url="https://nuramesh.com"
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
COPY --from=builder /opt/venv /opt/venv
# The base image's pip is removed, so the image has no pip command (ensurepip's bundled wheel and
# apt remain; this makes installing harder, not impossible).
RUN /usr/local/bin/python -m pip uninstall -y pip setuptools wheel
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin nuramesh \
 && mkdir -p /data /keys /secret-store && chown nuramesh /data /keys /secret-store \
 && chmod 0700 /secret-store
# R1.29: builds NURAMESH_DATABASE_URL from a password file (deploy/compose); a no-op without it
COPY --chmod=755 build/images/nuramesh-env /usr/local/bin/nuramesh-env
USER 10001
WORKDIR /data
# The default command stays on loopback: `nuramesh serve` refuses any other host until the
# R1.22 rules (authentication, trusted proxy, allowed hosts) exist. Staging overrides --host
# only after they do. Run with --read-only and a /data volume plus /tmp.
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/v1/health', timeout=3)"]
ENTRYPOINT ["nuramesh"]
CMD ["serve", "/data/team", "--host", "127.0.0.1", "--port", "8765"]
