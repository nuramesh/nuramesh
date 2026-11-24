# NuraMesh web image (R1.29): the built apps/app single-page app behind nginx-unprivileged, with the
# TLS server and API forwarding of deploy/compose/nginx/default.conf. Build with
# deploy/compose/build-web.sh (it builds apps/app first into build/web/dist). Local only; no push.
# nginx-unprivileged 1.29.8-alpine (BSD-2-Clause), pinned by tag and digest. No secret is baked in:
# the certificate and key are Docker secrets mounted at run time.
FROM nginxinc/nginx-unprivileged:1.29.8-alpine@sha256:0c79d56aee561a1d81c63f00eee5fb5fe29279560cdc55e91425133104c7fbe6
LABEL org.opencontainers.image.title="nuramesh-web" \
      org.opencontainers.image.source="https://github.com/nuramesh/nuramesh" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="NuraMesh is an open-core platform: the control plane for AI agents. Every action verified, approved where it matters, and always on the record. This image is the web app." \
      org.opencontainers.image.vendor="AI Collective Labs Inc." \
      org.opencontainers.image.url="https://nuramesh.com"
COPY deploy/compose/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY build/web/dist/ /usr/share/nginx/html/
USER 101
EXPOSE 8443
HEALTHCHECK --interval=10s --timeout=5s --retries=12 \
  CMD ["wget", "--no-check-certificate", "-q", "-O", "/dev/null", "https://127.0.0.1:8443/"]
