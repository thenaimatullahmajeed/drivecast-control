#!/usr/bin/env bash
# Run in Azure Cloud Shell Bash from the extracted source/azure directory.
set -euo pipefail
umask 077
REGION="${REGION:-centralindia}"
ZONE="${ZONE:-1}"
VM_SIZE="${VM_SIZE:-Standard_D2as_v5}"
RG="${RG:-drivecast-test}"
VM="${VM:-drivecast-primary}"
ADMIN="${ADMIN:-drivecast}"
DNS_LABEL="${DNS_LABEL:?Set DNS_LABEL to your unique lowercase Azure DNS label}"
SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:?Set SSH_PUBLIC_KEY to an existing .pub file}"
ADMIN_CIDR="${ADMIN_CIDR:-}"
[[ "$DNS_LABEL" =~ ^[a-z][a-z0-9-]{2,61}[a-z0-9]$ ]] || { echo 'Invalid DNS label'; exit 2; }
az account show --query '{subscription:name,state:state}' -o table
SUB=$(az account show --query id -o tsv)
sku=$(az rest --method get --url "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.Compute/skus?api-version=2021-07-01&\$filter=location%20eq%20%27$REGION%27" -o json)
python3 -c 'import json,sys; r=[r for r in json.load(sys.stdin)["value"] if r["name"]==sys.argv[1]]; assert r, "VM SKU absent from region"; blocked=[x for x in r[0].get("restrictions",[]) if x["type"]=="Location" or sys.argv[2] in x.get("restrictionInfo",{}).get("zones",[])]; assert not blocked, "VM size/zone unavailable for this subscription. Choose a permitted REGION/VM_SIZE/ZONE; no resources were created."' "$VM_SIZE" "$ZONE" <<< "$sku"
ZONE_ARGS=(); [[ -z "$ZONE" ]] || ZONE_ARGS=(--zone "$ZONE")
if [[ "${APPROVED_CREATE:-0}" != 1 ]]; then
  echo 'Review APPROVAL-PLAN.md and current Azure prices. Set APPROVED_CREATE=1 only after approval.'; exit 2
fi
az provider register --namespace Microsoft.Compute --wait -o none
az provider register --namespace Microsoft.Network --wait -o none
az provider register --namespace Microsoft.Storage --wait -o none
az group create -n "$RG" -l "$REGION" -o none
az network vnet create -g "$RG" -n drivecast-vnet --address-prefix 10.73.0.0/16 --subnet-name app --subnet-prefix 10.73.1.0/24 -o none
az network nsg create -g "$RG" -n drivecast-nsg -l "$REGION" -o none
az network nsg rule create -g "$RG" --nsg-name drivecast-nsg -n Web --priority 100 --access Allow --protocol Tcp --direction Inbound --source-address-prefixes Internet --destination-port-ranges 80 443 -o none
if [[ -n "$ADMIN_CIDR" ]]; then
  python3 -c 'import ipaddress,sys; n=ipaddress.ip_network(sys.argv[1]); assert n.prefixlen>=32 and n.version==4, "SSH requires one operator IPv4 /32"' "$ADMIN_CIDR"
  az network nsg rule create -g "$RG" --nsg-name drivecast-nsg -n OperatorSSH --priority 110 --access Allow --protocol Tcp --direction Inbound --source-address-prefixes "$ADMIN_CIDR" --destination-port-ranges 22 -o none
fi
az network public-ip create -g "$RG" -n drivecast-ip --sku Standard --allocation-method Static "${ZONE_ARGS[@]}" --dns-name "$DNS_LABEL" -l "$REGION" -o none
az network nic create -g "$RG" -n drivecast-nic --vnet-name drivecast-vnet --subnet app --network-security-group drivecast-nsg --public-ip-address drivecast-ip -o none
az vm create -g "$RG" -n "$VM" -l "$REGION" --nics drivecast-nic --size "$VM_SIZE" "${ZONE_ARGS[@]}" --image Ubuntu2404 --os-disk-size-gb 64 --storage-sku StandardSSD_LRS --security-type TrustedLaunch --enable-secure-boot true --enable-vtpm true --admin-username "$ADMIN" --authentication-type ssh --ssh-key-values "$SSH_PUBLIC_KEY" --custom-data cloud-init.yml --assign-identity -o none
az network public-ip show -g "$RG" -n drivecast-ip --query '{ip:ipAddress,host:dnsSettings.fqdn}' -o json
echo 'VM created. Web ports are open, but the application is not deployed yet. Check cloud-init status before install.'
echo 'Private Blob storage + VM identity scope is a separate explicit access-grant step; see README.md.'
