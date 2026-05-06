# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.7] - 2026-05-06

### Fixed

- DuckDNS path: route `/oauth/callback` on the public 443 HTTPS server. Previously only `.well-known/...` was served, so Tesla's OAuth redirect (`https://<sub>.duckdns.org/oauth/callback?code=...`) returned 404 and setup stalled at step 5. The wizard's OAuth handler now runs on both HA-ingress (8099) and the public DuckDNS HTTPS server.

## [0.9.6] - 2026-03-19

### Security

- Pin self-signed TLS cert for proxy commands instead of disabling TLS verification (`ssl=False`)
- Gate `/api/credentials-for-ha` endpoint — require X-Ingress-Path header (403 without HA ingress)
- Validate UPnP SSDP location URLs are private RFC-1918 addresses to prevent SSRF
- Add 64KB XML size cap on UPnP responses to prevent billion-laughs DoS
- Add security headers on all responses: CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy
- Add input length limits (128 chars) on credential fields
- Tighten DuckDNS token regex to UUID format
- Sanitize certbot error output — no raw details returned to browser
- Clear VIN cache on wizard reset

### Changed

- Replace deprecated `asyncio.ensure_future` with `asyncio.create_task`
- Replace deprecated `asyncio.get_event_loop()` with `asyncio.get_running_loop()`
- Narrow `except BaseException` to `except Exception` in save_state
- Narrow `except Exception` to specific `JSONDecodeError`/`ContentTypeError` in command handler
- Use `logger.exception` instead of `logger.error` in except blocks for full tracebacks
- Use `Path.chmod()` instead of `os.chmod()` in duckdns module
- Use `Path.read_text()` instead of bare `open()` in tests
- Remove unused `miniupnpc` package from Dockerfile (UPnP is pure Python)
- Remove unused `os` import from duckdns module

### Added

- 18 new tests (103 → 121): credential exposure, security headers, SSRF prevention,
  ingress access control, input length limits

## [0.1.5] - 2026-03-06

### Changed

- Always use Cloudflare tunnel for setup — Nabu Casa and external URLs point
  to HA Core which doesn't serve `/.well-known/appkeys`. Only this add-on does,
  so a direct tunnel is always needed during the setup process.
- Simplified Step 2 UI — removed Nabu Casa/external URL auto-detection options
  that couldn't actually work

### Fixed

- Self-test for tunnel URLs: test localhost instead of trying to resolve the
  tunnel hostname from inside the container (DNS can't resolve own tunnel)

## [0.1.4] - 2026-03-06

### Fixed

- Nabu Casa detection: try multiple cloud API paths, handle case where cloud
  component is loaded but URL cannot be auto-resolved
- Manual URL input: when Nabu Casa is detected but URL unknown, user can paste
  their `https://xxx.ui.nabu.casa` URL directly
- Version display in wizard header

## [0.1.3] - 2026-03-06

### Fixed

- SUPERVISOR_TOKEN detection: try s6 container environment files as fallback
- Improved Nabu Casa / external URL detection with diagnostic logging
- Version number now shown in wizard UI header

### Security

- Tunnel guard middleware: Cloudflare tunnel now only exposes `/.well-known/appkeys`
  and `/oauth/callback` — all other paths return 404 when accessed via tunnel

## [0.1.2] - 2026-03-06

### Fixed

- Improved Nabu Casa / external URL detection with diagnostic logging

## [0.1.1] - 2026-03-06

### Fixed

- HA ingress compatibility: inject `<base href>` from `X-Ingress-Path` header
  so all relative URLs (API calls, screenshots) route through the ingress proxy
  instead of hitting HA Core directly
- Use relative URLs throughout wizard JS and HTML instead of absolute paths
- Corrected installation instructions: use Supervisor Add-on Store (not HACS)

## [0.1.0] - 2026-03-06

### Added

- Initial release
- 5-step guided wizard: key generation, URL exposure, Tesla app registration,
  partner verification, and OAuth connection
- Automatic EC P-256 key pair generation with secure storage (mode 0600)
- Automatic Nabu Casa and external URL detection via HA Supervisor API
- Cloudflare quick tunnel for users without external URL access
- Self-test to verify public key reachability before proceeding
- Tesla partner authentication (client credentials + partner registration)
- OAuth authorization flow with CSRF protection (random state parameter)
- Step-by-step Tesla developer portal walkthrough with copy-paste fields
- Progress persistence across add-on restarts (state saved to /data)
- Collapsible troubleshooting sections for common issues
- Tesla dark theme UI consistent with the Tesla card aesthetic

### Security

- Credential sanitization in all log output (tokens, secrets, codes redacted)
- HTTP access logs disabled (prevents OAuth codes in log files)
- State file written with 0600 permissions
- OAuth state cleared after use to prevent replay
- Error responses to browser sanitized (no raw API bodies exposed)
