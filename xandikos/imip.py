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

"""iMIP (RFC 6047) message parsing and construction.

iMIP is the email transport for iTIP messages. This module deliberately
stays independent of WebDAV resources: callers provide or receive parsed
``icalendar.Calendar`` objects and handle storage/routing themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, make_msgid

from icalendar.cal import Calendar

from xandikos import itip


SUPPORTED_METHODS = frozenset({"REQUEST", "REPLY", "CANCEL"})


class InvalidIMIPMessage(Exception):
    """The supplied email does not contain a usable iMIP payload."""


@dataclass(frozen=True)
class IMIPPayload:
    """Parsed iMIP payload extracted from an email message."""

    calendar: Calendar
    calendar_data: bytes
    method: str


def parse_message(data: bytes) -> EmailMessage:
    """Parse a raw RFC 5322 email message."""
    msg = BytesParser(policy=policy.default).parsebytes(data)
    if not isinstance(msg, EmailMessage):
        raise InvalidIMIPMessage("Message is not an EmailMessage")
    return msg


def extract_payload(message: Message) -> IMIPPayload:
    """Extract and parse the first ``text/calendar`` iMIP part from *message*.

    Raises:
      InvalidIMIPMessage: if no calendar part exists, the calendar is
        malformed, or the declared MIME method conflicts with the iCalendar
        ``METHOD`` property.
    """
    part = _find_calendar_part(message)
    if part is None:
        raise InvalidIMIPMessage("Message does not contain a text/calendar part")

    calendar_data = _part_payload_bytes(part)
    try:
        calendar = Calendar.from_ical(calendar_data)
    except ValueError as exc:
        raise InvalidIMIPMessage(f"Invalid iCalendar payload: {exc}") from exc
    if not isinstance(calendar, Calendar):
        raise InvalidIMIPMessage("iMIP payload is not a VCALENDAR")

    calendar_method = _calendar_method(calendar)
    mime_method = part.get_param("method", header="content-type")
    if mime_method is not None:
        mime_method = mime_method.upper()
        if calendar_method != mime_method:
            raise InvalidIMIPMessage(
                "MIME method %r does not match iCalendar METHOD %r"
                % (mime_method, calendar_method)
            )

    return IMIPPayload(calendar, calendar_data, calendar_method)


def extract_payload_from_bytes(data: bytes) -> IMIPPayload:
    """Parse *data* as an email message and extract its iMIP payload."""
    return extract_payload(parse_message(data))


def calendar_user_addresses_from_message(message: Message) -> set[str]:
    """Return ``mailto:`` calendar-user addresses named by mail headers.

    Recipient-like headers are used for routing inbound messages to a local
    principal. The returned addresses are lower-cased because mail transport
    addresses are generally matched case-insensitively in user configuration.
    """
    values: list[str] = []
    for header in ("To", "Cc", "Bcc", "Delivered-To", "Envelope-To", "X-Original-To"):
        value = message.get_all(header, [])
        values.extend(value)
    return {_mailto(addr) for _name, addr in getaddresses(values) if addr}


def target_calendar_user_addresses(payload: IMIPPayload) -> set[str]:
    """Return the calendar-user addresses that should receive *payload*.

    For inbound import, REQUEST and CANCEL messages target ATTENDEE values;
    REPLY messages target ORGANIZER values.
    """
    targets: set[str] = set()
    for comp in payload.calendar.subcomponents:
        if comp.name not in itip.SCHEDULING_COMPONENTS and comp.name != "VFREEBUSY":
            continue
        if payload.method == "REPLY":
            organizer = comp.get("ORGANIZER")
            if organizer is not None:
                targets.add(str(organizer))
            continue
        attendees = comp.get("ATTENDEE", [])
        if not isinstance(attendees, list):
            attendees = [attendees]
        for attendee in attendees:
            targets.add(str(attendee))
    return targets


def candidate_calendar_user_addresses(message: Message, payload: IMIPPayload) -> set[str]:
    """Return all plausible local calendar-user addresses for inbound routing."""
    addresses = calendar_user_addresses_from_message(message)
    addresses.update(target_calendar_user_addresses(payload))
    return addresses


def build_message(
    calendar: Calendar,
    sender: str,
    recipient: str,
    *,
    subject: str | None = None,
    prodid: str | None = None,
) -> EmailMessage:
    """Build a MIME iMIP email for *calendar*.

    The resulting message contains a ``text/calendar`` body with a ``method``
    parameter as required by RFC 6047. It does not send the email; SMTP
    transport code can use this as its serialization boundary.
    """
    method = _calendar_method(calendar)
    calendar_data = calendar.to_ical()

    msg = EmailMessage(policy=policy.default)
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject or _default_subject(calendar, method)
    msg["Message-ID"] = make_msgid()
    if prodid is not None:
        msg["X-Xandikos-Prodid"] = prodid
    msg.set_content(
        calendar_data.decode("utf-8"),
        subtype="calendar",
        charset="utf-8",
        params={"method": method},
    )
    return msg


def _find_calendar_part(message: Message) -> Message | None:
    if message.get_content_type() == "text/calendar":
        return message
    if not message.is_multipart():
        return None
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.get_content_type() == "text/calendar":
            return part
    return None


def _part_payload_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload
    raw = part.get_payload()
    if isinstance(raw, str):
        charset = part.get_content_charset() or "utf-8"
        return raw.encode(charset)
    raise InvalidIMIPMessage("Calendar part has no decodable payload")


def _calendar_method(calendar: Calendar) -> str:
    method = calendar.get("METHOD")
    if method is None:
        raise InvalidIMIPMessage("VCALENDAR is missing METHOD")
    method_text = str(method).upper()
    if method_text not in SUPPORTED_METHODS:
        raise InvalidIMIPMessage("Unsupported iMIP METHOD %r" % method_text)
    return method_text


def _mailto(address: str) -> str:
    return "mailto:" + address.lower()


def _default_subject(calendar: Calendar, method: str) -> str:
    summary = None
    for comp in calendar.subcomponents:
        if comp.name in itip.SCHEDULING_COMPONENTS:
            summary = comp.get("SUMMARY")
            break
    if summary is None:
        return "Calendar %s" % method.lower()
    return "Calendar %s: %s" % (method.lower(), summary)
