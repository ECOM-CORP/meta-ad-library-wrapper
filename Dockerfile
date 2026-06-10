# MCP server image. Bundles Chromium so the bootstrap tool can re-harvest the session
# fully HEADLESS (incl. the reach doc_id) — the browser is forced to English, so no
# virtual display (xvfb) is needed.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python deps + the matching Chromium build (+ its system libs).
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY meta_ad_library/ ./meta_ad_library/
COPY run_mcp.py ./

# Container defaults: networked transport, bind public, persist session under /data.
# (The package now defaults to stdio for local/Claude-Desktop use; the VPS needs HTTP.)
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8765 \
    MCP_SESSION_CACHE=/data/session_cache.json \
    MCP_PROFILE_DIR=/data/.pw-profile
EXPOSE 8765
VOLUME ["/data"]

CMD ["python", "run_mcp.py"]
