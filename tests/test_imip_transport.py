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

"""Tests for xandikos.imip_transport (outbound iMIP transports)."""

import smtplib
import subprocess
import unittest
from email.message import EmailMessage
from unittest import mock

from xandikos import imip_transport


def _build_message() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "calendar@example.com"
    msg["To"] = "alice@example.org"
    msg["Subject"] = "Calendar request"
    msg.set_content("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", subtype="calendar")
    return msg


class NullTransportTests(unittest.TestCase):
    def test_send_drops_message(self) -> None:
        transport = imip_transport.NullTransport()
        transport.send(_build_message())


class CapturingTransportTests(unittest.TestCase):
    def test_send_records_messages(self) -> None:
        transport = imip_transport.CapturingTransport()
        msg1 = _build_message()
        msg2 = _build_message()
        transport.send(msg1)
        transport.send(msg2)
        self.assertEqual([msg1, msg2], transport.sent)


class SendmailTransportTests(unittest.TestCase):
    def test_send_invokes_sendmail(self) -> None:
        transport = imip_transport.SendmailTransport(binary="/sbin/sendmail")
        msg = _build_message()
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            transport.send(msg)
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(["/sbin/sendmail", "-t", "-i"], args[0])
        self.assertEqual(bytes(msg), kwargs["input"])

    def test_send_raises_when_binary_missing(self) -> None:
        transport = imip_transport.SendmailTransport(binary="/does/not/exist")
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(imip_transport.IMIPTransportError):
                transport.send(_build_message())

    def test_send_raises_on_nonzero_exit(self) -> None:
        transport = imip_transport.SendmailTransport()
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"queue full"
        )
        with mock.patch.object(subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(
                imip_transport.IMIPTransportError, "queue full"
            ):
                transport.send(_build_message())

    def test_send_raises_on_timeout(self) -> None:
        transport = imip_transport.SendmailTransport()
        with mock.patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="sendmail", timeout=1),
        ):
            with self.assertRaises(imip_transport.IMIPTransportError):
                transport.send(_build_message())


class FakeSMTP:
    """Minimal smtplib.SMTP stand-in tracking calls made against it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.quit_called = False

    def starttls(self) -> None:
        self.calls.append(("starttls", ()))

    def ehlo(self) -> None:
        self.calls.append(("ehlo", ()))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", (user, password)))

    def send_message(self, message: EmailMessage) -> None:
        self.calls.append(("send_message", (message,)))

    def quit(self) -> None:
        self.quit_called = True


class SMTPTransportTests(unittest.TestCase):
    def test_plain_send(self) -> None:
        fake = FakeSMTP()
        transport = imip_transport.SMTPTransport("smtp.example.org", 25)
        msg = _build_message()
        with mock.patch.object(smtplib, "SMTP", return_value=fake):
            transport.send(msg)
        self.assertEqual([("send_message", (msg,))], fake.calls)
        self.assertTrue(fake.quit_called)

    def test_starttls_then_login(self) -> None:
        fake = FakeSMTP()
        transport = imip_transport.SMTPTransport(
            "smtp.example.org",
            587,
            username="alice",
            password="hunter2",
            use_starttls=True,
        )
        msg = _build_message()
        with mock.patch.object(smtplib, "SMTP", return_value=fake):
            transport.send(msg)
        self.assertEqual(
            [
                ("starttls", ()),
                ("ehlo", ()),
                ("login", ("alice", "hunter2")),
                ("send_message", (msg,)),
            ],
            fake.calls,
        )

    def test_ssl_uses_smtp_ssl(self) -> None:
        fake = FakeSMTP()
        transport = imip_transport.SMTPTransport("smtp.example.org", 465, use_ssl=True)
        with mock.patch.object(smtplib, "SMTP_SSL", return_value=fake) as ssl_ctor:
            transport.send(_build_message())
        ssl_ctor.assert_called_once_with(
            "smtp.example.org", 465, timeout=transport.timeout
        )

    def test_starttls_and_ssl_rejected(self) -> None:
        with self.assertRaises(ValueError):
            imip_transport.SMTPTransport(
                "smtp.example.org", 25, use_starttls=True, use_ssl=True
            )

    def test_username_without_password_rejected(self) -> None:
        with self.assertRaises(ValueError):
            imip_transport.SMTPTransport("smtp.example.org", 25, username="alice")

    def test_connect_failure_wrapped(self) -> None:
        transport = imip_transport.SMTPTransport("smtp.example.org", 25)
        with mock.patch.object(smtplib, "SMTP", side_effect=OSError("refused")):
            with self.assertRaisesRegex(
                imip_transport.IMIPTransportError, "smtp.example.org:25"
            ):
                transport.send(_build_message())

    def test_send_failure_wrapped(self) -> None:
        fake = FakeSMTP()
        refusal = smtplib.SMTPRecipientsRefused({"alice@example.org": (550, b"no")})
        transport = imip_transport.SMTPTransport("smtp.example.org", 25)
        with mock.patch.object(smtplib, "SMTP", return_value=fake):
            with mock.patch.object(fake, "send_message", side_effect=refusal):
                with self.assertRaises(imip_transport.IMIPTransportError):
                    transport.send(_build_message())
        self.assertTrue(fake.quit_called)


if __name__ == "__main__":
    unittest.main()
