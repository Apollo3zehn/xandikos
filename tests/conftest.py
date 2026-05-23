"""General-purpose fixtures for xandikos's testsuite."""

from __future__ import annotations

import atexit
import logging
import os
import tempfile

import pytest


def _isolate_git_config() -> None:
    """Prevent the developer's gitconfig from leaking into tests.

    The git-backed stores call ``dulwich.porcelain.get_user_identity``, which
    reads ``~/.gitconfig``, XDG config, and ``/etc/gitconfig``. Without
    isolation, commit authors, hooks, and unusual config options from the host
    environment can change test behavior.
    """
    fd, path = tempfile.mkstemp(prefix="xandikos-test-gitconfig-")
    with os.fdopen(fd, "w") as f:
        f.write("[user]\n\tname = Xandikos Test\n\temail = test@xandikos.invalid\n")
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))

    os.environ["GIT_CONFIG_GLOBAL"] = path
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_AUTHOR_NAME"] = "Xandikos Test"
    os.environ["GIT_AUTHOR_EMAIL"] = "test@xandikos.invalid"
    os.environ["GIT_COMMITTER_NAME"] = "Xandikos Test"
    os.environ["GIT_COMMITTER_EMAIL"] = "test@xandikos.invalid"


_isolate_git_config()


@pytest.fixture(autouse=True)
def setup_logging():
    """Configure logging for tests, hiding DEBUG output from dulwich."""
    # Hide DEBUG logging from dulwich to reduce noise in test output
    logging.getLogger("dulwich").setLevel(logging.WARNING)
