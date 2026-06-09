#!/usr/bin/env bash
# Deploy / update the Meta Ad Library MCP server on this VPS.
#
# Pulls the latest code, (re)builds the image, and restarts the container. The
# container runs with `restart: unless-stopped`, so Docker keeps it alive 24/7
# (across crashes and reboots) on its own — this script is only for deploying
# updates. The session in ./data/ is a Docker volume and survives redeploys.
#
# Usage:  ./deploy.sh        (run from the repo dir; clone the repo first)
set -euo pipefail

cd "$(dirname "$0")"

# docker compose v2 ("docker compose") vs legacy v1 ("docker-compose")
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "ERROR: Docker Compose not found. Install Docker first." >&2
  exit 1
fi

# First run: create .env with a random token if it isn't there yet.
if [ ! -f .env ]; then
  echo "==> No .env found — creating one with a random MCP_TOKEN"
  cp .env.example .env
  TOKEN="$(openssl rand -hex 32)"
  sed -i "s|^MCP_TOKEN=.*|MCP_TOKEN=${TOKEN}|" .env
  echo "    MCP_TOKEN=${TOKEN}"
  echo "    Save it — it IS your URL path: https://<your-domain>/${TOKEN}"
fi

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Building image & (re)starting container"
$DC up -d --build

echo "==> Container status"
$DC ps

# Quick local reachability check (406 = up; the MCP endpoint rejects a plain GET).
TOKEN="$(grep -E '^MCP_TOKEN=' .env | cut -d= -f2- || true)"
if [ -n "${TOKEN}" ]; then
  sleep 2
  CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8765/${TOKEN}" || echo 000)"
  echo "==> http://127.0.0.1:8765/<token> -> HTTP ${CODE}  (406 = up & reachable)"
fi

echo "==> Done. Expose it by reverse-proxying to 127.0.0.1:8765 (see DEPLOY.md)."
