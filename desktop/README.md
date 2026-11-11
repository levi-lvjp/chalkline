# Owner page

The Desktop page uses the shared Hallmark layout. Its route is `/chalkline` and its sidebar label is **Chalkline**. Layout hooks use `data-page`, `data-view`, `data-state`, `data-job-number`, `data-source-id` and `data-approval-code`.

## Open in Desktop

In Hermes Desktop, open Settings → Plugins and install the plugin from a local checkout. Enable its Desktop half. Select the backend/profile **chalkline-ui**, enable the agent half there, then click **Chalkline** in the sidebar. The page requests its backend through `ctx.rest`, using the selected profile and the Desktop connection's authentication.

For local verification use a separate Hermes home with a profile named `chalkline-ui`; `scripts/demo_seed.py` seeds it with fictional data. The seed clock is `2026-11-11T10:00:00+00:00`.

To start a dedicated local backend from a checkout:

```powershell
$env:HERMES_HOME = Join-Path (Get-Location) '.runtime'
$env:CHALKLINE_TEST_NOW = '2026-11-11T10:00:00+00:00'
hermes -p chalkline-ui serve --isolated --host 127.0.0.1 --port 18193
```

The hidden dashboard manifest mounts the API without adding a duplicate standalone-dashboard sidebar tab. Native Desktop activation remains an owner step. The completed local verification used the actual v0.22.2 server plus a clean Edge browser harness running the same React page at 390px. It loaded 12 job cards, selected a real job, rendered its ID-based thumbnails, and reported no page errors or horizontal overflow.

## Authentication boundary

The API is loaded only by the enabled plugin's documented dashboard manifest. It relies on Hermes' auth middleware when mounted under `/api/plugins/chalkline`. Do not expose this router as an independent unauthenticated application.

Verified against the installed v0.22.2 source:

- `hermes_cli/web_server.py:721` defines the middleware requiring a valid session token for non-public `/api/` requests. The plugin namespace is covered. `web_server.py:709` invokes the cookie/OAuth gate for reachable interfaces configured with authentication.
- `hermes_cli/web_server.py:674` checks enablement on every authenticated plugin request.
- `hermes_cli/web_server_dashboard.py:1020` mounts the manifest's router under `/api/plugins/<name>` with request-profile scoping.
- `apps/desktop/src/api/plugins.ts:72` implements `ctx.rest` using the namespaced profile-scoped `hermesApi` bridge. `apps/desktop/electron/main.ts:5631` attaches `X-Hermes-Session-Token` for token connections, with Bearer credentials on the corresponding connection path.

The plugin imports no private auth helpers. Route tests apply the host auth middleware and check that every route returns 401 without its credential. Approve/reject/payment services receive `actor="dashboard"`. CORE retains payload hashes, expiry, single-use checks and audit attribution. A payment form creates a proposal for separate owner approval.

## Components and API

- `plugin.js` registers the page and sidebar.
- `page.js` coordinates fetches, refreshes, selection, actions and export.
- `components.js` renders evidence, approvals, payments and job records.
- `money.js` formats integer minor units according to the currency's scale.
- `../chalkline/ui/api.py` projects CORE queries and sends mutations through service.py.

The API exposes overview, job detail, bounded JPEG thumbnails, base64 ZIP export, approve/reject and payment proposals. Responses remove filesystem paths. Monetary values stay in integer minor units. Generated document filenames remain visible in approval previews.

## Theme selection

Change `ACTIVE_THEME` at the top of `tokens.js` to `console`, `coral` or `dose`. Console is the default. Palette, embedded fonts, type, surfaces, radii, shadows, texture and responsive rules live in that single module. Font notices are in `FONT-NOTICES.txt`. The page keeps styles inside `.chalkline-ui` to avoid changing Hermes Desktop chrome.

The board scrolls sideways on narrow screens; the selected job moves below the working area. Pending approvals expand to show exact proposed text and documents. Payments create a proposal for owner approval; accountant export downloads a ZIP. Loading, empty and retry states retain the page navigation.
