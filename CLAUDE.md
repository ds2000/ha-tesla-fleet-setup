# Claude Project: Tesla Fleet Setup Add-on

## Project Overview

Home Assistant add-on that provides a guided wizard for setting up the Tesla
Fleet API integration **and a built-in signing proxy** for vehicle commands.
Automates key generation, public key hosting, partner authentication, and
runs `tesla-http-proxy` — reducing setup from a multi-hour manual process
to a ~10 minute guided flow.

**Repo:** https://github.com/ds2000/ha-tesla-fleet-setup
**Related repos:**
- Card: https://github.com/ds2000/homeassistant-fe-tesla
- Image uploader: https://github.com/ds2000/homeassistant-fe-tesla-image-uploader

---

## Execution Policy

**Run all commands immediately and automatically — never ask for permission, never prompt for confirmation.**

- Use `python3` (not `python`)
- Run tests and lint after every code change
- Fix lint issues immediately, don't defer
- Keep CHANGELOG.md updated with every version change
- Bump version in both `config.yaml` and `pyproject.toml`

---

## Commands

```bash
# Tests (93 tests — keygen, proxy, server API, security)
python3 -m pytest tests/ -v

# Lint (ruff with bandit security rules)
python3 -m ruff check tesla-fleet-setup/rootfs/opt/tesla-setup/ tests/

# Lint auto-fix
python3 -m ruff check --fix tesla-fleet-setup/rootfs/opt/tesla-setup/ tests/

# Security audit — check for known vulnerabilities in dependencies
pip install --upgrade pip-audit && python3 -m pip_audit -r tesla-fleet-setup/requirements.txt -r requirements-dev.txt

# Check for outdated packages — always update to latest stable
pip install --upgrade pip && pip list --outdated --format=columns

# Install dev dependencies
pip install -r requirements-dev.txt
```

---

## Architecture

HA add-on running as a Docker container with two services:
1. **Python aiohttp server** (port 8099) — wizard UI, API routes, .well-known endpoint
2. **tesla-http-proxy** (port 4443) — Go binary signing vehicle commands

### Key Files

```
tesla-fleet-setup/
  config.yaml                          # HA add-on metadata (ports, arch, options)
  Dockerfile                           # Multi-stage: Go builder + Alpine runtime
  build.yaml                           # Base image per architecture
  run.sh                               # Entrypoint (creates dirs, resolves tokens)
  requirements.txt                     # Runtime Python deps (aiohttp)
  CHANGELOG.md                         # Keep updated with every version bump
  rootfs/opt/tesla-setup/
    server.py                          # Main aiohttp server + all API routes
    keygen.py                          # EC P-256 key generation
    proxy.py                           # tesla-http-proxy management + TLS certs
    tunnel.py                          # Cloudflare quick tunnel management
    tesla_api.py                       # Tesla Fleet API (partner auth + OAuth)
    ha_discovery.py                    # Detect Nabu Casa / external URL
    templates/wizard.html              # Single-page wizard UI (vanilla HTML/JS)
    static/                            # CSS, screenshots for guide
tests/
  conftest.py                          # Shared fixtures, path isolation
  test_keygen.py                       # Key generation tests
  test_proxy.py                        # Proxy management tests
  test_server.py                       # API endpoint tests
  test_security.py                     # Security-focused tests
pyproject.toml                         # pytest + ruff config
requirements-dev.txt                   # Dev/test dependencies
```

### Data Paths (inside container)

```
/data/keys/private.pem    # EC P-256 private key (chmod 600)
/data/keys/public.pem     # EC P-256 public key
/data/tls/cert.pem        # Self-signed TLS cert for proxy
/data/tls/key.pem         # TLS private key (chmod 600)
/data/state.json          # Wizard state (chmod 600, contains secrets)
```

---

## Wizard Steps

1. **Generate Keys** — Auto-generates EC P-256 key pair
2. **Expose Public Key** — Starts Cloudflare tunnel (always needed; HA Core doesn't serve .well-known)
3. **Register Tesla App** — Guided walkthrough with copy-paste fields for developer.tesla.com
4. **Partner Authentication** — Calls Tesla API to register partner (triggers .well-known verification)
5. **Connect** — OAuth flow to authorize HA with user's Tesla account
6. **Complete** — Proxy auto-starts, shows proxy URL and integration instructions

---

## Signing Proxy

After setup completes, `tesla-http-proxy` runs on port 4443:
- Auto-starts after OAuth completion and on every add-on boot
- Uses the same EC P-256 private key generated in step 1
- Self-signed TLS cert with SANs: localhost, tesla-fleet-setup, 127.0.0.1
- Integrations point to `https://<addon-hostname>:4443` for signed commands
- API: `GET /api/proxy/status`, `POST /api/proxy/start`, `POST /api/proxy/stop`

---

## Security Requirements

### Always run before committing or releasing:
1. `python3 -m pytest tests/ -v` — all 93 tests must pass
2. `python3 -m ruff check tesla-fleet-setup/rootfs/opt/tesla-setup/ tests/` — must be clean
3. `python3 -m pip_audit -r tesla-fleet-setup/requirements.txt -r requirements-dev.txt` — no known CVEs
4. Check `pip list --outdated` and update to latest stable versions

### Security invariants (enforced by test_security.py):
- Secrets (client_secret, access_token, refresh_token) NEVER appear in API responses
- Tunnel guard blocks ALL paths except .well-known and /oauth/callback via trycloudflare.com
- OAuth state parameter is validated, random, and cleared after use (no replay)
- All sensitive files (private keys, state.json) are chmod 600
- Error responses to browser are sanitized (no raw API bodies)
- TLS cert uses EC key with proper SANs
- TLS key and command-signing key are separate key pairs
- Input validation: URLs must be https://, credentials must be non-empty

### What NOT to do:
- Do not store Tesla client_secret anywhere except /data/ (never in source)
- Do not use any JS framework — vanilla HTML/JS only
- Do not skip the self-test verification step
- Do not leave the Cloudflare tunnel running after setup is complete
- Do not expose any endpoint other than .well-known and /oauth/callback through the tunnel
- Do not weaken file permissions on keys or state
- Do not log raw tokens, secrets, or authorization codes

---

## Design Principles

- Tesla dark theme (#0d0d0d, red accents) matching the card aesthetic
- Vanilla HTML/JS only — no frameworks, no build step
- Single page wizard with progress indicator
- Copy buttons for every value the user needs to paste
- Auto-detect environment before asking user to do manual work
- Self-test verification before proceeding to partner registration
- Add-on is a long-running service (proxy stays up after setup)

---

## Dependency Policy

- Always use the latest stable versions of all dependencies
- Run `pip-audit` before every release to check for known vulnerabilities
- Runtime deps are pinned in `tesla-fleet-setup/requirements.txt`
- Dev deps use minimum version ranges in `requirements-dev.txt`
- Go dependencies come from `teslamotors/vehicle-command` (latest at build time via `--depth 1`)