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

"""Tests for the htpasswd authentication module."""

import base64
import hashlib
import os
import tempfile
import time
import unittest

from xandikos.htpasswd import HtpasswdError, HtpasswdFile


def _sha1_entry(password: str) -> str:
    digest = base64.b64encode(hashlib.sha1(password.encode("utf-8")).digest()).decode(
        "ascii"
    )
    return "{SHA}" + digest


class HtpasswdFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".htpasswd", delete=False
        )
        self.addCleanup(os.unlink, self.tmp.name)

    def _write(self, content: str) -> None:
        self.tmp.write(content)
        self.tmp.flush()
        self.tmp.close()

    def test_sha1_verify(self):
        self._write(f"alice:{_sha1_entry('hunter2')}\n")
        f = HtpasswdFile(self.tmp.name)
        self.assertTrue(f.check("alice", "hunter2"))
        self.assertFalse(f.check("alice", "wrong"))
        self.assertFalse(f.check("unknown", "hunter2"))

    def test_bcrypt_verify(self):
        try:
            import bcrypt
        except ImportError:
            self.skipTest("bcrypt not installed")
        hashed = bcrypt.hashpw(b"swordfish", bcrypt.gensalt(rounds=4)).decode("ascii")
        self._write(f"bob:{hashed}\n")
        f = HtpasswdFile(self.tmp.name)
        self.assertTrue(f.check("bob", "swordfish"))
        self.assertFalse(f.check("bob", "nope"))

    def test_comments_and_blank_lines_ignored(self):
        self._write(f"# leading comment\n\ncarol:{_sha1_entry('s3cret')}\n  \n")
        f = HtpasswdFile(self.tmp.name)
        self.assertTrue(f.check("carol", "s3cret"))

    def test_missing_separator_raises(self):
        self._write("noseparator\n")
        with self.assertRaises(HtpasswdError):
            HtpasswdFile(self.tmp.name)

    def test_empty_username_raises(self):
        self._write(":somehash\n")
        with self.assertRaises(HtpasswdError):
            HtpasswdFile(self.tmp.name)

    def test_apr1_rejected(self):
        # $apr1$ entries are rejected with an actionable error.
        self._write("dave:$apr1$abc$xyz\n")
        f = HtpasswdFile(self.tmp.name)
        with self.assertRaises(HtpasswdError) as cm:
            f.check("dave", "anything")
        self.assertIn("htpasswd -B", str(cm.exception))

    def test_unknown_hash_rejected(self):
        self._write("eve:plaintextlooking\n")
        f = HtpasswdFile(self.tmp.name)
        with self.assertRaises(HtpasswdError):
            f.check("eve", "anything")

    def test_file_not_found(self):
        with self.assertRaises(HtpasswdError):
            HtpasswdFile("/nonexistent/htpasswd")

    def test_reload_on_mtime_change(self):
        self._write(f"alice:{_sha1_entry('one')}\n")
        f = HtpasswdFile(self.tmp.name)
        self.assertTrue(f.check("alice", "one"))
        # Ensure a different mtime.
        time.sleep(0.01)
        new_mtime = os.stat(self.tmp.name).st_mtime + 5
        with open(self.tmp.name, "w") as fh:
            fh.write(f"alice:{_sha1_entry('two')}\n")
        os.utime(self.tmp.name, (new_mtime, new_mtime))
        self.assertFalse(f.check("alice", "one"))
        self.assertTrue(f.check("alice", "two"))
