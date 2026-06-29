# agentchat — single-image Dockerfile
#
# Builds a slim Python 3.11-slim image with agentchat pre-installed.
# Run: docker run -p 7878:7878 -p 7879:7879 -v agentchat-data:/data agentchat
#
# Data is persisted to /data (volume recommended). The image exposes the
# API on :7878 and the WebUI on :7879. For production, run them under a
# reverse proxy (Caddy, Nginx, Traefik) for TLS termination — see README.md.

FROM python:3.11-slim

LABEL org.opencontainers.image.title="agentchat"
LABEL org.opencontainers.image.description="Lightweight agent-to-agent chat server with workspaces and bearer auth"
LABEL org.opencontainers.image.source="https://github.com/wayne-comerford/agentchat"
LABEL org.opencontainers.image.licenses="MIT"

# agentchat is a single-file Python package with stdlib-only deps.
# No build tools needed. We use python:3.11-slim (~150 MB) which has
# everything agentchat needs to run.
WORKDIR /app

# Copy package files first to leverage Docker layer caching.
COPY agentchat/ /app/agentchat/
COPY web/ /app/web/
COPY scripts/ /app/scripts/
COPY README.md HANDOFF.md LICENSE /app/

# agentchat stores DB + tokens at $AGENTCHAT_HOME (default: ~/.agentchat).
# In the container we point it at /data so a volume can persist state.
ENV AGENTCHAT_HOME=/data
ENV PYTHONUNBUFFERED=1

# Run as non-root for defense in depth.
RUN useradd --create-home --shell /bin/bash agentchat \
    && mkdir -p /data \
    && chown -R agentchat:agentchat /data /app
USER agentchat

EXPOSE 7878 7879

# Default: run the API on 7878. To run the WebUI too, override the CMD:
#   docker run ... agentchat python3 -m agentchat web --host 0.0.0.0 --port 7879 --api http://127.0.0.1:7878
CMD ["python3", "-m", "agentchat", "serve", "--host", "0.0.0.0", "--port", "7878"]