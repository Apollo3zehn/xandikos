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

"""LMTP listener for inbound iMIP messages.

Runs alongside the Xandikos HTTP server so a single ``serve`` invocation
provides both CalDAV and an LMTP delivery socket. MTAs and Sieve scripts
can hand iMIP messages to this socket instead of shelling out to
``xandikos import-imip``.

LMTP (RFC 2033) is preferred over SMTP because it returns a per-recipient
status to the MDA, matching how Postfix/Dovecot treat local mailbox
delivery. The handler accepts the message regardless of envelope
recipient: the socket is the authentication boundary, not the address,
and a Xandikos ``serve`` instance has exactly one principal anyway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
from typing import TYPE_CHECKING

from . import imip


if TYPE_CHECKING:
    from .web import SingleUserFilesystemBackend


logger = logging.getLogger(__name__)


class IMIPLMTPHandler:
    """aiosmtpd handler that imports iMIP messages into a schedule inbox."""

    def __init__(
        self,
        backend: SingleUserFilesystemBackend,
        principal_path: str,
    ) -> None:
        self._backend = backend
        self._principal_path = principal_path

    async def handle_DATA(self, server, session, envelope) -> str:
        del server, session
        recipients = list(envelope.rcpt_tos)
        rcpt_str = ", ".join(recipients) or "(no recipient)"
        try:
            message = imip.parse_message(envelope.content)
        except imip.InvalidIMIPMessage as exc:
            logger.error("Rejecting LMTP delivery to %s: %s", rcpt_str, exc)
            return "550 5.6.0 Message is not a valid iMIP email"

        msg_id = message.get("Message-ID") or "(no Message-ID)"
        if imip.is_auto_submitted(message):
            logger.info(
                "Accepting and discarding auto-submitted message %s "
                "(Auto-Submitted: %s) for %s",
                msg_id,
                message.get("Auto-Submitted"),
                rcpt_str,
            )
            return "250 2.0.0 Auto-submitted message discarded"

        try:
            payload = imip.extract_payload(message)
        except imip.InvalidIMIPMessage as exc:
            logger.error("Rejecting %s for %s: %s", msg_id, rcpt_str, exc)
            return "550 5.6.0 No usable iMIP payload"

        target = await _store_payload(
            self._backend, self._principal_path, payload, msg_id
        )
        if target is None:
            return "451 4.3.0 Failed to store iTIP message"
        logger.info(
            "Imported iMIP %s message %s into %s for %s",
            payload.method,
            msg_id,
            target,
            rcpt_str,
        )
        return "250 2.0.0 Message stored"


async def _store_payload(
    backend: SingleUserFilesystemBackend,
    principal_path: str,
    payload: imip.IMIPPayload,
    msg_id: str,
) -> str | None:
    from . import web

    principal = backend.get_resource(principal_path)
    if not isinstance(principal, web.Principal):
        logger.error(
            "Cannot import %s iMIP %s: %s is not a principal",
            msg_id,
            payload.method,
            principal_path,
        )
        return None
    inbox_path = posixpath.join(principal_path, principal.get_schedule_inbox_url())
    inbox = backend.get_resource(inbox_path)
    if not isinstance(inbox, web.ScheduleInbox):
        logger.error(
            "Cannot import %s iMIP %s: %s is not a schedule inbox",
            msg_id,
            payload.method,
            inbox_path,
        )
        return None
    try:
        name, _etag = await inbox.create_member(
            None,
            [payload.calendar_data],
            "text/calendar",
            requester="xandikos imip-listen",
        )
    except Exception as exc:
        logger.error(
            "Failed to store %s iMIP %s in %s: %s",
            msg_id,
            payload.method,
            inbox_path,
            exc,
        )
        return None
    return f"{inbox_path.rstrip('/')}/{name}"


class IMIPListenConfigError(Exception):
    """The ``--imip-listen`` configuration is invalid."""


class Listener:
    """An LMTP listener bound to a Unix socket or TCP host:port."""

    def __init__(
        self,
        server: asyncio.AbstractServer,
        socket_path: str | None,
    ) -> None:
        self._server = server
        self._socket_path = socket_path

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()
        if self._socket_path is not None:
            try:
                os.unlink(self._socket_path)
            except FileNotFoundError:
                pass


def parse_listen_target(spec: str) -> tuple[str, int] | str:
    """Parse an ``--imip-listen`` argument into a unix path or (host, port).

    Accepts ``unix:/path/to/sock`` (or any value starting with ``/``) for a
    Unix socket, and ``host:port`` for TCP. Bare ``:port`` binds to
    ``localhost``.

    Raises:
      IMIPListenConfigError: if *spec* is not a recognised target.
    """
    if spec.startswith("unix:"):
        return spec[len("unix:") :]
    if spec.startswith("/"):
        return spec
    host, sep, port = spec.rpartition(":")
    if not sep:
        raise IMIPListenConfigError(
            "imip-listen target must be unix:/path or host:port, got %r" % spec
        )
    if not port:
        raise IMIPListenConfigError("imip-listen target is missing a port: %r" % spec)
    if not port.isdigit():
        raise IMIPListenConfigError("imip-listen port is not an integer: %r" % port)
    return (host or "localhost", int(port))


def _parse_socket_mode(mode: str) -> int:
    if not mode or any(c not in "01234567" for c in mode):
        raise IMIPListenConfigError("Invalid imip-listen socket mode: %s" % mode)
    return int(mode, 8)


def _resolve_socket_group(group: str) -> int:
    import grp

    try:
        return grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise IMIPListenConfigError(
            "Unknown imip-listen socket group: %s" % group
        ) from exc


async def start_listener(
    target: tuple[str, int] | str,
    handler: IMIPLMTPHandler,
    *,
    socket_mode: str | None = None,
    socket_group: str | None = None,
) -> Listener:
    """Start an LMTP listener bound to *target*.

    *target* is either a filesystem path (Unix socket) or a ``(host, port)``
    tuple. Returns a :class:`Listener` whose ``stop()`` terminates the
    server cleanly.
    """
    # aiosmtpd is an optional dependency. Importing here ensures the package
    # is only required when the user actually opts into --imip-listen.
    from aiosmtpd.lmtp import LMTP

    # Validate config inputs upfront so binding failures don't leave a stray
    # socket on disk.
    socket_gid = _resolve_socket_group(socket_group) if socket_group else None
    socket_mode_int = _parse_socket_mode(socket_mode) if socket_mode else None

    def factory():
        return LMTP(handler, enable_SMTPUTF8=True)

    loop = asyncio.get_running_loop()
    socket_path: str | None = None
    if isinstance(target, tuple):
        host, port = target
        server = await loop.create_server(factory, host=host, port=port)
    else:
        socket_path = target
        # Avoid EADDRINUSE on a stale socket from a previous crash. This
        # mirrors aiohttp's behaviour for unix sites.
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        server = await loop.create_unix_server(factory, path=socket_path)
        if socket_gid is not None:
            os.chown(socket_path, -1, socket_gid)
        if socket_mode_int is not None:
            os.chmod(socket_path, socket_mode_int)

    return Listener(server, socket_path)


def add_arguments(parser) -> None:
    """Register ``--imip-listen`` arguments on *parser*."""
    group = parser.add_argument_group(title="iMIP Listener Options")
    group.add_argument(
        "--imip-listen",
        dest="imip_listen",
        default=os.environ.get("XANDIKOS_IMIP_LISTEN"),
        help=(
            "Listen for inbound iMIP messages over LMTP. Pass "
            "unix:/path/to/sock for a Unix domain socket, or host:port "
            "for TCP. Sieve/Postfix can deliver to this endpoint instead "
            "of piping to 'xandikos import-imip'."
        ),
    )
    group.add_argument(
        "--imip-listen-mode",
        dest="imip_listen_mode",
        default=os.environ.get("XANDIKOS_IMIP_LISTEN_MODE"),
        help=(
            "File mode (octal, e.g. 660) for the iMIP Unix socket. "
            "Ignored for TCP listeners."
        ),
    )
    group.add_argument(
        "--imip-listen-group",
        dest="imip_listen_group",
        default=os.environ.get("XANDIKOS_IMIP_LISTEN_GROUP"),
        help=("Group ownership for the iMIP Unix socket. Ignored for TCP listeners."),
    )
