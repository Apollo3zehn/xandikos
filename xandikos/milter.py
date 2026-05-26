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

"""Sendmail/Postfix milter for inbound iMIP messages.

This module implements just enough of the libmilter (SMFI) wire protocol
to let Postfix hand each inbound message to Xandikos at smtpd time. The
milter is a pure observer: it never modifies, defers, or rejects a
message. If the message carries an iMIP REQUEST/REPLY/CANCEL payload,
the message is forwarded to a running Xandikos for storage in the
schedule inbox; either way the message is accepted so normal mailbox
delivery proceeds.

Two transports are supported:

* ``--lmtp-socket``: hand the message to ``xandikos serve
  --imip-listen`` over LMTP. This is the recommended same-host setup
  and is symmetric with the existing iMIP LMTP listener.
* ``--server-url``: POST the extracted iTIP payload to a remote
  Xandikos's schedule-inbox URL over HTTP. This is the cross-host
  fallback.

A separate ``xandikos-milter`` console script is provided so the
milter can run as its own service alongside (or independently of)
``xandikos serve``.

Protocol references:
  * sendmail/libmilter/docs/xxfi_*.html in the sendmail source tree.
  * postfix/proto/MILTER_README (high-level overview).

The wire format is straightforward: each frame is a 4-byte big-endian
length, then a one-byte command, then the command body. We negotiate
once via SMFIC_OPTNEG, then for every phase up to ``BODYEOB`` we reply
``CONTINUE``; at ``BODYEOB`` we look at the reassembled message and
respond ``ACCEPT`` so Postfix continues with delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import struct
import sys
from dataclasses import dataclass, field

from . import imip
from .imip_listen import (
    IMIPListenConfigError,
    _parse_socket_mode,
    _resolve_socket_group,
    _store_payload,
    parse_listen_target,
)
from .import_imip import _post_itip_to_server, _read_password_file


logger = logging.getLogger(__name__)


# --- SMFI wire constants ---------------------------------------------------
#
# Subset of the libmilter protocol that is relevant for a body-inspecting
# milter. Values come from sendmail's libmilter/mfdef.h.

SMFIC_ABORT = b"A"
SMFIC_BODY = b"B"
SMFIC_CONNECT = b"C"
SMFIC_MACRO = b"D"
SMFIC_BODYEOB = b"E"
SMFIC_HELO = b"H"
SMFIC_QUIT_NC = b"K"
SMFIC_HEADER = b"L"
SMFIC_MAIL = b"M"
SMFIC_EOH = b"N"
SMFIC_OPTNEG = b"O"
SMFIC_QUIT = b"Q"
SMFIC_RCPT = b"R"
SMFIC_DATA = b"T"
SMFIC_UNKNOWN = b"U"

SMFIR_ACCEPT = b"a"
SMFIR_CONTINUE = b"c"
SMFIR_DISCARD = b"d"
SMFIR_REJECT = b"r"
SMFIR_TEMPFAIL = b"t"
SMFIR_REPLYCODE = b"y"

# Highest protocol version we understand. Postfix supports up to 6.
SMFI_PROTOCOL_VERSION = 6

# We don't change anything about the message.
SMFIF_NONE = 0

# Protocol option flags: tell the MTA which phases we don't need.
# We need headers and the body to detect iMIP, but nothing else.
SMFIP_NOCONNECT = 0x01
SMFIP_NOHELO = 0x02
SMFIP_NOMAIL = 0x04
SMFIP_NORCPT = 0x08
SMFIP_NOBODY = 0x10
SMFIP_NOHDRS = 0x20
SMFIP_NOEOH = 0x40
SMFIP_NOUNKNOWN = 0x100
SMFIP_NODATA = 0x200
SMFIP_SKIP = 0x400

# We need RCPT (to log/route by envelope recipient), HEADER, EOH and
# BODY. Everything else can be elided.
SMFI_PROTOCOL_FLAGS = (
    SMFIP_NOCONNECT | SMFIP_NOHELO | SMFIP_NOMAIL | SMFIP_NOUNKNOWN | SMFIP_NODATA
)

# Maximum frame size we will accept from the MTA. Postfix default is
# ~64KiB per BODY chunk; allow a generous ceiling per frame.
_MAX_FRAME_SIZE = 4 * 1024 * 1024

# Cap a single SMTP message before refusing to import it. iMIP payloads
# are tiny (a few KB); 8 MiB is more than enough headroom and stops a
# pathological mailing list from ballooning memory.
_MAX_MESSAGE_SIZE = 8 * 1024 * 1024


@dataclass
class _Message:
    """Per-MAIL-FROM state for the current SMTP transaction."""

    rcpts: list[str] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytearray = field(default_factory=bytearray)
    too_large: bool = False

    def reset(self) -> None:
        self.rcpts.clear()
        self.headers.clear()
        self.body.clear()
        self.too_large = False


class Transport:
    """How a milter delivers a detected iMIP message to Xandikos.

    Subclasses raise on failure; the milter logs and accepts the
    message anyway. ``target`` is a short human-readable description
    used in the success log line.
    """

    target: str

    async def deliver(
        self,
        message_bytes: bytes,
        payload: imip.IMIPPayload,
        rcpts: list[str],
    ) -> None:
        raise NotImplementedError


class LMTPTransport(Transport):
    """Forward the raw RFC 5322 message to ``xandikos serve --imip-listen``.

    LMTP is the same wire format the LMTP listener already accepts from
    Sieve/Postfix. The envelope ``MAIL FROM`` / ``RCPT TO`` are passed
    through from Postfix; the listener does not route on them.
    """

    def __init__(self, target: tuple[str, int] | str) -> None:
        self._target = target
        self.target = (
            target if isinstance(target, str) else "%s:%d" % target
        )  # for logging

    async def deliver(
        self,
        message_bytes: bytes,
        payload: imip.IMIPPayload,
        rcpts: list[str],
    ) -> None:
        del payload  # the LMTP listener parses the message itself
        loop = asyncio.get_running_loop()
        mail_from = "<>"
        # If Postfix gave us no RCPT, fabricate one — the LMTP listener
        # ignores it but smtplib requires at least one recipient.
        rcpt_tos = rcpts or ["xandikos@localhost"]
        await loop.run_in_executor(
            None, _lmtp_sendmail, self._target, mail_from, rcpt_tos, message_bytes
        )


class HTTPTransport(Transport):
    """POST the extracted iTIP payload to a Xandikos schedule-inbox URL.

    Useful when the milter and Xandikos run on different hosts and you
    want HTTPS + HTTP Basic credentials between them.
    """

    def __init__(
        self,
        server_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        unix_socket: str | None = None,
    ) -> None:
        self._server_url = server_url
        self._username = username
        self._password = password
        self._unix_socket = unix_socket
        self.target = server_url

    async def deliver(
        self,
        message_bytes: bytes,
        payload: imip.IMIPPayload,
        rcpts: list[str],
    ) -> None:
        del message_bytes, rcpts
        await _post_itip_to_server(
            self._server_url,
            payload.calendar_data,
            username=self._username,
            password=self._password,
            unix_socket=self._unix_socket,
        )


class InProcessTransport(Transport):
    """Store directly into a backend that lives in the same process.

    Used by ``xandikos serve --milter-listen`` so the embedded milter
    can drop iMIP payloads straight into the running backend's schedule
    inbox without going through LMTP or HTTP.
    """

    def __init__(self, backend, principal_path: str) -> None:
        self._backend = backend
        self._principal_path = principal_path
        self.target = principal_path

    async def deliver(
        self,
        message_bytes: bytes,
        payload: imip.IMIPPayload,
        rcpts: list[str],
    ) -> None:
        del message_bytes, rcpts
        msg_id = "(milter in-process)"
        result = await _store_payload(
            self._backend, self._principal_path, payload, msg_id
        )
        if result is None:
            # _store_payload already logged the error; re-raise so the
            # milter records the failure in its own log line and the
            # caller sees deliver() failed.
            raise RuntimeError(
                f"failed to store iMIP payload in {self._principal_path}"
            )


def _lmtp_sendmail(
    target: tuple[str, int] | str,
    mail_from: str,
    rcpt_tos: list[str],
    data: bytes,
) -> None:
    """Synchronous LMTP send. Called from a thread by LMTPTransport."""
    import smtplib

    if isinstance(target, tuple):
        host, port = target
        client = smtplib.LMTP(host=host, port=port)
    else:
        # smtplib.LMTP treats a host argument starting with '/' as a
        # Unix socket path.
        client = smtplib.LMTP(host=target)
    try:
        client.sendmail(mail_from, rcpt_tos, data)
    finally:
        try:
            client.quit()
        except smtplib.SMTPException:
            pass


class MilterHandler:
    """Filter inbound mail for iMIP and forward matches via a Transport."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def handle_message(self, message_bytes: bytes, rcpts: list[str]) -> None:
        """Inspect *message_bytes* and forward it if it is a valid iMIP message.

        Errors are logged but never propagated: the milter always accepts.
        """
        rcpt_str = ", ".join(rcpts) or "(no recipient)"
        try:
            message = imip.parse_message(message_bytes)
        except imip.InvalidIMIPMessage as exc:
            logger.debug("Not a parseable RFC 5322 message for %s: %s", rcpt_str, exc)
            return

        msg_id = message.get("Message-ID") or "(no Message-ID)"
        if imip.is_auto_submitted(message):
            logger.info(
                "Skipping auto-submitted message %s (Auto-Submitted: %s) for %s",
                msg_id,
                message.get("Auto-Submitted"),
                rcpt_str,
            )
            return

        try:
            payload = imip.extract_payload(message)
        except imip.InvalidIMIPMessage as exc:
            # Most mail flowing through Postfix won't be iMIP. Log at debug
            # so admins running with default INFO don't get flooded.
            logger.debug("No iMIP payload in %s for %s: %s", msg_id, rcpt_str, exc)
            return

        try:
            await self._transport.deliver(message_bytes, payload, rcpts)
        except Exception as exc:
            logger.error(
                "Failed to forward %s iMIP %s to %s: %s",
                msg_id,
                payload.method,
                self._transport.target,
                exc,
            )
            return
        logger.info(
            "Forwarded iMIP %s message %s to %s for %s",
            payload.method,
            msg_id,
            self._transport.target,
            rcpt_str,
        )


class MilterProtocolError(Exception):
    """The MTA sent something we cannot interpret as a valid SMFI frame."""


async def _read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes] | None:
    """Read one libmilter frame: ``(cmd, data)``. Returns None at EOF."""
    header = await _readexactly_or_eof(reader, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length == 0:
        raise MilterProtocolError("zero-length frame")
    if length > _MAX_FRAME_SIZE:
        raise MilterProtocolError(f"frame too large: {length} bytes")
    payload = await reader.readexactly(length)
    return payload[:1], payload[1:]


async def _readexactly_or_eof(reader: asyncio.StreamReader, n: int) -> bytes | None:
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise


def _write_frame(writer: asyncio.StreamWriter, cmd: bytes, data: bytes = b"") -> None:
    writer.write(struct.pack(">I", len(cmd) + len(data)) + cmd + data)


def _decode_cstring(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _parse_header(data: bytes) -> tuple[str, str]:
    # HEADER frames are: <name>\0<value>\0
    parts = data.split(b"\x00")
    name = parts[0].decode("utf-8", errors="replace") if parts else ""
    value = parts[1].decode("utf-8", errors="replace") if len(parts) > 1 else ""
    return name, value


def _reassemble_message(msg: _Message) -> bytes:
    """Rebuild an RFC 5322 message from accumulated header/body frames."""
    buf = bytearray()
    for name, value in msg.headers:
        buf.extend(name.encode("utf-8", errors="replace"))
        buf.extend(b": ")
        # Header values may contain folded CRLFs already; emit as-is.
        buf.extend(value.encode("utf-8", errors="replace"))
        buf.extend(b"\r\n")
    buf.extend(b"\r\n")
    buf.extend(msg.body)
    return bytes(buf)


async def handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    handler: MilterHandler,
) -> None:
    """Run one MTA<->milter conversation to completion."""
    state = _Message()
    try:
        while True:
            frame = await _read_frame(reader)
            if frame is None:
                return
            cmd, data = frame
            if cmd == SMFIC_OPTNEG:
                if len(data) < 12:
                    raise MilterProtocolError("short SMFIC_OPTNEG")
                mta_version, mta_actions, mta_protocol = struct.unpack(
                    ">III", data[:12]
                )
                version = min(SMFI_PROTOCOL_VERSION, mta_version)
                # AND with what the MTA advertises so we never ask for
                # something it can't deliver.
                protocol = SMFI_PROTOCOL_FLAGS & mta_protocol
                actions = SMFIF_NONE & mta_actions
                _write_frame(
                    writer,
                    SMFIC_OPTNEG,
                    struct.pack(">III", version, actions, protocol),
                )
                await writer.drain()
            elif cmd == SMFIC_MACRO:
                # Macros announce variables for the next command. We
                # don't use them but must not respond.
                continue
            elif cmd == SMFIC_CONNECT or cmd == SMFIC_HELO:
                _write_frame(writer, SMFIR_CONTINUE)
                await writer.drain()
            elif cmd == SMFIC_MAIL:
                state.reset()
                _write_frame(writer, SMFIR_CONTINUE)
                await writer.drain()
            elif cmd == SMFIC_RCPT:
                rcpt = _decode_cstring(data)
                if rcpt:
                    state.rcpts.append(rcpt)
                _write_frame(writer, SMFIR_CONTINUE)
                await writer.drain()
            elif cmd == SMFIC_DATA or cmd == SMFIC_EOH:
                _write_frame(writer, SMFIR_CONTINUE)
                await writer.drain()
            elif cmd == SMFIC_HEADER:
                name, value = _parse_header(data)
                if name and not state.too_large:
                    state.headers.append((name, value))
                _write_frame(writer, SMFIR_CONTINUE)
                await writer.drain()
            elif cmd == SMFIC_BODY:
                if not state.too_large:
                    if len(state.body) + len(data) > _MAX_MESSAGE_SIZE:
                        logger.warning(
                            "Truncating oversized message body (>%d bytes); "
                            "not importing as iMIP",
                            _MAX_MESSAGE_SIZE,
                        )
                        state.too_large = True
                        state.body.clear()
                    else:
                        state.body.extend(data)
                _write_frame(writer, SMFIR_CONTINUE)
                await writer.drain()
            elif cmd == SMFIC_BODYEOB:
                if not state.too_large:
                    message_bytes = _reassemble_message(state)
                    try:
                        await handler.handle_message(message_bytes, list(state.rcpts))
                    except Exception:
                        # Never let a handler failure block mail flow.
                        logger.exception("milter handler raised; accepting anyway")
                state.reset()
                _write_frame(writer, SMFIR_ACCEPT)
                await writer.drain()
            elif cmd == SMFIC_ABORT:
                state.reset()
                # No response per protocol.
            elif cmd == SMFIC_QUIT or cmd == SMFIC_QUIT_NC:
                return
            elif cmd == SMFIC_UNKNOWN:
                _write_frame(writer, SMFIR_CONTINUE)
                await writer.drain()
            else:
                logger.debug("Ignoring unknown milter command: %r", cmd)
                _write_frame(writer, SMFIR_CONTINUE)
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        return
    except MilterProtocolError as exc:
        logger.warning("milter protocol error: %s; closing connection", exc)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass


class Listener:
    """A milter listener bound to a Unix socket or TCP host:port."""

    def __init__(
        self,
        server: asyncio.AbstractServer,
        socket_path: str | None,
    ) -> None:
        self._server = server
        self._socket_path = socket_path

    async def serve_forever(self) -> None:
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()
        if self._socket_path is not None:
            try:
                os.unlink(self._socket_path)
            except FileNotFoundError:
                pass


async def start_listener(
    target: tuple[str, int] | str,
    handler: MilterHandler,
    *,
    socket_mode: str | None = None,
    socket_group: str | None = None,
) -> Listener:
    """Start a milter listener bound to *target*.

    Mirrors ``imip_listen.start_listener``: *target* is either a
    filesystem path (Unix socket) or a ``(host, port)`` tuple. The
    returned :class:`Listener` exposes ``serve_forever()`` / ``stop()``.
    """
    socket_gid = _resolve_socket_group(socket_group) if socket_group else None
    socket_mode_int = _parse_socket_mode(socket_mode) if socket_mode else None

    async def client_connected(reader, writer):
        await handle_connection(reader, writer, handler)

    socket_path: str | None = None
    if isinstance(target, tuple):
        host, port = target
        server = await asyncio.start_server(client_connected, host=host, port=port)
    elif sys.platform == "win32":
        raise IMIPListenConfigError(
            "Unix-socket milter targets are not supported on Windows; "
            "use host:port instead"
        )
    else:
        socket_path = target
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(client_connected, path=socket_path)
        if socket_gid is not None:
            os.chown(socket_path, -1, socket_gid)
        if socket_mode_int is not None:
            os.chmod(socket_path, socket_mode_int)

    return Listener(server, socket_path)


def add_listener_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``--milter-listen[-mode|-group]`` arguments on *parser*.

    Used by ``xandikos serve`` to expose the milter inside the same
    process as the web server. The standalone ``xandikos-milter`` CLI
    uses :func:`add_arguments` instead (which adds transport flags as
    well).
    """
    group = parser.add_argument_group(title="Milter Listener Options")
    group.add_argument(
        "--milter-listen",
        dest="milter_listen",
        default=os.environ.get("XANDIKOS_MILTER_LISTEN"),
        help=(
            "Listen for Postfix/Sendmail milter (SMFI) connections. Pass "
            "unix:/path/to/sock for a Unix domain socket, or host:port "
            "for TCP. Postfix uses this as smtpd_milters=<target>."
        ),
    )
    group.add_argument(
        "--milter-listen-mode",
        dest="milter_listen_mode",
        default=os.environ.get("XANDIKOS_MILTER_LISTEN_MODE"),
        help=(
            "File mode (octal, e.g. 660) for the milter Unix socket. "
            "Ignored for TCP listeners."
        ),
    )
    group.add_argument(
        "--milter-listen-group",
        dest="milter_listen_group",
        default=os.environ.get("XANDIKOS_MILTER_LISTEN_GROUP"),
        help=("Group ownership for the milter Unix socket. Ignored for TCP listeners."),
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the ``xandikos-milter`` command-line arguments."""
    transport_group = parser.add_argument_group(
        title="Transport (one of --lmtp-socket or --server-url is required)",
    )
    transport_group.add_argument(
        "--lmtp-socket",
        type=str,
        default=os.environ.get("XANDIKOS_MILTER_LMTP_SOCKET"),
        help=(
            "LMTP target of a running 'xandikos serve --imip-listen' "
            "endpoint. Pass unix:/path/to/sock for a Unix domain socket "
            "or host:port for TCP. This is the recommended same-host "
            "setup. May also be set via XANDIKOS_MILTER_LMTP_SOCKET."
        ),
    )
    transport_group.add_argument(
        "--server-url",
        type=str,
        default=os.environ.get("XANDIKOS_MILTER_SERVER_URL"),
        help=(
            "Schedule-inbox URL of a running Xandikos server. Each iMIP "
            "payload is POSTed here as text/calendar. Use this for "
            "cross-host deployments. May also be set via "
            "XANDIKOS_MILTER_SERVER_URL."
        ),
    )
    transport_group.add_argument(
        "--username",
        type=str,
        help="Username for HTTP Basic authentication against --server-url.",
    )
    transport_group.add_argument(
        "--password-file",
        type=str,
        help="File containing the HTTP Basic authentication password.",
    )
    transport_group.add_argument(
        "--unix-socket",
        type=str,
        help="Unix domain socket path for HTTP requests to the Xandikos server.",
    )
    parser.add_argument(
        "--listen",
        type=str,
        default=os.environ.get("XANDIKOS_MILTER_LISTEN"),
        help=(
            "Listen target. Pass unix:/path/to/sock for a Unix domain "
            "socket, or host:port for TCP. Postfix uses this as "
            "smtpd_milters=<target>. May also be set via "
            "XANDIKOS_MILTER_LISTEN."
        ),
    )
    parser.add_argument(
        "--listen-mode",
        default=os.environ.get("XANDIKOS_MILTER_LISTEN_MODE"),
        help=(
            "File mode (octal, e.g. 660) for the milter Unix socket. "
            "Ignored for TCP listeners."
        ),
    )
    parser.add_argument(
        "--listen-group",
        default=os.environ.get("XANDIKOS_MILTER_LISTEN_GROUP"),
        help=("Group ownership for the milter Unix socket. Ignored for TCP listeners."),
    )


def _build_transport(args: argparse.Namespace) -> Transport | None:
    """Resolve the configured transport, or None with an error logged."""
    if args.lmtp_socket and args.server_url:
        logger.error("--lmtp-socket and --server-url are mutually exclusive.")
        return None
    if args.lmtp_socket:
        try:
            target = parse_listen_target(args.lmtp_socket)
        except IMIPListenConfigError as exc:
            logger.error("Invalid --lmtp-socket: %s", exc)
            return None
        return LMTPTransport(target)
    if args.server_url:
        password = _read_password_file(args.password_file)
        return HTTPTransport(
            args.server_url,
            username=args.username,
            password=password,
            unix_socket=args.unix_socket,
        )
    logger.error(
        "Missing transport: pass --lmtp-socket (recommended) or --server-url. "
        "See 'xandikos-milter --help'."
    )
    return None


async def main(args: argparse.Namespace) -> int:
    """Run the ``xandikos-milter`` command."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.listen:
        logger.error(
            "Missing --listen target (or XANDIKOS_MILTER_LISTEN); "
            "see 'xandikos-milter --help'."
        )
        return 2
    transport = _build_transport(args)
    if transport is None:
        return 2
    try:
        target = parse_listen_target(args.listen)
    except IMIPListenConfigError as exc:
        logger.error("Invalid --listen target: %s", exc)
        return 2

    handler = MilterHandler(transport)
    try:
        listener = await start_listener(
            target,
            handler,
            socket_mode=args.listen_mode,
            socket_group=args.listen_group,
        )
    except IMIPListenConfigError as exc:
        logger.error("Cannot start milter: %s", exc)
        return 2

    logger.info(
        "xandikos milter listening on %s, forwarding to %s",
        args.listen,
        transport.target,
    )
    try:
        await listener.serve_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await listener.stop()
    return 0


def cli_main() -> None:
    """Entry point for the ``xandikos-milter`` console script."""
    parser = argparse.ArgumentParser(
        prog="xandikos-milter",
        description=(
            "Run a Postfix/Sendmail milter that imports inbound iMIP "
            "messages into a Xandikos principal's schedule inbox."
        ),
    )
    add_arguments(parser)
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(main(args)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    cli_main()
