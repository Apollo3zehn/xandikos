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

"""Tests for xandikos.webcal."""

import os
import shutil
import tempfile
import unittest

from xandikos.icalendar import ICalendarFile
from xandikos.store.git import TreeGitStore
from xandikos.webcal import merge_store_calendar

EVENT_ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Test//Test//EN\r
BEGIN:VEVENT\r
UID:one@example.com\r
DTSTAMP:20260101T120000Z\r
DTSTART:20260601T100000Z\r
DTEND:20260601T110000Z\r
SUMMARY:One\r
END:VEVENT\r
END:VCALENDAR\r
"""

TODO_ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Test//Test//EN\r
BEGIN:VTODO\r
UID:two@example.com\r
DTSTAMP:20260101T120000Z\r
SUMMARY:Two\r
END:VTODO\r
END:VCALENDAR\r
"""

TIMEZONE_VCAL = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Test//Test//EN\r
BEGIN:VTIMEZONE\r
TZID:Europe/Amsterdam\r
BEGIN:STANDARD\r
DTSTART:19701025T030000\r
TZOFFSETFROM:+0200\r
TZOFFSETTO:+0100\r
END:STANDARD\r
END:VTIMEZONE\r
END:VCALENDAR\r
"""

EVENT_IN_TZ = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Test//Test//EN\r
BEGIN:VTIMEZONE\r
TZID:Europe/Amsterdam\r
BEGIN:STANDARD\r
DTSTART:19701025T030000\r
TZOFFSETFROM:+0200\r
TZOFFSETTO:+0100\r
END:STANDARD\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
UID:tz-one@example.com\r
DTSTAMP:20260101T120000Z\r
DTSTART;TZID=Europe/Amsterdam:20260601T100000\r
DTEND;TZID=Europe/Amsterdam:20260601T110000\r
SUMMARY:Tz one\r
END:VEVENT\r
END:VCALENDAR\r
"""


class MergeStoreCalendarTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tempdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tempdir)
        self.store = TreeGitStore.create(os.path.join(self.tempdir, "c"))
        self.store.load_extra_file_handler(ICalendarFile)

    def test_empty_store(self):
        merged = merge_store_calendar(self.store)
        self.assertEqual("VCALENDAR", merged.name)
        self.assertEqual("2.0", str(merged["VERSION"]))
        self.assertEqual([], list(merged.subcomponents))

    def test_metadata_headers(self):
        merged = merge_store_calendar(
            self.store, displayname="My Cal", description="Notes"
        )
        self.assertEqual("My Cal", str(merged["X-WR-CALNAME"]))
        self.assertEqual("Notes", str(merged["X-WR-CALDESC"]))

    def test_merges_subcomponents(self):
        self.store.import_one("a.ics", "text/calendar", [EVENT_ICS])
        self.store.import_one("b.ics", "text/calendar", [TODO_ICS])
        merged = merge_store_calendar(self.store)
        names = sorted(c.name for c in merged.subcomponents)
        self.assertEqual(["VEVENT", "VTODO"], names)
        uids = sorted(str(c["UID"]) for c in merged.subcomponents)
        self.assertEqual(["one@example.com", "two@example.com"], uids)

    def test_dedupes_vtimezone_across_members(self):
        self.store.import_one("a.ics", "text/calendar", [EVENT_IN_TZ])
        second = EVENT_IN_TZ.replace(b"tz-one@", b"tz-two@")
        self.store.import_one("b.ics", "text/calendar", [second])
        merged = merge_store_calendar(self.store)
        vtimezones = [c for c in merged.subcomponents if c.name == "VTIMEZONE"]
        self.assertEqual(1, len(vtimezones))
        self.assertEqual("Europe/Amsterdam", str(vtimezones[0]["TZID"]))

    def test_seeded_timezone_used_and_deduped(self):
        self.store.import_one("a.ics", "text/calendar", [EVENT_IN_TZ])
        merged = merge_store_calendar(self.store, timezone=TIMEZONE_VCAL)
        vtimezones = [c for c in merged.subcomponents if c.name == "VTIMEZONE"]
        self.assertEqual(1, len(vtimezones))

    def test_invalid_timezone_is_skipped(self):
        merged = merge_store_calendar(self.store, timezone="not iCalendar")
        self.assertEqual([], list(merged.subcomponents))

    def test_skips_non_calendar_members(self):
        self.store.import_one("note.txt", "text/plain", [b"hello"])
        self.store.import_one("a.ics", "text/calendar", [EVENT_ICS])
        merged = merge_store_calendar(self.store)
        names = [c.name for c in merged.subcomponents]
        self.assertEqual(["VEVENT"], names)
