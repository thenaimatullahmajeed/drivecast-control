# DriveCast — Google Drive → Azure → YouTube

DriveCast ka online panel HTTPS login ke saath Azure par chalta hai. Google Drive akela video source hai. Har YouTube channel apne exact verified channel ID aur selected Drive subfolder se bind hota hai.

Start with [Fresh start](FRESH_START.md) and the [Roman Urdu Azure guide](azure/README.md).

## Simple setup

1. Hosted panel ke liye Google Web OAuth app add karein; displayed HTTPS callback Google Console mein register karein.
2. Main Drive folder authorize karein.
3. Har intended YouTube channel authorize karein; returned channel ID aur Google sign-in email verify karein.
4. Folder matching mein exact folder/channel preview, read-only dry run aur channel setup confirmation karein.
5. Publishing default off hai. Separately approved test aur go-live ke baad eligible videos daily limit ke andar Public request ke saath publish hote hain.

Unique folder/channel titles trim aur case-insensitive matching se suggest hote hain. Missing folders/accounts aur ambiguous names clearly shown hain; names alone never start uploads.

## Channel management

Dashboard mein real setup checklist aur status hai. Channels search/status filters, direct details aur per-channel/bulk Delete dete hain. YouTube page par unassigned saved authorizations bhi delete ho sakti hain. Backup → Fresh start sab channel data clear karta hai, while retaining the login, API app settings and Telegram configuration. Encrypted pre-cleanup checkpoint retained hota hai. Provider videos/files are never deleted by workspace cleanup.

GitHub page, filters, worker assignment aur recovery buttons removed hain. Active panel GitHub mutation endpoints accept nahi karta. Legacy adapter/workflow files sirf offline history/recovery compatibility ke liye source mein retained hain; Azure workers un par depend nahi karte.

## Durable operation

One Linux VM runs web, scheduler, worker pool, private encrypted backups and Caddy HTTPS. SQLite reservations occur before uploading. A lost job without a reservation can recover; accepted/uncertain uploads are never blindly retried. Exact channel/source identity and daily attempt limits are enforced.

At-most-once identity Drive file ID + YouTube channel ID hai. Copies/manual uploads ya deliberately reset history ko content/title matching se automatically deduplicate nahi kiya jata. Review your existing video backlog before go-live.

TXT/JSON sidecars can provide metadata. Videos move through Processing, Done and Failed only during approved real transfers. Requested Public and verified actual visibility are separate. Missing stats remain unavailable.

## Recovery and access

Invite-only accounts, hashed passwords, secure cookies, workspace isolation, optional TOTP and optional SMTP reset supported hain. Keep the encrypted backup and its separate key/password private. Restore always paused hota hai; new host resume se pehle old runner stopped aur final checkpoint verified hona chahiye.

The local Windows launcher remains a preparation/recovery tool. Use the hosted workspace for 24/7 jobs; a local copy with no cloud workers will not perform hosted transfers.


## Live controls

Dashboard and Channels show the next Drive check countdown and last scanned next filename. Post force now queues one new Public video without bypassing daily limits or duplicate protection. Automatic checks are five minutes apart and process at most one video per channel/check. Automation offers an explicit per-workspace opt-in to start after confirming a verified folder/channel setup.
