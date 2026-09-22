#!/usr/bin/env bash
#
# One-time deployment setup for Google Cloud. Idempotent: safe to re-run.
#
# Creates the secrets the service reads at runtime, and the Workload Identity
# Federation trust that lets this repository's GitHub Actions deploy without a
# long-lived service account key.
#
# Usage:
#   export NEON_HOST=ep-xxx.region.aws.neon.tech
#   export NEON_OWNER_PASSWORD=...      # neondb_owner, used only by migrations
#   export NEON_APP_PASSWORD=...        # clinic_app, used by the running service
#   ./scripts/setup_gcp_deploy.sh
#
set -euo pipefail

PROJECT="${GCP_PROJECT:-clinic-chatbot-darren}"
REGION="${GCP_REGION:-asia-east1}"
REPO="${GITHUB_REPO:-darren890311/clinic-chat-bot}"
DEPLOYER="github-deployer"
RUNTIME="clinic-runtime"
POOL="github"
PROVIDER="github-oidc"

: "${NEON_HOST:?set NEON_HOST}"
: "${NEON_OWNER_PASSWORD:?set NEON_OWNER_PASSWORD}"
: "${NEON_APP_PASSWORD:?set NEON_APP_PASSWORD}"

gcloud config set project "$PROJECT" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

say() { printf '\n== %s\n' "$1"; }

# --- secrets ---------------------------------------------------------------
# Values live here, never in the repository and never in a Cloud Run env var,
# where `gcloud run services describe` and the deployment logs would show them.

put_secret() { # name value
    if gcloud secrets describe "$1" >/dev/null 2>&1; then
        printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- >/dev/null
        echo "  updated $1"
    else
        printf '%s' "$2" | gcloud secrets create "$1" --replication-policy=automatic --data-file=- >/dev/null
        echo "  created $1"
    fi
}

say "Secrets"
# The service connects as clinic_app, which has neither SUPERUSER nor BYPASSRLS,
# so the row level security policies actually apply to it. Neon's own
# neondb_owner carries BYPASSRLS and must never be the runtime credential.
put_secret database-url \
    "postgresql+asyncpg://clinic_app:${NEON_APP_PASSWORD}@${NEON_HOST}/neondb?ssl=require"
# Migrations run as the owner, as a separate Cloud Run job, never as the service.
put_secret migration-database-url \
    "postgresql+psycopg://neondb_owner:${NEON_OWNER_PASSWORD}@${NEON_HOST}/neondb?sslmode=require"
put_secret token-encryption-key \
    "${TOKEN_ENCRYPTION_KEY:-$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')}"
# Placeholders so the first deploy succeeds; fill them before the agent ships.
put_secret anthropic-api-key "${ANTHROPIC_API_KEY:-placeholder}"
put_secret openai-api-key "${OPENAI_API_KEY:-placeholder}"
put_secret google-client-secret "${GOOGLE_CLIENT_SECRET:-placeholder}"
put_secret microsoft-client-secret "${MICROSOFT_CLIENT_SECRET:-placeholder}"

# --- service accounts ------------------------------------------------------
# Two identities with different jobs. The deployer can ship code but cannot read
# secrets; the runtime can read secrets but cannot deploy.

say "Service accounts"
make_sa() { # id description
    gcloud iam service-accounts describe "$1@${PROJECT}.iam.gserviceaccount.com" >/dev/null 2>&1 ||
        gcloud iam service-accounts create "$1" --display-name="$2" >/dev/null
    echo "  $1@${PROJECT}.iam.gserviceaccount.com"
}
make_sa "$DEPLOYER" "GitHub Actions deployer"
make_sa "$RUNTIME" "Cloud Run runtime identity"

DEPLOYER_SA="${DEPLOYER}@${PROJECT}.iam.gserviceaccount.com"
RUNTIME_SA="${RUNTIME}@${PROJECT}.iam.gserviceaccount.com"

say "Roles"
for role in roles/run.admin roles/artifactregistry.writer roles/iam.serviceAccountUser; do
    gcloud projects add-iam-policy-binding "$PROJECT" \
        --member="serviceAccount:${DEPLOYER_SA}" --role="$role" --condition=None >/dev/null
    echo "  deployer  $role"
done

for secret in database-url migration-database-url token-encryption-key \
    anthropic-api-key openai-api-key google-client-secret microsoft-client-secret; do
    gcloud secrets add-iam-policy-binding "$secret" \
        --member="serviceAccount:${RUNTIME_SA}" \
        --role=roles/secretmanager.secretAccessor >/dev/null
done
echo "  runtime   secretAccessor on 7 secrets"

# --- workload identity federation ------------------------------------------
# GitHub Actions presents an OIDC token; GCP exchanges it for a credential that
# expires in under an hour. No JSON key exists, so none can leak.

say "Workload Identity Federation"
gcloud iam workload-identity-pools describe "$POOL" --location=global >/dev/null 2>&1 ||
    gcloud iam workload-identity-pools create "$POOL" \
        --location=global --display-name="GitHub Actions" >/dev/null
echo "  pool: $POOL"

# The attribute-condition is the security boundary. Without it, a workflow in
# ANY GitHub repository could present a valid token and assume this account.
gcloud iam workload-identity-pools providers describe "$PROVIDER" \
    --location=global --workload-identity-pool="$POOL" >/dev/null 2>&1 ||
    gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
        --location=global --workload-identity-pool="$POOL" \
        --display-name="GitHub OIDC" \
        --issuer-uri="https://token.actions.githubusercontent.com" \
        --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
        --attribute-condition="assertion.repository == '${REPO}'" >/dev/null
echo "  provider: $PROVIDER  (restricted to ${REPO})"

gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" \
    --role=roles/iam.workloadIdentityUser \
    --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}" >/dev/null
echo "  ${REPO} may impersonate ${DEPLOYER}"

# --- output ----------------------------------------------------------------

cat <<SUMMARY

== Add these to GitHub -> Settings -> Secrets and variables -> Actions

GCP_PROJECT       ${PROJECT}
GCP_DEPLOY_SA     ${DEPLOYER_SA}
GCP_WIF_PROVIDER  projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}

== Cloud Run must run as the runtime identity, not the default account
   deploy.yml passes --service-account ${RUNTIME_SA}

== Still placeholders, fill before the agent ships
   anthropic-api-key, openai-api-key, google-client-secret, microsoft-client-secret
SUMMARY
