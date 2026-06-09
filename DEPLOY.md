# Deploying the MCP server on a VPS

The MCP server (Streamable HTTP) is the only thing that needs to be public — it wraps
the library in-process, so the FastAPI REST API is **not** involved and does **not** need
to be exposed. The Docker image runs `run_mcp.py` only.

## Auth

- **Secret-in-URL (always on)** — the endpoint is `…/<MCP_TOKEN>`; any other path 404s, so
  only a caller who knows the full URL gets in (a "capability URL"). Always serve over HTTPS.
  This is the real gate.
- **Auto-approve OAuth (optional, `MCP_OAUTH=1`)** — **default is authless.** Only enable
  this if claude.ai's *web* connector won't connect authless. It runs a no-users,
  auto-approve OAuth layer purely to satisfy Claude's connector flow (Claude Code / the API
  don't need it). When on, the metadata URL is built from `MCP_DOMAIN` (override with
  `MCP_PUBLIC_URL`).

> ⚠️ **If you're behind Cloudflare and OAuth "succeeds" but Claude still says "Authorization
> with the MCP server failed":** that's **not** an auth problem — Cloudflare's Bot Fight Mode
> / WAF is blocking Anthropic's server-to-server `Authorization: Bearer` POST to your MCP
> endpoint (datacenter IP, no browser fingerprint → looks like a bot). Check **Security →
> Events**, then **turn off Bot Fight Mode** or add a **WAF Skip rule** for the MCP hostname.
> This is the usual cause and applies to authless too.

## 1. Build & run (behind your existing Apache/nginx)

This is the right path if the VPS already hosts other sites. The container binds to
**127.0.0.1:8765** and your existing web server proxies to it — Caddy is not used, so
nothing fights Apache over ports 80/443.

```bash
git clone https://github.com/ECOM-CORP/meta-ad-library-wrapper.git
cd meta-ad-library-wrapper
cp .env.example .env
# set MCP_TOKEN to a long random value:
python3 -c "import secrets; print('MCP_TOKEN='+secrets.token_urlsafe(32))" >> .env
# then edit .env and set MCP_DOMAIN to your subdomain, e.g.
#   MCP_DOMAIN=metamcp.yourdomain.com
docker compose up -d --build
```

The MCP is now at `http://127.0.0.1:8765/<MCP_TOKEN>` on the box (not yet public).

### Apache vhost (reverse proxy + your existing HTTPS)

Enable the proxy modules once: `a2enmod proxy proxy_http ssl`. Then add a vhost for a
subdomain (get its cert with `certbot --apache -d mcp.yourdomain.com`):

```apache
<VirtualHost *:443>
    ServerName mcp.yourdomain.com

    SSLEngine on
    SSLCertificateFile    /etc/letsencrypt/live/mcp.yourdomain.com/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/mcp.yourdomain.com/privkey.pem

    ProxyPreserveHost On
    # flushpackets=on keeps the MCP streaming (SSE) responses from being buffered.
    ProxyPass        /  http://127.0.0.1:8765/  flushpackets=on
    ProxyPassReverse /  http://127.0.0.1:8765/
</VirtualHost>
```

`systemctl reload apache2`. Public URL: `https://mcp.yourdomain.com/<MCP_TOKEN>`.

> nginx equivalent: `proxy_pass http://127.0.0.1:8765;` with `proxy_buffering off;` and
> `proxy_http_version 1.1;` so SSE isn't buffered.

## 1b. Alternative: no existing web server (bundled Caddy)

Only if the VPS does **not** already use ports 80/443. Set `MCP_DOMAIN` in `.env` (A
record pointed at the VPS), then:

```bash
docker compose -f docker-compose.caddy.yml up -d --build
```

Caddy fetches HTTPS automatically. URL: `https://<MCP_DOMAIN>/<MCP_TOKEN>`.

## 2. Seed the session

The container can serve search/scan/reach as soon as `data/session_cache.json` exists.
Two ways to get one:

- **Bootstrap in the container (recommended).** Call the `bootstrap` MCP tool. It runs
  **headless** — the browser is forced to English so it captures everything including the
  reach (`details`) doc_id, and it auto-dismisses the cookie-consent dialog. No display /
  xvfb needed. If it ever fails on a login/consent wall, fall back to seeding from your PC.
- **Seed from your PC.** Run `bootstrap_session()` locally and copy the file up:
  ```bash
  scp session_cache.json user@vps:/path/meta-ad-library-wrapper/data/session_cache.json
  ```

**Tokens expire**, so refresh periodically: call `bootstrap` again, or re-`scp` a fresh
`session_cache.json`. `session_status` reports `valid:false` when it's time.

## 3. Connect from Claude

- **claude.ai web / desktop:** Settings → Connectors → Add custom connector →
  `https://mcp.yourdomain.com/<MCP_TOKEN>`.
- **Claude Code:** `claude mcp add --transport http meta-ad-library https://mcp.yourdomain.com/<MCP_TOKEN>`

## Staying up & updating

- **24/7:** the container runs with `restart: unless-stopped`, so the Docker daemon
  (a systemd service that starts on boot) keeps it alive across crashes and reboots. You
  don't run anything to keep it up — it stays up until you `docker compose stop` it.
- **Updating:** to ship new code, just run **`./deploy.sh`** — it pulls the latest, rebuilds
  the image, and restarts the container. The session in `data/` survives the redeploy, so
  you don't re-bootstrap. (On first run it also creates `.env` with a random token.)

## Notes

- `data/` (the session + browser profile) and `.env` (your token) are gitignored — never
  commit them. The session file holds live credentials.
- A datacenter IP makes Meta's anti-bot/rate-limiting more aggressive than a residential
  one; keep request volume modest.
