# DriveCast: fixed-IP groups, signup and fresh deployments

Checked 25 October 2026. Research/read-only validation only; no new resources, billing upgrade, IP assignment or channel migration performed.

## Observed current account

- Central India: Standard_D2as_v5 VM, one NIC/IP configuration; public IP 98.70.57.182.
- Central India network PublicIPAddresses usage 1 / limit 3; regional vCPU usage 2 / 4; B-family usage 0 / 4.
- South India network PublicIPAddresses usage 0 / limit 3; regional vCPU usage 0 / 4; B-family usage 0 / 4.
- These are reported quotas, not a successful deployment or a guarantee of SKU capacity/offer eligibility. B1s availability lookup was stopped before completion; do not call it deployment-verified.
- Live site /auth/session: public_signup=false, hosted=true, mail_ready=false.
- Seven isolated account/gateway tests passed: distinct workspaces and backend processes, separate vault/jobs, cross-user token rejection, Argon2id, lockout/reset, verification-email flow with a mocked SMTP provider, and gateway authentication/CSRF/origin protection. These do not prove live email delivery or an end-to-end second-Azure deployment.

## Fixed-IP design

Each group has at most five YouTube channels and an explicit, tested outgoing route. Keep binding stable; drain work before changing a route. Test actual outgoing IP, not just the IP assigned to a resource. A failed group must not silently use the main server IP; retain uncertain-upload reservations.

One VM can have multiple private/public IP configurations. Explicit worker source routing/SNAT is necessary to bind each group. Azure supports this mechanism; availability and subscription quotas still apply. Do not attach a pool of NAT IPs and assume deterministic per-group mapping.

| Channels | Groups/IPs, maximum five channels each | IP charge for 30 days, USD |
|---|---:|---:|
| 39 | 8 | 28.80 |
| 139 | 28 | 100.80 |

IP counts assume the current public IP can also serve one upload group; a separately reserved management IP would add another address. Live Azure Retail Prices API returned Central India Standard IPv4 Static Public IP at USD 0.005/hour: 720 hours x IP count. IP-only estimate excludes VMs, disks, backup storage, traffic, taxes, exchange rates and contract adjustments.

Short-term alternative within allowed regional quotas: retain the control panel and evaluate small egress worker/gateway VMs in other supported regions, with several bound static IPs per worker. South India B1s Linux retail rate was USD 0.0145/hour (USD 10.44/30 days), compute only; deployment availability not confirmed. Inter-region transfers and management complexity add cost. Start with a bounded pilot before committing to a multi-region fleet. Check overall offer restrictions as well as regional counters.

Long-term alternative: an owner-approved paid subscription with an approved public-IP quota, avoiding a large regional fleet solely for addresses. Free Trial cannot request quota increases. Upgrading retains remaining credit only through the original 30-day credit window; it does not renew credits or prevent later paid charges. Never upgrade automatically.

App Service default outbound addresses are shared/selected from a set; Container Apps outbound addresses can change. Guaranteed static egress needs explicit networking, and can add NAT, compute and data costs. A different service is not a guaranteed cheaper substitute for deterministic groups.

## Capacity prerequisite

Two videos/channel/day means 78 uploads/day for 39 channels or 278 for 139. Current app project guard is 90/day. Current YouTube documentation specifies a default videos.insert quota bucket of 100 calls/day per project; actual owner quota must be checked in Cloud Console. More IPs do not expand the project quota. Request the appropriate quota extension before raising app guards. The current VM has not been load-tested for 139 channels; benchmark transfer size, concurrent work, disk, stats refresh and backup behavior.

## Signup and fresh install

Public signup was selected by the owner. Azure Communication Services Email with an Azure-managed sender domain is a candidate for verification/reset email; it avoids requiring the owner to supply personal Gmail SMTP credentials. It requires resource/domain linkage, delivery configuration, rate limits and a real delivery check before opening public registration. It does not provide fixed IPs for YouTube uploads. Current public signup remains closed.

Maintain two explicit flows:

- Fresh install into another person's Azure: source/runtime only, new account and vault, no existing channels/grants/data, owner authorizes their own Google accounts.
- Owner recovery/migration: authenticated encrypted checkpoint and matching recovery key, original ledger/history preserved, restore paused and old runner fenced.

The full owner recovery ZIP contains the owner's decryption key and grants and must never be used as a student fresh-install package. Existing CLI/bootstrap scripts support operator-led setup; a complete few-click fresh-install wizard is not delivered by this research task.

## Primary references

- Multiple IPs: https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/virtual-network-multiple-ip-addresses-cli
- Limits and Free Trial restrictions: https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/azure-subscription-service-limits
- Azure account upgrade and remaining credits: https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/upgrade-azure-subscription
- IP prices: https://azure.microsoft.com/en-us/pricing/details/ip-addresses/
- Live rate API: https://prices.azure.com/api/retail/prices
- App Service outgoing IPs: https://learn.microsoft.com/en-us/azure/app-service/overview-inbound-outbound-ips
- Container Apps networking: https://learn.microsoft.com/en-gb/azure/container-apps/networking
- YouTube quota: https://developers.google.com/youtube/data/determine_quota_cost
- Azure-managed email domains: https://learn.microsoft.com/en-us/azure/communication-services/quickstarts/email/add-azure-managed-domains
