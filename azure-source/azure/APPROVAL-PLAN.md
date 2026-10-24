# Azure deployment approval plan — 23 October 2026

Scope: one DriveCast test deployment in Azure subscription 1. Existing channels and GitHub workflows remain unchanged. Live uploads disabled.

- One Ubuntu 24.04 LTS VM, 2 vCPU, at least 4 GB RAM; target Standard_D2as_v5 (8 GB), with a lower-cost available two-core alternative only after SKU/quota checks.
- One 64 GB Standard SSD, static IPv4, public Azure DNS label, one private Blob backup container.
- Planning total $85–100 for 30 days for small files; do not create a VM whose compute alone exceeds $75/month. Total operating budget $150/month including traffic/backups. Budget alert is not a hard Azure billing cap.
- Only public web ports 80/443. SSH key-only, restricted to the operator IP if used; otherwise leave port 22 closed and use Azure Run Command. No database port or Docker socket exposed.
- HTTPS login and invite-only registration. Optional SMTP and Google Web OAuth setup require separate credentials from the operator.
- Cloud test workspace starts empty. Copy of existing encrypted backup only if separately approved; no live channel handover or publishing from it.
- Backups encrypted before upload; private Azure Blob primary recovery, downloadable encrypted export for a different Azure subscription. GitHub is optional for source archives, not required to resume work.
- Portal shows $200 credit remaining, expiry 20 November 2026. No pay-as-you-go upgrade authorized. Keep spending limit enabled.

Action-time approval needed for resource creation and the public web/SSH network rules. Azure CLI sign-in is used to make deployment reproducible without extracting browser session credentials.
