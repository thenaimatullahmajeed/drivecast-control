# DriveCast project and live-site audit

**Audit date:** 31 October 2026  
**Live site checked:** https://drivecast-2279e37387.centralindia.cloudapp.azure.com/  
**Source reviewed:** DriveCast Google Drive → Azure → YouTube project  
**Audit mode:** read-only live checks and local automated tests. No account was signed in; no channel, authorization, settings, DNS, or server data was changed. No upload was started.

## Executive summary

The hosted sign-in page is reachable over HTTPS, and anonymous requests to the tested account-data APIs are rejected. The local automated backend suite gives useful evidence for the queue, duplicate protection, recovery, folder matching, authorization, and upload-safety logic.

This audit does **not** establish that every live integration is working end to end. The live session endpoint reports that public signup and email delivery are disabled. The complete local suite also has one failing legacy local-panel check. Real Google Drive reads, YouTube uploads/statistics, Telegram delivery, worker heartbeats, live backups, and authenticated dashboard pages could not be confirmed without signing into the private workspace and exercising live integrations. Accordingly, “all functionality is verified working” would overstate the available evidence.

## Live checks

| Check | Result | Evidence and meaning |
|---|---|---|
| Main website | **PASS** | `GET /` returned HTTP 200 and the DriveCast sign-in page over HTTPS. |
| TLS/security response | **PASS, limited** | HTTPS loaded. Response included HSTS, a restrictive Content Security Policy, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`. This is an external header check, not a full security assessment. |
| Application health endpoint | **PASS, shallow** | `GET /healthz` returned HTTP 200, `status: ok`, `version: azure-v1`. The endpoint reports app health only; it does not prove that the database, scheduler, workers, Drive, YouTube, or backups are healthy. |
| Anonymous account/session status | **PASS** | `GET /auth/session` returned HTTP 200, `hosted: true`, `signed_in: false`, `public_signup: false`, and `mail_ready: false`. No private user data was returned. |
| Anonymous API protection | **PASS** | `GET /api/state`, `/api/records`, `/api/backup`, and `/api/server/status` each returned HTTP 401 with “Sign in to your workspace.” |
| Public registration | **NOT ENABLED** | The live session response says `public_signup: false`. Anyone cannot currently create an account from the public form. |
| Email verification/password reset | **NOT READY** | The live session response says `mail_ready: false`; email delivery and password-reset links are not configured/verified. |
| HTTP HEAD on `/` | **MINOR GAP** | A `HEAD /` probe returned HTTP 501. Normal browser `GET /` works; this does not block ordinary page use, but the server does not implement HEAD. |

## Local automated test results

Tests ran from the DriveCast source using the project virtual environment. They used local fixtures/mocks and did not post to real channels.

- **Cloud backend suite:** 38/38 passed. Coverage includes same-name folder/channel matching, queue claims, per-channel and project limits, duplicate reservations, crash/uncertain-upload handling, dry-run/private gates, history import, paused restore, and workspace concurrency.
- **All Python test modules:** 196 tests ran; **195 passed and 1 failed**.
- **Failing check:** `test_control_v2.ControlTests.test_http_only_local_nonce_and_origin` expects the literal text `DriveCast / Local control`. The current panel HTML uses the newer `DriveCast` title, while the local-server reuse check in `app.py` still searches for the old phrase. This is a real legacy local-mode detection mismatch: a second local launch can incorrectly report that an older panel or another app is using the port. It does not by itself show that the hosted Azure login or upload worker is broken.
- The Python `pytest` command is not installed in the available environment. The test suite was run with Python's built-in `unittest` runner instead.
- Browser regression tests in `.cjs` files were not run in this environment because the Playwright Node package is unavailable here.

## Feature-by-feature evidence

| Area | Audit status | What is supported by this audit |
|---|---|---|
| Login/session isolation | **Partially verified** | Live session endpoint works, and the suite tests account isolation/session controls. A real user login and workspace view were not exercised. |
| Dashboard and channel pages | **Not verified live** | Private pages require an account session. The anonymous probe was deliberately limited to confirming access control. |
| Google Drive connection and folder access | **Logic tested; live integration unverified** | Matching and queue safety passed local tests. A live Drive API read and selected-folder scan were not run. |
| YouTube authorization, upload, visibility, and stats | **Logic tested; live integration unverified** | Upload safety and uncertainty handling passed local tests. No real channel token was exercised; no upload or stats refresh was attempted. |
| Scheduler and worker availability | **Not verified live** | `/healthz` does not report scheduler/worker heartbeat. No authenticated Server page or VM service inspection was performed. |
| Encrypted backup and restore | **Recovery logic tested; current backup unverified** | Recovery tests passed within the cloud suite; no live backup was downloaded, decrypted, or restored. |
| Telegram alerts/commands | **Not verified live** | No bot/chat message or command was sent. |
| Publishing safety | **Local safeguards tested** | Tests verify dry-run gates, daily/project limits, reservations, and no blind retry of uncertain uploads. Actual live workspace settings and any external YouTube API approval limits were not inspected. |
| Azure deployment and rollback | **Source reviewed; deployment path not run** | Compose and deployment/restore scripts are present. No VM rebuild, image build, migration, restart, or rollback was performed as part of this read-only audit. |

## Items to address before presenting this as fully operational

1. Configure and verify the intended signup mode. If public signup is desired, configure a real email sender, verify registration and password-reset delivery, then deliberately enable signup. The current live setting is closed.
2. Fix the stale local-panel identity check and update its test to match the current panel. Re-run the full Python suite and the Playwright browser regressions in an environment with Playwright installed.
3. Sign in as the owner and inspect the Server page: web, database, scheduler, worker heartbeat, disk, current version, and last successful encrypted backup.
4. Run non-mutating Drive and YouTube connection checks for each intended route. Confirm exact Drive folder ID and exact YouTube channel ID.
5. If proving upload end to end, use one explicitly approved test video and intended channel. Confirm the resulting YouTube video ID and actual visibility before claiming upload success. This audit did not perform that action.
6. Send a Telegram test only if the bot is configured; confirm the received message and command response.
7. Verify recovery by restoring a copy of the latest encrypted checkpoint into an isolated test workspace and checking that routes remain paused. Do not test by overwriting the live workspace.

## Safe conclusion for sharing

DriveCast’s hosted entry point is live and its anonymous API boundary behaved as expected during this check. The automated cloud-control tests provide good evidence for important safety and recovery logic. The project is **not yet verified end to end in its live authenticated environment**, and account signup/email is currently disabled. A single local test also exposes an outdated local-panel detection string. Resolve the listed items before describing every feature as confirmed operational.

## Scope and limitations

This report is an operational smoke audit, not a penetration test, legal/security certification, performance/capacity test, or YouTube/Google compliance review. No owner credentials were requested or used. A 200 response from `/healthz` confirms only that the HTTP health handler responded. Provider integrations can still fail due to expired/revoked consent, provider quotas, account permissions, or application configuration.
