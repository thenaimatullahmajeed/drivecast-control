# DriveCast Azure — asan Roman Urdu guide

## Is build ka scope

Google Drive akela video source hai. Instagram/Meta downloader aur login removed hain. Purane Facebook records backup mein bachay rehte hain; Facebook publishing is Drive → YouTube build mein active nahi hai. Telegram, YouTube stats aur purani history retained hain. GitHub page aur uske controls removed hain. Offline legacy adapter sirf purani history/recovery compatibility ke liye retained hai. Azure worker ko GitHub ki zaroorat nahi.

Yeh release **dry-run-only** start hota hai. Public posting, private test aur har existing channel ka handover alag explicit confirmation mangta hai. Sirf account connect, folder match ya backup restore hone se upload shuru nahi hota.

## 1. Budget aur trial

Portal mein Free Trial subscription check karein. Last observed credit $200 tha aur expiry **20 November 2026** thi. Credit VM banane ke din se dobara 30 din nahi chalta.

Target: ek 2 CPU / 4–8 GB Linux VM, 64 GB Standard SSD, static IP aur private Blob backup. Small videos ke liye planning $85–100/30 din hai, guarantee nahi. Transfer volume, region aur extra services bill badal sakte hain. Approved planning ceiling $150 hai; yeh code Azure billing ka hard cap nahi lagata.

Cost Management → Budgets → Add: monthly amount **$200**, actual alert **75% ($150)** aur forecast alert 50%, apni email. Resource-group filter `drivecast-test` lagayen. Budget warning VM ko automatically band nahi karti. Trial spending limit enabled rakhein; pay-as-you-go upgrade khud se na karein.

## 2. VM ka reproducible setup

Azure Cloud Shell mein **Bash** choose karein. Is project ka source upload/extract karein aur `azure` directory kholein. Apna SSH public key use karein; private key share na karein.

```bash
export REGION=centralindia
export ZONE=1
export VM_SIZE=Standard_D2as_v5
export RG=drivecast-test
export DNS_LABEL=apna-unique-drivecast-name
export SSH_PUBLIC_KEY="$HOME/.ssh/id_ed25519.pub"
# Sirf agar SSH chahiye: apni current public IPv4 /32. Warna SSH band rahega.
# export ADMIN_CIDR=203.0.113.10/32
export APPROVED_CREATE=1
bash create.sh
```

Script pehle SKU availability check karta hai. Unavailable aaye to script resource banane se pehle rukta hai; kisi permitted region/size ki current price aur quota check karke parameters badlein. Spot VM use nahi karna. Windows/RDP, Bastion, NAT Gateway, load balancer ya managed database is first single-VM setup ke liye zaroori nahi.

VM par `cloud-init status --wait` success confirm karein. Docker, Compose, security updates aur time sync install honge. NSG mein public sirf TCP 80/443; optional SSH sirf operator IPv4. Database, app 8080 aur Docker socket public nahi hain.

## 3. Source deploy aur HTTPS

Source ko VM ke `/opt/drivecast/releases/<version>/` mein rakhein. Is directory mein Dockerfile aur azure folder dono hon.

`azure/.env.example` se `/opt/drivecast/secrets/.env` banayen. `DRIVECAST_HOST` Azure static IP ka DNS name ho; `DRIVECAST_ORIGIN=https://<host>` aur `DRIVECAST_OAUTH_REDIRECT=https://<host>/oauth/callback` set karein. Random 32-byte base64 `DRIVECAST_BACKUP_KEY` generate karke private file mein save karein. Key ko app log, repo ya chat mein paste na karein. File `chmod 600` aur secrets folder `chmod 700` rakhein.

```bash
sudo bash /opt/drivecast/releases/<version>/azure/deploy.sh /opt/drivecast/releases/<version>
```

Deploy script build, additive schema initialization, service restart, health check aur application rollback karta hai. Existing release upgrade se pehle encrypted snapshot leta hai. Source pull automatic tabhi ho sakta hai jab aap source repo configure karein; default extracted release se deploy karta hai. Caddy automatic HTTPS certificate leta hai. Public site par certificate aur `/healthz` verify karein.

SQLite ek VM ke persistent local disk par authoritative database hai. PostgreSQL/multiple-VM mode abhi implemented nahi; multi-VM scale ke liye database adapter aur distributed fencing pehle add aur test karna hoga. Ek database ko network share par rakh kar multiple VMs na chalayein.

## 4. Pehla login

Registration invite-only hai. VM par owner invitation protected file mein banayen:

```bash
cd /opt/drivecast/current/azure
sudo docker compose exec web python cloud_admin.py invite --email your-email@example.com --output /data/owner-invitation.txt
```

Invitation URL private taur par khol kar apna sign-in password set karein. Password kam az kam 12 characters ho. Har login ka apna separate workspace hai. Server page → authenticator optional 2FA aur session list available hain. “Sign out everywhere” tamam current workspace sessions revoke karta hai.

Email password-reset SMTP configure hone ke baad kaam karta hai. SMTP ke baghair reset button unavailable message deta hai; is feature ko working assume na karein. Mail provider app password/relay credentials env mein set karein. Sign-up later kholne/band karne ke liye server operator `cloud_admin.py signup-open` / `signup-close` chala sakta hai; ordinary workspace browser account ko global security switch nahi diya gaya.

## 5. Purani authorizations ko bachana

Local panel → Export encrypted workspace. Hosted panel mein pehle backup unlock password create/unlock karein, phir Backup → Preview/Restore mein original exported file aur uska original recovery password dein. Restore purane routes ko **paused** rakhta hai. Same Google OAuth client + refresh tokens preserve hote hain; har channel ko foran dobara login karwana zaroori nahi, jab tak grant valid ho. Windows “remember unlock” Linux par transfer nahi hota.

Automatic restart ke liye hosted server per-workspace vault key ko owner-only service key file mein rakhta hai. Root/server compromise par vault bhi accessible ho sakta hai, is liye backups aur VM access ko private rakhein. Sign-in password aur backup recovery password alag hain.

Naye Google authorizations ke liye Google Cloud mein **Web application** OAuth client banayen. Exact redirect URI `https://<host>/oauth/callback` register karein. Hosted YouTube page par Web client JSON set karein. Existing Desktop client ke refresh tokens ko new Web client ID se replace na karein; saved old grants apna original client use karte hain.

Consent screen “In production” karein. Sensitive scopes ke liye Google verification jo applicable ho complete karein. Testing-mode grants/expiry aur revoked access reconnect mang sakte hain. Har consent ke baad tool `channels.list(mine=true)` se exact returned channel ID verify karta hai. Google sign-in link apne intended browser/profile mein kholein.

## 6. Drive → channel matching

Google Drive mein main folder once connect karein. Editor access zaroori hai. Subfolders refresh karein. Processing / Done / Failed operational folders list se hidden hain. Folder ke directly andar videos process hoti hain; nested client folders alag choose hote hain.

Folder matching page ke states:

- **Matched:** unique folder name aur unique authorized channel title spaces trim aur case ignore karne ke baad same.
- **Folder found, waiting for channel authorization:** usi naam ka YouTube channel authorize karein.
- **Channel found, no folder:** main folder mein same naam ka client folder banayen.
- **Ambiguous name:** exact folder path/ID aur YouTube ID manually select/confirm karein.

Review cloud preview → exact folder/channel ID confirm → read-only dry run. Yeh Drive files move, folder create, ledger reservation ya YouTube upload nahi karta. 39 channels ke liye pehle matching/result list check karein; authorized channels/folders ki actual quantity ke baghair 39 matched hone ka claim nahi.

## 7. Safe handover aur go-live

Har live channel ka exact handover separately confirm karna hai. Purana GitHub worker disabled + ENABLED=false hona aur koi active run na hona fresh check se verify hoga. Uske baad complete ledger import hota hai. Suspended/inaccessible GitHub account se complete history/fence prove nahi ho sakti; us route ka automated handover blocked rahega.

Cloud stays paused after handover. Pehla one-video private test separately approve karein. Test ka accepted video ID aur actual visibility verify karein. Uske baad `DRIVECAST_LIVE_UPLOADS=1` server gate aur per-channel Start explicitly approve karein. Public route mein schedule nahi: worker ke next eligible check par Public request hoti hai, completion time guarantee nahi. Per-channel daily cap attempts ko count karta hai, uncertain attempts bhi. Default 2 attempts/channel/day; workers 2 concurrent transfers, project cap 90/day, max video 250 MB aur application monthly transfer cap 100 GiB conservative defaults hain. Yeh app caps hain, Google ya Azure authoritative quota/billing reading nahi.

39 × 2 = 78 upload attempts/day. API project ka actual Console quota confirm karna zaroori hai. 2 CPU par 39 metadata routes sensible starting target hai; load test ke baghair maximum throughput guarantee nahi. Transcoding/rendering included nahi. First benchmark file size, download/upload duration, free disk aur memory record karein; phir concurrency badhayen.

Reservation database transaction upload se **pehle** commit hoti hai. Worker lease lost ho to unreserved job safe worker ko mil sakti hai; reserved/uncertain video manual-review quarantine mein rehti hai. YouTube accepted ID aur Drive Done/Failed states record hote hain. Automatic replay/re-upload nahi.

## 8. Encrypted backup aur next Azure par move

Primary off-server recovery ke liye private Azure Blob use karein. Storage account mein public blob access off, TLS 1.2+, private `drivecast-backups` container. VM managed identity ko **sirf is container** par Storage Blob Data Contributor role den. Yeh persistent access grant approval ke baad hi karein. `DRIVECAST_BACKUP_STORAGE` env mein account name set karein. Backup service archive encrypt karke Blob upload aur size verify karti hai; Server page “remote verified” status dikhata hai. Nightly run + “Run encrypted backup” available.

Retention Azure Storage lifecycle setting mein choose karein, misal 14–30 days. Application khud cloud backup delete nahi karti. Local backups bhi retain rehte hain; free disk monitor karein. Retention/delete policy change aapke explicit approval ke baad hi apply karein. Encrypted export ko additionally apne Drive mein rakhna theek hai, lekin automated Drive-backup destination is version mein configured nahi. GitHub code archive optional; credentials ya unencrypted records repo mein kabhi nahi.

Migration ka zero-surprise tareeqa: old server se publishing pause → in-flight work finish → final encrypted checkpoint + verify → old workers stop → new VM setup → same private backup key restore → `restore.sh <encrypted-file>` → channels/records counts aur exact IDs compare → dry run → explicitly resume. Restore always paused. Nightly backup se restore karne par recovery point last successful backup hai; last backup ke baad ke events ko reconcile kiye baghair zero-data-loss assume na karein.

## 9. Monitoring aur Telegram

Server page disk, scheduler/worker heartbeat, recent checks, version, last backup aur idle-worker restart dikhata hai. Restart request active transfers ko kill nahi karti. Telegram private bot/chat link kar ke commands enable karein: `/today`, `/stats`, `/channel <exact name>`, `/status`, `/pause <name>`, `/resume <name>`. Cloud `/resume` bhi history verification aur server go-live gate ke baghair publish nahi karta.

Azure Monitor → Application Insights availability test se `https://<host>/healthz` check aur apni email ka Action Group add karein. Retest interval/cost pehle review karein. Server band ho to apna Telegram alert khud nahi bhej sakta; independent availability alert isi liye chahiye. SMTP, Telegram chat authorization aur email Action Group ki required human approvals ko bypass nahi kiya jata.

## 10. Trial ending checklist

**17 November 2026 (3 din pehle):** final backup run/verify, offline backup key + encrypted Export save, current Azure costs check, paid continuation ya new host decide. Server page 3-day warning dikhata hai; linked Telegram ko configured scheduler se reminder milta hai. Agar server band ho ya Telegram unconfigured ho to external calendar/reminder bhi rakhein.

**20 November se pehle:** pause → final checkpoint → old runner fence → approved upgrade ya new VM restore. Trial credit expiry par shutdown/data retention ko backup strategy na samjhein. No automatic pay-as-you-go upgrade is performed.

## Primary references

- [Azure trial / avoid charges](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/avoid-charges-free-account)
- [Azure budget alerts](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-acm-create-budgets)
- [Azure Linux VM prices](https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/)
- [Azure bandwidth prices](https://azure.microsoft.com/en-us/pricing/details/bandwidth/)
- [Google offline access](https://developers.google.com/identity/protocols/oauth2/web-server#offline)
- [YouTube quota / audits](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits)


## Naye VM par chhota install / restore command

Source ZIP extract karne aur VM/cloud-init ready hone ke baad first install:

```bash
sudo bash azure/install.sh <azure-dns-hostname> <apna-owner-email>
```

Script protected configuration aur random backup key banata hai, image build karta hai, owner invitation private file mein rakhta hai aur HTTPS services start karta hai. Key ka offline copy alag rakhna zaroori hai. Private Blob storage account aur container-scoped VM role pehle configure karein, warna backup local-only rahega aur Server page isay wazeh batayega.

Naye Azure par existing whole-server backup restore karna ho to dono private files pehle securely copy karein:

```bash
sudo RECOVERY_KEY_FILE=/private/original-recovery.key SERVER_BACKUP_FILE=/private/latest.encrypted bash azure/install.sh <new-dns-hostname> <apna-owner-email>
```

Existing data overwrite nahi hota. Existing logins/grants/records restore hote hain; channels paused rehte hain. Purana server fenced aur final checkpoint verified hone ke baad hi resume karein.

At-most-once tracking Drive file ID + exact YouTube channel ID par hai. Nayi file ID wali copy ya tool se bahar ki manual uploads ko content/title se automatically duplicate declare nahi kiya jata. Pehle go-live par purani manual-posted backlog review karein; same title ko duplicate ka reliable proof na samjhein.
