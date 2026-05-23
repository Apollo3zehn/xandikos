# Xandikos
# Copyright (C) 2026 Jelmer Vernooij <jelmer@jelmer.uk>, et al.
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

"""Tests for xandikos.imip_listen (LMTP listener for inbound iMIP)."""

import asyncio
import importlib.util
import shutil
import sys
import tempfile
import unittest
from email.message import EmailMessage
from types import SimpleNamespace

from xandikos import imip_listen
from xandikos.web import SingleUserFilesystemBackend

_HAS_AIOSMTPD = importlib.util.find_spec("aiosmtpd") is not None


REQUEST = b"""\
BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Test//EN\r
METHOD:REQUEST\r
BEGIN:VEVENT\r
UID:imip-listen@example.com\r
DTSTAMP:20260101T000000Z\r
DTSTART:20260601T100000Z\r
DTEND:20260601T110000Z\r
SUMMARY:Listener invite\r
ORGANIZER:mailto:alice@example.com\r
ATTENDEE:mailto:bob@example.com\r
END:VEVENT\r
END:VCALENDAR\r
"""


def _imip_bytes(*, auto_submitted: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "Bob <bob@example.com>"
    msg["Message-ID"] = "<test@example.com>"
    if auto_submitted is not None:
        msg["Auto-Submitted"] = auto_submitted
    msg.set_content(
        REQUEST.decode("utf-8"),
        subtype="calendar",
        charset="utf-8",
        params={"method": "REQUEST"},
    )
    return msg.as_bytes()


def _envelope(content: bytes, rcpts=("bob@example.com",)) -> SimpleNamespace:
    # aiosmtpd's Envelope is a plain attribute bag; SimpleNamespace is enough
    # for handle_DATA which only reads .rcpt_tos and .content.
    return SimpleNamespace(rcpt_tos=list(rcpts), content=content)


class ParseListenTargetTests(unittest.TestCase):
    def test_unix_prefix(self):
        self.assertEqual(
            "/run/xandikos/imip.sock",
            imip_listen.parse_listen_target("unix:/run/xandikos/imip.sock"),
        )

    def test_bare_path(self):
        self.assertEqual("/tmp/x.sock", imip_listen.parse_listen_target("/tmp/x.sock"))

    def test_host_port(self):
        self.assertEqual(
            ("localhost", 2525), imip_listen.parse_listen_target("localhost:2525")
        )

    def test_bare_port_defaults_localhost(self):
        self.assertEqual(("localhost", 2525), imip_listen.parse_listen_target(":2525"))

    def test_missing_port(self):
        with self.assertRaises(imip_listen.IMIPListenConfigError):
            imip_listen.parse_listen_target("nope")

    def test_non_integer_port(self):
        with self.assertRaises(imip_listen.IMIPListenConfigError):
            imip_listen.parse_listen_target("host:abc")


class HandleDataTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)
        self.backend = SingleUserFilesystemBackend(self.test_dir)
        self.backend._mark_as_principal("/user/")
        self.backend.create_principal("/user/", create_defaults=True)
        self.handler = imip_listen.IMIPLMTPHandler(self.backend, "/user/")

    def _handle(self, envelope) -> str:
        return asyncio.run(self.handler.handle_DATA(None, None, envelope))

    def test_stores_valid_request(self):
        status = self._handle(_envelope(_imip_bytes()))
        self.assertEqual("250 2.0.0 Message stored", status)
        inbox = self.backend.get_resource("/user/inbox")
        self.assertEqual(1, len(list(inbox.members())))

    def test_auto_submitted_is_accepted_and_discarded(self):
        status = self._handle(_envelope(_imip_bytes(auto_submitted="auto-generated")))
        self.assertEqual("250 2.0.0 Auto-submitted message discarded", status)
        inbox = self.backend.get_resource("/user/inbox")
        self.assertEqual(0, len(list(inbox.members())))

    def test_unparseable_message_is_rejected(self):
        with self.assertLogs("xandikos.imip_listen", level="ERROR"):
            status = self._handle(_envelope(b"not an email"))
        self.assertEqual("550 5.6.0 No usable iMIP payload", status)

    def test_message_without_calendar_part_is_rejected(self):
        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg.set_content("hello")
        with self.assertLogs("xandikos.imip_listen", level="ERROR"):
            status = self._handle(_envelope(msg.as_bytes()))
        self.assertEqual("550 5.6.0 No usable iMIP payload", status)

    def test_principal_not_a_principal_returns_temp_failure(self):
        handler = imip_listen.IMIPLMTPHandler(self.backend, "/nonexistent/")
        with self.assertLogs("xandikos.imip_listen", level="ERROR"):
            status = asyncio.run(
                handler.handle_DATA(None, None, _envelope(_imip_bytes()))
            )
        self.assertEqual("451 4.3.0 Failed to store iTIP message", status)


class SocketModeTests(unittest.TestCase):
    def test_invalid_socket_mode(self):
        with self.assertRaises(imip_listen.IMIPListenConfigError):
            imip_listen._parse_socket_mode("not-octal")

    def test_invalid_socket_mode_with_eight_or_nine(self):
        with self.assertRaises(imip_listen.IMIPListenConfigError):
            imip_listen._parse_socket_mode("680")

    def test_valid_socket_mode(self):
        self.assertEqual(0o660, imip_listen._parse_socket_mode("660"))


@unittest.skipIf(sys.platform == "win32", "Unix sockets are not supported on Windows")
@unittest.skipUnless(_HAS_AIOSMTPD, "aiosmtpd is not installed")
class LMTPEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Drive the listener over a real Unix socket using smtplib.LMTP."""

    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)
        self.socket_path = f"{self.test_dir}/imip.sock"
        self.backend = SingleUserFilesystemBackend(self.test_dir)
        self.backend._mark_as_principal("/user/")
        self.backend.create_principal("/user/", create_defaults=True)
        handler = imip_listen.IMIPLMTPHandler(self.backend, "/user/")
        self.listener = await imip_listen.start_listener(self.socket_path, handler)

    async def asyncTearDown(self):
        await self.listener.stop()

    async def test_lmtp_delivery_round_trip(self):
        import smtplib

        def deliver():
            client = smtplib.LMTP(self.socket_path)
            try:
                client.sendmail("alice@example.com", ["bob@example.com"], _imip_bytes())
            finally:
                client.quit()

        await asyncio.get_running_loop().run_in_executor(None, deliver)
        inbox = self.backend.get_resource("/user/inbox")
        self.assertEqual(1, len(list(inbox.members())))


if __name__ == "__main__":
    unittest.main()
