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

"""Tests for xandikos.milter (Postfix/Sendmail milter for inbound iMIP)."""

import asyncio
import importlib.util
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from xandikos import milter


_HAS_AIOSMTPD = importlib.util.find_spec("aiosmtpd") is not None


REQUEST = b"""\
BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Test//EN\r
METHOD:REQUEST\r
BEGIN:VEVENT\r
UID:milter-event@example.com\r
DTSTAMP:20260101T000000Z\r
DTSTART:20260601T100000Z\r
DTEND:20260601T110000Z\r
SUMMARY:Milter invite\r
ORGANIZER:mailto:alice@example.com\r
ATTENDEE:mailto:bob@example.com\r
END:VEVENT\r
END:VCALENDAR\r
"""


def _imip_message_bytes(*, auto_submitted: str | None = None) -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "Bob <bob@example.com>"
    msg["Message-ID"] = "<milter-test@example.com>"
    if auto_submitted is not None:
        msg["Auto-Submitted"] = auto_submitted
    msg.set_content(
        REQUEST.decode("utf-8"),
        subtype="calendar",
        charset="utf-8",
        params={"method": "REQUEST"},
    )
    return msg.as_bytes()


class _CapturingHandler:
    """Stand-in for MilterHandler that records calls instead of POSTing."""

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, list[str]]] = []

    async def handle_message(self, message_bytes: bytes, rcpts: list[str]) -> None:
        self.calls.append((message_bytes, list(rcpts)))


def _make_http_handler():
    """Return a MilterHandler whose Transport is HTTP (covers --server-url)."""
    return milter.MilterHandler(
        milter.HTTPTransport(
            "https://dav.example/user/inbox/",
            username="bob",
            password="hunter2",
            unix_socket="/run/xandikos/web.sock",
        )
    )


def _make_lmtp_handler(target="/run/xandikos/imip.sock"):
    """Return a MilterHandler whose Transport is LMTP (covers --lmtp-socket)."""
    return milter.MilterHandler(milter.LMTPTransport(target))


class HTTPTransportTests(unittest.IsolatedAsyncioTestCase):
    """Cover MilterHandler driven by HTTPTransport (--server-url)."""

    async def test_valid_request_is_posted(self):
        captured: dict[str, object] = {}

        async def fake_post(
            url, calendar_data, *, username, password, unix_socket
        ) -> None:
            captured["url"] = url
            captured["calendar_data"] = calendar_data
            captured["username"] = username
            captured["password"] = password
            captured["unix_socket"] = unix_socket

        from xandikos import imip

        raw = _imip_message_bytes()
        expected_calendar_data = imip.extract_payload_from_bytes(raw).calendar_data

        with patch("xandikos.milter._post_itip_to_server", fake_post):
            await _make_http_handler().handle_message(raw, ["bob@example.com"])

        self.assertEqual("https://dav.example/user/inbox/", captured["url"])
        self.assertEqual(expected_calendar_data, captured["calendar_data"])
        self.assertEqual("bob", captured["username"])
        self.assertEqual("hunter2", captured["password"])
        self.assertEqual("/run/xandikos/web.sock", captured["unix_socket"])

    async def test_non_imip_message_is_silently_ignored(self):
        async def fake_post(*a, **kw):
            raise AssertionError("should not POST for a non-iMIP message")

        with patch("xandikos.milter._post_itip_to_server", fake_post):
            await _make_http_handler().handle_message(
                b"From: a@example.com\r\nTo: b@example.com\r\n"
                b"Subject: hello\r\n\r\nhi there\r\n",
                ["b@example.com"],
            )

    async def test_unparseable_input_is_silently_ignored(self):
        async def fake_post(*a, **kw):
            raise AssertionError("should not POST for unparseable input")

        with patch("xandikos.milter._post_itip_to_server", fake_post):
            await _make_http_handler().handle_message(b"\x00\x01\x02 nope", [])

    async def test_auto_submitted_message_is_skipped(self):
        async def fake_post(*a, **kw):
            raise AssertionError("should not POST an auto-submitted message")

        with patch("xandikos.milter._post_itip_to_server", fake_post):
            with self.assertLogs("xandikos.milter", level="INFO") as logs:
                await _make_http_handler().handle_message(
                    _imip_message_bytes(auto_submitted="auto-generated"),
                    ["bob@example.com"],
                )
        self.assertTrue(
            any("auto-submitted" in line.lower() for line in logs.output),
            logs.output,
        )

    async def test_post_failure_is_logged_but_not_raised(self):
        async def fake_post(*a, **kw):
            raise RuntimeError("boom")

        with patch("xandikos.milter._post_itip_to_server", fake_post):
            with self.assertLogs("xandikos.milter", level="ERROR") as logs:
                # Must not propagate — mail flow comes first.
                await _make_http_handler().handle_message(
                    _imip_message_bytes(), ["bob@example.com"]
                )
        joined = "\n".join(logs.output)
        self.assertIn("boom", joined)
        self.assertIn("https://dav.example/user/inbox/", joined)


class LMTPTransportTests(unittest.IsolatedAsyncioTestCase):
    """Cover MilterHandler driven by LMTPTransport (--lmtp-socket)."""

    async def test_valid_request_is_forwarded_via_lmtp(self):
        captured: dict[str, object] = {}

        def fake_lmtp(target, mail_from, rcpt_tos, data) -> None:
            captured["target"] = target
            captured["mail_from"] = mail_from
            captured["rcpt_tos"] = list(rcpt_tos)
            captured["data"] = data

        with patch("xandikos.milter._lmtp_sendmail", fake_lmtp):
            await _make_lmtp_handler().handle_message(
                _imip_message_bytes(), ["bob@example.com"]
            )

        self.assertEqual("/run/xandikos/imip.sock", captured["target"])
        self.assertEqual("<>", captured["mail_from"])
        self.assertEqual(["bob@example.com"], captured["rcpt_tos"])
        # The LMTP listener parses the message itself; we must forward the
        # raw RFC 5322 bytes unchanged.
        self.assertEqual(_imip_message_bytes(), captured["data"])

    async def test_no_rcpts_uses_placeholder_recipient(self):
        # smtplib refuses to send with zero RCPTs; ensure the milter
        # supplies a placeholder so a missing envelope RCPT is not fatal.
        captured: dict[str, object] = {}

        def fake_lmtp(target, mail_from, rcpt_tos, data) -> None:
            captured["rcpt_tos"] = list(rcpt_tos)

        with patch("xandikos.milter._lmtp_sendmail", fake_lmtp):
            await _make_lmtp_handler().handle_message(_imip_message_bytes(), [])

        self.assertEqual(["xandikos@localhost"], captured["rcpt_tos"])

    async def test_lmtp_failure_is_logged_but_not_raised(self):
        def fake_lmtp(target, mail_from, rcpt_tos, data) -> None:
            raise RuntimeError("lmtp boom")

        with patch("xandikos.milter._lmtp_sendmail", fake_lmtp):
            with self.assertLogs("xandikos.milter", level="ERROR") as logs:
                await _make_lmtp_handler().handle_message(
                    _imip_message_bytes(), ["bob@example.com"]
                )
        joined = "\n".join(logs.output)
        self.assertIn("lmtp boom", joined)
        self.assertIn("/run/xandikos/imip.sock", joined)

    async def test_non_imip_message_is_silently_ignored(self):
        def fake_lmtp(*a, **kw):
            raise AssertionError("should not LMTP-forward a non-iMIP message")

        with patch("xandikos.milter._lmtp_sendmail", fake_lmtp):
            await _make_lmtp_handler().handle_message(
                b"From: a@example.com\r\nTo: b@example.com\r\n"
                b"Subject: hello\r\n\r\nhi there\r\n",
                ["b@example.com"],
            )

    async def test_lmtp_target_is_used_in_log_line(self):
        # Success log mentions the configured target so admins can match
        # log lines against their --lmtp-socket value.
        def fake_lmtp(target, mail_from, rcpt_tos, data) -> None:
            return None

        with patch("xandikos.milter._lmtp_sendmail", fake_lmtp):
            with self.assertLogs("xandikos.milter", level="INFO") as logs:
                await _make_lmtp_handler("/run/xandikos/imip.sock").handle_message(
                    _imip_message_bytes(), ["bob@example.com"]
                )
        self.assertTrue(
            any("/run/xandikos/imip.sock" in line for line in logs.output),
            logs.output,
        )


class LMTPTransportTargetTests(unittest.TestCase):
    """Exercise the small bit of plumbing inside LMTPTransport itself."""

    def test_unix_target_uses_path_as_log_target(self):
        t = milter.LMTPTransport("/run/xandikos/imip.sock")
        self.assertEqual("/run/xandikos/imip.sock", t.target)

    def test_tcp_target_serialises_host_port_for_log(self):
        t = milter.LMTPTransport(("mta.example", 24))
        self.assertEqual("mta.example:24", t.target)


class InProcessTransportTests(unittest.IsolatedAsyncioTestCase):
    """Cover the in-process transport used by ``serve --milter-listen``."""

    async def asyncSetUp(self):
        from xandikos.web import SingleUserFilesystemBackend

        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)
        self.backend = SingleUserFilesystemBackend(self.test_dir)
        self.backend._mark_as_principal("/user/")
        self.backend.create_principal("/user/", create_defaults=True)

    async def test_valid_request_lands_in_inbox(self):
        handler = milter.MilterHandler(
            milter.InProcessTransport(self.backend, "/user/")
        )
        await handler.handle_message(_imip_message_bytes(), ["bob@example.com"])
        inbox = self.backend.get_resource("/user/inbox")
        self.assertEqual(1, len(list(inbox.members())))

    async def test_missing_principal_is_logged_but_not_raised(self):
        handler = milter.MilterHandler(
            milter.InProcessTransport(self.backend, "/nonexistent/")
        )
        with self.assertLogs("xandikos.milter", level="ERROR") as logs:
            # Must not propagate — the milter always accepts.
            await handler.handle_message(_imip_message_bytes(), ["bob@example.com"])
        joined = "\n".join(logs.output)
        self.assertIn("/nonexistent/", joined)
        inbox = self.backend.get_resource("/user/inbox")
        self.assertEqual(0, len(list(inbox.members())))

    async def test_target_is_principal_path_for_log(self):
        t = milter.InProcessTransport(self.backend, "/user/")
        self.assertEqual("/user/", t.target)


class BuildTransportTests(unittest.TestCase):
    """Validate the --lmtp-socket / --server-url selection in main()."""

    def _ns(self, **kwargs):
        import argparse

        defaults = dict(
            lmtp_socket=None,
            server_url=None,
            username=None,
            password_file=None,
            unix_socket=None,
        )
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_lmtp_socket_builds_lmtp_transport(self):
        t = milter._build_transport(self._ns(lmtp_socket="/run/xandikos/imip.sock"))
        self.assertIsInstance(t, milter.LMTPTransport)
        self.assertEqual("/run/xandikos/imip.sock", t.target)

    def test_server_url_builds_http_transport(self):
        t = milter._build_transport(
            self._ns(server_url="https://dav.example/user/inbox/")
        )
        self.assertIsInstance(t, milter.HTTPTransport)
        self.assertEqual("https://dav.example/user/inbox/", t.target)

    def test_both_transports_set_is_rejected(self):
        with self.assertLogs("xandikos.milter", level="ERROR"):
            self.assertIsNone(
                milter._build_transport(
                    self._ns(
                        lmtp_socket="/run/xandikos/imip.sock",
                        server_url="https://dav.example/user/inbox/",
                    )
                )
            )

    def test_neither_transport_set_is_rejected(self):
        with self.assertLogs("xandikos.milter", level="ERROR"):
            self.assertIsNone(milter._build_transport(self._ns()))

    def test_invalid_lmtp_target_is_rejected(self):
        with self.assertLogs("xandikos.milter", level="ERROR"):
            self.assertIsNone(
                milter._build_transport(self._ns(lmtp_socket="nope-no-port"))
            )


def _frame(cmd: bytes, data: bytes = b"") -> bytes:
    return struct.pack(">I", len(cmd) + len(data)) + cmd + data


class FakeMilterClient:
    """Build a stream of SMFI frames the way Postfix would."""

    def __init__(self) -> None:
        self._to_milter: list[bytes] = []

    def optneg(self, version: int = 6, actions: int = 0, protocol: int = 0) -> None:
        self._to_milter.append(
            _frame(milter.SMFIC_OPTNEG, struct.pack(">III", version, actions, protocol))
        )

    def mail_from(self, addr: str = "<alice@example.com>") -> None:
        self._to_milter.append(_frame(milter.SMFIC_MAIL, addr.encode() + b"\x00"))

    def rcpt(self, addr: str) -> None:
        self._to_milter.append(_frame(milter.SMFIC_RCPT, addr.encode() + b"\x00"))

    def header(self, name: str, value: str) -> None:
        payload = name.encode() + b"\x00" + value.encode() + b"\x00"
        self._to_milter.append(_frame(milter.SMFIC_HEADER, payload))

    def eoh(self) -> None:
        self._to_milter.append(_frame(milter.SMFIC_EOH))

    def body(self, chunk: bytes) -> None:
        self._to_milter.append(_frame(milter.SMFIC_BODY, chunk))

    def end_of_message(self) -> None:
        self._to_milter.append(_frame(milter.SMFIC_BODYEOB))

    def quit(self) -> None:
        self._to_milter.append(_frame(milter.SMFIC_QUIT))

    def encoded(self) -> bytes:
        return b"".join(self._to_milter)


async def _drive(handler, client_bytes: bytes) -> bytes:
    """Feed *client_bytes* through handle_connection; return milter replies.

    Uses a real socketpair so we don't have to fake out asyncio's
    StreamWriter/Transport plumbing.
    """
    import socket

    server_sock, client_sock = socket.socketpair()
    try:
        client_sock.sendall(client_bytes)
        client_sock.shutdown(socket.SHUT_WR)

        reader, writer = await asyncio.open_connection(sock=server_sock)
        await milter.handle_connection(reader, writer, handler)

        chunks: list[bytes] = []
        client_sock.setblocking(False)
        while True:
            try:
                chunk = client_sock.recv(65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        client_sock.close()


def _replay_message(client: FakeMilterClient, raw: bytes) -> None:
    """Replay *raw* as separate HEADER/EOH/BODY/EOM frames."""
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(raw)
    for name, value in message.items():
        client.header(name, value)
    client.eoh()
    body_text = message.get_payload()
    if isinstance(body_text, str):
        body_bytes = body_text.encode("utf-8")
    elif isinstance(body_text, bytes):
        body_bytes = body_text
    else:
        body_bytes = b""
    client.body(body_bytes)
    client.end_of_message()


def _parse_responses(data: bytes) -> list[tuple[bytes, bytes]]:
    out: list[tuple[bytes, bytes]] = []
    offset = 0
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        offset += 4
        cmd = data[offset : offset + 1]
        body = data[offset + 1 : offset + length]
        offset += length
        out.append((cmd, body))
    return out


class HandleConnectionTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end test of the libmilter wire protocol."""

    async def test_imip_request_drives_handler_once_and_accepts(self):
        handler = _CapturingHandler()
        client = FakeMilterClient()
        client.optneg()
        client.mail_from()
        client.rcpt("<bob@example.com>")
        _replay_message(client, _imip_message_bytes())
        client.quit()

        responses = _parse_responses(await _drive(handler, client.encoded()))
        self.assertEqual(milter.SMFIC_OPTNEG, responses[0][0])
        self.assertEqual(milter.SMFIR_ACCEPT, responses[-1][0])
        for cmd, _ in responses[1:-1]:
            self.assertEqual(milter.SMFIR_CONTINUE, cmd)

        from xandikos import imip

        self.assertEqual(1, len(handler.calls))
        message_bytes, rcpts = handler.calls[0]
        expected_calendar_data = imip.extract_payload_from_bytes(
            _imip_message_bytes()
        ).calendar_data
        actual_calendar_data = imip.extract_payload_from_bytes(
            message_bytes
        ).calendar_data
        self.assertEqual(expected_calendar_data, actual_calendar_data)
        self.assertEqual(["<bob@example.com>"], rcpts)

    async def test_optneg_only_then_quit(self):
        handler = _CapturingHandler()
        client = FakeMilterClient()
        client.optneg()
        client.quit()
        responses = _parse_responses(await _drive(handler, client.encoded()))
        self.assertEqual(1, len(responses))
        self.assertEqual(milter.SMFIC_OPTNEG, responses[0][0])
        self.assertEqual([], handler.calls)

    def test_protocol_flags_request_rcpt(self):
        # SMFIP_NORCPT in SMFI_PROTOCOL_FLAGS would tell Postfix to skip
        # RCPT events, leaving handle_message with an empty recipient
        # list. We log envelope recipients and may route by them, so
        # RCPT must not be elided.
        self.assertEqual(0, milter.SMFI_PROTOCOL_FLAGS & milter.SMFIP_NORCPT)


class FrameParsingTests(unittest.TestCase):
    def test_parse_header_splits_name_and_value(self):
        self.assertEqual(
            ("Subject", "Hello"),
            milter._parse_header(b"Subject\x00Hello\x00"),
        )

    def test_parse_header_handles_missing_terminator(self):
        # Some MTAs omit the trailing NUL; we should still decode.
        self.assertEqual(
            ("From", "alice@example.com"),
            milter._parse_header(b"From\x00alice@example.com"),
        )

    def test_decode_cstring_stops_at_first_null(self):
        self.assertEqual(
            "bob@example.com",
            milter._decode_cstring(b"bob@example.com\x00body=esmtparg"),
        )

    def test_reassemble_message_produces_parseable_rfc5322(self):
        msg = milter._Message()
        msg.headers.append(("From", "alice@example.com"))
        msg.headers.append(("To", "bob@example.com"))
        msg.body.extend(b"body line\r\n")
        self.assertEqual(
            b"From: alice@example.com\r\nTo: bob@example.com\r\n\r\nbody line\r\n",
            milter._reassemble_message(msg),
        )


@unittest.skipIf(sys.platform == "win32", "Unix sockets are not supported on Windows")
class StartListenerTests(unittest.IsolatedAsyncioTestCase):
    """Spin up the listener on a real Unix socket and round-trip a message."""

    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)
        self.socket_path = f"{self.test_dir}/milter.sock"
        self.handler = _CapturingHandler()
        self.listener = await milter.start_listener(self.socket_path, self.handler)

    async def asyncTearDown(self):
        await self.listener.stop()

    async def test_unix_socket_roundtrip(self):
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            client = FakeMilterClient()
            client.optneg()
            client.mail_from()
            client.rcpt("<bob@example.com>")
            _replay_message(client, _imip_message_bytes())
            client.quit()
            writer.write(client.encoded())
            await writer.drain()
            writer.write_eof()
            received = await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

        responses = _parse_responses(received)
        self.assertEqual(milter.SMFIR_ACCEPT, responses[-1][0])
        self.assertEqual(1, len(self.handler.calls))


@unittest.skipIf(sys.platform == "win32", "Unix sockets are not supported on Windows")
@unittest.skipUnless(_HAS_AIOSMTPD, "aiosmtpd is not installed")
class LMTPEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Drive LMTPTransport against a real imip_listen LMTP listener.

    Proves the milter's LMTP client and Xandikos's LMTP server agree on
    the wire and that a forwarded iMIP message actually lands in the
    schedule inbox.
    """

    async def asyncSetUp(self):
        from xandikos import imip_listen
        from xandikos.web import SingleUserFilesystemBackend

        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)
        self.socket_path = f"{self.test_dir}/imip.sock"
        self.backend = SingleUserFilesystemBackend(self.test_dir)
        self.backend._mark_as_principal("/user/")
        self.backend.create_principal("/user/", create_defaults=True)
        listener_handler = imip_listen.IMIPLMTPHandler(self.backend, "/user/")
        self.imip_listener = await imip_listen.start_listener(
            self.socket_path, listener_handler
        )

    async def asyncTearDown(self):
        await self.imip_listener.stop()

    async def test_milter_forwards_through_lmtp_and_message_lands_in_inbox(self):
        transport = milter.LMTPTransport(self.socket_path)
        handler = milter.MilterHandler(transport)
        await handler.handle_message(_imip_message_bytes(), ["bob@example.com"])
        inbox = self.backend.get_resource("/user/inbox")
        self.assertEqual(1, len(list(inbox.members())))


if __name__ == "__main__":
    unittest.main()
