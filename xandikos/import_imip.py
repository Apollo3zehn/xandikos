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

"""Command support for importing iMIP email messages."""

import logging
import posixpath
import sys
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree as ET


def add_parser(parser):
    """Add arguments for the import-imip subcommand."""
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "-d",
        "--directory",
        type=str,
        help="Root directory containing collections",
    )
    target.add_argument(
        "--server-url",
        type=str,
        help="Schedule inbox URL to POST the extracted iTIP text/calendar data to.",
    )
    target.add_argument(
        "--principal-url",
        type=str,
        help="Principal URL whose schedule-inbox-URL should be discovered.",
    )
    parser.add_argument(
        "--principal",
        type=str,
        default="/user/",
        help="Principal path whose schedule inbox should receive the message. [%(default)s]",
    )
    parser.add_argument(
        "--autocreate",
        action="store_true",
        help="Create the principal, default calendar, and schedule inbox if missing.",
    )
    parser.add_argument(
        "--username",
        type=str,
        help="Username for HTTP Basic authentication with --server-url.",
    )
    parser.add_argument(
        "--password-file",
        type=str,
        help="File containing the HTTP Basic authentication password.",
    )
    parser.add_argument(
        "--unix-socket",
        type=str,
        help="Unix domain socket path for HTTP requests to the Xandikos server.",
    )


async def main(args, parser, data: bytes | None = None):
    """Import a raw iMIP email message into a principal's schedule inbox."""
    from . import imip

    logger = logging.getLogger(__name__)

    if data is None:
        data = sys.stdin.buffer.read()
    assert isinstance(data, bytes)
    try:
        message = imip.parse_message(data)
    except imip.InvalidIMIPMessage as exc:
        logger.error("Could not parse incoming message: %s", exc)
        return 1
    msg_id = _describe_message(message)
    if imip.is_auto_submitted(message):
        # RFC 3834: don't bounce server-generated iTIP back into the
        # mailbox it came from. Returns 0 because the Sieve hook
        # treated this message correctly by handing it to us.
        logger.info(
            "Skipping auto-submitted message %s (Auto-Submitted: %s)",
            msg_id,
            message.get("Auto-Submitted"),
        )
        return 0
    try:
        payload = imip.extract_payload(message)
    except imip.InvalidIMIPMessage as exc:
        candidates = sorted(imip.calendar_user_addresses_from_message(message))
        logger.error(
            "Invalid iMIP payload in %s: %s (recipient headers: %s)",
            msg_id,
            exc,
            ", ".join(candidates) if candidates else "(none)",
        )
        return 1

    if args.server_url or args.principal_url:
        return await _import_imip_to_server(args, payload, msg_id)
    return await _import_imip_to_directory(args, payload, msg_id)


def _describe_message(message) -> str:
    """Return a short human-readable identifier for *message* for logs."""
    msg_id = message.get("Message-ID") or "(no Message-ID)"
    subject = message.get("Subject")
    if subject:
        return f"{msg_id} ({subject!r})"
    return str(msg_id)


async def _import_imip_to_directory(args, payload, msg_id: str) -> int:
    from . import web

    logger = logging.getLogger(__name__)

    principal_path = _normalise_principal_path(args.principal)
    backend = web.SingleUserFilesystemBackend(args.directory)
    backend._mark_as_principal(principal_path)
    principal = backend.get_resource(principal_path)
    if principal is None:
        if not args.autocreate:
            logger.error(
                "Cannot import %s iMIP %s: principal %s does not exist; "
                "pass --autocreate to create it.",
                msg_id,
                payload.method,
                principal_path,
            )
            return 1
        backend.create_principal(principal_path, create_defaults=True)
        principal = backend.get_resource(principal_path)

    if not isinstance(principal, web.Principal):
        logger.error(
            "Cannot import %s iMIP %s: %s is not a principal.",
            msg_id,
            payload.method,
            principal_path,
        )
        return 1

    inbox_path = posixpath.join(principal_path, principal.get_schedule_inbox_url())
    inbox = backend.get_resource(inbox_path)
    if not isinstance(inbox, web.ScheduleInbox) and args.autocreate:
        web.create_principal_defaults(backend, principal)
        inbox = backend.get_resource(inbox_path)
    if not isinstance(inbox, web.ScheduleInbox):
        logger.error(
            "Cannot import %s iMIP %s: %s is not a schedule inbox; "
            "pass --autocreate or create defaults first.",
            msg_id,
            payload.method,
            inbox_path,
        )
        return 1

    try:
        name, _etag = await inbox.create_member(
            None,
            [payload.calendar_data],
            "text/calendar",
            requester="xandikos import-imip",
        )
    except Exception as exc:
        logger.error(
            "Failed to store %s iMIP %s in %s: %s",
            msg_id,
            payload.method,
            inbox_path,
            exc,
        )
        return 1

    logger.info(
        "Imported iMIP %s message %s into %s/%s.",
        payload.method,
        msg_id,
        inbox_path.rstrip("/"),
        name,
    )
    return 0


async def _import_imip_to_server(args, payload, msg_id: str) -> int:
    logger = logging.getLogger(__name__)
    password = _read_password_file(args.password_file)

    try:
        server_url = args.server_url
        if server_url is None:
            server_url = await _discover_schedule_inbox_url(
                args.principal_url,
                username=args.username,
                password=password,
                unix_socket=args.unix_socket,
            )
        await _post_itip_to_server(
            server_url,
            payload.calendar_data,
            username=args.username,
            password=password,
            unix_socket=args.unix_socket,
        )
    except Exception as exc:
        target_url = args.server_url or args.principal_url
        logger.error("Unable to POST iTIP message to %s: %s", target_url, exc)
        return 1

    logger.info("Posted iMIP %s message to %s.", payload.method, server_url)
    return 0


async def _post_itip_to_server(
    server_url: str,
    calendar_data: bytes,
    *,
    username: str | None = None,
    password: str | None = None,
    unix_socket: str | None = None,
) -> None:
    import aiohttp

    parsed = urlparse(server_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("server URL must use http or https")

    auth = None
    if username is not None:
        auth = aiohttp.BasicAuth(username, password or "")
    connector = _aiohttp_connector(unix_socket)
    async with aiohttp.ClientSession(auth=auth, connector=connector) as session:
        async with session.post(
            server_url,
            data=calendar_data,
            headers={"Content-Type": "text/calendar"},
        ) as response:
            if 200 <= response.status < 300:
                return
            body = await response.text()
            raise RuntimeError(
                "server returned HTTP %d %s: %s"
                % (response.status, response.reason, body.strip())
            )


async def _discover_schedule_inbox_url(
    principal_url: str,
    *,
    username: str | None = None,
    password: str | None = None,
    unix_socket: str | None = None,
) -> str:
    import aiohttp

    parsed = urlparse(principal_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("principal URL must use http or https")

    auth = None
    if username is not None:
        auth = aiohttp.BasicAuth(username, password or "")
    connector = _aiohttp_connector(unix_socket)
    body = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        b"<D:prop><C:schedule-inbox-URL/></D:prop>"
        b"</D:propfind>"
    )
    async with aiohttp.ClientSession(auth=auth, connector=connector) as session:
        async with session.request(
            "PROPFIND",
            principal_url,
            data=body,
            headers={"Content-Type": "application/xml", "Depth": "0"},
        ) as response:
            response_body = await response.read()
            if not 200 <= response.status < 300:
                text = response_body.decode("utf-8", "replace").strip()
                raise RuntimeError(
                    "server returned HTTP %d %s: %s"
                    % (response.status, response.reason, text)
                )

    try:
        root = ET.fromstring(response_body)
    except ET.ParseError as exc:
        raise RuntimeError("invalid PROPFIND response XML: %s" % exc) from exc
    href = root.find(".//{urn:ietf:params:xml:ns:caldav}schedule-inbox-URL/{DAV:}href")
    if href is None or not href.text:
        raise RuntimeError("PROPFIND response did not contain schedule-inbox-URL")
    return urljoin(principal_url, unquote(href.text))


def _aiohttp_connector(unix_socket: str | None):
    if unix_socket is None:
        return None
    import aiohttp

    return aiohttp.UnixConnector(path=unix_socket)


def _read_password_file(path: str | None) -> str | None:
    if path is None:
        return None
    with open(path) as f:
        return f.read().strip()


def _normalise_principal_path(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return posixpath.normpath(path)
