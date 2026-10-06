"""Things that only break after you deploy."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_review_templates_are_declared_as_package_data() -> None:
    """They are loaded from disk at request time, so if they are not in the
    wheel the container starts fine and 500s the first time somebody opens the
    review queue."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package_data = config["tool"]["setuptools"]["package-data"]
    assert "review/templates/*.html" in package_data["recon"]


def test_every_template_the_code_renders_actually_exists() -> None:
    review = ROOT / "src" / "recon" / "review"
    for module, name in (
        ("web.py", "queue.html"),
        ("web.py", "report.html"),
        ("setup.py", "setup.html"),
    ):
        assert (review / "templates" / name).exists()
        assert name in (review / module).read_text()


def test_the_dockerfile_ships_the_corpus_and_the_model() -> None:
    """The image has to boot into a working queue with no training step."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY fixtures" in dockerfile
    assert "COPY models" in dockerfile
    assert "USER recon" in dockerfile, "do not run as root"
    assert "${PORT}" in dockerfile, "the host picks the port"


def test_the_image_can_reach_a_hosts_postgres() -> None:
    """A database URL baked into the image would win over the DATABASE_URL a
    host sets when it attaches Postgres, and the books would quietly stay in a
    file the next redeploy deletes."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert '".[postgres]"' in dockerfile
    assert "RECON_DATABASE_URL" not in dockerfile
    assert "RECON_OFFLINE=1" not in dockerfile, "real payments must be verified"
    assert "--proxy-headers" in dockerfile, "the webhook address must come out as https"


def test_the_env_example_exists_and_carries_no_real_key() -> None:
    """`sk_live_` may appear in a comment explaining why it is refused; it may
    never appear as the value of anything."""
    example = (ROOT / ".env.example").read_text()
    assert "sk_test_" in example
    assignments = [
        line for line in example.splitlines() if "=" in line and not line.strip().startswith("#")
    ]
    assert any(line.startswith("PAYSTACK_SECRET_KEY=sk_test_") for line in assignments)
    assert not any("sk_live_" in line for line in assignments)
    assert ".env" in (ROOT / ".gitignore").read_text()


def test_the_commit_msg_hook_is_versioned_and_installed_by_make() -> None:
    """git does not version .git/hooks, so a fresh clone would silently lose it."""
    hook = ROOT / "scripts" / "hooks" / "commit-msg"
    assert hook.exists()
    assert hook.stat().st_mode & 0o111, "the hook has to be executable"
    assert "Co-" in hook.read_text(), "it has to actually strip trailers"
    assert "scripts/hooks/commit-msg .git/hooks/commit-msg" in (ROOT / "Makefile").read_text()
