# Reassigning a channel

GitHub cards have a **Reassign channels** button. Choose one prepared channel and a healthy GitHub account with a free slot, then preview the handover. The preview checks the destination identity, active workers, portable credentials and original upload ledgers. The copy stops the source workflow, verifies every branch/tag and ledger, then resumes the destination only if the original was enabled. Uncertain uploads remain reserved; the source repository remains an archive.

Each GitHub account can receive at most **2 channels**. The backend applies this limit to new routes, automatic placement, CSV imports and manual/automatic migrations, including direct API calls. Older accounts already holding more channels are preserved; they cannot receive additional channels until space is available.

Automatic failover remains **staged** in this workspace until the user adds an eligible account and requests activation. After activation, eligible handovers use the existing verified migration without asking for each move. The local DriveCast server must remain running. If the source cannot be stopped and its complete history verified, no second worker is created or enabled.

GitHub suspensions require GitHub appeal and reinstatement. Reassign cannot move a suspended account to bypass enforcement. Provider rate limits wait for reset rather than switching accounts around the limit. There is no hidden automation or human-impersonation feature.

No live migration was performed while implementing this feature.
