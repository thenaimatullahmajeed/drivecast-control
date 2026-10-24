# DriveCast: one main folder, one subfolder per YouTube channel

1. Open **Google Drive** and connect the Google account with Editor access to the project main folder. This is separate from YouTube authorization and is done once.
2. Save the main folder. DriveCast lists accessible nested subfolders. Search and pagination help find a folder; duplicate names are distinguished by path and ID.
3. Select a subfolder, choose the exact saved YouTube channel, and confirm the route. Folder names never assign a destination automatically.
4. Prepare the worker, inspect its dry run, then Start Public publishing.

Only videos directly in the selected subfolder are processed. Nested folders can be selected as separate sources; selecting a parent does not publish every child's videos. The worker moves files through Processing, Done and Failed, so Editor access is required. Those operational folders are excluded from source selection.

The backend verifies the selected folder is in the saved project tree and checks its live ancestry again. A moved folder outside the main folder is rejected. An incomplete or failed refresh retains the previous complete folder list. Refreshing never starts or reassigns channels.

Existing YouTube refresh tokens, channel IDs, Google sign-in emails, accepted video IDs, daily reservations and upload history remain intact. Removed-source channels show **Choose Drive folder** and cannot start before confirmation. Existing Drive workers are not stopped by the local schema migration.

Instagram fetching, cookies, Meta/Facebook app settings, their OAuth callbacks and publishing modules, and yt-dlp have been removed from the active application. Earlier provider fields may remain in historical records or private recovery archives. An encrypted workspace backup and source snapshot were made before cleanup under `work/recovery-backups/`.

Remote workers are independent of the panel. Two removed-source workers were paused and their ledgers imported. The suspended account's remote pause could not be confirmed; its local route remains blocked pending folder selection and worker-account repair. Imported legacy sources are normalized into paused Drive routes rather than reactivated.

No proxy settings were added. Stable channel-specific egress can be considered separately for a concrete network requirement. Proxies do not raise a Google API project's quota or replace OAuth.
# Folder discovery and channel connection

New Drive subfolders refresh every five minutes on the Azure scheduler and after successful YouTube authorization. Discovery only updates the folder inventory; it never assigns a channel or starts publishing. Failed scans retain the previous complete inventory and show a notice.

YouTube and unmatched channel rows have **Connect Drive folder**. The exact authorized destination is preselected. The chooser has **Refresh Drive folders**, preserves the channel/daily-limit selection, and requires an explicit subfolder. Only folders below the connected project root are selectable. A new preview queues a read-only dry check and leads to **Confirm channel setup**; existing routes and upload history remain protected.

