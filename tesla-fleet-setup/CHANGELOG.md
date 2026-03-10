# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] - 2026-03-10

### Added

- **Cloudflare Tunnel fallback** — when DuckDNS reachability test fails
  (ISP blocks port 443, double NAT), the wizard offers a Cloudflare Tunnel
  option. User creates a named tunnel in Cloudflare Zero Trust, pastes the
  token, and the add-on runs `cloudflared` as a persistent subprocess
- Tunnel auto-restarts on crash and persists across add-on reboots
- Step-by-step Cloudflare setup instructions in the wizard
- `cloudflared` binary added back to Docker image (all architectures)
- API endpoints: `/api/cloudflare/start`, `/api/cloudflare/stop`,
  `/api/cloudflare/status`

### Changed

- DuckDNS remains the **primary** domain option (works for most users)
- Cloudflare Tunnel section only appears when reachability test fails
- Reachability test now works for both DuckDNS and Cloudflare contexts

## [0.6.13] - 2026-03-10

### Fixed

- SSDP discovery now filters responses by `InternetGatewayDevice` ST header —
  no longer picks up Hue bridges or other non-IGD UPnP devices
- UPnP port mapping supports different external/internal ports for diagnostics

### Added

- Diagnostic endpoint `/api/duckdns/port-test` — maps external 8443 → internal
  443 to test if ISP blocks specific ports

## [0.6.12] - 2026-03-10

### Fixed

- **UPnP rewritten in pure Python** — replaced `upnpc` CLI with direct SSDP
  multicast discovery + SOAP `AddPortMapping` calls. Fixes Netgear Orbi (and
  similar mesh routers) which `miniupnpc` reports as "(not connected?) IGD"
  despite working UPnP. The new implementation discovers the IGD, parses the
  XML root description for the `WANIPConnection` control URL, and calls the
  SOAP action directly — bypassing miniupnpc's broken connection status check

## [0.6.11] - 2026-03-10

### Fixed

- UPnP now binds to the correct network interface — auto-detects the LAN IP
  (e.g. `192.168.1.23`) and passes it to `upnpc -m <ip>` so SSDP multicast
  goes out the right interface instead of failing with `SIOCGIFADDR: No such
  device`. Previous `-m 5` was wrong (treated as an address, not a timeout)

## [0.6.10] - 2026-03-10

### Changed

- UPnP now logs network interfaces (`ip -4 addr show`) on each attempt —
  confirms whether `host_network: true` is active. If you see only `lo`
  and `docker0`, the add-on needs to be **uninstalled and reinstalled**
  (not just updated) for host networking to take effect

## [0.6.9] - 2026-03-10

### Fixed

- UPnP discovery now uses extended 5-second multicast timeout (`-m 5`) —
  mesh routers like Orbi need more time for SSDP responses to propagate.
  Also logs full discovery output to help diagnose UPnP failures

## [0.6.8] - 2026-03-10

### Fixed

- Retry UPnP button threw "BASE is not defined" — switched to relative URL
  matching all other fetch calls in the wizard

## [0.6.7] - 2026-03-10

### Added

- **Retry UPnP button** on the UPnP warning — when UPnP port forwarding fails,
  the warning now includes a "Retry UPnP" button that re-attempts the port
  forward without re-running the whole DuckDNS activation

## [0.6.6] - 2026-03-10

### Fixed

- Subdomain input no longer doubles `.duckdns.org` — if the user pastes the
  full domain (e.g. `ha-fleet-xyz.duckdns.org`), the suffix is stripped
  automatically on both client and server side
- Reachability test button now shows "Retry" on failure and "Test Again" on
  success, and disables during the test to prevent double-clicks

## [0.6.5] - 2026-03-10

### Fixed

- Enable `host_network: true` so UPnP multicast discovery can reach the
  router. Docker bridge networking blocks SSDP multicast, preventing UPnP
  port forwarding from working

## [0.6.4] - 2026-03-10

### Fixed

- Subdomain must not contain "tesla" — Tesla's developer portal rejects
  domains with "tesla" in the name. Auto-generated subdomain changed from
  `ha-tesla-xxxx` to `ha-fleet-xxxx`, validation added before activation,
  and a visible warning added to the subdomain step

## [0.6.3] - 2026-03-10

### Changed

- DuckDNS activation now shows step-by-step progress with live checkmarks:
  verify credentials, UPnP port forward, IP update, certificate, HTTPS server.
  Each step shows a spinner while active and a tick/warning/cross when done
- Split monolithic `/api/duckdns/setup` into individual endpoints (`/upnp`,
  `/ip`, `/cert`, `/start`) for granular progress reporting
- UPnP failure shows a warning (not an error) since manual port forwarding
  is a valid fallback

## [0.6.2] - 2026-03-09

### Fixed

- DuckDNS input fields now properly styled — larger, full-width, visible
  placeholder text, and monospace font matching the rest of the wizard

## [0.6.1] - 2026-03-09

### Changed

- Replaced Cloudflare tunnel with DuckDNS-only domain setup
- Step-by-step DuckDNS wizard with auto-generated subdomain and screenshots
- UPnP auto-port-forwarding integrated into setup flow
- Removed cloudflared binary from Docker image (smaller build)
- Removed tunnel guard middleware (no longer needed)

## [0.6.0] - 2026-03-09

### Added

- **DuckDNS integration** — stable permanent domain (`subdomain.duckdns.org`)
  that persists across add-on restarts. Tesla periodically verifies
  `.well-known/appkeys` and revokes the vehicle key when the endpoint is
  unreachable — a stable domain is essential
- **Step-by-step DuckDNS guide** in the wizard with screenshots, auto-generated
  subdomain suggestion with copy button, and inline instructions
- Automatic Let's Encrypt certificate via DNS-01 challenge (no port 80 needed,
  uses DuckDNS API for DNS verification)
- HTTPS server on port 443 serving `.well-known/appkeys` with valid TLS
- DuckDNS IP updater runs every 5 minutes in the background
- Certificate auto-renewal check every 12 hours
- **UPnP auto-port-forwarding** — port 443 is automatically opened on the
  router via UPnP, no manual port forwarding needed on most routers. UPnP
  mapping is refreshed every 5 minutes to prevent expiry. Falls back to manual
  port forward with a clear message if UPnP is unavailable
- Port 443 added to config.yaml for DuckDNS HTTPS
- `certbot` and `miniupnpc` added to Docker image

### Changed

- DuckDNS state (`duckdns_subdomain`, `duckdns_token`) persisted across restarts

### Removed

- **Cloudflare quick tunnel** — removed entirely. Quick tunnels generated
  ephemeral hostnames that changed on every restart, causing Tesla to revoke
  vehicle keys. DuckDNS replaces this with a stable, permanent domain
- Tunnel guard middleware (no longer needed — port 8099 is only accessible
  via HA ingress, port 443 only serves `.well-known`)
- `cloudflared` binary removed from Docker image (smaller image size)

## [0.5.5] - 2026-03-09

### Fixed

- Wizard page now auto-refreshes state when opened via HA sidebar — no more
  manual page refresh needed (listens for visibility and focus events)

## [0.5.4] - 2026-03-09

### Fixed

- Removed spinner animation from generate keys — just shows "Generating..." and
  moves straight to step 2
- API test diagnostics now show which response keys were returned when a section
  is missing, and handle both nested and flat response structures

## [0.5.3] - 2026-03-09

### Added

- **Re-register button** on completion page — quick tunnels get new hostnames on
  restart, so partner registration becomes stale. Users can now re-register the
  current domain with Tesla without resetting the whole wizard

## [0.5.2] - 2026-03-09

### Fixed

- **Key enrolment instructions** — replaced incorrect "tap NFC key card" with
  the correct virtual key enrolment flow: open `https://tesla.com/_ak/DOMAIN`
  on your phone, approve in the Tesla app. Completion page now shows the enrol
  URL with a copy button

## [0.5.1] - 2026-03-09

### Added

- **Region dropdown** in Step 3 — users pick North America, Europe/Middle East/
  Africa, or Asia-Pacific before entering credentials. Region is sent with
  credentials and applied immediately so partner auth and all API calls use the
  correct endpoint from the start
- Region selection is restored when returning to Step 3

## [0.5.0] - 2026-03-09

### Added

- **EU/EMEA region support** — auto-detects the user's Fleet API region (NA vs
  EU) after OAuth using Tesla's `/api/1/users/region` endpoint. Previously
  hardcoded to NA, which caused 412 errors for European users
- Partner registration now registers on **both** NA and EU endpoints so the app
  works regardless of where the user's account lives
- API requests auto-retry on 412 (wrong region) by detecting and switching to
  the correct regional endpoint
- Region is persisted in state and restored on add-on restart
- 4 new tests for region detection (106 total)

## [0.4.2] - 2026-03-09

### Changed

- Simplified completion page — clear 4-step "Next Steps" guide that anyone can
  follow, compact proxy/credentials status, API testing tucked into a collapsible
  section. Removed duplicate instructions, lock/honk test cards, and proxy
  integration docs

## [0.4.1] - 2026-03-09

### Fixed

- Completion page showed "(unknown)" for the domain after an add-on restart —
  the tunnel URL from the status API was not being read before rendering the
  completion page, so users saw no domain to copy into the HA integration

## [0.4.0] - 2026-03-08

### Added

- **Auto-install private key** — private key is automatically copied to
  `/config/tesla_fleet.key` so the HA Tesla Fleet integration finds it
  without manual file management
- **Finish Setup guide** on completion page — shows the exact domain to paste
  when adding the Tesla Fleet integration in HA, with copy button
- **Tunnel stays running** after setup — the Cloudflare tunnel keeps serving
  the public key so the HA Tesla Fleet integration can verify it during its
  own domain registration step
- Tunnel auto-restarts on add-on reboot (warns if hostname changed)

### Changed

- Added `config:rw` mapping so the add-on can write the private key to HA's
  config directory
- Step 3 instructions now show two origin URLs and two redirect URIs
  (one for the wizard, one for the HA integration)
- Completion page reorganised: "Finish Setup in HA" guide appears first,
  followed by proxy status and API testing

## [0.3.3] - 2026-03-08

### Fixed

- Step 3 now instructs users to add **two** redirect URIs and **two** origin URLs:
  one for the setup wizard, and one for the HA Tesla Fleet integration
  (`https://my.home-assistant.io/redirect/oauth`). Without the second URI,
  adding the Tesla Fleet integration in HA fails with "redirect_uri not registered"

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
