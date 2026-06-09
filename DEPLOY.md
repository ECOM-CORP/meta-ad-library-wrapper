# Deploying the MCP server on a VPS

The MCP server (Streamable HTTP) is the only thing that needs to be public — it wraps
the library in-process, so the FastAPI REST API is **not** involved and does **not** need
to be exposed. The Docker image runs `run_mcp.py` only.

## Auth: secret-in-URL

The endpoint is `…/mcp/<MCP_TOKEN>`. Requests to any other path return 404, so only a
caller who knows the full URL gets in (a "capability URL"). Always serve it over HTTPS.

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
docker compose up -d --build
```

The MCP is now at `http://127.0.0.1:8765/mcp/<MCP_TOKEN>` on the box (not yet public).

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

`systemctl reload apache2`. Public URL: `https://mcp.yourdomain.com/mcp/<MCP_TOKEN>`.

> nginx equivalent: `proxy_pass http://127.0.0.1:8765;` with `proxy_buffering off;` and
> `proxy_http_version 1.1;` so SSE isn't buffered.

## 1b. Alternative: no existing web server (bundled Caddy)

Only if the VPS does **not** already use ports 80/443. Set `MCP_DOMAIN` in `.env` (A
record pointed at the VPS), then:

```bash
docker compose -f docker-compose.caddy.yml up -d --build
```

Caddy fetches HTTPS automatically. URL: `https://<MCP_DOMAIN>/mcp/<MCP_TOKEN>`.

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
  `https://mcp.yourdomain.com/mcp/<MCP_TOKEN>`.
- **Claude Code:** `claude mcp add --transport http meta-ad-library https://mcp.yourdomain.com/mcp/<MCP_TOKEN>`

## Notes

- `data/` (the session + browser profile) and `.env` (your token) are gitignored — never
  commit them. The session file holds live credentials.
- A datacenter IP makes Meta's anti-bot/rate-limiting more aggressive than a residential
  one; keep request volume modest.
