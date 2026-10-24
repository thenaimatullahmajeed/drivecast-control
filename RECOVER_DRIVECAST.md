# DriveCast ko naye server par recover karein

Is file ko nayi chat mein dein aur kahein: **“Mera DriveCast last verified encrypted checkpoint se recover karo. Purane uploads dobara upload nahi karne. Pehle target host aur backup verify karo.”**

## Kya chahiye

1. Latest verified `.encrypted` server backup.
2. Uski **original recovery key file**, secure file transfer se; chat text ya GitHub mein key paste na karein.
3. Source ZIP ya is backup ke andar bundled source.
4. Approved new Linux VM with Docker Compose, persistent disk, public HTTPS hostname, and Google OAuth app access for callback registration.

Sirf yeh MD/config file data recover nahi karti. Actual encrypted backup aur matching key required hain. Valid/unrevoked YouTube/Drive grants backup mein preserved rehte hain; revoked/expired grants ko reconnect karna pad sakta hai.

## Current system identifiers — secrets nahi

- Existing URL: https://drivecast-2279e37387.centralindia.cloudapp.azure.com/
- Azure RG / VM: `drivecast-test` / `drivecast-primary`, Central India.
- Private independent recovery folder: https://drive.google.com/drive/folders/108fYNMXe62BaQWbb04tdjhrh_tKsK4Qq
- Recovery Drive account: `themyqueenofficial@gmail.com`.
- Private source repo: https://github.com/thenaimatullahmajeed/drivecast-control — source under `azure-source/`.
- Original local task: `C:/Users/naima/Documents/Codex/2026-10-16/search-github-and-web-for-open`.
- Local backup: `work/DriveCast-Azure-Latest.encrypted`.
- Original key: `work/DriveCast-Azure-Recovery.key`.
- Private recovery destination config is bundled inside each server snapshot, at `recovery/destination.json`.

The videos main folder is separate and shared. Recovery snapshots must stay in the private recovery folder; never move them into the videos root or change sharing implicitly.

## Backup behavior

Automatic encrypted snapshots go to Azure Blob and independently to private Google Drive. Each target is attempted even if the other fails. Drive size/checksum are verified after upload. Source is embedded in the encrypted snapshot; a standalone allowlisted source ZIP is also mirrored into private Drive and reused when unchanged.

Authorizations, route/control changes, reservations and accepted/uncertain upload updates mark recovery dirty. The backup service coalesces changes in the background, normally on its next minute check, and takes an hourly checkpoint even without these events. Publishing never waits for remote backup. A failed remote backup retries with backoff and remains visible.

Recovery point is the **last actually verified remote checkpoint**, not the last upload or latest local file. A sudden outage between a change and a completed remote backup can leave a gap. Never promise absolute zero data loss. Reconcile post-checkpoint uploads against Drive and YouTube before resuming; uncertain identities stay quarantined.

Backups include workspaces, encrypted grants and service unlock material inside outer encryption, database history/reservations/stats, accounts/login hashes, recovery destination config, and the source bundle. The external recovery key is deliberately excluded. Runtime `.env` and server SSH/deploy keys are not in the source repo; configure new-host operational settings separately. Drive source video files remain in Google Drive.

## Restore on Azure or another provider

Use an Ubuntu 24.04 VM or equivalent compatible Linux Docker host with persistent local storage. A static-only website host or short-lived serverless function is not a drop-in replacement. Start with roughly 2 CPU / 4–8 GB RAM and room for temporary video transfers; benchmark before increasing concurrency. The app does not require Azure-specific upload workers.

1. If the old server is reachable: pause publishing, settle in-flight work, take and verify a final checkpoint, then stop old workers. If inaccessible: confirm its shutdown/fence before new publishing; a suspension that later lifts must not create two active runners.
2. Download the newest **verified** `.encrypted` file from private Drive. Check saved SHA256/provider checksum. Preserve the backup unchanged and keep the recovery key separately.
3. Download/extract `DriveCast-Source-*.zip`, or clone the private source repo and use `azure-source/`. If only the encrypted snapshot is available, use `hosting/extract_source.py` with the original key; it extracts authenticated source without starting any worker.
4. On a clean approved target, copy backup/key privately, then run from the extracted source:

```bash
sudo RECOVERY_KEY_FILE=/private/original-recovery.key SERVER_BACKUP_FILE=/private/latest.encrypted bash hosting/bootstrap.sh new.example.com existing-owner@example.com
```

This installs Docker if required and invokes the shared installer. It refuses to overwrite active data and restores routes **paused**. It does not create cloud resources or change DNS/firewall rules. Those must be configured for the approved target: public 80/443, SSH restricted/key-only, no exposed database port. Actual provider costs are separate.

5. Register `https://new.example.com/oauth/callback` on the **same Google Web OAuth client**. Preserve original Google client IDs/secrets from the vault. Update hostname; do not casually replace the OAuth app or reauthorize all channels.
6. Verify account/workspace counts, channel IDs, folder mappings, accepted YouTube IDs, reservations, service unlocks, and backup target access. Restored running/queued jobs are quarantined; reserved uncertain uploads are not automatically retried.
7. Reconcile any gap since the last backup. Complete a read-only check and verify the old server fence. Only then enable the server live gate and approved routes.
8. Verify a new off-Azure recovery checkpoint on the new host. Reconfigure optional Azure Blob identity/storage when using Azure; on other providers Drive recovery works without Azure managed identity. GitHub deploy-key source sync is optional and must be re-provisioned per host. An unset trial deadline on paid hosting is supported.

If another chat cannot access these local paths, ask the owner for the encrypted snapshot/key through secure file handling; do not guess credentials or claim recovery is complete from the MD alone.

## Original safeguards to retain

- Google Drive is the only video source; Instagram/Meta source is removed.
- Default two attempts per channel/day; current worker pool two; project guard 90/day; max file 250 MB; monthly transfer guard 100 GiB. These are app guards, not vendor quotas or verified capacity guarantees.
- Reserve in a database transaction before uploading. No blind retry after uncertain upload or a lost worker lease.
- Exact YouTube channel ID and folder ancestry verification; no fuzzy-name automatic publishing.
- Separate account workspaces; do not merge one student's vault/data into another workspace.
- Never run both old and new publishing workers concurrently for the same routes.

## Files for the next operator

- `hosting/bootstrap.sh`: provider-independent clean-host install/restore.
- `hosting/extract_source.py`: source-only extraction from encrypted snapshot.
- `azure/install.sh`, `azure/restore.sh`: shared restore mechanics.
- `cloud_backup.py`, `recovery.py`: encrypted snapshots and independent Drive verification.
- `source-files.json`: strict source allowlist; runtime files/secrets excluded.
- `hosting/push-source.sh`: private repo-scoped source mirror after successful deployments.

No AI SEO integration is assumed by this recovery guide. Preserve the actual recovered version and inspect its configuration rather than inventing completed features from prior discussion.
