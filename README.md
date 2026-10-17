# Private DriveCast recovery

workspace.json contains non-secret configuration and connection labels; records.json contains publishing history.
connections.enc.json, once present, is AES-GCM encrypted with the operator's Recovery Password. No plaintext platform credentials are stored here.

First-use status: set a Recovery Password in the local panel, import the existing GitHub connection, reconnect legacy Google connections once, then press Sync. Existing GitHub repository Secrets cannot be read back; they remain valid in their pipeline but are not yet part of portable encrypted recovery.

Meta has not been configured by the owner. Instagram integration is implemented but has not passed a live account test. Use the local Setup guide to create the app, authorize a supported connection and validate actual source media before enabling.

Per-client runtime upload records are committed independently to that pipeline's drivecast-state branch. Sync checkpoints combined history here. Keep this repository private.
