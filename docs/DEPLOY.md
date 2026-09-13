# Running it

## On your machine

```bash
make install          # dependencies and the pre-commit hooks
cp .env.example .env  # then put your sk_test_ key in it
make corpus           # build the month of fake payments
make train            # fit the matcher, writes models/
make eval             # every number in the README
make serve            # http://localhost:8000/review
```

`make eval` takes about ten seconds. It refits the model from scratch every
time, on purpose: a number you cannot regenerate is a number you cannot trust.

## In a container

```bash
docker build -t recon .
docker run -p 8080:8080 -e PAYSTACK_SECRET_KEY=sk_test_xxx recon
```

The image carries the corpus and the fitted model, so it boots straight into a
working review queue with no database and no training step.

## On Cloud Run

```bash
PROJECT=your-project
REGION=europe-west1

gcloud builds submit --tag gcr.io/$PROJECT/recon

gcloud run deploy recon \
  --image gcr.io/$PROJECT/recon \
  --region $REGION \
  --allow-unauthenticated \
  --set-env-vars RECON_OFFLINE=0 \
  --set-secrets PAYSTACK_SECRET_KEY=paystack-test-key:latest
```

Put the key in Secret Manager, not in `--set-env-vars`. Environment variables
set that way are visible to anyone who can describe the service.

### Pointing Paystack at it

The webhook URL is `https://<your-service>/webhooks/paystack`, set on the
Paystack dashboard under Settings → API Keys & Webhooks. Use the **test** URL
field. The endpoint answers 200 immediately and does the work afterwards, which
matters: Paystack retries anything slow roughly every three minutes for four
attempts and then hourly for seventy-two hours, and a slow handler turns one
event into dozens.

### What to watch once it is up

* `GET /health` — for the load balancer.
* `GET /api/report` — the day's numbers as JSON. If `balances` is ever `false`,
  something is wrong that the report could not explain, and the `problems` list
  says what.
* `GET /api/queue` — what is waiting for a person.
* The `webhook_events` table — any row with `signature_ok = false` is somebody
  POSTing at your endpoint who does not have your secret key.

## Notes on state

The demo holds the corpus and the matches in memory in one process. That is
fine for one shop and one bookkeeper, and it is not a cluster. For anything
bigger, point `RECON_DATABASE_URL` at Postgres and read the queue and the
report from there; the pages take plain data and do not care where it came
from.

Settlement batches are read from the corpus. Against a live account you would
pull them from Paystack's settlement endpoints on a schedule, which is not
built yet and is listed in the README's limitations.
