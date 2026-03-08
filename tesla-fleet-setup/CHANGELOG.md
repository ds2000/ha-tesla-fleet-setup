# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.2] - 2026-03-08

### Fixed

- API test cards now show "Vehicle is offline/asleep — tap Wake Up and try again"
  when data sections are empty due to vehicle state, instead of confusing
  "section may not be available" message

## [0.3.1] - 2026-03-08

### Fixed

- Docker build failure: `BUILD_FROM` arg moved before first `FROM` for multi-stage builds
- Vehicle data queries now show actual HTTP errors (403, 401, 408) instead of
  misleading "vehicle may be asleep" message

## [0.3.0] - 2026-03-08

### Added

- **Built-in signing proxy** — tesla-http-proxy compiled from source and included
  in the Docker image. After setup, it runs automatically on port 4443 to sign
  vehicle commands (security level 10+)
- **Auto-inject HA credentials** — Client ID and Client Secret are automatically
  saved to HA's Application Credentials store via WebSocket API after OAuth
  completion. The Tesla Fleet integration picks them up with zero re-entry.
- Self-signed TLS certificate generation for inter-container proxy communication
- Proxy management API endpoints: `/api/proxy/status`, `/api/proxy/start`, `/api/proxy/stop`
- Credential injection endpoint: `/api/inject-credentials` with manual retry button
- Proxy status card on completion page with start/stop toggle
- HA credentials status card showing injection state
- Integration guide showing how to configure HA Tesla Fleet, tesla_custom_component,
  and other integrations to use the signing proxy
- Proxy auto-starts on add-on boot if setup was previously completed
- Comprehensive test suite: 102 tests covering keygen, proxy, server API, credentials,
  and security
- Security tests: credential exposure, tunnel isolation, OAuth replay, file permissions,
  TLS cert validation, input sanitization
- Ruff linting configuration with security rules (bandit)

### Changed

- Dockerfile now uses multi-stage build (Go builder + Alpine runtime)
- Dropped i386 architecture (Go tesla-http-proxy doesn't support it)
- Add-on is now a long-running service (proxy keeps running after setup)
- Completion page updated: "keep this add-on running" instead of "safe to remove"
- Finish Setup instructions simplified (no manual credential entry needed)
- aiohttp upgraded to 3.13.3 (fixes 10 CVEs from 3.9.5)

## [0.2.6] - 2026-03-06

### Changed

- Removed Go/tesla-control from Docker build — fast builds again
- Key enrollment for security level 10 is handled by the HA Tesla Fleet
  integration itself (via Application Credentials + key pairing in the car)
- Added step-by-step guide for completing setup in HA (Application Credentials,
  integration setup, key enrollment with NFC card tap)

### Fixed

- Tesla API "not_a_JSON_request" error: send `{}` body for commands with no parameters

## [0.1.9] - 2026-03-06

### Added

- API Testing page: after setup, test popular Tesla Fleet APIs directly from the add-on
- Vehicle list, state, charge, climate, location, lock, flash lights, honk horn
- Vehicle selector dropdown with Wake Up button for asleep vehicles
- Green/red status indicators show which APIs are working
- Automatic token refresh when access token expires

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
