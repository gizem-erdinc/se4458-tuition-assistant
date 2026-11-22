#!/usr/bin/env bash
# Azure deployment: Azure Container Apps (API, gateway, MCP server, agent)
#                 + Azure Database for PostgreSQL Flexible Server + Azure Container Registry.
# Works with an "Azure for Students" subscription. Requires: Azure CLI (az login done).
#
#   export LLM_API_KEY=sk-ant-...         # used by the agent container only
#   bash deploy/azure/deploy.sh
#
# Afterwards: bash deploy/azure/seed.sh   (loads the test data into the cloud database)
# Tear down after grading:  az group delete -n "$RG" --yes
set -euo pipefail

RG=${RG:-rg-se4458-tuition}
LOC=${LOC:-swedencentral}   # Azure for Students allows only a few regions: check
#   az policy assignment list --disable-scope-strict-match --query "[].parameters.listOfAllowedLocations.value"
SUFFIX=${SUFFIX:-$RANDOM}
ACR=${ACR:-se4458tuition$SUFFIX}            # globally unique, lowercase
PG=${PG:-se4458-tuition-pg-$SUFFIX}         # globally unique
ENV=${ENV_NAME:-se4458-tuition-env}
PG_USER=tuitionadmin
PG_PASS=${PG_PASS:-"P$(openssl rand -hex 12)!a"}
JWT_SECRET=${JWT_SECRET:-$(openssl rand -hex 32)}
GATEWAY_SECRET=${GATEWAY_SECRET:-$(openssl rand -hex 32)}
LLM_PROVIDER=${LLM_PROVIDER:-anthropic}
LLM_MODEL=${LLM_MODEL:-claude-haiku-5-5}
IMAGE=tuition:1.0

az extension add --name containerapp --upgrade -y >/dev/null
az provider register -n Microsoft.App --wait
az provider register -n Microsoft.OperationalInsights --wait

echo "== resource group, registry, image"
az group create -n "$RG" -l "$LOC" -o none
az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled true -o none
ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)
# ACR Tasks (az acr build) is not available on student subscriptions -> build locally and push
az acr login -n "$ACR"
docker build -t "$ACR_SERVER/$IMAGE" .
docker push "$ACR_SERVER/$IMAGE"
ACR_PASS=$(az acr credential show -n "$ACR" --query "passwords[0].value" -o tsv)

echo "== PostgreSQL Flexible Server (Burstable B1ms)"
az postgres flexible-server create -g "$RG" -n "$PG" -l "$LOC" \
  --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 \
  --admin-user "$PG_USER" --admin-password "$PG_PASS" \
  --public-access 0.0.0.0 --yes -o none          # 0.0.0.0 = allow Azure services only
az postgres flexible-server db create --resource-group "$RG" --server-name "$PG" --name tuition -o none
DB_URL="postgresql://$PG_USER:$PG_PASS@$PG.postgres.database.azure.com:5432/tuition?sslmode=require"

echo "== Container Apps environment"
az containerapp env create -g "$RG" -n "$ENV" -l "$LOC" -o none

common=(--resource-group "$RG" --environment "$ENV" --image "$ACR_SERVER/$IMAGE"
        --registry-server "$ACR_SERVER" --registry-username "$ACR" --registry-password "$ACR_PASS"
        --cpu 0.25 --memory 0.5Gi --min-replicas 0 --max-replicas 1 -o none)

echo "== tuition-api (only callable through the gateway: origin verification via shared secret)"
# Internal-ingress name resolution (http://tuition-api, *.internal.*) did not work in this
# environment type, so the API has HTTPS ingress but rejects every /api call that does not
# carry the X-Gateway-Key header the gateway adds -> 403 DIRECT_ACCESS_FORBIDDEN.
az containerapp create -n tuition-api "${common[@]}" --ingress external --target-port 8001 --min-replicas 1 \
  --secrets db-url="$DB_URL" jwt="$JWT_SECRET" gw-key="$GATEWAY_SECRET" \
  --env-vars SERVICE=tuition-api DATABASE_URL=secretref:db-url JWT_SECRET=secretref:jwt \
             GATEWAY_SHARED_SECRET=secretref:gw-key
API=$(az containerapp show -g "$RG" -n tuition-api --query properties.configuration.ingress.fqdn -o tsv)

echo "== gateway (public)"
az containerapp create -n gateway "${common[@]}" --ingress external --target-port 8000 \
  --secrets db-url="$DB_URL" jwt="$JWT_SECRET" gw-key="$GATEWAY_SECRET" \
  --env-vars SERVICE=gateway DATABASE_URL=secretref:db-url JWT_SECRET=secretref:jwt \
             GATEWAY_SHARED_SECRET=secretref:gw-key TUITION_API_URL="https://$API"

echo "== mcp-server (public, read-only policy search)"
az containerapp create -n mcp-server "${common[@]}" --ingress external --target-port 8765 \
  --env-vars SERVICE=mcp-server

GW=$(az containerapp show -g "$RG" -n gateway --query properties.configuration.ingress.fqdn -o tsv)
MCP=$(az containerapp show -g "$RG" -n mcp-server --query properties.configuration.ingress.fqdn -o tsv)

echo "== agent chat UI (public)"
KEY_VAR=ANTHROPIC_API_KEY; [ "$LLM_PROVIDER" = anthropic ] || KEY_VAR=OPENAI_API_KEY
az containerapp create -n agent "${common[@]}" --ingress external --target-port 8080 \
  --secrets llm-key="${LLM_API_KEY:?set LLM_API_KEY}" \
  --env-vars SERVICE=agent LLM_PROVIDER="$LLM_PROVIDER" LLM_MODEL="$LLM_MODEL" "$KEY_VAR"=secretref:llm-key \
             ${LLM_BASE_URL:+LLM_BASE_URL=$LLM_BASE_URL} \
             GATEWAY_URL="https://$GW" MCP_SERVER_URL="https://$MCP/mcp"
AGENT=$(az containerapp show -g "$RG" -n agent --query properties.configuration.ingress.fqdn -o tsv)

cat > deploy/azure/.deployment.env <<EOF
RG=$RG
PG=$PG
DB_URL=$DB_URL
GATEWAY_URL=https://$GW
MCP_URL=https://$MCP/mcp
AGENT_URL=https://$AGENT
EOF
echo
echo "Gateway : https://$GW        (Swagger: https://$GW/docs, routes: https://$GW/gateway/routes)"
echo "MCP     : https://$MCP/mcp"
echo "Chat UI : https://$AGENT"
echo "Saved to deploy/azure/.deployment.env (contains the DB password — do not commit)."
