# Xandikos
# Copyright (C) 2025-2026 Jelmer Vernooĳ <jelmer@jelmer.uk>, et al.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 3
# of the License or (at your option) any later version of
# the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.

"""Shared fixtures for Xandikos benchmarks.

These benchmarks are designed to run against different xandikos versions
(v0.3.0, v0.3.4, v0.3.5, HEAD) to detect performance regressions.

Usage:
    # Run benchmarks on current checkout:
    pytest benchmarks/ --benchmark-enable

    # Save results for later comparison:
    pytest benchmarks/ --benchmark-enable --benchmark-save=v0.3.5

    # Compare saved results:
    pytest-benchmark compare v0.3.4 v0.3.5 master --sort=fullname
"""

import atexit
import inspect
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone


def _isolate_git_config() -> None:
    """Prevent the developer's gitconfig from leaking into benchmarks.

    Mirrors the isolation done in ``tests/conftest.py`` so benchmark runs
    are not affected by the host's ``~/.gitconfig`` or ``/etc/gitconfig``.
    """
    fd, path = tempfile.mkstemp(prefix="xandikos-bench-gitconfig-")
    with os.fdopen(fd, "w") as f:
        f.write("[user]\n\tname = Xandikos Bench\n\temail = bench@xandikos.invalid\n")
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))

    os.environ["GIT_CONFIG_GLOBAL"] = path
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_AUTHOR_NAME"] = "Xandikos Bench"
    os.environ["GIT_AUTHOR_EMAIL"] = "bench@xandikos.invalid"
    os.environ["GIT_COMMITTER_NAME"] = "Xandikos Bench"
    os.environ["GIT_COMMITTER_EMAIL"] = "bench@xandikos.invalid"


_isolate_git_config()

import pytest  # noqa: E402

from xandikos.icalendar import ICalendarFile  # noqa: E402
from xandikos.store.git import BareGitStore, TreeGitStore  # noqa: E402
from xandikos.store.memory import MemoryStore  # noqa: E402


# Number of items in collections used for benchmarks.
SMALL_COLLECTION = 50
LARGE_COLLECTION = 500


def _make_vcalendar(i: int, base_date: datetime) -> bytes:
    """Generate a VCALENDAR with a single VEVENT."""
    event_date = base_date + timedelta(days=i)
    end_date = event_date + timedelta(hours=1)
    return (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Xandikos Bench//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        + f"UID:bench-event-{i}@example.com\r\n".encode()
        + f"DTSTART:{event_date.strftime('%Y%m%dT%H%M%SZ')}\r\n".encode()
        + f"DTEND:{end_date.strftime('%Y%m%dT%H%M%SZ')}\r\n".encode()
        + f"SUMMARY:Benchmark Event {i}\r\n".encode()
        + b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )


def _import_one(store, name, content_type, data):
    """Call import_one with the right kwargs for the current xandikos version.

    v0.3.0-v0.3.4 use ``author=``, v0.3.5+ use ``remote_user=``.
    """
    sig = inspect.signature(store.import_one)
    if "remote_user" in sig.parameters:
        return store.import_one(name, content_type, data)
    else:
        return store.import_one(name, content_type, data)


def _populate_store(store, n):
    """Add *n* calendar events to *store*, return {name: etag} map."""
    store.load_extra_file_handler(ICalendarFile)
    base_date = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    etags = {}
    for i in range(n):
        name = f"event-{i}.ics"
        (name, etag) = _import_one(
            store, name, "text/calendar", [_make_vcalendar(i, base_date)]
        )
        etags[name] = etag
    return etags


# -- BareGitStore fixtures ---------------------------------------------------


@pytest.fixture(scope="session")
def bare_store_small():
    store = BareGitStore.create_memory()
    etags = _populate_store(store, SMALL_COLLECTION)
    return store, etags


@pytest.fixture(scope="session")
def bare_store_large():
    store = BareGitStore.create_memory()
    etags = _populate_store(store, LARGE_COLLECTION)
    return store, etags


# -- TreeGitStore fixtures ----------------------------------------------------


@pytest.fixture(scope="session")
def _tree_store_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d)


@pytest.fixture(scope="session")
def tree_store_small(_tree_store_dir):
    store = TreeGitStore.create(os.path.join(_tree_store_dir, "small"))
    etags = _populate_store(store, SMALL_COLLECTION)
    return store, etags


@pytest.fixture(scope="session")
def tree_store_large(_tree_store_dir):
    store = TreeGitStore.create(os.path.join(_tree_store_dir, "large"))
    etags = _populate_store(store, LARGE_COLLECTION)
    return store, etags


# -- MemoryStore fixtures -----------------------------------------------------


@pytest.fixture(scope="session")
def memory_store_small():
    store = MemoryStore()
    etags = _populate_store(store, SMALL_COLLECTION)
    return store, etags


@pytest.fixture(scope="session")
def memory_store_large():
    store = MemoryStore()
    etags = _populate_store(store, LARGE_COLLECTION)
    return store, etags


# -- helpers available to test modules ----------------------------------------


def has_get_etag():
    """True if the installed xandikos has the direct get_etag() method."""
    return hasattr(BareGitStore, "get_etag") and not isinstance(
        BareGitStore.__dict__.get("get_etag"), property
    )


def has_get_file_meta():
    """True if the installed xandikos has get_file_meta()."""
    from xandikos.store import Store

    return hasattr(Store, "get_file_meta")
