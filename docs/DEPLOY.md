# Running Reckon

Reckon runs in one of two ways:

- **On your own books** (the default). Pages read from the database and need a
  login. They stay shut until there is an admin (`RECON_ADMIN_EMAIL` and
  `RECON_PASSWORD`). Empty until you bring data in on the **Your data** page
  (`/setup`).
- **On the practice data** (`RECON_DEMO=1`). A generated month of payments,
  open to anyone, nothing to set up.

## On your machine

```bash
make install          # dependencies and the pre-commit hooks
cp .env.example .env  # your sk_test_ key, RECON_ADMIN_EMAIL and RECON_PASSWORD
make corpus           # build the month of fake payments
make train            # fit the matcher, writes models/
make eval             # every number in the README
make demo             # the practice data, http://localhost:8000/review
make serve            # your own books, http://localhost:8000/setup
```

`make eval` takes about ten seconds. It refits the model from scratch every
time, on purpose: a number you cannot regenerate is a number you cannot trust.

## In a container

```bash
docker build -t reckon .

# your own books
docker run -p 8080:8080 -e PAYSTACK_SECRET_KEY=sk_test_xxx \
  -e RECON_ADMIN_EMAIL=you@example.com -e RECON_PASSWORD=at-least-ten-chars \
  -e DATABASE_URL=postgresql://user:pass@host:5432/reckon reckon

# the practice data
docker run -p 8080:8080 -e RECON_DEMO=1 -e RECON_OFFLINE=1 reckon
```

The image carries the fitted model and the practice corpus, so it boots with no
training step. Without a database URL it keeps your books in a SQLite file
inside the container, which is gone when the container is; the setup page says
so in red until you fix it.

## On Railway

This is the shortest way to run it on your own books.

1. **New project → Deploy from GitHub repo**, and pick this repository. Railway
   finds the `Dockerfile` and builds it.
2. **Add a database.** In the project, add a PostgreSQL database.
3. **Set the service's variables** (the Reckon service, not the database):

   | Variable | Value |
   | --- | --- |
   | `PAYSTACK_SECRET_KEY` | your `sk_test_` key, from Paystack's Settings → API Keys & Webhooks |
   | `RECON_ADMIN_EMAIL` | your email; this account is the admin |
   | `RECON_PASSWORD` | your admin password the first time, at least 10 characters |
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}`, a reference to the database from step 2 |

   If Railway offered the variables from `.env.example` and you accepted them,
   delete `RECON_DATABASE_URL` and `RECON_OFFLINE`. The first would keep your
   books in a file the next deploy wipes, and the second would skip checking
   payments with Paystack.
4. **Generate a domain** under the service's Networking settings, open it, and
   log in with your email and that password. Change the password straight away
   from your name in the top bar: after the first start, `RECON_PASSWORD` is
   never read again. Then go to **Your data**. Copy the webhook address it shows into Paystack
   (next section), upload your invoices, and pull your past payments.

Every push to the branch Railway watches redeploys. With Postgres attached, your
books and every decision survive it.

## On Cloud Run

One command. As written it deploys the practice data, open to anyone. To run it
on your own books, set `_DEMO` to `0` in `cloudbuild.yaml`, create secrets for
`RECON_ADMIN_EMAIL`, `RECON_PASSWORD` and `DATABASE_URL` the same way as the
Paystack key below, and
add both to the `--set-secrets` line of the deploy step.

```bash
gcloud config set project your-project
printf %s 'sk_test_xxx' | gcloud secrets create paystack-test-key --data-file=-

make deploy
```

`make deploy` runs `scripts/deploy.sh`, which builds the corpus and fits the
matcher if they are missing, submits `cloudbuild.yaml`, and prints the URL.

The build has three steps and the middle one matters: it boots the image it just
built, opens `/review`, `/report` and `/api/report` against it, and checks the
day's report actually balances. Only then does it deploy. A container can start
cleanly and still 500 on the review queue — that is exactly what happened when
the HTML templates were left out of the wheel — so a health endpoint is not
enough of a gate.

`scripts/smoke.py` is the same check, and you can point it at anything. On your
own books, give it the admin login:

```bash
RECON_ADMIN_EMAIL=you@example.com RECON_PASSWORD=your-password \
  python3 scripts/smoke.py https://reckon-xxxx.a.run.app
```

The key goes in Secret Manager, never in `--set-env-vars`. Environment variables
set that way are readable by anyone who can describe the service.

Overrides, if the defaults do not suit:

```bash
PROJECT=other-project REGION=us-central1 SERVICE=reckon-staging make deploy
```

### Pointing Paystack at it

The webhook URL is `https://<your-service>/webhooks/paystack`, and the **Your
data** page shows it ready to copy. Set it on the Paystack dashboard under
Settings → API Keys & Webhooks, in the **test** URL field. The same page counts
deliveries refused for a bad signature, which is how a key from the wrong
dashboard shows up.

The endpoint answers 200 immediately and does the work afterwards, which
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

## People

Anyone can open `/signup` and make an account. It waits, and can do nothing,
until an admin lets it in from **People**. Admins can also remove someone (their
logins end on their next click; their name stays on what they already did),
make someone else an admin, or step down once there is another admin. The app
refuses any change that would leave no active admin, and on every start it
makes the `RECON_ADMIN_EMAIL` account an active admin again, so the person
running the deployment cannot be locked out of it.

**Activity** is the audit log, newest first: sign-ups, approvals, uploads,
pulls from Paystack and every decision, with who did it. Nothing on that page,
or anywhere else, can change or delete an entry.

## Notes on state

Your books (invoices, payments, decisions, the audit log) live in the database.
The matches do not: they are rebuilt in memory, in one process, the next time a
page is opened after anything changes. That is under a second for a month of a
small shop's payments. It is one process and not a cluster, so run one copy.

`DATABASE_URL` and `RECON_DATABASE_URL` are both read, ours first. A
`postgres://` address, the form hosts hand out, is used as it is.

Settlement batches are not pulled from Paystack, so on your own books the
"landed in the bank today" half of the report stays empty. That is in the
README's limitations.

The Postgres paths have their own tests, skipped unless a database is given:

```bash
RECON_TEST_POSTGRES_URL=postgresql://user@localhost:5432/reckon_test pytest tests/test_postgres.py
```
