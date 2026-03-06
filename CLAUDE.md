# Claude Project: Tesla Fleet Setup Add-on

## Project Overview

Home Assistant add-on that provides a guided wizard for setting up the Tesla
Fleet API integration. Automates key generation, public key hosting, and
partner authentication — reducing the setup from a multi-hour manual process
to a ~10 minute guided flow.

**Repo:** https://github.com/ds2000/ha-tesla-fleet-setup
**Related repos:**
- Card: https://github.com/ds2000/homeassistant-fe-tesla
- Image uploader: https://github.com/ds2000/homeassistant-fe-tesla-image-uploader

---

## Architecture

HA add-on running as a Docker container. Python aiohttp server provides:
- Wizard UI via HA Ingress (appears in sidebar)
- `/.well-known/appkeys` endpoint for Tesla verification
- API routes for each wizard step
- Cloudflare quick tunnel for users without external URLs

### Key Files

```
config.yaml                          # HA add-on metadata
Dockerfile                           # Alpine + Python + cloudflared
build.yaml                           # Base image per architecture
run.sh                               # Entrypoint
rootfs/opt/tesla-setup/
  server.py                          # Main aiohttp server + API routes
  keygen.py                          # EC P-256 key generation
  tunnel.py                          # Cloudflare quick tunnel management
  tesla_api.py                       # Tesla Fleet API (partner auth + OAuth)
  ha_discovery.py                    # Detect Nabu Casa / external URL
  templates/wizard.html              # Single-page wizard UI
  static/                            # CSS, screenshots for guide
```

### State Persistence

Wizard state stored at `/data/state.json` (survives add-on restarts).
Keys stored at `/data/keys/` with private key chmod 600.

---

## Wizard Steps

1. **Generate Keys** — Auto-generates EC P-256 key pair
2. **Expose Public Key** — Auto-detects Nabu Casa / external URL, or starts Cloudflare tunnel
3. **Register Tesla App** — Guided walkthrough with copy-paste fields for developer.tesla.com
4. **Partner Authentication** — Calls Tesla API to register partner (triggers .well-known verification)
5. **Connect** — OAuth flow to authorize HA with user's Tesla account

---

## Tesla Fleet API Flow

```
1. Generate EC P-256 key pair
2. Host public key at https://{domain}/.well-known/appkeys
3. Register app at developer.tesla.com → get client_id + client_secret
4. POST /oauth2/v3/token (client_credentials) → partner token
5. POST /api/1/partner_accounts { domain } → Tesla verifies public key
6. OAuth authorize → user consent → authorization code
7. POST /oauth2/v3/token (authorization_code) → access + refresh tokens
```

---

## Design Principles

- Tesla dark theme (#0d0d0d, red accents) matching the card aesthetic
- Vanilla HTML/JS only — no frameworks
- Single page wizard with progress indicator
- Copy buttons for every value the user needs to paste
- Auto-detect environment before asking user to do manual work
- Self-test verification before proceeding to partner registration

---

## What NOT to Do

- Do not store Tesla client_secret anywhere except /data/ (never in source)
- Do not use any JS framework
- Do not skip the self-test verification step
- Do not leave the Cloudflare tunnel running after setup is complete
- Do not expose any endpoint other than /.well-known/appkeys through the tunnel
