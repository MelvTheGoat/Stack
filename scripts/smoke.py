"""Prove a running instance actually works, from outside it.

Used three times: by Cloud Build against the freshly built image before it is
allowed to deploy, by `scripts/deploy.sh` against the deployed URL, and by hand
whenever you want to know whether the thing is up.

It checks more than a health endpoint. A container can boot cleanly and still
500 on the review queue — that is exactly what happened when the HTML templates
were left out of the wheel — so this opens the real pages and checks the day's
report actually balances.

An instance on its own books has its pages behind RECON_PASSWORD. Put the same
password in this shell's environment and the check logs in with it.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

PAGES = ("/health", "/review", "/report", "/setup", "/api/queue", "/api/report")


def fetch(base: str, path: str, timeout: float = 15.0) -> tuple[int, bytes]:
    request = urllib.request.Request(f"{base}{path}")
    password = os.environ.get("RECON_PASSWORD")
    if password:
        token = base64.b64encode(f"smoke:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 503):
            raise SystemExit(
                f"{path} is locked ({exc.code}). Set RECON_PASSWORD on the service, "
                "and in this shell so the smoke test can log in."
            ) from exc
        raise


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
