#!/usr/bin/env bash
# Loads the reproducible test data into the cloud database from your machine.
# Opens the PostgreSQL firewall for your current public IP only.
set -euo pipefail
source deploy/azure/.deployment.env
MYIP=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create --resource-group "$RG" --server-name "$PG" --name seed-from-laptop \
  --start-ip-address "$MYIP" --end-ip-address "$MYIP" -o none
DATABASE_URL="$DB_URL" python -m tuition_api.seed
python scripts/gateway_evidence.py "$GATEWAY_URL" > docs/evidence/gateway_evidence_cloud.md
echo "Seeded. Gateway evidence written to docs/evidence/gateway_evidence_cloud.md"
