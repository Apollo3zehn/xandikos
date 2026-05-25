# Xandikos
# Copyright (C) 2026 Jelmer Vernooĳ <jelmer@jelmer.uk>, et al.
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

"""Basic-auth credential checking against an Apache-style htpasswd file.

Supported hash formats:

* bcrypt (``$2y$``, ``$2b$``, ``$2a$``) - recommended; requires the
  ``bcrypt`` package.
* SHA1 (``{SHA}``) - legacy, NOT recommended; provided for compatibility
  with files created by older ``htpasswd`` invocations.

The ``$apr1$`` (Apache MD5) and traditional crypt formats are rejected
with an actionable error so the admin knows to regenerate the entry with
``htpasswd -B`` (bcrypt).
"""

import base64
import hashlib
import hmac
import logging
import os
import threading

logger = logging.getLogger(__name__)


class HtpasswdError(Exception):
    """Raised for unparseable lines or unsupported hash formats."""


class HtpasswdFile:
    """In-memory view of an htpasswd file with on-demand reload."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._users: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError as exc:
            raise HtpasswdError(f"htpasswd file not found: {self.path}") from exc
        users: dict[str, str] = {}
        with open(self.path, encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.rstrip("\r\n")
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if ":" not in line:
                    raise HtpasswdError(f"{self.path}:{lineno}: missing ':' separator")
                user, hashed = line.split(":", 1)
                if not user:
                    raise HtpasswdError(f"{self.path}:{lineno}: empty username")
                users[user] = hashed
        self._users = users
        self._mtime = st.st_mtime

    def _maybe_reload(self) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            try:
                self._load()
            except HtpasswdError as exc:
                logger.warning("Failed to reload %s: %s", self.path, exc)

    def check(self, username: str, password: str) -> bool:
        """Return True if the credentials match an entry in the file."""
        with self._lock:
            self._maybe_reload()
            hashed = self._users.get(username)
        if hashed is None:
            return False
        return _verify(password, hashed)


def _verify(password: str, hashed: str) -> bool:
    if hashed.startswith(("$2y$", "$2b$", "$2a$")):
        try:
            import bcrypt
        except ImportError as exc:
            raise HtpasswdError(
                "bcrypt-hashed htpasswd entries require the 'bcrypt' package. "
                "Install with: pip install bcrypt"
            ) from exc
        # Apache htpasswd writes the $2y$ prefix; bcrypt accepts both $2y$
        # and $2b$ on verify.
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
        except ValueError:
            return False
    if hashed.startswith("{SHA}"):
        digest = base64.b64encode(
            hashlib.sha1(password.encode("utf-8")).digest()
        ).decode("ascii")
        return hmac.compare_digest(digest, hashed[len("{SHA}") :])
    if hashed.startswith("$apr1$"):
        raise HtpasswdError(
            "Unsupported htpasswd hash $apr1$ (Apache MD5). "
            "Regenerate the entry with: htpasswd -B"
        )
    raise HtpasswdError(
        "Unsupported htpasswd hash format. Use bcrypt (htpasswd -B) or SHA1 ({SHA})."
    )
