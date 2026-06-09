# MCP server image. Includes Chromium (for bootstrap) and xvfb so the *headed*
# browser flow works on a headless VPS — headless can't capture the reach doc_id.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# xvfb provides a virtual display for the headed bootstrap.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps + the matching Chromium build (+ its system libs).
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY meta_ad_library/ ./meta_ad_library/
COPY run_mcp.py ./

# Container defaults: bind public, persist the session under the /data volume.
ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8765 \
    MCP_SESSION_CACHE=/data/session_cache.json \
    MCP_PROFILE_DIR=/data/.pw-profile
EXPOSE 8765
VOLUME ["/data"]

# Run under a virtual display so headed Playwright (the reach bootstrap) works.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1280x1024x24", "python", "run_mcp.py"]
