#!/usr/bin/env bash
# Ship it. One command, from a clean checkout.
#
#   ./scripts/deploy.sh                 # uses the current gcloud project
#   PROJECT=my-project ./scripts/deploy.sh
#
# What it does, in order: checks you are logged in, makes sure the test key is
# in Secret Manager, submits the build (which builds the image, boots it, hits
# the real pages, and only then deploys), and prints the URL.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-recon}"
SECRET="${SECRET:-paystack-test-key}"

if [[ -z "$PROJECT" || "$PROJECT" == "(unset)" ]]; then
  echo "No GCP project. Set one with: gcloud config set project <id>" >&2
  exit 1
fi

echo "project: $PROJECT"
echo "region:  $REGION"
echo "service: $SERVICE"

# The corpus and the fitted model ship inside the image, so they have to exist
# before the build starts. Building them here is cheap and beats deploying a
# container whose review queue is empty.
if [[ ! -f fixtures/transactions.json || ! -f models/model.json ]]; then
  echo "building the corpus and fitting the matcher first"
  make corpus
  make train
fi

if ! gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  echo
  echo "Secret '$SECRET' does not exist yet. Create it with your test key:"
  echo "  printf %s 'sk_test_xxx' | gcloud secrets create $SECRET --data-file=- --project $PROJECT"
  echo
  echo "Never pass the key with --set-env-vars: that makes it readable to"
  echo "anyone who can describe the service."
  exit 1
fi

gcloud builds submit \
  --project "$PROJECT" \
  --config cloudbuild.yaml \
  --substitutions "_REGION=$REGION,_SERVICE=$SERVICE,_SECRET=$SECRET"

URL="$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT" --region "$REGION" \
  --format 'value(status.url)')"

echo
echo "checking the deployed instance"
python3 scripts/smoke.py "$URL"

echo
echo "live: $URL/review"
echo "webhook URL for the Paystack dashboard: $URL/webhooks/paystack"
echo
echo "Put that first URL in the README's opening paragraph."
