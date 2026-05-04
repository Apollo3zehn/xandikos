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

"""Tests for xandikos.imip (RFC 6047 iMIP email transport helpers)."""

import unittest
from email.message import EmailMessage

from icalendar.cal import Calendar

from xandikos import imip


REQUEST = b"""\
BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Test//EN\r
METHOD:REQUEST\r
BEGIN:VEVENT\r
UID:meeting@example.com\r
DTSTAMP:20260101T000000Z\r
DTSTART:20260601T100000Z\r
DTEND:20260601T110000Z\r
SUMMARY:Sync\r
ORGANIZER:mailto:alice@example.com\r
ATTENDEE:mailto:bob@example.com\r
END:VEVENT\r
END:VCALENDAR\r
"""


REPLY = b"""\
BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Test//EN\r
METHOD:REPLY\r
BEGIN:VEVENT\r
UID:meeting@example.com\r
DTSTAMP:20260101T000000Z\r
DTSTART:20260601T100000Z\r
DTEND:20260601T110000Z\r
SUMMARY:Sync\r
ORGANIZER:mailto:alice@example.com\r
ATTENDEE;PARTSTAT=ACCEPTED:mailto:bob@example.com\r
END:VEVENT\r
END:VCALENDAR\r
"""


def _message(calendar_data=REQUEST, method="REQUEST"):
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "Bob <bob@example.com>"
    msg.set_content(
        calendar_data.decode("utf-8"),
        subtype="calendar",
        charset="utf-8",
        params={"method": method},
    )
    return msg


class ExtractPayloadTests(unittest.TestCase):
    def test_extracts_text_calendar_payload(self):
        payload = imip.extract_payload(_message())

        self.assertEqual("REQUEST", payload.method)
        self.assertIn(b"METHOD:REQUEST", payload.calendar_data)
        self.assertEqual("REQUEST", str(payload.calendar["METHOD"]))

    def test_extracts_nested_calendar_payload(self):
        msg = EmailMessage()
        msg["From"] = "Alice <alice@example.com>"
        msg["To"] = "Bob <bob@example.com>"
        msg.set_content("Invitation attached")
        msg.add_alternative(
            REQUEST.decode("utf-8"),
            subtype="calendar",
            charset="utf-8",
            params={"method": "REQUEST"},
        )

        payload = imip.extract_payload(msg)

        self.assertEqual("REQUEST", payload.method)

    def test_rejects_missing_calendar_part(self):
        msg = EmailMessage()
        msg.set_content("hello")

        with self.assertRaises(imip.InvalidIMIPMessage):
            imip.extract_payload(msg)

    def test_rejects_method_mismatch(self):
        with self.assertRaises(imip.InvalidIMIPMessage):
            imip.extract_payload(_message(method="CANCEL"))

    def test_rejects_missing_method(self):
        body = REQUEST.replace(b"METHOD:REQUEST\r\n", b"")

        with self.assertRaises(imip.InvalidIMIPMessage):
            imip.extract_payload(_message(calendar_data=body))

    def test_extract_payload_from_bytes(self):
        payload = imip.extract_payload_from_bytes(_message().as_bytes())

        self.assertEqual("REQUEST", payload.method)


class AddressRoutingTests(unittest.TestCase):
    def test_calendar_user_addresses_from_message(self):
        msg = _message()
        msg["Cc"] = "Carol <Carol@Example.COM>"
        msg["Delivered-To"] = "bob@example.com"

        self.assertEqual(
            {"mailto:bob@example.com", "mailto:carol@example.com"},
            imip.calendar_user_addresses_from_message(msg),
        )

    def test_request_targets_attendees(self):
        payload = imip.extract_payload(_message())

        self.assertEqual(
            {"mailto:bob@example.com"},
            imip.target_calendar_user_addresses(payload),
        )

    def test_reply_targets_organizer(self):
        payload = imip.extract_payload(_message(calendar_data=REPLY, method="REPLY"))

        self.assertEqual(
            {"mailto:alice@example.com"},
            imip.target_calendar_user_addresses(payload),
        )

    def test_candidate_addresses_include_headers_and_calendar_targets(self):
        msg = _message()
        msg.replace_header("To", "different@example.com")
        payload = imip.extract_payload(msg)

        self.assertEqual(
            {"mailto:different@example.com", "mailto:bob@example.com"},
            imip.candidate_calendar_user_addresses(msg, payload),
        )


class BuildMessageTests(unittest.TestCase):
    def test_builds_text_calendar_message(self):
        calendar = Calendar.from_ical(REQUEST)
        msg = imip.build_message(
            calendar,
            "alice@example.com",
            "bob@example.com",
        )

        self.assertEqual("alice@example.com", msg["From"])
        self.assertEqual("bob@example.com", msg["To"])
        self.assertEqual("text/calendar", msg.get_content_type())
        self.assertEqual("REQUEST", msg.get_param("method", header="content-type"))
        self.assertIn("Calendar request: Sync", msg["Subject"])

    def test_build_message_requires_supported_method(self):
        calendar = Calendar.from_ical(REQUEST.replace(b"METHOD:REQUEST", b"METHOD:PUBLISH"))

        with self.assertRaises(imip.InvalidIMIPMessage):
            imip.build_message(calendar, "alice@example.com", "bob@example.com")


if __name__ == "__main__":
    unittest.main()
