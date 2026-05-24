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

"""Read-only webcal export.

Merges every member of a calendar store into a single VCALENDAR for
clients that subscribe to a calendar by URL (e.g. Google Calendar's
"Add by URL"). The export is not CalDAV; subscribers cannot create or
modify events through it.
"""

from logging import getLogger

from icalendar.cal import Calendar

from .caldav import PRODID
from .icalendar import ICalendarFile
from .store import InvalidFileContents, Store

logger = getLogger(__name__)


def merge_store_calendar(
    store: Store,
    *,
    displayname: str | None = None,
    description: str | None = None,
    timezone: str | None = None,
) -> Calendar:
    """Return a single VCALENDAR with every member's subcomponents.

    Args:
      store: Calendar store to read members from.
      displayname: Optional value for X-WR-CALNAME.
      description: Optional value for X-WR-CALDESC.
      timezone: Optional iCalendar string containing one or more
        VTIMEZONE components to seed the merged calendar with.

    VTIMEZONE components are de-duplicated by TZID across the seed
    timezone and all members. Members that cannot be parsed are logged
    and skipped rather than aborting the whole export.
    """
    merged = Calendar()
    merged["VERSION"] = "2.0"
    merged["PRODID"] = PRODID
    if displayname:
        merged.add("X-WR-CALNAME", displayname)
    if description:
        merged.add("X-WR-CALDESC", description)

    seen_timezones: set[str] = set()
    if timezone:
        try:
            tz_cal = Calendar.from_ical(timezone)
        except ValueError:
            logger.warning("Unable to parse calendar timezone, skipping.")
        else:
            for comp in tz_cal.subcomponents:
                if comp.name != "VTIMEZONE":
                    continue
                tzid = str(comp.get("TZID", ""))
                if tzid and tzid in seen_timezones:
                    continue
                if tzid:
                    seen_timezones.add(tzid)
                merged.add_component(comp)

    for name, content_type, etag in store.iter_with_etag():
        if content_type != "text/calendar":
            continue
        try:
            file = store.get_file(name, content_type, etag)
        except KeyError:
            continue
        if not isinstance(file, ICalendarFile):
            continue
        try:
            cal = file.calendar
        except InvalidFileContents:
            logger.warning("Unable to parse %s, skipping.", name)
            continue
        for comp in cal.subcomponents:
            if comp.name == "VTIMEZONE":
                tzid = str(comp.get("TZID", ""))
                if tzid and tzid in seen_timezones:
                    continue
                if tzid:
                    seen_timezones.add(tzid)
            merged.add_component(comp)
    return merged
