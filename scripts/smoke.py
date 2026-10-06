"""Prove a running instance actually works, from outside it.

Used three times: by Cloud Build against the freshly built image before it is
allowed to deploy, by `scripts/deploy.sh` against the deployed URL, and by hand
whenever you want to know whether the thing is up.

It checks more than a health endpoint. A container can boot cleanly and still
500 on the review queue — that is exactly what happened when the HTML templates
were left out of the wheel — so this opens the real pages and checks the day's
report actually balances.

An instance on its own books has its pages behind a login. Put the admin's
email and password in this shell (RECON_ADMIN_EMAIL, RECON_PASSWORD) and the
check logs in with them first.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PAGES = ("/health", "/review", "/report", "/setup", "/api/queue", "/api/report")

#: Keeps the login cookie between requests, the way a browser would.
_browser = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)

LOCKED = (
    "is locked. Set RECON_ADMIN_EMAIL and RECON_PASSWORD on the service, and the "
    "same two in this shell so the smoke test can log in."
)


def fetch(base: str, path: str, timeout: float = 15.0) -> tuple[int, bytes]:
    try:
        with _browser.open(f"{base}{path}", timeout=timeout) as response:
            # A page that needs a login answers with the login box, which is a
            # 200. Landing there means the page itself was never seen.
            if path != "/login" and urllib.parse.urlparse(response.geturl()).path == "/login":
                raise SystemExit(f"{path} {LOCKED}")
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 503):
            raise SystemExit(f"{path} ({exc.code}) {LOCKED}") from exc
        raise


def log_in(base: str) -> bool:
    email = os.environ.get("RECON_ADMIN_EMAIL")
    password = os.environ.get("RECON_PASSWORD")
    if not (email and password):
        return False
    form = urllib.parse.urlencode({"email": email, "password": password}).encode()
    try:
        with _browser.open(f"{base}/login", data=form, timeout=15) as response:
            if urllib.parse.urlparse(response.geturl()).path == "/login":
                raise SystemExit("logging in failed: check RECON_ADMIN_EMAIL and RECON_PASSWORD")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"logging in failed ({exc.code}): check the email and password") from exc
    return True


def wait_for(base: str, seconds: int = 60) -> None:
    deadline = time.time() + seconds
    last: Exception | None = None
    while time.time() < deadline:
        try:
            fetch(base, "/health", timeout=3)
            return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            time.sleep(2)
    raise SystemExit(f"{base} never came up: {last}")


def main(argv: list[str]) -> int:
    base = (argv[1] if len(argv) > 1 else "http://localhost:8080").rstrip("/")
    wait_for(base)
    _, raw = fetch(base, "/health")
    demo = json.loads(raw).get("books") == "demo"
    if log_in(base):
        print("  logged in")

    for path in PAGES:
        status, body = fetch(base, path)
        if status != 200:
            raise SystemExit(f"{path} returned {status}")
        if not body:
            raise SystemExit(f"{path} returned an empty body")
        print(f"  {path:14} {status}  {len(body):>7} bytes")

    _, raw = fetch(base, "/api/report")
    report = json.loads(raw)
    if not report.get("balances"):
        raise SystemExit(f"the day does not balance: {report.get('problems')}")

    _, raw = fetch(base, "/api/queue")
    queue = json.loads(raw)
    if demo and queue.get("waiting", 0) <= 0:
        # Only the demo is guaranteed a queue. Your own books may well be empty.
        raise SystemExit("the review queue is empty, so the corpus did not ship with the image")

    print(f"  report balances, {queue['waiting']} cases waiting, {queue['money_at_risk']} at risk")
    print(f"ok: {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
