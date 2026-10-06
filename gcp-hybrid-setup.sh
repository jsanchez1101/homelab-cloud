#!/usr/bin/env bash
# Hybrid homelab: one free-tier GCP VM joined to the existing Tailscale tailnet.
# No public SSH. Admin access is through IAP for bootstrap, then Tailscale only.
#
# Usage:
#   PROJECT_ID=jason-homelab-cloud BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX ./gcp-hybrid-setup.sh
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${BILLING_ACCOUNT:?set BILLING_ACCOUNT (gcloud billing accounts list)}"
REGION="${REGION:-us-east1}"          # free-tier eligible, closest to NJ
ZONE="${ZONE:-us-east1-b}"
NETWORK="homelab-vpc"
SUBNET="homelab-cloud"
# Must not overlap the home LAN or the lab. The first build used 10.50.0.0/24,
# which collided with R2's router ID (10.50.0.2); see "Lessons" in the README.
SUBNET_RANGE="${SUBNET_RANGE:-10.60.0.0/24}"
VM="cloud-node1"

echo "== 1. Project, billing, budget alert"
gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1 || gcloud projects create "$PROJECT_ID"
gcloud config set project "$PROJECT_ID"
gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT"
gcloud services enable compute.googleapis.com iap.googleapis.com billingbudgets.googleapis.com
gcloud billing budgets create \
  --billing-account="$BILLING_ACCOUNT" \
  --display-name="homelab-cloud \$5 cap alert" \
  --budget-amount=5USD \
  --filter-projects="projects/$PROJECT_ID" \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
  || echo "   (budget may already exist, skipping)"

echo "== 2. Custom VPC and firewall (custom VPCs deny all ingress by default)"
gcloud compute networks describe "$NETWORK" >/dev/null 2>&1 || \
  gcloud compute networks create "$NETWORK" --subnet-mode=custom
gcloud compute networks subnets describe "$SUBNET" --region="$REGION" >/dev/null 2>&1 || \
  gcloud compute networks subnets create "$SUBNET" \
    --network="$NETWORK" --region="$REGION" --range="$SUBNET_RANGE"

# SSH only from Google's IAP proxy range, never from the internet.
gcloud compute firewall-rules describe "$NETWORK-allow-iap-ssh" >/dev/null 2>&1 || \
  gcloud compute firewall-rules create "$NETWORK-allow-iap-ssh" \
    --network="$NETWORK" --direction=INGRESS --action=ALLOW \
    --rules=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=homelab
# Tailscale's WireGuard port, so peers can connect directly instead of via DERP relays.
gcloud compute firewall-rules describe "$NETWORK-allow-tailscale" >/dev/null 2>&1 || \
  gcloud compute firewall-rules create "$NETWORK-allow-tailscale" \
    --network="$NETWORK" --direction=INGRESS --action=ALLOW \
    --rules=udp:41641 --source-ranges=0.0.0.0/0 --target-tags=homelab

echo "== 3. Free-tier VM (e2-micro, 30 GB standard disk), Tailscale preinstalled"
gcloud compute instances describe "$VM" --zone="$ZONE" >/dev/null 2>&1 || \
  gcloud compute instances create "$VM" \
    --zone="$ZONE" --machine-type=e2-micro \
    --subnet="$SUBNET" --tags=homelab \
    --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
    --boot-disk-size=30GB --boot-disk-type=pd-standard \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --metadata=startup-script='#!/bin/bash
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh'

cat <<EOF

== 4. Join the tailnet (you do this part, it needs your Tailscale login)
  gcloud compute ssh $VM --zone=$ZONE --tunnel-through-iap
  sudo tailscale up --ssh --hostname=$VM
  # open the printed URL and approve the machine in your Tailscale admin console

Then verify from a mini PC:
  tailscale ping $VM
  ssh $VM
EOF
